"""账本兜底：Bridge 层的恢复可用性、丢失阻断与锚点重建（走真实计划链路）。

第 14 项（``design/WPS-账本丢失与损坏的兜底.md``）在 Bridge 层的四件事：

1. 账本损坏时**恢复面板仍然打得开**（否则界面指向一个此刻打不开的入口）；
2. 账本不可用时，「退场」「只记录备注」仍可执行，「确认云端已完成/未执行」被拒；
3. 可证明的丢失（意图日志里记着已同步、账本里查不到）→ 预览/上传**失败关闭**；
4. 「账本锚点重建」能提案、能确认落盘、能把预览重新放行，且**永不写云端**。

全部离线：合成云表 + 临时账本/日志；不联网、不碰真实客户数据与真实用户目录。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge
from app.wps.errors import LedgerCorruptError
from app.wps.journal import SyncJournal, journal_path_for
from app.wps.ledger import SyncLedger
from app.wps.sync import target_date_for
from tests.wps_ledger_harness import (COL_NAME, COL_PHONE, COL_TARGET, COL_TOTAL,
                                      FILE_ID, SHEET, GridCloud, make_grid,
                                      write_local_excel)

NAME = "合成客户"
PHONE = "13800000000"
PERSON_KEY = f"{NAME}\u0000{PHONE}"
OP_ID = "wps-0123456789abcdef"


def _corrupt_ledger_class(ledger_path: Path):
    """``SyncLedger`` 替身：构造即抛（账本损坏），但 ``empty()`` 仍给出账本路径。

    这样 Bridge 的路径探测才会指向临时目录 —— 恢复视图读到的意图日志也就
    一定在同目录，绝不会落到真实用户目录。
    """

    class _CorruptLedger:
        def __init__(self, *args, **kwargs):
            raise LedgerCorruptError("账本 JSON 损坏")

        @classmethod
        def empty(cls, path=None):
            return SimpleNamespace(path=ledger_path)

    return _CorruptLedger


def _bound_ledger_class(ledger_path: Path):
    """``SyncLedger`` 替身：**真的**读临时目录那个文件（坏就抛），``empty()`` 给路径。

    与 ``_corrupt_ledger_class`` 的区别：那个永远抛（模拟"彻底读不出来"），这个
    会跟着磁盘上的文件变 —— 抢救写回之后它就能正常读，正好验证"救完自动恢复"。
    """

    class _Bound:
        def __init__(self, *args, **kwargs):
            self._inner = SyncLedger(ledger_path)

        def __getattr__(self, item):
            return getattr(self._inner, item)

        @classmethod
        def empty(cls, path=None):
            return SimpleNamespace(path=ledger_path)

    return _Bound


@pytest.fixture
def env(tmp_path, monkeypatch):
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    target = target_date_for(start_hour=bridge._config.wps_target_hour_start,
                             end_hour=bridge._config.wps_target_hour_end)
    excel = tmp_path / "排单.xlsx"
    write_local_excel(excel, name=NAME, phone=PHONE, meals=2, target=target)
    cloud = GridCloud(make_grid(target=target, rows=[
        {COL_NAME: NAME, COL_PHONE: PHONE, COL_TARGET: "1", COL_TOTAL: "5"}]))
    ledger_path = tmp_path / "wps_sync_state.json"

    bridge._config.excel_path = excel
    bridge._config.wps_enabled = True
    bridge._config.wps_test_mode = False
    bridge._config.wps_marker_enabled = False
    bridge._config.wps_sort_enabled = False
    bridge._config.wps_tables = {SHEET: {"file_id": FILE_ID}}
    bridge._config.wps_production_tables = {SHEET: {"file_id": FILE_ID}}
    monkeypatch.setattr(bridge_module, "SyncLedger",
                        lambda *args, **kwargs: SyncLedger(ledger_path))
    monkeypatch.setattr(bridge, "_wps_cli", lambda: cloud)

    state = SimpleNamespace(bridge=bridge, cloud=cloud, target=target, excel=excel,
                            ledger_path=ledger_path,
                            journal_path=journal_path_for(ledger_path))

    def use_corrupt_ledger() -> None:
        monkeypatch.setattr(bridge_module, "SyncLedger",
                            _corrupt_ledger_class(ledger_path))

    def use_bound_ledger() -> None:
        monkeypatch.setattr(bridge_module, "SyncLedger",
                            _bound_ledger_class(ledger_path))

    state.use_corrupt_ledger = use_corrupt_ledger
    state.use_bound_ledger = use_bound_ledger
    return state


def _write_journal(env, *, status="writing", entries=None) -> None:
    journal = SyncJournal(env.journal_path)
    record = {"sheet": SHEET, "file_id": FILE_ID,
              "target_date": env.target.isoformat(), "status": status,
              "next_action": "recover_journal", "problems": []}
    if entries is not None:
        record["ledger_entries"] = entries
    journal.create_operation(OP_ID, {f"0:{SHEET}:{FILE_ID}": record},
                             target_date=env.target.isoformat())
    journal.save()


def _journal_says_synced(env) -> None:
    """意图日志说「这一批已经入账 2 餐」——但账本里没有（可证明的丢失）。"""
    _write_journal(env, status="verified", entries={
        PERSON_KEY: {"slots": [2], "orders": ["A-1"], "positions": [1],
                     "local": 2, "total": 7}})


# ----------------------------------------------------------------------
# 1 + 2：账本坏掉时恢复入口仍然可用
# ----------------------------------------------------------------------

def test_recovery_view_survives_a_corrupt_ledger(env):
    env.use_corrupt_ledger()
    _write_journal(env)

    status = env.bridge.wps_recovery_status()

    assert status["ok"] is True
    assert status["ledger_state"] == "corrupt"
    assert [op["operation_id"] for op in status["operations"]] == [OP_ID]


def test_retire_guarded_works_while_the_ledger_is_unreadable(env):
    env.use_corrupt_ledger()
    _write_journal(env)

    result = env.bridge.wps_recovery_resolve({
        "operation_id": OP_ID, "decision": "retire_guarded",
        "confirm": "retire_guarded", "note": "账本损坏，先带审计退场",
        "confirm_structure_checked": True})

    assert result["ok"] is True
    assert result["status"] == "retired_guarded"
    assert result["changed"] is True
    on_disk = SyncJournal(env.journal_path).get_operation(OP_ID)
    assert on_disk["status"] == "retired_guarded"
    assert on_disk["retire_note"] == "账本损坏，先带审计退场"


def test_keep_works_while_the_ledger_is_unreadable(env):
    env.use_corrupt_ledger()
    _write_journal(env)

    result = env.bridge.wps_recovery_resolve({
        "operation_id": OP_ID, "decision": "keep",
        "confirm": "keep", "note": "只留核对备注，保持阻断"})

    assert result["ok"] is True
    assert result["status"] == "keep_recorded"
    assert result["next_action"] == "manual_reconcile"


def test_cloud_decisions_are_refused_while_the_ledger_is_unreadable(env):
    env.use_corrupt_ledger()
    _write_journal(env)

    result = env.bridge.wps_recovery_resolve({
        "operation_id": OP_ID, "decision": "cloud_verified",
        "confirm": "cloud_verified", "note": "云端已确认完成"})

    assert result["ok"] is False
    assert result["code"] == "ledger_unavailable_requires_repair"
    assert result["changed"] is False
    on_disk = SyncJournal(env.journal_path).get_operation(OP_ID)
    assert on_disk["status"] == "writing"      # 一个字节都没改


# ----------------------------------------------------------------------
# 状态入口：把「缺失 / 有痕迹 / 损坏」如实说出来
# ----------------------------------------------------------------------

def test_status_reports_the_ledger_state(env):
    missing = env.bridge.wps_status()
    assert missing["ok"] is True
    assert missing["ledger_state"] == "missing"      # 没有账本，也没有任何痕迹
    assert missing["ledger_lost"] is False
    assert missing["state_path"].endswith("wps_sync_state.json")

    _write_journal(env)
    with_history = env.bridge.wps_status()
    assert with_history["ok"] is True
    assert with_history["ledger_state"] == "missing_with_history"

    env.use_corrupt_ledger()
    broken = env.bridge.wps_status()
    assert broken["ok"] is False                     # 状态入口也不能 500
    assert broken["ledger_state"] == "corrupt"
    assert broken["reason"]


# ----------------------------------------------------------------------
# 3：可证明的丢失 → 失败关闭
# ----------------------------------------------------------------------

def test_preview_is_blocked_when_anchors_are_provably_lost(env):
    _journal_says_synced(env)

    result = env.bridge.wps_preview()

    assert result["ok"] is False
    assert result["code"] == "local_ledger_lost"
    assert result["status"] == "blocked"
    assert result["written"] == 0
    assert env.cloud.writes == []


def test_upload_path_is_also_blocked_when_anchors_are_provably_lost(env):
    _journal_says_synced(env)
    env.use_corrupt_ledger()      # 上传前的上下文准备也读账本

    result = env.bridge.wps_upload("pv-不存在")

    # 令牌不存在时在读取上下文之前就被拒；换成损坏账本也一样不许有云端写入。
    assert result["ok"] is False
    assert env.cloud.writes == []


def test_preview_warns_when_the_ledger_is_missing_but_history_exists(env):
    _write_journal(env)
    assert not env.ledger_path.exists()

    result = env.bridge.wps_preview()

    assert result["ok"] is True
    assert result["status"] == "preview_ready"
    assert any("账本文件不存在" in warning for warning in result["warnings"])


def test_preview_speaks_differently_on_a_fresh_install(env):
    """全新安装也要提示（那正是"卸载重装/清过应用数据"的样子），但措辞不同。

    账本丢失与全新安装**长得一模一样**，本地没有任何证据能把两者分开，所以只能
    把人工核对的要求说清楚；有历史痕迹时说法更明确（"很可能账本丢过"）。
    """
    fresh = env.bridge.wps_preview()
    assert fresh["ok"] is True and fresh["status"] == "preview_ready"
    assert any("账本文件不存在" in warning and "第一次用就是对的" in warning
               for warning in fresh["warnings"])
    assert not any("痕迹" in warning for warning in fresh["warnings"])

    _write_journal(env)      # 留下历史痕迹
    with_history = env.bridge.wps_preview()
    assert any("痕迹" in warning for warning in with_history["warnings"])
    assert "missing_with_history" not in fresh["ledger_state"]


# ----------------------------------------------------------------------
# 4：账本锚点重建
# ----------------------------------------------------------------------

def test_rebuild_proposal_is_read_only(env):
    result = env.bridge.wps_ledger_rebuild()

    assert result["ok"] is True
    assert result["status"] == "rebuild_ready"
    assert result["read_only"] is True
    assert result["cloud_write"] is False
    assert result["ledger_write"] is False
    assert result["counts"]["mark_synced"] == 1
    row = result["tables"][0]["rows"][0]
    assert row["decision"] == "mark_synced"
    assert row["anchor"] == 2
    assert not env.ledger_path.exists()
    assert env.cloud.writes == []


def test_rebuild_commit_writes_the_anchors(env):
    ledger = SyncLedger(env.ledger_path)
    ledger.record("2026-01-01", "OTHER", {"别人\u0000000": {
        "slots": [1], "orders": [""], "positions": [1], "local": 1, "total": 1}})
    ledger.save()
    proposal = env.bridge.wps_ledger_rebuild()

    result = env.bridge.wps_ledger_rebuild({
        "confirm": "rebuild", "note": "账本丢失，按云端日期格重建",
        "expected_fingerprint": proposal["fingerprint"]})

    assert result["ok"] is True
    assert result["status"] == "rebuilt"
    assert result["changed"] is True
    assert result["verified_on_disk"] is True
    assert result["cloud_write"] is False
    assert result["ledger_write"] is True
    assert Path(result["backup"]).exists()
    assert env.cloud.writes == []

    fresh = SyncLedger(env.ledger_path)
    assert fresh.synced_slots(env.target.isoformat(), FILE_ID, NAME, PHONE) == [2]
    assert fresh.synced_slots("2026-01-01", "OTHER", "别人", "000") == [1]
    batch = fresh.data["batches"][env.target.isoformat()][FILE_ID]
    assert batch["rebuilt_at"]
    assert batch["rebuilt_note"] == "账本丢失，按云端日期格重建"


def test_rebuild_unblocks_the_preview_after_a_proven_loss(env):
    _journal_says_synced(env)
    assert env.bridge.wps_preview()["code"] == "local_ledger_lost"

    proposal = env.bridge.wps_ledger_rebuild()
    assert proposal["ledger_lost"] is True
    committed = env.bridge.wps_ledger_rebuild({
        "confirm": "rebuild", "note": "按云端日期格重建锚点",
        "expected_fingerprint": proposal["fingerprint"]})
    assert committed["ok"] is True

    after = env.bridge.wps_preview()

    assert after["ok"] is True
    assert after["status"] == "preview_ready"
    assert after["tables"][0]["changes"][0]["delta"] == 0


@pytest.mark.parametrize("payload,expected", [
    ({"confirm": "yes", "note": "确认重建锚点"}, "confirmation_required"),
    ({"confirm": "rebuild", "note": "短"}, "note_required"),
])
def test_rebuild_requires_explicit_confirmation(env, payload, expected):
    result = env.bridge.wps_ledger_rebuild(payload)

    assert result["ok"] is False
    assert result["code"] == expected
    assert result["changed"] is False
    assert not env.ledger_path.exists()


def test_rebuild_rejects_a_stale_fingerprint(env):
    result = env.bridge.wps_ledger_rebuild({
        "confirm": "rebuild", "note": "指纹对不上",
        "expected_fingerprint": "0" * 64})

    assert result["ok"] is False
    assert result["code"] == "rebuild_changed"
    assert not env.ledger_path.exists()


def test_rebuild_is_refused_when_cloud_sync_is_disabled(env):
    env.bridge._config.wps_enabled = False

    proposal = env.bridge.wps_ledger_rebuild()
    commit = env.bridge.wps_ledger_rebuild({
        "confirm": "rebuild", "note": "开关关着也要拒绝"})

    assert proposal["ok"] is False and proposal["code"] == "wps_disabled"
    assert commit["ok"] is False and commit["code"] == "wps_disabled"
    assert not env.ledger_path.exists()


# ----------------------------------------------------------------------
# 5：账本抢救（结构受损但还能解析时，逐条丢坏的、留好的）
# ----------------------------------------------------------------------

def _damage_ledger(env) -> None:
    """账本文件结构受损：合成客户那条是好的，王五那条的 slots 是空数组。"""
    payload = {"version": 1, "batches": {env.target.isoformat(): {FILE_ID: {
        "synced_at": "", "people": {
            PERSON_KEY: {"slots": [2], "orders": ["A-1"], "positions": [1],
                         "local": 2, "total": 5},
            "王五\u0000333": {"slots": [], "local": 0},
        }}}}}
    env.ledger_path.write_text(json.dumps(payload, ensure_ascii=False),
                               encoding="utf-8")


def test_salvage_proposal_is_read_only_and_names_who_would_be_dropped(env):
    _damage_ledger(env)
    env.use_bound_ledger()
    before = env.ledger_path.read_text(encoding="utf-8")

    result = env.bridge.wps_ledger_salvage()

    assert result["ok"] is True
    assert result["status"] == "salvage_ready"
    assert result["read_only"] is True
    assert result["ledger_write"] is False
    assert result["changed"] is False
    assert result["counts"]["kept_people"] == 1
    assert result["counts"]["dropped_people"] == 1
    assert result["dropped"][0]["name"] == "王五"
    assert result["dropped"][0]["phone"] == "333"
    assert result["dropped"][0]["reason"] == "账本 slots 值非法"
    assert result["next_action"] == "wps_ledger_salvage(confirm='salvage')"
    assert env.ledger_path.read_text(encoding="utf-8") == before
    assert env.cloud.writes == []


def test_salvage_requires_the_confirm_word_and_a_note(env):
    _damage_ledger(env)
    env.use_bound_ledger()
    before = env.ledger_path.read_text(encoding="utf-8")

    for payload, expected in [
        ({"confirm": "yes", "note": "确认抢救这条账本"}, "confirmation_required"),
        ({"confirm": "salvage", "note": "短"}, "note_required"),
    ]:
        result = env.bridge.wps_ledger_salvage(payload)
        assert result["ok"] is False
        assert result["code"] == expected
        assert result["changed"] is False

    assert env.ledger_path.read_text(encoding="utf-8") == before


def test_salvage_write_unblocks_the_preview(env):
    _damage_ledger(env)
    env.use_bound_ledger()
    before = env.ledger_path.read_text(encoding="utf-8")

    blocked = env.bridge.wps_preview()
    assert blocked["ok"] is False
    assert blocked["code"] == "local_state_blocked"

    written = env.bridge.wps_ledger_salvage({
        "confirm": "salvage", "note": "账本损坏，按清单抢救"})

    assert written["ok"] is True
    assert written["status"] == "salvaged"
    assert written["changed"] is True
    assert written["verified_on_disk"] is True
    assert written["cloud_write"] is False
    assert written["ledger_write"] is True
    assert written["ledger_state"] == "ok"
    assert Path(written["backup"]).read_text(encoding="utf-8") == before
    assert written["audit"]["dropped_people"] == 1
    assert env.cloud.writes == []

    status = env.bridge.wps_status()
    assert status["ok"] is True
    assert status["ledger_state"] == "ok"

    after = env.bridge.wps_preview()
    assert after["ok"] is True
    assert after["status"] == "preview_ready"


def test_salvage_works_while_cloud_sync_is_disabled(env):
    """抢救是纯本地修复：云同步关着也必须能用（否则用户被锁死）。"""
    _damage_ledger(env)
    env.use_bound_ledger()
    env.bridge._config.wps_enabled = False

    proposal = env.bridge.wps_ledger_salvage()
    written = env.bridge.wps_ledger_salvage({
        "confirm": "salvage", "note": "关着云同步也要能修本地账本"})

    assert proposal["ok"] is True
    assert written["ok"] is True and written["status"] == "salvaged"


def test_salvage_is_refused_while_an_operation_holds_the_lock(env):
    """上传/恢复正在写账本时抢着抢救，会把那一批刚写下的锚点覆盖掉。"""
    import threading

    from app.wps.sync import FileLock, operation_lock_path_for

    _damage_ledger(env)
    env.use_bound_ledger()
    before = env.ledger_path.read_text(encoding="utf-8")
    holder = FileLock(operation_lock_path_for(env.ledger_path))
    ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        holder.acquire()
        ready.set()
        release.wait(30)
        holder.release()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(10), "锁没拿上"
        result = env.bridge.wps_ledger_salvage({
            "confirm": "salvage", "note": "另一个操作正在跑"})
        assert result["ok"] is False
        assert result["code"] == "operation_conflict"
        assert result["changed"] is False
        assert env.ledger_path.read_text(encoding="utf-8") == before
    finally:
        release.set()
        worker.join(10)


def test_rebuild_is_a_noop_when_no_row_is_marked(env):
    env.cloud.grid.pop((2, COL_TARGET), None)      # 云端这一格是空的
    proposal = env.bridge.wps_ledger_rebuild()
    assert proposal["counts"]["mark_synced"] == 0

    result = env.bridge.wps_ledger_rebuild({
        "confirm": "rebuild", "note": "没有可写的行",
        "expected_fingerprint": proposal["fingerprint"]})

    assert result["ok"] is True
    assert result["status"] == "rebuild_noop"
    assert result["changed"] is False
    assert not env.ledger_path.exists()
