"""W5 + W7：服务端闸门与只读云入口的统一互斥。

W5：``wps_enabled=False`` 时服务端**自己**拒绝预览与上传（不靠前端隐藏按钮）。
即使客户端手里还攥着关闭之前拿到的 ``preview_id``，也不能写一个字；拒绝时
外部（云端）写入次数必须是 0。

W7：``wps_preview`` / ``wps_check_copies`` / ``sss_day_orders`` 都会读同一批云表
（消耗金山每日额度），``sss_day_orders`` 还会把名单留档进《闪时送.xlsx》——
它可能是闪时送下单来源。三者必须与 ``wps_upload``/订单/闪时送任务共用同一个
原子占位：冲突立即拒绝，不排队、不嵌套（详见 Bridge 内 W7 注释里的跨进程范围）。

全部离线：云端读取用合成替身，不联网、不读真实账本/客户数据。
"""
from __future__ import annotations

from app.api import bridge as bridge_module
from app.api.bridge import Bridge


class _Cli:
    path = "/fake/kdocs-cli"

    def authenticated(self) -> bool:
        return True


def _bridge(tmp_path) -> Bridge:
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    return bridge


def _ready_for_preview(bridge: Bridge, tmp_path, monkeypatch) -> dict:
    """把预览所需的外部依赖换成替身，并记录云端调用次数。"""
    captured = {"build_plan": 0, "ledger_saves": 0, "cloud_reads": 0}
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"x")
    bridge._config.excel_path = excel
    bridge._config.wps_enabled = True
    bridge._config.wps_test_mode = True
    bridge._config.wps_test_tables = {"东湖中餐": "F-TEST-1"}
    bridge._config.wps_tables = {"东湖中餐": {"file_id": "F-PROD-1"}}
    bridge._config.wps_production_tables = {"东湖中餐": {"file_id": "F-PROD-1"}}
    monkeypatch.setattr(bridge_module, "read_local_orders",
                        lambda path, log=None: {"东湖中餐": []})

    class _Ledger:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def save(self):
            captured["ledger_saves"] += 1
            return tmp_path / "ledger.json"

    monkeypatch.setattr(bridge_module, "SyncLedger", _Ledger)

    def fake_build_plan(cli, **kwargs):
        captured["build_plan"] += 1
        captured["cloud_reads"] += 1
        return [type("P", (), {"sheet": "东湖中餐", "warnings": []})()]

    monkeypatch.setattr(bridge_module, "build_plan", fake_build_plan)
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "预览正文")
    monkeypatch.setattr(bridge_module, "summarize_plan",
                        lambda plans: {"to_update": 0, "to_append": 0})
    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    return captured


# ----------------------------------------------------------------------
# W5：wps_enabled=False 服务端拒绝，且零外部写入
# ----------------------------------------------------------------------
def test_w5_disabled_preview_is_refused_with_zero_cloud_touch(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    captured = _ready_for_preview(bridge, tmp_path, monkeypatch)
    bridge._config.wps_enabled = False

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert got["status"] == "rejected"
    assert got["code"] == "wps_disabled"
    assert "关闭" in got["reason"] and "未读取" in got["reason"]
    assert got["next_action"]
    # 零外部写入：连计划都没建、账本没落盘、云端一次没碰。
    assert captured == {"build_plan": 0, "ledger_saves": 0, "cloud_reads": 0}
    assert got["execution_summary"]["proven_no_write"] is True
    assert got["execution_summary"]["rows"]["verified"] == 0
    # 没有发放任何令牌。
    assert "preview_id" not in got or not got.get("preview_id")
    assert bridge._previews._items == {}, "关闭态绝不能签发预览令牌"

    # 更严格：把 CLI 换成“任何调用都算错”的替身，确认连只读云端都没有发生。
    class _ExplodingCli:
        path = "/fake/kdocs-cli"

        def __getattr__(self, name):
            raise AssertionError(f"关闭态不得触碰云端：调用了 {name}")

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _ExplodingCli())
    assert bridge.wps_preview()["code"] == "wps_disabled"
    assert bridge.wps_upload("pv-whatever")["code"] == "wps_disabled"


def test_w5_disabled_upload_refused_even_with_old_preview_id(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    captured = _ready_for_preview(bridge, tmp_path, monkeypatch)

    preview = bridge.wps_preview()
    assert preview["ok"] is True, preview
    old_id = preview["preview_id"]

    # 用户关闭云同步（配置保存路径的真实字段），旧令牌仍然攥在客户端手里。
    bridge._config.wps_enabled = False
    got = bridge.wps_upload(old_id)

    assert got["ok"] is False
    assert got["status"] == "rejected"
    assert got["code"] == "wps_disabled"
    assert got["preview_id"] == old_id
    assert got["written"] == 0
    assert got["execution_summary"]["rows"]["verified"] == 0
    assert got["execution_summary"]["proven_no_write"] is True
    # 旧令牌被作废：重新开启后也不能拿它直接上传。
    assert bridge._previews.get(old_id)[1] == "preview_invalidated"
    assert captured["build_plan"] == 1  # 只有开启时那次预览

    # 重新开启后同一个旧 preview_id 依然不能写。
    bridge._config.wps_enabled = True
    replay = bridge.wps_upload(old_id)
    assert replay["ok"] is False
    assert replay["code"] in ("preview_invalidated", "preview_not_found")
    assert replay["written"] == 0
    assert captured["build_plan"] == 1


def test_w5_disabled_upload_without_preview_id_still_reports_missing_preview(
        tmp_path, monkeypatch):
    """旧无参调用优先按 missing_preview 报，且同样零写入。"""
    bridge = _bridge(tmp_path)
    captured = _ready_for_preview(bridge, tmp_path, monkeypatch)
    bridge._config.wps_enabled = False

    got = bridge.wps_upload("")

    assert got["ok"] is False
    assert got["code"] == "missing_preview"
    assert got["execution_summary"]["proven_no_write"] is True
    assert captured["build_plan"] == 0


# ----------------------------------------------------------------------
# W7：只读云入口与上传/任务统一互斥
# ----------------------------------------------------------------------
def _hold_upload(bridge: Bridge):
    reservation = bridge._operations.try_reserve(
        "wps_upload", summary={"title": "云文档上传"})
    assert reservation.granted
    return reservation.operation


def test_w7_preview_is_refused_while_upload_active(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    captured = _ready_for_preview(bridge, tmp_path, monkeypatch)
    operation = _hold_upload(bridge)
    try:
        got = bridge.wps_preview()
    finally:
        bridge._operations.finish(operation, status="success")

    assert got["ok"] is False
    assert got["status"] == "rejected"
    assert got["code"] == "operation_conflict"
    assert "云文档上传" in got["reason"]
    assert got["next_action"]
    assert got["operation_id"] == operation.operation_id
    assert captured["build_plan"] == 0, "被拒时绝不能开始读云端"
    assert got["execution_summary"]["proven_no_write"] is True
    # 冲突方元数据是最小白名单，不含客户数据。
    conflict = got["conflicting_operation"]
    assert conflict["mode"] == "wps_upload"
    assert "合成" not in str(conflict)


def test_w7_check_copies_is_refused_while_upload_active(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    _ready_for_preview(bridge, tmp_path, monkeypatch)
    cloud_calls = {"n": 0}

    class _CopyCli(_Cli):
        def read_grid(self, *args, **kwargs):
            cloud_calls["n"] += 1
            return {}

    monkeypatch.setattr(bridge, "_wps_cli", lambda: _CopyCli())
    operation = _hold_upload(bridge)
    try:
        got = bridge.wps_check_copies()
    finally:
        bridge._operations.finish(operation, status="success")

    assert got["ok"] is False
    assert got["code"] == "operation_conflict"
    assert got["operation_id"] == operation.operation_id
    assert cloud_calls["n"] == 0, "被拒时不得读云端"


def test_w7_sss_day_orders_is_refused_while_upload_active(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    called = {"n": 0}

    def fake_prepare(config, **kwargs):
        called["n"] += 1
        raise AssertionError("被拒时不得读取云端名单/写留档")

    monkeypatch.setattr("app.api.bridge.prepare_day_orders", fake_prepare)
    operation = _hold_upload(bridge)
    try:
        got = bridge.sss_day_orders()
    finally:
        bridge._operations.finish(operation, status="success")

    assert got["ok"] is False
    assert got["code"] == "operation_conflict"
    assert got["operation_id"] == operation.operation_id
    assert called["n"] == 0


def test_w7_read_entries_keep_legacy_busy_shape_when_worker_runs(tmp_path):
    """旧协议兼容：worker 在跑时仍返回 ok=False + reason（文案含“正在运行”）。"""
    class _AliveWorker:
        def is_alive(self) -> bool:
            return True

    bridge = _bridge(tmp_path)
    bridge._worker = _AliveWorker()
    assert bridge.sss_day_orders()["ok"] is False
    assert "正在运行" in bridge.sss_day_orders()["reason"]


def test_w7_successful_read_releases_slot_and_updates_operation_status(
        tmp_path, monkeypatch):
    """只读入口用完后必须释放槽位：否则一次预览会把 Bridge 永久锁死。"""
    bridge = _bridge(tmp_path)
    _ready_for_preview(bridge, tmp_path, monkeypatch)

    preview = bridge.wps_preview()
    assert preview["ok"] is True
    assert bridge.operation_status()["active"] is False
    recent = bridge.operation_status()["operations"][0]
    assert recent["mode"] == "wps_preview"
    assert recent["status"] == "success"

    # 释放干净后互斥仍然生效：订单任务可以正常占位。
    reservation = bridge._operations.try_reserve("order", summary={"title": "订单"})
    assert reservation.granted
    bridge._operations.finish(reservation.operation, status="success")


def test_w7_failed_read_also_releases_slot(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    captured = _ready_for_preview(bridge, tmp_path, monkeypatch)
    bridge._config.wps_enabled = False
    assert bridge.wps_preview()["ok"] is False

    # 关闭态根本没占位：直接可以起任务。
    reservation = bridge._operations.try_reserve("order", summary={"title": "订单"})
    assert reservation.granted
    bridge._operations.finish(reservation.operation, status="success")
    assert captured["build_plan"] == 0


def test_w7_upload_still_wins_over_read_only_entries(tmp_path, monkeypatch):
    """上传占位期间，三个只读入口全部被拒（同一把占位，不嵌套）。"""
    bridge = _bridge(tmp_path)
    _ready_for_preview(bridge, tmp_path, monkeypatch)
    monkeypatch.setattr("app.api.bridge.prepare_day_orders",
                        lambda config, **kwargs: (_ for _ in ()).throw(
                            AssertionError("不得调用")))
    operation = _hold_upload(bridge)
    try:
        results = {
            "wps_preview": bridge.wps_preview(),
            "wps_check_copies": bridge.wps_check_copies(),
            "sss_day_orders": bridge.sss_day_orders(),
        }
    finally:
        bridge._operations.finish(operation, status="success")

    for name, result in results.items():
        assert result["ok"] is False, name
        assert result["code"] == "operation_conflict", name
    assert bridge.operation_status()["active"] is False


def test_w7_no_deadlock_when_read_entries_race_with_upload(tmp_path, monkeypatch):
    """真实线程并发：上传持占位时三个只读入口必须**立刻**被拒，释放后恢复可用。

    “避免嵌套锁死锁”的可执行证据：占位只在微秒级内存操作里持有、冲突立即返回，
    所以另一个线程的只读请求不会被上传拖住，也不存在“持锁等锁”。
    """
    import threading
    import time

    bridge = _bridge(tmp_path)
    _ready_for_preview(bridge, tmp_path, monkeypatch)

    def forbidden_prepare(config, **kwargs):
        raise AssertionError("被拒时不得读取云端名单/写留档")

    monkeypatch.setattr("app.api.bridge.prepare_day_orders", forbidden_prepare)

    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        reservation = bridge._operations.try_reserve(
            "wps_upload", summary={"title": "云文档上传"})
        assert reservation.granted
        entered.set()
        release.wait(10)
        bridge._operations.finish(reservation.operation, status="success")

    worker = threading.Thread(target=holder, daemon=True)
    worker.start()
    try:
        assert entered.wait(5), "上传线程必须已持占位"
        started = time.monotonic()
        results = {
            "wps_preview": bridge.wps_preview(),
            "wps_check_copies": bridge.wps_check_copies(),
            "sss_day_orders": bridge.sss_day_orders(),
        }
        elapsed = time.monotonic() - started
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive(), "上传线程必须能正常退出（没有死锁）"
    assert elapsed < 3.0, f"只读入口必须立即返回，不允许排队等待：{elapsed:.3f}s"
    for name, result in results.items():
        assert result["ok"] is False, name
        assert result["code"] == "operation_conflict", name

    # 释放后同一实例必须立刻恢复可用（占位没有泄漏、线程锁没有残留）。
    preview = bridge.wps_preview()
    assert preview["ok"] is True, preview
    assert bridge.operation_status()["active"] is False


def test_w7_read_entry_exception_never_leaks_the_slot(tmp_path, monkeypatch):
    """只读入口抛异常时也必须释放占位：否则一次失败会把 Bridge 永久锁死。

    实测反例（独立复核发现）：``wps_check_copies`` 的 ``effective_tables()``
    异常在占位之后抛出，旧写法会让 ``operation_status().active`` 永远为 true，
    之后订单/上传/恢复全部 ``operation_conflict``。
    """
    from app.wps.sync import WpsCloudError

    def boom(_cfg):
        raise WpsCloudError("目标表已过期")

    bridge = _bridge(tmp_path)
    _ready_for_preview(bridge, tmp_path, monkeypatch)
    monkeypatch.setattr(bridge_module, "effective_tables", boom)

    try:
        bridge.wps_check_copies()
    except WpsCloudError:
        pass  # 异常照旧向上抛（既有行为），但槽位必须已释放
    else:  # pragma: no cover - 现有实现会抛
        raise AssertionError("effective_tables 异常应继续向上抛")

    status = bridge.operation_status()
    assert status["active"] is False, status
    assert status["operations"][0]["status"] == "error"
    # 槽位没有泄漏：订单任务仍然起得来。
    reservation = bridge._operations.try_reserve("order", summary={"title": "订单"})
    assert reservation.granted
    bridge._operations.finish(reservation.operation, status="success")


def test_w7_preview_exception_never_leaks_the_slot(tmp_path, monkeypatch):
    """预览在结果组装阶段抛异常（如令牌表写入失败）时同样不得泄漏占位。"""
    bridge = _bridge(tmp_path)
    _ready_for_preview(bridge, tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("令牌表写入失败")

    monkeypatch.setattr(bridge._previews, "create", boom)
    try:
        bridge.wps_preview()
    except RuntimeError:
        pass
    else:  # pragma: no cover - 现有实现会抛
        raise AssertionError("非预期异常应继续向上抛")

    assert bridge.operation_status()["active"] is False
    monkeypatch.undo()
    _ready_for_preview(bridge, tmp_path, monkeypatch)
    assert bridge.wps_preview()["ok"] is True, "释放后必须立刻恢复可用"


def test_w5_disabling_cloud_sync_invalidates_untouched_previews(
        tmp_path, monkeypatch):
    """关闭云同步要立刻作废所有未用令牌，而不是等下一次上传才懒作废。

    反例（独立复核发现）：preview → 关闭 → 重新开启（关闭窗口内一次上传都不调）
    → 旧 preview_id 仍然真的写云。
    """
    bridge = _bridge(tmp_path)
    captured = _ready_for_preview(bridge, tmp_path, monkeypatch)

    preview = bridge.wps_preview()
    assert preview["ok"] is True
    old_id = preview["preview_id"]

    # 走真实配置保存路径：管理员在「云文档同步」里关掉，再打开。
    assert bridge.save_wps_config({"enabled": False})["ok"] is True
    assert bridge._config.wps_enabled is False
    assert bridge._previews.get(old_id)[1] == "preview_invalidated"
    assert bridge.save_wps_config({"enabled": True})["ok"] is True

    got = bridge.wps_upload(old_id)
    assert got["ok"] is False, got
    assert got["code"] in ("preview_invalidated", "preview_not_found")
    assert got["written"] == 0
    assert captured["build_plan"] == 1, "作废的令牌不得再次建计划/写云端"
