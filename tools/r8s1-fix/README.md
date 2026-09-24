# tools/r8s1-fix —— R8-S1 修复验收探针（离线、隔离）

本目录只包含**验收工具**，不被生产代码导入（当时的验收文档已随历史资料清理）。

| 文件 | 作用 |
| --- | --- |
| `probe_url_alias_guard.py` | 端到端探针：同一份脚本可在修复前快照与修复后工作区运行，输出 JSON 证据 |
| `guard_child.py` | 隔离子进程：真实 `run_sss_job` + 网络守卫，用于“进程重启/并发”场景 |
| `../tests/r8s1_url_guard_harness.py` | 共用装置：只监听回环的模拟平台（HTTP/本地 HTTPS）、网络守卫、合成证书/Excel/配置 |
| `../tests/test_sss_url_authority_guard.py` | 专项 pytest（61 项） |

## 隔离与合规

- 请求只发给 `127.0.0.1` 上的本地模拟平台；网络守卫把合成主机名映射到该端口，
  其它连接一律 `OSError(101)`；
- `HOME` 不覆盖（Python user site-packages 需要），但 `YIKOU_DATA_DIR` /
  `YIKOU_SSS_AUTHORITATIVE_ROOT` / `YIKOU_SSS_LOCK_ROOT` / `TMPDIR` /
  `XDG_*` 全部指向隔离临时目录；
- 账号、密码、名单、证书全部为合成值；不读取真实凭据/客户数据；
- 不执行真实登录、真实下单、WPS 操作。

## 复跑命令

```bash
# 1) 修复后：全部场景应 PASS（exit 0）
cd <repo>
R8S1FIX_OUT=/tmp/r8s1fix/probe-fixed.json \
  PYTHONPATH=$PWD python3 tools/r8s1-fix/probe_url_alias_guard.py

# 2) 修复前：重复提交必须复现（exit 1）——在修复前快照里运行
cd /tmp/r8s1fix/baseline     # 修复前 tar 快照（生产文件哈希见 /tmp/r8s1fix/baseline-hashes.txt）
env TMPDIR=/tmp/r8s1fix/tmp PYTHONPATH=$PWD R8S1FIX_OUT=/tmp/r8s1fix/probe-baseline.json \
  python3 tools/r8s1-fix/probe_url_alias_guard.py

# 3) 专项测试
python3 -m pytest -q tests/test_sss_url_authority_guard.py
```

探针各场景与验收期望写在脚本头部表格里；`[汇总] failed=...` 与 JSON 的
`scenarios` 字段是机器可读结论。
