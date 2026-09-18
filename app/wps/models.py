"""WPS 云同步的数据结构（纯数据，无 IO）。"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field


@dataclass
class CloudOrder:
    """本地排单表里的**一行**订单。

    ``meals`` 是这一行的「餐次」（**本次要加的餐**，不是云端总餐次的绝对值）：
    本地排单表每次「订单处理」都会清空六张子表重写，所以它只代表这一批（次日）的餐。

    同一个人在同一子表里可能有多行（一晚两单、一单 x2 份）—— 读取层**不合并**，
    由 ``planner._sum_rows_per_person`` 按"每行算 1 餐"相加（用户 2026-09-18 明确）。
    """

    sheet: str
    name: str
    address: str
    phone: str
    meal_type: str          # 中餐 / 晚餐
    meal_kind: str          # 经济 / 豪华
    meals: int              # 「餐次」列合计
    row: int = 0            # 本地行号（多行时取第一行），便于报错定位
    # ---- 以下字段在末尾追加，避免影响按位置构造 CloudOrder 的老代码 ----
    order_no: str = ""      # 本地「订单」列（W 编号），多行时取第一行
    rows: tuple[int, ...] = ()          # 该人占用的本地行号（多行时全部列出）
    weekday_marks: tuple[str, ...] = ()  # 该人本地行的「周一~周日」标记（批次日期核对用）


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
    # ---- 累加语义（2026-09-18 起）：总餐次 = 云端现值 + 本次增量 ----
    # 本次本地「餐次」（同一人多行时相加，见 planner._sum_rows_per_person）
    local_meals: int = 0
    # 账本里「本批已同步的本地餐次」；None = 账本里没有这个人本日的记录（首次）
    ledger_prev: int | None = None
    # 目标日期格里协作者已经写下的值（非空且不是 1，例如 0 = 当天不送）。
    # 非空时程序**只读不写**这一格 —— 那是协作者的明确决定，不是"还没写"。
    target_occupied: str = ""
    # 这个人在本地表里占用的行号（预览里写"本地第 19 行"，便于人工核对）
    local_rows: tuple[int, ...] = ()
    # 槽位（1-based）：本地第 i 行 ↔ 云端这个人的第 i 行。
    # 同一个人一天下了两单（或一单两份）时会有 slot=2 的变更，两行都标当天 1，
    # 这样「闪时送下单」按"日期格 == 1"出两单、这天真的送两餐。
    slot: int = 1

    @property
    def target_blocked(self) -> bool:
        """目标日期格已被协作者占用（写了 0 等），本次不能覆盖。"""
        return bool(str(self.target_occupied).strip())

    @property
    def needs_write(self) -> bool:
        """这条变更是否真的需要写云端（目标格已是目标状态且无需补格式时为 ``False``）。"""
        return (((not self.target_ok) and not self.target_blocked)
                or self.total_after != self.total_before
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
    # 排序区实际覆盖到的最后一列（1-based）；probe_sort_area 的探测起点
    sort_probe_col: int = 0
    # 每个数据行的排序键：{(排序前不能用的) 行号: 键值}
    row_keys: dict[int, int] = field(default_factory=dict)
    # 预测的排序后行号：{(姓名, 电话): (第 1 行, 第 2 行, ...)}，按槽位顺序
    final_rows: dict[tuple[str, str], tuple[int, ...]] = field(default_factory=dict)
    # 实际排序结果与预测不一致（真机 sort_range 行为异常）
    sort_mismatch: bool = False
    # 排序后数据区的最后一行（1-based）
    last_data_row: int = 0
    # 不在地址清单里的地址（预览里提示"会排到表尾"）
    unknown_addresses: list[str] = field(default_factory=list)
    # 整张表被拒绝写入的原因（非空 = 本次一个格子都不写）。
    # 目前只有一个来源：本地排单表的批次日期与目标日期不符（见 planner）。
    blocked_reason: str = ""
    # 本批（目标日期 + 文件）已同步过的摘要，形如「29 人（2026-09-17T21:27:06）」；
    # 空 = 本批还没同步过（首次上传）
    previous_batch: str = ""

    @property
    def applied(self) -> bool:
        """本次计划是否包含任何变更。"""
        return bool(self.changes)
