# 一口轻食（网页版 / Android APK）

这是一个订单处理服务：Python 提供 HTTP 接口 + React/TypeScript/Tailwind 前端。
既可以在手机（Termux）上当服务器、其他设备用浏览器访问，也支持打成
**一个自带最小 kdocs-cli 运行环境的 arm64 APK**，无需安装 Termux。

- 固定**纯接口模式**：直接调用平台 HTTP 接口完成登录、读单和下单，不启动任何浏览器（桌面原生窗口版与 Playwright 备用模式已全部移除）。
- 账号密码不会写入源码；Android 上用 Android Keystore + AES-GCM 保存，Termux/桌面用系统
  密钥环（`keyring`），没有可用密钥环时退化为每次运行手动输入。
- 自用项目，功能不完善；当前代码结构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 三个任务模式

- **订单处理**：登录管理后台，读取最新订单并写入排单 Excel；
- **云文档同步**：把本地排单表的内容增量写入 WPS 云端的排单表（见
  [docs/WPS-SYNC-RULES.md](docs/WPS-SYNC-RULES.md)）；
- **闪时送下单**：从《闪时送.xlsx》或云端当天名单读取订单，在闪时送平台逐单创建预约单。

## 快速开始

### 网页版（手机 / 服务器当主机）

```bash
python run.py --web                  # 默认监听 0.0.0.0:8756（同一 WiFi 下其它设备可访问）
python run.py --web --port 9000      # 换端口；--host 127.0.0.1 只允许本机；--new-token 轮换令牌
```

启动后终端会列出可直接点开的网址（含 `?token=`），把「其它设备访问」那条在电脑/平板上
打开即可。页面由 `app/web/server.py` 提供（静态前端 + `POST /api/<方法名>`），业务逻辑全在
`app/api/bridge.py`；事件是「Python 追加 + 前端按 cursor 轮询」，不需要 WebSocket。
「选择 Excel 文件」用的是**主机上**的文件浏览器（任务与 Excel 都在主机上）。

### 账号与访问审批

网页版等于把这台机器的完整操作权限交给「能进得来的人」，因此除了令牌还有一套账号体系：
整站（含首页与静态资源）都要求登录，未登录一律跳 `/login`。

- **注册申请**：访客在登录页填账号密码提交申请，进入待审批队列；
- **管理员审批**：`approved` 之前**密码正确也登不进来**；管理员在 `/admin` 点同意/拒绝，
  也可生成邀请码让凭码注册直接通过，省一轮人工审批；
- **会话**：登录成功发 `HttpOnly` 会话 Cookie（30 天）；失效/被拒/改密后旧会话立即失效。

```bash
python run.py --web --create-admin          # 交互式创建第一个管理员（--username 指定账号名）
python run.py --web --reset-admin-password  # 忘记密码时重置；--list-users 列出账号与审批状态
```

账号数据落在用户配置目录的 `users.json`（手机上是 `~/.config/yikou-light-food/`）。

### Android APK（arm64 独立安装）

WebView 加载现有前端，Chaquopy 在 App 进程内跑现有 `app/` 业务代码，Kotlin `WpsRuntime`
负责 `proot + kdocs-cli`、DNS/CA 适配与 Custom Tabs 授权。完整方案、接口与验收矩阵见
[design/APK-PLAN.md](design/APK-PLAN.md) 与 [design/APK-STATUS.md](design/APK-STATUS.md)。

```bash
python scripts/fetch_kdocs_cli.py --platform linux-arm64
python scripts/fetch_android_runtime.py    # 下载 proot/依赖 + NDK 编译 xdg shim
cd frontend && pnpm install --frozen-lockfile && pnpm build && cd ../android && ./gradlew assembleRelease
```

CI 工作流 [.github/workflows/android.yml](.github/workflows/android.yml) 用 x86_64 runner 下载
linux-arm64 kdocs-cli、Termux proot 依赖与 Mozilla CA，patchelf 改 RUNPATH 后打包。正式分发必须在
GitHub Secrets 配置 `YIKOU_KEYSTORE_FILE` / `YIKOU_KEYSTORE_PASSWORD` / `YIKOU_KEY_ALIAS` /
`YIKOU_KEY_PASSWORD`；缺失时退化为 debug 签名，只能做 M0/M1 验证。

**应用内更新**：App 打开后自动检查 GitHub Release，点「下载并安装」后会选择 arm64 APK 与
`.sha256`，校验完整性、SHA-256、包名、签名和 `versionCode`，通过后拉起系统安装器。
普通 Android App 无法静默安装；新旧 APK 必须使用同一把签名证书。该功能从 **v3.6.5** 起存在。

## 云同步与闪时送要点

**云文档同步**把本地《排单.xlsx》**增量写入** WPS 云端排单表，不整表覆盖，因此云端的公式、
自定义排序、字体和列宽都不会被破坏。要点：

- 先核对批次日期：整张表的日期标记与目标日期不符 → **拒绝写入整张表**（唯一的重复加餐防线）；
- 旧客户**总餐次 = 云端现有 + 本次增量**；目标日期格已有协作者写的值（如 `0`）**只读不写**；
- 新客户统一插到第 3/4 行之间，再按列B 地址顺序重排整张表；
- 幂等靠本地账本 `wps_sync_state.json`；上传前先「预览」，同一批重复上传不会翻倍；
- **测试模式**（默认开启）把正式表替换为「测试-」副本，绝不会碰正式排单表。

完整规则（地址排序表、目标日期口径、首次使用、安全边界、Termux 上的 kdocs-cli 依赖）见
[docs/WPS-SYNC-RULES.md](docs/WPS-SYNC-RULES.md)；总体方案与实施史见
[design/WPS-CLOUD-SYNC-PLAN.md](design/WPS-CLOUD-SYNC-PLAN.md)。

**闪时送下单**默认每次「开始下单」（含干跑/预检）前从云端取当天名单（20:00 之后识别次日），
地址是「大西」「小」的人不下单；名单会留档进《闪时送.xlsx》；云端读不到或要下单的人数据
不完整 → **拒绝下单**并点名是哪个单元格。日期口径、地址过滤与拒绝语义见
[design/SSS-云端名单导入.md](design/SSS-云端名单导入.md)；被阻断「存在未解决的不确定记录」时
**不要重跑或补发**，现场核对与管理员解除入口见
[docs/SSS-不确定记录处置.md](docs/SSS-不确定记录处置.md)。只读自检：
`python -m app.main --sss-import-check`。闪时送登录有图形验证码，必须人工输入一次。

## 项目结构

```
app/          Python 后端：main.py CLI 入口；core/ 配置与凭据；integrations/ 平台 HTTP 客户端；
              order/ 订单处理；ordering/ 闪时送下单与云端名单；wps/ 云同步；api/ Bridge；web/ HTTP 服务
frontend/     React + TypeScript 前端（构建产物在 frontend/dist/）
android/      Android APK 工程（Chaquopy + WpsRuntime 兼容层）
tests/        pytest 与前端契约测试      scripts/ 运维/构建脚本
docs/         架构说明、契约与处置手册    design/ 历史方案与验证资料（见 design/README.md）
CHANGELOG.md  已发布版本的更新记录
```

依赖方向：`web → api → 领域包（order / ordering / wps）→ core / integrations`，
由 `tests/test_architecture_boundaries.py` 静态约束。

## 开发与测试

```bash
python -m venv .venv && python -m pip install -r requirements.txt
cd frontend && pnpm install && pnpm build && cd ..   # 构建产物在 frontend/dist/
python run.py                                        # 无参数即启动网页服务
.venv/bin/python -m pytest -q                        # 后端全量测试
cd frontend && pnpm test && pnpm lint && pnpm build  # 前端测试/lint/构建
```

前端改动后必须重新 `pnpm build`，否则服务仍在提供旧的构建产物。前端细节（浏览器交互检查、
变异检查、目录说明）见 [frontend/README.md](frontend/README.md)。Termux（Android）上开发不需要
GTK/WebKitGTK，也不需要任何浏览器：

```bash
pkg install python nodejs-lts git proot python-cryptography
python -m venv --system-site-packages .venv && .venv/bin/pip install openpyxl keyring requests
cd frontend && pnpm install && pnpm build
.venv/bin/python run.py
```

## 部署与公网访问

手机在 CGNAT 后面没有公网 IP，用 Cloudflare Tunnel 由手机主动向 Cloudflare 建出站连接，
公网请求沿该连接回源，因此不需要公网 IP、端口映射或 DDNS（**不要做端口映射**）。
服务用 runit 托管（崩溃自动拉起、开机自动启动），开机自启依赖 **Termux:Boot**；`/admin`
建议再叠一层 Cloudflare Access：设好 `YIKOU_ACCESS_TEAM_DOMAIN` 与 `YIKOU_ACCESS_AUD`
后该页会校验 `Cf-Access-Jwt-Assertion` 的签名。

- 手机开着 VPN/代理时 `cloudflared` 必须加 `--protocol http2`（默认 QUIC 会被 fake-ip
  代理破坏，症状是隧道 inactive、公网 1033）；它是隐藏参数，清理参数时不要删。`pkg upgrade`
  可能覆盖包自带的 run 脚本、冲掉该参数，用 `~/fix-http2.sh` 一键重放。

> ⚠️ **域名阻断（2026-09-16 实测）**：`zimu5683.kdns.fr` 在中国大陆被 SNI 阻断，服务端与隧道正常，**换一个未被阻断的域名即可**；证据链与换域名步骤见 [design/公网访问现状与域名阻断.md](design/公网访问现状与域名阻断.md)。

## 已知限制

- **令牌只对本地/局域网直连有效**：请求经网关（带 `CF-Connecting-IP` / `X-Forwarded-*`）
  进来时 `?token=` 一律失效，不能绕过审批；服务自身只提供 HTTP，公网 HTTPS 由 Cloudflare
  边缘终结，纯局域网 HTTP 理论上可被嗅探；
- Termux 上云文档同步/闪时送云端名单**硬依赖 `pkg install proot`**：静态链接的 kdocs-cli
  读不到 `/etc/resolv.conf` 与 CA，Android seccomp 还会拦 `faccessat2`；
- `ruff==0.15.22` 在 Android 上没有可安装的包，可用 `pkg install ruff` 代替，
  但版本不同、默认规则集比 CI 更严，以 CI 结果为准；
- 闪时送登录有图形验证码，必须人工输入一次；被阻断的不确定记录不能靠重跑解决。

## 数据与安全

配置、失败快照保存在用户配置目录（Windows：`%APPDATA%\yikou-light-food`），Excel 只在用户
选择的位置读写，运行前会创建 `backups/` 时间戳备份。请不要把真实 Excel、日志、密码或浏览器
缓存提交到 Git。
