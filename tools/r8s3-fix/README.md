# tools/r8s3-fix —— R8-S3 修复验收探针（离线、隔离）

本目录只包含**验收工具**，不被生产代码导入（当时的验收与设计文档已随历史资料清理）。

| 文件 | 作用 |
| --- | --- |
| `probe_authority_location_guard.py` | 端到端探针：C1–C5、重启、并发、登记故障、正常路径、升级边界；同一份脚本可在修复前/后运行 |
| `guard_child_locations.py` | 隔离子进程：可指定 config 属性或环境变量形式的显式权威路径 |
| `evidence/*.json` | 修复前/后探针结论、监督方探针 before/after、变异汇总、全量门禁摘要 |
| `../tests/r8s3_location_guard_harness.py` | 共用装置（在 R8-S1 装置之上加显式权威路径配置与隔离子进程） |
| `../tests/test_sss_authority_location_guard.py` | 专项 pytest（26 项） |

## 隔离与合规

- 请求只发给 `127.0.0.1` 上的本地模拟平台；网络守卫拒绝非回环连接；
- `YIKOU_DATA_DIR`（登记锚点）、`YIKOU_SSS_AUTHORITATIVE_ROOT`（默认权威根）、
  `YIKOU_SSS_LOCK_ROOT`、`TMPDIR`、`XDG_*` 全部指向隔离临时目录；
- 每个场景独立锚点/权威根；子进程另有独立配置工作目录（避免并发写同一 xlsx）；
- 账号、密码、名单、证书全部为合成值；不读取真实凭据/客户数据/真实历史状态。

## 复跑命令

```bash
# 修复后：12/12 场景 PASS（exit 0）
cd <repo>
R8S3FIX_OUT=/tmp/r8s3fix/probe-fixed.json \
  PYTHONPATH=$PWD python3 tools/r8s3-fix/probe_authority_location_guard.py

# 修复前：C2–C5 与重启场景必须失败（exit 1，复现第二次 POST）
#   /tmp/r8s1fix/frozen = R8-S1 之后、R8-S3 之前的冻结副本
cd /tmp/r8s1fix/frozen
env PYTHONPATH=$PWD TMPDIR=/tmp/r8s3fix/tmp \
  R8S3FIX_OUT=/tmp/r8s3fix/probe-baseline.json \
  python3 tools/r8s3-fix/probe_authority_location_guard.py

# 专项测试
python3 -m pytest -q tests/test_sss_authority_location_guard.py
```

探针各场景的期望写在脚本头部表格；`[汇总] failed=...` 与 JSON 的 `scenarios`
字段是机器可读结论。`legacy_upgrade_process` 场景同时记录“未登记旧位置不会被自动
发现”这一**明确边界**（`boundaries` 字段），不计入失败但必须随报告一起阅读。
