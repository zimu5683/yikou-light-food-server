"""R7 独立验收探针 3：W2（journal 未知版本/未知状态必须阻断且不得改写审计）、
W3（可操作恢复路径 + 退场后不得变成可盲目重传）、W4（协作者占用新增目标行不得覆盖）。

与 R6 的 `probe_gate.py` / `probe_deadlock_clobber.py` 同形，但只依赖公开 API，
可在任意仓库副本上运行：

    PYTHONPATH=<repo> python3 probe_w2w3w4_journal_gate.py [--mutate-revert-w2]

``--mutate-revert-w2`` 把 R7 新加的 version/未知状态校验关掉，用于证明本探针
确实抓得住该回归（而不是空转）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

TARGET = dt.date(2026, 9, 11)
FILE_ID = "F1"


def _imports():
    from test_wps_cloud import BASE_HEADER, FakeCli, make_grid  # noqa: E402
    from app.wps.sync import CloudOrder, SyncLedger, apply_plan, build_plan  # noqa: E402
    from app.wps.recovery import recovery_status, resolve_pending_operation  # noqa: E402
    from app.wps.journal import journal_path_for  # noqa: E402
    return (BASE_HEADER, FakeCli, make_grid, CloudOrder, SyncLedger, apply_plan,
            build_plan, recovery_status, resolve_pending_operation, journal_path_for)


def make_cli(BASE_HEADER, FakeCli, make_grid, *, name="张", phone="111", total="5"):
    return FakeCli(make_grid(BASE_HEADER, [
        {0: name, 2: phone, 4: "1", 5: "中餐", 6: "经济", 7: total,
         8: "=SUM(D3)", 9: "=H3-I3", 10: "备注"},
    ]))


def make_plan(build_plan, cli, ledger, CloudOrder):
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6, rows=(19,))]
    return build_plan(cli, local_orders={"东湖中餐": orders},
                      tables={"东湖中餐": {"file_id": FILE_ID}}, target=TARGET,
                      ledger=ledger, marker_enabled=False,
                      address_order={}, sort_enabled=True)


def make_pending_journal(tmp: Path, ctx) -> Path:
    (BASE_HEADER, FakeCli, make_grid, CloudOrder, SyncLedger, apply_plan,
     build_plan, *_rest) = ctx
    ledger = SyncLedger(tmp / "state.json")
    cli = make_cli(BASE_HEADER, FakeCli, make_grid)
    plans = make_plan(build_plan, cli, ledger, CloudOrder)

    class BoomCli(FakeCli):
        def write_cells(self, *a, **kw):
            raise RuntimeError("模拟进程内未预期异常（非 WpsCloudError）")

    try:
        apply_plan(BoomCli(cli.grid), plans, ledger=ledger, marker_enabled=False)
    except RuntimeError:
        pass
    return ledger


def rewrite_journal(tmp: Path, mutator) -> tuple[Path, dict]:
    jp = Path(str(tmp / "state.json") + ".journal")
    data = json.loads(jp.read_text(encoding="utf-8"))
    op_id = next(iter(data["operations"]))
    mutator(data, data["operations"][op_id])
    jp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return jp, data


def probe_w2(ctx, mutate: bool) -> bool:
    """返回 True 表示“安全（被阻断且审计未被改写）”。"""
    (BASE_HEADER, FakeCli, make_grid, CloudOrder, SyncLedger, apply_plan,
     build_plan, recovery_status, _resolve, _jp) = ctx
    print("\n===== W2：未知版本 / 两层未知状态必须阻断，审计不得被改成 verified =====")
    ok = True

    if mutate:
        # 还原 R6 行为：不校验 version、未知状态不算 pending、_refresh 把未知写成 verified
        import app.wps.journal as journal_module
        journal_module.SyncJournal._original_unsupported = \
            journal_module.SyncJournal.unsupported_statuses

        def _loose_load(self):  # type: ignore[no-untyped-def]
            return None
        # 直接放宽 pending_operations：未知状态不再进 pending
        def loose_pending(self):  # type: ignore[no-untyped-def]
            result = {}
            for op_id, op in self.operations().items():
                statuses = [str(s.get("status", "")) for s in (op.get("sheets") or {}).values()
                            if isinstance(s, dict)]
                from app.wps.journal import PENDING_SHEET_STATUSES
                if op.get("status") in PENDING_SHEET_STATUSES or any(
                        st in PENDING_SHEET_STATUSES for st in statuses):
                    result[op_id] = op
            return result
        journal_module.SyncJournal.pending_operations = loose_pending
        print("[MUTATION] 已把 pending 判定放宽为 R6 的“只认已知状态”")

    for label, mutator in (
        ("version=2（跨版本降级读取）",
         lambda d, op: (d.__setitem__("version", 2),
                        op.__setitem__("status", "awaiting_cloud_v2"),
                        [s.__setitem__("status", "awaiting_cloud_v2")
                         for s in op["sheets"].values()])),
        ("两层未知状态（结构合法但词表外）",
         lambda d, op: (op.__setitem__("status", "awaiting_cloud_v2"),
                        [s.__setitem__("status", "awaiting_cloud_v2")
                         for s in op["sheets"].values()])),
    ):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            ledger = make_pending_journal(tmp, ctx)
            jp, data = rewrite_journal(tmp, mutator)
            op_id = next(iter(data["operations"]))
            before_status = json.loads(jp.read_text(encoding="utf-8"))["operations"][op_id]["status"]

            cli = make_cli(BASE_HEADER, FakeCli, make_grid)
            plans = make_plan(build_plan, cli, SyncLedger(tmp / "state.json"), CloudOrder)
            res = apply_plan(cli, plans, ledger=SyncLedger(tmp / "state.json"),
                             marker_enabled=False)
            written = len(cli.writes)
            after = json.loads(jp.read_text(encoding="utf-8"))["operations"][op_id]
            after_status = str(after.get("status") or "")
            st = recovery_status(SyncLedger(tmp / "state.json"))

            blocked = res["status"] != "ok" and written == 0
            # 要求：不得被改写成 verified；必须仍是阻断值；原始审计字符串必须保留。
            never_verified = after_status != "verified"
            # 版本不受支持时整个 journal 被判为不可读 → 原始值原样保留、
            # recovery 给出 fix_journal；这同样是最保守的阻断，不应算失败。
            still_blocking = (after_status in ("uncertain", "writing", "ledger_pending",
                                               "planned", "failed", "not_started")
                              or st["next_action"] == "fix_journal")
            raw_kept = _raw_status_preserved(after, before_status)
            reported = st["next_action"] in ("manual_reconcile", "recover_journal", "fix_journal")
            print(f"  [{label}] apply status={res['status']} 云端写入批次={written}")
            print(f"     磁盘 op 状态 {before_status!r} -> {after_status!r}"
                  f"（不得为 verified；原始值须保留）")
            print(f"     原始审计保留={raw_kept}  阻断值={still_blocking}  "
                  f"recovery next_action={st['next_action']}")
            good = blocked and never_verified and still_blocking and raw_kept and reported
            print(f"     => {'安全' if good else '不安全（fail-open 或审计被改写）'}")
            ok = ok and good
    return ok


def _raw_status_preserved(op: dict, original: str) -> bool:
    """原始状态字符串必须仍能在 operation 记录里找到（任一审计字段）。"""
    if str(op.get("status") or "") == original:
        return True
    for key, value in op.items():
        if isinstance(value, str) and value == original:
            return True
        if isinstance(value, (list, tuple)) and original in [str(v) for v in value]:
            return True
        if isinstance(value, dict):
            if original in [str(v) for v in value.values()]:
                return True
    for sheet in (op.get("sheets") or {}).values():
        if isinstance(sheet, dict):
            for value in sheet.values():
                if isinstance(value, str) and value == original:
                    return True
    return False


def probe_w3(ctx) -> bool:
    """W3：恢复路径可用；且**云端已有该订单证据时不得清障**；退场后不得盲目重传。"""
    (BASE_HEADER, FakeCli, make_grid, CloudOrder, SyncLedger, apply_plan,
     build_plan, recovery_status, resolve_pending_operation, _jp) = ctx
    print("\n===== W3：可操作恢复路径 + 有云端证据时不得清障 + 退场后防重复闸门仍在 =====")
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        ledger_path = tmp / "state.json"

        # --- 场景 A：崩溃发生在任何云端写入之前（云端确实未动）---
        make_pending_journal(tmp, ctx)
        j0 = json.loads(Path(str(ledger_path) + ".journal").read_text(encoding="utf-8"))
        op_a = next(iter(j0["operations"]))
        cli_a = make_cli(BASE_HEADER, FakeCli, make_grid)
        r_verified = resolve_pending_operation(SyncLedger(ledger_path), op_a,
                                               "cloud_verified", cli=cli_a, note="人工核对")
        print(f"  A) 云端无写入证据时 cloud_verified -> ok={r_verified.get('ok')} "
              f"status={r_verified.get('status')}（应为拒绝）")
        checks["cloud_verified_refused_without_proof"] = not r_verified.get("ok")
        r_retire = resolve_pending_operation(SyncLedger(ledger_path), op_a, "retire_guarded",
                                             cli=cli_a, note="人工核对云端后仍无法判定",
                                             confirm_structure_checked=True)
        print(f"     retire_guarded -> ok={r_retire.get('ok')} "
              f"status={r_retire.get('status')}（应可用）")
        checks["retire_guarded_path_exists"] = bool(r_retire.get("ok"))
        from app.wps.journal import SyncJournal
        j = SyncJournal(Path(str(ledger_path) + ".journal"))
        print(f"     退场后 pending_operations={len(j.pending_operations())} "
              f"has_guard={j.has_guard(TARGET.isoformat(), FILE_ID)}")
        checks["pending_cleared"] = len(j.pending_operations()) == 0
        checks["guard_retained"] = bool(j.has_guard(TARGET.isoformat(), FILE_ID))
        cli_r = make_cli(BASE_HEADER, FakeCli, make_grid)
        plans_r = make_plan(build_plan, cli_r, SyncLedger(ledger_path), CloudOrder)
        res_r = apply_plan(cli_r, plans_r, ledger=SyncLedger(ledger_path), marker_enabled=False)
        print(f"     退场后再上传: status={res_r['status']} 云端写入={len(cli_r.writes)}（应拒绝/0）")
        checks["no_retransmit_after_retire"] = res_r["status"] != "ok" and not cli_r.writes
        r_again = resolve_pending_operation(SyncLedger(ledger_path), op_a, "retire_guarded",
                                            cli=cli_a, note="再点一次",
                                            confirm_structure_checked=True)
        print(f"     重复退场 -> ok={r_again.get('ok')} status={r_again.get('status')} "
              f"changed={r_again.get('changed')}（应幂等）")
        checks["retire_idempotent"] = bool(r_again.get("ok")) and r_again.get("changed") in (False, None)

    # --- 场景 B（决定性）：云端确实已写入 → cloud_untouched 必须被拒绝 ---
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        ledger_path = tmp / "state.json"
        cli_b = make_cli(BASE_HEADER, FakeCli, make_grid)
        led = SyncLedger(ledger_path)
        res = apply_plan(cli_b, make_plan(build_plan, cli_b, led, CloudOrder),
                         ledger=led, marker_enabled=True)
        print(f"  B) 先做一次真实成功写入: status={res['status']} writes={len(cli_b.writes)}")
        from app.wps.journal import SyncJournal, journal_path_for, new_operation_id
        jp = journal_path_for(ledger_path)
        j = SyncJournal(jp)
        op_b = new_operation_id()
        j.create_operation(op_b, {"东湖中餐": {"sheet": "东湖中餐", "file_id": FILE_ID,
                                              "status": "writing"}},
                           target_date=TARGET.isoformat())
        j.save()
        r_b = resolve_pending_operation(SyncLedger(ledger_path), op_b, "cloud_untouched",
                                        cli=cli_b, note="人工声称从未写过")
        print(f"     云端已有该订单证据时 cloud_untouched -> ok={r_b.get('ok')} "
              f"status={r_b.get('status')}（必须拒绝）")
        checks["cloud_untouched_refused_when_cloud_has_evidence"] = not r_b.get("ok")

    for key, value in checks.items():
        print(f"     {key}: {'OK' if value else 'FAIL'}")
    return all(checks.values())


def probe_w4(ctx) -> bool:
    """协作者占用“新增行目标行”时必须拒绝，且不得覆盖。"""
    (BASE_HEADER, FakeCli, make_grid, CloudOrder, SyncLedger, apply_plan,
     build_plan, *_rest) = ctx
    print("\n===== W4：append_only 新增行的目标行被协作者占用 =====")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        ledger = SyncLedger(tmp / "state.json")
        # 空云表 → append_only 计划
        empty = FakeCli(make_grid(BASE_HEADER, []))
        plans = build_plan(empty, local_orders={"东湖中餐": [
            CloudOrder("东湖中餐", "新人A", "addr", "201", "中餐", "经济", 2, rows=(3,)),
            CloudOrder("东湖中餐", "新人B", "addr", "202", "中餐", "经济", 3, rows=(4,)),
        ]}, tables={"东湖中餐": {"file_id": FILE_ID}}, target=TARGET,
            ledger=ledger, marker_enabled=True, address_order={}, sort_enabled=True)
        append_only = [b.append_only for b in plans[0].insert_blocks]
        print(f"  计划 insert_blocks append_only={append_only}")

        # 协作者在计划之后填了目标行
        live = make_cli(BASE_HEADER, FakeCli, make_grid, name="老客户X", phone="901", total="9")
        live.grid[(2, 0)] = "老客户X"
        live.grid[(2, 2)] = "901"
        live.grid[(3, 0)] = "老客户Y"
        live.grid[(3, 2)] = "902"
        before = {(r, c): v for (r, c), v in live.grid.items() if r in (2, 3) and c in (0, 2)}

        res = apply_plan(live, plans, ledger=ledger, marker_enabled=False)
        after = {(r, c): v for (r, c), v in live.grid.items() if r in (2, 3) and c in (0, 2)}
        overwritten = before != after
        print(f"  apply status={res['status']} problems={res['sheets'][0].get('problems')}")
        print(f"  协作者行 before={before}")
        print(f"  协作者行 after ={after}")
        print(f"  => {'安全（拒绝且未覆盖）' if (res['status'] != 'ok' and not overwritten) else '不安全（覆盖了协作者数据）'}")
        return res["status"] != "ok" and not overwritten


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutate-revert-w2", action="store_true")
    args = parser.parse_args()
    ctx = _imports()
    results = {
        "W2": probe_w2(ctx, args.mutate_revert_w2),
        "W3": probe_w3(ctx),
        "W4": probe_w4(ctx),
    }
    print("\n===== 汇总 =====")
    for key, value in results.items():
        print(f"  {key}: {'PASS（安全）' if value else 'FAIL（不安全）'}")
    if args.mutate_revert_w2:
        print("  [mutation] 期望 W2 = FAIL（证明探针能抓回归）")
        return 0 if results["W2"] is False else 1
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
