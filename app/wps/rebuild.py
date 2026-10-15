"""WPS 账本锚点的半自动重建：账本丢失/损坏之后唯一的出路。

背景（见 ``design/WPS-账本丢失与损坏的兜底.md``）：账本是**唯一**记录「哪一批
已经落进云端」的地方。云端只存总餐次与日期格，不存「哪一批加了多少」，所以锚点
一丢，增量从任何地方都推算不出来 —— 猜 0 就重复加餐，猜「已同步」就静默漏加。

本模块只做一件事：拿一份**按空账本建出来的计划**（``build_plan(..., ledger=None)``），
按云端现状逐行推断「这一批是不是已经入账」，产出可以写进账本的锚点。方向永远
**宁漏勿重**：漏加可以人工补，重复加要按云端日期格往回减。

三条判定，逐人逐槽位：

1. 云端**没有**这个人 → 留空。必须留空：标成「已同步」的话，这一批的新客户
   永远不会被创建出来；
2. 云端有这个人，且**目标日期格已经有值**（我们自己写的 ``1``，或协作者写的
   ``0`` 等）→ 视为已入账，锚点 = 本地餐次（下一批增量 0，一个格子都不写）；
3. 云端有这个人，但目标日期格是**空的** → 留空（按未同步处理，下一批会正常加）。

本模块**不写云端**；落盘由 :func:`app.wps.ledger.rebuild_anchors` 在数据锁内完成
（并先留档原件）。整表被拒绝写入的表（批次日期不符）不做重建：那时本地行与目标
日期根本不对应，没有可推断的前提。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.wps.executor import _change_key, _ledger_entries, _plan_slot_rows
from app.wps.ledger import SyncLedger, rebuild_anchors
from app.wps.models import Change, SheetPlan

#: 确认写入时用户必须逐字输入的值（与 ``wps_recovery_resolve`` 的 confirm 同源做法）。
REBUILD_CONFIRM_WORD = "rebuild"
#: 人工核对说明的最短长度（写进账本审计）。
REBUILD_MIN_NOTE = 4

#: 判定结果。
MARK_SYNCED = "mark_synced"
LEAVE_EMPTY = "leave_empty"

#: 判定理由（机器码，界面按它出中文）。
REASON_NEW_PERSON = "cloud_person_absent"
REASON_CELL_MARKED = "cloud_cell_marked"
REASON_CELL_EMPTY = "cloud_cell_empty"


@dataclass(frozen=True)
class RebuildRow:
    """一个人的一个槽位（本地一行）的重建判定。"""

    sheet: str
    name: str
    phone: str
    slot: int
    local_meals: int
    cloud_present: bool
    cell_marked: bool
    cell_value: str
    decision: str
    reason: str
    anchor: int
    order_no: str
    row: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "phone": self.phone,
            "slot": self.slot,
            "local_meals": self.local_meals,
            "cloud_present": self.cloud_present,
            "cell_marked": self.cell_marked,
            "cell_value": self.cell_value,
            "decision": self.decision,
            "reason": self.reason,
            "anchor": self.anchor,
            "order_no": self.order_no,
            "row": self.row,
        }


@dataclass
class RebuildSheet:
    """一张表的重建结果；``entries`` 是**要写进账本**的锚点（不对外返回）。"""

    sheet: str
    file_id: str
    target_date: str
    rows: list[RebuildRow] = field(default_factory=list)
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped_reason: str = ""

    @property
    def counts(self) -> dict[str, int]:
        """计数键与 :attr:`RebuildRow.reason` 是**同一套机器码**，界面据此出中文。"""
        counts = {"mark_synced": 0, "leave_empty": 0,
                  REASON_NEW_PERSON: 0, REASON_CELL_MARKED: 0,
                  REASON_CELL_EMPTY: 0,
                  "rows": len(self.rows), "people": len(self.entries)}
        for row in self.rows:
            counts[row.decision] = counts.get(row.decision, 0) + 1
            counts[row.reason] = counts.get(row.reason, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "file_id": self.file_id,
            "target_date": self.target_date,
            "skipped_reason": self.skipped_reason,
            "counts": self.counts,
            "rows": [row.as_dict() for row in self.rows],
        }


@dataclass
class RebuildProposal:
    """一次重建的完整提案：给人看的逐行判定 + 要落盘的锚点。"""

    target_date: str
    sheets: list[RebuildSheet] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        counts = {"mark_synced": 0, "leave_empty": 0,
                  REASON_NEW_PERSON: 0, REASON_CELL_MARKED: 0,
                  REASON_CELL_EMPTY: 0,
                  "rows": 0, "people": 0, "sheets": len(self.sheets),
                  "skipped_sheets": 0}
        for sheet in self.sheets:
            if sheet.skipped_reason:
                counts["skipped_sheets"] += 1
                continue
            for key, value in sheet.counts.items():
                counts[key] = counts.get(key, 0) + value
        return counts

    @property
    def fingerprint(self) -> str:
        """要落盘内容的指纹：确认时重算一遍，不一致就拒绝写入。

        只覆盖 ``entries``（真正写下去的东西）：云端或本地表在"看提案"与"确认"
        之间变了，锚点就会变，指纹随之变化 —— 这正是要拦住的窗口。
        """
        payload = [{"target_date": sheet.target_date, "file_id": sheet.file_id,
                    "entries": sheet.entries}
                   for sheet in self.sheets if sheet.entries]
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_date": self.target_date,
            "fingerprint": self.fingerprint,
            "counts": self.counts,
            "warnings": list(self.warnings),
            "sheets": [sheet.as_dict() for sheet in self.sheets],
        }


def _row_decision(change: Change) -> tuple[str, str]:
    """一条变更（一个人的一个槽位）该判成「已入账」还是「留空」。"""
    if change.kind != "existing":
        # 云端没有这个人：必须留空，否则这一批的新客户永远建不出来。
        return LEAVE_EMPTY, REASON_NEW_PERSON
    if change.target_ok or change.target_blocked:
        # 云端这一格已经有值：要么是我们上一批写进去的 1，要么是协作者写的 0 等。
        # 分不清，按"已经入账"处理 —— 宁漏勿重。
        return MARK_SYNCED, REASON_CELL_MARKED
    return LEAVE_EMPTY, REASON_CELL_EMPTY


def _cell_value(change: Change) -> str:
    if change.target_blocked:
        return str(change.target_occupied)
    return "1" if change.target_ok else ""


def _sheet_entries(plan: SheetPlan,
                   rows: Sequence[RebuildRow]) -> dict[str, dict[str, Any]]:
    """按判定产出账本条目；未判成「已入账」的槽位锚点记 0（下一批会正常加）。

    复用执行器同一套 :func:`app.wps.executor._ledger_entries`：按空账本算出来的
    ``slots`` 正好就是「每个槽位已入账 = 本地餐次」，再按判定把未入账的槽位清零。
    这样重建写下的锚点与真实上传写下的锚点**同一套口径**，不会出现第二套记账。
    """
    marked: dict[str, set[int]] = {}
    for row in rows:
        if row.decision != MARK_SYNCED:
            continue
        marked.setdefault(_row_key(row.name, row.phone), set()).add(int(row.slot))
    if not marked:
        return {}
    entries = _ledger_entries(
        plan.changes, only_keys=set(marked), ledger=None,
        date_key=plan.target_date.isoformat() if plan.target_date else "",
        file_id=plan.file_id, slot_rows=_plan_slot_rows(plan))
    before_total: dict[str, int] = {}
    for change in plan.changes:
        key = _change_key(change)
        if key in marked:
            before_total[key] = before_total.get(key, 0) + int(change.total_before)
    for key, entry in entries.items():
        slots = [int(value) for value in (entry.get("slots") or [])]
        for index, slot in enumerate(range(1, len(slots) + 1)):
            if slot not in marked.get(key, set()):
                slots[index] = 0
        entry["slots"] = slots
        entry["local"] = sum(slots)
        # 重建**不写云端**：这一批结束后的云端总餐次仍然是建计划时读到的值。
        entry["total"] = before_total.get(key, 0)
    return entries


def _row_key(name: str, phone: str) -> str:
    """账本里一个人的键（与 :func:`app.wps.executor._change_key` 完全一致）。"""
    return f"{name}\u0000{phone}"


def build_rebuild(plans: Iterable[SheetPlan]) -> RebuildProposal:
    """从「按空账本建出来的计划」产出重建提案；只读，不写任何东西。"""
    sheets: list[RebuildSheet] = []
    warnings: list[str] = []
    target_date = ""
    for plan in plans:
        if not target_date and plan.target_date:
            target_date = plan.target_date.isoformat()
        sheet = RebuildSheet(
            sheet=plan.sheet, file_id=plan.file_id,
            target_date=plan.target_date.isoformat() if plan.target_date else "")
        if plan.blocked_reason:
            # 整表被拒绝写入（批次日期不符）：本地行与目标日期不对应，没有可推断
            # 的前提，绝不猜。
            sheet.skipped_reason = str(plan.blocked_reason)
            sheets.append(sheet)
            warnings.append(
                f"{plan.sheet}：本次不重建锚点（{plan.blocked_reason}）")
            continue
        for change in plan.changes:
            decision, reason = _row_decision(change)
            sheet.rows.append(RebuildRow(
                sheet=plan.sheet, name=change.name, phone=change.phone,
                slot=int(change.slot), local_meals=int(change.local_meals),
                cloud_present=change.kind == "existing",
                cell_marked=bool(change.target_ok or change.target_blocked),
                cell_value=_cell_value(change), decision=decision, reason=reason,
                anchor=int(change.local_meals) if decision == MARK_SYNCED else 0,
                order_no=str(change.order_no or ""), row=int(change.row or 0)))
        sheet.entries = _sheet_entries(plan, sheet.rows)
        if sheet.rows and not sheet.entries:
            warnings.append(
                f"{plan.sheet}：没有任何一行判定为「已入账」，这张表不会写锚点")
        sheets.append(sheet)
    return RebuildProposal(target_date=target_date, sheets=sheets, warnings=warnings)


def format_rebuild(proposal: RebuildProposal) -> str:
    """把提案渲染成人类可读的多行文本（写日志、界面展开用）。"""
    counts = proposal.counts
    lines = [f"账本重建提案（目标日期 {proposal.target_date or '?'}）："
             f"共 {counts['rows']} 行，判为已入账 {counts['mark_synced']} 行、"
             f"留空 {counts['leave_empty']} 行"]
    for warning in proposal.warnings:
        lines.append(f"⚠ {warning}")
    for sheet in proposal.sheets:
        head = f"【{sheet.sheet}】"
        if sheet.skipped_reason:
            lines.append(f"{head}本次不重建（{sheet.skipped_reason}）")
            continue
        sheet_counts = sheet.counts
        lines.append(f"{head}已入账 {sheet_counts['mark_synced']} 行 / "
                     f"留空 {sheet_counts['leave_empty']} 行")
        for row in sheet.rows:
            slot_note = f"第 {row.slot} 行 " if row.slot > 1 else ""
            if row.decision == MARK_SYNCED:
                lines.append(
                    f"    · {row.name}（{row.phone}）{slot_note}"
                    f"云端这一格已是「{row.cell_value}」→ 记已入账 {row.anchor} 餐"
                    f"（本批不会再加）")
            elif row.reason == REASON_NEW_PERSON:
                lines.append(f"    · {row.name}（{row.phone}）{slot_note}"
                             f"云端还没有这个人 → 留空（下一批会正常新增）")
            else:
                lines.append(f"    · {row.name}（{row.phone}）{slot_note}"
                             f"云端这一格是空的 → 留空（下一批会正常加 "
                             f"{row.local_meals} 餐）")
    return "\n".join(lines)



def verify_rebuild(proposal: RebuildProposal, *, ledger_path: Any) -> bool:
    """回读磁盘账本，证明提案里的锚点确实落盘了（不信内存对象）。

    「写入成功」不能只看 ``_save_unlocked`` 没抛异常：另一进程可能在这之间把账本
    改掉。所以这里**重新读盘**，逐个槽位比对 ``slots``。
    """
    try:
        fresh = SyncLedger(ledger_path)
    except Exception:  # noqa: BLE001 - 读不回来就是没证明，绝不当作成功
        return False
    checked = 0
    for sheet in proposal.sheets:
        for storage_key, entry in sheet.entries.items():
            name, separator, phone = str(storage_key).partition("\u0000")
            if not separator:
                return False
            want = [int(value) for value in (entry.get("slots") or [])]
            if not want:
                continue
            have = fresh.synced_slots(sheet.target_date, sheet.file_id, name, phone)
            if have is None or [int(value) for value in have] != want:
                return False
            checked += 1
    return checked > 0


def write_rebuild(proposal: RebuildProposal, *, ledger_path: Any, note: str = "",
                  actor: str = "local") -> dict[str, Any]:
    """把提案里的锚点写进账本（先留档原件，整批一次落盘）。**不写云端。**"""
    batches = [(sheet.target_date, sheet.file_id, sheet.entries)
               for sheet in proposal.sheets if sheet.entries]
    if not batches:
        return {"changed": False, "people": 0, "slots": 0, "batches": [],
                "replaced_corrupt": False, "backup": "", "rebuilt_at": "",
                "fingerprint": proposal.fingerprint}
    result = rebuild_anchors(ledger_path, batches, note=note,
                             source="cloud_scan", actor=actor)
    result["fingerprint"] = proposal.fingerprint
    return result


__all__ = [
    "LEAVE_EMPTY",
    "MARK_SYNCED",
    "REASON_CELL_EMPTY",
    "REASON_CELL_MARKED",
    "REASON_NEW_PERSON",
    "REBUILD_CONFIRM_WORD",
    "REBUILD_MIN_NOTE",
    "RebuildProposal",
    "RebuildRow",
    "RebuildSheet",
    "build_rebuild",
    "format_rebuild",
    "verify_rebuild",
    "write_rebuild",
]
