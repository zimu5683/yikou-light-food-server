"""WPS「核对并补传」：上传后按意图日志核对云端缺失订单，只补缺失部分。

背景（2026-11-10 用户需求）：上传成功后云端可能因为各种原因「看不到」这批餐，
而本地账本已经把「本批已同步」记为锚点，直接重新预览只会得到「计划不变/无需
写入」——程序没有办法再传一次。整表强制重传又会把已经入账的餐重复加一遍。

本模块只做**只读核对 + 缺失计划**，不写云端、不写账本、不写意图日志：

* 期望值来源是意图日志（``<账本>.journal`` 及其 ``.archive``）里**最近一次上传**
  的 ``intents``（每单/槽位的写前总餐次、写后期望总餐次、日期格期望值、订单号、
  本地餐次）；不重新读本地排班表 —— 本地表可能已经被「订单处理」重写；
* 逐单和云端现状比较，得出「已在云端 / 需补传（缺行/缺日期格/餐次不足）/
  无法判定」；无法判定的**不写**；
* 补传计划用「合成订单 + 合成只读账本」调用现有 planner，语义与普通上传完全
  一致：``增量 = 本地餐次 − 合成锚点``，写前值取云端实际值。因此只补缺失的格子，
  已完整的行一个都不动；
* 真正写入仍由 ``executor.apply_plan`` 完成（意图落盘、写前只读复核、逐格回读
  校验、失败分类、账本锚点提交、排序/插行/格式全部沿用）。

安全原则：宁漏勿重。任何一处对不上（云端被别人改过、找不到唯一行、表结构/目标
日期列变化、日志记录缺失或未结案）都归为「无法判定」或整表阻断，不猜、不写。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from app.wps.common import CELL_MARK, person_key, weekday_number
from app.wps.models import LEDGER_SNAPSHOT_ABSENT, CloudOrder, SheetPlan
from app.wps.planner import build_plan
from app.wps.recovery import classify_journal_sheet

#: 已给出终态、可以拿来当「上次上传期望值」的子表状态。
TERMINAL_SHEET_STATUSES = frozenset({"verified", "failed_no_write", "not_started"})
#: 未结案的子表状态：只读核对照常做，但本次不能补传。
PENDING_SHEET_STATUSES = frozenset({
    "planned", "writing", "ledger_pending", "uncertain", "retired_guarded",
})

#: 单条订单（槽位）的核对结论。
STATUS_PRESENT = "present"            # 云端已是上次上传的期望值
STATUS_MISSING_ROW = "missing_row"    # 整行不在云端（新客户/新增槽位）
STATUS_MISSING_MEALS = "missing_meals"  # 行在，但总餐次少于期望
STATUS_MISSING_MARK = "missing_mark"  # 行与总餐次都对，日期格没写
STATUS_MISSING_FIELDS = "missing_fields"  # 半成品行：类型/餐种/公式等空着
STATUS_MANUAL = "manual"              # 无法判定，本次不写
_WRITE_STATUSES = frozenset({
    STATUS_MISSING_ROW, STATUS_MISSING_MEALS, STATUS_MISSING_MARK,
    STATUS_MISSING_FIELDS,
})

#: 子表整体结论。
SHEET_READY = "ready"          # 有可补传的订单，全部可判定
SHEET_PARTIAL = "partial"      # 有可补传的订单，另有无法判定的订单（不写它们）
SHEET_COMPLETE = "complete"    # 云端与上次上传期望一致，无需补传
SHEET_MANUAL = "manual"        # 有无法判定的订单，且没有可补传的
SHEET_BLOCKED = "blocked"      # 整表不能补传（未结案/目标变化/表结构异常等）
_WRITEABLE_SHEET_STATUSES = frozenset({SHEET_READY, SHEET_PARTIAL})

#: 合成订单没有本地行号，planner 会给出带「？」或星期标记的提示，均与补传无关。
_NOISE_WARNING_PARTS = (
    "本地表里没有星期标记",
    "在本地表里有",
    # 合成订单没有真正的本地行号/锚点，planner 会拿合成值给出这类提示，与补传无关。
    "账本说这一行本批已同步",
)


def _text(value: Any) -> str:
    return str(value or "")


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fingerprint(payload: Any) -> str:
    """稳定 JSON 序列化后取 SHA-256（与预览令牌同口径，但本模块不依赖 api 层）。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def context_fingerprint(context: Mapping[str, Any] | None) -> str:
    """核对令牌绑定的配置/目标快照指纹（供 Bridge 使用）。"""
    return _fingerprint(dict(context or {}))


def _plan_fingerprint(plan: SheetPlan | None) -> str:
    """计划指纹：只覆盖会影响写入结果的字段（与预览令牌口径一致）。"""
    if plan is None:
        return ""
    changes: list[dict[str, Any]] = []
    for change in plan.changes or []:
        changes.append({
            "kind": change.kind,
            "name": change.name,
            "phone": change.phone,
            "row": int(change.row or 0),
            "slot": int(change.slot or 0),
            "delta": int(change.delta or 0),
            "target_col": int(change.target_col or 0),
            "total_before": int(change.total_before or 0),
            "total_after": int(change.total_after or 0),
            "target_ok": bool(change.target_ok),
            "target_occupied": str(change.target_occupied or ""),
            "local_meals": int(change.local_meals or 0),
            "ledger_prev": change.ledger_prev,
            "cloud_row": int(change.cloud_row or 0),
            "cloud_before_rows": [int(row) for row in change.cloud_before_rows or ()],
            "pre_row": int(change.pre_row or 0),
            "insert_row": int(change.insert_row or 0),
            "order_no": str(change.order_no or ""),
            "address": str(change.address or ""),
            "meal_type": str(change.meal_type or ""),
            "meal_kind": str(change.meal_kind or ""),
            "fill_type": bool(change.fill_type),
            "fill_kind": bool(change.fill_kind),
            "fill_formula": bool(change.fill_formula),
        })
    payload = {
        "sheet": plan.sheet,
        "file_id": plan.file_id,
        "target_date": (plan.target_date.isoformat() if plan.target_date else ""),
        "target_col": int(plan.target_col or 0),
        "target_header": str(plan.target_header or ""),
        "weekday_number": int(plan.weekday_number or 0),
        "columns": {str(key): int(value or 0)
                    for key, value in (plan.columns or {}).items()},
        "date_cols": [int(col) for col in plan.date_cols or []],
        "sort_enabled": bool(plan.sort_enabled),
        "sort_key_col": int(plan.sort_key_col or 0),
        "sort_range": str(plan.sort_range or ""),
        "last_data_row": int(plan.last_data_row or 0),
        "append_row": int(plan.append_row or 0),
        "blocked_reason": str(plan.blocked_reason or ""),
        "insert_blocks": [
            {"position": int(block.position or 0), "count": int(block.count or 0),
             "first_row": int(block.first_row or 0),
             "append_only": bool(block.append_only)}
            for block in plan.insert_blocks or []
        ],
        "changes": changes,
    }
    return _fingerprint(payload)


def _clean_warnings(warnings: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for warning in warnings or []:
        text = str(warning or "")
        if not text:
            continue
        if any(part in text for part in _NOISE_WARNING_PARTS):
            continue
        if text not in result:
            result.append(text)
    return result


# ----------------------------------------------------------------------
# 上传记录：意图日志 + 归档
# ----------------------------------------------------------------------
@dataclass
class UploadRecordRef:
    """一条「上次上传」子表记录（来自热 journal 或 ``.archive``）。"""

    operation_id: str
    op_updated_at: str
    op_status: str
    sheet_key: str
    record: dict[str, Any]

    @property
    def sheet(self) -> str:
        return _text(self.record.get("sheet"))

    @property
    def file_id(self) -> str:
        return _text(self.record.get("file_id"))

    @property
    def status(self) -> str:
        return _text(self.record.get("status"))

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_SHEET_STATUSES

    @property
    def identity(self) -> dict[str, Any]:
        return {"operation_id": self.operation_id,
                "sheet_key": self.sheet_key,
                "updated_at": self.op_updated_at,
                "op_status": self.op_status,
                "status": self.status}


def load_journal_operations(journal: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """读取热 journal 的 operations，并尽力合并 ``.archive``；返回 ``(ops, 警告)``。

    归档文件损坏只记警告、不阻断：热日志里的记录仍然可用；**需要的记录只在
    归档里**时，调用方会按「找不到记录」失败关闭。
    """
    warnings: list[str] = []
    merged: dict[str, dict[str, Any]] = {}
    try:
        operations = journal.operations()
    except Exception as exc:  # noqa: BLE001 - 日志不可读由调用方失败关闭
        raise RuntimeError(f"journal_unreadable:{type(exc).__name__}:{exc}") from exc
    if isinstance(operations, Mapping):
        for op_id, op in operations.items():
            if isinstance(op, Mapping):
                merged[str(op_id)] = dict(op)
    path = getattr(journal, "path", None)
    if path is None:
        return merged, warnings
    archive_path = str(path) + ".archive"
    try:
        with open(archive_path, "r", encoding="utf-8") as handle:
            archive = json.load(handle)
    except FileNotFoundError:
        return merged, warnings
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        warnings.append(f"意图日志归档不可读（{type(exc).__name__}），只核对仍在热日志里的记录")
        return merged, warnings
    archived = archive.get("operations") if isinstance(archive, Mapping) else None
    if not isinstance(archived, Mapping):
        warnings.append("意图日志归档结构异常，只核对仍在热日志里的记录")
        return merged, warnings
    for op_id, op in archived.items():
        if isinstance(op, Mapping) and str(op_id) not in merged:
            merged[str(op_id)] = dict(op)
    return merged, warnings


def latest_sheet_records(
        journal: Any, sheets: Sequence[str] | None = None,
) -> tuple[OrderedDict[str, UploadRecordRef],
           OrderedDict[str, UploadRecordRef], list[str]]:
    """找出每张子表**最近一次**上传记录。

    返回 ``(可核对记录, 未结案记录, 警告)``：

    * 每张表取 ``updated_at`` 最新的一条记录；
    * 最新记录是未结案状态时放进「未结案」集合 —— 只读核对照常展示，但本次
      不能补传（同一（目标日期 + 云表）上一次写入还没走到可证明的终点）；
    * 记录表里没有的表不返回（调用方另行提示「没有上传记录」）。
    """
    operations, warnings = load_journal_operations(journal)
    per_sheet: dict[str, list[UploadRecordRef]] = {}
    for op_id, op in operations.items():
        op_updated = _text(op.get("updated_at") or op.get("created_at"))
        op_status = _text(op.get("status"))
        sheet_map = op.get("sheets")
        if not isinstance(sheet_map, Mapping):
            continue
        for sheet_key, record in sheet_map.items():
            if not isinstance(record, Mapping):
                continue
            sheet = _text(record.get("sheet"))
            if not sheet:
                continue
            per_sheet.setdefault(sheet, []).append(UploadRecordRef(
                operation_id=str(op_id), op_updated_at=op_updated,
                op_status=op_status, sheet_key=str(sheet_key),
                record=dict(record)))
    wanted = [str(sheet) for sheet in (sheets or []) if str(sheet or "").strip()]
    if not wanted:
        wanted = sorted(per_sheet)
    terminal: "OrderedDict[str, UploadRecordRef]" = OrderedDict()
    pending: "OrderedDict[str, UploadRecordRef]" = OrderedDict()
    for sheet in wanted:
        candidates = per_sheet.get(sheet) or []
        if not candidates:
            continue
        candidates.sort(key=lambda ref: (ref.op_updated_at, ref.operation_id),
                        reverse=True)
        chosen = candidates[0]
        if chosen.terminal:
            terminal[sheet] = chosen
        else:
            pending[sheet] = chosen
    return terminal, pending, warnings


# ----------------------------------------------------------------------
# 逐单核对
# ----------------------------------------------------------------------
@dataclass
class IntentState:
    """一条订单（槽位）的核对结论。"""

    detail: dict[str, Any]
    status: str = STATUS_PRESENT
    missing: list[str] = field(default_factory=list)
    reason: str = ""
    #: 合成只读账本里这一槽位的「已同步本地餐次」。
    synth_prev: int = 0

    @property
    def kind(self) -> str:
        return _text(self.detail.get("kind"))

    @property
    def slot(self) -> int:
        return _int(self.detail.get("slot"), 0)

    @property
    def row(self) -> int:
        return _int(self.detail.get("row"), 0)

    @property
    def order_no(self) -> str:
        return _text(self.detail.get("order_no")).strip()

    @property
    def name(self) -> str:
        return _text(self.detail.get("name"))

    @property
    def phone(self) -> str:
        return _text(self.detail.get("phone"))

    @property
    def phone_key(self) -> str:
        return _text(self.detail.get("phone_key"))

    @property
    def local_meals(self) -> int:
        return _int(self.detail.get("local_meals"), 0)

    @property
    def expected_total(self) -> int:
        return _int(self.detail.get("total_after"), 0)

    @property
    def before_total(self) -> int:
        return _int(self.detail.get("total_before"), 0)

    @property
    def actual_total(self) -> int | None:
        value = self.detail.get("actual_total")
        return None if value is None else _int(value)

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.phone_key)

    @property
    def manual(self) -> bool:
        return self.status == STATUS_MANUAL

    @property
    def write(self) -> bool:
        return self.status in _WRITE_STATUSES


def _classify_intent(detail: Mapping[str, Any], *,
                     person_rows: Mapping[tuple[str, str], Sequence[int]],
                     columns: Mapping[str, Any] | None = None) -> IntentState:
    """把一条意图的云端实际值判成 present / missing_* / manual。

    ``columns`` 是上次上传记录里的列号（``type`` / ``kind`` 等），用来判断
    「类型/餐种格子空着、planner 还能补写」的情形 —— 只靠意图里的 ``need_type``
    会漏掉协作方把格子清空、或上次上传本来就不需要写类型的半成品行。
    """
    state = IntentState(detail=dict(detail))
    name = _text(detail.get("name"))
    row = _int(detail.get("row"), 0)
    kind = _text(detail.get("kind"))
    expected_total = _int(detail.get("total_after"), 0)
    before_total = _int(detail.get("total_before"), 0)
    local_meals = _int(detail.get("local_meals"), 0)
    actual_total_raw = detail.get("actual_total")
    actual_total = None if actual_total_raw is None else _int(actual_total_raw)
    expected_target = _text(detail.get("target_expected")) or CELL_MARK
    before_target = _text(detail.get("target_before"))
    actual_target = _text(detail.get("actual_target"))
    total_match = bool(detail.get("total_match"))
    before_match = bool(detail.get("before_total_match"))
    target_match = bool(detail.get("target_match"))
    before_target_match = bool(detail.get("before_target_match"))
    need_type = bool(detail.get("need_type"))
    need_kind = bool(detail.get("need_kind"))
    actual_type = _text(detail.get("actual_type")).strip()
    actual_kind = _text(detail.get("actual_kind")).strip()
    want_type = _text(detail.get("meal_type")).strip()
    want_kind = _text(detail.get("meal_kind")).strip()
    columns = columns if isinstance(columns, Mapping) else {}
    type_col = _int(columns.get("type"), 0)
    kind_col = _int(columns.get("kind"), 0)

    def manual(reason: str) -> IntentState:
        state.status = STATUS_MANUAL
        state.missing = []
        state.reason = reason
        return state

    if row <= 0:
        if kind != "new":
            return manual("云端找不到这一单对应的行（可能被协作者删除），不自动重建")
        key = (name, _text(detail.get("phone_key")))
        if person_rows.get(key):
            return manual("这一单应新增整行，但云端同名客户已有其它行且定位不到本行，"
                          "不自动插入")
        state.status = STATUS_MISSING_ROW
        state.missing = ["row"]
        state.synth_prev = 0
        return state

    if not total_match:
        if expected_total <= before_total:
            return manual(
                f"云端总餐次 {actual_total}，期望值 {expected_total} 不高于写前值 "
                f"{before_total}（退单类），不自动补")
        if not before_match:
            return manual(
                f"云端总餐次 {actual_total}，既不是写前值 {before_total} 也不是期望值 "
                f"{expected_total}（可能已被别人改过），不自动补")
        if not (target_match or before_target_match):
            return manual(
                f"云端日期格是「{actual_target}」，既不是写前的「{before_target}」"
                f"也不是期望的「{expected_target}」，不自动改")
        if need_type and actual_type and not bool(detail.get("type_match")):
            return manual(f"云端类型是「{actual_type}」，与期望的「{want_type}」不一致，"
                          f"不自动覆盖")
        if need_kind and actual_kind and not bool(detail.get("kind_match")):
            return manual(f"云端餐种是「{actual_kind}」，与期望的「{want_kind}」不一致，"
                          f"不自动覆盖")
        if not bool(detail.get("address_match")):
            return manual("云端地址与期望不一致，不自动覆盖")
        synth_prev = local_meals - (expected_total - before_total)
        if synth_prev < 0 or synth_prev > local_meals:
            return manual("按日志推算的账本锚点不合理，不自动补")
        state.status = STATUS_MISSING_MEALS
        state.missing = ["meals"]
        if not target_match:
            state.missing.append("mark")
        state.synth_prev = synth_prev
        return state

    # 总餐次已等于期望值。
    if not target_match:
        if before_target_match:
            state.status = STATUS_MISSING_MARK
            state.missing = ["mark"]
            state.synth_prev = local_meals
            return state
        return manual(
            f"云端日期格是「{actual_target}」，既不是写前的「{before_target}」"
            f"也不是期望的「{expected_target}」，不自动改")
    if need_type and actual_type and not bool(detail.get("type_match")):
        return manual(f"云端类型是「{actual_type}」，与期望的「{want_type}」不一致，"
                      f"不自动覆盖")
    if need_kind and actual_kind and not bool(detail.get("kind_match")):
        return manual(f"云端餐种是「{actual_kind}」，与期望的「{want_kind}」不一致，"
                      f"不自动覆盖")
    if not bool(detail.get("address_match")):
        return manual("云端地址与期望不一致，不自动覆盖")
    # 总餐次与日期格都已对，但行内还有**空着的**格子（类型 / 餐种 / 已出餐与
    # 剩余公式）—— 这是上次写入中断留下的半成品行，planner 能按空单元格补写。
    # 非空且与期望不一致的格子不算「空着」：那是协作者/别人改过的值，一律不覆盖。
    half_row_fields: list[str] = []
    if type_col and want_type and not actual_type:
        half_row_fields.append("type")
    if kind_col and want_kind and not actual_kind:
        half_row_fields.append("kind")
    if bool(detail.get("formula_needed")) and not bool(detail.get("formula_ok")):
        served_actual = _text(detail.get("actual_served_formula"))
        left_actual = _text(detail.get("actual_left_formula"))
        if not served_actual and not left_actual:
            half_row_fields.append("formula")
        else:
            return manual("云端已出餐/剩余公式与期望不一致（不是空格），不自动覆盖")
    if half_row_fields:
        state.status = STATUS_MISSING_FIELDS
        state.missing = ["fields"]
        state.synth_prev = local_meals
        return state
    state.status = STATUS_PRESENT
    state.synth_prev = local_meals
    return state


class _ReplayLedger:
    """给 planner 用的**只读**合成账本：把逐单结论编码成「已同步本地餐次」。

    绝不落盘：``path`` 故意为空串，planner 只会把 plan.ledger_digest 记成
    ``None``，调用方随后用真实账本的磁盘摘要覆盖（跨进程 stale 校验用）。
    """

    path = ""

    def __init__(self, entries: Mapping[tuple[str, str], Mapping[str, Any]]) -> None:
        self._entries = dict(entries)

    def batch_summary(self, date_key: str, file_id: str) -> dict[str, Any] | None:
        return None

    def _entry(self, name: str, phone: str) -> Mapping[str, Any] | None:
        return self._entries.get((str(name), str(phone)))

    def synced_slots(self, date_key: str, file_id: str, name: str,
                     phone: str) -> list[int] | None:
        entry = self._entry(name, phone)
        return None if entry is None else [int(v) for v in entry.get("slots") or []]

    def synced_orders(self, date_key: str, file_id: str, name: str,
                      phone: str) -> list[str] | None:
        entry = self._entry(name, phone)
        return None if entry is None else [str(v or "") for v in entry.get("orders") or []]

    def synced_positions(self, date_key: str, file_id: str, name: str,
                         phone: str) -> list[int] | None:
        entry = self._entry(name, phone)
        return None if entry is None else [int(v) for v in entry.get("positions") or []]


def _parse_person_rows(details: Mapping[str, Any]
                       ) -> dict[tuple[str, str], list[int]]:
    result: dict[tuple[str, str], list[int]] = {}
    for item in details.get("person_rows") or []:
        if not isinstance(item, Mapping):
            continue
        key = (_text(item.get("name")), _text(item.get("phone_key")))
        rows = [_int(row) for row in item.get("rows") or [] if _int(row) > 0]
        if key[0]:
            result[key] = sorted(rows)
    return result


def _run_date_for_weekday(marker_weekday: int, target: _dt.date) -> _dt.date:
    """找一个运行日，使 ``weekday_number(run_date) == marker_weekday``。"""
    value = int(marker_weekday or 0)
    if 1 <= value <= 7:
        for delta in range(0, 8):
            day = target + _dt.timedelta(days=delta)
            if weekday_number(day) == value:
                return day
    return target


def _source_run_date(record: Mapping[str, Any], target: _dt.date) -> _dt.date:
    """上次上传的运行日：优先用记录里的通讯记号数字，其次按目标日期。"""
    return _run_date_for_weekday(_int(record.get("weekday_number"), 0), target)


def _build_replay_orders(states: Sequence[IntentState]) -> list[CloudOrder]:
    orders: list[CloudOrder] = []
    for state in sorted(states, key=lambda item: item.slot):
        detail = state.detail
        orders.append(CloudOrder(
            sheet=_text(detail.get("sheet")),
            name=state.name,
            address=_text(detail.get("address")).strip(),
            phone=_text(detail.get("phone")),
            meal_type=_text(detail.get("meal_type")),
            meal_kind=_text(detail.get("meal_kind")),
            meals=max(0, state.local_meals),
            row=0,
            order_no=state.order_no,
            rows=(),
            weekday_marks=(),
        ))
    return orders


def _build_replay_ledger(states: Sequence[IntentState],
                         person_rows: Mapping[tuple[str, str], Sequence[int]]
                         ) -> _ReplayLedger:
    """按逐单结论合成 planner 用的只读账本。

    * present / 只缺日期格 → 合成锚点 = 本地餐次（增量 0）；
    * 总餐次不足 → 合成锚点 = 本地餐次 −（期望 − 写前），使 planner 算出的增量
      恰好等于「期望 − 实际」；
    * 缺整行 → 合成锚点 = 0（新槽位全额补）；
    * 整人完全不在云端 → 不记账本条目，planner 按首次同步处理。
    """
    grouped: "OrderedDict[tuple[str, str], list[IntentState]]" = OrderedDict()
    for state in states:
        grouped.setdefault(state.key, []).append(state)
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for key, items in grouped.items():
        cloud_rows = list(person_rows.get(key) or [])
        if not cloud_rows:
            # 整人完全不在云端：当作首次同步，不塞合成锚点（否则 planner 会
            # 报「账本说已同步 0 餐但云端没这一行」这类噪声）。
            continue
        max_slot = max((item.slot for item in items), default=0)
        if max_slot <= 0:
            continue
        slots = [0] * max_slot
        orders = [""] * max_slot
        positions = [0] * max_slot
        for item in items:
            index = item.slot - 1
            if index < 0 or index >= max_slot:
                continue
            slots[index] = int(item.synth_prev)
            orders[index] = item.order_no
            if item.row > 0 and item.row in cloud_rows:
                positions[index] = cloud_rows.index(item.row) + 1
            else:
                # 缺整行：给一个超出范围的 position，planner 会算成 row=0
                # （新行），不会误套到同一个人别的物理行上。
                positions[index] = len(cloud_rows) + 1
        entries[key] = {"slots": slots, "orders": orders, "positions": positions}
    return _ReplayLedger(entries)


@dataclass
class SheetCheck:
    """一张子表的核对结论（含内部补传计划，不直接序列化）。"""

    sheet: str
    file_id: str
    target_date: _dt.date | None
    status: str
    reason: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    orders: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    record: UploadRecordRef | None = None
    details: dict[str, Any] = field(default_factory=dict)
    plan: SheetPlan | None = None
    plan_fingerprint: str = ""
    manual_people: list[str] = field(default_factory=list)
    marker_enabled: bool = False
    #: 未结案（只读展示、不能补传）
    pending: bool = False

    @property
    def writeable(self) -> bool:
        return (self.plan is not None
                and self.status in _WRITEABLE_SHEET_STATUSES)

    @property
    def to_resend(self) -> int:
        return _int(self.counts.get("to_resend"), 0)

    def record_identity(self) -> dict[str, Any]:
        return {} if self.record is None else self.record.identity

    def public(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "file_id": self.file_id,
            "target_date": (self.target_date.isoformat()
                            if self.target_date else ""),
            "status": self.status,
            "reason": self.reason,
            "pending": bool(self.pending),
            "counts": dict(self.counts),
            "orders": [dict(order) for order in self.orders],
            "warnings": list(self.warnings),
            "target_col": _int(self.details.get("target_col"), 0),
            "target_header": _text(self.details.get("target_header")),
            "last_upload": self.record_identity(),
            "plan_fingerprint": self.plan_fingerprint,
        }


def _intent_payload(state: IntentState) -> dict[str, Any]:
    detail = state.detail
    missing_meals = 0
    if state.status == STATUS_MISSING_MEALS:
        missing_meals = max(0, state.expected_total - _int(state.actual_total, 0))
    return {
        "order_no": state.order_no,
        "name": state.name,
        "phone": state.phone,
        "slot": state.slot,
        "kind": state.kind,
        "local_meals": state.local_meals,
        "status": state.status,
        "missing": list(state.missing),
        "missing_meals": missing_meals,
        "expected_total": state.expected_total,
        "before_total": state.before_total,
        "actual_total": state.actual_total,
        "expected_mark": _text(detail.get("target_expected")) or CELL_MARK,
        "actual_mark": _text(detail.get("actual_target")),
        "actual_row": state.row,
        "reason": state.reason,
    }


def _validate_plan_against_states(
        plan: SheetPlan,
        plan_states: Sequence[IntentState],
) -> tuple[bool, str, dict[tuple[tuple[str, str], int], IntentState]]:
    """用 planner 生成的变更反推每条订单的最终结论；任何不一致都整表阻断。

    ``plan_states`` 是**纳入计划的完整人**的全部槽位（含本来已完整的槽位），
    这样 planner 的槽位序号不会因为漏掉前面的槽位而错位。
    """
    changes: dict[tuple[tuple[str, str], int], Any] = {}
    for change in plan.changes or []:
        key = (person_key(change.name, change.phone), int(change.slot or 0))
        changes[key] = change
    final: dict[tuple[tuple[str, str], int], IntentState] = {}
    for state in plan_states:
        key = (state.key, state.slot)
        change = changes.get(key)
        if change is None:
            return False, f"内部核对不一致：计划里缺少「{state.name}」槽位 {state.slot}", {}
        if int(change.total_after) != state.expected_total:
            return False, (f"内部核对不一致：「{state.name}」计划总餐次 "
                           f"{change.total_after} ≠ 期望 {state.expected_total}"), {}
        if int(change.total_before) != _int(state.actual_total, 0):
            return False, (f"内部核对不一致：「{state.name}」计划写前总餐次 "
                           f"{change.total_before} ≠ 云端 {state.actual_total}"), {}
        derived = IntentState(detail=state.detail, synth_prev=state.synth_prev)
        if change.kind == "new":
            derived.status = STATUS_MISSING_ROW
            derived.missing = ["row"]
        elif not change.needs_write:
            derived.status = STATUS_PRESENT
            derived.missing = []
        else:
            missing: list[str] = []
            if int(change.total_after) != int(change.total_before):
                missing.append("meals")
            if (change.target_col and not change.target_ok
                    and not change.target_blocked):
                missing.append("mark")
            if change.fill_type or change.fill_kind or change.fill_formula:
                missing.append("fields")
            if not missing:
                return False, (f"内部核对不一致：「{state.name}」计划需要写入但"
                               f"看不出缺什么"), {}
            derived.status = (STATUS_MISSING_MEALS if "meals" in missing
                              else (STATUS_MISSING_MARK if "mark" in missing
                                    else STATUS_MISSING_FIELDS))
            # 一个槽位可能同时缺多项；状态取"最重的"那一项，缺项列表全保留。
            derived.missing = missing
        final[key] = derived
    # 计划里不应该出现本次没有纳入补传的人
    allowed_keys = {state.key for state in plan_states}
    for key in changes:
        if key[0] not in allowed_keys:
            return False, (f"内部核对不一致：计划包含了未列入补传的客户"
                           f"「{key[0][0]}」"), {}
    return True, "", final


def check_sheet(cli: Any, ref: UploadRecordRef, *,
                effective_tables: Mapping[str, Any],
                config: Any,
                ledger: Any,
                log: Callable[[str, str], Any] | None = None,
                ) -> SheetCheck:
    """只读核对一张子表；不写云端/账本/日志。"""
    emit = log or (lambda *_args, **_kwargs: None)
    record = ref.record
    sheet = ref.sheet
    file_id = ref.file_id
    check = SheetCheck(sheet=sheet, file_id=file_id, target_date=None,
                       status=SHEET_BLOCKED, record=ref)
    try:
        target = _dt.date.fromisoformat(_text(record.get("target_date")))
    except (TypeError, ValueError):
        check.reason = "上传记录缺少合法的目标日期，无法核对"
        return check
    check.target_date = target
    conf = effective_tables.get(sheet) if isinstance(effective_tables, Mapping) else None
    if not isinstance(conf, Mapping) or not _text(conf.get("file_id")):
        check.reason = "这张表已不在当前写入目标里（配置已变化），请重新预览"
        return check
    current_file = _text(conf.get("file_id"))
    if current_file != file_id:
        check.reason = ("目标云文档已变化（测试/正式切换或换了文件），"
                        "补传可能写到别的表，已拒绝；请重新预览")
        return check
    check.marker_enabled = bool(record.get("marker_enabled"))

    # 只读核对云端（含逐单明细）。
    try:
        classified = classify_journal_sheet(cli, record, with_details=True)
    except Exception as exc:  # noqa: BLE001 - 核对失败一律不写
        emit(f"[云同步补传] {sheet}：只读核对失败：{exc}", "ERROR")
        check.reason = f"只读核对失败：{type(exc).__name__}: {exc}"
        return check
    details = classified.get("details")
    if not isinstance(details, Mapping):
        problems = classified.get("problems") or []
        reason = _text(problems[0]) if problems else _text(classified.get("reason"))
        check.reason = (f"云端状态无法核对（{classified.get('state') or 'unknown'}）"
                        + (f"：{reason}" if reason else "")
                        + "。请重新预览后再决定是否上传")
        return check
    check.details = dict(details)
    intent_details = [item for item in (details.get("intents") or [])
                      if isinstance(item, Mapping)]
    if not intent_details:
        check.reason = "上次上传记录里没有订单明细，无法核对（请重新预览上传）"
        return check
    person_rows = _parse_person_rows(details)
    columns = details.get("columns") if isinstance(details, Mapping) else {}

    states = [_classify_intent(item, person_rows=person_rows, columns=columns)
              for item in intent_details]
    # 同一个人只要有一条无法判定，整人跳过（不写他的任何一行）。
    manual_keys = {state.key for state in states if state.manual}
    if manual_keys:
        for state in states:
            if state.key in manual_keys and not state.manual:
                # 这些订单本身可能没缺，但同一人的其它订单无法判定：不参与补传。
                state.status = STATUS_MANUAL
                state.reason = "同一客户另有订单无法判定，本次整人跳过"
                state.missing = []
        manual_names: list[str] = []
        for state in states:
            if state.key in manual_keys and state.name not in manual_names:
                manual_names.append(state.name)
        check.manual_people = manual_names
    else:
        check.manual_people = []

    eligible_people: "OrderedDict[tuple[str, str], list[IntentState]]" = OrderedDict()
    for state in states:
        if state.key in manual_keys or state.status == STATUS_PRESENT:
            continue
        eligible_people.setdefault(state.key, []).append(state)

    # 槽位连续性：有洞时必须有订单号身份，否则 planner 只能按行号硬套。
    for key, items in list(eligible_people.items()):
        max_slot = max(item.slot for item in items)
        slots = {item.slot for item in items}
        if slots != set(range(1, max_slot + 1)):
            if any(not item.order_no for item in items):
                manual_keys.add(key)
                for state in states:
                    if state.key == key and state.status != STATUS_PRESENT:
                        state.status = STATUS_MANUAL
                        state.reason = ("订单槽位不连续且缺少订单号，无法安全配对，"
                                        "本次整人跳过")
                        state.missing = []
                eligible_people.pop(key, None)
    if manual_keys and not check.manual_people:
        check.manual_people = sorted({state.name for state in states
                                      if state.key in manual_keys})

    # 未结案：只读展示，不生成补传计划。
    if ref.status in PENDING_SHEET_STATUSES or not ref.terminal:
        check.pending = True
        check.status = SHEET_BLOCKED
        check.reason = (f"上次上传仍未结案（状态 {ref.status or 'unknown'}），"
                        f"只读核对可以看云端实际值，但本次不能补传；"
                        f"该表仍被防重复闸门保护")
        check.orders = [_intent_payload(state) for state in states]
        check.counts = _count_orders(check.orders)
        return check

    if not eligible_people:
        check.orders = [_intent_payload(state) for state in states]
        check.counts = _count_orders(check.orders)
        check.status = SHEET_MANUAL if manual_keys else SHEET_COMPLETE
        if check.status == SHEET_MANUAL:
            check.reason = "有无法判定的订单，且没有可以安全补传的订单"
        return check

    # 生成合成订单与只读账本，调用现有 planner。注意必须把这些人的**全部槽位**
    # 都带上（包括本来已完整、本次不用写的槽位），否则 planner 的槽位序号会前移，
    # 增量可能写到同一个人别的行上。
    #
    # 另外：本次需要**新增行 + 地址排序**时，排序会改变整张表的物理行序。补传计划
    # 只含部分人时，其它人的账本 positions 会过时（下一批增量可能配错行），所以
    # 这种情况下把同表所有「可判定的人」都纳入计划（他们增量为 0，只是让执行器
    # 回填实际行号后一起补记账本位置）。
    sort_enabled = bool(getattr(config, "wps_sort_enabled", True))
    needs_insert = any(state.status == STATUS_MISSING_ROW
                       for items in eligible_people.values() for state in items)
    if sort_enabled and needs_insert:
        plan_states: list[IntentState] = [state for state in states
                                          if state.key not in manual_keys]
    else:
        eligible_keys = set(eligible_people)
        plan_states = [state for state in states if state.key in eligible_keys]
    orders = _build_replay_orders(plan_states)
    replay_ledger = _build_replay_ledger(plan_states, person_rows)
    try:
        plans = build_plan(
            cli,
            local_orders={sheet: orders},
            tables={sheet: {"file_id": file_id,
                            "drive_id": _text(conf.get("drive_id"))}},
            target=target,
            ledger=replay_ledger,
            marker_enabled=check.marker_enabled,
            run_date=_source_run_date(record, target),
            address_order=getattr(config, "wps_address_order", None),
            sort_enabled=bool(getattr(config, "wps_sort_enabled", True)),
            log=emit,
        )
    except Exception as exc:  # noqa: BLE001 - 计划失败一律不写
        emit(f"[云同步补传] {sheet}：生成补传计划失败：{exc}", "ERROR")
        check.reason = f"生成补传计划失败：{type(exc).__name__}: {exc}"
        check.status = SHEET_BLOCKED
        return check
    plan = plans[0] if plans else None
    if plan is None:
        check.status = SHEET_BLOCKED
        check.reason = "内部核对失败：没有生成补传计划"
        return check
    if plan.blocked_reason:
        check.status = SHEET_BLOCKED
        check.reason = plan.blocked_reason
        return check
    if not plan.target_col:
        check.status = SHEET_BLOCKED
        check.reason = ("云端表里没有目标日期列，无法补传（通常是协作者还没加"
                        "当天的列），请重新预览核对")
        return check
    # 真实账本摘要：apply_plan 在跨进程锁内要拿它做 stale 校验。
    plan.ledger_digest = (getattr(ledger, "_loaded_digest", None)
                          or LEDGER_SNAPSHOT_ABSENT)
    marker_weekday = _int(record.get("weekday_number"), 0)
    plan.weekday_number = (marker_weekday if 1 <= marker_weekday <= 7
                           else weekday_number(target))
    plan_warnings = _clean_warnings(list(plan.warnings or []))

    ok, reason, final = _validate_plan_against_states(plan, plan_states)
    if not ok:
        check.status = SHEET_BLOCKED
        check.reason = reason
        return check
    # 用最终结论刷新每条订单的 payload（需补传的人以计划为准）。
    for state in states:
        key = (state.key, state.slot)
        derived = final.get(key)
        if derived is not None:
            state.status = derived.status
            state.missing = derived.missing
            state.reason = derived.reason
    check.orders = [_intent_payload(state) for state in states]
    check.counts = _count_orders(check.orders)

    if manual_keys and (plan.insert_blocks or plan.sort_enabled):
        check.status = SHEET_BLOCKED
        check.reason = ("本次补传需要新增行并重排整表，但同一张表里有订单无法判定；"
                        "为避免影响协作者的行，整表停止补传，请重新预览核对")
        return check
    check.plan = plan
    check.plan_fingerprint = _plan_fingerprint(plan)
    check.warnings = plan_warnings
    check.status = SHEET_PARTIAL if manual_keys else SHEET_READY
    if check.status == SHEET_PARTIAL:
        check.reason = (f"有 {len(check.manual_people)} 人无法判定，本次不写；"
                        f"其余订单只补缺失部分")
    return check


def _count_orders(orders: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"total": 0, "to_resend": 0, "present": 0, "manual": 0}
    for order in orders:
        counts["total"] += 1
        status = _text(order.get("status"))
        if status in _WRITE_STATUSES:
            counts["to_resend"] += 1
        elif status == STATUS_PRESENT:
            counts["present"] += 1
        else:
            counts["manual"] += 1
    return counts


# ----------------------------------------------------------------------
# 批量核对 + 对外结构
# ----------------------------------------------------------------------
def reupload_context(config: Any, effective_tables: Mapping[str, Any]) -> dict[str, Any]:
    """核对令牌绑定的配置/目标快照；任何变化都会让令牌失效。"""
    order = getattr(config, "wps_address_order", None) or {}
    address_order: dict[str, list[str]] = {}
    if isinstance(order, Mapping):
        for sheet, values in order.items():
            if isinstance(values, (list, tuple)):
                address_order[str(sheet)] = [str(item) for item in values]
    tables: dict[str, dict[str, str]] = {}
    for sheet, conf in (effective_tables or {}).items():
        if isinstance(conf, Mapping):
            tables[str(sheet)] = {
                "file_id": _text(conf.get("file_id")),
                "drive_id": _text(conf.get("drive_id")),
            }
        else:
            tables[str(sheet)] = {"file_id": _text(conf), "drive_id": ""}
    return {
        "wps_enabled": bool(getattr(config, "wps_enabled", False)),
        "test_mode": bool(getattr(config, "wps_test_mode", False)),
        "sort_enabled": bool(getattr(config, "wps_sort_enabled", True)),
        "marker_enabled": bool(getattr(config, "wps_marker_enabled", True)),
        "address_order": address_order,
        "tables": tables,
    }


def check_sheets(cli: Any, *,
                 records: Mapping[str, UploadRecordRef],
                 pending_records: Mapping[str, UploadRecordRef],
                 sheets: Sequence[str],
                 effective_tables: Mapping[str, Any],
                 config: Any,
                 ledger: Any,
                 log: Callable[[str, str], Any] | None = None,
                 ) -> tuple[list[SheetCheck], dict[str, Any]]:
    """按选中的子表逐张核对；返回 ``(结果, 上下文)``。"""
    context = reupload_context(config, effective_tables)
    checks: list[SheetCheck] = []
    for sheet in sheets:
        ref = records.get(sheet) or pending_records.get(sheet)
        if ref is None:
            checks.append(SheetCheck(
                sheet=sheet, file_id="", target_date=None,
                status=SHEET_BLOCKED,
                reason="没有找到这张表的上传记录（日志可能已归档或这是新表），"
                       "请用普通预览上传"))
            continue
        checks.append(check_sheet(
            cli, ref, effective_tables=effective_tables, config=config,
            ledger=ledger, log=log))
    return checks, context


def check_fingerprint(checks: Sequence[SheetCheck],
                      context: Mapping[str, Any]) -> str:
    """核对指纹：配置 + 上次记录身份 + 计划 + 逐单云端实际值。

    只读核对与真正写入之间云端/日志任何变化都会改变这个指纹，调用方据此让
    令牌失效并要求重新核对（与预览令牌同一套两段式语义）。
    """
    payload = {
        "context": context,
        "sheets": [
            {
                "sheet": check.sheet,
                "file_id": check.file_id,
                "target_date": (check.target_date.isoformat()
                                if check.target_date else ""),
                "status": check.status,
                "pending": bool(check.pending),
                "record": check.record_identity(),
                "plan": check.plan_fingerprint,
                "orders": [
                    [order.get("slot"), order.get("order_no"), order.get("status"),
                     order.get("missing"), order.get("expected_total"),
                     order.get("actual_total"), order.get("actual_row"),
                     order.get("expected_mark"), order.get("actual_mark")]
                    for order in check.orders
                ],
            }
            for check in checks
        ],
    }
    return _fingerprint(payload)


def summarize_checks(checks: Sequence[SheetCheck]) -> dict[str, int]:
    summary = {"sheets": len(checks), "writeable_sheets": 0,
               "to_resend": 0, "present": 0, "manual": 0,
               "complete_sheets": 0, "blocked_sheets": 0}
    for check in checks:
        summary["to_resend"] += check.to_resend
        summary["present"] += _int(check.counts.get("present"), 0)
        summary["manual"] += _int(check.counts.get("manual"), 0)
        if check.writeable:
            summary["writeable_sheets"] += 1
        elif check.status == SHEET_COMPLETE:
            summary["complete_sheets"] += 1
        elif check.status in (SHEET_BLOCKED, SHEET_MANUAL):
            summary["blocked_sheets"] += 1
    return summary


def build_check_payload(checks: Sequence[SheetCheck]) -> dict[str, Any]:
    """把核对结果转成前端可直接渲染的结构（不含内部 plan）。"""
    summary = summarize_checks(checks)
    public_sheets = [check.public() for check in checks]
    warnings: list[str] = []
    for check in checks:
        if check.status in (SHEET_BLOCKED, SHEET_MANUAL) and check.reason:
            warnings.append(f"{check.sheet}：{check.reason}")
        for warning in check.warnings:
            warnings.append(f"{check.sheet}：{warning}")
    if summary["writeable_sheets"]:
        status = "check_ready"
        next_action = "wps_reupload_upload(check_id)"
    elif checks and all(check.status == SHEET_COMPLETE for check in checks):
        status = "noop"
        next_action = ""
    else:
        status = "blocked"
        next_action = ""
    target_date = ""
    dates = {check.target_date for check in checks if check.target_date}
    if len(dates) == 1:
        target_date = next(iter(dates)).isoformat()
    return {
        "summary": summary,
        "tables": public_sheets,
        "warnings": warnings,
        "target_date": target_date,
        "status": status,
        "next_action": next_action,
    }
