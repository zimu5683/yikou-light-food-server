"""金山 kdocs-cli 的查找、授权状态与命令封装。"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.wps.common import (
    HEADER_ROW,
    MAX_SCAN_COL,
    RATE_LIMIT_CODES,
    WRITE_BATCH_CELLS,
    _find_column,
    _is_transient,
)
from app.wps.errors import WpsCloudError
from app.wps import android_runtime

CLI_NAME = "kdocs-cli"

CLI_NAME_WIN = "kdocs-cli.exe"

def find_cli(explicit: str | os.PathLike[str] | None = None) -> str:
    """按优先级查找 kdocs-cli：显式配置 → 打包内置 → 仓库 vendor → 程序同目录 → PATH。

    APK 内置模式下没有真实可执行文件路径，返回 :data:`android_runtime.RUNTIME_MARKER`；
    真正的 ``proot + kdocs-cli`` 由 Kotlin ``WpsRuntime`` 托管。
    """
    if android_runtime.is_android():
        return android_runtime.RUNTIME_MARKER
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    names = [CLI_NAME_WIN, CLI_NAME] if os.name == "nt" else [CLI_NAME]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        for name in names:
            candidates.append(Path(bundle) / name)
    # 源码运行：仓库内的 vendor/kdocs-cli/
    repo_vendor = Path(__file__).resolve().parents[2] / "vendor" / "kdocs-cli"
    for name in names:
        candidates.append(repo_vendor / name)
    exe_dir = Path(sys.executable).parent
    for name in names:
        candidates.append(exe_dir / name)
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    raise WpsCloudError(
        "找不到 kdocs-cli 组件。请确认程序完整安装，或在「云文档同步」里手动指定路径。")

def effective_tables(config: Any) -> dict[str, dict[str, str]]:
    """返回当前实际生效的云端目标表，并拒绝过期或越权目标。

    测试模式一律写测试副本（且副本 ID 不能是正式表、也不能是已废弃的试验田）；
    正式模式只允许写正式表 ID。「闪时送下单」的云端名单读取同样走这里，
    这样测试模式下读的也是副本，不会拿正式表的数据做实验。
    """
    production = {sheet: conf.get("file_id", "") for sheet, conf in
                  (getattr(config, "wps_production_tables", None) or {}).items()}
    legacy_test_ids = {
        "H8vzKoTJVrMP7mA9QG591xqS9W8Bg57iG",
        "qFBgqf13GxM7vPUbTSJmxxsrgopD4DnpA",
        "p9P2p2NFfxMZjLXGZ1fyxxrFTBf9s81Kn",
        "RBLtXB8x3rMcQhCp6zp11xGBN7Wey2xCD",
        "afnJ5h5Di1M3U9VwX3rvxx9jpTn8EUw9o",
        "rxYTF8Juk9MBbhkbfjE9Bx1dQ3vGeZ3zr",
    }
    if bool(getattr(config, "wps_test_mode", False)):
        test = {sheet: str(fid).strip() for sheet, fid in
                (getattr(config, "wps_test_tables", None) or {}).items()
                if str(fid).strip()}
        if not test:
            raise WpsCloudError("测试模式未配置新的测试副本，拒绝写入；请先从正式表创建副本")
        # 注意：必须比对正式表的 **file_id 值**，而不是 dict 的键（键是子表名）。
        stale = {fid for fid in test.values() if fid in set(production.values())}
        if stale:
            raise WpsCloudError("测试副本配置包含正式表 ID，拒绝写入")
        if set(test.values()) & legacy_test_ids:
            raise WpsCloudError("测试副本配置包含已过期试验田 ID，拒绝写入")
        return {sheet: {"file_id": fid} for sheet, fid in test.items()}
    active = {sheet: dict(conf) for sheet, conf in
              (getattr(config, "wps_tables", None) or {}).items()}
    active_ids = {conf.get("file_id", "") for conf in active.values()}
    if active_ids - set(production.values()):
        raise WpsCloudError("正式模式目标包含非正式表 ID，拒绝写入")
    return active

def termux_cli_runtime() -> tuple[list[str], dict[str, str]]:
    """Termux/Android 上运行 kdocs-cli 需要的 (命令前缀, 额外环境变量)。

    kdocs-cli 是**静态链接**的 linux/arm64 Go 程序，不经过 Termux 对绝对路径的
    重写（那套机制依赖动态链接器），因此在 Android 上会遇到两个必然失败的问题：

    1. 读不到 ``/etc/resolv.conf``。Android 的 ``/etc`` 是指向只读 ``/system/etc``
       的符号链接，里面没有 resolv.conf；纯 Go 解析器因此退化成只查本机 DNS，
       所有请求都以 ``lookup ... connection refused`` 失败。
       用 ``proot`` 把 Termux 的 resolv.conf 绑定到 ``/etc/resolv.conf`` 解决。
    2. 读不到 ``/etc/ssl/certs/ca-certificates.crt``（Termux 的 CA 包在
       ``$PREFIX/etc/tls/cert.pem``），证书池为空，每个 HTTPS 请求都报
       ``x509: certificate signed by unknown authority``。
       用 ``SSL_CERT_FILE`` 指过去解决。

    非 Termux 环境返回空前缀与空环境变量，桌面端行为完全不变。
    """
    prefix = os.environ.get("PREFIX", "")
    if "com.termux" not in prefix:
        return [], {}
    extra_env: dict[str, str] = {}
    ca_bundle = Path(prefix) / "etc" / "tls" / "cert.pem"
    if ca_bundle.is_file():
        extra_env["SSL_CERT_FILE"] = str(ca_bundle)
    resolv_conf = Path(prefix) / "etc" / "resolv.conf"
    proot = shutil.which("proot")
    # 只有当系统真的缺 /etc/resolv.conf 时才套 proot（有则无需付出开销）。
    if proot and resolv_conf.is_file() and not Path("/etc/resolv.conf").exists():
        return [proot, "-b", f"{resolv_conf}:/etc/resolv.conf"], extra_env
    return [], extra_env

class KdocsCli:
    """kdocs-cli 的最小封装。"""

    def __init__(self, cli_path: str | os.PathLike[str] | None = None,
                 *, timeout: int = 300, token: str | None = None) -> None:
        self.path = find_cli(cli_path)
        self.timeout = timeout
        self.token = token or os.environ.get("KINGSOFT_DOCS_TOKEN")

    # ---- 底层 ----

    def _run(self, *args: str, params: Mapping[str, Any] | None = None,
             retries: int = 2) -> dict[str, Any]:
        """调用 kdocs-cli 并解析 JSON。

        网络抖动（TLS handshake timeout / connection reset）会重试 ``retries`` 次 ——
        实测写入过程中偶发 TLS 超时，一次失败就让整张表判定失败代价太大。
        接口业务错误（code != 0）不重试。
        """
        last_error: WpsCloudError | None = None
        for attempt in range(retries + 1):
            try:
                return self._run_once(*args, params=params)
            except WpsCloudError as exc:
                if not _is_transient(exc):
                    raise
                last_error = exc
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _run_once(self, *args: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        # APK 内置模式：命令行、proot、DNS/CA、保留 token 都由 Kotlin 处理。
        if android_runtime.is_android():
            try:
                result = android_runtime.run_cli(
                    args, params, timeout_ms=max(1, int(self.timeout)) * 1000)
            except android_runtime.AndroidRuntimeError as exc:
                raise WpsCloudError(
                    f"Android 运行时不可用（{exc.error_code}）：{exc}") from exc
            return self._parse_cli_output(
                result.stdout, result.stderr, args,
                exit_code=result.exit_code, timed_out=result.timed_out,
                error_code=result.error_code)

        # Termux/Android 需要 proot 绑 resolv.conf + SSL_CERT_FILE；桌面端两项都为空。
        prefix, extra_env = termux_cli_runtime()
        cmd = [*prefix, self.path, *args]
        if self.token:
            cmd += ["--token", self.token]
        tmp: str | None = None
        if params is not None:
            # 关键：参数走临时文件，避免命令行长度上限（约 128 KiB）
            fd, tmp = tempfile.mkstemp(prefix="kdocs-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(params, fh, ensure_ascii=False)
            cmd += ["--file", tmp]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout,
                                  env={**os.environ, **extra_env} if extra_env else None)
        except FileNotFoundError as exc:
            raise WpsCloudError(f"无法执行 kdocs-cli：{exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise WpsCloudError(f"kdocs-cli 超时（{self.timeout}s）：{' '.join(args)}") from exc
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        return self._parse_cli_output(
            proc.stdout or "", proc.stderr or "", args,
            exit_code=proc.returncode, timed_out=False, error_code=None)

    def _parse_cli_output(self, stdout: str, stderr: str, args: tuple[str, ...], *,
                          exit_code: int, timed_out: bool = False,
                          error_code: str | None = None) -> dict[str, Any]:
        """解析 kdocs-cli 的 JSON 输出；桌面与 Android 两条路径共用。"""
        if timed_out:
            raise WpsCloudError(f"kdocs-cli 超时（{self.timeout}s）：{' '.join(args)}")
        raw = (stdout or "").strip()
        payload: dict[str, Any] | None = None
        if raw.startswith("{"):
            try:
                payload, _ = json.JSONDecoder().raw_decode(raw)
            except json.JSONDecodeError:
                payload = None
        if payload is None:
            hint = (stderr or raw or "").strip()[:300]
            if error_code:
                hint = f"{error_code}: {hint}".strip(": ")
            raise WpsCloudError(f"kdocs-cli 无有效输出（exit {exit_code}）：{hint}")
        # 注意：CLI 在接口报错时退出码仍可能是 0，必须看 code 字段
        code = payload.get("code")
        if code in RATE_LIMIT_CODES:
            # 429001 = 当日总量用尽；429002 = 短时间频繁触发，均次日 08:00 恢复。
            # 接口返回的 reset_at 时区口径不稳定（实测与提示文案差 8 小时），
            # 因此只显示"还有多久"，不显示具体时点，避免误导。
            detail = payload.get("data") or {}
            when = ""
            reset_at = detail.get("reset_at")
            if isinstance(reset_at, (int, float)) and reset_at > 0:
                remain = reset_at - _dt.datetime.now().timestamp()
                if remain > 0:
                    hours, minutes = divmod(int(remain // 60), 60)
                    when = f"，约 {hours} 小时 {minutes} 分钟后恢复"
            elif detail.get("retry_after"):
                when = f"，约 {int(detail['retry_after']) // 60} 分钟后可再试"
            raise WpsCloudError(
                "今日云文档调用额度已用尽（金山接口限流）"
                f"{when}。这不是程序故障：读表、写表、搜索都会受限，"
                "本地排单任务不受影响。")
        if code not in (0, None):
            raise WpsCloudError(
                f"云文档接口返回 code={code}：{payload.get('message') or payload.get('msg')}")
        data = payload.get("data", payload)
        return data if isinstance(data, dict) else {"data": data}
    # ---- 认证 ----

    def authenticated(self) -> bool:
        """kdocs-cli 是否已授权（跑 ``auth status``）；命令缺失或超时按未授权处理。"""
        if android_runtime.is_android():
            try:
                return android_runtime.auth_status()
            except android_runtime.AndroidRuntimeError as exc:
                raise WpsCloudError(
                    f"Android 运行时不可用（{exc.error_code}）：{exc}") from exc
        try:
            proc = subprocess.run([self.path, "auth", "status"],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return False
        try:
            return bool(json.loads(proc.stdout.strip()).get("authenticated"))
        except (json.JSONDecodeError, AttributeError):
            return False

    def login_argv(self) -> list[str]:
        """返回可交给调用方在终端/新窗口里执行的授权命令。

        在 Termux/Android 上会带上 ``proot`` 前缀：``auth login`` 原本会因 Android
        seccomp 拦截 ``faccessat2`` 而以 ``SIGSYS: bad system call`` 崩溃，
        proot 接管系统调用后即可正常走完 OAuth 流程。

        APK 内置模式不走这里：授权由 Kotlin ``WpsRuntime.authorize`` 拉起浏览器。
        """
        if android_runtime.is_android():
            raise WpsCloudError(
                "APK 内置模式的 WPS 授权请调用 WpsRuntime.authorize()")
        prefix, _ = termux_cli_runtime()
        return [*prefix, self.path, "auth", "login"]

    def login_env(self) -> dict[str, str] | None:
        """授权命令需要的环境变量（Termux 下需要 ``SSL_CERT_FILE``），无需时返回 None。"""
        if android_runtime.is_android():
            return None
        _, extra_env = termux_cli_runtime()
        return {**os.environ, **extra_env} if extra_env else None

    def logout(self) -> bool:
        """退出授权；成功返回 True。Android 模式走 Kotlin WpsRuntime.logout。"""
        if android_runtime.is_android():
            return android_runtime.logout().ok
        prefix, extra_env = termux_cli_runtime()
        try:
            proc = subprocess.run([*prefix, self.path, "auth", "logout"],
                                  capture_output=True, text=True, timeout=60,
                                  env={**os.environ, **extra_env} if extra_env else None)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    # ---- 表格读写 ----

    def sheets_info(self, file_id: str) -> list[dict[str, Any]]:
        """读取在线表格的子表信息列表（``sheetsInfo``）；读不到返回空列表。"""
        data = self._run("sheet", "get-sheets-info", params={"file_id": file_id})
        detail = data.get("detail") or {}
        return detail.get("sheetsInfo") or []

    def read_grid(self, file_id: str, worksheet_id: int,
                  row_from: int, row_to: int,
                  col_from: int, col_to: int,
                  *, with_format: bool = False) -> dict[tuple[int, int], str]:
        """读取矩形区域，返回 {(0-based 行, 0-based 列): cellText}。

        接口返回里没有 ``detail`` 说明这张表读不了（例如是二进制 xlsx 而非在线
        表格），此时抛错而不是返回空结果 —— 否则调用方会把"读不了"误判成"表是空的"。
        """
        data = self._run("sheet", "get-range-data", params={
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range": {"rowFrom": row_from, "rowTo": row_to,
                      "colFrom": col_from, "colTo": col_to}})
        if not isinstance(data, dict) or not isinstance(data.get("detail"), dict):
            raise WpsCloudError(
                f"表格内容读取失败（file_id={file_id}）：{str(data)[:200]}")
        cells = data["detail"].get("rangeData") or []
        grid: dict[tuple[int, int], str] = {}
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            text = cell.get("cellText")
            if text in (None, ""):
                continue
            if with_format:
                key = (int(cell.get("originRow", 0)), int(cell.get("originCol", 0)))
                grid[key] = {"text": str(text),
                             "fill": (cell.get("cell_background_color")
                                      or cell.get("fill") or "")}
                continue
            grid[(int(cell.get("originRow", 0)), int(cell.get("originCol", 0)))] = str(text)
        return grid

    def read_row(self, file_id: str, worksheet_id: int, row: int,
                 col_from: int = 0, col_to: int = MAX_SCAN_COL - 1) -> dict[int, str]:
        """读一整行，返回 {0-based 列号: 文本}（表头解析用，避免坐标元组混淆）。

        ``row`` 为 **1-based** 行号（与 Excel 一致）。
        """
        grid = self.read_grid(file_id, worksheet_id, row - 1, row - 1, col_from, col_to)
        return {col: text for (_r, col), text in grid.items()}

    def find_column(self, file_id: str, worksheet_id: int, names: Sequence[str],
                    row: int = HEADER_ROW) -> int | None:
        """按表头文字找列，返回 **1-based** 列号；找不到返回 None。"""
        return _find_column(self.read_row(file_id, worksheet_id, row), names)

    def write_cells(self, file_id: str, worksheet_id: int,
                    cells: Sequence[Mapping[str, Any]]) -> None:
        """写入多个单元格，自动按接口上限分批。

        cells 每项：{"row": 1-based 行, "col": 1-based 列, "value": str}

        实测：``update-range-data`` 单次 ``rangeData`` 最多 **100** 项，超出返回
        ``400001 rangeData length N exceeds limit 100``。排单表追加新客户时
        单元格数很容易过百（东湖中餐一次 25 人 ≈ 175 格），因此这里必须分批。
        """
        if not cells:
            return
        pending = list(cells)
        for start in range(0, len(pending), WRITE_BATCH_CELLS):
            batch = pending[start:start + WRITE_BATCH_CELLS]
            range_data = [{
                "opType": "formula",
                "rowFrom": int(c["row"]) - 1, "rowTo": int(c["row"]) - 1,
                "colFrom": int(c["col"]) - 1, "colTo": int(c["col"]) - 1,
                "formula": str(c["value"]),
            } for c in batch]
            self._run("sheet", "update-range-data", params={
                "file_id": file_id, "worksheet_id": worksheet_id,
                "rangeData": range_data})

    def read_formulas(self, file_id: str, worksheet_id: int,
                      row_from: int, row_to: int,
                      col_from: int, col_to: int) -> dict[tuple[int, int], str]:
        """读取指定区域的公式本体（而不是计算后的显示值）。"""
        data = self._run("sheet", "get-range-data", params={
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range": {"rowFrom": row_from, "rowTo": row_to,
                      "colFrom": col_from, "colTo": col_to}})
        cells = (data.get("detail") or {}).get("rangeData") or {}
        return {(int(c.get("originRow", 0)), int(c.get("originCol", 0))): str(c["fmlaText"])
                for c in cells if isinstance(c, dict) and c.get("fmlaText")}

    # ---- 格式 ----

    def insert_rows(self, file_id: str, worksheet_id: int, *,
                    row: int, count: int) -> None:
        """在 1-based 行号 ``row`` 之前插入 ``count`` 个空行。

        新行占据 row..row+count-1，原有内容（含公式、底色）整体下移。
        接口参数是 0-based 闭区间：row_from = row_to = row - 1 + count - 1。
        """
        if count <= 0:
            return
        self._run("sheet", "insert-rows-cols", params={
            "file_id": file_id, "worksheet_id": worksheet_id, "type": "row",
            "row_from": row - 1, "row_to": row - 1 + count - 1})

    def delete_rows(self, file_id: str, worksheet_id: int, *,
                    row: int, count: int) -> None:
        """删除 1-based 行号 ``row`` 起的 ``count`` 行（插入失败时的回滚手段）。"""
        if count <= 0:
            return
        self._run("sheet", "delete-range-data", params={
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range_data": [{
                "col_from": 0, "col_to": 16383,
                "row_from": row - 1, "row_to": row - 1 + count - 1}],
            "shift_type": "shift_up"})

    def delete_columns(self, file_id: str, worksheet_id: int, *,
                       column: int, rows: int) -> None:
        """删除一整列（排序辅助列的收尾清理）。``column`` 为 1-based 列号。

        辅助列一定在该表所有内容列的右侧，所以左移删除不会动到任何数据。
        """
        self._run("sheet", "delete-range-data", params={
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range_data": [{
                "col_from": column - 1, "col_to": column - 1,
                "row_from": 0, "row_to": max(0, rows - 1)}],
            "shift_type": "shift_left"})

    def write_format_ops(self, file_id: str, worksheet_id: int,
                         ops: Sequence[Mapping[str, Any]]) -> None:
        """批量写格式操作（opType=format），按接口单次上限自动分批。"""
        if not ops:
            return
        pending = [dict(op) for op in ops]
        for start in range(0, len(pending), WRITE_BATCH_CELLS):
            self._run("sheet", "update-range-data", params={
                "file_id": file_id, "worksheet_id": worksheet_id,
                "rangeData": pending[start:start + WRITE_BATCH_CELLS]})

    def read_cell_format(self, file_id: str, worksheet_id: int,
                         row: int, col: int) -> dict[str, Any] | None:
        """读取单个单元格的格式（1-based 行列）；空单元格返回 None。

        只用于"学一行参考格式"。注意：**只有带内容的格才会被接口返回**，
        所以调用方要挑一个确实有值的格子。
        """
        data = self._run("sheet", "get-range-data", params={
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range": {"rowFrom": row - 1, "rowTo": row - 1,
                      "colFrom": col - 1, "colTo": col - 1}})
        detail = data.get("detail") if isinstance(data, dict) else None
        cells = (detail or {}).get("rangeData") or []
        for cell in cells:
            if isinstance(cell, dict) and cell.get("cellText") not in (None, ""):
                return cell
        return None

    def sort_range(self, file_id: str, worksheet_id: int, *, range_ref: str,
                   key: str, order: str = "asc", header: bool = True,
                   key2: str | None = None, order2: str | None = None) -> None:
        """原地排序（供后续功能使用）。``range_ref`` 形如 ``A3:L42``。"""
        params: dict[str, Any] = {
            "file_id": file_id, "worksheet_id": worksheet_id,
            "range": range_ref, "key": key, "order": order, "header": header}
        if key2:
            params["key2"] = key2
        if order2:
            params["order2"] = order2
        self._run("sheet", "range-sort", params=params)

    def list_files(self, drive_id: str, parent_id: str = "0",
                   page_size: int = 200) -> list[dict[str, Any]]:
        """列出云盘目录下的文件；接口两种返回形态都兼容，取不到时返回空列表。"""
        data = self._run("drive", "list-files", params={
            "drive_id": drive_id, "parent_id": parent_id, "page_size": page_size})
        return data.get("data", {}).get("items") or data.get("items") or []
