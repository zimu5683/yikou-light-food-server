# 更新记录

只记录**已发布**的版本；开发过程资料见 `design/archive/`（历史，含逐轮迭代日志），
当前手册与规则见 `README.md` 与 `docs/`。

## 3.6.14

一次以「精简」为主的重构，**不改变三个任务模式的业务行为**。

### 文档

- 删除 WPS 云同步的交接文档（564 行）；README 436 → 158 行，云同步长规则移入
  `docs/WPS-SYNC-RULES.md`，README 只保留「是什么 / 快速开始 / 要点 / 开发 / 已知限制」。
- `design/` 历史资料与 `tools/` 一次性探针归档到 `design/archive/`；
  `design/` 只保留仍被代码注释引用的方案文档（APK 计划、云同步方案、网页版设计等）。
- 新增门禁 `tests/test_doc_references.py`：文档里的相对路径必须真实存在，
  且已删除/已归档的文档不许在旧路径复活。

### 前端

- 三个页签的流程条改为**只在「任务在跑 / 待核对 / 失败」时渲染**，闲置态不再占屏幕。
- 云同步页删除 5 处冗余 chrome：重复的「当前写入」状态项、与上方重复的设置提示、
  原始预览全文转储、调试性的「预览编号」、无预览时的占位提示；9 处长句文案压到一句。
- **安全闸门一个都没动**：预览 → 确认 → 上传、真实下单二次确认、未决记录处置、
  WPS 恢复处置、上传单飞闸门、地址排序与凭据开关。
- 新增门禁 `pnpm check:anchors`（`frontend/scripts/check-mutation-anchors.mjs`）：
  变异检查的 18 个精确源码锚点必须在目标文件里唯一命中。

### 后端

- 删除无消费者的桥接通道 `echo_test` / `frontend_report` / `pop_reports`
  （含 HTTP 白名单项与 50 条上限的快照缓存）。
- 删除桌面版更新提示轨道（`DESKTOP_REPOSITORY` 与 `desktop_update:available` 事件），
  版本检查收敛为单轨道。
- 删除 legacy_一口轻食.py（旧版脚本，非运行入口）。

### CI

- **浏览器门禁接入流水线**：`frontend` job 每次 push / PR 跑
  `browser-interaction-check.mjs`（真实 headless Chrome + CDP，202 条界面断言）。
- 新增 `mutation` job（PR 与手动触发）：注入已知缺陷，要求指定断言必须变 FAIL，
  证明上面那些断言不是空转。
- 新增 `workflow_dispatch`，可在 Actions 页手动触发。

### 验证

- `pytest -q`：**1515 passed / 0 failed**（另有 1 xpassed）。
- `pnpm test` 219 passed；`pnpm build` 与 `pnpm check:anchors`（18/18）通过。
- 独立审计 7/7 PASS（锚点唯一性、门禁一致性、变异语义、安全语义、门禁非空转、
  前后端契约、文档死引用）。
- 说明：浏览器门禁与变异门禁在本版**第一次接入 CI**，此前只在本机手动跑；
  本机无 Chrome/Chromium，故这两项的实跑结论以 CI 为准。

## 3.6.13

此前版本的记录见 `design/archive/迭代进展.md`（2026-09-23 条目起）。
