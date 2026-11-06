"""``Bridge.wps_preview`` 的回归锁（改动前未测过）。

它是「云文档同步」的**预览**入口 —— 用户看着它输出的内容决定要不要真的上传。
两个方向都危险：

* **guard 漏了**：带着未授权 / 没选排单表 / 测试副本没配好的状态去读云端；
* **guard 多了或搞错**：把本该能预览的情况挡掉，用户只能盲传。

其中一条是**安全属性**：测试模式下必须把 ``marker_enabled`` **强制关掉**。
协作者的通讯记号写在正式表上，测试模式若把它也写了，等于在别人的正式表里留下痕迹。

另外它是**只读**的：不写云端、不动本地账本。这一点也要钉住。
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app.api.bridge import Bridge
from app.wps.sync import WpsCloudError
from app.api import bridge as bridge_module


def _bridge(tmp_path) -> Bridge:
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    bridge._config.excel_path = tmp_path / "排单.xlsx"
    bridge._config.excel_path.write_bytes(b"x")
    # W5：云同步默认关闭（wps_enabled=False）时服务端直接拒绝预览/上传。
    # 本文件测的是**已开启**时的预览守卫，因此显式开启；关闭态的行为见
    # tests/test_bridge_wps_gates.py。
    bridge._config.wps_enabled = True
    return bridge


class _Cli:
    path = "/fake/kdocs-cli"

    def __init__(self, *, authenticated: bool = True) -> None:
        self._authenticated = authenticated

    def authenticated(self) -> bool:
        return self._authenticated


@pytest.fixture
def preview_env(tmp_path, monkeypatch):
    """把 wps_preview 的外部依赖全部替换成可控替身，并记录调用参数。"""
    captured: dict = {"build_plan": [], "ledger_saves": [], "read_calls": []}

    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "SyncLedger",
                        lambda *a, **k: type("L", (), {
                            "save": lambda self: captured["ledger_saves"].append(True),
                        })())
    monkeypatch.setattr(bridge_module, "read_local_orders",
                        lambda path, log=None: captured["read_calls"].append(path) or
                        {"东湖中餐": []})

    class _Plan:
        sheet = "东湖中餐"
        warnings = ["名单里有 2 个清单外地址"]

    def fake_build_plan(cli, **kwargs):
        captured["build_plan"].append(kwargs)
        return [_Plan()]

    monkeypatch.setattr(bridge_module, "build_plan", fake_build_plan)
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "预览正文")
    monkeypatch.setattr(bridge_module, "summarize_plan",
                        lambda plans: {"to_update": 1, "to_append": 2})

    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    return bridge, captured, monkeypatch


# ----------------------------------------------------------------------
# 逐个前置守卫：都要返回 ok=False + 明确原因，且绝不抛异常
# ----------------------------------------------------------------------
def test_missing_excel_path_is_refused(tmp_path, preview_env):
    bridge, captured, _ = preview_env
    bridge._config.excel_path = None

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert "排单表" in got["reason"]
    assert captured["build_plan"] == [], "前置条件不满足时不该去读云端"


def test_unconfigured_tables_are_refused(preview_env):
    bridge, _, monkeypatch = preview_env
    monkeypatch.setattr(bridge_module, "effective_tables", lambda _cfg: {})

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert "测试文件 id" in got["reason"]


def test_effective_tables_error_is_surfaced(preview_env):
    bridge, _, monkeypatch = preview_env

    def boom(_cfg):
        raise WpsCloudError("目标表已过期")

    monkeypatch.setattr(bridge_module, "effective_tables", boom)

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert got["status"] == "rejected"
    assert got["code"] == "effective_tables_error"
    assert got["reason"] == "目标表已过期"
    assert got["next_action"]
    assert got["operation_id"] == ""


def test_unauthenticated_cli_is_refused(preview_env):
    bridge, captured, monkeypatch = preview_env
    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli(authenticated=False))

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert "去授权" in got["reason"]
    assert captured["build_plan"] == []


def test_cloud_error_while_building_the_plan_is_surfaced(preview_env):
    bridge, _, monkeypatch = preview_env

    def boom(*_a, **_k):
        raise WpsCloudError("云端表读不了")

    monkeypatch.setattr(bridge_module, "build_plan", boom)

    got = bridge.wps_preview()
    assert got["ok"] is False
    assert got["status"] == "failed"
    assert got["code"] == "plan_failed"
    assert got["reason"] == "云端表读不了"


def test_unexpected_exception_is_reported_not_raised(preview_env):
    """本地 Excel 坏了之类的意外错误也要变成 reason，不能把异常抛给前端。"""
    bridge, _, monkeypatch = preview_env

    def boom(*_a, **_k):
        raise KeyError("bad workbook")

    monkeypatch.setattr(bridge_module, "read_local_orders", boom)

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert got["reason"] == "KeyError: 'bad workbook'", "要带异常类型，便于排查"


# ----------------------------------------------------------------------
# 安全属性：测试模式下强制关掉通讯记号
# ----------------------------------------------------------------------
def test_marker_is_forced_off_in_test_mode(preview_env):
    """测试模式绝不能写协作者的通讯记号（那是正式表上的东西）。"""
    bridge, captured, _ = preview_env
    bridge._config.wps_marker_enabled = True
    bridge._config.wps_test_mode = True

    bridge.wps_preview()

    assert captured["build_plan"][0]["marker_enabled"] is False


def test_marker_follows_config_outside_test_mode(preview_env):
    bridge, captured, _ = preview_env
    bridge._config.wps_marker_enabled = True
    bridge._config.wps_test_mode = False

    bridge.wps_preview()

    assert captured["build_plan"][0]["marker_enabled"] is True


def test_sort_and_address_order_come_from_config(preview_env):
    bridge, captured, _ = preview_env
    bridge._config.wps_sort_enabled = False
    bridge._config.wps_address_order = {"东湖中餐": ["小", "大西"]}

    bridge.wps_preview()

    kwargs = captured["build_plan"][0]
    assert kwargs["sort_enabled"] is False
    assert kwargs["address_order"] == {"东湖中餐": ["小", "大西"]}
    assert kwargs["run_date"] == _dt.date.today()


def test_target_date_uses_the_configured_window(preview_env):
    from app.wps.sync import target_date_for

    bridge, captured, _ = preview_env
    bridge._config.wps_target_hour_start = 20
    bridge._config.wps_target_hour_end = 10

    got = bridge.wps_preview()

    expected = target_date_for(start_hour=20, end_hour=10)
    assert got["target_date"] == expected.isoformat()
    assert captured["build_plan"][0]["target"] == expected


# ----------------------------------------------------------------------
# 只读保证 + 成功返回形状
# ----------------------------------------------------------------------
def test_preview_never_writes_the_ledger(preview_env):
    bridge, captured, _ = preview_env
    bridge.wps_preview()
    assert captured["ledger_saves"] == [], "预览是只读的，绝不能落账本"


def test_success_shape(preview_env):
    bridge, _, _ = preview_env

    got = bridge.wps_preview()

    assert got["ok"] is True
    assert got["status"] == "preview_ready"
    assert got["text"] == "预览正文"
    assert got["summary"] == {"to_update": 1, "to_append": 2}
    assert got["stats"] == got["summary"]
    # 一次性上传令牌与失效判定输入。
    assert got["preview_id"]
    assert got["expires_in"] >= 0
    assert len(got["local_sha256"]) == 64
    assert got["plan_fingerprint"] and got["context_fingerprint"]
    assert got["next_action"] == "wps_upload(preview_id)"
    assert got["tables"] and got["tables"][0]["sheet"] == "东湖中餐"
    assert got["operation_id"] == ""
    # test_mode 如实反映当前配置（注意 AppConfig 默认**开启**测试模式）
    assert got["test_mode"] == bridge._config.wps_test_mode
    assert _dt.date.fromisoformat(got["target_date"])


def test_test_mode_flag_is_reported(preview_env):
    bridge, _, _ = preview_env
    bridge._config.wps_test_mode = True
    assert bridge.wps_preview()["test_mode"] is True


def test_plan_warnings_are_logged_as_warnings(preview_env):
    bridge, _, _ = preview_env

    bridge.wps_preview()

    events = [e for e in bridge.drain_events(0)["events"] if e["event"] == "log"]
    warned = [e["payload"]["msg"] for e in events
              if e["payload"]["level"] == "WARN" and "云同步预览" in e["payload"]["msg"]]
    assert warned and "清单外地址" in warned[0]

# ----------------------------------------------------------------------
# 「重新预览」：连续调用 wps_preview 必须每次都签发新令牌
#
# 前端允许用户连续点「重新预览」；服务端的设计本来就是每次 create 一个
# 新 preview_id，旧令牌不主动作废（靠 10 分钟 TTL / 显式失效兜底）。
# 这组用例钉住：两次都成功、令牌互不相同、旧令牌仍然 valid，且整条只读
# 路径不落账本、不写云端、不调用 apply/upload，也不遗留互斥占位。
# ----------------------------------------------------------------------
def _preview_twice(bridge: Bridge) -> tuple[dict, dict]:
    first = bridge.wps_preview()
    assert first["ok"] is True, first
    second = bridge.wps_preview()
    assert second["ok"] is True, second
    return first, second


def test_consecutive_previews_both_succeed_with_distinct_tokens(preview_env):
    bridge, captured, _ = preview_env

    first, second = _preview_twice(bridge)

    assert first["status"] == "preview_ready"
    assert second["status"] == "preview_ready"
    first_id, second_id = first["preview_id"], second["preview_id"]
    assert first_id, "每次预览都必须签发非空 preview_id"
    assert second_id, "每次预览都必须签发非空 preview_id"
    assert first_id != second_id, "连续预览必须各自发放新的 preview_id"
    # 两次都真的重新走了一遍只读计划构建，而不是复用第一次的结果。
    assert len(captured["build_plan"]) == 2
    assert len(captured["read_calls"]) == 2
    for got in (first, second):
        assert got["next_action"] == "wps_upload(preview_id)"
        assert got["expires_in"] > 0
        assert len(got["plan_fingerprint"]) == 64


def test_first_token_remains_valid_after_a_second_preview(preview_env):
    """第二次预览不得 consume/invalidate 第一次的令牌（否则用户无从选择）。"""
    bridge, _, _ = preview_env

    first, second = _preview_twice(bridge)

    # 沿用本仓库既有观察方式（tests/test_bridge_wps_gates.py 里的
    # bridge._previews.get(old_id)[1]）：空错误码 = 未被消费/作废。
    record, code = bridge._previews.get(first["preview_id"])
    assert code == "", f"第二次预览不得作废旧令牌：{code}"
    assert record is not None
    assert record.preview_id == first["preview_id"]
    assert record.state() == "valid"
    assert record.consumed_at is None
    assert record.invalidated_at is None
    # 两份令牌同时存活，各自可被查到。
    for preview_id in (first["preview_id"], second["preview_id"]):
        assert bridge._previews.get(preview_id)[1] == ""


def test_consecutive_previews_never_write_ledger_cloud_or_apply(preview_env):
    bridge, captured, monkeypatch = preview_env
    apply_calls: list = []
    upload_calls: list = []
    cli_calls: dict = {"authenticated": 0}

    monkeypatch.setattr(bridge_module, "apply_plan",
                        lambda *args, **kwargs: apply_calls.append(args) or {})
    monkeypatch.setattr(bridge, "wps_upload",
                        lambda *args, **kwargs: upload_calls.append(args) or {})

    class _ReadOnlyCli:
        """预览只许做授权探测；任何写云端的方法都算测试失败。"""

        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            cli_calls["authenticated"] += 1
            return True

        def __getattr__(self, name):
            raise AssertionError(f"预览不得调用云端写入方法：{name}")

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _ReadOnlyCli())

    _preview_twice(bridge)

    assert captured["ledger_saves"] == [], "预览是只读的，绝不能落账本"
    assert apply_calls == [], "预览绝不能调用 apply_plan"
    assert upload_calls == [], "预览绝不能调用 wps_upload"
    # build_plan 是替身，云端侧只有两次授权探测，没有任何写方法被触发。
    assert cli_calls == {"authenticated": 2}


def test_consecutive_previews_leave_no_active_operation(preview_env):
    bridge, _, _ = preview_env

    _preview_twice(bridge)

    status = bridge.operation_status()
    assert status["active"] is False, status
    assert status["mode"] == "wps_preview"
    assert status["status"] == "success"
    recent = status["operations"]
    assert [op["mode"] for op in recent[:2]] == ["wps_preview", "wps_preview"]
    assert [op["status"] for op in recent[:2]] == ["success", "success"]
    assert all(op["active"] is False for op in recent[:2])
    # 占位确实释放干净：其他互斥操作立刻能占上（没有遗留锁死）。
    reservation = bridge._operations.try_reserve("order", summary={"title": "订单"})
    assert reservation.granted
    bridge._operations.finish(reservation.operation, status="success")
