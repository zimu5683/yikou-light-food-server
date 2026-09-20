# R7 独立验收可复跑资产

本目录是 `docs/OPTIMIZATION-FINAL-ACCEPTANCE-R7.md` 的可复跑脚本与日志。
**只读工作区**：所有实验在 `/tmp` 隔离副本 + 临时数据目录 + 合成数据下进行，
不联网、不登录真实平台、不写任何真实云表、不读真实凭据/客户数据。

## 一键复跑

```bash
bash tools/r7-acceptance/run_all.sh          # 门禁 + 探针 + 变异敏感性，日志写入 logs/
python3 tools/r7-acceptance/mutation_check.py # 仅变异敏感性（逐条还原修复，验证探针会失败）

# R6-4（T2）：M4/M6/M8 三个盲区的探针 + 三段式变异验证
python3 tests/independent_final_counterexample_probe.py --list
python3 tests/independent_final_counterexample_probe.py --only m4-json-corrupt,m6-post-timeout,m8-batch-scope
python3 tools/r7-acceptance/mutation_check_r6_4.py            # before → injected → restored
python3 tools/r7-acceptance/mutation_check_r6_4.py --json /tmp/r6-4.json --keep
```

脚本会：把仓库 tar 到 `$WORK/py`（默认 `/tmp/yikou-r7-runall`），在副本根放
`sitecustomize.py` 网络守卫（非回环 `connect/connect_ex/sendto` → `OSError(101)`，
子进程经 `PYTHONPATH` 继承），再跑后端全量 pytest、`git diff --check`、
五个独立探针、以及逐条变异。

## 探针清单

| 脚本 | 覆盖 | 关键输出 |
| --- | --- | --- |
| `probe_w1_bridge_double_write.py` | W1 生产 Bridge 接线：`wps_preview` 后、`apply_plan` 锁前，另一进程提交账本+云端 | `判定 = REFUSED（零写入，安全）` |
| `probe_w1_stale_plan.py` | W1 与 R6 `probe_double.py` 同形的旧计划竞争；`--mutate-r6-wiring` 关掉层 B 可复现 `DOUBLE WRITE` | `VERDICT: no double write` / 变异下 `DOUBLE WRITE` |
| `probe_w2w3w4_journal_gate.py` | W2 未知版本/两层未知状态必须阻断且审计不得变 `verified`；W3 恢复路径可用且**有云端证据时不得清障**；W4 协作者占用新增行不得覆盖 | 七个 W3 子检查 + W2/W4 判定 |
| `probe_http_gates.py` | 直接 HTTP：恢复入口非管理员 403、`wps_enabled=False` 拒绝预览+上传、只读云入口在占位期间 `operation_conflict` 且 0 次 kdocs-cli 调用 | 4 项 OK |
| `probe_local_safety.py` | R6-1 Excel 无输出/0 字节/损坏/占用取消都必须保住原文件；ordering journal 落盘失败 0 POST；R6-5 不同 `TMPDIR` 锁仍互斥 | 7 项 OK |
| `tests/independent_final_counterexample_probe.py` | 21 个场景：闪时送跨进程重复提交/阻断、journal 迁移、origin 权威范围，以及 R6-4 新增的 M4/M6/M8 | 21 `BLOCKED` / 0 `DEFECT`，exit 0 |
| `indep_sss_faulty_post_child.py` | R6-4 合成子进程：**POST 已落库后再抛** `ReadTimeout`/`ConnectionError`，复现“服务端可能已落单、客户端拿不到响应” | 供探针 M6 场景使用 |
| `mutation_check.py` | 逐条还原 W1/W2/W8 修复，断言探针或测试**必须失败** | `4/4 OK` |
| `mutation_check_r6_4.py` | 逐条注入 M4/M6/M8（含 M8b 变体），三段式 before/注入/恢复 + 锚点唯一性 + 独立临时目录 | `4/4 OK` |

## 日志（`logs/`）

`backend-pytest.log`、`frontend-{test,lint,tsc-app,build,browser}.log`、
`probe_*.log`、`probe_*.mutated.log`、
`mutation-r6-4/<变异>.<before|injected|restored>.log`。

## R6-4 证据报告

`docs/OPTIMIZATION-R6-4-MUTATION-EVIDENCE.md`：每个变异的修复前/注入后/恢复后
退出码、探针实际抓到什么、复跑命令与未覆盖事项。

## 已知未覆盖（不要当成已通过）

* 真实 WPS 云端/协作者/远端 CAS、真实闪时送登录与 POST、真实 keyring/断电/磁盘满、
  真实 Android WebView 与软键盘：**均未执行**，见 R7 报告第 8 节。
* R6-3（URL 尾点/IDN 与权威范围）、R6-7（架构边界测试）、R6-8（浏览器检查对
  uncertain≠成功 的盲区）：**未修复**，已在 R7 报告中列为后续任务 T1/T3/T4。
  R6-4 的 M4/M6/M8 证据补强见上面 R6-4 报告。
