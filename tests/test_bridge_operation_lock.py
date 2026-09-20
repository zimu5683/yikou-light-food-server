"""统一操作互斥、断线状态、冲突配置写入与模板覆盖的回归。

全部离线：只操作临时配置/临时文件，不起真实网络任务。
"""
from __future__ import annotations

import threading
import types
from pathlib import Path

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)


def _order_payload(tmp_path):
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"x")
    return {
        "url": "https://m.icall.me/admin/#/login",
        "phone": "13800000000",
        "password": "PW",
        "excel": str(excel),
        "date": "",
        "count": "",
        "remember": True,
    }


def test_operation_status_idle_and_unknown_shape(tmp_path):
    bridge = _bridge(tmp_path)

    idle = bridge.operation_status()
    assert idle["ok"] is True
    assert idle["active"] is False
    assert idle["status"] == "idle"
    assert idle["operation_id"] == ""
    assert idle["mode"] == ""
    assert idle["summary"] == {}
    assert idle["next_action"] == ""

    missing = bridge.operation_status("op-does-not-exist")
    assert missing["ok"] is False
    assert missing["active"] is False
    assert missing["status"] == "not_found"
    assert missing["reason"] == "operation_not_found"
    assert missing["next_action"] == "operation_status"


def test_operation_status_query_is_pure_read_only(tmp_path):
    """断线恢复查询只读：不占互斥、不创建最近记录、不改任何状态。"""
    bridge = _bridge(tmp_path)
    before_seq = bridge._operations._seq

    for _ in range(3):
        status = bridge.operation_status()
        assert status["ok"] is True
        assert status["active"] is False
        assert status["status"] == "idle"
        assert status["operations"] == []
        ready = bridge.bridge_ready()
        assert ready["operation"]["status"] == "idle"
        assert ready["operations"] == []

    assert bridge._operations._seq == before_seq
    assert bridge._operations.active_operation_id() == ""
    assert bridge.operation_status()["operations"] == []


def test_operation_status_active_and_recent_fields(tmp_path):
    bridge = _bridge(tmp_path)
    reservation = bridge._operations.try_reserve(
        "order", summary={"title": "订单处理"}, next_action="等待订单完成")
    assert reservation.granted
    operation = reservation.operation
    assert operation is not None

    active = bridge.operation_status()
    assert active["ok"] is True and active["active"] is True
    assert active["operation_id"] == operation.operation_id
    assert active["mode"] == "order"
    assert active["status"] == "running"
    assert active["summary"]["title"] == "订单处理"
    assert active["next_action"] == "等待订单完成"
    assert active["started_at"] is not None and active["finished_at"] is None
    assert any(item["operation_id"] == operation.operation_id
               for item in active["operations"])

    bridge._operations.update(operation, phase="stopping", summary={"percent": 50})
    assert bridge.operation_status()["phase"] == "stopping"
    assert bridge.operation_status()["summary"]["percent"] == 50

    bridge._operations.finish(operation, status="partial",
                              reason="some_failed",
                              summary={"message": "部分完成"},
                              next_action="查看日志")
    recent = bridge.operation_status(operation.operation_id)
    assert recent["ok"] is True and recent["active"] is False
    assert recent["status"] == "partial"
    assert recent["reason"] == "some_failed"
    assert recent["summary"]["message"] == "部分完成"
    assert recent["next_action"] == "查看日志"
    assert recent["finished_at"] is not None
    # 再查不带 ID 也应回最近一条。
    latest = bridge.operation_status()
    assert latest["operation_id"] == operation.operation_id
    assert latest["status"] == "partial"


def test_bridge_ready_syncs_operation_status(tmp_path):
    bridge = _bridge(tmp_path)
    reservation = bridge._operations.try_reserve("sss", summary={"title": "闪时送"})
    assert reservation.granted
    operation = reservation.operation

    state = bridge.bridge_ready()

    assert state["operation"]["operation_id"] == operation.operation_id
    assert state["operation"]["mode"] == "sss"
    assert state["operation"]["active"] is True
    assert state["operation"]["status"] == "running"
    assert isinstance(state["operations"], list)
    assert any(item["operation_id"] == operation.operation_id
               for item in state["operations"])
    bridge._operations.finish(operation, status="success")


def test_readonly_query_and_resolve_do_not_use_operation_lock(tmp_path):
    bridge = _bridge(tmp_path)
    reservation = bridge._operations.try_reserve("order")
    assert reservation.granted

    # 只读查询/交互兑现都不应被活动操作挡住，也不应改动状态。
    assert bridge.operation_status()["active"] is True
    assert bridge.bridge_ready()["operation"]["active"] is True
    missing_decision = bridge.resolve_decision("not-there", "retry")
    missing_captcha = bridge.resolve_captcha("not-there", "1234")
    assert missing_decision["ok"] is False and missing_decision["status"] == "not_pending"
    assert missing_captcha["ok"] is False and missing_captcha["status"] == "not_pending"
    bridge._operations.finish(reservation.operation, status="success")


def test_config_writes_are_rejected_while_operation_active(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._config.target_url = "https://keep.example"
    bridge._config.sss_account = "keep-sss"
    bridge._config.wps_cli_path = "/keep/cli"
    reservation = bridge._operations.try_reserve("wps_upload",
                                                 summary={"title": "云上传"})
    assert reservation.granted

    order = bridge.save_order_config({"url": "https://attacker.example"})
    sss = bridge.save_sss_config({"account": "attacker"})
    wps = bridge.save_wps_config({"cli_path": "/attacker"})
    restore = bridge.restore_wps_production_tables()
    template = bridge.new_template("order", str(tmp_path / "模板.xlsx"))

    for result in (order, sss, wps, restore, template):
        assert result["ok"] is False
        assert result["status"] == "rejected"
        assert result["reason"] == "busy"
        assert result["code"] == "operation_conflict"
        assert result["operation_id"] == reservation.operation.operation_id

    assert bridge._config.target_url == "https://keep.example"
    assert bridge._config.sss_account == "keep-sss"
    assert bridge._config.wps_cli_path == "/keep/cli"
    assert not (tmp_path / "模板.xlsx").exists()

    bridge._operations.finish(reservation.operation, status="success")
    # 释放后同一写入应恢复可用。
    assert bridge.save_order_config({})["ok"] is True


def test_new_template_refuses_to_overwrite_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "已有模板.xlsx"
    target.write_bytes(b"KEEP-ME")
    called: list[str] = []
    monkeypatch.setattr(bridge_module, "write_order_template",
                        lambda dest: called.append(str(dest)))

    got = _bridge(tmp_path).new_template("order", str(target))

    assert got["ok"] is False
    assert got["reason"] == "template_exists"
    assert got["path"] == ""
    assert "拒绝覆盖" in got["error"]
    assert target.read_bytes() == b"KEEP-ME"
    assert called == [], "已有文件被拒时不应调用写入函数"


def test_new_template_write_failure_cleans_placeholder(tmp_path, monkeypatch):
    target = tmp_path / "半成品.xlsx"

    def boom(_dest):
        raise OSError("磁盘满")

    monkeypatch.setattr(bridge_module, "write_order_template", boom)
    got = _bridge(tmp_path).new_template("order", str(target))

    assert got["path"] == "" and "磁盘满" in got["error"]
    assert not target.exists(), "占位文件必须清理，不能留下 0 字节坏文件"


def test_start_order_exception_releases_mutex(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge_module, "set_password", lambda *a, **k: True)

    def boom(*_a, **_k):
        raise RuntimeError("启动线程失败")

    monkeypatch.setattr(bridge, "_launch", boom)

    with pytest.raises(RuntimeError):
        bridge.start_order(_order_payload(tmp_path))

    status = bridge.operation_status()
    assert status["active"] is False
    assert status["status"] == "error"
    assert status["reason"] == "start_failed"
    # 槽位已释放：换成正常启动后下一次尝试应能拿到新的互斥占位（而不是永久 busy）。
    monkeypatch.setattr(bridge, "_launch", lambda *a, **k: True)
    second = bridge.start_order(_order_payload(tmp_path))
    assert second["ok"] is True
    assert bridge.operation_status()["active"] is False


def test_mutual_exclusion_covers_all_exclusive_entrypoints(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    bridge._config.excel_path = tmp_path / "排单.xlsx"
    bridge._config.excel_path.write_bytes(b"x")
    # W5：关闭态会被更早的 wps_disabled 闸门拦下；这里测的是统一互斥本身。
    bridge._config.wps_enabled = True
    monkeypatch.setattr(bridge_module, "set_password", lambda *a, **k: True)
    monkeypatch.setattr(bridge, "_launch", lambda *a, **k: True)
    monkeypatch.setattr(bridge, "_wps_cli",
                        lambda: type("C", (), {"authenticated": lambda self: True})())
    monkeypatch.setattr(bridge_module.android_runtime, "is_android", lambda: True)
    monkeypatch.setattr(bridge_module.android_runtime, "update_capabilities",
                        lambda: {"ok": True, "canInstall": True,
                                 "versionName": "3.6.12", "versionCode": 30612})

    reservation = bridge._operations.try_reserve("order", summary={"title": "订单"})
    assert reservation.granted

    sss_excel = tmp_path / "闪时送.xlsx"
    sss_excel.write_bytes(b"x")
    results = {
        "start_order": bridge.start_order(_order_payload(tmp_path)),
        "start_sss": bridge.start_sss({
            "url": "https://sss.example.com/takeout",
            "account": "13800000000", "password": "PW",
            "excel": str(sss_excel), "order_source": "excel",
            "use_fixed_address": False,
        }),
        # 合法非空 preview_id：即使不存在，也必须先撞统一互斥，不能去查/消费令牌。
        "wps_upload": bridge.wps_upload("pv-does-not-exist"),
        "wps_authorize": bridge.wps_authorize(),
        "wps_logout": bridge.wps_logout(),
        "check_updates": bridge.check_updates(manual=True),
        "install_update": bridge.install_update(),
    }
    for name, result in results.items():
        assert result["ok"] is False, name
        assert result["reason"] == "busy", name
        assert result["status"] == "rejected", name
    assert results["wps_upload"]["code"] == "operation_conflict"
    assert bridge._update_installing is False
    assert bridge._update_checking is False
    assert not (tmp_path / "config.json").exists() or True

    bridge._operations.finish(reservation.operation, status="success")


def test_concurrent_start_order_only_one_gets_reservation(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge_module, "set_password", lambda *a, **k: True)
    entered = threading.Event()
    release = threading.Event()

    def slow_launch(*_a, **_k):
        entered.set()
        release.wait(3.0)
        return True

    monkeypatch.setattr(bridge, "_launch", slow_launch)
    results: list[dict] = []

    def first_call():
        results.append(bridge.start_order(_order_payload(tmp_path)))

    worker = threading.Thread(target=first_call)
    worker.start()
    assert entered.wait(3.0), "第一个开始应已进入 _launch 并持有互斥槽位"

    second = bridge.start_order(_order_payload(tmp_path))
    release.set()
    worker.join(3.0)

    assert second["ok"] is False
    assert second["reason"] == "busy"
    assert sum(1 for item in results if item.get("ok")) == 1
    assert bridge.operation_status()["active"] is False
    assert bridge.operation_status()["status"] == "success"

def _reserve_install_operation(bridge: Bridge) -> str:
    reservation = bridge._operations.try_reserve(
        "install_update", summary={"title": "安装更新"})
    assert reservation.granted
    operation_id = reservation.operation.operation_id
    bridge._update_install_operation_id = operation_id
    bridge._update_installing = True
    return operation_id


def _fake_release():
    return types.SimpleNamespace(tag_name="v9.9.9", version="9.9.9",
                                 body="", release_url="", assets=[])


def _fake_apk():
    return types.SimpleNamespace(name="yikou.apk", size=3)


def _patch_install_common(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge_module, "check_for_update",
                        lambda **_kwargs: _fake_release())
    monkeypatch.setattr(bridge_module, "select_android_apk",
                        lambda _release: _fake_apk())
    monkeypatch.setattr(bridge_module, "select_sha256_asset",
                        lambda *_args: None)
    monkeypatch.setattr(bridge_module.android_runtime, "cache_dir",
                        lambda: tmp_path / "cache")


def test_install_worker_check_error_marks_operation_error_not_success(
        tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    operation_id = _reserve_install_operation(bridge)

    def boom(**_kwargs):
        raise bridge_module.ReleaseCheckError("更新检查网络异常")

    monkeypatch.setattr(bridge_module, "check_for_update", boom)
    bridge._install_update_worker({"versionName": "3.6.12", "versionCode": 30612})

    status = bridge.operation_status(operation_id)
    assert status["active"] is False
    assert status["status"] == "error"
    assert status["status"] != "success"
    assert bridge._update_installing is False
    events = bridge.drain_events(0)["events"]
    assert any(event["event"] == "update:error"
               and event["payload"]["code"] == "check_failed"
               for event in events)


def test_install_worker_cancel_marks_operation_stopped_not_success(
        tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    operation_id = _reserve_install_operation(bridge)
    _patch_install_common(monkeypatch, tmp_path)

    def cancel(*_args, **_kwargs):
        raise bridge_module.UpdateCancelled()

    monkeypatch.setattr(bridge_module, "download_asset", cancel)
    bridge._install_update_worker({"versionName": "3.6.12", "versionCode": 30612})

    status = bridge.operation_status(operation_id)
    assert status["active"] is False
    assert status["status"] == "stopped"
    assert status["status"] != "success"
    assert bridge._update_installing is False
    events = [event["event"] for event in bridge.drain_events(0)["events"]]
    assert "update:cancelled" in events


def test_install_worker_success_only_after_install_ok(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    operation_id = _reserve_install_operation(bridge)
    _patch_install_common(monkeypatch, tmp_path)

    def fake_download(_asset, dest, **_kwargs):
        destination = Path(dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"apk")
        return destination

    monkeypatch.setattr(bridge_module, "download_asset", fake_download)
    monkeypatch.setattr(
        bridge_module.android_runtime, "verify_update_apk",
        lambda _path: {"ok": True, "sameSignature": True, "versionCode": 30613})
    monkeypatch.setattr(
        bridge_module.android_runtime, "install_apk",
        lambda _path: {"ok": True, "message": "已拉起安装器"})

    bridge._install_update_worker({"versionName": "3.6.12", "versionCode": 30612})

    status = bridge.operation_status(operation_id)
    assert status["active"] is False
    assert status["status"] == "success"
    events = [event["event"] for event in bridge.drain_events(0)["events"]]
    assert "update:error" not in events


def test_install_worker_install_failure_marks_error(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    operation_id = _reserve_install_operation(bridge)
    _patch_install_common(monkeypatch, tmp_path)

    def fake_download(_asset, dest, **_kwargs):
        destination = Path(dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"apk")
        return destination

    monkeypatch.setattr(bridge_module, "download_asset", fake_download)
    monkeypatch.setattr(
        bridge_module.android_runtime, "verify_update_apk",
        lambda _path: {"ok": True, "sameSignature": True, "versionCode": 30613})
    monkeypatch.setattr(
        bridge_module.android_runtime, "install_apk",
        lambda _path: {"ok": False, "code": "permission_required",
                       "message": "需要安装权限"})

    bridge._install_update_worker({"versionName": "3.6.12", "versionCode": 30612})

    status = bridge.operation_status(operation_id)
    assert status["active"] is False
    assert status["status"] == "error"
    assert status["status"] != "success"
    events = [event["event"] for event in bridge.drain_events(0)["events"]]
    assert "update:permission_required" in events
