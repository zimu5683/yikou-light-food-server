"""R8-S1 隔离子进程：在独立环境里跑一次真实 ``run_sss_job``。

父进程（专项测试或探针）负责启动本地模拟平台；本子进程：

* 只允许回环连接，把合成主机名映射到 ``R8S1_PORT``；
* 使用独立的 ``HOME`` / ``TMPDIR`` / ``YIKOU_DATA_DIR`` /
  ``YIKOU_SSS_AUTHORITATIVE_ROOT`` / ``YIKOU_SSS_LOCK_ROOT``（由父进程注入）；
* 固定时钟、真实安全闸门、真实 ``run_sss_job``，不使用任何真实凭据。

结果写到 ``R8S1_RESULT`` 指定的 JSON 文件，同时打印到 stdout 末行。
退出码：0 = 正常返回结果；3 = 运行期抛异常（错误写入结果文件）。
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
    port = int(os.environ["R8S1_PORT"])
    url = os.environ["R8S1_URL"]
    work = Path(os.environ["R8S1_WORK"])
    result_path = Path(os.environ["R8S1_RESULT"])
    synthetic_host = os.environ.get("R8S1_SYNTH_HOST", SYNTH_HOST)
    work.mkdir(parents=True, exist_ok=True)

    payload: dict[str, object] = {"url": url, "host": synthetic_host}
    with loopback_guard(port, hosts=(synthetic_host, synthetic_host + ".")):
        config = make_config(work, url)
        payload["scope"] = try_scope(config)
        try:
            result = run_job(config)
            payload["status"] = result.get("status")
            payload["semantics"] = result.get("semantics")
            payload["next_action"] = result.get("next_action")
            payload["summary"] = result.get("summary")
            exit_code = 0
        except Exception as exc:  # noqa: BLE001 - 子进程要把失败类型如实回报
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
