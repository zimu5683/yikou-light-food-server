"""W1：计划快照（``SheetPlan.ledger_digest``）从建计划到执行完整流经生产 Bridge。

本文件**不 stub** ``build_plan`` / ``apply_plan``：这两个核心函数是真函数，
整条链路真的跑一遍 ——

    真实 openpyxl 本地排单表 → read_local_orders → build_plan（读合成云表）
      → Bridge 预览指纹/令牌 → apply_plan（锁内 stale 校验 + 真实写入） → 账本落盘

只有三处是替身，且都不是被验证的对象：

* 云表：内存合成网格（``SyntheticCloud``），绝不联网、绝不碰真实文档；
* 账本路径：``bridge_module.SyncLedger`` 指向 ``tmp_path``，绝不读写真实用户的
  ``wps_sync_state.json``；
* 目标表：直接写进 ``AppConfig.wps_tables`` / ``wps_production_tables``，走真实的
  ``effective_tables`` 校验。

为什么必须这样测：R6 报告 4.1（W1）证明旧接线在“建计划”和“执行”处各新建了一个
``SyncLedger()``，于是 ``apply_plan`` 的 stale 比较退化成“同一时刻的两份相同快照”。
旧测试把 ``build_plan``/``apply_plan`` 一起 stub 掉，因此**测不到**这个接线错误。
本文件另有反证用例：把快照传递拆掉（还原旧接线）后，同一场景会真的写云 ——
证明正向用例盯住的是真实保护，而不是空转。
"""
from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge
from app.order.templates import write_order_template
from app.wps.journal import SyncJournal, journal_path_for, new_operation_id
from app.wps.models import LEDGER_SNAPSHOT_ABSENT
from app.wps.sync import SyncLedger, target_date_for

# 合成云表列（0-based）：与真实排单表一致的结构性表头。
_NAME, _ADDR, _PHONE, _DATE, _TYPE, _KIND, _TOTAL = 0, 1, 2, 3, 4, 5, 6
_SERVED, _LEFT, _REMARK = 7, 8, 9
_SHEET = "东湖中餐"
_FILE_ID = "F-SYNTH-1"
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# ----------------------------------------------------------------------
# 合成云表 + 真实本地排单表
# ----------------------------------------------------------------------
class SyntheticCloud:
    """``KdocsCli`` 的内存替身：只记录调用，不联网、不写任何真实文档。"""

    path = "/fake/kdocs-cli"

    def __init__(self, grid: dict[tuple[int, int], str]) -> None:
        self.grid = dict(grid)
        self.writes: list[list[dict]] = []
        self.inserts: list[tuple[int, int]] = []
        self.deletes: list[tuple[int, int]] = []
        self.formats: list[list[dict]] = []
        self.sorts: list[dict] = []

    # -- 只读 --------------------------------------------------------
    def authenticated(self) -> bool:
        return True

    def sheets_info(self, file_id: str):
        return [{"sheetId": 1, "sheetName": "Sheet1", "rowTo": 200, "colTo": 40}]

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format: bool = False):
        hits = {key: value for key, value in self.grid.items()
                if row_from <= key[0] <= row_to and col_from <= key[1] <= col_to}
        if with_format:
            return {key: {"text": str(value), "fill": ""} for key, value in hits.items()}
        return hits

    def read_formulas(self, file_id, worksheet_id, row_from, row_to, col_from, col_to):
        return {key: str(value) for key, value in self.grid.items()
                if row_from <= key[0] <= row_to and col_from <= key[1] <= col_to
                and str(value).startswith("=")}

    # -- 写入（只改内存） --------------------------------------------
    def write_cells(self, file_id, worksheet_id, cells):
        self.writes.append([dict(cell) for cell in cells])
        for cell in cells:
            self.grid[(int(cell["row"]) - 1, int(cell["col"]) - 1)] = str(cell["value"])

    def insert_rows(self, file_id, worksheet_id, *, row, count):
        self.inserts.append((row, count))
        shifted = {(r + count if r >= row - 1 else r, c): v
                   for (r, c), v in self.grid.items()}
        self.grid = shifted

    def delete_rows(self, file_id, worksheet_id, *, row, count):
        self.deletes.append((row, count))
        lo, hi = row - 1, row - 1 + count - 1
        shifted = {}
        for (r, c), v in self.grid.items():
            if r < lo:
                shifted[(r, c)] = v
            elif r > hi:
                shifted[(r - count, c)] = v
        self.grid = shifted

    def write_format_ops(self, file_id, worksheet_id, ops):
        self.formats.append([dict(op) for op in ops])

    def sort_range(self, file_id, worksheet_id, **kwargs):
        self.sorts.append(dict(kwargs))

    def delete_columns(self, file_id, worksheet_id, **kwargs):
        return None

    def read_cell_format(self, file_id, worksheet_id, row, col):
        return None


def _cloud_rows(grid: dict[tuple[int, int], str]) -> list[tuple[int, str, str]]:
    """按行号列出云表里的“有姓名”行：``(1-based 行号, 姓名, 总餐次)``。"""
    rows: list[tuple[int, str, str]] = []
    by_row: dict[int, dict[int, str]] = {}
    for (row, col), value in grid.items():
        by_row.setdefault(row + 1, {})[col] = str(value)
    for row in sorted(by_row):
        if row < 3:
            continue
        cells = by_row[row]
        name = cells.get(_NAME, "")
        if name:
            rows.append((row, name, cells.get(_TOTAL, "")))
    return rows


def _make_grid(target: _dt.date, rows: list[dict[int, str]]) -> dict[tuple[int, int], str]:
    """第 2 行表头、第 3 行起数据（与真实排单表一致）。"""
    header = {
        _NAME: "名字", _ADDR: "地址", _PHONE: "电话",
        _DATE: f"{target.month}.{target.day} {_WEEKDAY_NAMES[target.weekday()]}",
        _TYPE: "类型", _KIND: "餐种", _TOTAL: "总餐次",
        _SERVED: "已出餐", _LEFT: "剩余餐", _REMARK: "备注",
    }
    grid: dict[tuple[int, int], str] = {(1, col): text for col, text in header.items()}
    for offset, row in enumerate(rows):
        for col, text in row.items():
            if str(text) != "":
                grid[(2 + offset, col)] = str(text)
    return grid


def _write_local(excel, target: _dt.date, *, name: str, phone: str, meals: int,
                 address: str = "西溪北苑", order_no: str = "A-1") -> None:
    """用真实模板生成排单表，再填一行真实数据（读回走真实 read_local_orders）。"""
    from openpyxl import load_workbook
    write_order_template(excel)
    wb = load_workbook(excel)
    try:
        ws = wb[_SHEET]
        ws.cell(row=3, column=1, value=order_no)
        ws.cell(row=3, column=2, value=name)
        ws.cell(row=3, column=3, value=address)
        ws.cell(row=3, column=4, value=phone)
        # 周一到周日 7 列（第 5~11 列）：勾上“目标日期是周几”，通过批次日期核对。
        ws.cell(row=3, column=5 + target.weekday(), value="1")
        ws.cell(row=3, column=12, value="中餐")
        ws.cell(row=3, column=13, value="经济")
        ws.cell(row=3, column=14, value=meals)
        wb.save(excel)
    finally:
        wb.close()


@pytest.fixture
def wps_e2e(tmp_path, monkeypatch):
    """生产接线的离线复刻：真实 build_plan/apply_plan + 临时账本 + 合成云表。"""
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    target = target_date_for(start_hour=bridge._config.wps_target_hour_start,
                             end_hour=bridge._config.wps_target_hour_end)

    excel = tmp_path / "排单.xlsx"
    _write_local(excel, target, name="合成客户", phone="13800000000", meals=2)
    cloud = SyntheticCloud(_make_grid(target, [
        {_NAME: "合成客户", _ADDR: "西溪北苑", _PHONE: "13800000000",
         _TYPE: "中餐", _KIND: "经济", _TOTAL: "5",
         _SERVED: "=SUM(D3:D3)", _LEFT: "=G3-H3"},
    ]))
    ledger_path = tmp_path / "wps_sync_state.json"

    bridge._config.excel_path = excel
    bridge._config.wps_enabled = True
    bridge._config.wps_test_mode = False
    bridge._config.wps_marker_enabled = False
    bridge._config.wps_sort_enabled = False
    bridge._config.wps_tables = {_SHEET: {"file_id": _FILE_ID}}
    bridge._config.wps_production_tables = {_SHEET: {"file_id": _FILE_ID}}
    monkeypatch.setattr(bridge_module, "SyncLedger",
                        lambda *args, **kwargs: SyncLedger(ledger_path))
    monkeypatch.setattr(bridge, "_wps_cli", lambda: cloud)
    return SimpleNamespace(bridge=bridge, cloud=cloud, target=target,
                           excel=excel, ledger_path=ledger_path, tmp_path=tmp_path)


def _preview(env) -> dict:
    got = env.bridge.wps_preview()
    assert got["ok"] is True, got
    return got


def _capture_plans(env, monkeypatch) -> list:
    """包一层 ``_wps_read_plans`` 记录真实计划对象（不改行为）。"""
    captured: list = []
    real = env.bridge._wps_read_plans

    def wrapper(bundle):
        plans, error = real(bundle)
        if error is None and plans is not None:
            captured.append(list(plans))
        return plans, error

    monkeypatch.setattr(env.bridge, "_wps_read_plans", wrapper)
    return captured


# ----------------------------------------------------------------------
# 正向：完整跑通 + 计划真的带快照 + 幂等
# ----------------------------------------------------------------------
def test_w1_full_preview_upload_uses_real_build_and_apply(wps_e2e, monkeypatch):
    env = wps_e2e
    plans_seen = _capture_plans(env, monkeypatch)

    preview = _preview(env)
    uploaded = env.bridge.wps_upload(preview["preview_id"])

    # apply_plan 是真函数：状态必须来自它的真实返回。
    assert uploaded["ok"] is True, uploaded
    assert uploaded["status"] == "success"
    assert uploaded["executor_status"] in ("ok", "")
    assert uploaded["written"] == 1
    assert uploaded["failed"] == 0
    # 云端真的被改：5 + 2 = 7，并标了当天 1。
    row3 = {col: env.cloud.grid.get((2, col)) for col in range(10)}
    assert row3[_TOTAL] == "7"
    assert row3[_DATE] == "1"
    # 账本落盘：幂等锚点必须存在，否则下一次会重复加餐。
    ledger = SyncLedger(env.ledger_path)
    assert ledger.synced_local(env.target.isoformat(), _FILE_ID,
                               "合成客户", "13800000000") == 2
    assert ledger.synced_total(env.target.isoformat(), _FILE_ID,
                               "合成客户", "13800000000") == 7

    # 计划对象真的流经 Bridge：ledger_digest 必须是计划时刻的账本快照。
    assert plans_seen, "Bridge 必须用真实 build_plan 生成计划"
    digests = [getattr(plan, "ledger_digest", None) for plan in plans_seen[0]]
    assert digests and all(digests), f"计划必须携带账本快照：{digests}"
    assert not any(digest == "" for digest in digests)
    # 首次运行时账本还不存在，快照是“明确不存在”标记（不是空值）。
    assert any(digest == LEDGER_SNAPSHOT_ABSENT
               or len(str(digest)) == 64 for digest in digests), digests

    # 第二次预览 + 上传：同一批重复上传必须零增量（账本幂等）。
    writes_before = len(env.cloud.writes)
    second_preview = _preview(env)
    second = env.bridge.wps_upload(second_preview["preview_id"])
    assert second["ok"] is True, second
    assert second["status"] in ("success", "noop")
    assert {col: env.cloud.grid.get((2, col)) for col in range(10)}[_TOTAL] == "7"
    assert len(env.cloud.writes) == writes_before, "重复上传不得再写云端"


def test_w1_plan_digest_matches_ledger_at_plan_time(wps_e2e, monkeypatch):
    """计划快照必须来自“建计划时刻”的账本，并与它同源。"""
    env = wps_e2e
    # 先造一份已存在的账本，让摘要是一个真实哈希。
    seed = SyncLedger(env.ledger_path)
    seed.record(env.target.isoformat(), _FILE_ID, {"别人\u000013900000000": 1})
    seed.save()
    seen = _capture_plans(env, monkeypatch)

    preview = _preview(env)
    assert preview["ok"] is True
    assert seen and seen[0]
    digest = seen[0][0].ledger_digest
    assert isinstance(digest, str) and len(digest) == 64, digest
    # 与当时磁盘账本的加载摘要一致。
    assert SyncLedger(env.ledger_path)._loaded_digest == digest


# ----------------------------------------------------------------------
# W1 核心：窗口内另一个进程提交账本 → 零写入 + 重新预览
# ----------------------------------------------------------------------
def test_w1_concurrent_ledger_commit_before_apply_is_refused_zero_write(
        wps_e2e, monkeypatch):
    """计划构建完成、apply_plan 取锁之前，另一个进程提交了账本 → 必须零写入。"""
    env = wps_e2e
    # 用“新增客户”构造真实重复写入场景：append 型计划最容易多写一行。
    _write_local(env.excel, env.target, name="新人A", phone="13800000001", meals=1)
    env.cloud.grid = _make_grid(env.target, [
        {_NAME: "老客户X", _ADDR: "大西", _PHONE: "13800000009",
         _TYPE: "中餐", _KIND: "经济", _TOTAL: "3"},
    ])
    seed = SyncLedger(env.ledger_path)
    seed.record(env.target.isoformat(), _FILE_ID, {"老客户X\u000013800000009": 3})
    seed.save()

    preview = _preview(env)
    real_read_plans = env.bridge._wps_read_plans
    fired = {"n": 0}

    def hooked(bundle):
        plans, error = real_read_plans(bundle)
        if error is None and plans is not None and fired["n"] == 0:
            fired["n"] += 1
            # 另一个进程：把“新人A 已同步”提交到同一份账本（真实文件）。
            other = SyncLedger(env.ledger_path)
            other.record(env.target.isoformat(), _FILE_ID,
                         {"新人A\u00001380000001": {"local": 1, "slots": [1], "total": 1}})
            other.save()
        return plans, error

    monkeypatch.setattr(env.bridge, "_wps_read_plans", hooked)
    grid_before = dict(env.cloud.grid)

    got = env.bridge.wps_upload(preview["preview_id"])

    assert fired["n"] == 1, "测试必须真的在计划与执行之间插入并发提交"
    assert got["ok"] is False, got
    assert got["status"] in ("failed", "uncertain")
    assert got["written"] == 0 and got["failed"] >= 1
    assert got["next_action"] == "repreview"
    assert "账本" in got["reason"] and "重新预览" in got["reason"]
    # 零外部写入：cli 一次都没被写、云端网格逐字节不变。
    assert env.cloud.writes == [], "stale 计划必须零云端写入"
    assert env.cloud.inserts == []
    assert env.cloud.grid == grid_before
    # 执行摘要也不能声称写成功（W6 口径）。
    assert got["execution_summary"]["rows"]["verified"] in (0, None)
    assert got["planned_summary"]["kind"] == "plan"


def test_w1_negative_control_old_wiring_would_write(wps_e2e, monkeypatch):
    """反证（变异）：还原 R6 W1 的旧接线后，同一场景会真的写云。

    这条用例是“正向用例没有空转”的证据：它故意把计划快照与共享账本拆掉
    （计划不带 ``ledger_digest``，执行时再新建 ``SyncLedger()``），于是
    stale 比较退化成同一时刻的两份相同快照 —— 云端被照写。
    如果哪天底层把这条路彻底堵死，这条用例会失败，说明该更新它而不是放宽它。
    """
    env = wps_e2e
    _write_local(env.excel, env.target, name="新人B", phone="13800000002", meals=1)
    env.cloud.grid = _make_grid(env.target, [
        {_NAME: "老客户Y", _ADDR: "大西", _PHONE: "13800000019",
         _TYPE: "中餐", _KIND: "经济", _TOTAL: "3"},
    ])
    seed = SyncLedger(env.ledger_path)
    seed.record(env.target.isoformat(), _FILE_ID, {"老客户Y\u000013800000019": 3})
    seed.save()

    preview = _preview(env)
    real_read_plans = env.bridge._wps_read_plans
    fired = {"n": 0}

    def hooked(bundle):
        plans, error = real_read_plans(bundle)
        if error is None and plans is not None and fired["n"] == 0:
            fired["n"] += 1
            for plan in plans:
                plan.ledger_digest = None      # 旧接线：计划不带快照
            bundle.pop("ledger", None)         # 旧接线：执行时另建账本
            other = SyncLedger(env.ledger_path)
            other.record(env.target.isoformat(), _FILE_ID,
                         {"新人B\u00001380000002": {"local": 1, "slots": [1], "total": 1}})
            other.save()
        return plans, error

    monkeypatch.setattr(env.bridge, "_wps_read_plans", hooked)
    got = env.bridge.wps_upload(preview["preview_id"])

    assert fired["n"] == 1
    assert env.cloud.writes, f"旧接线应当照写（这正是 W1 的漏洞）：{got}"
    assert got["ok"] is True, got
    assert got["status"] == "success"


# ----------------------------------------------------------------------
# W6（真实执行路径）：计划数与执行结果分开
# ----------------------------------------------------------------------
def test_w6_real_apply_reports_sheet_and_row_counts_separately(wps_e2e):
    env = wps_e2e
    preview = _preview(env)

    got = env.bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is True, got
    planned = got["planned_summary"]
    execution = got["execution_summary"]
    # 计划口径：明确 kind=plan，且行数在 rows 子对象里。
    assert planned["kind"] == "plan"
    assert planned["rows"]["to_update"] == 1
    assert planned["rows"]["to_append"] == 0
    # 执行口径：表数与行数是两个不同的字段，不会互相冒充。
    assert execution["kind"] == "execution"
    assert execution["counts_source"] == "apply_plan"
    assert execution["sheets"]["verified"] == 1
    assert execution["sheets"]["failed"] == 0
    assert execution["rows"]["verified"] == 1, execution
    assert execution["rows"]["failed"] == 0
    assert execution["rows"]["uncertain"] == 0
    assert execution["rows"]["skipped"] == 0
    assert execution["rows"]["planned"] == 1
    assert execution["rows_unknown"] is False
    # summary/stats 也是执行口径（旧字段名保留，含义不再含糊）。
    assert got["summary"] == execution
    assert got["stats"] == execution

    logs = [event["payload"]["msg"] for event in env.bridge.drain_events(0)["events"]
            if event["event"] == "log"]
    assert any(line.startswith("[云同步] 计划：更新 1 行") for line in logs), logs
    assert any("实际：实际已验证 1 行" in line and "已验证表 1 张" in line
               for line in logs), logs
    # 旧文案把计划数当成功数，必须消失。
    assert not any("完成：更新" in line for line in logs), logs


def test_w6_uncertain_result_never_reports_plan_rows_as_written(wps_e2e,
                                                                monkeypatch):
    """执行器报 uncertain 时，行数必须未知/0，绝不能回落到计划数。"""
    env = wps_e2e
    preview = _preview(env)

    real_apply = bridge_module.apply_plan

    def uncertain_apply(cli, plans, **kwargs):
        # 真实 apply_plan 不写任何东西，但返回“结果不确定”，且没有 people 计数。
        return {"status": "uncertain", "uncertain": True,
                "next_action": "manual_reconcile",
                "reason": "写入已完成，但回读校验失败",
                "written": 0, "failed": 1,
                "sheets": [{"sheet": _SHEET, "status": "uncertain", "uncertain": True,
                            "reason": "写入已完成，但回读校验失败", "problems": []}]}

    monkeypatch.setattr(bridge_module, "apply_plan", uncertain_apply)
    got = env.bridge.wps_upload(preview["preview_id"])
    monkeypatch.setattr(bridge_module, "apply_plan", real_apply)

    assert got["ok"] is False
    assert got["status"] == "uncertain"
    assert got["next_action"] == "只读核对，不重新上传"
    assert got["planned_summary"]["rows"]["to_update"] == 1
    execution = got["execution_summary"]
    assert execution["sheets"]["uncertain"] == 1
    assert execution["rows"]["verified"] in (0, None)
    assert execution["rows"]["uncertain"] is None, "不确定行数不能声称是 0 或计划数"
    assert execution["rows_unknown"] is True
    # 旧字段 summary 也不再是计划数（不含 to_update）。
    assert "to_update" not in got["summary"]
    assert env.cloud.writes == [], "uncertain 的替身没有写，测试不应依赖真实写入"
    assert got["text"]  # 结果文本仍可展示


# ----------------------------------------------------------------------
# W3（端到端）：带闸门退场后，同一目标日期/表不能被自动重传
# ----------------------------------------------------------------------
def test_w3_w1_retired_guard_blocks_same_target_upload(wps_e2e):
    """退场只退出全局 pending；同一日期+云表的闸门仍在 → 上传被拒、零写入。

    这正是 R6 W3 要求的“未知写入结果不得通过退场/归档变成可自动重传”。
    """
    env = wps_e2e
    journal = SyncJournal(journal_path_for(env.ledger_path))
    operation_id = new_operation_id()
    journal.create_operation(operation_id, {
        f"0:{_SHEET}:{_FILE_ID}": {
            "sheet": _SHEET, "file_id": _FILE_ID,
            "target_date": env.target.isoformat(),
            "status": "writing", "next_action": "recover_journal",
            "reason": "写入过程中断电，无法判断是否已写", "problems": [],
        },
    }, target_date=env.target.isoformat())
    journal.save()

    retired = env.bridge.wps_recovery_resolve({
        "operation_id": operation_id, "decision": "retire_guarded",
        "confirm": "retire_guarded",
        "note": "人工核对云端后仍无法判定写入结果，保留防重复闸门",
        "confirm_structure_checked": True,
    })
    assert retired["ok"] is True, retired
    assert retired["scope"]["blocking"] == "retired_guarded"
    assert SyncJournal(journal_path_for(env.ledger_path)).has_guard(
        env.target.isoformat(), _FILE_ID) is True

    preview = _preview(env)
    grid_before = dict(env.cloud.grid)
    got = env.bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False, got
    assert got["status"] == "uncertain"
    assert got["next_action"] == "只读核对，不重新上传"
    assert got["written"] == 0
    assert env.cloud.writes == [], "带闸门的目标必须零云端写入"
    assert env.cloud.grid == grid_before
    assert got["execution_summary"]["rows"]["verified"] != got["planned_summary"]["rows"]["to_update"]
