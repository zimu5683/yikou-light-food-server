"""Bridge 任务结果映射 P0 回归。

覆盖订单/闪时送 runner 的 failed/partial/unconfirmed/blocked_uncertain/
dry_run/preflight_ok/no_orders 等明确 status，锁死：
- 不能把 failed/partial/uncertain/blocked_uncertain 报成 success；
- 成功型 task:done 只能用于 confirmed；
- 错误事件必须携带 reason/next_action/summary/operation_id；
- 重复完成事件幂等；
- confirmed 正常成功仍然保持 success。
全部离线：直接调用 Bridge 私有 finish 通道，不跑真实 runner、不写 WPS/下单。
"""
from __future__ import annotations

from app.api import bridge as bridge_module
from app.api.bridge import Bridge


def _bridge_with_operation(tmp_path):
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    reservation = bridge._operations.try_reserve("order")
    assert reservation.granted
    operation_id = reservation.operation.operation_id
    bridge._task_operation_id = operation_id
    # 防止旧实现的 worker_alive 分支影响；finish 本身会清空。
    bridge._worker = None
    return bridge, operation_id


def _task_events(bridge):
    return [event for event in bridge.drain_events(0)["events"]
            if event["event"] in ("task:done", "task:error")]


def _is_success_type_task_done(event) -> bool:
    """成功型 task:done 必须同时是 ok=true/success=true 且无 stopped/partial。"""
    if event["event"] != "task:done":
        return False
    payload = event["payload"]
    if payload.get("ok") is False or payload.get("success") is False:
        return False
    return not bool(payload.get("stopped")) and not bool(payload.get("partial"))


def test_failed_runner_result_never_maps_to_success(tmp_path):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    result = {
        "status": "failed",
        "processed": 3,
        "found": 1,
        "failed": 2,
        "reason": "fetch_failed",
        "next_action": "核对站内订单与原表后重试，不要重复提交",
        "summary": {"status": "failed", "failed": 2},
    }

    bridge._finish_task("处理完成：已处理 3 项，找到 1 项", result)

    operation = bridge.operation_status(operation_id)
    assert operation["status"] != "success"
    assert bridge.status != "success"
    events = _task_events(bridge)
    assert not any(_is_success_type_task_done(event) for event in events), events
    errors = [event for event in events if event["event"] == "task:error"]
    assert errors, events
    payload = errors[0]["payload"]
    assert payload["ok"] is False
    assert payload["reason"] == "fetch_failed"
    assert "核对" in payload["next_action"]
    assert payload["summary"]["status"] == "failed"
    assert payload["operation_id"] == operation_id


def test_partial_status_MUST_STAY_PARTIAL(tmp_path):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    result = {
        "status": "partial",
        "processed": 4,
        "found": 2,
        "failed": 1,
        "next_action": "只处理未完成项；核对失败单号后再处理",
        "summary": {"status": "partial", "failed": 1},
    }

    bridge._finish_task("处理完成：已处理 4 项，找到 2 项", result)

    assert bridge.status == "partial"
    operation = bridge.operation_status(operation_id)
    assert operation["status"] == "partial"
    events = _task_events(bridge)
    done = [event for event in events if event["event"] == "task:done"]
    assert done, events
    payload = done[0]["payload"]
    assert payload["partial"] is True
    assert payload["ok"] is False
    assert not _is_success_type_task_done(done[0])
    assert payload["next_action"] == result["next_action"]


def test_post_unconfirmed_uncertain_never_success_and_keeps_review_fields(tmp_path):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    result = {
        "status": "unconfirmed",
        "uncertain": True,
        "processed": 3,
        "created": 1,
        "reason": "reconcile_failed",
        "next_action": "存在未确认或已发送未知订单；只读核对，禁止重试或重跑",
        "summary": {"status": "unconfirmed", "uncertain": 1},
    }

    bridge._finish_task("闪时送任务结束：站内对账失败，无法确认已创建数量", result)

    operation = bridge.operation_status(operation_id)
    assert operation["status"] != "success"
    assert bridge.status != "success"
    events = _task_events(bridge)
    assert not any(_is_success_type_task_done(event) for event in events), events
    error_events = [list(event["payload"].items()) for event in events
                    if event["event"] == "task:error"]
    assert error_events, events
    # task:error payload 也必须带结构化字段，而不是只有一个 message。
    payload = dict(error_events[0]) if error_events else {}
    assert payload.get("reason")
    assert payload.get("next_action")
    assert payload.get("summary")
    assert payload.get("operation_id") == operation_id
    assert payload.get("uncertain") is True


def test_legacy_uncertain_flag_without_status_stays_needs_review(tmp_path):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    result = {
        "processed": 2,
        "created": 0,
        "stopped": False,
        "partial": False,
        "reconciled": False,
        "uncertain": True,
        "summary": {"status": "reconciliation_failed", "uncertain": 1},
    }

    bridge._finish_task("闪时送任务结束：站内对账失败，无法确认已创建数量", result)

    operation = bridge.operation_status(operation_id)
    assert operation["status"] != "success"
    assert bridge.status == "partial"
    events = _task_events(bridge)
    assert not any(_is_success_type_task_done(event) for event in events), events
    done = [event for event in events if event["event"] == "task:done"]
    assert done, events
    payload = done[0]["payload"]
    assert payload["partial"] is True
    assert payload["ok"] is False
    assert payload["uncertain"] is True
    assert "只读核对" in payload["next_action"]


def test_blocked_uncertain_keeps_blocked_review_stopped_semantics(tmp_path):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    result = {
        "status": "blocked_uncertain",
        "uncertain": True,
        "stopped": True,
        "reason": "uncertain-journal-guard",
        "next_action": "先只读核对站内订单与本地不确定记录；未确认前不要重跑或补发",
        "summary": {"status": "blocked_uncertain", "uncertain": 2},
    }

    bridge._finish_task("闪时送任务已停止：存在未解决的不确定记录", result)

    operation = bridge.operation_status(operation_id)
    assert operation["status"] != "success"
    assert bridge.status != "success"
    assert operation["summary"]["blocked"] is True
    assert operation["summary"]["needs_review"] is True
    assert operation["summary"]["stopped"] is True
    events = _task_events(bridge)
    assert not any(_is_success_type_task_done(event) for event in events), events
    errors = [event for event in events if event["event"] == "task:error"]
    assert errors, events
    payload = errors[0]["payload"]
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert payload["needs_review"] is True
    assert payload["stopped"] is True
    assert payload["next_action"] == result["next_action"]


def test_dry_run_does_not_pretend_real_order_success(tmp_path):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    result = {
        "status": "dry_run",
        "processed": 3,
        "previewed": 3,
        "next_action": "干跑未发送任何 POST；确认报文后再正式运行",
        "summary": {"status": "dry_run"},
    }

    bridge._finish_task(
        "闪时送干跑完成：已组装 3 单（名单来源：本地 Excel），未创建真实订单",
        result)

    operation = bridge.operation_status(operation_id)
    assert operation["status"] != "success"
    events = _task_events(bridge)
    assert not any(_is_success_type_task_done(event) for event in events), events
    done = [event for event in events if event["event"] == "task:done"]
    assert done, events
    payload = done[0]["payload"]
    assert payload["ok"] is False
    assert payload["real_order"] is False
    assert payload["status"] == "dry_run"
    assert "未创建真实订单" in payload["message"]
    assert "未发送" in payload["next_action"]


def test_preflight_and_no_orders_are_not_real_order_success(tmp_path):
    for runner_status, message in [
        ("preflight_ok", "闪时送预检完成：已有站内匹配 0 单，未提交新订单"),
        ("no_orders", "闪时送没有需要下单的订单"),
    ]:
        bridge, operation_id = _bridge_with_operation(tmp_path)
        result = {
            "status": runner_status,
            "next_action": ("预检只读模式，未提交新订单；确认后再正式运行"
                            if runner_status == "preflight_ok"
                            else "没有需要下单的订单，无需操作"),
            "summary": {"status": runner_status},
        }

        bridge._finish_task(message, result)

        operation = bridge.operation_status(operation_id)
        assert operation["status"] != "success", runner_status
        events = _task_events(bridge)
        assert not any(_is_success_type_task_done(event) for event in events), events
        done = [event for event in events if event["event"] == "task:done"]
        assert done, events
        assert done[0]["payload"]["ok"] is False
        assert done[0]["payload"]["real_order"] is False
        assert done[0]["payload"]["status"] == runner_status


def test_repeated_finish_events_are_idempotent(tmp_path):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    bridge._finish_task("处理完成：已处理 1 项，找到 1 项", {
        "status": "confirmed", "processed": 1, "found": 1,
        "summary": {"status": "confirmed"},
    })

    bridge._finish_task("处理完成：已处理 1 项，找到 1 项", {
        "status": "confirmed", "processed": 1, "found": 1,
        "summary": {"status": "confirmed"},
    })
    bridge._finish_task("不应被接受", {
        "status": "failed", "processed": 0, "found": 0,
        "reason": "late_failure", "next_action": "late",
        "summary": {"status": "failed"},
    })

    events = _task_events(bridge)
    assert len([e for e in events if e["event"] == "task:done"]) == 1, events
    assert not [e for e in events if e["event"] == "task:error"], events
    operation = bridge.operation_status(operation_id)
    assert operation["status"] == "success"


def test_run_order_failed_status_uses_failure_message(tmp_path, monkeypatch):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    monkeypatch.setattr(bridge_module, "run_job", lambda *_a, **_k: {
        "status": "failed",
        "processed": 2,
        "found": 1,
        "planned": 2,
        "failed": 1,
        "abort_reason": "fetch_failed",
        "next_action": "核对站内订单与原表后重试，不要重复提交",
        "summary": {"status": "failed", "failed": 1},
    })

    bridge._run_order(bridge._config, None, "pw")

    operation = bridge.operation_status(operation_id)
    assert operation["status"] == "error"
    events = _task_events(bridge)
    assert not any(_is_success_type_task_done(event) for event in events), events
    errors = [event for event in events if event["event"] == "task:error"]
    assert errors, events
    payload = errors[0]["payload"]
    assert "失败" in payload["message"]
    assert "核对" in payload["message"]
    assert payload["reason"] == "fetch_failed"
    assert payload["operation_id"] == operation_id


def test_run_order_partial_status_keeps_partial_message(tmp_path, monkeypatch):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    monkeypatch.setattr(bridge_module, "run_job", lambda *_a, **_k: {
        "status": "partial",
        "processed": 3,
        "found": 2,
        "planned": 3,
        "failed": 1,
        "next_action": "只处理未完成项；核对失败单号后再处理",
        "summary": {"status": "partial", "failed": 1},
    })

    bridge._run_order(bridge._config, None, "pw")

    operation = bridge.operation_status(operation_id)
    assert operation["status"] == "partial"
    events = _task_events(bridge)
    done = [event for event in events if event["event"] == "task:done"]
    assert done, events
    payload = done[0]["payload"]
    assert payload["partial"] is True
    assert "部分完成" in payload["message"]
    assert "未完成" in payload["message"] or "核对" in payload["message"]


def test_confirmed_still_reports_real_success(tmp_path):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    result = {
        "status": "confirmed",
        "processed": 2,
        "found": 2,
        "next_action": "",
        "summary": {"status": "confirmed"},
    }

    bridge._finish_task("处理完成：已处理 2 项，找到 2 项", result)

    assert bridge.status == "success"
    operation = bridge.operation_status(operation_id)
    assert operation["status"] == "success"
    events = _task_events(bridge)
    done = [event for event in events if event["event"] == "task:done"]
    assert done, events
    payload = done[0]["payload"]
    assert payload["ok"] is True
    assert payload["real_order"] is True
    assert not [event for event in events if event["event"] == "task:error"]


def test_task_error_exception_style_payload_contains_structured_fields(tmp_path):
    bridge, operation_id = _bridge_with_operation(tmp_path)
    bridge._task_error("接口超时")

    events = _task_events(bridge)
    errors = [event for event in events if event["event"] == "task:error"]
    assert errors, events
    payload = errors[0]["payload"]
    assert payload["reason"] == "task_error"
    assert payload["next_action"]
    assert payload["summary"]
    assert payload["operation_id"] == operation_id


# ----------------------------------------------------------------------
# R6-9：blocked_concurrent（批次级跨进程锁拿不到）不能被说成“站内对账失败”
# ----------------------------------------------------------------------
def test_blocked_concurrent_is_mapped_and_never_reports_reconciliation_failure(
        tmp_path, monkeypatch):
    """runner 在发送任何 POST 之前就退出 → 文案必须是“等待后刷新”，不是对账失败。"""
    bridge, operation_id = _bridge_with_operation(tmp_path)
    result = {
        "status": "blocked_concurrent",
        "processed": 3,
        "created": 0,
        "submitted": 0,
        "stopped": True,
        "partial": False,
        "reconciled": False,
        "uncertain": True,
        "unconfirmed": 3,
        "semantics": "batch-submission-lock",
        "next_action": "另一进程正在处理同一批次或跨进程锁不可用；未发送任何 POST，"
                       "请等待锁释放后重试，严禁并行重跑本批",
    }

    bridge._finish_task("闪时送任务已停止：站内对账失败，无法确认已创建数量", result)

    operation = bridge.operation_status(operation_id)
    assert operation["status"] == "blocked_concurrent"
    assert bridge.status == "blocked_concurrent"
    assert bridge.status != "success"
    events = _task_events(bridge)
    assert not any(_is_success_type_task_done(event) for event in events), events
    errors = [event for event in events if event["event"] == "task:error"]
    assert errors, events
    payload = errors[0]["payload"]
    assert payload["status"] == "blocked_concurrent"
    assert payload["result_status"] == "blocked_concurrent"
    assert payload["ok"] is False and payload["success"] is False
    assert payload["real_order"] is False
    # 保守语义：确实没有创建订单，也确实被阻断。
    assert payload["stopped"] is True
    assert payload["blocked"] is True
    assert payload["partial"] is False
    # 并发阻断不需要人工对账，用户只需等待刷新。
    assert payload["needs_review"] is False
    assert "另一个任务正在运行，请等待后刷新" in payload["message"]
    assert "站内对账失败" not in payload["message"]
    assert "未发送任何下单请求" in payload["message"]
    assert payload["next_action"]
    assert payload["summary"]["bridge_status"] == "blocked_concurrent"
    assert payload["operation_id"] == operation_id


def test_blocked_concurrent_without_next_action_gets_wait_advice(tmp_path):
    """runner 没给 next_action 时，Bridge 也必须给出“等待后刷新”。"""
    bridge, operation_id = _bridge_with_operation(tmp_path)

    bridge._finish_task("闪时送任务失败", {
        "status": "blocked_concurrent",
        "stopped": True,
        "reconciled": False,
        "uncertain": True,
    })

    payload = [event for event in _task_events(bridge)
               if event["event"] == "task:error"][0]["payload"]
    assert payload["next_action"] == "另一个任务正在运行，请等待后刷新"
    assert payload["reason"] == "task_status:blocked_concurrent"
    assert payload["message"] == "另一个任务正在运行，请等待后刷新（本次未发送任何下单请求）"


def test_run_sss_blocked_concurrent_message_chain(tmp_path, monkeypatch):
    """走真实 ``_run_sss`` 消息链：包含 stopped 与 reconciled=False 也要先说并发。"""
    bridge, operation_id = _bridge_with_operation(tmp_path)

    def fake_run_sss_job(config, stop_event, log, **kwargs):
        return {
            "status": "blocked_concurrent",
            "processed": 4, "created": 0, "submitted": 0,
            "stopped": True, "partial": False, "reconciled": False,
            "uncertain": True, "unconfirmed": 4,
            "semantics": "batch-submission-lock",
            "next_action": "另一进程正在处理同一批次或跨进程锁不可用；未发送任何 POST",
        }

    monkeypatch.setattr(bridge_module, "run_sss_job", fake_run_sss_job)
    bridge._run_sss(bridge._config, "pw")

    events = _task_events(bridge)
    errors = [event for event in events if event["event"] == "task:error"]
    assert errors, events
    payload = errors[0]["payload"]
    assert payload["status"] == "blocked_concurrent"
    assert "另一个任务正在运行，请等待后刷新" in payload["message"]
    assert "站内对账失败" not in payload["message"]
    assert payload["summary"]["bridge_status"] == "blocked_concurrent"
    assert payload["next_action"].startswith("另一进程正")
    assert bridge.operation_status(operation_id)["status"] == "blocked_concurrent"
