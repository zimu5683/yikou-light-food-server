"""从 WPS 云端读当天名单：列定位、地址过滤、数据校验与日期口径。"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from typing import Any, Callable

from app.ordering.import_models import (
    CELL_MARK, COL_ADDRESS, FIRST_DATA_ROW, ImportRefused, MealImport,
)
from app.wps.sync import (
    ADDRESS_ALIASES,
    HEADER_ADDRESS,
    HEADER_NAME,
    HEADER_PHONE,
    HEADER_ROW,
    KdocsCli,
    WpsCloudError,
    _address_key,
    _find_column,
    column_name,
    date_region,
    effective_tables,
    find_target_column,
    scan_bounds,
)


SSS_SHEET_SOURCES: dict[str, str] = {"午餐": "东湖中餐", "晚餐": "东湖晚餐"}

SKIP_ADDRESSES: tuple[str, ...] = ("大西", "小")

_PHONE_RE = re.compile(r"^[0-9]{11}$")

_WHITESPACE_RE = re.compile(r"\s+")

_MAX_PROBLEM_ROWS = 10

def _log(log: Callable[[str], Any] | None, message: str, level: str = "INFO") -> None:
    """写日志；兼容只接收一个参数的简单回调。"""
    if log is None:
        return
    try:
        log(message)
    except TypeError:
        try:
            log(message, level)  # type: ignore[call-arg]
        except Exception:
            pass
    except Exception:
        pass

def _clean_text(value: Any) -> str:
    """单元格文本规范化：NFKC（全角转半角）+ 去首尾空白。"""
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()

def normalise_phone(value: Any) -> str:
    """规范成 11 位 ASCII 数字字符串，拒绝含糊格式。

    与 :func:`app.ordering.sss._normalise_phone` 行为一致（这里独立实现是为了避免
    与 ``app.ordering.sss`` 形成循环导入）。
    """
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        # openpyxl/接口把整数字段读成 float 时只接受精确整数，防止 .5 被截断。
        if not value.is_integer():
            return ""
        text = str(int(value))
    else:
        text = unicodedata.normalize("NFKC", str(value)).strip()
    return _WHITESPACE_RE.sub("", text)

def address_group(value: Any) -> str:
    """地址归一化键：小西→小、忽略空格与大小写（与云同步地址规则一致）。"""
    text = _clean_text(value)
    text = ADDRESS_ALIASES.get(text, text)
    return _address_key(text)

_SKIP_ADDRESS_KEYS = frozenset(
    _address_key(ADDRESS_ALIASES.get(item, item)) for item in SKIP_ADDRESSES)

def should_skip_address(value: Any) -> bool:
    """地址是否属于「不需要闪时送」的组（大西 / 小）。"""
    key = address_group(value)
    return bool(key) and key in _SKIP_ADDRESS_KEYS

def ordering_target_date(now: _dt.datetime | None = None, *,
                         start_hour: int = 20) -> _dt.date:
    """闪时送下单要识别的「当天」日期。

    用户口径（2026-09-15 原话）：「9.15 的晚上 8 点之后到 9.16 早上 10 点之前，
    程序需要识别的日期是 9.16，然后下 9.16 的中午晚上的单」。因此：

    - ``hour >= start_hour``（晚上 20:00 之后）→ 识别**次日**；
    - 其余时刻（含次日 00:00~10:00 的清晨）→ 识别**运行日**。

    与云同步的 :func:`app.wps.sync.target_date_for` **刻意不同**：后者在
    ``[0, end_hour)`` 也 +1 天（那是"这一晚该往哪一列写"的问题）；下单必须与
    实际送达日期一致 —— 清晨 9 点下单送的是**当天**的午餐/晚餐，不能顺延。
    """
    now = now or _dt.datetime.now()
    if now.hour >= max(0, min(23, int(start_hour))):
        return (now + _dt.timedelta(days=1)).date()
    return now.date()

def _cloud_error(table: str, exc: BaseException) -> ImportRefused:
    return ImportRefused(
        f"读取云端表「{table}」失败：{exc}。已拒绝下单，避免用隔天名单下单。"
        "可先在「云文档同步」页签完成授权或等额度恢复；"
        "也可以把「名单来源」切到「本地 Excel」，自己把当天名单填进《闪时送.xlsx》后再下单。")

def check_target_day(target: _dt.date, delivery_date: _dt.date) -> None:
    """日期闸门：识别到的目标日必须等于本次下单真正使用的送达日期。"""
    if target == delivery_date:
        return
    raise ImportRefused(
        f"日期不匹配：云端当天名单用的是 {target.month}.{target.day}，"
        f"而本次下单的送达日期是 {delivery_date.month}.{delivery_date.day}。"
        "已拒绝下单（只有 20:00 ~ 次日 10:00 之间运行时两者才会一致，"
        "请在 20:00 之后再跑，或改用「本地 Excel」名单来源）。")

def read_cloud_meal(cli: Any, *, file_id: str, table: str, meal: str,
                    target: _dt.date,
                    log: Callable[[str], Any] | None = None) -> MealImport:
    """读一张云端表的当天列，返回该餐要下单的人（只读，2 次接口调用）。

    - 列位置全部按表头文字定位（协作者常插列，不能写死列号）；
    - 日期列只在「电话列右一列 ~ 第一个结构列左一列」之间找，备注右侧那句
      「9.16 周三」是协作者的标记，绝不当成日期列；
    - 地址归一化后属于「大西/小」的行计入 ``skipped_address``，不进下单名单。
    """
    result = MealImport(meal=meal, table=table, file_id=file_id, target=target)
    try:
        infos = cli.sheets_info(file_id)
    except WpsCloudError as exc:
        raise _cloud_error(table, exc) from exc
    if not infos:
        raise ImportRefused(
            f"云端表「{table}」不可读或不是在线表格（file_id={file_id}）。"
            "已拒绝下单；可改用「本地 Excel」名单来源人工下单。")
    info = infos[0]
    worksheet_id = int(info.get("sheetId") or 1)
    row_to, col_to = scan_bounds(info)
    try:
        grid = cli.read_grid(file_id, worksheet_id, 0, row_to, 0, col_to)
    except WpsCloudError as exc:
        raise _cloud_error(table, exc) from exc

    header = {col: text for (row, col), text in grid.items() if row == HEADER_ROW - 1}
    name_col = _find_column(header, HEADER_NAME)
    phone_col = _find_column(header, HEADER_PHONE)
    if name_col is None or phone_col is None:
        missing = []
        if name_col is None:
            missing.append("名字/姓名")
        if phone_col is None:
            missing.append("电话")
        raise ImportRefused(
            f"云端表「{table}」表头异常：找不到「{'、'.join(missing)}」列，"
            "已拒绝下单（请人工核对云端表结构，或改用「本地 Excel」名单来源）。")
    addr_col = _find_column(header, HEADER_ADDRESS) or COL_ADDRESS
    columns = {"name": name_col, "address": addr_col, "phone": phone_col}

    date_lo, date_hi = date_region(columns)
    found = find_target_column(header, target, col_from=date_lo, col_to=date_hi)
    if not found:
        result.skipped = True
        result.skip_reason = (
            f"云端表「{table}」没有 {target.month}.{target.day} 这一列，"
            "当天不送这一餐（周末不做晚餐时属正常）")
        _log(log, f"云名单：{result.skip_reason}")
        return result
    target_col, date_text = found[0] + 1, str(found[1]).strip()
    result.date_text = date_text

    seen: set[tuple[str, str]] = set()
    for row0 in range(FIRST_DATA_ROW - 1, row_to + 1):
        if _clean_text(grid.get((row0, target_col - 1), "")) != CELL_MARK:
            continue
        result.marked_total += 1
        name = _clean_text(grid.get((row0, name_col - 1), ""))
        door = _clean_text(grid.get((row0, addr_col - 1), ""))
        phone = normalise_phone(grid.get((row0, phone_col - 1), ""))
        if should_skip_address(door):
            result.skipped_address += 1
            continue
        key = (name, phone)
        if key in seen:
            result.warnings.append(
                f"{table} 第 {row0 + 1} 行：{name or '（无姓名）'} 重复标 1，已去重")
            continue
        seen.add(key)
        result.orders.append({
            "row": row0 + 1,          # 云端行号，便于日志与报错定位
            "name": name,
            "door": door,
            "phone": phone,
            "source": table,
            # 报错时指出"哪一格、现在是什么"，用户可直接照着去云端改。
            "name_cell": f"{column_name(name_col)}{row0 + 1}",
            "address_cell": f"{column_name(addr_col)}{row0 + 1}",
            "phone_cell": f"{column_name(phone_col)}{row0 + 1}",
            "raw_phone": _clean_text(grid.get((row0, phone_col - 1), "")),
        })
    return result

def validate_orders(meals: list[MealImport]) -> list[str]:
    """校验**要下单的人**（被地址过滤掉的人不参与），返回问题行描述。

    描述里带上**云端单元格地址与当前值**（如 ``C106「0」``），用户可以直接照着
    去云端改，不必再来回排查"到底哪一格不对"。
    """
    problems: list[str] = []
    for meal in meals:
        for order in meal.orders:
            row = order.get("row", "?")
            name = order.get("name") or "无姓名"
            issues = []
            if not order.get("name"):
                issues.append(f"{order.get('name_cell') or '姓名格'} 是空的")
            if not order.get("door"):
                issues.append(f"{order.get('address_cell') or '地址格'} 是空的")
            if not _PHONE_RE.fullmatch(str(order.get("phone") or "")):
                raw = str(order.get("raw_phone") or "").strip()
                shown = f"现在是「{raw}」" if raw else "是空的"
                issues.append(
                    f"{order.get('phone_cell') or '电话格'} {shown}，不是 11 位手机号")
            if issues:
                problems.append(f"{meal.table} 第 {row} 行 {name}：" + "；".join(issues))
    return problems

def collect_day_orders(config: Any, *, target: _dt.date,
                       cli: Any | None = None,
                       log: Callable[[str], Any] | None = None) -> list[MealImport]:
    """读两张云端表（东湖中餐/东湖晚餐）并返回当天要下单的名单。

    读取目标沿用云同步页签的**生效目标**（测试模式下读测试副本）；
    任何读取/校验问题都抛 :class:`ImportRefused`，绝不返回半份名单。
    """
    try:
        tables = effective_tables(config)
    except WpsCloudError as exc:
        raise ImportRefused(
            f"云端表配置不可用：{exc}。已拒绝下单；"
            "请在「云文档同步」页签确认写入目标后再试。") from exc
    if cli is None:
        try:
            cli = KdocsCli(getattr(config, "wps_cli_path", "") or None)
        except WpsCloudError as exc:
            raise ImportRefused(
                f"找不到 kdocs-cli 组件：{exc} 已拒绝下单；"
                "可改用「本地 Excel」名单来源人工下单。") from exc

    meals: list[MealImport] = []
    for meal, table in SSS_SHEET_SOURCES.items():
        conf = tables.get(table) or {}
        file_id = str(conf.get("file_id") or "").strip()
        if not file_id:
            skipped = MealImport(meal=meal, table=table, target=target, skipped=True,
                                 skip_reason=f"云端表「{table}」未配置 file_id，这一餐不下单")
            _log(log, f"云名单：{skipped.skip_reason}")
            meals.append(skipped)
            continue
        meals.append(read_cloud_meal(cli, file_id=file_id, table=table, meal=meal,
                                     target=target, log=log))

    problems = validate_orders(meals)
    if problems:
        shown = "；".join(problems[:_MAX_PROBLEM_ROWS])
        more = f"；另有 {len(problems) - _MAX_PROBLEM_ROWS} 行" if len(problems) > _MAX_PROBLEM_ROWS else ""
        raise ImportRefused(
            f"云端当天名单存在无效数据（共 {len(problems)} 行）：{shown}{more}。"
            "已拒绝下单，请先在云端补全姓名/地址/电话（必须是 11 位手机号）后再试。")
    return meals
