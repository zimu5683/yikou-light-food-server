"""R7 独立验收探针 2：W1 —— 与 R6 完全相同的“旧计划竞争”场景，看是否仍重复写入。

场景（与 R6 `probe_double.py` 同形，这是当初真的写出重复行的那种计划形状）：

  账本 seed：P 已有 slot 1（本地 1 餐）
  本地排单：P 有 2 餐 → 计划 = slot1(existing, needs_write=False) + slot2(new 行)
  B：用 L0 建计划（此时快照 D0）
  A：同样的计划先执行并提交（云端写入 + 账本落盘）→ 磁盘快照变 D1
  B：带着 L0 的计划执行，调用方（旧接线）传的是**执行时新建**的 L1（摘要 = D1）

R6 结论：旧接线的 stale 比较退化成“同一时刻两份相同快照”→ B 照写 → 重复行。
R7 修复：计划携带 ``ledger_digest``（D0），锁内与磁盘摘要（D1）比较 → 零写入拒绝。

用法::

    PYTHONPATH=<repo> python3 probe_w1_stale_plan.py [--mutate-r6-wiring]

``--mutate-r6-wiring`` 关闭层 B（计划快照比较），并保持“传执行时新建的账本”这一
旧接线，用来证明本探针确实能抓到该回归。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import tempfile
from pathlib import Path

TARGET = dt.date(2026, 9, 11)
FILE_ID = "F1"
PHONE = "111"
SORT = True  # 生产默认 cfg.wps_sort_enabled = True


def _imports():
    from test_wps_cloud import BASE_HEADER, FakeCli, make_grid  # noqa: E402
    from app.wps.sync import CloudOrder, SyncLedger, apply_plan, build_plan  # noqa: E402
    return BASE_HEADER, FakeCli, make_grid, CloudOrder, SyncLedger, apply_plan, build_plan


def cloud(BASE_HEADER, FakeCli, make_grid):
    return FakeCli(make_grid(BASE_HEADER, [
        {0: "P", 2: PHONE, 4: "1", 5: "中餐", 6: "经济", 7: "5",
         8: "=SUM(D3)", 9: "=H3-I3", 10: "备注"},
    ]))


def orders(CloudOrder):
    return [CloudOrder("东湖中餐", "P", "addr-1", PHONE, "中餐", "经济", 1, row=3, rows=(3,)),
            CloudOrder("东湖中餐", "P", "addr-1", PHONE, "中餐", "经济", 1, row=4, rows=(4,))]


def build(build_plan, cli, ledger, CloudOrder):
    return build_plan(cli, local_orders={"东湖中餐": orders(CloudOrder)},
                      tables={"东湖中餐": {"file_id": FILE_ID}}, target=TARGET,
                      ledger=ledger, marker_enabled=True,
                      address_order={}, sort_enabled=SORT)


def rows_with_name(cli, name):
    out = []
    for (r, c), v in sorted(cli.grid.items()):
        if c == 0 and str(v).strip() == name:
            out.append({"row": r + 1, "total": cli.grid.get((r, 7), ""),
                        "mark": cli.grid.get((r, 4), "")})
    return out


def run(*, mutate: bool) -> int:
    BASE_HEADER, FakeCli, make_grid, CloudOrder, SyncLedger, apply_plan, build_plan = _imports()
    if mutate:
        import app.wps.executor as executor_module
        executor_module._plan_ledger_digests = lambda plans: set()
        print("[MUTATION] 已关闭层 B（plan.ledger_digest 比较）→ 还原 R6 旧接线的 stale 盲区")

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        lpath = tmp / "state.json"
        seed = SyncLedger(lpath)
        seed.record(TARGET.isoformat(), FILE_ID,
                    {f"P\u0000{PHONE}": {"slots": [1], "local": 1, "total": 5}})
        seed.save()

        cli = cloud(BASE_HEADER, FakeCli, make_grid)

        plans_b = build(build_plan, cli, SyncLedger(lpath), CloudOrder)
        digests = [getattr(p, "ledger_digest", None) for p in plans_b]
        print(f"[probe W1] B 的计划携带 ledger_digest = {digests}")

        cli_a = cloud(BASE_HEADER, FakeCli, make_grid)
        plans_a = build(build_plan, cli_a, SyncLedger(lpath), CloudOrder)
        # A 执行：旧接线语义 —— 调用方传执行时新建的账本（摘要 = 计划时快照，
        # 因为此刻磁盘还没被 A 改）。修复后这里仍会被“计划快照 vs 锁内快照”拦住。
        res_a = apply_plan(cli_a, plans_a, ledger=SyncLedger(lpath), marker_enabled=True)
        print(f"[probe W1] A status={res_a['status']} rows={rows_with_name(cli_a, 'P')}")
        cli.grid = dict(cli_a.grid)

        writes_before = len(cli.writes)
        res_b = apply_plan(cli, plans_b, ledger=SyncLedger(lpath), marker_enabled=True)
        wrote_b = len(cli.writes) - writes_before
        marks = [r for r in rows_with_name(cli, "P") if r["mark"] == "1"]
        print(f"[probe W1] B status={res_b['status']} next_action={res_b.get('next_action')}")
        print(f"[probe W1] B 期间云端写入批次={wrote_b}")
        print(f"[probe W1] 最终 P 的标记行数={len(marks)}（期望 2）")
        verdict = "DOUBLE WRITE" if len(marks) > 2 else "no double write"
        print(f"[probe W1] VERDICT: {verdict}")

    if mutate:
        print("[probe W1] 变异期望 = DOUBLE WRITE")
        return 0 if verdict == "DOUBLE WRITE" else 1
    return 0 if verdict == "no double write" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutate-r6-wiring", action="store_true")
    args = parser.parse_args()
    code = run(mutate=args.mutate_r6_wiring)
    print(f"[probe W1] exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
