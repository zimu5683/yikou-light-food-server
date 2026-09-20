"""R8-S3 专项测试/探针共用装置：显式权威位置切换场景。

在 R8-S1 装置（本地模拟平台、网络守卫、合成证书/配置、子进程）之上只加两件事：

* 构造带 ``sss_authoritative_uncertain_path``（config 字段）或
  ``YIKOU_SSS_AUTHORITATIVE_PATH``（环境变量）的合成配置；
* 启动一个可指定显式权威路径的隔离子进程（重启/并发场景）。

不修改 R8-S1 的装置文件；所有请求仍只发往回环模拟平台。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.r8s1_url_guard_harness import (  # noqa: F401  供测试/探针复用
    ACCOUNT,
    REPO_ROOT,
    SYNTH_HOST,
    MockPlatform,
    active_records,
    child_result,
    fixed_clock,
    journal_records,
    loopback_guard,
    make_config,
    run_job,
    try_scope,
    write_synthetic_excel,
    write_test_certs,
)

LOCATION_CHILD = REPO_ROOT / "tools" / "r8s3-fix" / "guard_child_locations.py"
REGISTRY_FILENAME = "sss-authority-locations.json"


def make_location_config(work: str | os.PathLike[str], url: str, *,
                         authoritative_path: str | None = None,
                         account: str = ACCOUNT,
                         read_timeout: float = 1.0) -> SimpleNamespace:
    """合成配置；``authoritative_path`` 非空 = config 字段形式的显式覆盖。"""
    config = make_config(work, url, account=account, read_timeout=read_timeout)
    if authoritative_path is not None:
        config.sss_authoritative_uncertain_path = str(authoritative_path)
    return config


def run_location_job(config: Any, **kwargs: Any) -> dict[str, Any]:
    """真实 ``run_sss_job``（与 R8-S1 装置同一固定时钟/开关）。"""
    return run_job(config, **kwargs)


def registry_path(config: Any = None) -> Path:
    """当前登记文件路径（与生产 ``authority_locations_path`` 一致）。"""
    from app.ordering import uncertain as sss_uncertain

    return sss_uncertain.authority_locations_path(config)


def read_registry(config: Any = None) -> dict[str, Any]:
    target = registry_path(config)
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"_unreadable": True, "_path": str(target)}


def registered_paths(config: Any = None) -> list[str]:
    payload = read_registry(config)
    return [str(item.get("path") or "") for item in payload.get("locations", [])
            if isinstance(item, dict)]


def write_registry(config: Any, locations: list[str], *,
                   version: int = 1) -> Path:
    """按生产格式写登记文件（用于“运维显式声明旧位置”的升级场景）。"""
    target = registry_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "version": version,
        "updated_at": "2026-09-16T10:00:00",
        "locations": [{"path": str(item), "kind": "explicit_file",
                       "first_seen_at": "2026-09-16T09:00:00",
                       "last_used_at": "2026-09-16T09:00:00"}
                      for item in locations],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def write_unresolved_record(path: str | os.PathLike[str], *,
                            journal_id: str = "legacy-1",
                            account: str = ACCOUNT,
                            platform: str = "http://sss.example.invalid",
                            status: str = "unresolved",
                            delivery_date: str = "2026-09-16") -> bytes:
    """写一条合成未决记录（模拟修复前遗留文件）；返回写入后的字节。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "version": 1,
        "records": [{
            "journal_id": journal_id,
            "identifier": "第 3 行 张三",
            "batch_key": f"{delivery_date}|{account}",
            "delivery_date": delivery_date,
            "source": "excel",
            "account": account,
            "platform": platform,
            "fingerprint": {},
            "status": status,
            "error": "ReadTimeout",
            "created_at": f"{delivery_date}T10:00:00",
        }],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return target.read_bytes()


def spawn_location_child(*, work: str | os.PathLike[str], url: str, port: int,
                         authoritative_path: str | None = None,
                         env_authority_path: str | None = None,
                         env_extra: dict[str, str] | None = None,
                         result_name: str | None = None,
                         config_work: str | os.PathLike[str] | None = None,
                         ) -> tuple[Any, Path]:
    """隔离子进程：真实 run_sss_job + 网络守卫 + 指定显式权威路径。

    ``work`` 是**共享**的权威状态/锁/数据根（用于位置切换与竞争场景）；
    ``config_work`` 是每个子进程独立的配置工作目录（合成 Excel 等），避免两个
    进程并发写同一个 xlsx 造成与业务闸门无关的 BadZipFile（R8-S3 §1.1 的教训）。
    """
    root = Path(work)
    if result_name is None:
        result_name = f"location-{uuid.uuid4().hex[:8]}.json"
    result_path = root / result_name
    config_root = Path(config_work) if config_work else root / "config"
    tmp = root / "tmp"
    xdg = root / "xdg"
    tmp.mkdir(parents=True, exist_ok=True)
    xdg.mkdir(parents=True, exist_ok=True)
    config_root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "TMPDIR": str(tmp),
        "XDG_STATE_HOME": str(xdg / "state"),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "YIKOU_DATA_DIR": str(root / "userdata"),
        "YIKOU_SSS_AUTHORITATIVE_ROOT": str(root / "authority-root"),
        "YIKOU_SSS_LOCK_ROOT": str(root / "locks"),
        "YIKOU_SSS_JOURNAL_LOCK_TIMEOUT": "3",
        "R8S3_PORT": str(port),
        "R8S3_URL": url,
        "R8S3_SYNTH_HOST": SYNTH_HOST,
        "R8S3_RESULT": str(result_path),
        "R8S3_WORK": str(root),
        "R8S3_CONFIG_WORK": str(config_root),
    }
    for key in ("YIKOU_SSS_AUTHORITATIVE_PATH", "YIKOU_SSS_UNCERTAIN_PATH",
                "YIKOU_SSS_AUTHORITY_LOCATIONS", "REQUESTS_CA_BUNDLE"):
        env.pop(key, None)
    if authoritative_path:
        env["R8S3_AUTHORITATIVE_PATH"] = str(authoritative_path)
    if env_authority_path:
        env["YIKOU_SSS_AUTHORITATIVE_PATH"] = str(env_authority_path)
    env.update({str(key): str(value) for key, value in (env_extra or {}).items()})
    process = subprocess.Popen(
        [sys.executable, str(LOCATION_CHILD)],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return process, result_path


__all__ = [
    "ACCOUNT", "LOCATION_CHILD", "MockPlatform", "REGISTRY_FILENAME",
    "SYNTH_HOST", "active_records", "child_result", "fixed_clock",
    "journal_records", "loopback_guard", "make_location_config",
    "read_registry", "registered_paths", "registry_path", "run_location_job",
    "spawn_location_child", "try_scope", "write_registry",
    "write_synthetic_excel", "write_test_certs", "write_unresolved_record",
]
