# 架构说明

本文是当前代码结构的入口文档。历史设计/验证记录在 [`../design/`](../design/README.md)，
其中的模块路径可能停留在重构之前，仅作史料参考。

## 分层

```
浏览器 (frontend/)
   │  POST /api/<method> + 轮询 /api/drain_events
   ▼
app/web/        HTTP 交付层：路由、鉴权、静态资源、服务器端文件浏览器
   ▼
app/api/       应用 API/编排层：Bridge、事件队列、任务线程、交互等待
   ▼
app/order/    管理后台订单领域      app/ordering/  闪时送下单领域
app/wps/      WPS 云文档同步领域    app/core/      配置、凭据、模型、版本检查
app/integrations/  外部 HTTP 客户端（requests）
```

依赖方向只能向下：`core` 不依赖任何领域/交付层；领域层不依赖 `api/web`；
`web` 只通过 `api.Bridge` 调业务。`tests/test_architecture_boundaries.py`
会用 AST 静态锁死这个方向。

## 包职责

| 路径 | 职责 |
|---|---|
| `app/main.py` | CLI 入口：`--web` / `--self-check` / `--wps-check` / `--sss-import-check` |
| `app/core/config.py` | `AppConfig` 原子持久化、WPS 映射与地址排序默认值 |
| `app/core/credentials.py` | 密码读写：Termux/Linux 用 keyring，Android 用 Keystore（双命名空间） |
| `app/wps/android_runtime.py` | Python ↔ Kotlin `WpsRuntime` 的唯一桥；仅 `YIKOU_APP_MODE=android` 生效 |
| `app/core/models.py` | `MealInfo` / `OrderInfo` 纯数据模型 |
| `app/core/update.py` | 版本检查与更新：只查网页版 Release，发现新版本提示更新 |
| `app/integrations/api_client.py` | 管理后台与闪时送 HTTP 会话、登录、错误映射 |
| `app/order/parsing.py` | 订单文本解析、Excel 列写入、备份等纯函数 |
| `app/order/runner.py` | 管理后台订单抓取、合并、退款过滤、写排单表 |
| `app/order/templates.py` | 排单表 / 闪时送表模板生成 |
| `app/order/aliases.py` | 地址别名与待确认地址落盘 |
| `app/order/delivery.py` | 收货地址到校区 / 取餐点的归一化 |
| `app/ordering/sss.py` | 闪时送登录、任务构建、提交、对账（至少一次 + 对账确认） |
| `app/ordering/cloud_import.py` | 从 WPS 云端读当天名单、地址过滤、日期闸门、留档 |
| `app/wps/sync.py` | kdocs-cli 封装、增量计划、原地排序、格式复制、执行校验与账本 |
| `app/api/bridge.py` | 前端唯一 API 表面：事件、任务、交互、WPS、配置、更新检查 |
| `app/web/server.py` | ThreadingHTTPServer、统一鉴权、路由、静态资源、文件浏览器 |
| `app/web/auth.py` | 账号/PBKDF2/会话/邀请码/Cloudflare Access JWT |
| `app/web/pages.py` | 登录、拒绝、管理员审批页的 HTML 模板 |

## Android APK 运行形态

APK 复用同一套 `app/` 代码和 HTTP 协议，只在进程外多一层 Kotlin 原生兼容层：

```
MainActivity(WebView)
   │  http://127.0.0.1:<随机端口>/?token=...
   ▼
android_bootstrap.py  →  app/web/server.py + Bridge（Chaquopy 进程内）
   ▲
TaskService（前台 Service dataSync）
   ▲
WpsRuntime（Kotlin）→ proot → kdocs-cli     SecureStore（Keystore）
```

- Python 源集仍是仓库根 `app/`；Gradle 构建时只 Sync 到 `build/generated/`，不改源码。
- `app/wps/cli.py` 在 Android 模式下把 `subprocess.run` 换成
  `android_runtime.run_cli`，领域 planner/executor 完全不知道运行形态。
- DNS 用 `ConnectivityManager` 读系统 DNS 写 `resolv.conf`；CA 用 Mozilla bundle
  通过 `SSL_CERT_FILE` 注入；token 落在 App 私有目录，卸载即清。
- 完整接口、里程碑与真机验收矩阵见 `design/APK-PLAN.md` / `design/APK-STATUS.md`。

## 一次前端请求怎么走

1. `frontend/src/lib/bridge.ts` 以 `POST /api/<method>` 调用；
2. `app/web/server.py` 在 `parse_request` 统一做会话/令牌鉴权，再按方法白名单
   分发到 `Bridge.<method>`；
3. `Bridge` 更新配置或起后台线程，把日志/状态/交互事件写入事件队列；
4. 前端轮询 `drain_events(last_sequence, ack_sequence)`，按 `event_id` 去重、
   应用成功后推进 cursor；队列溢出会收到 `events:dropped` 告警。

## 新增代码放哪里

- 新的平台 HTTP 调用 → `app/integrations/`
- 新的订单/闪时送业务规则 → 对应领域包（`app/order/` / `app/ordering/` / `app/wps/`）
- 新的前端可调接口 → `app/api/bridge.py` 加方法，并在 `app/web/server.py`
  的 `_NON_ADMIN_METHODS` 里明确普通用户权限（默认仅管理员）
- 新的 HTTP 页面/路由 → `app/web/`
- 纯配置/凭据/通用模型 → `app/core/`

## 约束

- 文件职责单一，领域逻辑不要放进 `web/` 或 `api/`；`Bridge` 只做协议与编排。
- 配置集中在 `AppConfig`，密码只进 keyring，不写入源码或仓库。
- 写云端/下单等外部副作用必须可预览、可回读校验，并让领域层返回明确结果。
- 新增依赖先加进 `requirements.txt`，前端依赖进 `frontend/package.json`。
- 修改后必须跑：`python -m pytest -q`、`cd frontend && pnpm build && pnpm test`。

## 后续可选拆分（当前已知技术债）

第一轮重构先把「顶层大杂烩」拆成领域包并清掉桌面/浏览器残留。
第二轮已完成两个最大单文件的深水拆分：

- `app/wps/sync.py` → `errors / models / common / cli / ledger / reader / planner / executor`
- `app/ordering/sss.py` → `constants / common / records / workbook / models / fingerprint / payload / reconcile / submission / runner`
- `app/order/runner.py` → `common / fetching / excel_io / formatting`
- `app/ordering/cloud_import.py` → `import_models / roster / archive / service`
- `app/web/server.py` 的静态文件与文件浏览 → `static_files.py` / `fs_browser.py`

以下文件仍偏大，建议继续按“每次移动一个模块、保持 public API 与测试绿”拆分：

1. `app/api/bridge.py`（约 1560 行）→ `events.py` / `interactions.py` /
   `services/wps.py` / `tasks.py`，`Bridge` 用组合拼起来。
2. `app/web/server.py`（约 990 行）→ 继续抽 `routing.py` / `session_auth.py`
   （静态资源与文件浏览已拆出）。

拆分前先用 rope 的 `move-module`/`rename-module` 做机械化移动（见
`.zcode/skills/python-rope-refactor`），再改行为；`tests/test_architecture_boundaries.py`
会保证新依赖方向不倒退。
