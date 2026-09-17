"""云端名单导入主流程：算目标日 -> 日期闸门 -> 读云端 -> 校验 -> 留档。"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Callable

from app.ordering.archive import archive_to_excel
from app.ordering.import_models import DayOrders
from app.ordering.roster import _log, check_target_day, collect_day_orders, ordering_target_date


def prepare_day_orders(config: Any, *, now: _dt.datetime | None = None,
                       delivery_date: _dt.date | None = None,
                       cli: Any | None = None,
                       log: Callable[[str], Any] | None = None) -> DayOrders:
    """下单前的主入口：算目标日 → 日期闸门 → 读云端 → 校验 → 留档。

    ``delivery_date`` 传入时先过日期闸门（不一致直接拒绝，**不写任何文件**）；
    ``cli`` 可注入（测试用）。留档失败只记 ``archive_error`` 与告警，不影响下单。
    """
    now = now or _dt.datetime.now()
    target = ordering_target_date(
        now,
        start_hour=int(getattr(config, "wps_target_hour_start", 20) or 0),
    )
    if delivery_date is not None:
        check_target_day(target, delivery_date)
    _log(log, f"云端当天名单：识别日期 {target.year}-{target.month:02d}-{target.day:02d}"
              f"（{target.month}.{target.day}）")

    meals = collect_day_orders(config, target=target, cli=cli, log=log)
    day = DayOrders(target_date=target, meals=meals,
                    orders_by_sheet={meal.meal: list(meal.orders) for meal in meals})
    for meal in meals:
        if meal.skipped:
            continue
        _log(log, f"{meal.table} {target.month}.{target.day}：标 1 共 {meal.marked_total} 人，"
                  f"其中 {meal.skipped_address} 人地址是「大西/小」不走闪时送，"
                  f"实际下单 {meal.order_count} 人")

    excel_path = getattr(config, "sss_excel_path", None)
    if excel_path:
        try:
            day.archive = archive_to_excel(excel_path, meals, log=log)
            parts = "、".join(
                f"{name} {info['written']} 人"
                + (f"（E1={info['date_text']}）" if info["date_text"] else "（E1 已清空）")
                for name, info in day.archive.items())
            _log(log, f"已留档到《{Path(excel_path).name}》：{parts}")
        except Exception as exc:  # 留档只是留痕，绝不因此阻断下单
            day.archive_error = str(exc)
            _log(log, f"留档写入失败：{exc}（不影响下单，下单用的是云端内存名单）", "WARN")
    else:
        _log(log, "未选择订单 Excel 文件：跳过留档（下单用的是云端内存名单）")
    return day
