"""WPS 云同步的数据结构（纯数据，无 IO）。"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field


@dataclass
class CloudOrder:
    """本地排单表里的一行订单。"""

    sheet: str
    name: str
    address: str
    phone: str
    meal_type: str          # 中餐 / 晚餐
    meal_kind: str          # 经济 / 豪华
    meals: int              # 「餐次」列
    row: int = 0            # 本地行号，便于报错定位


@dataclass
class Change:
    """一条待写入云端的变更。"""

    kind: str               # existing / new
    name: str
    phone: str
    row: int                # 云端行号（排序后要写入的行号）
    delta: int              # 总餐次差值（want - before），0 表示已一致
    target_col: int         # 目标日期列（1-based）
    total_before: int = 0
    total_after: int = 0
    target_ok: bool = False  # 目标日期格是否已经是 1
    # 新客户追加行需要一并写入的字段（来自本地排单表）
    address: str = ""
    meal_type: str = ""     # 类型：中餐 / 晚餐
    meal_kind: str = ""     # 餐种：经济 / 豪华
    detail: str = ""
    # 补全标记：云端这些格子目前是空的，本次要补上（防止上次中断留下半成品行）
    fill_type: bool = False
    fill_kind: bool = False
    fill_formula: bool = False
    # 新客户**排序前**所在的物理行号：排序前要写的东西（姓名/地址/电话/类型/餐种、
    # 底色）必须写在这里，否则会覆盖掉别人（排序后行号在那时还属于其他人）。
    # 放在最后，避免影响按位置构造 Change 的老代码。
    insert_row: int = 0

    @property
    def needs_write(self) -> bool:
        """这条变更是否真的需要写云端（目标格已是目标状态且无需补格式时为 ``False``）。"""
        return ((not self.target_ok) or self.total_after != self.total_before
                or self.fill_type or self.fill_kind or self.fill_formula)


@dataclass
class InsertBlock:
    """一批要插入云端的新客户行。

    2026-09-15 起流程改为：**所有新客户合成一块，统一插到第 3 行与第 4 行之间**，
    再按列B 的地址顺序把整张表重排（见 ``SheetPlan.sort_*``）。
    ``position`` 是「插到这一行之前」（固定 = 4）；``append_only`` 表示表里本来
    就没有数据行，直接写第 4 行起即可，不需要调用插入行接口。
    """

    position: int
    count: int
    first_row: int
    address: str = ""
    append_only: bool = False


@dataclass
class SheetPlan:
    """单张表的写入计划。"""

    sheet: str
    file_id: str
    drive_id: str = ""
    target_date: _dt.date | None = None
    target_col: int = 0
    target_header: str = ""
    weekday_number: int = 0
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    append_row: int = 0
    columns: dict[str, int] = field(default_factory=dict)
    # 供"学格式"参考的已有数据行号（1-based）
    format_rows: list[int] = field(default_factory=list)
    # 参考行的逐列底色 {1-based 列: "#AARRGGBB"}（build_plan 里一次性读好并缓存）
    format_fills: dict[int, str] = field(default_factory=dict)
    # 新客户插入块（统一一块：插到第 4 行之前）；老客户行号已计入排序结果
    insert_blocks: list[InsertBlock] = field(default_factory=list)
    # 协作者通讯记号列（1-based）；0 = 没找到
    marker_col: int = 0
    date_cols: list[int] = field(default_factory=list)
    # ---- 排序（新增客户时按列B 的地址顺序重排整张表）----
    # 本次是否真的执行了「插到第 4 行 + 整表重排」
    sort_enabled: bool = False
    # 排序键辅助列（1-based）；0 = 本次不排序
    sort_key_col: int = 0
    # 排序区域，形如 ``A3:GS142``
    sort_range: str = ""
    # 每个数据行的排序键：{(排序前不能用的) 行号: 键值}
    row_keys: dict[int, int] = field(default_factory=dict)
    # 预测的排序后行号：{(姓名, 电话): 行号}
    final_rows: dict[tuple[str, str], int] = field(default_factory=dict)
    # 实际排序结果与预测不一致（真机 sort_range 行为异常）
    sort_mismatch: bool = False
    # 排序后数据区的最后一行（1-based）
    last_data_row: int = 0
    # 不在地址清单里的地址（预览里提示"会排到表尾"）
    unknown_addresses: list[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        """本次计划是否包含任何变更。"""
        return bool(self.changes)
