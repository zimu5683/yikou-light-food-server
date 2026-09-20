"""R8-S1 专项测试/探针共用的隔离运行装置。

只做四件事，全部离线：

* :class:`MockPlatform` —— 只监听 ``127.0.0.1`` 的闪时送模拟平台（明文 HTTP 或
  本地自签 HTTPS），记录每次 create-order POST；
* :func:`loopback_guard` —— 网络守卫：只允许回环连接，把合成主机名映射到本地
  模拟端口，任何真实外部连接直接 ``OSError(101)``；
* :func:`write_test_certs` —— 合成 CA + 服务器证书（SAN=合成主机名），用于
  本地 HTTPS 链路；
* 合成 Excel / 配置 / 固定时钟下的 ``run_sss_job`` 调用封装。

风险模型声明：模拟平台把不同 URL 写法映射到同一个服务、同一个账号。这是
**风险模型**，代表「两种写法实际是同一平台、同一账号」这一假设，不是对真实
平台身份同源的证明。所有请求只发给回环地址，绝不发起真实网络请求。
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import socket
import ssl
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest import mock

ACCOUNT = "18758187001"
SYNTH_HOST = "sss.example.invalid"
CREATE_PATH = "/consumer/order/one-touch-send/create-order-from-client"
LIST_PATH = "/consumer/order/one-touch-send/list"
ACCOUNT_PATH = "/consumer/account/get-login-user-account"
CREATE_PATH_FRAGMENT = "create-order-from-client"

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD_SCRIPT = REPO_ROOT / "tools" / "r8s1-fix" / "guard_child.py"
SUBMISSION_STATE = "submission-result.json"


class MockPlatform:
    """本地模拟闪时送平台：同一服务、同一账号，仅监听回环。"""

    def __init__(self, *, list_visible: bool = False, first_post_delay: float = 0.0,
                 state_path: str | os.PathLike[str] | None = None) -> None:
        self.list_visible = bool(list_visible)
        self.first_post_delay = float(first_post_delay)
        self.state_path = Path(state_path) if state_path else None
        self._lock = threading.Lock()
        self.posts: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._httpd_thread: threading.Thread | None = None
        self.scheme = "http"
        self.port = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self, *, tls: bool = False, certfile: str | None = None,
              keyfile: str | None = None,
              host: str = "127.0.0.1") -> "MockPlatform":
        handler = _make_handler(self)
        server = ThreadingHTTPServer((host, 0), handler)
        server.daemon_threads = True
        if tls:
            if not certfile or not keyfile:
                raise ValueError("TLS 模拟平台需要 certfile/keyfile")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile, keyfile)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            self.scheme = "https"
        self._server = server
        self.port = int(server.server_address[1])
        self._httpd_thread = threading.Thread(target=server.serve_forever,
                                              daemon=True)
        self._httpd_thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._httpd_thread is not None:
            self._httpd_thread.join(timeout=5)
            self._httpd_thread = None

    def __enter__(self) -> "MockPlatform":
        if self._server is None:
            self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    # -- state -------------------------------------------------------------
    @property
    def count(self) -> int:
        with self._lock:
            return len(self.posts)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self.posts]

    def reset(self) -> None:
        with self._lock:
            self.posts.clear()
            self.records.clear()

    def state_count(self) -> int:
        """从落盘状态文件读取 POST 次数（跨进程场景用）。"""
        if self.state_path is None or not self.state_path.exists():
            return 0
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return int(payload.get("count") or 0)

    # -- handler helpers ---------------------------------------------------
    def _record_post(self, headers: dict[str, str], body: dict[str, Any]) -> int:
        record = {
            "receiveName": body.get("receiveName"),
            "receivePhone": body.get("receivePhone"),
            "expectedDeliveryTime": body.get("expectedDeliveryTime"),
            "host": headers.get("Host", ""),
        }
        with self._lock:
            seq = len(self.posts) + 1
            self.posts.append({**record, "seq": seq})
            order = {
                "id": f"order-{seq}",
                "receiveName": record["receiveName"],
                "receivePhone": record["receivePhone"],
                "expectedDeliveryTime": record["expectedDeliveryTime"],
                "orderType": body.get("orderType"),
                "storeId": body.get("storeId"),
                "goodsDetail": body.get("goodsDetail"),
                "receiveAddress": body.get("receiveAddress"),
                "account": ACCOUNT,
                "created_at": int(time.time() * 1000),
            }
            self.records.append(order)
            count = len(self.posts)
        if self.state_path is not None:
            _write_state(self.state_path, count)
        return seq


def _write_state(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"count": count}), encoding="utf-8")
    os.replace(tmp, path)


def _make_handler(platform: MockPlatform):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args: Any) -> None:  # noqa: D102
            return

        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/consumer/customer/verify-code"):
                png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
                return
            if self.path.startswith(ACCOUNT_PATH):
                self._json({"success": True,
                            "result": {"totalAmount": 5000.0,
                                       "freezeAmount": 0.0}})
                return
            if self.path.startswith(LIST_PATH):
                if not platform.list_visible:
                    # 模拟“列表最终一致性延迟”：已落单但列表暂时看不到
                    self._json({"success": True, "result": {"records": [],
                                                            "total": 0}})
                    return
                with platform._lock:
                    records = [dict(item) for item in platform.records]
                self._json({"success": True,
                            "result": {"records": records, "total": len(records)}})
                return
            self._json({"success": True, "result": {"records": [], "total": 0}})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if self.path.startswith("/channel/login"):
                self._json({"code": 200, "data": {"token": "tk",
                                                  "uniacid": "uni"}})
                return
            if self.path.startswith("/consumer/customer/password/login"):
                self._json({"success": True,
                            "result": {"token": "t", "uniacid": "u"}})
                return
            if self.path.startswith(CREATE_PATH):
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = {}
                seq = platform._record_post(dict(self.headers), body)
                if seq == 1 and platform.first_post_delay > 0:
                    # 服务端继续执行、客户端读超时 → “已发送未知”
                    time.sleep(platform.first_post_delay)
                self._json({"success": True})
                return
            self._json({"success": True})

    return _Handler


@contextlib.contextmanager
def loopback_guard(port: int, *, hosts: tuple[str, ...] = (SYNTH_HOST,
                                                           SYNTH_HOST + ".")
                   ) -> Iterator[None]:
    """网络守卫：合成主机名→回环；其它连接一律 ``OSError(101)``。"""
    real_gai = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    wanted = {str(item).strip() for item in hosts}

    def guarded_gai(host: Any, port_arg: Any, *args: Any, **kwargs: Any):
        if str(host or "").strip() in wanted:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", int(port)))]
        return real_gai(host, port_arg, *args, **kwargs)

    def guarded_connect(self: socket.socket, address: Any):
        host = address[0] if isinstance(address, tuple) else ""
        if str(host) not in ("127.0.0.1", "::1"):
            raise OSError(101, f"network guard blocked connect to {host!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any):
        host = address[0] if isinstance(address, tuple) else ""
        if str(host) not in ("127.0.0.1", "::1"):
            return 101
        return real_connect_ex(self, address)

    with mock.patch.object(socket, "getaddrinfo", guarded_gai), \
            mock.patch.object(socket.socket, "connect", guarded_connect), \
            mock.patch.object(socket.socket, "connect_ex", guarded_connect_ex):
        yield


def write_test_certs(directory: str | os.PathLike[str],
                     dns_names: tuple[str, ...] = (SYNTH_HOST,)
                     ) -> dict[str, str]:
    """生成合成 CA + 服务器证书（SAN=dns_names），返回 PEM 文件路径。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone.utc)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                            "R8-S1 Offline Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                       critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(
            ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                dns_names[0])])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name).issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(name) for name in dns_names]), critical=False)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=True, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            ca_key.public_key()), critical=False)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.
                                              SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = target / "ca.pem"
    cert_path = target / "server.pem"
    key_path = target / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(server_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return {"ca": str(ca_path), "cert": str(cert_path), "key": str(key_path)}


def write_synthetic_excel(path: str | os.PathLike[str]) -> Path:
    """写一份合成排单表（1 人 1 单，无真实客户数据）。"""
    from openpyxl import Workbook

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "午餐"
    sheet.append(["午餐", None, None, None])
    sheet.append(["姓名", "门牌号", "电话", "送达时间"])
    sheet.append(["张三", "A101", "13800000001", "11:00"])
    workbook.save(target)
    workbook.close()
    return target


def make_config(work: str | os.PathLike[str], url: str, *,
                account: str = ACCOUNT, read_timeout: float = 1.0,
                store_id: int = 211053) -> SimpleNamespace:
    """合成配置：门店/地址走缓存，避免无关 GET；只有模拟平台会收到请求。"""
    root = Path(work)
    excel = write_synthetic_excel(root / "闪时送.xlsx")
    return SimpleNamespace(
        sss_excel_path=str(excel), sss_order_source="excel",
        sss_account=account, sss_dry_run=False, sss_preflight=False,
        sss_store_name="一口轻食", sss_common_address="嗯哼",
        sss_use_fixed_address=True, sss_fixed_lnt=1.0, sss_fixed_lat=2.0,
        sss_fixed_area_code="330110", sss_fixed_address_detail="X",
        sss_product_name="轻食", sss_url=url,
        sss_store_id=store_id, sss_store_name_cached="一口轻食",
        sss_max_workers=1, sss_unit_price=0.0,
        sss_read_timeout_s=read_timeout, sss_idempotency_field="",
        sss_uncertain_path=str(root / "configured-uncertain.json"),
    )


class _Stop:
    def __init__(self) -> None:
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True


@contextlib.contextmanager
def fixed_clock(moment: tuple[int, int, int, int, int, int] = (2026, 9, 16, 10, 0, 0)
                ) -> Iterator[None]:
    """固定 runner 的“现在”，让批次日期/送达日期可复现。"""
    from app.ordering import reconcile as sss_reconcile
    from app.ordering import runner as sss_runner
    from app.ordering import submission as sss_submission

    class _Fixed(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return cls(*moment)

    original_dt = sss_runner._dt.datetime
    prefilter = sss_reconcile._SSS_SERVER_PREFILTER
    poll_interval = sss_reconcile._RECONCILE_POLL_INTERVAL_S
    zero_retry = sss_submission._PREFILTER_ZERO_RETRY_DELAY_S
    sss_runner._dt.datetime = _Fixed
    sss_reconcile._SSS_SERVER_PREFILTER = False
    sss_reconcile._RECONCILE_POLL_INTERVAL_S = 0.0
    sss_submission._PREFILTER_ZERO_RETRY_DELAY_S = 0.0
    try:
        yield
    finally:
        sss_runner._dt.datetime = original_dt
        sss_reconcile._SSS_SERVER_PREFILTER = prefilter
        sss_reconcile._RECONCILE_POLL_INTERVAL_S = poll_interval
        sss_submission._PREFILTER_ZERO_RETRY_DELAY_S = zero_retry


def run_job(config: Any, *, now: tuple[int, int, int, int, int, int] =
            (2026, 9, 16, 10, 0, 0)) -> dict[str, Any]:
    """在当前进程调用真实 ``run_sss_job``（固定时钟、真实安全闸门）。"""
    from app.ordering import runner as sss_runner

    stop = _Stop()
    with fixed_clock(now):
        return sss_runner.run_sss_job(
            config, stop, lambda _message: None, password="synthetic",
            captcha_callback=lambda _image: "1234")


def try_scope(config: Any) -> str:
    """尽力计算 authority scope（探针在修复前/后都可用）。"""
    try:
        from app.ordering import uncertain as sss_uncertain
        return sss_uncertain.authority_scope_key(config)
    except Exception as exc:  # noqa: BLE001 - 探针需要记录失败类型
        return f"<{type(exc).__name__}: {exc}>"


def journal_records(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [record for record in payload.get("records", [])
            if isinstance(record, dict)]


def active_records(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    return [record for record in journal_records(path)
            if str(record.get("status") or "unresolved")
            not in {"resolved", "discarded"}]


def spawn_child(*, work: str | os.PathLike[str], url: str, port: int,
                env_extra: dict[str, str] | None = None,
                ca_bundle: str | None = None,
                child: str | os.PathLike[str] | None = None,
                result_name: str | None = None) -> tuple[Any, Path]:
    """启动一个隔离的闪时送下单子进程（真实 run_sss_job + 网络守卫）。"""
    import subprocess
    import sys
    import uuid

    root = Path(work)
    if result_name is None:
        # 并发/多次启动不能共用一个结果文件（会被后启动的进程覆盖）。
        result_name = f"submission-{uuid.uuid4().hex[:8]}.json"
    result_path = root / result_name
    tmp = root / "tmp"
    xdg = root / "xdg"
    tmp.mkdir(parents=True, exist_ok=True)
    xdg.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        # 不覆盖 HOME：Python 的 user site-packages（openpyxl 等）装在 $HOME 下；
        # 应用状态目录全部由下面的 YIKOU_*/XDG_*/TMPDIR 显式隔离。
        "TMPDIR": str(tmp),
        "XDG_STATE_HOME": str(xdg / "state"),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "YIKOU_DATA_DIR": str(root / "userdata"),
        "YIKOU_SSS_AUTHORITATIVE_ROOT": str(root / "authority-root"),
        "YIKOU_SSS_LOCK_ROOT": str(root / "locks"),
        "YIKOU_SSS_JOURNAL_LOCK_TIMEOUT": "3",
        "R8S1_PORT": str(port),
        "R8S1_URL": url,
        "R8S1_SYNTH_HOST": SYNTH_HOST,
        "R8S1_RESULT": str(result_path),
        "R8S1_WORK": str(root),
    }
    for key in ("YIKOU_SSS_AUTHORITATIVE_PATH", "YIKOU_SSS_UNCERTAIN_PATH",
                "REQUESTS_CA_BUNDLE"):
        env.pop(key, None)
    if ca_bundle:
        env["REQUESTS_CA_BUNDLE"] = ca_bundle
    env.update({str(key): str(value) for key, value in (env_extra or {}).items()})
    process = subprocess.Popen(
        [sys.executable, str(child or CHILD_SCRIPT)],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return process, result_path


def child_result(process: Any, result_path: Path) -> dict[str, Any]:
    """等待子进程结束并返回其 JSON 结果（stdout 末行或结果文件）。"""
    out, err = process.communicate(timeout=90)
    payload: dict[str, Any] = {}
    for line in reversed((out or "").strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
    if result_path.exists():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    payload.setdefault("returncode", process.returncode)
    payload.setdefault("stderr_tail", (err or "")[-400:])
    payload.setdefault("stdout_tail", (out or "")[-400:])
    return payload


def temp_work(prefix: str = "r8s1-guard-") -> Path:
    """在系统临时目录下建独立工作目录（仍会再叠加测试自己的 tmp_path）。"""
    return Path(tempfile.mkdtemp(prefix=prefix))


__all__ = [
    "ACCOUNT", "CHILD_SCRIPT", "CREATE_PATH", "LIST_PATH", "MockPlatform",
    "REPO_ROOT", "SUBMISSION_STATE", "SYNTH_HOST", "active_records",
    "child_result", "fixed_clock", "journal_records", "loopback_guard",
    "make_config", "run_job", "spawn_child", "temp_work", "try_scope",
    "write_synthetic_excel", "write_test_certs",
]
