"""R8-S1 专项测试：URL 写法不得绕过未确认订单保护。

覆盖范围（本文件即这组验收清单的权威副本）：

* 闪时送网址分类：大小写/默认端口/path 归一；尾点、中文域名、缺协议、
  非法端口、非法 IP 等明确配置错误；
* 配置保存入口、任务启动入口、``SssApiClient`` 直接路径规则一致；
* 跨作用域只读核对：修复前遗留的尾点 scope unresolved 记录必须阻断提交，
  且不被迁移/改写/删除/标成 verified；
* 真实 ``run_sss_job`` + 本地模拟平台的端到端：第一次进入 uncertain 后，
  同写法重跑被阻断，换写法启动直接配置错误（0 次 POST）；
* 进程重启、并发启动、损坏 journal、HTTP 与本地 HTTPS 链路；
* 无旧未确认记录的正常配置仍能完成一次模拟提交。

隔离：所有环境变量指向 ``tmp_path``；请求只发给 ``127.0.0.1`` 的模拟平台，
网络守卫拒绝一切非回环连接；凭据/账号/名单全部为合成值。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integrations.api_client import (
    AdminApiClient,
    SssApiClient,
    origin_from_url,
)
from app.integrations.sss_url import SssUrlConfigError, canonical_sss_origin
from app.ordering import uncertain as sss_uncertain
from tests.r8s1_url_guard_harness import (
    ACCOUNT,
    MockPlatform,
    active_records,
    child_result,
    loopback_guard,
    make_config,
    run_job,
    spawn_child,
    try_scope,
    write_test_certs,
)

SYNTH_ORIGIN = "http://sss.example.invalid"


# ---------------------------------------------------------------------------
# 隔离夹具与工具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_sss_state(tmp_path, monkeypatch):
    """每个测试独立的数据/权威/锁/临时目录；显式单文件覆盖一律不设置。"""
    state = tmp_path / "state"
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    monkeypatch.delenv("YIKOU_SSS_UNCERTAIN_PATH", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT",
                       str(state / "authority-root"))
    monkeypatch.setenv("YIKOU_DATA_DIR", str(state / "userdata"))
    monkeypatch.setenv("YIKOU_SSS_LOCK_ROOT", str(state / "locks"))
    monkeypatch.setenv("YIKOU_SSS_JOURNAL_LOCK_TIMEOUT", "3")
    monkeypatch.setenv("TMPDIR", str(state / "tmp"))
    (state / "tmp").mkdir(parents=True, exist_ok=True)
    return state


def _authority_root() -> Path:
    return Path(os.environ["YIKOU_SSS_AUTHORITATIVE_ROOT"])


def _record(journal_id: str, *, platform: str | None = SYNTH_ORIGIN,
            account: str = ACCOUNT, status: str = "unresolved",
            delivery_date: str = "2026-09-16") -> dict:
    record = {
        "journal_id": journal_id,
        "identifier": journal_id,
        "batch_key": f"{delivery_date}|{account}",
        "delivery_date": delivery_date,
        "source": "excel",
        "fingerprint": {},
        "status": status,
        "error": "ReadTimeout",
        "created_at": f"{delivery_date}T10:00:00",
    }
    if account:
        record["account"] = account
    if platform is not None:
        record["platform"] = platform
    return record


def _write_journal(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "records": records},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _legacy_alias_journal_path(root: Path, alias_url: str,
                               account: str = ACCOUNT) -> Path:
    """复刻**修复前**的 authority 命名规则，用于生成旧遗留文件。

    修复前 ``authority_scope_key`` = ``origin_from_url(配置原文) + '|' + 账号``，
    尾点/中文域名不会被归一。这里只读调用既有的 ``origin_from_url`` 来复现那个
    摘要，不代表认可该写法；修复后的逻辑不会这样做。
    """
    legacy_origin = origin_from_url(alias_url)
    digest = hashlib.sha256(f"{legacy_origin}|{account}".encode("utf-8")
                            ).hexdigest()[:24]
    return root / f"{digest}.json"


def _config(url: str = SYNTH_ORIGIN, account: str = ACCOUNT):
    return SimpleNamespace(sss_url=url, sss_account=account)


# ---------------------------------------------------------------------------
# 1) 网址分类：保留合法写法，明确拒绝非规范/不支持写法
# ---------------------------------------------------------------------------
SUPPORTED_CASES = [
    ("https://sssplusnew.zhuopaikeji.com/takeout",
     "https://sssplusnew.zhuopaikeji.com"),
    ("HTTPS://EXAMPLE.INVALID:443/other", "https://example.invalid"),
    ("http://Example.COM:8080/a", "http://example.com:8080"),
    ("https://example.invalid/a?b=c#d", "https://example.invalid"),
    ("http://x.cn:80/a/b?c=d", "http://x.cn"),
    ("http://[2001:DB8::1]:8080/x?y=1", "http://[2001:db8::1]:8080"),
    ("https://[::1]/x", "https://[::1]"),
    ("http://127.0.0.1:8080/x", "http://127.0.0.1:8080"),
    ("https://xn--fsqu00a.xn--0zwm56d/takeout",
     "https://xn--fsqu00a.xn--0zwm56d"),
    ("http://local.invalid", "http://local.invalid"),
    ("http://sss.example.invalid:8443/x", "http://sss.example.invalid:8443"),
]

REJECTED_CASES = [
    ("", "为空"),
    ("   ", "为空"),
    ("m.icall.me/x", "http"),
    ("//example.invalid", "http"),
    ("not a url", "空格"),
    ("http://sss.example.invalid.", "点号结尾"),
    ("http://sss.example.invalid..", "点号结尾"),
    ("http://sss.example.invalid.:80", "点号结尾"),
    ("https://例子.测试/takeout", "非 ASCII"),
    ("https://例子.测试./takeout", "点号结尾"),
    ("ftp://example.invalid/x", "仅支持"),
    ("https://svc:secret@example.invalid/takeout", "用户名"),
    ("http://example.invalid:0/", "端口"),
    ("http://example.invalid:99999/", "端口"),
    ("http://example.invalid:8x0/", "端口"),
    ("http://127.0.0.999/", "IPv4"),
    ("http://[fe80::1%25eth0]/", "zone id"),
    ("http://ex%41mple.com/", "不支持的字符"),
    ("http://example.invalid:8 0/", "空格"),
]


@pytest.mark.parametrize("url,expected", SUPPORTED_CASES)
def test_supported_writings_keep_previous_compatibility(url, expected):
    """合法写法（大小写/默认端口/路径/IPv4/IPv6/punycode/单标签主机）仍受理。"""
    assert canonical_sss_origin(url) == expected
    # 与 SssApiClient 实际使用的 origin_from_url 完全一致，规则不存在两套
    assert canonical_sss_origin(url) == origin_from_url(url)


@pytest.mark.parametrize("url,keyword", REJECTED_CASES)
def test_non_canonical_writings_fail_with_clear_config_error(url, keyword):
    with pytest.raises(SssUrlConfigError) as excinfo:
        canonical_sss_origin(url)
    message = str(excinfo.value)
    assert "闪时送网址配置错误" in message
    assert keyword in message, message


def test_case_and_default_port_share_one_authority_scope():
    lower = _config("https://example.invalid/takeout", "187 5818 7001")
    upper = _config("HTTPS://EXAMPLE.INVALID:443/other", "18758187001")
    assert (sss_uncertain.authority_scope_key(lower)
            == sss_uncertain.authority_scope_key(upper))


@pytest.mark.parametrize("variant", [
    "http://example.invalid/takeout",
    "https://other.invalid/takeout",
    "https://example.invalid:8443/takeout",
])
def test_scheme_host_and_port_stay_distinct_scopes(variant):
    base = _config("https://example.invalid/takeout")
    assert (sss_uncertain.authority_scope_key(_config(variant))
            != sss_uncertain.authority_scope_key(base))


def test_punycode_is_supported_while_unicode_is_a_config_error():
    assert (canonical_sss_origin("https://xn--fsqu00a.xn--0zwm56d/takeout")
            == "https://xn--fsqu00a.xn--0zwm56d")
    with pytest.raises(SssUrlConfigError, match="非 ASCII"):
        canonical_sss_origin("https://例子.测试/takeout")
    # 中文写法不会再变成一个“悄悄可用的新 authority scope”
    with pytest.raises(sss_uncertain.UncertainJournalError):
        sss_uncertain.authority_scope_key(
            _config("https://例子.测试/takeout"))


def test_scope_key_rejects_trailing_dot_writing():
    with pytest.raises(sss_uncertain.UncertainJournalError, match="点号结尾"):
        sss_uncertain.authority_scope_key(_config("http://sss.example.invalid."))


def test_platform_origin_fails_closed_for_non_canonical_url():
    with pytest.raises(sss_uncertain.UncertainJournalError):
        sss_uncertain.platform_origin(_config("http://sss.example.invalid."))


# ---------------------------------------------------------------------------
# 2) 入口一致性：SssApiClient 直接路径 & 管理后台不受影响
# ---------------------------------------------------------------------------
def test_sss_client_rejects_before_creating_any_session(monkeypatch):
    """非规范网址必须在构造 Session/发请求之前被拒绝。"""
    def _boom(*_args, **_kwargs):
        raise AssertionError("Session 被创建，说明校验发生得太晚")

    monkeypatch.setattr("app.integrations.api_client.requests.Session", _boom)
    for url in ("http://sss.example.invalid.", "https://例子.测试/takeout",
                "http://sss.example.invalid.:443"):
        with pytest.raises(SssUrlConfigError):
            SssApiClient(url, ACCOUNT, "synthetic")


def test_admin_client_keeps_previous_behaviour():
    """共享客户端里的管理后台业务不受闪时送收紧影响。"""
    admin = AdminApiClient("https://example.invalid./admin/#/login", "u", "p")
    assert admin.origin == "https://example.invalid."
    assert origin_from_url("https://例子.测试/x") == "https://例子.测试"


def test_sss_client_accepts_supported_writing():
    client = SssApiClient("HTTPS://EXAMPLE.INVALID:443/takeout", ACCOUNT, "p")
    assert client.origin == "https://example.invalid"


# ---------------------------------------------------------------------------
# 3) 跨作用域只读核对（旧记录处理）
# ---------------------------------------------------------------------------
def _current_journal_and_config():
    config = _config()
    journal = sss_uncertain.authoritative_uncertain_path(config)
    return config, journal


def _scan(config, journal):
    return sss_uncertain.cross_scope_unresolved_records(
        journal, config=config, account=ACCOUNT, origin=SYNTH_ORIGIN)


def test_legacy_alias_journal_is_flagged_and_not_modified():
    config, journal = _current_journal_and_config()
    alias_url = "http://sss.example.invalid.:443"
    legacy = _legacy_alias_journal_path(_authority_root(), alias_url)
    _write_journal(legacy, [_record("legacy-alias-1",
                                    platform=origin_from_url(alias_url))])
    before = legacy.read_bytes()

    conflicts = _scan(config, journal)

    assert [item["journal_id"] for item in conflicts] == ["legacy-alias-1"]
    assert conflicts[0]["journal"] == str(legacy)
    assert "已不再支持" in conflicts[0]["reason"]
    # 只读：字节完全不变，不迁移、不改写、不标 verified
    assert legacy.read_bytes() == before
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    assert payload["records"][0]["status"] == "unresolved"
    assert "verified" not in json.dumps(payload, ensure_ascii=False)


def test_distinct_canonical_origin_is_not_overblocked():
    """不同规范 origin（host/scheme/端口）= 不同安全域，不能被误伤阻断。"""
    config, journal = _current_journal_and_config()
    _write_journal(_authority_root() / "other-platform.json",
                   [_record("other-1", platform="http://other.invalid")])
    assert _scan(config, journal) == []


def test_canonical_origin_records_are_not_inferred_as_same_identity():
    """规范 origin 的记录不做“可能同源”推断：同 origin 另一份文件也不阻断。

    规范 origin 直接决定 journal 路径（同 origin + 同账号只有一个权威文件），
    不同 origin 按既有产品约定是不同安全域；跨作用域兜底只针对**无法安全归属**
    的旧写法（尾点/中文域名/平台字段缺失），见下一个测试。
    """
    config, journal = _current_journal_and_config()
    _write_journal(_authority_root() / "same-origin-copy.json",
                   [_record("same-1", platform=SYNTH_ORIGIN)])
    assert _scan(config, journal) == []


def test_other_account_is_not_blocked():
    config, journal = _current_journal_and_config()
    _write_journal(_authority_root() / "other-account.json",
                   [_record("other-account-1", account="18758187002")])
    assert _scan(config, journal) == []


@pytest.mark.parametrize("status", ["resolved", "discarded"])
def test_inactive_records_do_not_block(status):
    config, journal = _current_journal_and_config()
    _write_journal(_authority_root() / "inactive.json",
                   [_record(f"inactive-{status}", status=status,
                            platform="http://sss.example.invalid.:443")])
    assert _scan(config, journal) == []


def test_record_without_account_or_platform_blocks():
    config, journal = _current_journal_and_config()
    _write_journal(_authority_root() / "no-account.json",
                   [_record("no-account-1", account="", platform=SYNTH_ORIGIN)])
    _write_journal(_authority_root() / "no-platform.json",
                   [_record("no-platform-1", platform=None)])
    conflicts = _scan(config, journal)
    ids = {item["journal_id"] for item in conflicts}
    assert ids == {"no-account-1", "no-platform-1"}
    assert all(item["reason"] for item in conflicts)


def test_current_journal_is_not_reported_by_itself():
    config, journal = _current_journal_and_config()
    _write_journal(journal, [_record("current-1")])
    assert _scan(config, journal) == []


def test_corrupt_foreign_journal_fails_closed():
    config, journal = _current_journal_and_config()
    broken = _authority_root() / "broken.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not-json", encoding="utf-8")
    before = broken.read_bytes()
    with pytest.raises(sss_uncertain.UncertainJournalError) as excinfo:
        _scan(config, journal)
    message = str(excinfo.value)
    assert str(broken) in message
    assert "人工核对" in message
    assert broken.read_bytes() == before


def test_unreadable_foreign_journal_fails_closed():
    config, journal = _current_journal_and_config()
    unreadable = _authority_root() / "unreadable.json"
    _write_journal(unreadable, [_record("unreadable-1")])
    unreadable.chmod(0o000)
    try:
        with pytest.raises(sss_uncertain.UncertainJournalError):
            _scan(config, journal)
    finally:
        unreadable.chmod(0o600)


def test_explicit_single_file_override_skips_scan(monkeypatch, tmp_path):
    """显式单文件覆盖时所有 scope 共用一个文件，写法无法拆分它。"""
    override = tmp_path / "explicit.json"
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_PATH", str(override))
    config, journal = _current_journal_and_config()
    assert journal == override
    assert _scan(config, journal) == []


# ---------------------------------------------------------------------------
# 4) 端到端：真实 run_sss_job + 本地模拟平台（HTTP）
# ---------------------------------------------------------------------------
def test_uncertain_then_same_writing_blocked_then_alias_rejected(tmp_path):
    with MockPlatform(list_visible=False) as platform:
        port = platform.port
        canonical = f"http://sss.example.invalid:{port}"
        alias = f"http://sss.example.invalid.:{port}"
        work = tmp_path / "work"
        with loopback_guard(port):
            config = make_config(work, canonical)
            journal = sss_uncertain.authoritative_uncertain_path(config)
            assert try_scope(config) == f"{canonical}|{ACCOUNT}"

            first = run_job(config)
            assert first["status"] == "unconfirmed"
            assert platform.count == 1
            assert len(active_records(journal)) == 1

            # 同写法重跑：既有反重复闸门阻断
            second = run_job(config)
            assert second["status"] == "blocked_uncertain"
            assert second["semantics"] == "uncertain-journal-guard"
            assert platform.count == 1

            # 只改 URL 写法（尾点）：在任何请求之前配置错误
            with pytest.raises(SssUrlConfigError):
                run_job(make_config(work, alias))
            assert platform.count == 1
            assert first["created"] == 0


def test_normal_config_still_submits_once(tmp_path):
    """没有旧未确认记录的正常配置必须仍能完成一次模拟提交。"""
    with MockPlatform(list_visible=True) as platform:
        port = platform.port
        work = tmp_path / "work"
        with loopback_guard(port):
            config = make_config(work, f"http://sss.example.invalid:{port}")
            result = run_job(config)
            assert result["status"] == "confirmed"
            assert result["created"] == 1
            assert platform.count == 1
            journal = sss_uncertain.authoritative_uncertain_path(config)
            assert active_records(journal) == []


def test_legacy_alias_journal_blocks_canonical_start(tmp_path):
    """修复前遗留的尾点 scope unresolved → 规范写法启动必须 0 POST 阻断。"""
    with MockPlatform(list_visible=True) as platform:
        port = platform.port
        canonical = f"http://sss.example.invalid:{port}"
        alias = f"http://sss.example.invalid.:{port}"
        work = tmp_path / "work"
        legacy = _legacy_alias_journal_path(_authority_root(), alias)
        _write_journal(legacy, [_record("legacy-alias-1",
                                        platform=origin_from_url(alias))])
        before = legacy.read_bytes()
        with loopback_guard(port):
            config = make_config(work, canonical)
            result = run_job(config)
            assert result["status"] == "blocked_uncertain"
            assert result["semantics"] == "cross-scope-authority-guard"
            assert result["summary"]["cross_scope_records"] == 1
            assert platform.count == 0
            assert "影响范围" in result["next_action"]
        assert legacy.read_bytes() == before
        assert active_records(legacy)[0]["status"] == "unresolved"


def test_canonical_unresolved_then_alias_start_is_config_error(tmp_path):
    """普通写法旧记录 → 改尾点写法启动：配置错误、0 POST、旧记录不变。"""
    with MockPlatform(list_visible=False) as platform:
        port = platform.port
        canonical = f"http://sss.example.invalid:{port}"
        alias = f"http://sss.example.invalid.:{port}"
        work = tmp_path / "work"
        with loopback_guard(port):
            config = make_config(work, canonical)
            journal = sss_uncertain.authoritative_uncertain_path(config)
            assert run_job(config)["status"] == "unconfirmed"
            before = journal.read_bytes()
            with pytest.raises(SssUrlConfigError):
                run_job(make_config(work, alias))
            assert journal.read_bytes() == before
            assert platform.count == 1
            assert len(active_records(journal)) == 1


def test_config_change_after_validation_does_not_split_scope(tmp_path,
                                                            monkeypatch):
    """校验与执行之间配置被改写：整个执行期仍绑定启动时冻结的规范 origin。

    R8-S1 要求“校验与执行之间配置变化不能绕过保护”。这里在验证码请求（即校验
    之后、提交之前）把 ``config.sss_url`` 改成另一个合法 URL，未决记录必须仍落在
    启动时冻结的作用域；否则下一次用原网址启动就会看不到它，等于再开一条重复
    提交通道。
    """
    from app.integrations import api_client as api_client_module

    with MockPlatform(list_visible=False) as platform:
        port = platform.port
        canonical = f"http://sss.example.invalid:{port}"
        other = f"http://other.invalid:{port}"
        work = tmp_path / "work"
        with loopback_guard(port):
            config = make_config(work, canonical)
            frozen_journal = sss_uncertain.authoritative_uncertain_path(config)
            original_fetch = api_client_module.SssApiClient.fetch_captcha

            def patched_fetch(self):
                config.sss_url = other  # 校验之后改写配置
                return original_fetch(self)

            monkeypatch.setattr(api_client_module.SssApiClient,
                                "fetch_captcha", patched_fetch)
            first = run_job(config)
            assert first["status"] == "unconfirmed"
            assert platform.count == 1
            assert len(active_records(frozen_journal)) == 1
            # 被改写后的 other.invalid 作用域里不应出现任何记录
            assert active_records(
                sss_uncertain.authoritative_uncertain_path(config)) == []

        # 用原网址再启动：冻结作用域里的未决记录仍然阻断，且不再 POST
        # （只还原 fetch_captcha 这一个补丁，不能 undo 掉隔离环境变量）
        monkeypatch.setattr(api_client_module.SssApiClient,
                            "fetch_captcha", original_fetch)
        config2 = make_config(work, canonical)
        assert (sss_uncertain.authoritative_uncertain_path(config2)
                == frozen_journal)
        with loopback_guard(port):
            second = run_job(config2)
        assert second["status"] == "blocked_uncertain"
        assert second["semantics"] == "uncertain-journal-guard"
        assert platform.count == 1


def test_corrupt_foreign_journal_blocks_real_run(tmp_path):
    with MockPlatform(list_visible=True) as platform:
        port = platform.port
        work = tmp_path / "work"
        broken = _authority_root() / "broken.json"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{not-json", encoding="utf-8")
        before = broken.read_bytes()
        with loopback_guard(port):
            config = make_config(work, f"http://sss.example.invalid:{port}")
            result = run_job(config)
        assert result["status"] == "failed"
        assert result["semantics"] == "cross-scope-authority-guard"
        assert platform.count == 0
        assert broken.read_bytes() == before


# ---------------------------------------------------------------------------
# 5) HTTPS 链路（本地自签 CA，证书 SAN = 合成主机名）
# ---------------------------------------------------------------------------
def test_https_chain_confirms_and_alias_rejected(tmp_path, monkeypatch):
    certs = write_test_certs(tmp_path / "certs")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certs["ca"])
    platform = MockPlatform(list_visible=True)
    platform.start(tls=True, certfile=certs["cert"], keyfile=certs["key"])
    try:
        port = platform.port
        canonical = f"https://sss.example.invalid:{port}"
        alias = f"https://sss.example.invalid.:{port}"
        work = tmp_path / "work"
        with loopback_guard(port):
            config = make_config(work, canonical)
            result = run_job(config)
            assert result["status"] == "confirmed", result
            assert platform.count == 1
            with pytest.raises(SssUrlConfigError):
                run_job(make_config(work, alias))
            assert platform.count == 1
    finally:
        platform.stop()


# ---------------------------------------------------------------------------
# 6) 进程重启 / 并发
# ---------------------------------------------------------------------------
def test_restart_with_legacy_alias_journal_still_blocks(tmp_path):
    with MockPlatform(list_visible=True) as platform:
        port = platform.port
        canonical = f"http://sss.example.invalid:{port}"
        alias = f"http://sss.example.invalid.:{port}"
        work = tmp_path / "job"
        # 子进程的权威根是 work/authority-root（与 spawn_child 注入的环境一致）
        legacy = _legacy_alias_journal_path(work / "authority-root", alias)
        _write_journal(legacy, [_record("legacy-alias-1",
                                        platform=origin_from_url(alias))])
        before = legacy.read_bytes()

        for _attempt in range(2):  # 两次独立进程 = 重启后仍阻断
            process, result_path = spawn_child(work=work, url=canonical,
                                               port=port)
            payload = child_result(process, result_path)
            assert payload["returncode"] == 0, payload
            assert payload["status"] == "blocked_uncertain", payload
            assert payload["semantics"] == "cross-scope-authority-guard"

        # 尾点写法：配置错误，且不留任何 POST
        process, result_path = spawn_child(work=work, url=alias, port=port)
        payload = child_result(process, result_path)
        assert payload["returncode"] == 3, payload
        assert "SssUrlConfigError" in payload.get("error", "")

        assert platform.count == 0
        assert legacy.read_bytes() == before


def test_concurrent_starts_never_double_post(tmp_path):
    with MockPlatform(list_visible=False) as platform:
        port = platform.port
        canonical = f"http://sss.example.invalid:{port}"
        work = tmp_path / "job"
        first_proc, first_result = spawn_child(work=work, url=canonical,
                                               port=port)
        second_proc, second_result = spawn_child(work=work, url=canonical,
                                                 port=port)
        first = child_result(first_proc, first_result)
        second = child_result(second_proc, second_result)

        assert platform.count <= 1, (platform.snapshot(), first, second)
        statuses = {first.get("status"), second.get("status")}
        assert statuses & {"blocked_concurrent", "blocked_uncertain"}, statuses
        if platform.count == 1:
            assert "unconfirmed" in statuses, statuses


# ---------------------------------------------------------------------------
# 7) 配置保存 / 任务启动入口
# ---------------------------------------------------------------------------
def test_bridge_entry_points_reject_non_canonical_url(tmp_path):
    from app.api.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    bridge._config.sss_url = "https://sss.example.invalid/takeout"

    saved = bridge.save_sss_config({"url": "http://sss.example.invalid."})
    assert saved["ok"] is False
    assert saved["reason"] == "invalid_sss_url"
    assert "url" in saved["fields"]
    assert bridge._config.sss_url == "https://sss.example.invalid/takeout"

    started = bridge.start_sss({
        "url": "http://sss.example.invalid.:443", "account": ACCOUNT,
        "password": "synthetic", "order_source": "wps",
    })
    assert started["status"] == "rejected"
    assert started["reason"] == "validation_failed"
    assert "url" in started["fields"]

    # 合法写法（含大小写与默认端口）仍可保存
    ok = bridge.save_sss_config({"url": "HTTPS://EXAMPLE.INVALID:443/takeout"})
    assert ok["ok"] is True
    assert bridge._config.sss_url == "HTTPS://EXAMPLE.INVALID:443/takeout"
