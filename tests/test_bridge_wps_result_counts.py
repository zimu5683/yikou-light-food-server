"""W6：计划口径（planned_summary）与执行口径（execution_summary）必须分开。

R6 反例 4.6（``probe_report_counts.py``）证明旧实现把 ``summarize_plan`` 的
**计划行数**直接当成结果摘要报给日志与 UI：执行器返回
``upload ok: False | status: uncertain | written: 0`` 时，界面仍然显示
``更新 1 · 新增 0``（与预览完全相同），用户会以为真的改了 1 行。

本文件钉住固定统计口径：

* ``planned_summary``：``kind="plan"``，行数放在 ``rows.to_update/to_append/...``；
* ``execution_summary``：``kind="execution"``，``sheets.*`` 是**表数**，
  ``rows.*`` 才是行数；拿不到可证明的行数一律 ``None``（未知），
  ``rows_unknown=True``；
* 只有 ``ok=True`` 且逐表 ``status=="ok"`` 时 ``rows.verified`` 才是执行器证明
  的整数；其余情况不得回落到计划数；
* 日志分两行“计划：…”“实际：…”，不再出现“完成：更新 N 行”。

真实 build_plan/apply_plan 的端到端口径见 ``tests/test_bridge_wps_e2e.py``；
本文件用受控执行器返回值覆盖各种失败/畸形组合（这些值无法靠合成云表稳定造出）。
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge
from app.wps.errors import LedgerCorruptError
from app.wps.models import Change, SheetPlan


class _Cli:
    path = "/fake/kdocs-cli"

    def authenticated(self) -> bool:
        return True


def _plan() -> SheetPlan:
    """一张表、一个需要更新的老行：真实 summarize_plan → to_update = 1。"""
    change = Change(
        kind="existing", name="合成客户", phone="13800000000", row=4, delta=1,
        target_col=6, total_before=2, total_after=3, target_ok=True, slot=1,
        local_meals=1,
    )
    return SheetPlan(
        sheet="东湖中餐", file_id="F1", target_date=_dt.date(2026, 9, 20),
        target_col=6, target_header="9.20", weekday_number=5, changes=[change],
        columns={"name": 1, "phone": 3, "address": 2},
    )


@pytest.fixture
def counts_env(tmp_path, monkeypatch):
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"v1")
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    bridge._config.excel_path = excel
    bridge._config.wps_enabled = True
    bridge._config.wps_marker_enabled = False
    state: dict = {"apply": None}

    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})

    class _Ledger:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def save(self):
            return tmp_path / "ledger.json"

    monkeypatch.setattr(bridge_module, "SyncLedger", _Ledger)
    monkeypatch.setattr(bridge_module, "read_local_orders",
                        lambda path, log=None: {"东湖中餐": []})
    monkeypatch.setattr(bridge_module, "build_plan",
                        lambda cli, **kwargs: [_plan()])
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "预览正文")

    def fake_apply(cli, plans, **kwargs):
        result = state["apply"]
        if isinstance(result, Exception):
            raise result
        return json.loads(json.dumps(result))  # 深拷贝，避免被就地修改

    monkeypatch.setattr(bridge_module, "apply_plan", fake_apply)
    monkeypatch.setattr(bridge, "_wps_cli", lambda: _Cli())
    return bridge, state


def _upload(env) -> dict:
    bridge, state = env
    preview = bridge.wps_preview()
    assert preview["ok"] is True, preview
    return bridge.wps_upload(preview["preview_id"])


def _logs(bridge: Bridge) -> list[str]:
    return [event["payload"]["msg"] for event in bridge.drain_events(0)["events"]
            if event["event"] == "log"]


def test_w6_ok_result_reports_real_row_count_and_marks_plan_separately(counts_env):
    bridge, state = counts_env
    state["apply"] = {
        "status": "ok", "uncertain": False, "written": 1, "failed": 0,
        "sheets": [{"sheet": "东湖中餐", "status": "ok", "people": 3,
                    "problems": []}],
    }

    got = _upload(counts_env)

    assert got["ok"] is True
    assert got["planned_summary"]["kind"] == "plan"
    assert got["planned_summary"]["rows"]["to_update"] == 1
    execution = got["execution_summary"]
    assert execution["sheets"]["verified"] == 1
    assert execution["rows"]["verified"] == 3, "行数必须来自执行器证明的 people"
    assert execution["rows"]["planned"] == 1
    assert execution["rows_unknown"] is False
    assert got["summary"] == execution and got["stats"] == execution
    logs = _logs(bridge)
    assert any(line.startswith("[云同步] 计划：更新 1 行") for line in logs), logs
    assert any("实际：实际已验证 3 行" in line for line in logs), logs


def test_w6_uncertain_without_counts_marks_rows_unknown_not_plan(counts_env):
    """R6 probe_report_counts 的原始场景：written=0 + 计划 1 行 → 行数未知。"""
    bridge, state = counts_env
    state["apply"] = {
        "status": "uncertain", "uncertain": True, "written": 0, "failed": 1,
        "next_action": "manual_reconcile", "reason": "写入后回读校验未通过",
        "sheets": [{"sheet": "东湖中餐", "status": "uncertain", "uncertain": True,
                    "reason": "写入后回读校验未通过", "problems": ["第 4 行不一致"]}],
    }

    got = _upload(counts_env)

    assert got["ok"] is False and got["status"] == "uncertain"
    execution = got["execution_summary"]
    assert execution["sheets"]["uncertain"] == 1
    assert execution["rows"]["verified"] is None
    assert execution["rows"]["uncertain"] is None
    assert execution["rows_unknown"] is True
    assert execution["rows"]["planned"] == 1  # 计划数只出现在带 kind=plan 的字段里
    # 旧行为：summary 直接等于计划摘要（含 to_update）。
    assert "to_update" not in got["summary"]
    assert got["planned_summary"]["rows"]["to_update"] == 1
    logs = _logs(bridge)
    assert any("实际写入行数未知" in line for line in logs), logs
    assert not any("完成：更新" in line for line in logs), logs


def test_w6_mixed_sheets_counts_states_separately(counts_env):
    """逐表状态各自计数：ok / noop / failed / uncertain / skipped 不混为一谈。"""
    bridge, state = counts_env
    state["apply"] = {
        "status": "partial", "uncertain": False, "written": 1, "failed": 2,
        "sheets": [
            {"sheet": "A", "status": "ok", "people": 2, "problems": []},
            {"sheet": "B", "status": "noop", "problems": []},
            {"sheet": "C", "status": "failed", "reason": "回滚后确认未写入"},
            {"sheet": "D", "status": "stale_batch", "reason": "批次日期不符"},
            {"sheet": "E", "status": "skipped", "reason": "未找到目标日期列"},
        ],
    }

    got = _upload(counts_env)

    execution = got["execution_summary"]
    assert execution["sheets"] == {
        "total": 5, "verified": 1, "noop": 1, "failed": 2,
        "uncertain": 0, "skipped": 1, "blocked": 0, "other": 0,
    }
    # 只有 ok 表贡献行数；noop/skipped 证明零写入，failed 表由执行器保证零写入。
    assert execution["rows"]["verified"] == 2
    assert execution["rows"]["failed"] == 0
    assert execution["rows"]["uncertain"] == 0
    assert execution["rows"]["skipped"] == 0
    assert execution["rows_unknown"] is False


def test_w6_ok_sheet_without_people_count_is_unknown(counts_env):
    """ok 表没有 people 计数时不能猜行数（例如旧版执行器）。"""
    bridge, state = counts_env
    state["apply"] = {
        "status": "ok", "uncertain": False, "written": 1, "failed": 0,
        "sheets": [{"sheet": "东湖中餐", "status": "ok", "problems": []}],
    }

    got = _upload(counts_env)

    execution = got["execution_summary"]
    assert execution["sheets"]["verified"] == 1
    assert execution["rows"]["verified"] is None
    assert execution["rows_unknown"] is True


def test_w6_malformed_sheets_cannot_claim_rows(counts_env):
    bridge, state = counts_env
    state["apply"] = {
        "status": "ok", "uncertain": False, "written": 1, "failed": 0,
        "sheets": {"东湖中餐": {"status": "ok"}},   # 不是列表：结构畸形
    }

    got = _upload(counts_env)

    assert got["ok"] is False, got
    execution = got["execution_summary"]
    assert execution["sheets"]["total"] == 0
    assert execution["rows"]["verified"] is None
    assert execution["rows"]["failed"] is None
    assert execution["rows_unknown"] is True


def test_w6_executor_exception_marks_all_rows_unknown(counts_env):
    """apply_plan 抛错时可能已写一部分：四个行数全部未知，且不得声称零写入。"""
    bridge, state = counts_env
    state["apply"] = RuntimeError("模拟写入过程中断")

    got = _upload(counts_env)

    assert got["ok"] is False
    execution = got["execution_summary"]
    assert execution["executed"] is False
    assert execution["proven_no_write"] is False
    for key in ("verified", "failed", "uncertain", "skipped"):
        assert execution["rows"][key] is None, key
    assert got["written"] is None and got["failed"] is None
    assert "to_update" not in got["summary"]


def test_w6_ledger_corrupt_during_apply_is_blocked_with_unknown_rows(counts_env):
    bridge, state = counts_env
    state["apply"] = LedgerCorruptError("账本损坏")

    got = _upload(counts_env)

    assert got["ok"] is False
    assert got["status"] == "blocked"
    assert got["code"] == "local_state_blocked"
    assert "修复本地日志" in got["next_action"]
    execution = got["execution_summary"]
    assert execution["proven_no_write"] is False
    assert execution["rows"]["verified"] is None


def test_w6_pre_apply_rejections_are_proven_zero_write(counts_env):
    """在消费令牌/写入之前拒绝的路径可以证明零写入（rows 为 0，而不是未知）。"""
    bridge, _state = counts_env

    got = bridge.wps_upload("pv-does-not-exist")

    assert got["ok"] is False
    assert got["written"] == 0 and got["failed"] == 0
    execution = got["execution_summary"]
    assert execution["proven_no_write"] is True
    assert execution["rows"]["verified"] == 0
    assert execution["rows_unknown"] is False


def test_w6_plan_summary_is_never_labelled_execution(counts_env):
    """两个口径的 kind 字段必须稳定，前端据此分流，不靠猜字段是否存在。"""
    bridge, state = counts_env
    state["apply"] = {
        "status": "ok", "uncertain": False, "written": 1, "failed": 0,
        "sheets": [{"sheet": "东湖中餐", "status": "ok", "people": 1}],
    }
    preview = bridge.wps_preview()
    assert preview["planned_summary"]["kind"] == "plan"
    assert preview["execution_summary"]["kind"] == "execution"
    assert preview["execution_summary"]["proven_no_write"] is True
    assert preview["summary"] == preview["stats"]  # 预览仍保留旧字段

    got = bridge.wps_upload(preview["preview_id"])
    assert got["planned_summary"]["kind"] == "plan"
    assert got["execution_summary"]["kind"] == "execution"
    assert got["execution_summary"]["contract_version"] == 1


def test_w6_top_level_uncertain_with_ok_sheet_still_marks_rows_unknown(counts_env):
    """整轮结果 uncertain 时，即使某张表 ok 也不能把它的行数当“已验证总数”。"""
    bridge, state = counts_env
    state["apply"] = {
        "status": "ok", "uncertain": True, "written": 1, "failed": 0,
        "sheets": [{"sheet": "东湖中餐", "status": "ok", "people": 2}],
    }

    got = _upload(counts_env)

    assert got["ok"] is False and got["status"] == "uncertain"
    execution = got["execution_summary"]
    assert execution["sheets"]["verified"] == 1
    assert execution["rows"]["verified"] is None, "整轮不确定时不得给出数字行数"
    assert execution["rows_unknown"] is True
    assert "to_update" not in got["summary"]


def test_w6_executed_with_no_sheet_reports_is_unknown(counts_env):
    """执行器没回报任何表时，0 张表不能推出“已验证 0 行”。"""
    bridge, state = counts_env
    state["apply"] = {"status": "failed", "uncertain": False,
                      "written": 0, "failed": 1, "sheets": []}

    got = _upload(counts_env)

    assert got["ok"] is False
    execution = got["execution_summary"]
    assert execution["sheets"]["total"] == 0
    assert execution["rows"]["verified"] is None
    assert execution["rows_unknown"] is True


def test_w6_aggregate_failure_with_ok_sheet_is_contradictory_unknown(counts_env):
    """顶层失败却说某张表 ok：自相矛盾，行数按未知处理。"""
    bridge, state = counts_env
    state["apply"] = {
        "status": "failed", "uncertain": False, "written": 1, "failed": 0,
        "sheets": [{"sheet": "东湖中餐", "status": "ok", "people": 3}],
    }

    got = _upload(counts_env)

    assert got["ok"] is False and got["status"] == "failed"
    assert got["execution_summary"]["sheets"]["verified"] == 1
    assert got["execution_summary"]["rows"]["verified"] is None
    assert got["execution_summary"]["rows_unknown"] is True


def test_w6_post_write_exception_does_not_claim_rejected_before_write(
        counts_env, monkeypatch):
    """写入之后的结果组装异常：不能把 counts_source 写成 rejected_before_write。"""
    bridge, state = counts_env
    state["apply"] = {
        "status": "ok", "uncertain": False, "written": 1, "failed": 0,
        "sheets": [{"sheet": "东湖中餐", "status": "ok", "people": 2}],
    }
    calls = {"n": 0}
    real = bridge._wps_structured_tables

    def boom(plans):
        calls["n"] += 1
        if calls["n"] >= 2:      # 第 2 次是 _wps_apply_plan 里的结果组装
            raise RuntimeError("结果组装失败（写入已完成）")
        return real(plans)

    monkeypatch.setattr(bridge, "_wps_structured_tables", boom)
    got = _upload(counts_env)

    assert got["ok"] is False
    assert got["code"] == "unexpected"
    execution = got["execution_summary"]
    assert execution["counts_source"] == "unknown_after_exception"
    assert execution["proven_no_write"] is False
    for key in ("verified", "failed", "uncertain", "skipped"):
        assert execution["rows"][key] is None, key
    assert got["written"] is None and got["failed"] is None


def test_w6_negative_and_duplicate_people_are_not_counted(counts_env):
    """畸形逐表计数（负数 / 同一张表重复上报）不得被当成“已证明的行数”。"""
    bridge, state = counts_env
    state["apply"] = {
        "status": "ok", "uncertain": False, "written": 1, "failed": 0,
        "sheets": [{"sheet": "东湖中餐", "status": "ok", "people": -5}],
    }
    negative = _upload(counts_env)
    assert negative["execution_summary"]["rows"]["verified"] is None
    assert negative["execution_summary"]["rows_unknown"] is True

    bridge2, state2 = counts_env
    state2["apply"] = {
        "status": "ok", "uncertain": False, "written": 2, "failed": 0,
        "sheets": [{"sheet": "东湖中餐", "status": "ok", "people": 2},
                   {"sheet": "东湖中餐", "status": "ok", "people": 3}],
    }
    duplicated = _upload(counts_env)
    execution = duplicated["execution_summary"]
    assert execution["sheets"]["verified"] == 2
    assert execution["rows"]["verified"] is None, "重复上报不得累加成 5"
    assert execution["rows_unknown"] is True


def test_w6_missing_sheet_key_and_non_dict_items_never_break_reporting(counts_env):
    """畸形逐表记录（非 dict / 缺 sheet 键）不能让“已写入完成”的上报抛异常。"""
    bridge, state = counts_env
    state["apply"] = {
        "status": "ok", "uncertain": False, "written": 1, "failed": 0,
        "sheets": ["坏记录", {"status": "ok", "people": 2}],
    }

    got = _upload(counts_env)

    # 仍然返回完整 JSON（不是 500/异常），行数按未知上报。
    assert isinstance(got, dict)
    assert got["code"] != "unexpected", got
    # 关键：没有掉进“写后异常”兜底 —— 说明上报阶段本身已经容错。
    assert got["execution_summary"]["counts_source"] == "apply_plan"
    assert got["execution_summary"]["rows"]["verified"] is None
    assert got["execution_summary"]["rows_unknown"] is True
    assert got["execution_summary"]["proven_no_write"] is False
    logs = _logs(bridge)
    assert any("?：" in line or "?" in line for line in logs)
