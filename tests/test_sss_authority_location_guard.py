"""R8-S3 专项测试：显式权威位置切换不得绕过未确认订单保护。

场景（与 ``docs/OPTIMIZATION-FINAL-ACCEPTANCE-R8-S3.md`` 的 C1–C5 对应）：

* C1 显式文件里已有旧尾点 unresolved → 普通 URL：仍阻断；
* C2 默认权威根有 unresolved → 启用空显式文件：第二轮 POST=0；
* C3 显式 A 有 unresolved → 显式 B：第二轮 POST=0；
* C4 显式位置有 unresolved → 取消覆盖：第二轮 POST=0；
* C5 环境变量形式的覆盖同样不能绕过；
* 进程重启、两进程竞争切换/提交、登记文件损坏/不可读/不可写、未知版本、
  config 属性 vs 环境变量优先级、正常路径恰好 1 次 POST、账号隔离、
  未登记旧位置的升级边界（明确未关闭）。

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
    """跑一轮真实 run_sss_job；返回状态/语义/本次 POST 数/结果。"""
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
    try:
        result = run_location_job(config)
        status = str(result.get("status"))
    except Exception as exc:  # noqa: BLE001 - 测试要如实看到异常类型
        result = {"status": f"EXCEPTION:{type(exc).__name__}", "error": str(exc)}
        status = str(result["status"])
    return {"status": status, "semantics": result.get("semantics"),
            "posts": platform.count - before, "result": result}


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
# 1) C1–C5：位置切换不再放行第二次 POST
# ---------------------------------------------------------------------------
def test_c1_explicit_file_with_legacy_record_still_blocks(tmp_path, monkeypatch):
    """C1：显式文件里已有旧尾点 unresolved → 普通 URL，仍必须阻断。"""
    explicit = tmp_path / "explicit-c1.json"
    before = write_unresolved_record(
        explicit, journal_id="c1-legacy",
        platform="http://sss.example.invalid.")
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", explicit=str(explicit),
                   monkeypatch=monkeypatch)
    assert out["status"] == "blocked_uncertain"
    assert out["result"]["semantics"] == "uncertain-journal-guard"
    assert out["posts"] == 0
    assert explicit.read_bytes() == before
    assert [r["status"] for r in active_records(explicit)] == ["unresolved"]
    assert "verified" not in explicit.read_text(encoding="utf-8")


def test_c2_default_unresolved_then_empty_explicit_blocks(tmp_path,
                                                          monkeypatch):
    """C2：默认根有 unresolved → 空显式文件，第二轮 POST=0。"""
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

    assert second["status"] == "blocked_uncertain"
    assert second["result"]["semantics"] == "authority-location-guard"
    assert second["posts"] == 0
    assert second["result"]["location_conflicts"] >= 1
    # 冲突位置要么直接是那份 journal，要么是它所在的默认权威根（目录扫描单元）
    conflict_locations = second["result"]["location_conflict_locations"]
    assert any(str(journals[0]) == str(loc) or Path(str(loc)) == _default_root()
               for loc in conflict_locations), conflict_locations
    assert "POST" in second["result"]["next_action"]
    # 前置记录仍在、未被改写/删除
    assert journals[0].read_bytes() == before
    assert len(active_records(journals[0])) == 1


def test_c3_explicit_a_unresolved_then_b_blocks(tmp_path, monkeypatch):
    """C3：显式 A 有 unresolved → 显式 B，第二轮 POST=0。"""
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

    assert second["status"] == "blocked_uncertain"
    assert second["result"]["semantics"] == "authority-location-guard"
    assert second["posts"] == 0
    assert second["result"]["location_conflict_locations"] == [str(a.resolve())]
    assert a.read_bytes() == before
    assert [r["status"] for r in active_records(a)] == ["unresolved"]
    assert not b.exists() or active_records(b) == []


def test_c4_explicit_unresolved_then_drop_override_blocks(tmp_path,
                                                          monkeypatch):
    """C4：显式位置有 unresolved → 取消覆盖，第二轮 POST=0。"""
    c = tmp_path / "explicit-C.json"
    with _platform(list_visible=False) as platform:
        first = _run(platform, tmp_path / "w", explicit=str(c),
                     monkeypatch=monkeypatch)
        assert first["status"] == "unconfirmed"
        assert first["posts"] == 1
        assert len(active_records(c)) == 1
        before = c.read_bytes()

        second = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)

    assert second["status"] == "blocked_uncertain"
    assert second["result"]["semantics"] == "authority-location-guard"
    assert second["posts"] == 0
    assert second["result"]["location_conflict_locations"] == [str(c.resolve())]
    assert c.read_bytes() == before
    assert [r["status"] for r in active_records(c)] == ["unresolved"]


def test_c5_env_override_blocks(tmp_path, monkeypatch):
    """C5：环境变量形式的覆盖同样不能绕过（默认根 → 空显式文件）。"""
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

    assert second["status"] == "blocked_uncertain"
    assert second["result"]["semantics"] == "authority-location-guard"
    assert second["posts"] == 0
    assert journals[0].read_bytes() == before
    assert len(active_records(journals[0])) == 1


def test_c5_env_override_a_to_b_blocks(tmp_path, monkeypatch):
    """C5 补充：环境变量形式显式 A（有未决）→ 环境变量形式显式 B。"""
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

    assert second["status"] == "blocked_uncertain"
    assert second["posts"] == 0
    assert second["result"]["location_conflict_locations"] == [str(a.resolve())]
    assert a.read_bytes() == before


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
# 3) 登记文件故障与并发
# ---------------------------------------------------------------------------
def test_corrupt_registry_fails_closed(tmp_path, monkeypatch):
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not-json 张三 13800000001", encoding="utf-8")
    before = target.read_bytes()
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "failed"
    assert out["result"]["semantics"] == "authority-location-guard"
    assert out["posts"] == 0
    assert "人工核对" in out["result"]["next_action"]
    assert "删除" in out["result"]["next_action"]
    assert target.read_bytes() == before


def test_unsupported_registry_version_fails_closed(tmp_path, monkeypatch):
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 99, "locations": []}),
                      encoding="utf-8")
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "failed"
    assert out["posts"] == 0
    assert "版本" in out["result"]["next_action"]


def test_registry_path_is_directory_fails_closed(tmp_path, monkeypatch):
    target = registry_path()
    target.mkdir(parents=True, exist_ok=True)
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "failed"
    assert out["posts"] == 0


def test_unreadable_registry_fails_closed(tmp_path, monkeypatch):
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 1, "locations": []}),
                      encoding="utf-8")
    target.chmod(0o000)
    try:
        with _platform(list_visible=True) as platform:
            out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    finally:
        target.chmod(0o600)
    assert out["status"] == "failed"
    assert out["posts"] == 0


def test_registry_write_failure_blocks_before_post(tmp_path, monkeypatch):
    data_dir = Path(os.environ["YIKOU_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o500)
    try:
        with _platform(list_visible=True) as platform:
            out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    finally:
        data_dir.chmod(0o700)
    assert out["status"] == "failed"
    assert out["result"]["semantics"] == "authority-location-guard"
    assert out["posts"] == 0
    assert not registry_path().exists()


def test_invalid_registry_entries_fail_closed(tmp_path, monkeypatch):
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    for payload in ({"version": 1, "locations": "not-a-list"},
                    {"version": 1, "locations": [{"path": ""}]},
                    {"version": 1, "locations": ["not-an-object"]}):
        target.write_text(json.dumps(payload), encoding="utf-8")
        with _platform(list_visible=True) as platform:
            out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
        assert out["status"] == "failed", payload
        assert out["posts"] == 0, payload


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


def test_registry_unreadable_record_in_other_location_blocks(tmp_path,
                                                             monkeypatch):
    """其他位置里的 journal 损坏 → 不能当成“没有记录”。"""
    broken = tmp_path / "broken-other.json"
    broken.write_text("{not-json 张三", encoding="utf-8")
    write_registry(None, [str(broken)])
    before = broken.read_bytes()
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "failed"
    assert out["result"]["semantics"] == "authority-location-guard"
    assert out["posts"] == 0
    assert str(broken) in out["result"]["next_action"]
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


def test_same_origin_record_in_other_location_blocks(tmp_path, monkeypatch):
    """路径切换不走“不同规范 origin 才跳过”的规则：同 origin 留在另一文件也阻断。"""
    other = tmp_path / "same-origin-other-file.json"
    with _platform(list_visible=True) as platform:
        origin = f"http://sss.example.invalid:{platform.port}"
        before = write_unresolved_record(other, journal_id="same-origin-1",
                                         platform=origin)
        write_registry(None, [str(other)])
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)

    assert out["status"] == "blocked_uncertain"
    assert out["result"]["semantics"] == "authority-location-guard"
    assert out["posts"] == 0
    assert out["result"]["location_conflict_locations"] == [str(other.resolve())]
    assert other.read_bytes() == before


def test_record_without_account_in_other_location_blocks(tmp_path, monkeypatch):
    """其他位置里缺账号的活跃记录无法安全归属 → 保守阻断。"""
    other = tmp_path / "no-account.json"
    before = write_unresolved_record(other, journal_id="no-account-1",
                                     account="")
    write_registry(None, [str(other)])
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "blocked_uncertain"
    assert out["result"]["semantics"] == "authority-location-guard"
    assert out["posts"] == 0
    assert other.read_bytes() == before


# ---------------------------------------------------------------------------
# 4) 重启 / 并发
# ---------------------------------------------------------------------------
def test_restart_default_then_explicit_blocks(tmp_path, monkeypatch):
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

        explicit = _empty_journal(tmp_path / "explicit-restart.json")
        second_proc, second_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            authoritative_path=str(explicit), config_work=work / "child-2")
        second = child_result(second_proc, second_result)
        assert second["returncode"] == 0, second
        assert second["status"] == "blocked_uncertain", second
        assert second["semantics"] == "authority-location-guard", second
        assert platform.count == 1

        root = Path(os.environ["YIKOU_SSS_AUTHORITATIVE_ROOT"])
        assert root != work / "authority-root"  # 父进程环境仍是隔离根
        journals = sorted((work / "authority-root").glob("*.json"))
        assert len(journals) == 1
        assert len(active_records(journals[0])) == 1


def test_restart_explicit_then_drop_blocks(tmp_path, monkeypatch):
    with _platform(list_visible=False) as platform:
        work = tmp_path / "job"
        url = _url(platform)
        explicit = tmp_path / "explicit-restart-C.json"
        first_proc, first_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            authoritative_path=str(explicit), config_work=work / "child-1")
        first = child_result(first_proc, first_result)
        assert first["status"] == "unconfirmed", first
        assert platform.count == 1
        assert len(active_records(explicit)) == 1
        before = explicit.read_bytes()

        second_proc, second_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            config_work=work / "child-2")
        second = child_result(second_proc, second_result)
        assert second["status"] == "blocked_uncertain", second
        assert second["semantics"] == "authority-location-guard", second
        assert platform.count == 1
        assert explicit.read_bytes() == before


def test_concurrent_switch_never_second_post(tmp_path, monkeypatch):
    """两进程竞争“默认根 vs 显式文件”：总 POST 恰为 1，另一个被闸门阻断。"""
    with _platform(list_visible=False) as platform:
        work = tmp_path / "race"
        url = _url(platform)
        explicit = _empty_journal(tmp_path / "explicit-race.json")
        first_proc, first_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            config_work=work / "child-1")
        second_proc, second_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            authoritative_path=str(explicit), config_work=work / "child-2")
        first = child_result(first_proc, first_result)
        second = child_result(second_proc, second_result)

    assert platform.count == 1, (platform.snapshot(), first, second)
    statuses = {first.get("status"), second.get("status")}
    assert statuses & {"blocked_concurrent", "blocked_uncertain"}, statuses
    blocked = [payload for payload in (first, second)
               if payload.get("status") in {"blocked_concurrent",
                                            "blocked_uncertain"}]
    assert blocked and "POST" in str(blocked[0].get("next_action") or "")


# ---------------------------------------------------------------------------
# 5) 升级边界：登记过的旧位置 vs 从未登记的旧位置
# ---------------------------------------------------------------------------
def test_declared_legacy_location_blocks_after_switch(tmp_path, monkeypatch):
    """运维在登记文件里显式声明的旧位置：切换到默认根后必须阻断。"""
    legacy = tmp_path / "legacy-declared.json"
    before = write_unresolved_record(legacy, journal_id="legacy-declared-1")
    write_registry(None, [str(legacy)])
    with _platform(list_visible=True) as platform:
        out = _run(platform, tmp_path / "w", monkeypatch=monkeypatch)
    assert out["status"] == "blocked_uncertain"
    assert out["result"]["semantics"] == "authority-location-guard"
    assert out["posts"] == 0
    assert out["result"]["location_conflict_locations"] == [str(legacy.resolve())]
    assert legacy.read_bytes() == before
    assert [r["status"] for r in active_records(legacy)] == ["unresolved"]


def test_unregistered_legacy_explicit_path_is_not_auto_discovered(
        tmp_path, monkeypatch):
    """升级边界（明确未关闭）：从未登记、任意位置的旧显式文件无法自动发现。

    修复后只保护 (a) 默认权威根、(b) 修复后代码使用过并被登记的位置、
    (c) 运维在登记文件里声明的旧位置。本测试固定这一边界：未声明的旧位置不会被
    扫描，也不会被声称已保护；旧记录本身不被删除/改写。升级流程见
    ``docs/OPTIMIZATION-R8-S3-FIX.md``。
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
