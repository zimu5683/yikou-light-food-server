"""WPS 云同步的公共常量与纯工具函数。

这里只放无副作用、被多个子模块复用的逻辑：表头常量、行号/列号规则、
日期与地址解析、排序键、扫描边界等。
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.wps.errors import WpsCloudError


HEADER_NAME = ("名字", "姓名")


HEADER_ADDRESS = ("地址",)


HEADER_PHONE = ("电话",)


HEADER_TYPE = ("类型",)


HEADER_KIND = ("餐种",)


HEADER_TOTAL = ("总餐次", "总餐数")


HEADER_SERVED = ("已出餐", "出餐")


HEADER_LEFT = ("剩余餐", "剩余")


HEADER_REMARK = ("备注",)


CELL_MARK = "1"


# 本地排单表里「周一~周日」七列的名字（下标 = ``datetime.date.weekday()``）。
# 排除「当天不送」的 0 之前必须先知道协作者写的 0 是什么意思：日期格里的 0 是
# 协作者的明确决定，程序只读不写（见 planner/executor）。
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


FIRST_DATA_ROW = 3


HEADER_ROW = 2


TITLE_ROW = 1


MARKER_OFFSET = 3


MARKER_VALUES = range(1, 8)


STRUCT_COLUMN_KEYS = ("type", "kind", "total", "served", "left", "remark")


MAX_SORT_COL = 1000
# 排序前的“右侧边界探测”宽度（列数）。辅助列放在「已扫描到的最后一个有内容的列」
# 右边，这一带必须为空，否则说明有内容落在 MAX_SCAN_COL 之外 —— 那样排序区覆盖不到它，
# 排序后行与列就会错位，必须拒绝排序而不是硬排。
SORT_PROBE_WIDTH = 40


SORT_KEY_WIDTH = 4


SORT_FLAG_EXISTING = 0


SORT_FLAG_NEW = 1


SORT_FLAG_BLANK = 2


ADDRESS_ALIASES = {"小西": "小"}


MAX_SCAN_ROW = 400


MAX_SCAN_COL = 200


MAX_READ_CELLS = 45000


WRITE_BATCH_CELLS = 80


FONT_SIZE_TO_TWIP = 20


FILL_LUXURY = 0xFFFFC000        # 豪华餐整行底色：金黄（与"总餐次"列同色）


FILL_GOLD = 0xFFFFC000          # 总餐次/已出餐/剩余餐 的既有底色


_ALIGN_H = {"haGeneral": 0, "haLeft": 1, "haCenter": 2, "haRight": 3,
            "haFill": 4, "haJustify": 5, "haCenterContinuous": 6}


_ALIGN_V = {"vaTop": 0, "vaCenter": 1, "vaBottom": 2, "vaJustify": 3}


RATE_LIMIT_CODES = {429001, 429002}


WPS_PLAN_WORKERS = 2


DATE_RE = re.compile(r"^\s*(\d{1,2})\s*[.．]\s*(\d{1,2})\s*(?:周|星期|礼拜)?\s*([一二三四五六日天])?")


def target_date_for(now: _dt.datetime | None = None, *,
                    start_hour: int = 20, end_hour: int = 10) -> _dt.date:
    """按"晚上跑算次日"的规则算目标日期。

    窗口为 [start_hour, 24) ∪ [0, end_hour)：落在窗口内则 +1 天。
    """
    now = now or _dt.datetime.now()
    if now.hour >= start_hour or now.hour < end_hour:
        return (now + _dt.timedelta(days=1)).date()
    return now.date()


def weekday_number(day: _dt.date) -> int:
    """通讯记号数字：周日=1、周一=2 … 周六=7。"""
    return 1 if day.weekday() == 6 else day.weekday() + 2


def parse_date_header(text: Any) -> tuple[int, int] | None:
    """从表头文字解析 (月, 日)；只认月.日，忽略星期。"""
    if text is None:
        return None
    m = DATE_RE.match(str(text))
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return month, day


def cell_value_text(value: Any) -> str:
    """把云端单元格的**原值**（``originalCellValue``）转成最贴近本地写法的文本。

    * 数字字符串原样保留（``"159678999961"``）；
    * 浮点整数值去掉多余的 ``.0``（``159678999961.0`` → ``"159678999961"``）；
    * ``None`` → 空串。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def looks_like_number(text: str) -> bool:
    """纯数字文本（去掉两侧空白后全为 0-9，且至少一位）。"""
    return bool(text) and text.isdigit()


def text_cell_formula(text: Any) -> str:
    """写单元格用的"强制文本"公式：纯数字串一律带前置单引号。

    平台的写值操作只有 ``opType=formula`` 一种。直接把纯数字字符串交给它当公式
    写，12 位号码会被当成数字存起来：回读的原值仍在，但**显示**成科学计数
    ``1.59679E+11``，协作者看到的就不是客户填的号码（2026-10-12 医学院中餐事故）。
    前置单引号是电子表格的"按文本存"约定：显示与回读都保持原样，值不变形。

    非数字文本（姓名/地址/餐种，以及带空格/连字符/+86 的电话）原样写，不改写。
    """
    raw = "" if text is None else str(text)
    if looks_like_number(raw.strip()):
        return "'" + raw
    return raw


def person_key(name: Any, phone: Any) -> tuple[str, str]:
    """客户匹配键：名字 + 电话（都归一化：去空白、电话去小数点与非数字尾巴）。

    电话**不做位数校验、不截断、不清洗语义**：空格、连字符、``+86``、前导 0
    都只丢符号留数字。匹配建立在"值"上 —— 调用方必须先把回读值取成
    :func:`cell_value_text`（原值），不能拿科学计数法的显示文本当值。
    """
    n = str(name or "").strip()
    p = str(phone or "").strip()
    if p.endswith(".0"):
        p = p[:-2]
    p = re.sub(r"\D", "", p)
    return n, p


def normalize_phone(value: Any) -> str:
    """把手机号规范化成「11 位 ASCII 数字」形式（与 :func:`person_key` 口径一致）。"""
    return person_key("", value)[1]


def _address_key(text: Any) -> str:
    """地址组匹配键：去空白 + 忽略大小写（云端「b2」和本地「B2」是同一组）。"""
    return re.sub(r"\s+", "", str(text or "")).casefold()


def canonical_address(raw: Any, order: Sequence[str] = ()) -> str:
    """落表时用的地址写法：先过别名（本地「小西」= 云端「小」），
    再按清单里的标准写法统一（本地写「B5」→ 落「b5」；匹配忽略大小写与空格）。"""
    text = str(raw or "").strip()
    text = ADDRESS_ALIASES.get(text, text)
    key = _address_key(text)
    for item in order:
        if _address_key(item) == key:
            return str(item).strip()
    return text


_NATURAL_CHUNK = re.compile(r"(\d+)")


def natural_key(text: Any) -> tuple:
    """自然序排序键：「医2号」排在「医10号」之前（数字按数值比，不按字典序）。

    返回可比较的元组：文本块用 (0, 文本)，数字块用 (1, 数值)。
    """
    chunks: list[tuple[int, Any]] = []
    for piece in _NATURAL_CHUNK.split(str(text or "")):
        if not piece:
            continue
        if piece.isdigit():
            chunks.append((1, int(piece)))
        else:
            chunks.append((0, piece.casefold()))
    return tuple(chunks)


def build_address_ranks(order: Sequence[str],
                        addresses: Iterable[str]) -> tuple[dict[str, int], int]:
    """算出「归一化地址 -> 名次」，名次越小越靠前。

    ``order`` 非空：按清单顺序排名，清单里没有的一律 ``len(order)``（排表尾）；
    ``order`` 为空：对出现过的地址按**自然序**升序排名（医学院用这种）。
    返回 ``(名次表, 表尾名次)``。
    """
    normalized = [_address_key(addr) for addr in order if str(addr).strip()]
    if normalized:
        ranks = {addr: idx for idx, addr in enumerate(normalized)}
        return ranks, len(normalized)
    distinct = sorted({_address_key(a) for a in addresses if _address_key(a)}, key=natural_key)
    return {addr: idx for idx, addr in enumerate(distinct)}, len(distinct)


def sort_key_value(rank: int, flag: int) -> int:
    """复合排序键：地址名次为主，同组内已有行（0）在前、新增行（1）在后。"""
    return rank * 10 + flag


def format_sort_key(value: int) -> str:
    """零填充成定宽字符串 —— 接口若按文本排序，字典序也必须等于数值序。"""
    return f"{value:0{SORT_KEY_WIDTH}d}"


def sort_key_column(*, sheet_col_to: int, extra_cols: Iterable[int] = ()) -> int:
    """选排序辅助列：一定落在该表**所有内容列右侧**。

    排序范围必须覆盖所有有内容的列，否则右侧那些列不会跟着行一起移动，
    行与列就错位了。放在最后使用列的右边一格，天然满足这个条件。
    """
    last = max([int(sheet_col_to or 0), 1, *[int(c or 0) for c in extra_cols]])
    return last + 1


def date_region(columns: Mapping[str, int],
                *, fallback_hi: int = MAX_SCAN_COL) -> tuple[int, int]:
    """日期列允许出现的区间 ``(lo, hi)``（1-based 闭区间）。

    规则：从**电话列的右一列**开始，到**第一个结构列（类型/餐种/总餐次/已出餐/
    剩余餐/备注）的左一列**结束。备注右侧是协作者写「9.14 周一」标记的区域，
    把它排除掉才不会把标记列当成日期列。
    """
    lo = int(columns.get("phone") or 3) + 1
    struct = [int(columns[key]) for key in STRUCT_COLUMN_KEYS if columns.get(key)]
    hi = (min(struct) - 1) if struct else int(fallback_hi)
    if hi < lo:
        hi = lo - 1
    return lo, hi


def date_headers(header: Mapping[int, str], lo: int, hi: int) -> list[tuple[int, str]]:
    """表头里落在日期区间内的日期样式格，返回 ``[(1-based 列, 原文)]``。"""
    found: list[tuple[int, str]] = []
    for col in sorted(header):
        column = int(col) + 1
        if lo <= column <= hi and parse_date_header(header[col]):
            found.append((column, str(header[col])))
    return found


def find_marker_column(header: Mapping[int, str], remark_col: int) -> int:
    """定位协作者通讯记号列（返回 1-based 列号，找不到用兜底位置）。

    优先：备注列右侧第一个内容为 1~7 整数的格子 —— 那是协作者**正在用**的
    记号位（实测它会随协作方式变化，不能写死偏移）。
    兜底：备注列右边第 3 列（备注+2 已被协作者的日期标记占用）。
    **护栏**：找到的格子若本身是「9.14 周一」这类日期样式，说明那是协作者的
    标记列，宁可不用也不覆盖 —— 此时返回兜底位置。
    """
    for col in sorted(header):
        if col + 1 <= remark_col:
            continue
        text = str(header[col]).strip()
        if parse_date_header(text):
            continue
        if text.isdigit() and int(text) in MARKER_VALUES:
            return col + 1
    return remark_col + MARKER_OFFSET


def _find_column(header: Mapping[int, str], names: Sequence[str]) -> int | None:
    """在表头里按名字找列，返回 **1-based** 列号（与写入接口保持一致）。

    注意：``header`` 的键来自 ``read_grid``，是 0-based 列号。
    """
    for col, text in header.items():
        if str(text).strip() in names:
            return int(col) + 1
    return None


def find_target_column(header: Mapping[int, str],
                       target: _dt.date,
                       *, col_from: int = 1, col_to: int = MAX_SCAN_COL
                       ) -> tuple[int, str] | None:
    """在表头里找目标日期的列（只比对月.日），**限定在日期区间内**。

    必须限定区间：协作者的标记格里也写着「9.14 周一」，若不设限，在当天还没有
    真实日期列时会命中标记列，把 1 写进协作者的格子。
    """
    for col in sorted(header):
        column = int(col) + 1
        if not (col_from <= column <= col_to):
            continue
        parsed = parse_date_header(header[col])
        if parsed and parsed == (target.month, target.day):
            return col, str(header[col])
    return None


def column_name(column: int) -> str:
    """把 1-based 列号转成 Excel 列名（1 → ``A``，27 → ``AA``）。"""
    result = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _as_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


_TRANSIENT_HINTS = (
    "TLS handshake timeout", "connection reset", "connection refused",
    "i/o timeout", "EOF", "broken pipe", "no such host",
    "temporarily unavailable", "timeout awaiting response",
)


def _is_transient(exc: BaseException) -> bool:
    text = str(exc)
    return any(hint.lower() in text.lower() for hint in _TRANSIENT_HINTS)


def scan_bounds(info: Mapping[str, Any] | None, *,
                max_col: int = MAX_SCAN_COL,
                max_row: int = MAX_SCAN_ROW,
                budget: int = MAX_READ_CELLS) -> tuple[int, int]:
    """按接口单次上限，算出安全的**单次**读取范围 ``(row_to, col_to)``（0-based 闭区间）。

    先按需取列宽，再据此压缩行数 —— 宽表（排单表有 100+ 个日期列）必须少读行。

    **这只是"这一次请求"的范围，不是整张表的范围**：拿它当整表去数人会静默漏掉
    后面的行（旧事故：230 行的客户被当成不存在）。要读全请用
    :func:`scan_region` + :func:`read_grid_chunked`。
    """
    sheet = (info or {}) if isinstance(info, Mapping) else {}
    col_to = min(int(sheet.get("colTo") or 0), max_col)
    row_to = min(int(sheet.get("rowTo") or 0), max_row)
    cols = col_to + 1
    if cols > 0:
        row_to = min(row_to, max(0, budget // cols - 1))
    return row_to, col_to


def scan_region(info: Mapping[str, Any] | None, *,
                max_col: int = MAX_SCAN_COL,
                max_row: int = MAX_SCAN_ROW) -> tuple[int, int, str]:
    """算出「**整张表**」要读的范围 ``(row_to, col_to, block_reason)``（0-based 闭区间）。

    与 :func:`scan_bounds` 的区别：那个只保证**单次**请求不超格数上限，宽表里会把
    行数压到「45000 ÷ 列数」行以内 —— 直接拿它当读取范围就是**静默截断**（老客户
    被读成"不存在" → 名单漏人、同步时被当新客户插重复行）。这里给出按
    ``MAX_SCAN_ROW``／``MAX_SCAN_COL`` 要读全的范围，配合 :func:`read_grid_chunked`
    分块读取即可覆盖整张表。

    ``block_reason`` 非空表示**无法证明读全**（used range 超过 ``MAX_SCAN_ROW``）：
    调用方必须阻断（写侧拒绝写入、读侧按拒绝语义拒单），绝不能拿这份残缺快照
    判断"当天名单为空 / 这是新客户"。
    """
    sheet = (info or {}) if isinstance(info, Mapping) else {}
    reported_rows = int(sheet.get("rowTo") or 0)
    col_to = min(int(sheet.get("colTo") or 0), max_col)
    row_to = min(reported_rows, max_row)
    if reported_rows > max_row:
        return row_to, col_to, (
            f"云端表的 used range 到第 {reported_rows + 1} 行，超过可完整读取的上限 "
            f"{max_row + 1} 行：读到的只是其中一部分，"
            f"可能漏掉已存在的客户或当天要送的人。")
    return row_to, col_to, ""


def read_grid_chunked(cli: Any, file_id: str, worksheet_id: int,
                      row_from: int, row_to: int, col_from: int, col_to: int,
                      *, with_format: bool = False) -> dict[tuple[int, int], Any]:
    """按单次格数上限**分块**读取 0-based 闭区间，保证覆盖整个范围。

    单次请求撞 5 万格上限会被接口拒绝；过去靠"压缩读取范围"躲开它，代价是宽表
    只读到前几行。正确做法是**多读几次**：每块的行数 = ``MAX_READ_CELLS // 列数``。
    """
    if row_to < row_from or col_to < col_from:
        return {}
    width = col_to - col_from + 1
    chunk = max(1, MAX_READ_CELLS // max(1, width))
    grid: dict[tuple[int, int], Any] = {}
    start = row_from
    while start <= row_to:
        end = min(row_to, start + chunk - 1)
        if with_format:
            grid.update(cli.read_grid(file_id, worksheet_id, start, end,
                                      col_from, col_to, with_format=True))
        else:
            grid.update(cli.read_grid(file_id, worksheet_id, start, end,
                                      col_from, col_to))
        start = end + 1
    return grid


def effective_last_col(reported: int, *,
                       scan_width: int = MAX_SCAN_COL) -> int:
    """把 ``sheetsInfo.colTo`` 换算成可用的「最后使用列」（1-based）。

    不能无条件相信它：那是 WPS 记的 used range 右下角，一旦表格被“整列/到最右列”
    的操作碰过（本程序自己就会用 ``col_to: 16383`` 删行），它就会停在网格最右列。
    实测东湖中餐返回 16383，而真实内容只到第 22 列；据此算出的辅助列超过
    ``MAX_SORT_COL``，排序会被静默跳过 —— 这正是“东湖中餐排不了序”的根因。

    规则：报出来的宽度没超过我们真正读过的范围时照用；超过则退回读取宽度，
    边界再由 :func:`probe_sort_area` 兜底。
    """
    reported = max(1, int(reported or 0))
    return reported if reported <= max(1, int(scan_width)) else max(1, int(scan_width))


def content_last_col(grid: Mapping[tuple[int, int], Any], fallback: int) -> int:
    """已读到的内容里最后一个有内容的列（1-based）；没有内容时用 ``fallback``。"""
    last = 0
    for _row, col in grid:
        try:
            column = int(col) + 1
        except (TypeError, ValueError):
            continue
        if column > last:
            last = column
    return max(int(fallback or 0), last, 1)


def probe_sort_area(cli: Any, plan: Any, worksheet_id: int, *,
                    col_from: int, row_to: int,
                    width: int = SORT_PROBE_WIDTH) -> tuple[bool, str]:
    """确认「辅助列右边的这一带」是空的 —— 排序区必须覆盖到表里所有内容。

    ``col_from`` 必须从辅助列 + 1 开始，避免把辅助列自己的排序键误判成右侧内容。
    只读一次；``read_grid`` 的键是 0-based 坐标。
    """
    col_to = min(MAX_SORT_COL, int(plan.sort_key_col) + max(0, int(width) - 1), 16383)
    if col_to <= int(col_from):
        return True, ""
    try:
        grid = cli.read_grid(plan.file_id, worksheet_id, FIRST_DATA_ROW - 1,
                             int(row_to), int(col_from) - 1, col_to)
    except WpsCloudError:
        # 探测本身失败不阻断排序：按“没探到”处理（旧行为），只留提示机会。
        return True, ""
    if not grid:
        return True, ""
    row, col = min(grid, key=lambda key: (int(key[1]), int(key[0])))
    return False, (f"{column_name(int(col) + 1)}{int(row) + 1}「"
                   f"{str(grid[(row, col)])[:12]}」")


def _argb_to_int(hexcolor: str) -> int:
    """"#FF92D050" -> 4287811664（接口用 ARGB 整数传色）。"""
    text = str(hexcolor).strip().lstrip("#")
    try:
        return int(text, 16) & 0xFFFFFFFF
    except ValueError:
        return 0xFFFFFFFF
