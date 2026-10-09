"""R8-S3 专项测试：权威位置切换在 3.6.19 起**只提示、不阻断**。

背景：3.6.18 及以前，「另一权威位置还有当前账号的活跃未决记录」会 fail-closed
阻断整批（``authority-location-guard`` / ``blocked_uncertain``，要求人工核对后
才能重跑）。3.6.19 把这套「人工核对后才能重跑」的闸门下线：切换/取消显式权威
位置、登记文件损坏/不可读/不可写/未知版本/非法条目等，都只打印「提示：…」日志
行，本批照常提交（提交前仍做站内只读对账，站内已有的订单不会重复提交）。

本文件锁定回退后仍然保留的不变量（C1–C5 + 故障 + 重启/并发）：

* 位置切换只提示：第二轮照常提交（POST 增量 == 本轮任务数，通常 1），
  状态是 confirmed/unconfirmed（取决于站内是否可见），不再是 blocked/failed；
* 只读核对：其他位置/旧写法文件里的记录不被迁移、改写、删除、标 verified，
  字节保持原样（若该文件同时是当前权威 journal，则断言到**记录级**不变）；
* 账号隔离：其他账号在其他位置的活跃记录仍不影响本账号，且原样保留；
* 同作用域（同一权威 journal + 同一 batch_key）的**跨进程批次锁**仍生效：
  并发第二个进程被拒（``blocked_concurrent`` / ``batch-submission-lock``、0 POST），
  同批次绝不双发；跨位置的并发不再互相阻断，各自写各自的作用域；
* 登记锚点仍在 POST 之前落盘（正常路径），已消失的登记位置仍被清理并报告；
* 非规范 URL 写法仍是配置错误（R8-S1 文件覆盖，本文件不重复）。

隔离：数据目录（登记锚点）、默认权威根、锁目录、临时目录全部在 ``tmp_path``；
请求只发往 ``127.0.0.1`` 本地模拟平台，网络守卫拒绝非回环连接。
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.ordering import uncertain as sss_uncertain
from tests.r8s3_location_guard_harness import (
    ACCOUNT,
    MockPlatform,
    active_records,
    child_result,
    loopback_guard,
    make_location_config,
    read_registry,
    registered_paths,
    registry_path,
    run_location_job,
    spawn_location_child,
    write_registry,
    write_unresolved_record,
)

OTHER_ACCOUNT = "18758187002"
FIXED_NOW = (2026, 9, 16, 10, 0, 0)
# 子进程的批次/时钟与 R8-S1 装置一致（固定时钟 2026-09-16 10:00，excel 源）
BATCH_KEY = "2026-09-16|18758187001"


@pytest.fixture(autouse=True)
def _isolated_location_state(tmp_path, monkeypatch):
    """每个测试独立的数据/权威根/锁/临时目录 + 登记锚点。"""
    state = tmp_path / "state"
    for key in ("YIKOU_SSS_AUTHORITATIVE_PATH", "YIKOU_SSS_AUTHORITY_LOCATIONS",
                "YIKOU_SSS_UNCERTAIN_PATH", "REQUESTS_CA_BUNDLE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YIKOU_DATA_DIR", str(state / "userdata"))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(state / "auth-default"))
    monkeypatch.setenv("YIKOU_SSS_LOCK_ROOT", str(state / "locks"))
    monkeypatch.setenv("YIKOU_SSS_JOURNAL_LOCK_TIMEOUT", "3")
    monkeypatch.setenv("TMPDIR", str(state / "tmp"))
    (state / "tmp").mkdir(parents=True, exist_ok=True)
    return state


def _default_root() -> Path:
    return Path(os.environ["YIKOU_SSS_AUTHORITATIVE_ROOT"])


def _empty_journal(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "records": []}), encoding="utf-8")
    return path


@contextmanager
def _platform(*, list_visible: bool = False):
    platform = MockPlatform(list_visible=list_visible)
    platform.start()
    try:
        with loopback_guard(platform.port):
            yield platform
    finally:
        platform.stop()


def _url(platform: MockPlatform) -> str:
    return f"http://sss.example.invalid:{platform.port}"


def _run(platform: MockPlatform, work: Path, *, explicit: str | None = None,
         env_path: str | None = None, monkeypatch=None,
         list_visible: bool | None = None) -> dict:
    """跑一轮真实 run_sss_job；返回状态/本次 POST 数/提示日志/结果。"""
    if list_visible is not None:
        platform.list_visible = list_visible
    if monkeypatch is not None:
        if env_path:
            monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_PATH", env_path)
        else:
            monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    sub = work / f"run-{uuid.uuid4().hex[:6]}"
    sub.mkdir(parents=True, exist_ok=True)
    config = make_location_config(sub, _url(platform),
                                  authoritative_path=explicit)
    before = platform.count
    messages: list[str] = []
    try:
        result = run_location_job(config, messages=messages)
        status = str(result.get("status"))
    except Exception as exc:  # noqa: BLE001 - 测试要如实看到异常类型
        result = {"status": f"EXCEPTION:{type(exc).__name__}", "error": str(exc)}
        status = str(result["status"])
    return {"status": status, "semantics": result.get("semantics"),
            "posts": platform.count - before, "messages": messages,
            "result": result}


def _hints(out: dict) -> list[str]:
    return [message for message in out["messages"] if "提示" in message]


def _continued_after_hint(out: dict) -> bool:
    """本轮在给出提示后仍继续提交（3.6.19 契约：登记/位置问题只提示不阻断）。

    位置冲突提示写「本批继续提交」；登记读取/写盘失败提示写「不阻断本批」。
    """
    return any("本批继续提交" in message or "不阻断本批" in message
               for message in out["messages"])


# ---------------------------------------------------------------------------
# 0) 登记锚点与正常路径
# ---------------------------------------------------------------------------
def test_first_run_registers_location_and_submits_exactly_once(tmp_path,
                                                               monkeypatch):
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
        assert out["status"] == "confirmed"
        assert out["posts"] == 1
        assert out["result"]["created"] == 1

    payload = read_registry()
    assert payload.get("version") == 1
    entries = {item["path"]: item for item in payload["locations"]}
    config = make_location_config(tmp_path / "probe", _url(platform))
    journal = str(sss_uncertain.authoritative_uncertain_path(config))
    assert journal in entries
    entry = entries[journal]
    assert entry["kind"] == "default_root"
    assert entry["last_account"] == ACCOUNT
    assert entry["last_origin"] == sss_uncertain.platform_origin(config)


def test_explicit_location_registered_with_explicit_kind(tmp_path, monkeypatch):
    explicit = _empty_journal(tmp_path / "explicit.json")
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", explicit=str(explicit),
                   monkeypatch=monkeypatch)
        assert out["status"] == "confirmed"
        assert out["posts"] == 1
    entry = {item["path"]: item for item in read_registry()["locations"]}
    resolved = str(explicit.resolve())
    assert resolved in entry
    assert entry[resolved]["kind"] == "explicit_file"


# ---------------------------------------------------------------------------
# 1) C1–C5：位置切换只提示，第二轮照常提交（3.6.19 回退）
# ---------------------------------------------------------------------------
def test_c1_explicit_file_with_legacy_record_submits_and_keeps_record(
        tmp_path, monkeypatch):
    """C1：显式文件里已有旧尾点 unresolved —— 3.6.19 起不再阻断，照常提交。

    这条旧记录与本轮自己的记录同住一个权威 journal（它就写在显式文件里），
    因此这里断言到**记录级**：原记录逐字段不变、仍 unresolved、未被删除。
    """
    explicit = tmp_path / "explicit-c1.json"
    before_bytes = write_unresolved_record(
        explicit, journal_id="c1-legacy",
        platform="http://sss.example.invalid.")
    legacy_before = json.loads(before_bytes.decode("utf-8"))["records"][0]
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", explicit=str(explicit),
                   monkeypatch=monkeypatch)

    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert out["result"]["created"] == 1
    payload = json.loads(explicit.read_text(encoding="utf-8"))
    legacy = [item for item in payload["records"]
              if item.get("journal_id") == "c1-legacy"]
    assert legacy == [legacy_before]          # 未被改写、未被删除
    assert [r["status"] for r in active_records(explicit)] == ["unresolved"]
    assert "verified" not in explicit.read_text(encoding="utf-8")


def test_c2_default_unresolved_then_empty_explicit_submits(tmp_path,
                                                           monkeypatch):
    """C2：默认根有 unresolved → 空显式文件：第二轮照常提交（POST=1）。

    位置切换只发「提示：其他权威位置仍有…本批继续提交」日志；旧位置（默认根）
    的记录仍原样保留（只读核对，不迁移、不改写、不删除）。
    """
    root = _default_root()
    with _platform(list_visible=False) as platform:
        seed = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
        assert seed["status"] == "unconfirmed"
        assert seed["posts"] == 1
        journals = sorted(root.glob("*.json"))
        assert len(journals) == 1
        assert len(active_records(journals[0])) == 1
        before = journals[0].read_bytes()

        explicit = _empty_journal(tmp_path / "explicit-c2.json")
        second = _run(platform, tmp_path / "w", explicit=str(explicit),
                      monkeypatch=monkeypatch)

    # 第二轮照常提交：本轮恰好 1 次 POST（旧契约下这里是被闸门阻断的 0 次）
    assert second["status"] == "unconfirmed"
    assert second["posts"] == 1
    assert "blocked" not in second["status"]
    # 只提示不阻断：提示里点名了旧位置，并明确写着本批继续提交
    hints = _hints(second)
    assert any(journals[0].name in message for message in hints), hints
    assert _continued_after_hint(second), second["messages"]
    # 旧位置的记录字节不变、仍 unresolved
    assert journals[0].read_bytes() == before
    assert [r["status"] for r in active_records(journals[0])] == ["unresolved"]


def test_c3_explicit_a_unresolved_then_b_submits(tmp_path, monkeypatch):
    """C3：显式 A 有 unresolved → 显式 B：第二轮照常提交，A 原样保留。"""
    a = tmp_path / "explicit-A.json"
    b = tmp_path / "explicit-B.json"
    with _platform(list_visible=False) as platform:
        first = _run(platform, tmp_path / "w", explicit=str(a),
                     monkeypatch=monkeypatch)
        assert first["status"] == "unconfirmed"
        assert first["posts"] == 1
        assert len(active_records(a)) == 1
        before = a.read_bytes()

        second = _run(platform, tmp_path / "w", explicit=str(b),
                      monkeypatch=monkeypatch)

    assert second["status"] == "unconfirmed"
    assert second["posts"] == 1
    assert _continued_after_hint(second), second["messages"]
    assert a.read_bytes() == before
    assert [r["status"] for r in active_records(a)] == ["unresolved"]
    # B 成为本轮权威位置：本轮自己的未确认记录写在 B（不落到 A）
    assert [r["status"] for r in active_records(b)] == ["unresolved"]


def test_c4_explicit_unresolved_then_drop_override_submits(tmp_path,
                                                           monkeypatch):
    """C4：显式位置有 unresolved → 取消覆盖（回默认根）：第二轮照常提交。"""
    c = tmp_path / "explicit-C.json"
    with _platform(list_visible=False) as platform:
        first = _run(platform, tmp_path / "w", explicit=str(c),
                     monkeypatch=monkeypatch)
        assert first["status"] == "unconfirmed"
        assert first["posts"] == 1
        assert len(active_records(c)) == 1
        before = c.read_bytes()

        second = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)

    assert second["status"] == "unconfirmed"
    assert second["posts"] == 1
    assert _continued_after_hint(second), second["messages"]
    assert c.read_bytes() == before
    assert [r["status"] for r in active_records(c)] == ["unresolved"]


def test_c5_env_override_submits(tmp_path, monkeypatch):
    """C5：环境变量形式的覆盖同样不再阻断（默认根 → 空显式文件）。"""
    root = _default_root()
    with _platform(list_visible=False) as platform:
        seed = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
        assert seed["status"] == "unconfirmed"
        assert seed["posts"] == 1
        journals = sorted(root.glob("*.json"))
        assert len(journals) == 1
        before = journals[0].read_bytes()

        explicit = _empty_journal(tmp_path / "explicit-c5.json")
        second = _run(platform, tmp_path / "w", env_path=str(explicit),
                      monkeypatch=monkeypatch)

    assert second["status"] == "unconfirmed"
    assert second["posts"] == 1
    assert _continued_after_hint(second), second["messages"]
    assert journals[0].read_bytes() == before
    assert len(active_records(journals[0])) == 1


def test_c5_env_override_a_to_b_submits(tmp_path, monkeypatch):
    """C5 补充：环境变量形式显式 A（有未决）→ 环境变量形式显式 B：照常提交。"""
    a = tmp_path / "env-A.json"
    b = tmp_path / "env-B.json"
    with _platform(list_visible=False) as platform:
        first = _run(platform, tmp_path / "w", env_path=str(a),
                     monkeypatch=monkeypatch)
        assert first["status"] == "unconfirmed"
        assert first["posts"] == 1
        before = a.read_bytes()

        second = _run(platform, tmp_path / "w", env_path=str(b),
                      monkeypatch=monkeypatch)

    assert second["status"] == "unconfirmed"
    assert second["posts"] == 1
    assert _continued_after_hint(second), second["messages"]
    assert a.read_bytes() == before
    assert [r["status"] for r in active_records(a)] == ["unresolved"]


# ---------------------------------------------------------------------------
# 2) 两个入口与优先级
# ---------------------------------------------------------------------------
def test_config_field_takes_precedence_over_env(tmp_path, monkeypatch):
    """config 属性优先于环境变量；两者都只是“位置”，不会被合并身份。"""
    env_file = tmp_path / "env-override.json"
    env_before = write_unresolved_record(env_file, journal_id="env-record")
    config_file = _empty_journal(tmp_path / "config-override.json")
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_PATH", str(env_file))
    config = make_location_config(tmp_path / "cfg", _url_placeholder(),
                                  authoritative_path=str(config_file))
    assert sss_uncertain.authoritative_uncertain_path(config) \
        == config_file.resolve()

    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", explicit=str(config_file),
                   monkeypatch=None)
        assert out["status"] == "confirmed"
        assert out["posts"] == 1

    # 未登记的旧环境变量文件既不改写也不被发现（升级边界见专门测试）
    assert env_file.read_bytes() == env_before
    assert str(env_file.resolve()) not in registered_paths()
    assert str(config_file.resolve()) in registered_paths()


def _url_placeholder() -> str:
    return "http://sss.example.invalid:1"


def test_registry_path_env_override_is_used(tmp_path, monkeypatch):
    """`YIKOU_SSS_AUTHORITY_LOCATIONS` 可指定登记文件（运维声明旧位置/测试隔离）。"""
    custom = tmp_path / "custom-registry.json"
    monkeypatch.setenv("YIKOU_SSS_AUTHORITY_LOCATIONS", str(custom))
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert custom.exists()
    default_path = Path(os.environ["YIKOU_DATA_DIR"]) / "sss-authority-locations.json"
    assert not default_path.exists()


# ---------------------------------------------------------------------------
# 3) 登记文件故障：只提示、不阻断，且不破坏原文件（3.6.19）
# ---------------------------------------------------------------------------
def test_corrupt_registry_does_not_block_submission(tmp_path, monkeypatch):
    """登记文件损坏：不再 fail-closed 阻断，本批照常提交；原文件不被破坏。"""
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not-json 张三 13800000001", encoding="utf-8")
    before = target.read_bytes()
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert out["result"]["created"] == 1
    assert _continued_after_hint(out), out["messages"]
    assert target.read_bytes() == before


def test_unsupported_registry_version_does_not_block_submission(tmp_path,
                                                                monkeypatch):
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 99, "locations": []}),
                      encoding="utf-8")
    before = target.read_bytes()
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert _continued_after_hint(out), out["messages"]
    assert target.read_bytes() == before


def test_registry_path_is_directory_does_not_block_submission(tmp_path,
                                                              monkeypatch):
    target = registry_path()
    target.mkdir(parents=True, exist_ok=True)
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert _continued_after_hint(out), out["messages"]
    assert target.is_dir()


def test_unreadable_registry_does_not_block_submission(tmp_path, monkeypatch):
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 1, "locations": []}),
                      encoding="utf-8")
    before = target.read_bytes()
    target.chmod(0o000)
    try:
        with _platform(list_visible=True) as platform:
            out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    finally:
        target.chmod(0o600)
    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert _continued_after_hint(out), out["messages"]
    assert target.read_bytes() == before


def test_registry_write_failure_no_longer_blocks_before_post(tmp_path,
                                                             monkeypatch):
    """登记写失败不再阻断提交（3.6.19）：只提示，提交照常，原文件不被破坏。

    精确构造「登记写失败」：只让登记路径的原子写抛 ``UncertainJournalError``
    （journal 的读写不受影响），验证提交不被阻断、登记文件保持原样。
    """
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 1, "locations": []}),
                      encoding="utf-8")
    before = target.read_bytes()
    real_atomic_write = sss_uncertain._atomic_write

    def _fail_registry_write_only(path, payload):
        if Path(path).name == target.name:
            raise sss_uncertain.UncertainJournalError("注入的登记写失败（测试）")
        return real_atomic_write(path, payload)

    monkeypatch.setattr(sss_uncertain, "_atomic_write",
                        _fail_registry_write_only)
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert _continued_after_hint(out), out["messages"]
    assert target.read_bytes() == before


def test_invalid_registry_entries_do_not_block_submission(tmp_path,
                                                          monkeypatch):
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    for payload in ({"version": 1, "locations": "not-a-list"},
                    {"version": 1, "locations": [{"path": ""}]},
                    {"version": 1, "locations": ["not-an-object"]}):
        target.write_text(json.dumps(payload), encoding="utf-8")
        before = target.read_bytes()
        with _platform(list_visible=True) as platform:
            out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
        assert out["status"] == "confirmed", payload
        assert out["posts"] == 1, payload
        assert _continued_after_hint(out), payload
        assert target.read_bytes() == before, payload


def test_vanished_registered_location_is_pruned_and_reported(tmp_path,
                                                             monkeypatch):
    gone = tmp_path / "gone" / "old-authority.json"
    target = write_registry(None, [str(gone)])
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
        assert out["status"] == "confirmed"
        assert out["posts"] == 1
    assert str(gone.resolve()) not in registered_paths()
    entries = read_registry()["locations"]
    assert len(entries) == 1  # 只剩当前位置
    assert target.exists()


def test_registry_unreadable_record_in_other_location_submits(tmp_path,
                                                              monkeypatch):
    """其他位置里的 journal 损坏：只提示、不阻断；损坏文件原样保留。"""
    broken = tmp_path / "broken-other.json"
    broken.write_text("{not-json 张三", encoding="utf-8")
    write_registry(None, [str(broken)])
    before = broken.read_bytes()
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert _continued_after_hint(out), out["messages"]
    assert any(broken.name in message for message in out["messages"]), \
        out["messages"]
    assert broken.read_bytes() == before


def test_other_account_record_does_not_block_and_is_preserved(tmp_path,
                                                              monkeypatch):
    other = tmp_path / "other-account.json"
    before = write_unresolved_record(other, account=OTHER_ACCOUNT,
                                     journal_id="other-account-1")
    write_registry(None, [str(other)])
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert other.read_bytes() == before
    assert [r["status"] for r in active_records(other)] == ["unresolved"]


def test_same_origin_record_in_other_location_submits(tmp_path, monkeypatch):
    """同 origin 留在另一文件：3.6.19 起只提示、不阻断，且原样保留。"""
    other = tmp_path / "same-origin-other-file.json"
    with _platform(list_visible=True) as platform:
        origin = f"http://sss.example.invalid:{platform.port}"
        before = write_unresolved_record(other, journal_id="same-origin-1",
                                         platform=origin)
        write_registry(None, [str(other)])
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)

    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert _continued_after_hint(out), out["messages"]
    assert other.read_bytes() == before
    assert [r["status"] for r in active_records(other)] == ["unresolved"]


def test_record_without_account_in_other_location_submits(tmp_path, monkeypatch):
    """其他位置里缺账号的活跃记录无法安全归属：只提示、不阻断，且原样保留。"""
    other = tmp_path / "no-account.json"
    before = write_unresolved_record(other, journal_id="no-account-1",
                                     account="")
    write_registry(None, [str(other)])
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert _continued_after_hint(out), out["messages"]
    assert other.read_bytes() == before
    assert [r["status"] for r in active_records(other)] == ["unresolved"]


# ---------------------------------------------------------------------------
# 4) 重启 / 并发
# ---------------------------------------------------------------------------
def test_restart_default_then_explicit_submits_in_each_scope(tmp_path,
                                                             monkeypatch):
    """重启后从默认根切到空显式文件：第二轮照常提交（POST 增量 1）。"""
    with _platform(list_visible=False) as platform:
        work = tmp_path / "job"
        url = _url(platform)
        first_proc, first_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            config_work=work / "child-1")
        first = child_result(first_proc, first_result)
        assert first["returncode"] == 0, first
        assert first["status"] == "unconfirmed", first
        assert platform.count == 1

        root = Path(os.environ["YIKOU_SSS_AUTHORITATIVE_ROOT"])
        assert root != work / "authority-root"  # 父进程环境仍是隔离根
        journals = sorted((work / "authority-root").glob("*.json"))
        assert len(journals) == 1
        assert len(active_records(journals[0])) == 1
        before = journals[0].read_bytes()

        explicit = _empty_journal(tmp_path / "explicit-restart.json")
        second_proc, second_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            authoritative_path=str(explicit), config_work=work / "child-2")
        second = child_result(second_proc, second_result)
        assert second["returncode"] == 0, second
        assert second["status"] == "unconfirmed", second
        assert platform.count == 2

        # 切换后旧位置（默认根）的记录仍原样保留：只提示、不迁移不改写
        assert journals[0].read_bytes() == before
        assert [r["status"] for r in active_records(journals[0])] == ["unresolved"]
        # 新位置（显式文件）只写本轮自己的未确认记录
        assert [r["status"] for r in active_records(explicit)] == ["unresolved"]


def test_restart_explicit_then_drop_submits_and_preserves_explicit(tmp_path,
                                                                   monkeypatch):
    """重启后从显式文件切回默认根：第二轮照常提交，显式文件原样保留。"""
    with _platform(list_visible=False) as platform:
        work = tmp_path / "job"
        url = _url(platform)
        explicit = tmp_path / "explicit-restart-C.json"
        first_proc, first_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            authoritative_path=str(explicit), config_work=work / "child-1")
        first = child_result(first_proc, first_result)
        assert first["returncode"] == 0, first
        assert first["status"] == "unconfirmed", first
        assert platform.count == 1
        assert len(active_records(explicit)) == 1
        before = explicit.read_bytes()

        second_proc, second_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            config_work=work / "child-2")
        second = child_result(second_proc, second_result)
        assert second["returncode"] == 0, second
        assert second["status"] == "unconfirmed", second
        assert platform.count == 2
        assert explicit.read_bytes() == before
        assert [r["status"] for r in active_records(explicit)] == ["unresolved"]


def test_concurrent_switch_two_locations_submit_in_own_scope(tmp_path,
                                                             monkeypatch):
    """跨位置并发不再互相阻断（3.6.19）：两个子进程各写各的作用域。

    旧契约下「默认根 vs 显式文件」并发会被位置/未确定闸门挡住第二次提交；
    回退后跨位置不再是一条「必须人工核对」的阻断链——两轮都正常提交，各自的
    journal 各写各的、互不污染。这里把两个子进程的批次锁等待时间放宽，让它们
    在锁上串行而不是让后者等超时，使断言不依赖进程调度速度。
    """
    with _platform(list_visible=False) as platform:
        work = tmp_path / "race"
        url = _url(platform)
        explicit = _empty_journal(tmp_path / "explicit-race.json")
        serialised = {"YIKOU_SSS_JOURNAL_LOCK_TIMEOUT": "60"}
        first_proc, first_result = spawn_location_child(
            work=work, url=url, port=platform.port, env_extra=serialised,
            config_work=work / "child-1")
        second_proc, second_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            authoritative_path=str(explicit), env_extra=serialised,
            config_work=work / "child-2")
        first = child_result(first_proc, first_result)
        second = child_result(second_proc, second_result)

    assert first["returncode"] == 0, first
    assert second["returncode"] == 0, second
    assert {first["status"], second["status"]} == {"unconfirmed"}, (first, second)
    # 跨位置并发：两轮各提交 1 次（不再出现「第二个被阻断」）
    assert platform.count == 2, platform.snapshot()
    assert "blocked" not in str(first["status"])
    assert "blocked" not in str(second["status"])
    # 各自作用域的 journal 各写各的：每份恰好 1 条记录，互不污染
    root_journals = sorted((work / "authority-root").glob("*.json"))
    assert len(root_journals) == 1
    assert [r["status"] for r in active_records(root_journals[0])] == \
        ["unresolved"]
    assert [r["status"] for r in active_records(explicit)] == ["unresolved"]


def test_same_scope_concurrent_child_rejected_by_batch_lock(tmp_path,
                                                            monkeypatch):
    """同一作用域（同一权威 journal + 同一 batch_key）的批次锁仍生效。

    父进程按生产 API 持锁（等价于「另一个进程正在处理同一批次」）：子进程必须在
    任何 POST 之前被拒（``blocked_concurrent`` / ``batch-submission-lock``）；
    释放后同一批次的下一次运行恰好 1 次 POST —— 同批次绝不双发。
    """
    with _platform(list_visible=False) as platform:
        work = tmp_path / "job"
        url = _url(platform)
        monkeypatch.setenv("YIKOU_SSS_LOCK_ROOT", str(work / "locks"))
        guard = sss_uncertain.batch_submission_lock(None, BATCH_KEY)
        guard.acquire()
        try:
            locked_proc, locked_result = spawn_location_child(
                work=work, url=url, port=platform.port,
                env_extra={"YIKOU_SSS_JOURNAL_LOCK_TIMEOUT": "1"},
                config_work=work / "child-locked")
            locked = child_result(locked_proc, locked_result)
        finally:
            guard.release()

        assert locked["returncode"] == 0, locked
        assert locked["status"] == "blocked_concurrent", locked
        assert locked["semantics"] == "batch-submission-lock", locked
        assert platform.count == 0, platform.snapshot()

        free_proc, free_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            config_work=work / "child-free")
        free = child_result(free_proc, free_result)

    assert free["returncode"] == 0, free
    assert free["status"] == "unconfirmed", free
    assert platform.count == 1, platform.snapshot()


# ---------------------------------------------------------------------------
# 5) 升级边界：登记过的旧位置 vs 从未登记的旧位置
# ---------------------------------------------------------------------------
def test_declared_legacy_location_submits_and_preserves_record(tmp_path,
                                                               monkeypatch):
    """运维在登记文件里显式声明的旧位置：只提示、不阻断，记录原样保留。"""
    legacy = tmp_path / "legacy-declared.json"
    before = write_unresolved_record(legacy, journal_id="legacy-declared-1")
    write_registry(None, [str(legacy)])
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "confirmed"
    assert out["posts"] == 1
    assert _continued_after_hint(out), out["messages"]
    assert any(legacy.name in message for message in out["messages"]), \
        out["messages"]
    assert legacy.read_bytes() == before
    assert [r["status"] for r in active_records(legacy)] == ["unresolved"]


def test_unregistered_legacy_explicit_path_is_not_auto_discovered(
        tmp_path, monkeypatch):
    """升级边界（明确未关闭）：从未登记、任意位置的旧显式文件无法自动发现。

    回退后只读核对覆盖 (a) 默认权威根、(b) 登记文件里声明的位置；未声明的旧
    位置不会被扫描，也不会被声称已保护；旧记录本身不被删除/改写。升级流程即
    本文件覆盖的边界：只核对已声明或已登记的位置。
    """
    legacy = tmp_path / "legacy-unregistered.json"
    before = write_unresolved_record(legacy, journal_id="legacy-unregistered-1")
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "confirmed"  # 边界：未登记 → 本轮不会被发现
    assert out["posts"] == 1
    assert legacy.read_bytes() == before
    assert [r["status"] for r in active_records(legacy)] == ["unresolved"]
    assert str(legacy.resolve()) not in registered_paths()
