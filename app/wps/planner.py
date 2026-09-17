"""构建云端写入计划与人类可读预览（只读，不写云端）。"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app.wps.cli import KdocsCli
from app.wps.common import (
    CELL_MARK,
    FIRST_DATA_ROW,
    HEADER_ADDRESS,
    HEADER_KIND,
    HEADER_LEFT,
    HEADER_NAME,
    HEADER_PHONE,
    HEADER_REMARK,
    HEADER_ROW,
    HEADER_SERVED,
    HEADER_TOTAL,
    HEADER_TYPE,
    MAX_SORT_COL,
    SORT_FLAG_BLANK,
    SORT_FLAG_EXISTING,
    SORT_FLAG_NEW,
    WPS_PLAN_WORKERS,
    _address_key,
    _as_int,
    _find_column,
    build_address_ranks,
    content_last_col,
    effective_last_col,
    canonical_address,
    column_name,
    date_headers,
    date_region,
    find_marker_column,
    find_target_column,
    parse_date_header,
    person_key,
    scan_bounds,
    sort_key_column,
    sort_key_value,
    weekday_number,
)
from app.wps.errors import WpsCloudError
from app.wps.ledger import SyncLedger
from app.wps.models import Change, CloudOrder, InsertBlock, SheetPlan

def formula_cells_for_new_rows(plan: SheetPlan, rows: Sequence[int]) -> list[dict[str, Any]]:
    """为新增行生成已出餐/剩余餐公式，日期列按表头动态识别。"""
    served = plan.columns.get("served") or 0
    left = plan.columns.get("left") or 0
    total = plan.columns.get("total") or 0
    date_columns = plan.date_cols
    if not rows or not date_columns or not served or not left or not total:
        return []
    cells = []
    for row in rows:
        date_range = f"{column_name(min(date_columns))}{row}:{column_name(max(date_columns))}{row}"
        cells.extend((
            {"row": row, "col": served, "value": f"=SUM({date_range})"},
            {"row": row, "col": left,
             "value": f"={column_name(total)}{row}-{column_name(served)}{row}"},
        ))
    return cells

def _build_sheet_plan(cli: KdocsCli, *, sheet: str, orders: Sequence[CloudOrder],
                      conf: Mapping[str, str], target: _dt.date,
                      run_date: _dt.date | None,
                      order_map: Mapping[str, Sequence[str]],
                      sort_enabled: bool) -> SheetPlan:
    """为**单个**子表生成写入计划（只读云端，不写任何东西）。

    本函数是把 ``build_plan`` 的循环体**原样搬出来**的，除下列两点外没有任何改动：

    * 原先靠闭包读取的 ``sheet``/``orders``/``conf``/``target``/``run_date``/
      ``order_map``/``sort_enabled`` 改为显式入参；
    * 原先 ``plans.append(plan)`` 之后 ``continue``（末尾那处是落到循环底部），
      现在统一 ``return plan``。

    只读写自己的局部变量与入参，不碰任何共享可变状态，因此不同子表之间完全独立，
    可以安全地在工作线程里并发执行。
    """
    plan = SheetPlan(sheet=sheet, file_id=conf["file_id"],
                     drive_id=conf.get("drive_id", ""),
                     target_date=target,
                     weekday_number=weekday_number(run_date or target))
    infos = cli.sheets_info(plan.file_id)
    if not infos:
        plan.warnings.append("云端文件不可读或不是在线表格")
        return plan
    worksheet_id = int(infos[0].get("sheetId") or 1)
    row_to, col_to = scan_bounds(infos[0])
    grid = cli.read_grid(plan.file_id, worksheet_id, 0, row_to, 0, col_to)
    plan.append_row = max(plan.append_row, 0)

    header = {col: text for (row, col), text in grid.items() if row == HEADER_ROW - 1}

    def col_or(names: Sequence[str], fallback: int) -> int:
        # 注意：_find_column 返回 1-based 列号，A 列 = 1；
        # 不能用 `or` 兜底（1 为真但 0 才是假，容易把 A 列误判）。
        value = _find_column(header, names)
        return fallback if value is None else value

    plan.columns = {
        "name": col_or(HEADER_NAME, 1),
        "address": col_or(HEADER_ADDRESS, 2),
        "phone": col_or(HEADER_PHONE, 3),
        "type": col_or(HEADER_TYPE, 0),
        "kind": col_or(HEADER_KIND, 0),
        "total": col_or(HEADER_TOTAL, 0),
        "served": col_or(HEADER_SERVED, 0),
        "left": col_or(HEADER_LEFT, 0),
        "remark": col_or(HEADER_REMARK, 0),
    }
    # 日期列区间：电话右侧 ~ 第一个结构列左侧。
    # 区间外（备注右侧）的日期样式格子是协作者的标记，只提示、不参与统计。
    date_lo, date_hi = date_region(plan.columns)
    stray_dates = [(int(col) + 1, str(text)) for col, text in header.items()
                   if int(col) + 1 > date_hi and parse_date_header(text)]
    if stray_dates:
        shown = "、".join(f"{column_name(c)}{HEADER_ROW}「{t}」"
                         for c, t in stray_dates[:2])
        plan.warnings.append(
            f"已忽略日期区间外的日期样式格 {shown}（判定为协作者的标记列，不参与统计）")
    found = find_target_column(header, target, col_from=date_lo, col_to=date_hi)
    if not found:
        note = ("（注意：备注右侧那些『9.14 周一』样式的格子是协作者的标记列，"
                "不会当成日期列）" if stray_dates else "")
        plan.warnings.append(
            f"云端表里没有 {target.month}.{target.day} 这一列，"
            f"请确认协作者是否已加好当天的列{note}")
        return plan
    plan.target_col, plan.target_header = found[0] + 1, found[1]
    plan.date_cols = [column for column, _text in date_headers(header, date_lo, date_hi)]
    if not plan.date_cols:
        plan.warnings.append("日期区间里一个日期列都没读到，已拒绝写入")
        return plan

    # 结构性校验：表头必须能读出姓名、电话、类型、餐种、总餐次。
    # 缺任何一项通常意味着表头被人改乱了（例如某列表头被覆盖成了日期），
    # 此时宁可拒绝写入，也不要往一张看不懂的表里写数字。
    # 已核对 2026-09 的 6 张正式表，这些表头都存在。
    header_texts = {str(v).strip() for v in header.values()}
    missing_header = [name for name, keys in (
        ("姓名", HEADER_NAME), ("电话", HEADER_PHONE),
        ("类型", HEADER_TYPE), ("餐种", HEADER_KIND),
        ("总餐次", HEADER_TOTAL))
        if not (header_texts & set(keys))]
    if missing_header:
        plan.warnings.append(
            f"云端表头异常，缺少 {'、'.join(missing_header)} 列，已拒绝写入"
            f"（请人工核对云端表结构）")
        return plan

    # 客户索引 + 追加行
    people: dict[tuple[str, str], int] = {}
    last_used = FIRST_DATA_ROW - 1
    for (row, col), text in grid.items():
        if row < FIRST_DATA_ROW - 1 or col != plan.columns["name"] - 1:
            continue
        last_used = max(last_used, row + 1)
        phone = grid.get((row, plan.columns["phone"] - 1), "")
        key = person_key(text, phone)
        if key[0]:
            people.setdefault(key, row + 1)
    plan.append_row = last_used + 1

    # 数据行（有姓名的行）与逐行地址 —— 排序要用。
    # 只认「有姓名」的行：表尾若有合计/说明之类的非人员行，不参与排序。
    addr_col = plan.columns.get("address") or 0
    data_rows: list[int] = []                       # 1-based，升序
    address_of: dict[int, str] = {}                 # 行号 -> 地址原文
    for (row, col), text in grid.items():
        if row < FIRST_DATA_ROW - 1 or col != plan.columns["name"] - 1:
            continue
        if not str(text).strip():
            continue
        data_rows.append(row + 1)
    data_rows = sorted(set(data_rows))
    if addr_col:
        for row in data_rows:
            address_of[row] = str(grid.get((row - 1, addr_col - 1), "") or "").strip()

    # 通讯记号列：优先认协作者正在用的 1~7 数字格，找不到用备注+3 兜底。
    if plan.columns.get("remark"):
        plan.marker_col = find_marker_column(header, plan.columns["remark"])

    # 学格式：这类排单表的底色**不统一**（实测东湖中餐 73 行白底、24 行绿底），
    # 不能简单取第一行或最后一行 —— 否则整批新行会被涂成某个偶然行的颜色。
    #
    # 做法：**一次**批量读"姓名列"（带格式），统计多数派底色；再取多数派里
    # 最靠近表尾的一行当样板，把它的逐列底色缓存到 plan.format_fills。
    # 只花 1~2 次接口调用（逐行读格式会消耗几十次，曾把当日额度打满）。
    name_col = plan.columns.get("name") or 1
    remark_col = plan.columns.get("remark") or name_col
    try:
        fmt_grid = cli.read_grid(plan.file_id, worksheet_id,
                                 FIRST_DATA_ROW - 1, row_to, name_col - 1, remark_col - 1,
                                 with_format=True)
    except WpsCloudError:
        fmt_grid = {}
    name_cells: dict[int, dict[str, str]] = {}
    for (r, c), payload in (fmt_grid or {}).items():
        if not isinstance(payload, dict):
            continue
        if c == name_col - 1 and str(payload.get("text", "")).strip():
            name_cells[r + 1] = payload
    candidates = sorted(name_cells)
    plan.format_rows = list(reversed(candidates))
    if candidates:
        from collections import Counter
        counts = Counter(str(name_cells[r].get("fill") or "") for r in candidates)
        dominant = counts.most_common(1)[0][0] if counts else ""
        preferred = [r for r in candidates
                     if str(name_cells[r].get("fill") or "") == dominant]
        if preferred:
            plan.format_rows = list(reversed(preferred))
        sample = plan.format_rows[0]
        fills: dict[int, str] = {}
        for (r, c), payload in (fmt_grid or {}).items():
            if r + 1 == sample and isinstance(payload, dict) and payload.get("fill"):
                fills[c + 1] = str(payload["fill"])
        plan.format_fills = fills

    total_col = plan.columns["total"]
    existing_changes: list[Change] = []
    new_orders: list[CloudOrder] = []
    for order in orders:
        key = person_key(order.name, order.phone)
        if not key[0]:
            continue
        row = people.get(key)
        if row:
            before = _as_int(grid.get((row - 1, total_col - 1))) if total_col else 0
            already = str(grid.get((row - 1, plan.target_col - 1), "")).strip() == CELL_MARK
            # 总餐次 = 本地「餐次」的绝对值（不是累加）。
            # 本地表就是网站当前的完整状态，云端总餐次应当与之一致；
            # 写成绝对值天然幂等：重复运行不会翻倍。
            want = order.meals
            change = Change(kind="existing", name=order.name, phone=order.phone,
                            row=row, delta=want - before, target_col=plan.target_col,
                            total_before=before, total_after=want, target_ok=already,
                            address=str(order.address or "").strip(),
                            meal_type=order.meal_type, meal_kind=order.meal_kind)
            # 半成品行自愈：上次中断可能留下缺「类型/餐种/公式」的行，本次补齐。
            type_col = plan.columns.get("type") or 0
            kind_col = plan.columns.get("kind") or 0
            served_col = plan.columns.get("served") or 0
            left_col = plan.columns.get("left") or 0
            change.fill_type = bool(
                type_col and order.meal_type
                and not str(grid.get((row - 1, type_col - 1), "")).strip())
            change.fill_kind = bool(
                kind_col and order.meal_kind
                and not str(grid.get((row - 1, kind_col - 1), "")).strip())
            change.fill_formula = bool(
                served_col and left_col and plan.date_cols
                and not str(grid.get((row - 1, served_col - 1), "")).strip()
                and not str(grid.get((row - 1, left_col - 1), "")).strip())
            if want == before:
                change.detail = "总餐次已一致"
            elif want < before:
                change.detail = (f"本地 {want} 餐 < 云端 {before} 餐，"
                                 "按本地值覆盖（如非预期请人工核对）")
                plan.warnings.append(f"{order.name}：{change.detail}")
            existing_changes.append(change)
        else:
            new_orders.append(order)

    # ---- 新增客户：统一插到第 3 行与第 4 行之间，再按列B 顺序重排整张表 ----
    order_list = [str(item).strip() for item in (order_map.get(sheet) or [])]
    written: list[tuple[CloudOrder, str]] = [
        (order, canonical_address(order.address, order_list)) for order in new_orders]

    ranks, tail_rank = build_address_ranks(
        order_list, [*address_of.values(), *(addr for _o, addr in written)])
    unknown = sorted({addr for addr in
                      [*address_of.values(), *(a for _o, a in written)]
                      if _address_key(addr) and _address_key(addr) not in ranks})
    plan.unknown_addresses = unknown[:5]
    for addr in unknown[:5]:
        plan.warnings.append(f"地址「{addr}」不在排序清单里，将排到表格最后面")
    if len(unknown) > 5:
        plan.warnings.append(f"…另有 {len(unknown) - 5} 个清单外地址，同样排到表尾")

    new_count = len(written)
    # 有老数据时插到第 4 行之前（第 3 行是第一行数据，不动它）；
    # 空表直接写第 3 行起，不留空行。
    first_insert_row = (FIRST_DATA_ROW + 1) if data_rows else FIRST_DATA_ROW
    plan.insert_blocks = []
    if new_count:
        plan.insert_blocks.append(InsertBlock(
            position=first_insert_row, count=new_count, first_row=first_insert_row,
            address="", append_only=not data_rows))

    # 排序辅助列：放在该表所有内容列右侧，排序范围才能覆盖全部列（否则会错位）。
    # 表里本来没有数据行时无需排序（没什么可排的）。
    sort_on = bool(sort_enabled and new_count and data_rows)
    helper_col = 0
    if sort_on:
        # 宽度必须以**接口报的 used range** 为准，不能只看读到的内容：内容右侧的
        # 边框/底色即使没有文字也属于“表格内容”，辅助列插在那里会把它们推走。
        reported_last = max(1, int(infos[0].get("colTo") or 0) + 1)
        real_last_col = max(effective_last_col(reported_last),
                            content_last_col(grid, col_to + 1))
        helper_col = sort_key_column(
            sheet_col_to=real_last_col,
            extra_cols=[plan.marker_col, plan.columns.get("remark") or 0])
        if helper_col > MAX_SORT_COL:
            # used range 报得离谱（整列污染）或表真的过宽：宁可不排序，也不排错位。
            plan.warnings.append(
                f"表格宽度异常（接口报最后使用列 {reported_last}，辅助列 {helper_col}），"
                "本次跳过排序：新客户会留在表格最上面")
            sort_on = False
    elif new_count and not sort_enabled:
        plan.warnings.append(
            "已按设置关闭排序：新客户留在表格最上面，不会按地址归位")
    elif new_count and not data_rows:
        plan.warnings.append("表里还没有数据行，本次只写入新客户，无需排序")

    # 预测排序结果（稳定排序：等键保持源顺序，与云端 range-sort 的承诺一致）。
    # 排序前的物理顺序 = 新行（第 4 行起）+ 已有行（原有先后）。
    # 注意：插到"第 4 行之前"时**第 3 行不动**，第 4 行及以下才整体下移。
    def pre_insert_row(row: int) -> int:
        return row if row < first_insert_row else row + new_count

    entries: list[tuple[int, int]] = []
    if sort_on:
        for idx, (_order, addr) in enumerate(written):
            rank = ranks.get(_address_key(addr), tail_rank)
            entries.append((sort_key_value(rank, SORT_FLAG_NEW), first_insert_row + idx))
        # 表中间的空行（没有姓名的行，通常是分组之间的空行）：跟着**上一行**的
        # 地址组走、排在那组最后 —— 否则空行会被排序甩到整张表的末尾。
        name_rows = set(data_rows)
        scan_last = data_rows[-1] if data_rows else FIRST_DATA_ROW - 1
        first_rank = (ranks.get(_address_key(address_of.get(data_rows[0], "")), tail_rank)
                      if data_rows else tail_rank)
        carried = first_rank
        for row in range(FIRST_DATA_ROW, scan_last + 1):
            if row in name_rows:
                carried = ranks.get(_address_key(address_of.get(row, "")), tail_rank)
            flag = SORT_FLAG_EXISTING if row in name_rows else SORT_FLAG_BLANK
            entries.append((sort_key_value(carried, flag), pre_insert_row(row)))
        ordered = sorted(entries, key=lambda item: item[0])
        plan.row_keys = {pre_row: key for key, pre_row in entries}
        final_row_of: dict[int, int] = {
            pre_row: FIRST_DATA_ROW + idx for idx, (_key, pre_row) in enumerate(ordered)}
        plan.last_data_row = FIRST_DATA_ROW + len(ordered) - 1
        plan.sort_key_col = helper_col
        plan.sort_range = (f"A{FIRST_DATA_ROW}:"
                           f"{column_name(helper_col)}{plan.last_data_row}")
    else:
        # 不排序：新行留在第 4 行起，第 4 行及以下的已有行整体下移 new_count 行。
        final_row_of = {pre_insert_row(row): pre_insert_row(row) for row in data_rows}
        for idx in range(new_count):
            final_row_of[first_insert_row + idx] = first_insert_row + idx
        plan.last_data_row = FIRST_DATA_ROW + len(data_rows) + new_count - 1

    for change in existing_changes:
        change.row = final_row_of.get(pre_insert_row(change.row), change.row)
        plan.final_rows[person_key(change.name, change.phone)] = change.row

    for idx, (order, addr) in enumerate(written):
        pre_row = first_insert_row + idx
        row = final_row_of.get(pre_row, pre_row)
        if sort_on:
            where = f"排到第 {row} 行（地址「{addr}」）"
        elif new_count:
            where = f"插到第 {row} 行（本次未排序）"
        else:
            where = f"追加到第 {row} 行"
        change = Change(kind="new", name=order.name, phone=order.phone,
                        row=row, delta=order.meals,
                        target_col=plan.target_col,
                        insert_row=pre_row,
                        total_before=0, total_after=order.meals,
                        target_ok=False,
                        address=addr, meal_type=order.meal_type,
                        meal_kind=order.meal_kind,
                        detail=where, fill_formula=True)
        plan.changes.append(change)
        plan.final_rows[person_key(order.name, order.phone)] = row
    plan.changes = existing_changes + plan.changes
    if sort_on:
        plan.sort_enabled = True
    return plan

def build_plan(cli: KdocsCli, *, local_orders: Mapping[str, Sequence[CloudOrder]],
               tables: Mapping[str, Mapping[str, str]],
               target: _dt.date,
               ledger: SyncLedger | None,
               marker_enabled: bool = True,
               run_date: _dt.date | None = None,
               address_order: Mapping[str, Sequence[str]] | None = None,
               sort_enabled: bool = True,
               log: Callable[[str], Any] | None = None) -> list[SheetPlan]:
    """只读云端，生成写入计划（不写任何东西）。

    ``run_date``：运行日（通讯记号写的是**运行日**的周几，不是目标日期 ——
    实测目标表：周四晚跑记号 5、周五晚跑记号 6）。缺省退回目标日期。

    ``address_order``：``{子表名: [地址, ...]}``，列B 的规定顺序；空列表表示
    按地址自然升序。``sort_enabled``：新增客户时是否重排整张表。

    每个子表要 3 次云端往返，6 张子表串行最多 18 次。这里**按子表分批并发**，
    ``Executor.map`` 保证 ``plans`` 仍严格按 ``local_orders`` 的顺序产出。
    """
    order_map = address_order or {}
    # 先滤掉配置里没有 file_id 的子表：这一步不产生任何云端调用，串行版本对它们
    # 也是一个请求都不发，因此挪到并发之前不改变任何可观测行为。
    items: list[tuple[str, Sequence[CloudOrder], Mapping[str, str]]] = []
    for sheet, orders in local_orders.items():
        conf = tables.get(sheet)
        if not conf or not conf.get("file_id"):
            continue
        items.append((sheet, orders, conf))

    def _one(item: tuple[str, Sequence[CloudOrder], Mapping[str, str]]) -> SheetPlan:
        sheet, orders, conf = item
        return _build_sheet_plan(cli, sheet=sheet, orders=orders, conf=conf,
                                 target=target, run_date=run_date,
                                 order_map=order_map, sort_enabled=sort_enabled)

    if len(items) <= 1:
        # 没有可重叠的往返，直接顺序执行，省掉线程池开销。
        return [_one(item) for item in items]

    # 分批提交而不是一次提交全部：``_build_sheet_plan`` 的云端异常会向上抛，
    # 一次提交全部会在出错时把已经发出去的调用全部作废（``cancel()`` 只能取消
    # 尚未开始的任务）；分批后最多只多耗 workers-1 次，贴近串行「出错即停」。
    workers = max(1, min(WPS_PLAN_WORKERS, len(items)))
    plans: list[SheetPlan] = []
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="wps-build-plan") as pool:
        for start in range(0, len(items), workers):
            batch = items[start:start + workers]
            plans.extend(pool.map(_one, batch))
    return plans

def summarize_plan(plans: Iterable[SheetPlan]) -> dict[str, int]:
    """汇总计划：用于界面展示与日志。"""
    update = new = unchanged = warn = 0
    for plan in plans:
        for change in plan.changes:
            if change.kind == "new":
                new += 1
            elif change.delta == 0 and change.target_ok:
                unchanged += 1
            else:
                update += 1
            if change.delta < 0:
                warn += 1
    return {"to_update": update, "to_append": new,
            "unchanged": unchanged, "warned": warn}

def format_plan(plans: Iterable[SheetPlan]) -> str:
    """把计划渲染成人类可读的多行文本（给预览用）。"""
    lines: list[str] = []
    for plan in plans:
        head = f"【{plan.sheet}】目标日期 {plan.target_date} "
        if plan.target_col:
            head += f"→ 第 {plan.target_col} 列（{plan.target_header}）"
        lines.append(head)
        for warning in plan.warnings:
            lines.append(f"    ⚠ {warning}")
        new_count = sum(1 for c in plan.changes if c.kind == "new")
        if new_count:
            if plan.sort_enabled:
                lines.append(
                    f"    本次新增 {new_count} 人：先插到第 {FIRST_DATA_ROW + 1} 行起，"
                    f"再按地址顺序重排整张表"
                    f"（第 {FIRST_DATA_ROW}~{plan.last_data_row} 行）")
            else:
                lines.append(
                    f"    本次新增 {new_count} 人：插到第 {FIRST_DATA_ROW + 1} 行起"
                    f"（已关闭排序，新行会留在表格最上面）")
        for change in plan.changes:
            if change.kind == "new":
                lines.append(f"    + 新增 {change.name}（{change.phone}）"
                             f"总餐次 {change.total_after}、日期格 {CELL_MARK}"
                             f" → {change.detail}")
            elif change.delta == 0 and change.target_ok:
                lines.append(f"    ≈ {change.name}：已完成（总餐次 {change.total_before}，日期格已填）")
            else:
                need_cell = "" if change.target_ok else f"，日期格 {CELL_MARK}"
                lines.append(f"    · {change.name}（{change.phone}）"
                             f"总餐次 {change.total_before}→{change.total_after}{need_cell}")
                if change.detail:
                    lines.append(f"        {change.detail}")
        if not plan.changes:
            lines.append("    （无变更）")
    return "\n".join(lines)
