"""R8-S3 隔离子进程：按环境指定显式权威路径跑一次真实 ``run_sss_job``。

由 ``tests/r8s3_location_guard_harness.py`` 启动：

* ``YIKOU_PS3_PORT``/``R8S3_PORT`` 指向父进程的本地模拟平台；
* ``R8S3_AUTHORITATIVE_PATH`` → config 字段 ``sss_authoritative_uncertain_path``；
* ``YIKOU_SSS_AUTHORITATIVE_PATH`` → 环境变量形式的覆盖；
* 数据目录/默认权威根/锁目录/临时目录全部由父进程指向隔离副本。

只允许回环连接；输出 JSON 到 ``R8S3_RESULT``。
退出码 0 = 正常返回结果；3 = 运行期抛异常。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.r8s1_url_guard_harness import (  # noqa: E402
    SYNTH_HOST, loopback_guard, make_config, run_job, try_scope,
)


def main() -> int:
    port = int(os.environ["R8S3_PORT"])
    url = os.environ["R8S3_URL"]
    work = Path(os.environ["R8S3_WORK"])
    config_work = Path(os.environ.get("R8S3_CONFIG_WORK") or work)
    result_path = Path(os.environ["R8S3_RESULT"])
    synthetic_host = os.environ.get("R8S3_SYNTH_HOST", SYNTH_HOST)
    explicit = os.environ.get("R8S3_AUTHORITATIVE_PATH", "").strip()
    work.mkdir(parents=True, exist_ok=True)
    config_work.mkdir(parents=True, exist_ok=True)

    payload: dict[str, object] = {"url": url, "explicit": explicit,
                                  "env_path": os.environ.get(
                                      "YIKOU_SSS_AUTHORITATIVE_PATH", "")}
    with loopback_guard(port, hosts=(synthetic_host, synthetic_host + ".")):
        config = make_config(config_work, url)
        if explicit:
            config.sss_authoritative_uncertain_path = explicit
        payload["scope"] = try_scope(config)
        try:
            result = run_job(config)
            payload["status"] = result.get("status")
            payload["semantics"] = result.get("semantics")
            payload["location_conflicts"] = result.get("location_conflicts")
            payload["next_action"] = (result.get("next_action") or "")[:200]
            exit_code = 0
        except Exception as exc:  # noqa: BLE001 - 子进程如实回报失败类型
            payload["status"] = "EXCEPTION"
            payload["error"] = f"{type(exc).__name__}: {exc}"
            exit_code = 3

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                      default=str), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
