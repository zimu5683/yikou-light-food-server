"""执行 WPS 写入计划：插入/排序/格式化/回读校验，以及失败回滚。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Callable

from app.wps.cli import KdocsCli
from app.wps.common import (
    CELL_MARK,
    FILL_GOLD,
    FILL_LUXURY,
    FIRST_DATA_ROW,
    FONT_SIZE_TO_TWIP,
    HEADER_ROW,
    _ALIGN_H,
    _ALIGN_V,
    _argb_to_int,
    _as_int,
    column_name,
    format_sort_key,
    person_key,
    probe_sort_area,
)
from app.wps.errors import WpsCloudError
from app.wps.ledger import SyncLedger
from app.wps.models import Change, SheetPlan
from app.wps.planner import formula_cells_for_new_rows

def _rollback_inserts(cli: KdocsCli, plan: SheetPlan, worksheet_id: int,
                      inserted: Sequence[tuple[int, int]],
                      emit: Callable[[str], Any]) -> None:
    """插入成功但后续写入失败时，把插出来的行删掉，避免云端留下烂尾空行。

    从位置最靠后的块开始删（前面的删除不会影响后面的行号）。
    """
    if not inserted:
        return
    for first_row, count in sorted(inserted, reverse=True):
        try:
            cli.delete_rows(plan.file_id, worksheet_id, row=first_row, count=count)
        except WpsCloudError as exc:
            emit(f"[云同步] {plan.sheet}：回滚插入失败（第 {first_row} 行起 "
                 f"{count} 行可能残留空行，请人工检查）：{exc}")
            return
    emit(f"[云同步] {plan.sheet}：已回滚 {len(inserted)} 处插入，云端恢复原状")

def read_person_rows(cli: KdocsCli, plan: SheetPlan, worksheet_id: int, *,
                     last_row: int) -> dict[tuple[str, str], int]:
    """回读「姓名+电话」列，返回 ``{(姓名, 电话): 行号}``（1-based）。

    整表排序后人行号全变了，必须靠这个重新定位到每个人的新行号。
    只读 3 列，一次调用。
    """
    name_col = plan.columns.get("name") or 1
    phone_col = plan.columns.get("phone") or max(name_col + 1, 3)
    lo, hi = min(name_col, phone_col), max(name_col, phone_col)
    row_from = FIRST_DATA_ROW if last_row >= FIRST_DATA_ROW else FIRST_DATA_ROW
    grid = cli.read_grid(plan.file_id, worksheet_id,
                         row_from - 1, max(last_row, row_from) - 1, lo - 1, hi - 1)
    index: dict[tuple[str, str], int] = {}
    for (row, col), text in grid.items():
        if col != name_col - 1 or not str(text).strip():
            continue
        phone = grid.get((row, phone_col - 1), "")
        index.setdefault(person_key(text, phone), row + 1)
    return index

def apply_plan(cli: KdocsCli, plans: Iterable[SheetPlan], *,
               ledger: SyncLedger | None = None,
               marker_enabled: bool = True,
               log: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """按计划写入云端；写入后回读校验；成功才更新账本。

    单张表的执行顺序（2026-09-15 起）：
      1. 新客户统一插到第 3 行与第 4 行之间；
      2. 写新行的姓名/地址/电话/类型/餐种（与行号无关，排序后跟着行走）；
      3. 上新行底色（经济餐照抄模板、豪华餐整行金黄）；
      4. 写排序辅助列 → range-sort 按地址顺序重排整表 → 删掉辅助列；
      5. 回读列A~C，按（姓名,电话）重新定位每个人的**排序后行号**；
      6. 写日期格/总餐次/已出餐剩余餐公式/通讯记号；
      7. 回读校验 + 记账本。

    回滚红线：**只有排序之前**的失败才回滚（把插进去的行删掉）；排序一旦成功，
    新行已散落到各地址组里，此时删行会删错人 —— 只告警，让用户重传（重复执行安全）。
    """
    emit = log or (lambda _msg, _level="INFO": None)
    result: dict[str, Any] = {"sheets": [], "written": 0, "failed": 0}
    for plan in plans:
        if not plan.target_col:
            result["sheets"].append({"sheet": plan.sheet, "status": "skipped",
                                     "reason": "未找到目标日期列"})
            continue
        infos = cli.sheets_info(plan.file_id)
        if not infos:
            result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                     "reason": "云端文件不可读"})
            result["failed"] += 1
            continue
        worksheet_id = int(infos[0].get("sheetId") or 1)

        # 1) 插入新客户行：统一一块，插到第 3 行与第 4 行之间。
        #    表里本来没有数据行时不插（写单元格会自动把表扩出来）。
        inserted: list[tuple[int, int]] = []          # (first_row, count)，回滚用
        new_changes = [c for c in plan.changes if c.kind == "new"]
        block = plan.insert_blocks[0] if plan.insert_blocks else None
        if block and not block.append_only:
            try:
                cli.insert_rows(plan.file_id, worksheet_id,
                                row=block.first_row, count=block.count)
                inserted.append((block.first_row, block.count))
                emit(f"[云同步] {plan.sheet}：已在第 {block.first_row} 行前插入 "
                     f"{block.count} 行（新客户）")
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet} 插入新行失败：{exc}")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": f"插入新行失败：{exc}"})
                result["failed"] += 1
                continue

        # 2) 排序前先写"与行号无关"的整行信息：排序后这些值会跟着行走。
        #    必须写在 insert_row（排序前的物理行），不能写 change.row（那是排序后
        #    的目标行号，此刻还属于别人）。
        base_cells: list[dict[str, Any]] = []
        for change in new_changes:
            at = change.insert_row or change.row
            base_cells.append({"row": at, "col": plan.columns["name"],
                               "value": change.name})
            if plan.columns.get("address"):
                base_cells.append({"row": at, "col": plan.columns["address"],
                                   "value": change.address})
            base_cells.append({"row": at, "col": plan.columns["phone"],
                               "value": change.phone})
            if plan.columns.get("type") and change.meal_type:
                base_cells.append({"row": at, "col": plan.columns["type"],
                                   "value": change.meal_type})
            if plan.columns.get("kind") and change.meal_kind:
                base_cells.append({"row": at, "col": plan.columns["kind"],
                                   "value": change.meal_kind})
        if base_cells:
            try:
                cli.write_cells(plan.file_id, worksheet_id, base_cells)
            except WpsCloudError as exc:
                _rollback_inserts(cli, plan, worksheet_id, inserted, emit)
                emit(f"[云同步] {plan.sheet} 新客户行写入失败：{exc}")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": str(exc)})
                result["failed"] += 1
                continue

        # 3) 上底色：新客户行跟着原数据行的字体/对齐；经济餐照抄模板底色（黄带位置
        #    与老行一致）；**豪华餐整行金黄**。所有操作合并后分批，25 个新行只花
        #    1~2 次调用。格式失败不影响数据写入结论。
        if new_changes:
            col_lo = plan.columns.get("name") or 1
            col_hi = plan.columns.get("remark") or (col_lo + 12)
            try:
                spec = learn_row_format(cli, plan.file_id, worksheet_id,
                                        col_from=col_lo, col_to=col_hi,
                                        rows=[r for r in plan.format_rows if r],
                                        cached_fills=plan.format_fills)
                if spec:
                    econ = [c.insert_row or c.row for c in new_changes
                            if str(c.meal_kind).strip() != "豪华"]
                    lux = [c.insert_row or c.row for c in new_changes
                           if str(c.meal_kind).strip() == "豪华"]
                    ops = build_format_ops(
                        spec, econ_rows=econ, lux_rows=lux,
                        col_from=col_lo, col_to=col_hi,
                        # A~「餐种」整段刷底色（含日期与类型之间可能夹着的空列）
                        plain_to=(plan.columns.get("kind")
                                  or plan.columns.get("type") or 0),
                        # 金黄带按列身份固定：总餐次 / 已出餐 / 剩余餐
                        band_cols=tuple(c for c in (plan.columns.get("total"),
                                                    plan.columns.get("served"),
                                                    plan.columns.get("left")) if c))
                    cli.write_format_ops(plan.file_id, worksheet_id, ops)
                    emit(f"[云同步] {plan.sheet}：{len(new_changes)} 个新客户已套用"
                         f"表格原有格式（字体/对齐、经济餐照抄模板底色"
                         + ("、豪华餐整行金黄" if lux else "") + "）")
                else:
                    emit(f"[云同步] {plan.sheet}：读不到参考行格式，跳过格式设置")
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet}：格式设置失败（数据已写入）：{exc}")

        # 4) 排序：写排序键（辅助列，在该表所有内容列右侧）→ 云端原地排序 → 删辅助列。
        if plan.sort_key_col and plan.row_keys:
            # 4a) 排序区必须覆盖表里**所有**内容列：辅助列右边若还有内容，说明它在
            #     读取范围（MAX_SCAN_COL）之外，排序会让行与那些列错位。此时拒绝排序并
            #     回滚插入 —— 宁可不排序，也不能把表排坏。
            safe, found_at = probe_sort_area(
                cli, plan, worksheet_id, col_from=plan.sort_key_col + 1,
                row_to=max(plan.last_data_row, FIRST_DATA_ROW))
            if not safe:
                _rollback_inserts(cli, plan, worksheet_id, inserted, emit)
                reason = (f"第 {plan.sort_key_col} 列右侧（{found_at}）还有内容，"
                          "排序区覆盖不到它，已放弃排序以免行与列错位")
                emit(f"[云同步] {plan.sheet}：{reason}；新客户行已回滚，"
                     "请先清理该列内容后重新上传", "ERROR")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": reason})
                result["failed"] += 1
                continue
            key_cells = [{"row": row, "col": plan.sort_key_col,
                          "value": format_sort_key(key)}
                         for row, key in sorted(plan.row_keys.items())]
            try:
                cli.write_cells(plan.file_id, worksheet_id, key_cells)
                cli.sort_range(plan.file_id, worksheet_id, range_ref=plan.sort_range,
                               key=column_name(plan.sort_key_col), order="asc",
                               header=False)
            except WpsCloudError as exc:
                # 排序还没生效（行还在原位）→ 插进去的新行可以整块删掉
                _rollback_inserts(cli, plan, worksheet_id, inserted, emit)
                emit(f"[云同步] {plan.sheet} 按地址排序失败：{exc}")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": f"按地址排序失败：{exc}"})
                result["failed"] += 1
                continue
            emit(f"[云同步] {plan.sheet}：已按地址顺序重排第 {FIRST_DATA_ROW}~"
                 f"{plan.last_data_row} 行")
            # 4b) 排序后复查一次：确实错位了就不要继续往这些行写数据。
            safe, found_at = probe_sort_area(
                cli, plan, worksheet_id, col_from=plan.sort_key_col + 1,
                row_to=max(plan.last_data_row, FIRST_DATA_ROW))
            if not safe:
                emit(f"[云同步] {plan.sheet}：排序后第 {plan.sort_key_col} 列右侧出现内容"
                     f"（{found_at}），说明排序区未覆盖全部列，已停止写入后续数据。"
                     "表已重排，请重新上传（重复执行安全）", "ERROR")
                result["sheets"].append({
                    "sheet": plan.sheet, "status": "failed",
                    "reason": f"排序区未覆盖全部列（右侧有内容：{found_at}）"})
                result["failed"] += 1
                continue
            try:
                cli.delete_columns(plan.file_id, worksheet_id,
                                   column=plan.sort_key_col, rows=plan.last_data_row)
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet}：排序辅助列（第 {plan.sort_key_col} 列）"
                     f"删除失败，表右侧可能残留一排排序键：{exc}", "WARN")
                try:
                    cli.write_cells(plan.file_id, worksheet_id, [
                        {"row": row, "col": plan.sort_key_col, "value": ""}
                        for row in sorted(plan.row_keys)])
                except WpsCloudError:
                    pass

        # 5) 重定位：整表重排后人行号全变了，必须回读（姓名,电话）重新定位。
        #    定位失败就停手（不猜行号），此时只写了新行信息与底色，重传一次即可。
        if plan.sort_enabled:
            try:
                actual_rows = read_person_rows(cli, plan, worksheet_id,
                                               last_row=plan.last_data_row)
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet} 排序后无法重新定位人员行号，已停止写入"
                     f"（请重新上传，重复执行安全）：{exc}", "ERROR")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": f"排序后回读失败：{exc}"})
                result["failed"] += 1
                continue
            missing = [c for c in plan.changes
                       if person_key(c.name, c.phone) not in actual_rows]
            if missing:
                names = "、".join(c.name for c in missing[:5])
                emit(f"[云同步] {plan.sheet} 排序后定位不到这些人：{names}，已停止写入",
                     "ERROR")
                result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                         "reason": f"排序后定位不到：{names}"})
                result["failed"] += 1
                continue
            drift = 0
            for change in plan.changes:
                key = person_key(change.name, change.phone)
                row = actual_rows[key]
                if plan.final_rows.get(key, row) != row:
                    drift += 1
                    if drift <= 3:
                        emit(f"[云同步] {plan.sheet}：{change.name} 实际排在第 {row} 行，"
                             f"与预测不符（按实际行号写入）", "WARN")
                change.row = row
            if drift:
                plan.sort_mismatch = True
                emit(f"[云同步] {plan.sheet}：{drift} 人的实际行号与预测不同"
                     f"（已按实际行号写入，不影响数据正确性）", "WARN")

        # 6) 其余写入：日期格 / 总餐次 / 公式 / 通讯记号
        cells: list[dict[str, Any]] = []
        pending: list[Change] = []
        for change in plan.changes:
            if not change.needs_write:
                continue          # 已经一致：一个字都不写
            pending.append(change)
            if change.total_after != change.total_before and plan.columns.get("total"):
                cells.append({"row": change.row, "col": plan.columns["total"],
                              "value": change.total_after})
            if change.kind == "existing":
                if change.fill_type and plan.columns.get("type"):
                    cells.append({"row": change.row, "col": plan.columns["type"],
                                  "value": change.meal_type})
                if change.fill_kind and plan.columns.get("kind"):
                    cells.append({"row": change.row, "col": plan.columns["kind"],
                                  "value": change.meal_kind})
            if not change.target_ok:
                cells.append({"row": change.row, "col": change.target_col, "value": CELL_MARK})
        formula_cells = formula_cells_for_new_rows(
            plan, [c.row for c in pending if c.kind == "new"])
        # 半成品行自愈：老客户缺公式的也补上（同一套公式）
        formula_cells.extend(formula_cells_for_new_rows(
            plan, [c.row for c in pending if c.kind == "existing" and c.fill_formula]))
        cells.extend(formula_cells)
        if marker_enabled and plan.marker_col:
            cells.append({"row": HEADER_ROW, "col": plan.marker_col,
                          "value": str(plan.weekday_number)})
        if not cells:
            # 没有任何待写内容（本次也没新客户，否则一定有格子要写）
            result["sheets"].append({"sheet": plan.sheet, "status": "noop"})
            continue
        try:
            cli.write_cells(plan.file_id, worksheet_id, cells)
        except WpsCloudError as exc:
            note = "（表已按地址重排，请重新上传；重复执行是安全的）" if plan.sort_enabled else ""
            emit(f"[云同步] {plan.sheet} 写入失败：{exc}{note}", "ERROR")
            result["sheets"].append({"sheet": plan.sheet, "status": "failed",
                                     "reason": str(exc)})
            result["failed"] += 1
            continue

        # 回读校验：逐格核对，而不是只抽查首尾行。
        # （曾出现过"只抽查末尾行、恰好该行原本就是 1"导致的假阳性。）
        rows_written = sorted({int(c["row"]) for c in cells if int(c["row"]) >= FIRST_DATA_ROW})
        if rows_written:
            lo, hi = rows_written[0] - 1, rows_written[-1] - 1
            verify_cols = {plan.target_col}
            if plan.columns.get("total"):
                verify_cols.add(plan.columns["total"])
            if plan.columns.get("name"):
                verify_cols.add(plan.columns["name"])
            if formula_cells:
                for formula_col in (plan.columns.get("served"), plan.columns.get("left")):
                    if formula_col:
                        verify_cols.add(formula_col)
            c_lo, c_hi = min(verify_cols) - 1, max(verify_cols) - 1
            try:
                back = cli.read_grid(plan.file_id, worksheet_id, lo, hi, c_lo, c_hi)
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet} 写入已完成，但回读校验失败（网络/接口问题）：{exc}",
                     "WARN")
                result["sheets"].append({"sheet": plan.sheet, "status": "verify_unreadable",
                                         "reason": f"写入完成但回读失败：{exc}"})
                if ledger is not None and pending:
                    # 数据已写入，账本仍要记，避免下次重复写入
                    ledger.record(plan.target_date.isoformat(), plan.file_id,
                                  {f"{c.name}\u0000{c.phone}": c.total_after for c in pending})
                result["written"] += 1
                continue
            problems: list[str] = []
            formula_back = {}
            if formula_cells:
                formula_back = cli.read_formulas(
                    plan.file_id, worksheet_id, lo, hi, c_lo, c_hi)
            name_col = plan.columns.get("name") or 0
            for change in pending:
                if name_col:
                    got_name = str(back.get((change.row - 1, name_col - 1), "")).strip()
                    if got_name != str(change.name).strip():
                        problems.append(f"第 {change.row} 行应为「{change.name}」，"
                                        f"实际「{got_name}」")
                got_mark = str(back.get((change.row - 1, plan.target_col - 1), "")).strip()
                if got_mark != CELL_MARK:
                    problems.append(f"{change.name}: 目标列应为 {CELL_MARK}，实际 {got_mark!r}")
                if plan.columns.get("total"):
                    got_total = _as_int(back.get((change.row - 1, plan.columns["total"] - 1)))
                    if got_total != change.total_after:
                        problems.append(
                            f"{change.name}: 总餐次应为 {change.total_after}，实际 {got_total}")
                if change.kind == "new" and plan.columns.get("served") and plan.columns.get("left"):
                    expected_served = f"=SUM({column_name(min(plan.date_cols))}{change.row}:{column_name(max(plan.date_cols))}{change.row})"
                    expected_left = f"={column_name(plan.columns['total'])}{change.row}-{column_name(plan.columns['served'])}{change.row}"
                    if formula_back.get((change.row - 1, plan.columns["served"] - 1)) != expected_served:
                        problems.append(f"{change.name}: 已出餐公式未写入或不正确")
                    if formula_back.get((change.row - 1, plan.columns["left"] - 1)) != expected_left:
                        problems.append(f"{change.name}: 剩余餐公式未写入或不正确")
            if problems:
                for item in problems[:10]:
                    emit(f"[云同步] {plan.sheet} 校验不一致：{item}")
                result["sheets"].append({"sheet": plan.sheet, "status": "verify_failed",
                                         "problems": problems[:20]})
                result["failed"] += 1
                continue
        if ledger is not None and pending:
            # 账本仅作留痕/审计：记录本次写入后每人的总餐次与目标日期格状态
            entries = {f"{c.name}\u0000{c.phone}": c.total_after for c in pending}
            ledger.record(plan.target_date.isoformat(), plan.file_id, entries)
        result["sheets"].append({"sheet": plan.sheet, "status": "ok",
                                 "cells": len(cells), "people": len(pending),
                                 "sorted": bool(plan.sort_enabled),
                                 "sort_mismatch": bool(plan.sort_mismatch)})
        result["written"] += 1
    if ledger is not None:
        try:
            ledger.save()
        except OSError as exc:
            emit(f"[云同步] 账本保存失败：{exc}")
    return result

def learn_row_format(cli: "KdocsCli", file_id: str, worksheet_id: int, *,
                     col_from: int, col_to: int,
                     rows: Sequence[int],
                     cached_fills: Mapping[int, str] | None = None) -> dict[str, Any]:
    """从已有数据行里"学"一行模板：字体 + 对齐 + **每列底色**。

    按列照抄底色（而不是"读回目标行原底色"）的原因：接口不返回空单元格，
    目标行的空列读不到颜色，照抄会让那些列变成无填充（看起来像被涂黑）。
    模板行（如东湖中餐第 99 行）的颜色分布是「A~J 白 / K~M 黄 / N 白」，
    照抄后新行的黄带位置与老行完全一致。

    ``rows`` 里第一个能读到内容的行会被采用；全都读不到返回空 dict，
    调用方应跳过格式写入（宁可不设，也不要写错）。
    """
    for row in rows:
        grid = cli.read_grid(file_id, worksheet_id, row - 1, row - 1, col_from - 1, col_to - 1)
        cells = {col + 1: text for (_r, col), text in grid.items()}
        if not cells:
            continue
        # 逐列底色：优先用 build_plan 预先读好的缓存（省接口调用）；
        # 没有缓存时才逐列读 —— 宽表逐列读会消耗大量调用次数。
        fills: dict[int, str] = dict(cached_fills or {})
        if not fills:
            for col in range(col_from, col_to + 1):
                cell = cli.read_cell_format(file_id, worksheet_id, row, col)
                if cell and cell.get("cell_background_color"):
                    fills[col] = str(cell["cell_background_color"])
        spec_cell = cli.read_cell_format(file_id, worksheet_id, row, col_from)
        if not spec_cell:
            continue
        fonts = spec_cell.get("fonts") or {}
        align = spec_cell.get("alignment") or {}
        return {
            "font_name": fonts.get("font_east_asia") or fonts.get("name") or "",
            "font_size": int(fonts.get("size") or 10),
            "font_color": (_argb_to_int(fonts["color"]) if fonts.get("color") else None),
            "alcH": _ALIGN_H.get(str(align.get("horizontal") or ""), 2),
            "alcV": _ALIGN_V.get(str(align.get("vertical") or ""), 1),
            "fill_default": _argb_to_int(fills.get(col_from, "#FFFFFFFF")),
            "fills": fills,
            "sample_row": row,
        }
    return {}

def build_format_ops(spec: Mapping[str, Any], *, econ_rows: Sequence[int],
                     lux_rows: Sequence[int],
                     col_from: int, col_to: int,
                     plain_to: int = 0,
                     band_cols: Sequence[int] = ()) -> list[dict[str, Any]]:
    """把"新客户行 × 模板格式"压成尽量少的格式操作。

    - 经济行：**第 A 列 ~ 「餐种」列整段刷成模板底色**（用户 2026-09-15 要求）。
      原因：日期列与「类型」之间有时夹着一列**空列**，接口不返回空单元格、读不到
      它的底色，逐列照抄就会漏掉它 —— 那一格于是保留插入时从上一行继承的颜色，
      看起来就是"有一格没涂到"。整段刷过去就不会再有漏网的列。
    - 金黄色的三列（总餐次/已出餐/剩余餐）按**列的身份**固定涂金，不依赖模板行
      那几格有没有内容（实测有客户行的总餐次是空的，模板选到它就学不到金色）。
    - 豪华行：**整行金黄**（名字到备注整段，用户 2026-09-12 确认）；
    - 相邻同色列再合并成区间。

    这样 25 个新行只要 ~1 次接口调用（逐行设格式要 25 次，曾把当日额度打满）。
    """
    base_xf: dict[str, Any] = {"alcH": spec.get("alcH", 2), "alcV": spec.get("alcV", 1)}
    if spec.get("font_name"):
        font: dict[str, Any] = {
            "name": spec["font_name"],
            "dyHeight": int(spec.get("font_size", 10)) * FONT_SIZE_TO_TWIP,
        }
        if spec.get("font_color") is not None:
            font["color"] = {"type": 2, "value": int(spec["font_color"])}
        base_xf["font"] = font

    def _xf(color: int) -> dict[str, Any]:
        xf = dict(base_xf)
        xf["fill"] = {"type": 1, "back": {"type": 2, "value": color},
                      "fore": {"type": 255, "value": 0, "tint": 0}}
        return xf

    def _runs(values: Sequence[int]) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []
        for value in sorted(set(values)):
            if runs and value == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], value)
            else:
                runs.append((value, value))
        return runs

    fills: Mapping[int, str] = spec.get("fills") or {}
    # 注意：fills 里是 "#AARRGGBB" 字符串，而 fill_default 已经是整数 —— 别再转一次，
    # 否则 str(4294967295) 会被当十六进制解析成 0x94967295（离线仿真抓到的真实教训）。
    if fills.get(col_from):
        base_color = _argb_to_int(str(fills[col_from]))
    else:
        base_color = int(spec.get("fill_default") or 0xFFFFFFFF) & 0xFFFFFFFF
    # 「餐种」列及其左边整段用模板底色；它右边只有金黄三列是特殊的。
    plain_end = plain_to if col_from <= plain_to <= col_to else col_from - 1
    band = {int(c) for c in band_cols if col_from <= int(c) <= col_to}

    def color_of(column: int) -> int:
        if column <= plain_end:
            return base_color
        if column in band:
            return FILL_GOLD
        learned = fills.get(column)
        return _argb_to_int(learned) if learned else base_color

    ops: list[dict[str, Any]] = []
    for r0, r1 in _runs(econ_rows):
        col = col_from
        while col <= col_to:
            color = color_of(col)
            col_end = col
            while col_end + 1 <= col_to and color_of(col_end + 1) == color:
                col_end += 1
            ops.append({"opType": "format",
                        "rowFrom": r0 - 1, "rowTo": r1 - 1,
                        "colFrom": col - 1, "colTo": col_end - 1,
                        "xf": _xf(color)})
            col = col_end + 1
    for r0, r1 in _runs(lux_rows):
        ops.append({"opType": "format",
                    "rowFrom": r0 - 1, "rowTo": r1 - 1,
                    "colFrom": col_from - 1, "colTo": col_to - 1,
                    "xf": _xf(FILL_LUXURY)})
    return ops
