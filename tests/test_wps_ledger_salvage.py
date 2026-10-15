"""账本抢救：结构受损但还能解析时，逐条丢坏的、留好的。

只使用合成数据与 ``tmp_path``；不联网、不读真实客户数据、不碰真实用户目录。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.wps.ledger as ledger_module
from app.wps.errors import LedgerCorruptError
from app.wps.journal import SyncJournal, journal_path_for
from app.wps.ledger import (_entry_problem, _validate_payload, salvage_ledger,
                            salvage_payload)
from app.wps.sync import SyncLedger

DAY = "2026-09-11"
OTHER_DAY = "2026-09-12"
FILE = "F1"
OTHER_FILE = "F2"


def _key(name: str, phone: str) -> str:
    return f"{name}\u0000{phone}"


def _entry(slots: list[int], **extra) -> dict:
    entry = {"slots": list(slots), "orders": [""] * len(slots),
             "positions": list(range(1, len(slots) + 1)),
             "local": sum(slots), "total": 11}
    entry.update(extra)
    return entry


def _payload(people: dict[str, dict], *, day: str = DAY, file_id: str = FILE,
             **root) -> dict:
    payload = {"version": 1,
               "batches": {day: {file_id: {"synced_at": "", "people": people}}}}
    payload.update(root)
    return payload


def _healthy_people() -> dict[str, dict]:
    return {_key("张三", "111"): _entry([6]),
            _key("李四", "222"): _entry([2])}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ----------------------------------------------------------------------
# 判定规则（与严格校验同源）
# ----------------------------------------------------------------------

def test_healthy_entry_has_no_problem():
    assert _entry_problem(_entry([6])) == ""
    assert _entry_problem({"meals": 6}) == ""        # 旧字段名仍然算有锚点


@pytest.mark.parametrize("entry,expected", [
    ({"slots": [], "local": 6}, "账本 slots 值非法"),
    ({"slots": [6, "x"], "local": 6}, "账本 slots 值非法"),
    ({"slots": "6", "local": 6}, "账本 slots 结构非法"),
    ({"slots": [6, 2], "orders": [""]}, "账本 orders 与 slots 不一致"),
    ({"slots": [6, 2], "positions": [1]}, "账本 positions 与 slots 不一致"),
    ({"unexpected": 1}, "账本人员条目缺少幂等锚点"),
    ({"slots": [6], "total": "abc"}, "账本条目数字字段非法"),
    ("不是字典", "账本人员条目结构非法"),
])
def test_each_kind_of_damage_has_its_own_reason(entry, expected):
    assert _entry_problem(entry) == expected


# ----------------------------------------------------------------------
# 只丢坏的那一条
# ----------------------------------------------------------------------

def test_one_bad_person_is_dropped_and_the_others_survive():
    people = _healthy_people()
    people[_key("王五", "333")] = {"slots": [], "local": 0}
    salvaged, report = salvage_payload(_payload(people))

    assert report["kept_people"] == 2
    assert report["dropped_people"] == 1
    assert report["dropped_batches"] == 0
    # 清单必须写明丢的是谁（用户据此决定去核对谁）
    assert report["dropped"] == [{"scope": "person", "day": DAY, "file": FILE,
                                  "name": "王五", "phone": "333",
                                  "reason": "账本 slots 值非法"}]
    assert _key("王五", "333") not in salvaged["batches"][DAY][FILE]["people"]
    assert _key("张三", "111") in salvaged["batches"][DAY][FILE]["people"]
    _validate_payload(salvaged)          # 救回来的结果必须能被严格校验读回


def test_dropped_report_never_contains_the_raw_nul_key():
    salvaged, report = salvage_payload(_payload({
        _key("张三", "111"): {"slots": [], "local": 0}}))

    dumped = json.dumps(report, ensure_ascii=False)
    assert "\u0000" not in dumped
    assert report["dropped"][0]["name"] == "张三"
    assert report["dropped"][0]["phone"] == "111"
    assert salvaged["batches"][DAY][FILE]["people"] == {}


def test_a_whole_batch_layer_is_dropped_when_people_is_not_a_mapping():
    payload = _payload(_healthy_people())
    payload["batches"][DAY][OTHER_FILE] = {"synced_at": "", "people": ["坏"]}

    salvaged, report = salvage_payload(payload)

    assert report["dropped_batches"] == 1
    assert report["dropped_people"] == 0
    assert report["dropped"][0]["scope"] == "batch"
    assert OTHER_FILE not in salvaged["batches"][DAY]
    assert FILE in salvaged["batches"][DAY]
    _validate_payload(salvaged)


def test_a_whole_day_is_dropped_when_it_is_not_a_mapping():
    payload = _payload(_healthy_people())
    payload["batches"][OTHER_DAY] = ["坏掉了"]

    salvaged, report = salvage_payload(payload)

    assert report["dropped_batches"] == 1
    assert report["dropped"][0]["scope"] == "day"
    assert OTHER_DAY not in salvaged["batches"]
    assert DAY in salvaged["batches"]
    _validate_payload(salvaged)


def test_non_int_version_is_repaired_not_fatal():
    payload = _payload(_healthy_people(), version="1")

    salvaged, report = salvage_payload(payload)

    assert salvaged["version"] == 1
    assert report["version_note"]
    assert report["kept_people"] == 2
    _validate_payload(salvaged)


def test_unknown_root_keys_are_preserved():
    payload = _payload(_healthy_people(), 未来字段={"a": 1})

    salvaged, _report = salvage_payload(payload)

    assert salvaged["未来字段"] == {"a": 1}


def test_previous_salvage_audit_is_replaced_not_accumulated():
    payload = _payload(_healthy_people(), salvage={"at": "旧的"})

    salvaged, _report = salvage_payload(payload)

    assert "salvage" not in salvaged


def test_dropped_list_is_capped_but_the_total_is_truthful():
    people = {_key(f"人{index}", str(index)): {"slots": [], "local": 0}
              for index in range(70)}

    _salvaged, report = salvage_payload(_payload(people))

    assert report["dropped_total"] == 70
    assert len(report["dropped"]) == 64
    assert report["dropped_people"] == 70


@pytest.mark.parametrize("damage", [
    {"batches": {"2026-09-11": {"F1": {"people": {"张\u0000111": {"slots": []}}}}}},
    {"batches": {"2026-09-11": ["不是字典"]}},
    {"batches": {"2026-09-11": {"F1": {"people": {"坏键": "不是字典"}}}}},
    {"version": "x", "batches": {"2026-09-11": {"F1": {"people": {}}}}},
    {"batches": {}},
])
def test_salvaged_result_always_passes_the_strict_validator(damage):
    """不变式：不管怎么坏，救回来的结果都必须能被严格校验读回来。"""
    salvaged, report = salvage_payload(damage)

    assert report["unsalvageable"] == ""
    _validate_payload(salvaged)


# ----------------------------------------------------------------------
# 读盘与写回
# ----------------------------------------------------------------------

def test_dry_run_reports_and_writes_nothing(tmp_path):
    path = tmp_path / "state.json"
    people = _healthy_people()
    people[_key("王五", "333")] = {"slots": [], "local": 0}
    _write(path, _payload(people))
    before = path.read_text(encoding="utf-8")
    files_before = sorted(item.name for item in tmp_path.iterdir())

    result = salvage_ledger(path, dry_run=True)

    assert result["ok"] is True
    assert result["status"] == "salvage_ready"
    assert result["changed"] is False
    assert result["report"]["dropped_people"] == 1
    assert result["report"]["dropped"][0]["name"] == "王五"
    # 只读就是只读：连锁文件都不许出现
    assert path.read_text(encoding="utf-8") == before
    assert sorted(item.name for item in tmp_path.iterdir()) == files_before


def test_healthy_ledger_is_never_rewritten(tmp_path):
    path = tmp_path / "state.json"
    _write(path, _payload(_healthy_people()))
    before = path.read_text(encoding="utf-8")

    result = salvage_ledger(path)

    assert result["ok"] is True
    assert result["status"] == "nothing_to_salvage"
    assert result["changed"] is False
    assert result["backup"] == ""
    assert path.read_text(encoding="utf-8") == before


def test_write_keeps_the_good_entries_and_records_the_audit(tmp_path):
    path = tmp_path / "state.json"
    people = _healthy_people()
    people[_key("王五", "333")] = {"slots": [], "local": 0}
    _write(path, _payload(people))
    before = path.read_text(encoding="utf-8")

    result = salvage_ledger(path, note="账本损坏，按清单抢救", actor="tester")

    assert result["ok"] is True
    assert result["status"] == "salvaged"
    assert result["changed"] is True
    assert Path(result["backup"]).read_text(encoding="utf-8") == before
    assert result["report"]["dropped_people"] == 1
    assert result["report"]["dropped_batches"] == 0

    fresh = SyncLedger(path)                      # 抢救后必须能正常读
    assert fresh.synced_slots(DAY, FILE, "张三", "111") == [6]
    assert fresh.synced_slots(DAY, FILE, "李四", "222") == [2]
    assert fresh.synced_slots(DAY, FILE, "王五", "333") is None
    audit = fresh.data["salvage"]
    assert audit["note"] == "账本损坏，按清单抢救"
    assert audit["actor"] == "tester"
    assert audit["dropped_people"] == 1
    assert audit["at"]
    assert list(tmp_path.glob("*.tmp")) == []


def test_unparseable_json_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ broken", encoding="utf-8")

    result = salvage_ledger(path)

    assert result["ok"] is False
    assert result["status"] == "parse_failed"
    assert result["changed"] is False
    assert result["backup"] == ""
    assert path.read_text(encoding="utf-8") == "{ broken"
    assert list(tmp_path.glob("state.json.pre-salvage-*")) == []


def test_missing_file_is_refused(tmp_path):
    result = salvage_ledger(tmp_path / "state.json")

    assert result["ok"] is False
    assert result["status"] == "missing"
    assert result["changed"] is False


def test_unsalvageable_root_is_refused(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    result = salvage_ledger(path)

    assert result["ok"] is False
    assert result["status"] == "unsalvageable"
    assert result["report"]["unsalvageable"]
    assert result["changed"] is False


def test_self_check_blocks_a_write_that_would_not_read_back(tmp_path, monkeypatch):
    """规则两边同源是硬要求：自查不过宁可不写，绝不写进一个读不回来的文件。"""
    path = tmp_path / "state.json"
    people = _healthy_people()
    people[_key("王五", "333")] = {"slots": [], "local": 0}
    _write(path, _payload(people))
    before = path.read_text(encoding="utf-8")

    def boom(_payload):
        raise LedgerCorruptError("模拟规则不同源")

    monkeypatch.setattr(ledger_module, "_validate_payload", boom)
    result = salvage_ledger(path)

    assert result["ok"] is False
    assert result["status"] == "self_check_failed"
    assert result["changed"] is False
    assert result["backup"] == ""
    assert path.read_text(encoding="utf-8") == before


def test_salvage_never_touches_the_journal(tmp_path):
    path = tmp_path / "state.json"
    people = _healthy_people()
    people[_key("王五", "333")] = {"slots": [], "local": 0}
    _write(path, _payload(people))
    journal = SyncJournal(journal_path_for(path))
    journal.create_operation("wps-0123456789abcdef", {
        f"0:东湖中餐:{FILE}": {"sheet": "东湖中餐", "file_id": FILE,
                               "target_date": DAY, "status": "writing",
                               "next_action": "recover_journal"}},
        target_date=DAY)
    journal.save()
    journal_before = journal_path_for(path).read_bytes()

    salvage_ledger(path, note="抢救不动日志")

    assert journal_path_for(path).read_bytes() == journal_before
