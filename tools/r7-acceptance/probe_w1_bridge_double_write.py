"""R7 独立验收探针 1：W1 —— 生产 Bridge 接线下的“双实例旧计划竞争”。

为什么这样写：R6 的 W1 反例证明旧接线在“建计划”和“执行”两处各新建一个
``SyncLedger()``，于是 ``apply_plan`` 的 stale 比较退化成“同一时刻的两份相同
快照”。本探针**不 stub** ``build_plan`` / ``apply_plan``，走真实生产接线：

    真实 openpyxl 排单表 → read_local_orders → build_plan（合成云表）
      → Bridge 预览指纹/令牌 → upload（锁内 stale 校验）→ apply_plan 真实写入

只替换三处：云表（内存合成）、账本路径（tmp_path）、kdocs-cli（内存替身）。

用法::

    PYTHONPATH=<repo> python3 probe_w1_bridge_double_write.py [--mutate-old-wiring]

``--mutate-old-wiring`` 会还原 R6 的旧接线（计划不携带账本快照），用于证明
本探针确实能抓到该回归（而不是“永远通过”）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

NAME, ADDR, PHONE, DATE, TYPE, KIND, TOTAL = 0, 1, 2, 3, 4, 5, 6
SERVED, LEFT, REMARK = 7, 8, 9
SHEET = "东湖中餐"
FILE_ID = "F-SYNTH-W1"
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class SyntheticCloud:
    path = "/fake/kdocs-cli"

    def __init__(self, grid):
        self.grid = dict(grid)
        self.writes = []

    def authenticated(self):
        return True

    def sheets_info(self, file_id):
        return [{"sheetId": 1, "sheetName": "Sheet1", "rowTo": 200, "colTo": 40}]

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format=False):
        hits = {k: v for k, v in self.grid.items()
                if row_from <= k[0] <= row_to and col_from <= k[1] <= col_to}
        if with_format:
            return {k: {"text": str(v), "fill": ""} for k, v in hits.items()}
        return hits

    def read_formulas(self, f, w, r1, r2, c1, c2):
        return {k: str(v) for k, v in self.grid.items()
                if r1 <= k[0] <= r2 and c1 <= k[1] <= c2 and str(v).startswith("=")}

    def write_cells(self, file_id, worksheet_id, cells):
        self.writes.append([dict(c) for c in cells])
        for c in cells:
            self.grid[(int(c["row"]) - 1, int(c["col"]) - 1)] = str(c["value"])

    def insert_rows(self, f, w, *, row, count):
        self.grid = {(r + count if r >= row - 1 else r, c): v
                     for (r, c), v in self.grid.items()}

    def delete_rows(self, f, w, *, row, count):
        return None

    def write_format_ops(self, f, w, ops):
        return None

    def sort_range(self, f, w, **kw):
        return None

    def delete_columns(self, f, w, **kw):
        return None

    def read_cell_format(self, f, w, row, col):
        return None


def make_grid(target, rows):
    header = {NAME: "名字", ADDR: "地址", PHONE: "电话",
              DATE: f"{target.month}.{target.day} {WEEKDAYS[target.weekday()]}",
              TYPE: "类型", KIND: "餐种", TOTAL: "总餐次",
              SERVED: "已出餐", LEFT: "剩余餐", REMARK: "备注"}
    grid = {(1, c): t for c, t in header.items()}
    for offset, row in enumerate(rows):
        for col, text in row.items():
            if str(text) != "":
                grid[(2 + offset, col)] = str(text)
    return grid


def write_local(excel, target, *, name, phone, meals, address="西溪北苑"):
    from openpyxl import load_workbook
    from app.order.templates import write_order_template
    write_order_template(excel)
    wb = load_workbook(excel)
    try:
        ws = wb[SHEET]
        ws.cell(row=3, column=1, value="A-1")
        ws.cell(row=3, column=2, value=name)
        ws.cell(row=3, column=3, value=address)
        ws.cell(row=3, column=4, value=phone)
        ws.cell(row=3, column=5 + target.weekday(), value="1")
        ws.cell(row=3, column=12, value="中餐")
        ws.cell(row=3, column=13, value="经济")
        ws.cell(row=3, column=14, value=meals)
        wb.save(excel)
    finally:
        wb.close()


def marked_rows(cloud, name):
    rows = []
    by_row = {}
    for (r, c), v in cloud.grid.items():
        by_row.setdefault(r + 1, {})[c] = str(v)
    for row in sorted(by_row):
        cells = by_row[row]
        if cells.get(NAME) == name and str(cells.get(DATE, "")).strip():
            rows.append(row)
    return rows


def run(*, mutate_old_wiring: bool) -> int:
    from app.api import bridge as bridge_module
    from app.api.bridge import Bridge
    from app.wps.sync import SyncLedger, target_date_for

    tmp = Path(tempfile.mkdtemp(prefix="r7-w1-"))
    bridge = Bridge(config_path=str(tmp / "config.json"), is_admin=True)
    target = target_date_for(start_hour=bridge._config.wps_target_hour_start,
                             end_hour=bridge._config.wps_target_hour_end)
    excel = tmp / "排单.xlsx"
    write_local(excel, target, name="合成客户", phone="13800000000", meals=2)

    cloud = SyntheticCloud(make_grid(target, [
        {NAME: "合成客户", ADDR: "西溪北苑", PHONE: "13800000000",
         TYPE: "中餐", KIND: "经济", TOTAL: "5"},
    ]))
    ledger_path = tmp / "wps_sync_state.json"

    bridge._config.excel_path = excel
    bridge._config.wps_enabled = True
    bridge._config.wps_test_mode = False
    bridge._config.wps_marker_enabled = False
    bridge._config.wps_sort_enabled = False
    bridge._config.wps_tables = {SHEET: {"file_id": FILE_ID}}
    bridge._config.wps_production_tables = {SHEET: {"file_id": FILE_ID}}

    original_ledger = bridge_module.SyncLedger
    bridge_module.SyncLedger = lambda *a, **k: SyncLedger(ledger_path)
    bridge._wps_cli = lambda: cloud

    if mutate_old_wiring:
        # 还原 R6 旧接线需要**同时**关掉两层保护，缺一不可（这本身也验证了
        # 修复方“两层各自独立可拦”的说法）：
        #   层 B：apply_plan 内 plan.ledger_digest vs 锁内磁盘摘要；
        #   层 A：Bridge 复用“建计划时那份账本对象”的加载摘要比较 —— 旧接线
        #         在预览上下文和执行两处各新建一个 SyncLedger()，比较退化为
        #         “同一时刻的两份相同快照”。
        import app.wps.executor as executor_module
        executor_module._plan_ledger_digests = lambda plans: set()
        print("[MUTATION] 已关闭层 B（计划快照比较）")

    preview = bridge.wps_preview()
    if not preview.get("ok"):
        print("PREVIEW_FAILED", preview)
        return 2

    # ---- 竞争：B 正在 upload 的窗口内，进程 A 完成一次真实写入 + 账本提交 ----
    real_read_plans = bridge._wps_read_plans
    injected = {"done": False}

    def racing_read_plans(bundle):
        if mutate_old_wiring:
            # 层 A 变异：让计划与执行各自新建账本（R6 旧接线）。
            bundle.pop("ledger", None)
        plans, error = real_read_plans(bundle)
        if error is not None or plans is None or injected["done"]:
            return plans, error
        injected["done"] = True
        # A：同一目标日期/表的另一次提交（真实账本写入 + 云端标记）。
        # 注意：这里直接改 cloud.grid，不走 cloud.write_cells，否则探针会把
        # 自己注入的写入误记成“被测代码写了云端”（R7 自查发现的度量陷阱）。
        ledger = SyncLedger(ledger_path)
        ledger.record(target.isoformat(), FILE_ID,
                      {"合成客户\x0013800000000": {"local": 2, "slots": [1, 1],
                                                   "total": 2}})
        ledger.save()
        cloud.grid[(2, DATE)] = "1"
        cloud.grid[(2, TOTAL)] = "1"
        injected["writes_baseline"] = len(cloud.writes)
        print(f"[race] 进程 A 已提交：账本 -> {ledger_path.name}，云端第 3 行已标 1")
        return plans, error

    bridge._wps_read_plans = racing_read_plans
    result = bridge.wps_upload(preview["preview_id"])
    before_rows = marked_rows(cloud, "合成客户")
    # 竞争注入后的基线（A 的写入不算被测代码的写入）
    before_writes = injected["writes_baseline"]
    after_rows = marked_rows(cloud, "合成客户")
    new_writes = len(cloud.writes) - before_writes

    bridge_module.SyncLedger = original_ledger

    ok = bool(result.get("ok"))
    status = str(result.get("status") or "")
    code = str(result.get("code") or "")
    print(f"[probe W1] 云端标记行 before={before_rows} after={after_rows}")
    print(f"[probe W1] apply 期间新增云端写入批次 = {new_writes}")
    print(f"[probe W1] upload ok={ok} status={status} code={code}")
    print(f"[probe W1] next_action={result.get('next_action')}")

    refused = (not ok) and new_writes == 0 and after_rows == before_rows
    print(f"[probe W1] 判定 = {'REFUSED（零写入，安全）' if refused else 'WROTE/ACCEPTED（不安全）'}")
    if mutate_old_wiring:
        print("[probe W1] 期望（变异后）= WROTE/ACCEPTED，用于证明探针能抓回归")
        return 0 if not refused else 1
    return 0 if refused else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutate-old-wiring", action="store_true")
    args = parser.parse_args()
    code = run(mutate_old_wiring=args.mutate_old_wiring)
    print(f"[probe W1] exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
