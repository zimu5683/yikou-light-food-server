"""WPS 云文档同步：把本地排单表的内容增量写入云端排单表。

设计要点（均来自 2026-09 的真机验证，详见 design/WPS-CLOUD-SYNC-PLAN.md）：

- 通过金山官方 CLI ``kdocs-cli`` 访问云文档，**单元格级读写**，不做整表覆盖，
  因此不会破坏协作者维护的公式、自定义排序、字体与列宽。
- 云端表头不是固定列号（6 张表的目标日期列分别在第 6/88/113/87/110/84 列），
  因此一律**按内容定位**：日期列只比对「月.日」，忽略星期文字（协作者写错过星期）。
- 按【名字 + 电话】双重匹配客户；已存在则**累加**总餐次（云端现值 + 本次增量），
  不存在则追加新行。
- 同一个人本地有几行（一晚两单 / 一单两份），**云端就写几行**、各标当天 1：
  本地第 i 行 ↔ 云端这个人的第 i 行（槽位），这天就送几餐。
- 用本地账本按（人, 槽位）记录"本批已同步的本地餐次"，使重复运行零副作用、
  加餐只补差额。
- 协作者写在日期格里的非 1 值（例如 0 = 当天不送）只读不写；本地表的批次日期
  （周一~周日标记）与目标日期不符时整张表拒绝写入。
- 任何失败都只抛异常/写日志，绝不阻塞调用方的本地排单任务。

依赖：仅标准库 + openpyxl（读取本地 xlsx）+ kdocs-cli 可执行文件。
"""
from __future__ import annotations

from app.wps.common import (
    ADDRESS_ALIASES,
    CELL_MARK,
    DATE_RE,
    FILL_GOLD,
    FILL_LUXURY,
    FIRST_DATA_ROW,
    FONT_SIZE_TO_TWIP,
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
    MARKER_OFFSET,
    MARKER_VALUES,
    MAX_READ_CELLS,
    MAX_SCAN_COL,
    MAX_SCAN_ROW,
    MAX_SORT_COL,
    RATE_LIMIT_CODES,
    SORT_FLAG_BLANK,
    SORT_FLAG_EXISTING,
    SORT_FLAG_NEW,
    SORT_KEY_WIDTH,
    SORT_PROBE_WIDTH,
    STRUCT_COLUMN_KEYS,
    TITLE_ROW,
    WPS_PLAN_WORKERS,
    WRITE_BATCH_CELLS,
    _ALIGN_H,
    _ALIGN_V,
    _NATURAL_CHUNK,
    _TRANSIENT_HINTS,
    _address_key,
    _argb_to_int,
    _as_int,
    _find_column,
    _is_transient,
    build_address_ranks,
    canonical_address,
    column_name,
    content_last_col,
    date_headers,
    date_region,
    effective_last_col,
    find_marker_column,
    find_target_column,
    format_sort_key,
    probe_sort_area,
    natural_key,
    normalize_phone,
    parse_date_header,
    person_key,
    scan_bounds,
    sort_key_column,
    sort_key_value,
    target_date_for,
    weekday_number,
)
from app.wps.errors import WpsCloudError
from app.wps.models import Change, CloudOrder, InsertBlock, SheetPlan
from app.wps.ledger import SyncLedger, default_state_path
from app.wps.reader import LOCAL_COL, LOCAL_SHEETS, read_local_orders
from app.wps.planner import (
    _build_sheet_plan,
    build_plan,
    format_plan,
    formula_cells_for_new_rows,
    summarize_plan,
)
from app.wps.executor import (
    _rollback_inserts,
    apply_plan,
    build_format_ops,
    learn_row_format,
    read_person_rows,
)
from app.wps.cli import (
    CLI_NAME,
    CLI_NAME_WIN,
    KdocsCli,
    effective_tables,
    find_cli,
    termux_cli_runtime,
)



# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------


# 云端表里可能出现的工作列表头（不同表写法不同，统一按名字找列）

# 目标日期格与通讯记号写入的值
# 数据从第 3 行开始（第 1 行标题、第 2 行表头）
# 通讯记号默认偏移：备注列右边第 3 列。
# （备注+2 实测被协作者的「9.14 周一」日期标记占用，兜底位置必须让开）
# 通讯记号的合法数字（周几：周日=1 … 周六=7）。用于在表头行里认出记号位。
# 结构列（除姓名/地址/电话/日期以外的固定列）。
# **日期列只允许出现在「电话列」与「第一个结构列」之间** —— 备注右侧是协作者
# 写日期标记的区域（实测 6 张表都在备注+2），绝不能当成日期列：
# 否则新行的「已出餐」公式会把它统计进去，甚至把目标日期的 1 写进协作者的格子。
# 排序辅助列的列号上限：真实排单表最宽也就 200 多列，异常宽说明 used range 有问题，
# 此时宁可不排序，也不要对几万列的区域发排序请求。
# 排序键零填充宽度：接口可能把数字按文本比较（"10" < "2"），补零后字典序即数值序。
# 同一个地址组内：已有行在前（+0），本次新增行在后（+1），空行垫底（+2）。
# 本地排单路线名 -> 云端地址组的写法（协作者习惯，实测 2026-09-12 目标表）。
# 命中别名时：插入到云端组的末尾，且地址格按云端写法落表。
# 云端单次读取的行/列上限
# 接口单次读取的格数上限（实测 5 万）。扫描区域必须按"行×列"控制，
# 否则会撞 `range 选区过大（N 行 × M 列 = X 格）`。留 10% 余量。

# 单次 update-range-data 允许的最大单元格数（实测上限 100，留余量）。
# 字号 -> twip（1 磅 = 20 twip），接口的 font.dyHeight 用 twip。
# 颜色常量（ARGB 整数，接口用整数传色）
# 接口读回来的对齐是字符串枚举，写回去要整数（alcH/alcV）。

# 金山接口的限流错误码：当日额度用尽 / 短时频繁触发（均次日 08:00 恢复）。
# 金山接口的限流错误码：当日额度用尽 / 短时频繁触发（均次日 08:00 恢复）。

# ``build_plan`` 的并发度：每张子表 3 次只读往返，6 张表串行最多 18 次。
# 并发只改变往返的重叠方式，**调用次数与参数完全不变**，不额外消耗每日额度。
#
# 为什么是 2 而不是 6：429002「短时间频繁触发」不在 ``_TRANSIENT_HINTS`` 里，
# ``_run`` 不会重试它，突发触发会让该子表直接报错。取 2 只把瞬时速率翻倍，
# 与 ``bridge.WPS_COPY_CHECK_WORKERS`` 保持同一口径。





# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------









# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
































# ----------------------------------------------------------------------
# 本地排单表读取
# ----------------------------------------------------------------------

# 本地子表列（1-based），与 app/order/templates.py 的排单模板一致




# ----------------------------------------------------------------------
# 账本
# ----------------------------------------------------------------------





# ----------------------------------------------------------------------
# kdocs-cli 调用
# ----------------------------------------------------------------------









# ----------------------------------------------------------------------
# 云端表解析与计划
# ----------------------------------------------------------------------



















# ----------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------







# 传输层瞬时错误的特征词：这些值得重试（业务错误不重试）。

__all__ = [
    "ADDRESS_ALIASES",
    "CELL_MARK",
    "CLI_NAME",
    "CLI_NAME_WIN",
    "Change",
    "CloudOrder",
    "DATE_RE",
    "FILL_GOLD",
    "FILL_LUXURY",
    "FIRST_DATA_ROW",
    "FONT_SIZE_TO_TWIP",
    "HEADER_ADDRESS",
    "HEADER_KIND",
    "HEADER_LEFT",
    "HEADER_NAME",
    "HEADER_PHONE",
    "HEADER_REMARK",
    "HEADER_ROW",
    "HEADER_SERVED",
    "HEADER_TOTAL",
    "HEADER_TYPE",
    "InsertBlock",
    "KdocsCli",
    "LOCAL_COL",
    "LOCAL_SHEETS",
    "MARKER_OFFSET",
    "MARKER_VALUES",
    "MAX_READ_CELLS",
    "MAX_SCAN_COL",
    "MAX_SCAN_ROW",
    "MAX_SORT_COL",
    "RATE_LIMIT_CODES",
    "SORT_FLAG_BLANK",
    "SORT_FLAG_EXISTING",
    "SORT_FLAG_NEW",
    "SORT_KEY_WIDTH",
    "SORT_PROBE_WIDTH",
    "STRUCT_COLUMN_KEYS",
    "SheetPlan",
    "SyncLedger",
    "TITLE_ROW",
    "WPS_PLAN_WORKERS",
    "WRITE_BATCH_CELLS",
    "WpsCloudError",
    "_ALIGN_H",
    "_ALIGN_V",
    "_NATURAL_CHUNK",
    "_TRANSIENT_HINTS",
    "_address_key",
    "_argb_to_int",
    "_as_int",
    "_build_sheet_plan",
    "_find_column",
    "_is_transient",
    "_rollback_inserts",
    "apply_plan",
    "build_address_ranks",
    "build_format_ops",
    "build_plan",
    "canonical_address",
    "column_name",
    "content_last_col",
    "date_headers",
    "date_region",
    "default_state_path",
    "effective_last_col",
    "effective_tables",
    "find_cli",
    "find_marker_column",
    "find_target_column",
    "format_plan",
    "format_sort_key",
    "formula_cells_for_new_rows",
    "learn_row_format",
    "natural_key",
    "normalize_phone",
    "parse_date_header",
    "person_key",
    "probe_sort_area",
    "read_local_orders",
    "read_person_rows",
    "scan_bounds",
    "sort_key_column",
    "sort_key_value",
    "summarize_plan",
    "target_date_for",
    "termux_cli_runtime",
    "weekday_number",
]
