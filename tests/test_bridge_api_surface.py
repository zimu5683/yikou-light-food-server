"""``Bridge`` 的 js_api 面回归锁（改动前这 5 个方法一次都没被测过）。

`Bridge` 的公开方法就是**前端能调用的全部接口**（pywebview 把它们暴露成 JS
Promise）。改动前 `tests/` 只碰过其中 11 个，剩下 26 个没有任何直接测试，本文件
先覆盖其中**不依赖浏览器/网络**的一批：

* ``bridge_ready`` —— 前端握手，返回 40+ 字段的初始状态（前端 ``AppState`` 的镜像）。
  字段缺失或改名会让界面**静默失灵**。
* ``wps_status`` —— 云同步状态。里面那个 ``writing_test_copies`` 是**安全指示灯**：
  它判断「当前实际写入的是测试副本还是正式表」，判错会让用户以为在安全测试、
  实际却在改协作者的正式表。
* ``worker_alive`` / ``set_split_ratio`` / ``restore_wps_production_tables``
  —— 其余状态与配置动作。
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app.api.bridge import Bridge
from app.wps.sync import SyncLedger, WpsCloudError
from app.api import bridge as bridge_module


def _bridge(tmp_path) -> Bridge:
    # 这些用例验证的是**管理员**的完整能力（js_api 表面）。
    # Bridge 的默认角色是「非管理员」（安全默认），故此处显式以管理员构造；
    # 角色相关的拦截由 tests/test_web_roles.py 专门覆盖。
    return Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)


def test_removed_channels_are_not_on_the_bridge(tmp_path):
    """echo_test / frontend_report / pop_reports 已删：不再出现在 js_api 面上。"""
    bridge = _bridge(tmp_path)
    for name in ("echo_test", "frontend_report", "pop_reports"):
        assert not hasattr(bridge, name), name


class _FakeCli:
    def __init__(self, path: str = "/fake/kdocs-cli", authed: bool = True) -> None:
        self.path = path
        self._authed = authed

    def authenticated(self) -> bool:
        return self._authed


# ----------------------------------------------------------------------
# bridge_ready：前端握手契约
# ----------------------------------------------------------------------
def test_bridge_ready_exposes_version_status_and_producer_id(tmp_path):
    bridge = _bridge(tmp_path)
    state = bridge.bridge_ready()

    from app import __version__

    assert state["version"] == __version__
    assert state["status"] == "ready"
    assert state["event_producer_id"] == bridge._event_producer_id
    # 前端靠 producer_id 识别 Python 进程重启，不能为空
    assert state["event_producer_id"]


def test_bridge_ready_config_carries_every_field_the_frontend_needs(tmp_path):
    """前端 ``AppState['config']`` 依赖这些键；少一个界面就会读 undefined。"""
    bridge = _bridge(tmp_path)
    config = bridge.bridge_ready()["config"]

    expected = {
        "target_url", "phone_number", "excel_path", "order_date", "order_count",
        "split_ratio", "sss_url", "sss_account", "sss_excel_path",
        "sss_order_source", "sss_product_name", "sss_common_address",
        "sss_use_fixed_address", "sss_fixed_lnt", "sss_fixed_lat",
        "sss_fixed_area_code", "sss_fixed_address_detail", "sss_dry_run",
        "sss_preflight", "sss_idempotency_field",
        "wps_enabled", "wps_test_mode", "wps_test_file_id", "wps_test_drive_id",
        "wps_test_tables", "wps_drive_id", "wps_cli_path", "wps_tables",
        "wps_target_hour_start", "wps_target_hour_end", "wps_marker_enabled",
    }
    assert expected <= set(config), f"缺少字段：{sorted(expected - set(config))}"
    # 路径类字段必须是字符串（前端直接渲染），None 会显示成 "null"
    for key in ("excel_path", "sss_excel_path", "wps_cli_path", "target_url"):
        assert isinstance(config[key], str)


def test_bridge_ready_returns_passwords_mapping(tmp_path):
    bridge = _bridge(tmp_path)
    passwords = bridge.bridge_ready()["passwords"]
    assert set(passwords) == {"order", "sss"}
    # 未配置账号时不该去打扰系统密钥链，直接返回空串
    assert passwords["order"] == "" and passwords["sss"] == ""


def test_bridge_ready_loads_each_password_into_its_own_slot(tmp_path, monkeypatch):
    """两个槽位不能对调 —— 把闪时送密码当管理后台密码填进去会白跑一轮登录。

    原先只断言「都为空串」，两个槽位对调也能通过（空串换空串），属于断言太弱。
    """
    monkeypatch.setattr(bridge_module, "get_password", lambda account: f"ORDER:{account}")
    monkeypatch.setattr(bridge_module, "get_sss_password", lambda account: f"SSS:{account}")

    bridge = _bridge(tmp_path)
    bridge._config.phone_number = "13800000000"
    bridge._config.sss_account = "sss-user"

    passwords = bridge.bridge_ready()["passwords"]

    assert passwords["order"] == "ORDER:13800000000"
    assert passwords["sss"] == "SSS:sss-user"


def test_bridge_ready_stringifies_excel_paths(tmp_path):
    """路径要以字符串交给前端（路径对象会被序列化成 null / 报错）。

    ⚠️ 用 ``tmp_path`` 而不是硬编码 ``/tmp/...``：Windows 上 ``str(Path("/tmp/x"))``
    是 ``"\\tmp\\x"``，写死 POSIX 字面量会让这条测试只在 Linux/macOS 通过
    （CI 的 windows 作业正是这样抓到的）。
    """
    from pathlib import Path

    bridge = _bridge(tmp_path)
    excel = tmp_path / "排单 名单.xlsx"
    sss = tmp_path / "闪时送.xlsx"
    bridge._config.excel_path = Path(excel)
    bridge._config.sss_excel_path = Path(sss)

    config = bridge.bridge_ready()["config"]

    assert config["excel_path"] == str(excel)
    assert config["sss_excel_path"] == str(sss)
    assert "名单.xlsx" in config["excel_path"], "中文与空格要原样保留"


def test_bridge_ready_returns_a_fresh_top_level_mapping(tmp_path):
    """``wps_tables`` 用 ``dict(...)`` 做**浅**拷贝：外层是新的，内层 dict 共享。

    内层共享在这里是**无害**的 —— pywebview 会把返回值 JSON 序列化后再交给 JS，
    前端拿到的是快照，改不到 Python 对象。这里把实际行为写清楚，免得后人误以为
    它是深拷贝而依赖错的保证。
    """
    bridge = _bridge(tmp_path)
    bridge._config.wps_tables = {"东湖中餐": {"file_id": "F1"}}

    config = bridge.bridge_ready()["config"]

    # 外层是新 dict：加键不会影响配置
    config["wps_tables"]["新表"] = {"file_id": "X"}
    assert "新表" not in bridge._config.wps_tables
    # 取值确实来自配置
    assert config["wps_tables"]["东湖中餐"]["file_id"] == "F1"


def test_bridge_ready_reflects_config_changes_between_calls(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._config.target_url = "https://first.example"
    assert bridge.bridge_ready()["config"]["target_url"] == "https://first.example"

    bridge._config.target_url = "https://second.example"
    assert bridge.bridge_ready()["config"]["target_url"] == "https://second.example"


# ----------------------------------------------------------------------
# wps_status：安全指示灯
# ----------------------------------------------------------------------
def _patch_status_env(monkeypatch, tmp_path, *, tables, production,
                      test_tables=None, test_mode=False, cli=None, effective=None):
    monkeypatch.setattr(bridge_module, "SyncLedger",
                        lambda *a, **k: SyncLedger(tmp_path / "wps_sync_state.json"))
    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: effective if effective is not None else tables)
    bridge = _bridge(tmp_path)
    bridge._config.wps_tables = tables
    bridge._config.wps_production_tables = production
    bridge._config.wps_test_tables = test_tables or {}
    bridge._config.wps_test_mode = test_mode
    fake = cli if cli is not None else _FakeCli()
    monkeypatch.setattr(bridge, "_wps_cli", lambda: fake)
    return bridge


def test_wps_status_reports_writing_test_copies_when_targets_are_copies(tmp_path, monkeypatch):
    """生效目标是测试副本（不在正式表里）→ writing_test_copies 必须为 True。"""
    bridge = _patch_status_env(
        monkeypatch, tmp_path,
        tables={"东湖中餐": {"file_id": "COPY"}},
        production={"东湖中餐": {"file_id": "PROD"}},
        test_tables={"东湖中餐": "COPY"}, test_mode=True,
        effective={"东湖中餐": {"file_id": "COPY"}})

    status = bridge.wps_status()

    assert status["writing_test_copies"] is True
    assert status["effective_targets"] == ["COPY"]
    assert status["production_tables"] == {"东湖中餐": "PROD"}


def test_wps_status_reports_not_writing_copies_when_target_is_production(tmp_path, monkeypatch):
    """生效目标就是正式表 → 必须是 False，否则用户会以为在安全测试。"""
    bridge = _patch_status_env(
        monkeypatch, tmp_path,
        tables={"东湖中餐": {"file_id": "PROD"}},
        production={"东湖中餐": {"file_id": "PROD"}},
        effective={"东湖中餐": {"file_id": "PROD"}})

    status = bridge.wps_status()

    assert status["writing_test_copies"] is False


def test_wps_status_with_no_effective_targets_is_not_writing_copies(tmp_path, monkeypatch):
    """一张生效表都没有时不能报成「正在写副本」。"""
    bridge = _patch_status_env(monkeypatch, tmp_path, tables={}, production={},
                               effective={})
    assert bridge.wps_status()["writing_test_copies"] is False


def test_wps_status_shape_and_defaults(tmp_path, monkeypatch):
    bridge = _patch_status_env(
        monkeypatch, tmp_path,
        tables={"东湖中餐": {"file_id": "F1"}},
        production={"东湖中餐": {"file_id": "P1"}})

    status = bridge.wps_status()

    assert status["ok"] is True
    assert status["enabled"] is False and status["test_mode"] is False
    assert status["cli_found"] is True and status["cli_path"] == "/fake/kdocs-cli"
    assert status["authenticated"] is True
    assert status["marker_enabled"] is True and status["sort_enabled"] is True
    assert status["state_path"].endswith("wps_sync_state.json")
    # 出厂地址顺序要原样带给界面（「恢复默认」按钮用），且必须是副本。
    # 说明：default_wps_address_order() 本身每次就返回新的 dict + 新的 list，
    # 所以「直接返回它」与「再包一层推导式」等价（变异测试确认属等价变异）；
    # 这条断言锁的是**可观察行为**：改返回值不会污染下一次调用。
    defaults = status["address_order_defaults"]
    assert "东湖中餐" in defaults and isinstance(defaults["东湖中餐"], list)
    defaults["东湖中餐"].append("篡改")
    assert "篡改" not in bridge.wps_status()["address_order_defaults"]["东湖中餐"]


def test_wps_status_target_date_and_tables(tmp_path, monkeypatch):
    bridge = _patch_status_env(
        monkeypatch, tmp_path,
        tables={"东湖中餐": {"file_id": "F1"}},
        production={"东湖中餐": {"file_id": "P1"}})

    status = bridge.wps_status()

    assert status["target_date"] == status["target_date"]   # 形如 2026-09-16
    _dt.date.fromisoformat(status["target_date"])
    # 通讯记号写的是**运行日**的周几
    from app.wps.sync import weekday_number
    assert status["weekday_number"] == weekday_number(_dt.date.today())
    assert [t["sheet"] for t in status["tables"]] == ["东湖中餐"]
    assert status["tables"][0]["file_id"] == "F1"
    assert status["tables"][0]["effective_file_id"] == "F1"
    assert status["tables"][0]["last_sync"] == "" and status["tables"][0]["last_people"] == 0


def test_wps_status_survives_missing_cli(tmp_path, monkeypatch):
    """找不到 kdocs-cli 时要给出 reason，而不是抛异常。"""
    def boom():
        raise WpsCloudError("kdocs-cli 未找到")

    bridge = _patch_status_env(
        monkeypatch, tmp_path,
        tables={"东湖中餐": {"file_id": "F1"}},
        production={"东湖中餐": {"file_id": "P1"}})
    monkeypatch.setattr(bridge, "_wps_cli", boom)

    status = bridge.wps_status()

    assert status["ok"] is True
    assert status["cli_found"] is False and status["cli_path"] == ""
    assert "kdocs-cli 未找到" in status["reason"]


def test_wps_status_reads_ledger_last_sync(tmp_path, monkeypatch):
    """账本里有当天批次时，要带出 last_sync / last_people。"""
    bridge = _patch_status_env(
        monkeypatch, tmp_path,
        tables={"东湖中餐": {"file_id": "F1"}},
        production={"东湖中餐": {"file_id": "P1"}})
    target = bridge.wps_status()["target_date"]
    ledger = SyncLedger(tmp_path / "wps_sync_state.json")
    ledger.record(target, "F1", {"张三": 6, "李四": 3})
    ledger.save()

    status = bridge.wps_status()

    entry = status["tables"][0]
    assert entry["last_people"] == 2
    # synced_at 由 record() 用当前时间写入，形如 2026-09-16T21:00:00
    assert entry["last_sync"]
    _dt.datetime.fromisoformat(entry["last_sync"])


def test_wps_status_without_ledger_entry_reports_never_synced(tmp_path, monkeypatch):
    """账本里没有该表的批次时，last_sync 为空、last_people 为 0。"""
    bridge = _patch_status_env(
        monkeypatch, tmp_path,
        tables={"东湖中餐": {"file_id": "F1"}},
        production={"东湖中餐": {"file_id": "P1"}})

    entry = bridge.wps_status()["tables"][0]

    assert entry["last_sync"] == "" and entry["last_people"] == 0


# ----------------------------------------------------------------------
# worker_alive
# ----------------------------------------------------------------------
def test_worker_alive_is_false_without_a_worker(tmp_path):
    assert _bridge(tmp_path).worker_alive() is False


def test_worker_alive_reflects_thread_state(tmp_path):
    bridge = _bridge(tmp_path)

    class _Alive:
        def is_alive(self) -> bool:
            return True

    class _Dead:
        def is_alive(self) -> bool:
            return False

    bridge._worker = _Alive()  # type: ignore[assignment]
    assert bridge.worker_alive() is True
    bridge._worker = _Dead()  # type: ignore[assignment]
    assert bridge.worker_alive() is False


# ----------------------------------------------------------------------
# set_split_ratio
# ----------------------------------------------------------------------
@pytest.mark.parametrize("given,expected", [
    (0.1, 0.30), (0.38, 0.38), (0.5, 0.5), (0.9, 0.55), ("0.42", 0.42),
    (None, 0.38), ("abc", 0.38),
])
def test_set_split_ratio_clamps_and_persists(tmp_path, given, expected):
    from app.core.config import AppConfig

    bridge = _bridge(tmp_path)
    got = bridge.set_split_ratio(given)  # type: ignore[arg-type]

    assert got == {"ok": True, "ratio": expected}
    assert bridge._config.split_ratio == expected
    # 落盘后重新加载仍是夹紧后的值
    assert AppConfig.load(str(tmp_path / "config.json")).split_ratio == expected


# ----------------------------------------------------------------------
# restore_wps_production_tables
# ----------------------------------------------------------------------
def test_restore_reports_when_no_backup_exists(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._config.wps_production_tables = {}
    assert bridge.restore_wps_production_tables() == {
        "ok": False, "reason": "没有保存正式表备份"}


def test_restore_switches_write_target_back_to_production(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._config.wps_tables = {"东湖中餐": {"file_id": "COPY"}}
    bridge._config.wps_production_tables = {"东湖中餐": {"file_id": "PROD"}}

    got = bridge.restore_wps_production_tables()

    assert got == {"ok": True, "tables": {"东湖中餐": "PROD"}}
    assert bridge._config.wps_tables["东湖中餐"]["file_id"] == "PROD"
    # 切回正式表是**危险动作**，必须留下 WARN 日志
    logs = [e["payload"]["msg"] for e in bridge.drain_events(0)["events"]
            if e["event"] == "log"]
    assert any("切回正式排单表" in line for line in logs)
    levels = [e["payload"]["level"] for e in bridge.drain_events(0)["events"]
              if e["event"] == "log" and "切回正式排单表" in e["payload"]["msg"]]
    assert levels == ["WARN"]
