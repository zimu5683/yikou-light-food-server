# sss-review-probe（独立验证探针）

`probe_e2e.py` 是独立验证者（task-1）用的离线端到端探针，与
`tests/test_sss_uncertain_review_independent.py` 相互独立、结论一致：

- A. 未解除前 `run_sss_job` 阻断（零 POST）→ `run_sss_review_job` 生成
  `station_missing` 证据 → 管理员 `sss_uncertain_resolve(station_absent)` →
  重跑恰好重新 POST 一次并被站内对账确认；
- B. 站内已有匹配订单时，提交前的只读对账阻止 POST；
- C. 只读核对只发 GET 列表请求，零 POST；
- D. 观察项：非管理员直调 `Bridge.start_sss_review` 的实际行为（打印，
  不参与退出码；该缺口已作为 DEFECT 记录在测试文件的 xfail 中）。

运行：

```sh
python tools/sss-review-probe/probe_e2e.py
```

退出码 0 = A/B/C 全部通过；全部请求都走伪客户端，不联网、不改仓库文件
（临时目录位于 `$TMPDIR`）。
