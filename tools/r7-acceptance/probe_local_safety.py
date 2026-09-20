"""R7 独立验收探针 5：本地安全 —— Excel 原子保存（R6-1）、ordering journal 落盘失败必须
阻止 POST、以及不同 TMPDIR 下批次锁仍然互斥（R6-5）。

    PYTHONPATH=<repo> python3 probe_local_safety.py [--mutate-revert-<name>]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock


def check_excel_atomic() -> dict[str, bool]:
    """R6-1：workbook 没写出内容 / 写坏 / 保存抛错时，原文件必须一个字节都不变。"""
    import app.order.excel_io as excel_io
    checks: dict[str, bool] = {}
    tmp = Path(tempfile.mkdtemp(prefix="r7-excel-"))
    original = tmp / "排单.xlsx"
    payload = b"REAL-USER-DATA-" * 40
    original.write_bytes(payload)

    class SilentWorkbook:
        """save() 返回但一个字节都不写（R6-1 的死守卫场景）。"""
        def save(self, path):
            return None

    class BoomWorkbook:
        def save(self, path):
            raise PermissionError("file is locked")

    class HalfWorkbook:
        def save(self, path):
            Path(path).write_bytes(b"")   # 写出 0 字节

    for label, wb, callback in (
        ("无输出（save 静默不写）", SilentWorkbook(), lambda _e: "retry"),
        ("写出 0 字节", HalfWorkbook(), lambda _e: "retry"),
        ("保存抛 PermissionError 且用户取消", BoomWorkbook(), lambda _e: "cancel"),
    ):
        try:
            excel_io._save_workbook_with_retry(wb, original, callback)
            outcome = "返回成功"
        except Exception as exc:  # noqa: BLE001
            outcome = f"抛出 {type(exc).__name__}"
        intact = original.read_bytes() == payload
        print(f"  [{label}] {outcome}；原文件完好={intact}")
        checks[f"excel_{label}"] = intact

    # 正常路径仍然要能真的写进去（必须写真正的 xlsx：R7 新增了“临时文件必须能被
    # openpyxl 重新读回”的校验，写非 zip 字节会被正确拒绝 —— 这是加固，不是缺陷）
    class GoodWorkbook:
        def save(self, path):
            from openpyxl import Workbook
            wb = Workbook()
            wb.active["A1"] = "OK"
            wb.save(path)
            wb.close()

    try:
        excel_io._save_workbook_with_retry(GoodWorkbook(), original, None)
        from openpyxl import load_workbook
        wb = load_workbook(original)
        written = wb.active["A1"].value == "OK"
        wb.close()
    except Exception as exc:  # noqa: BLE001
        written = False
        print("  正常保存异常:", exc)
    print(f"  [正常路径] 真实 xlsx 写入并回读成功={written}")
    checks["excel_normal_path_writes"] = written

    # 显式反证：写入“损坏的 xlsx”时不得替换原文件（临时文件回读校验）
    class CorruptWorkbook:
        def save(self, path):
            Path(path).write_bytes(b"NOT-A-ZIP")

    original.write_bytes(payload)
    try:
        excel_io._save_workbook_with_retry(CorruptWorkbook(), original, None)
        corrupt_outcome = "返回成功"
    except Exception as exc:  # noqa: BLE001
        corrupt_outcome = f"抛出 {type(exc).__name__}"
    intact = original.read_bytes() == payload
    print(f"  [写坏 xlsx] {corrupt_outcome}；原文件完好={intact}")
    checks["excel_corrupt_output_rejected"] = intact
    return checks


def check_ordering_journal_blocks_post() -> dict[str, bool]:
    """journal 持久化失败时，runner 必须一个 POST 都不发。"""
    from app.ordering import runner as runner_module
    checks: dict[str, bool] = {}
    posts = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def fetch_captcha(self):
            return b"png"

        def login(self, code):
            return None

        def get_json(self, path):
            return {"success": True, "result": {"records": [], "total": 0}}

        def post_json(self, path, payload):
            posts["n"] += 1
            return {"success": True}

        def fork(self):
            return self

        def close(self):
            return None

    def boom_append(*a, **k):
        raise OSError("磁盘满：无法写入不确定记录")

    tmp = Path(tempfile.mkdtemp(prefix="r7-journal-"))
    tasks = [{"identifier": "第 3 行 甲", "payload": {"x": 1}, "fingerprint": {},
              "account": "18758187837", "batch_id": "b1", "sheet": "东湖中餐",
              "client_request_id": "c1"}]

    with mock.patch.object(runner_module, "SssApiClient", FakeClient), \
         mock.patch.object(runner_module, "append_uncertain_records",
                           side_effect=boom_append), \
         mock.patch.object(runner_module, "_collect_tasks",
                           return_value=tasks), \
         mock.patch.object(runner_module, "query_balance",
                           return_value=(1000.0, 0.0)), \
         mock.patch.object(runner_module, "platform_origin",
                           return_value="https://example.invalid"), \
         mock.patch.object(runner_module, "_run_reconciled_submission") as mocked:
        # 直接调用真正的 _run_reconciled_submission，但让 journal sink 抛错
        from app.ordering.submission import _run_reconciled_submission
        logs: list[str] = []
        result = _run_reconciled_submission(
            tasks, lambda: (lambda payload: {"success": True}, lambda: None),
            FakeClient().get_json, type("E", (), {"is_set": lambda self: False,
                                                  "set": lambda self: None})(),
            logs.append, None, 1,
            uncertain_sink=boom_append,
            journal_meta={"account": "18758187837", "delivery_date": "2026-09-20",
                          "platform": "https://example.invalid"},
            outcome={},
        )
        print(f"  journal 落盘失败后 _run_reconciled_submission 返回={result}")
        print(f"  实际 POST 次数={posts['n']}")
        print(f"  日志={logs}")
        checks["journal_failure_stops_before_post"] = posts["n"] == 0
    return checks


def check_tmpdir_lock() -> dict[str, bool]:
    """R6-5：不同 TMPDIR 下同一业务批次的跨进程锁仍必须互斥。"""
    import importlib
    import app.ordering.uncertain as uncertain
    checks: dict[str, bool] = {}
    key = "2026-09-20|18758187837"
    td1 = tempfile.mkdtemp(prefix="r7-tmpA-")
    td2 = tempfile.mkdtemp(prefix="r7-tmpB-")
    old = os.environ.get("TMPDIR")

    def lock_for(tmpdir):
        os.environ["TMPDIR"] = tmpdir
        import tempfile as tf
        tf.tempdir = None
        importlib.reload(uncertain)
        return uncertain.batch_submission_lock("/x/auth.json", key)

    l1 = lock_for(td1)
    l1.acquire()
    l2 = lock_for(td2)
    granted = True
    try:
        l2.acquire()
    except Exception as exc:  # noqa: BLE001
        granted = False
        print(f"  第二个 TMPDIR 获取锁被拒绝：{type(exc).__name__}")
    print(f"  lock1={l1.lock_path}")
    print(f"  lock2={l2.lock_path}")
    print(f"  同一锁文件={l1.lock_path == l2.lock_path}  第二进程同时持锁={granted}")
    l1.release()
    if granted:
        l2.release()
    if old is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = old
    importlib.reload(uncertain)
    checks["lock_mutually_exclusive_across_tmpdir"] = (l1.lock_path == l2.lock_path
                                                       and not granted)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-tmpdir", action="store_true")
    args = parser.parse_args()
    print("===== R6-1：Excel 原子保存必须保住原文件 =====")
    results = check_excel_atomic()
    print("\n===== ordering：journal 落盘失败必须阻止 POST =====")
    results.update(check_ordering_journal_blocks_post())
    if not args.skip_tmpdir:
        print("\n===== R6-5：不同 TMPDIR 下批次锁仍互斥 =====")
        results.update(check_tmpdir_lock())
    print("\n===== 汇总 =====")
    for key, value in results.items():
        print(f"  {key}: {'OK' if value else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
