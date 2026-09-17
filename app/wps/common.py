"""WPS 云同步的公共常量与纯工具函数。

这里只放无副作用、被多个子模块复用的逻辑：表头常量、行号/列号规则、
日期与地址解析、排序键、扫描边界等。
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


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


FIRST_DATA_ROW = 3


HEADER_ROW = 2


TITLE_ROW = 1


MARKER_OFFSET = 3


MARKER_VALUES = range(1, 8)


STRUCT_COLUMN_KEYS = ("type", "kind", "total", "served", "left", "remark")


MAX_SORT_COL = 1000


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


def person_key(name: Any, phone: Any) -> tuple[str, str]:
    """客户匹配键：名字 + 电话（都归一化：去空白、电话去小数点与非数字尾巴）。"""
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
    """按接口单次上限，算出安全的读取范围 ``(row_to, col_to)``（0-based 闭区间）。

    先按需取列宽，再据此压缩行数 —— 宽表（排单表有 100+ 个日期列）必须少读行。
    """
    sheet = (info or {}) if isinstance(info, Mapping) else {}
    col_to = min(int(sheet.get("colTo") or 0), max_col)
    row_to = min(int(sheet.get("rowTo") or 0), max_row)
    cols = col_to + 1
    if cols > 0:
        row_to = min(row_to, max(0, budget // cols - 1))
    return row_to, col_to


def _argb_to_int(hexcolor: str) -> int:
    """"#FF92D050" -> 4287811664（接口用 ARGB 整数传色）。"""
    text = str(hexcolor).strip().lstrip("#")
    try:
        return int(text, 16) & 0xFFFFFFFF
    except ValueError:
        return 0xFFFFFFFF
