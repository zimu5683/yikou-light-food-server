"""账本兜底 P0：备份留档、损坏留档、丢失证据、重建落盘。

只使用合成数据与 ``tmp_path``；**绝不**读写真实用户目录里的账本/日志/云表。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app.wps.errors import LedgerCorruptError
from app.wps.journal import SyncJournal, journal_path_for
from app.wps.ledger import SyncLedger, rebuild_anchors
from app.wps.recovery import ledger_loss_evidence, resolve_pending_operation

TARGET = "2026-09-11"
FILE = "F1"
OTHER = "F2"
SHEET = "东湖中餐"
OP_ID = "wps-0123456789abcdef"


def _key(name: str, phone: str) -> str:
    return f"{name}\u0000{phone}"


def _entry(slots: list[int], *, orders: list[str] | None = None,
           positions: list[int] | None = None, total: int = 0) -> dict:
    return {"slots": list(slots), "orders": orders or [""] * len(slots),
            "positions": positions or list(range(1, len(slots) + 1)),
            "local": sum(slots), "total": total}


def _save(path: Path, index: int) -> str:
    """写一次账本并返回这次写下去的字节（用于比对备份指向哪一版）。"""
    ledger = SyncLedger(path)
    ledger.record(TARGET, FILE, {_key(f"人{index}", "111"): _entry([index + 1])})
    ledger.save()
    return path.read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# 写盘前留档（多代 .bak + 每日快照）
# ----------------------------------------------------------------------

def test_save_rotates_generations_and_keeps_daily_snapshot(tmp_path):
    path = tmp_path / "state.json"
    versions = [_save(path, index) for index in range(4)]

    assert (tmp_path / "state.json.bak").read_text(encoding="utf-8") == versions[2]
    assert (tmp_path / "state.json.bak.1").read_text(encoding="utf-8") == versions[1]
    assert (tmp_path / "state.json.bak.2").read_text(encoding="utf-8") == versions[0]

    # 每日快照 = 当天**第一次**写盘前的那份（这里就是第 1 次写完的内容）
    day = dt.date.today().isoformat()
    snapshot = tmp_path / f"state.json.{day}.bak"
    assert snapshot.read_text(encoding="utf-8") == versions[0]


def test_daily_snapshot_is_written_once_per_day(tmp_path):
    path = tmp_path / "state.json"
    first = _save(path, 0)
    _save(path, 1)
    _save(path, 2)
    day = dt.date.today().isoformat()
    assert (tmp_path / f"state.json.{day}.bak").read_text(encoding="utf-8") == first


def test_daily_snapshots_are_pruned_to_seven_days(tmp_path):
    path = tmp_path / "state.json"
    _save(path, 0)
    for day in range(1, 10):
        (tmp_path / f"state.json.2026-08-{day:02d}.bak").write_text(
            "{}", encoding="utf-8")
    _save(path, 1)

    remaining = sorted(item.name for item in tmp_path.glob("state.json.*.bak"))
    assert len(remaining) == 7
    assert "state.json.2026-08-01.bak" not in remaining


def test_backup_failure_never_blocks_the_write(tmp_path, monkeypatch):
    """备份是加固：磁盘满/权限错时也不能把账本写入一起带崩。"""
    import app.wps.atomicio as atomicio

    path = tmp_path / "state.json"
    _save(path, 0)

    def boom(*_args, **_kwargs):
        raise OSError("模拟备份失败")

    monkeypatch.setattr(atomicio.shutil, "copy2", boom)
    ledger = SyncLedger(path)
    ledger.record(TARGET, FILE, {_key("甲", "111"): _entry([1])})
    ledger.save()

    assert SyncLedger(path).synced_slots(TARGET, FILE, "甲", "111") == [1]


# ----------------------------------------------------------------------
# 损坏时留档原件（否则下一次成功写入会把它原子替换掉）
# ----------------------------------------------------------------------

def test_corrupt_ledger_is_preserved_and_deduplicated(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ broken", encoding="utf-8")

    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)

    preserved = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "{ broken"

    # 同一份坏内容重复构造不重复留档（按内容摘要去重）
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1


def test_structurally_invalid_ledger_is_preserved(tmp_path):
    path = tmp_path / "state.json"
    payload = {"version": 1, "batches": {TARGET: {FILE: {
        "synced_at": "", "people": {_key("张", "111"): {"unexpected": 1}}}}}}
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)

    assert list(tmp_path.glob("state.json.corrupt-*"))


def test_unreadable_ledger_does_not_crash_留档(tmp_path):
    """读不出来（不是 JSON 问题）也必须失败关闭，且留档失败不能变成崩溃。"""
    path = tmp_path / "state.json"
    path.write_text('{"version": 1, "batches": {}}', encoding="utf-8")
    # 目录当文件读：模拟 OSError 分支
    directory = tmp_path / "dir"
    directory.mkdir()
    with pytest.raises(LedgerCorruptError):
        SyncLedger(directory)


# ----------------------------------------------------------------------
# 「缺失 / 丢过」的证据
# ----------------------------------------------------------------------

def _journal_with_verified(path: Path, *, status: str = "verified",
                           with_entries: bool = True) -> None:
    journal = SyncJournal(journal_path_for(path))
    record = {"sheet": SHEET, "file_id": FILE, "target_date": TARGET,
              "status": status, "next_action": "none"}
    if with_entries:
        record["ledger_entries"] = {_key("张", "111"): _entry([6], total=11)}
    journal.create_operation(OP_ID, {f"0:{SHEET}:{FILE}": record},
                             target_date=TARGET)
    journal.save()


def test_loss_is_proven_when_verified_anchors_are_gone(tmp_path):
    path = tmp_path / "state.json"
    _journal_with_verified(path)

    evidence = ledger_loss_evidence(SyncLedger(path))     # 账本文件不存在
    assert evidence["lost"] is True
    assert evidence["records"] == 1
    assert "journal" in evidence["traces"]
    assert evidence["reason"] == "ledger_anchors_missing"


def test_loss_is_cleared_once_the_anchor_is_back(tmp_path):
    path = tmp_path / "state.json"
    _journal_with_verified(path)
    ledger = SyncLedger(path)
    ledger.record(TARGET, FILE, {_key("张", "111"): _entry([6], total=11)})
    ledger.save()

    assert ledger_loss_evidence(SyncLedger(path))["lost"] is False


def test_greater_anchor_is_not_a_loss(tmp_path):
    """锚点只增不减：账本里的值比日志记的**大**是正常的（后来又加过餐）。"""
    path = tmp_path / "state.json"
    _journal_with_verified(path)
    ledger = SyncLedger(path)
    ledger.record(TARGET, FILE, {_key("张", "111"): _entry([8], total=13)})
    ledger.save()

    assert ledger_loss_evidence(SyncLedger(path))["lost"] is False


def test_missing_anchor_slot_is_a_loss(tmp_path):
    path = tmp_path / "state.json"
    journal = SyncJournal(journal_path_for(path))
    journal.create_operation(OP_ID, {f"0:{SHEET}:{FILE}": {
        "sheet": SHEET, "file_id": FILE, "target_date": TARGET,
        "status": "verified", "next_action": "none",
        "ledger_entries": {_key("张", "111"): _entry([6, 2])},
    }}, target_date=TARGET)
    journal.save()
    ledger = SyncLedger(path)
    ledger.record(TARGET, FILE, {_key("张", "111"): _entry([6])})
    ledger.save()

    assert ledger_loss_evidence(SyncLedger(path))["lost"] is True


def test_unfinished_records_never_prove_a_loss(tmp_path):
    """``writing`` 的锚点本来就不该在账本里 —— 只能当"以前用过"的痕迹。"""
    path = tmp_path / "state.json"
    _journal_with_verified(path, status="writing")

    evidence = ledger_loss_evidence(SyncLedger(path))
    assert evidence["lost"] is False
    # journal.save() 会同时创建 ledger/journal 共用的 .lock —— 两者都算痕迹。
    assert "journal" in evidence["traces"]


def test_loss_evidence_without_ledger_reports_traces_only(tmp_path):
    path = tmp_path / "state.json"
    _journal_with_verified(path)
    evidence = ledger_loss_evidence(None, path=path)
    assert evidence["lost"] is False
    assert "journal" in evidence["traces"]


def test_fresh_install_has_no_traces(tmp_path):
    path = tmp_path / "state.json"
    evidence = ledger_loss_evidence(SyncLedger(path))
    assert evidence == {"lost": False, "records": 0, "traces": [], "reason": ""}


# ----------------------------------------------------------------------
# 重建落盘
# ----------------------------------------------------------------------

def test_rebuild_anchors_writes_every_batch_atomically(tmp_path):
    path = tmp_path / "state.json"
    SyncLedger(path).save()
    result = rebuild_anchors(path, [
        (TARGET, FILE, {_key("甲", "111"): _entry([6], orders=["W1"], total=11)}),
        (TARGET, OTHER, {_key("乙", "222"): _entry([2], positions=[0], total=2)}),
    ], note="账本丢失后重建", actor="tester")

    assert result["changed"] is True
    assert result["people"] == 2 and result["slots"] == 2
    assert result["replaced_corrupt"] is False
    assert Path(result["backup"]).exists()

    fresh = SyncLedger(path)
    assert fresh.synced_slots(TARGET, FILE, "甲", "111") == [6]
    assert fresh.synced_slots(TARGET, OTHER, "乙", "222") == [2]
    batch = fresh.data["batches"][TARGET][FILE]
    assert batch["rebuilt_note"] == "账本丢失后重建"
    assert batch["rebuilt_at"] and batch["rebuilt_actor"] == "tester"


def test_rebuild_anchors_keeps_other_batches(tmp_path):
    path = tmp_path / "state.json"
    ledger = SyncLedger(path)
    ledger.record("2026-09-10", FILE, {_key("旧", "999"): _entry([3])})
    ledger.save()

    rebuild_anchors(path, [(TARGET, FILE, {_key("新", "111"): _entry([4])})],
                    note="只重建这一批")

    fresh = SyncLedger(path)
    assert fresh.synced_slots("2026-09-10", FILE, "旧", "999") == [3]
    assert fresh.synced_slots(TARGET, FILE, "新", "111") == [4]


def test_rebuild_anchors_recovers_from_a_corrupt_ledger(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ broken", encoding="utf-8")

    result = rebuild_anchors(path, [(TARGET, FILE, {_key("甲", "111"): _entry([4])})],
                             note="损坏后重建")

    assert result["replaced_corrupt"] is True
    assert Path(result["backup"]).read_text(encoding="utf-8") == "{ broken"
    assert SyncLedger(path).synced_slots(TARGET, FILE, "甲", "111") == [4]


def test_empty_ledger_never_reads_the_disk(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ broken", encoding="utf-8")

    ledger = SyncLedger.empty(path)

    assert ledger.data == {"version": 1, "batches": {}}
    assert ledger.path == path


# ----------------------------------------------------------------------
# 账本读不出来时，恢复入口仍然可用（只写 journal 的两个决策）
# ----------------------------------------------------------------------

def _pending_journal(path: Path) -> SyncJournal:
    journal = SyncJournal(journal_path_for(path))
    journal.create_operation(OP_ID, {f"0:{SHEET}:{FILE}": {
        "sheet": SHEET, "file_id": FILE, "target_date": TARGET,
        "status": "writing", "next_action": "recover_journal",
        "reason": "写入中途断电", "problems": [],
    }}, target_date=TARGET)
    journal.save()
    return journal


def test_retire_guarded_works_without_a_ledger(tmp_path):
    path = tmp_path / "state.json"
    journal = _pending_journal(path)

    result = resolve_pending_operation(
        None, OP_ID, "retire_guarded", journal=journal,
        note="账本损坏，先带审计退场", confirm_structure_checked=True)

    assert result["ok"] is True
    assert result["status"] == "retired_guarded"
    on_disk = SyncJournal(journal_path_for(path)).get_operation(OP_ID)
    assert on_disk["status"] == "retired_guarded"
    assert on_disk["retire_note"] == "账本损坏，先带审计退场"


def test_keep_works_without_a_ledger(tmp_path):
    path = tmp_path / "state.json"
    journal = _pending_journal(path)

    result = resolve_pending_operation(None, OP_ID, "keep", journal=journal,
                                       note="只留备注，保持阻断")

    assert result["status"] == "uncertain"
    assert result["next_action"] == "manual_reconcile"


def test_cloud_decisions_need_a_ledger(tmp_path):
    path = tmp_path / "state.json"
    journal = _pending_journal(path)

    for decision in ("cloud_verified", "cloud_untouched"):
        result = resolve_pending_operation(None, OP_ID, decision, journal=journal,
                                           note="云端已核对")
        assert result["ok"] is False
        assert result["status"] == "blocked"
        assert result["reason"] == "ledger_unavailable_requires_repair"
