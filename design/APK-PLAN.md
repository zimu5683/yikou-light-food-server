# APK 内置 kdocs-cli 实施计划

> 目标：把当前「Python 后端 + React 前端 + kdocs-cli + Termux」方案，改为
> **一个 Android APK 内自带最小兼容运行环境**。用户不需要安装 Termux，不需要
> 常驻服务器；WPS 授权沿用 kdocs-cli 方案，每个用户自己的 token 有效期约一年。

## 0. 结论与边界

### 0.1 最终形态

```text
APK
├─ WebView（沿用现有 React 前端，不重写 UI）
├─ 内嵌 Python（Chaquopy，复用现有业务代码）
│    ├─ app/api/bridge.py
│    ├─ app/order/*、app/ordering/*、app/wps/*
│    └─ app/web/server.py（仅监听 127.0.0.1）
└─ Kotlin WpsRuntime（专门运行 kdocs-cli）
     ├─ proot
     ├─ kdocs-cli
     ├─ DNS resolv.conf 适配
     ├─ SSL_CERT_FILE 适配
     ├─ 假 xdg-open + Custom Tabs 授权
     └─ token 持久化与诊断
```

### 0.2 要做的事

- 一个 arm64-v8a APK 可独立安装运行。
- 首次启动完成 Excel 文件、账号密码、WPS 授权等配置。
- 订单处理、云文档同步、闪时送下单三个任务全部可用。
- WPS 授权每个用户独立完成，token 落在应用私有目录。
- 支持 GitHub Releases 分发、APK 签名、版本升级、配置保留。
- 保留现有网页版启动方式，便于过渡期对照测试。

### 0.3 明确不做的事

- 不改用 WPS 365 官方开放平台 API（资质不可得）。
- 不重写 `app/order`、`app/ordering`、`app/wps` 的领域逻辑。
- 不做 32 位 ABIs；kdocs-cli 官方只有 linux-arm64。
- 不承诺 Google Play 上架；以自带签名 APK 分发为主。
- 不做多设备共享同一份本地数据；每台设备独立配置。

## 1. 核心技术决策

| 事项 | 决策 | 备注 |
|---|---|---|
| Python 运行时 | Chaquopy（MIT，可用于分发） | 目标 Python 3.13，必要时退 3.12 |
| UI | 现有 React/Vite 产物进 WebView | 不改前端业务组件 |
| Python 与服务 | Chaquopy 在 App 进程内启动 127.0.0.1 HTTP 服务 | 继续复用 `app/web/server.py` + `Bridge` |
| 任务保活 | Android 前台 Service（`dataSync`） | 防止息屏/切后台被杀 |
| WPS 进程 | Kotlin `WpsRuntime` 托管 proot + kdocs-cli | Python 不直接 `subprocess.run` |
| 二进制落盘 | 伪装成 `.so` 放 `jniLibs/arm64-v8a` | 使用 `nativeLibraryDir`，绕开应用私有目录执行限制 |
| Python 数据目录 | `context.filesDir` + `HOME` / `XDG_CONFIG_HOME` 指定 | 落在应用私有目录，卸载才清除 |
| 密码存储 | Android Keystore + AES-GCM | Android 模式下替换 keyring 后端 |
| Excel 文件 | v1 使用 `MANAGE_EXTERNAL_STORAGE` | 自用/直接分发；如未来上架再改 SAF |
| 版本分发 | GitHub Releases + APK 签名 | 保留同一 keystore，否则无法覆盖升级 |
| kdocs-cli 版本 | 固定 2.5.29 | 禁用 CLI 自带 upgrade，随 App 升 |

## 2. 目标架构与模块边界

### 2.1 Android 模块

```text
android/
  app/
    src/main/java/com/yikou/lightfood/
      MainActivity.kt           WebView + 权限引导
      TaskService.kt            前台 Service，持有 Python 进程
      PythonRuntime.kt          初始化 Chaquopy，启动本地 HTTP 服务
      WpsRuntime.kt             kdocs-cli 兼容层（本计划的重点）
      AuthCoordinator.kt        抓授权 URL，拉起 Custom Tabs
      SecureStore.kt            Android Keystore 封装
      AppUpdater.kt             GitHub Releases 检查与 APK 安装
    src/main/assets/runtime/    CA 证书、启动脚本、版本清单
    src/main/jniLibs/arm64-v8a/
      libkdocs_cli.so           即 kdocs-cli 二进制
      libproot.so               已 patchelf
      libtalloc.so
      libandroid_shmem.so
      libxdgopen_shim.so        假浏览器
    src/main/python/            仅放薄适配层
      android_bootstrap.py
  build.gradle.kts
  settings.gradle.kts
  gradle.properties
```

Python 源码不复制进 `android/`，Gradle 的 `python.srcDir` 指向：
- 仓库根 `app/`
- `android/app/src/main/python/`

### 2.2 Kotlin WpsRuntime 对外接口

Python 只通过这一组接口调用 WPS：

```kotlin
object WpsRuntime {
    fun initialize(context: Context): RuntimeStatus
    fun run(args: List<String>, paramsJson: String?, timeoutMs: Long): ExecResult
    fun authStatus(): AuthStatus
    fun authorize(onUrl: (String) -> Unit): AuthResult
    fun logout(): ExecResult
    fun diagnostics(): Map<String, Any?>
}
```

`ExecResult` 字段：
- `exitCode: Int`
- `stdout: String`
- `stderr: String`
- `timedOut: Boolean`
- `errorCode: String?`（`PROOT_MISSING` / `DNS_FAILED` / `TLS_FAILED` / `RUNTIME_CRASHED` 等）

### 2.3 Python 适配层

新增 `app/wps/android_runtime.py`：
- `is_android()`：由 App 启动时设置的环境变量或 Chaquopy 平台判断。
- `run_cli(args, params) -> ExecResult`：调用 `WpsRuntime.run`。
- `auth_status() -> bool`：调用 `WpsRuntime.authStatus`。
- `authorize()`：调用 `WpsRuntime.authorize`。

修改 `app/wps/cli.py`：
- `_run_once`：Android 分支调用 `run_cli`，桌面/Termux 分支保持原 `subprocess.run`。
- `authenticated`：Android 分支调用 `auth_status`。
- `login_argv` / `login_env`：仅桌面/Termux 使用；Android 走 `authorize()`。
- `find_cli`：Android 模式下返回固定标记，如 `@android-runtime`，不要求真实路径。

修改 `app/api/bridge.py`：
- `_wps_authorize_worker`：Android 分支调用 `WpsRuntime.authorize`，把 URL 和进度写日志；
  桌面/Termux 保持原逻辑。

修改 `app/core/credentials.py`：
- 增加 Android 后端，通过 `SecureStore` 读写密码。
- 无 Android 环境时保持 keyring/手动输入逻辑。

### 2.4 前端接入

- 前端协议不变：继续 `POST /api/<method>`。
- 增加 Android 原生能力只做三件事：
  1. 文件选择可以继续走 `fs_browser`，但服务端根目录映射 Android 存储；
  2. WPS 授权由原生层拉起浏览器，前端只显示状态；
  3. 版本更新、权限申请、电池优化引导由原生层负责。

## 3. WpsRuntime 兼容层规格

### 3.1 二进制来源与校验

- `kdocs-cli`：仓库已有 `vendor/kdocs-cli/kdocs-cli`，版本 2.5.29，直接用官方 checksums 校验。
- `proot` 运行时：从 Termux 包提取，固定版本并记录 sha256：
  - `proot`
  - `libtalloc.so.2`
  - `libandroid-shmem.so`
  - `libexec/proot/loader`（先验证同架构运行不需要 loader；若需要一并打包）
- CA：使用 Mozilla CA bundle，版本随 App 更新。
- 新增 `scripts/fetch_android_runtime.py` 负责一键下载/提取/校验。
- 构建前执行 `patchelf`：
  - `libproot.so` 的 RUNPATH 改成 `$ORIGIN`；
  - 替换 `DT_NEEDED` 中的 `libtalloc.so.2`、`libandroid-shmem.so`
    为 jniLibs 中的实际文件名；
  - 所有库统一放在 `jniLibs/arm64-v8a/`。
- `libxdgopen_shim.so`：用 Android NDK 编译一个极小 C 程序：
  - 读取环境变量 `YIKOU_AUTH_URL_FILE`；
  - 把 `argv[1]` 追加写入该文件；
  - 退出码 0。

### 3.2 命令构造

Android 模式下 `WpsRuntime.run` 实际执行：

```text
<nativeLibDir>/libproot.so
  -b <filesDir>/wps/resolv.conf:/etc/resolv.conf
  -b <nativeLibDir>/libxdgopen_shim.so:/usr/bin/xdg-open
  <nativeLibDir>/libkdocs_cli.so
  <args...>
  [--file <filesDir>/wps/tmp/<uuid>.json]
```

环境变量固定注入：

```text
HOME=<filesDir>/wps/home
XDG_CONFIG_HOME=<filesDir>/wps/home/.config
SSL_CERT_FILE=<filesDir>/wps/cacert.pem
YIKOU_AUTH_URL_FILE=<filesDir>/wps/auth_url.txt
LANG=C.UTF-8
```

注意：
- `nativeLibDir` 每次启动通过 `context.applicationInfo.nativeLibraryDir` 获取，
  不写入配置，因为 App 更新后路径会变。
- `--file` 临时文件由 Kotlin 或 Python 写，命令结束删除。
- 不允许把 token 作为命令行参数打印到日志。

### 3.3 DNS 适配

`WpsRuntime.initialize` 与网络变化时：

1. 通过 `ConnectivityManager.getAllNetworks()` 和 `LinkProperties.dnsServers`
   读取系统 DNS；
2. 生成 `<filesDir>/wps/resolv.conf`：

```text
nameserver <dns1>
nameserver <dns2>
options timeout:2 attempts:2
```

3. 去重，只写 IPv4/IPv6 地址；IPv6 link-local 带 zone 的要过滤或规范化；
4. 读不到时降级：
   - `223.5.5.5`
   - `119.29.29.29`
5. 注册 `NetworkCallback`，网络切换后自动重写文件。

验证点：VPN / Clash fake-ip 场景必须测试；不能只硬编码 8.8.8.8。

### 3.4 CA 适配

- 首次启动把 assets 中的 `cacert.pem` 复制到 `<filesDir>/wps/cacert.pem`。
- 每次 App 版本升级后可覆盖更新。
- 用户手动安装额外证书的场景 v1 暂不支持，留诊断入口。

### 3.5 授权流程

已验证 `kdocs-cli auth login` 行为：

1. 终端打印授权 URL；
2. 调用 `xdg-open`；
3. 在本地等待/轮询，最长由 `--oauth-timeout` 控制；
4. 用户确认后换 token 并写入 `HOME` 下的 kdocs-cli 配置目录。

Android 流程：

1. `WpsRuntime.authorize` 启动：
   `proot ... libkdocs_cli.so auth login --oauth-timeout 600000`
2. `libxdgopen_shim.so` 把 URL 写入 `auth_url.txt`；
3. `AuthCoordinator` 用 `FileObserver` 或轮询文件，读到 URL 后：
   - 启动 `CustomTabsIntent` 或 `Intent.ACTION_VIEW`；
   - 通知前端「已拉起浏览器，等待授权完成」；
4. 等 CLI 进程退出；
5. 执行 `auth status` 确认 `authenticated == true`；
6. 失败时给出诊断：网络、证书、proot 退出码、stderr 摘要。

约束：
- 同一时刻只允许一个授权流程；新流程先取消旧的。
- 授权完成后不复制、不上传、不打包 token；每台设备独立。
- 支持「退出授权」（`auth logout`），并同步前端状态。

### 3.6 token 与配置目录

- `HOME` 指向 `<filesDir>/wps/home`，CLI 会创建 `.kdocs-cli/client-id` 与 token 文件。
- token 所在目录必须：
  - 在 App 升级时保留；
  - 卸载 App 时自动清除；
  - 不进入 Android 自动备份（配置 `android:allowBackup="false"` 或排除规则）。
- 可选增强：授权成功后把整个 `.kdocs-cli` 目录做一次 Android Keystore 加密备份；
  恢复时解密写回。v1 先不做，但要保留接口。

### 3.7 错误模型

| 场景 | errorCode | 用户提示 |
|---|---|---|
| nativeLibDir 下找不到二进制 | `PROOT_MISSING` | App 运行时组件缺失，请重装 |
| proot 退出 SIGSYS | `SECCOMP_BLOCKED` | 系统拦截，需检查 ROM / targetSdk |
| resolv.conf 无 DNS | `DNS_CONFIG_EMPTY` | 未获取到网络 DNS |
| lookups 失败 | `DNS_FAILED` | 当前网络无法解析 WPS 域名 |
| x509 错误 | `TLS_CA_FAILED` | CA 证书不可用 |
| 超时 | `TIMEOUT` | 云文档请求超时 |
| CLI JSON code != 0 | 原样透传 | 沿用现有 `WpsCloudError` 文案 |

## 4. Android App 形态

### 4.1 MainActivity

- 启动时申请通知权限、存储权限、电池优化白名单引导。
- WebView 配置：
  - `javaScriptEnabled = true`
  - `domStorageEnabled = true`
  - `databaseEnabled = true`
  - 禁止明文外部 HTTP（本地 127.0.0.1 例外，使用 Network Security Config）
- WebView 加载 `http://127.0.0.1:<随机端口>/?token=<本地令牌>`。
- 返回键处理：先关闭弹窗/日志抽屉，再退出。
- 外部链接用系统浏览器打开。

### 4.2 TaskService

- 前台 Service 启动后初始化 Python 与 HTTP 服务。
- 通知渠道：`任务运行中`、`等待验证码`、`等待地址确认`。
- 任务在跑或 Bridge 有 pending interaction 时保持前台状态。
- 进程被杀后由用户再次打开 App 恢复；任务状态与事件日志落盘。

### 4.3 本地 HTTP 服务安全

- 只绑定 `127.0.0.1`，禁止 `0.0.0.0`。
- 端口使用随机可用端口，或固定端口但检测占用。
- 令牌随机生成，存 `filesDir`，WebView 首次 URL 带上。
- 如果未来要恢复局域网访问，再单独设计，不默认开放。

### 4.4 文件与权限

v1 决策：使用 `MANAGE_EXTERNAL_STORAGE`，流程：

1. 首次启动引导用户到系统设置授予「所有文件访问权限」；
2. `app/web/fs_browser.py` 的浏览根适配 `/sdcard`、`Download`、App 工作区；
3. 用户选择 Excel 后直接使用真实路径，最小化对现有 Python 逻辑的改动。

后续若需上架：
- 改为 SAF 选择文件 + 复制到 App 工作区；
- Python 只看到工作区路径；
- 保存时用 SAF 写回原文件。
该改造应在 v2 独立立项，不阻塞 v1。

### 4.5 数据目录映射

App 启动时设置：

```text
YIKOU_APP_MODE=android
XDG_CONFIG_HOME=<filesDir>/config
HOME=<filesDir>
YIKOU_DIST_DIR=<filesDir>/dist
```

修改 `app/core/config.py::user_data_dir()` 与 `app/web/server.py::default_dist_dir()`：
- 优先读取上述环境变量；
- 未设置时保持桌面行为。

### 4.6 更新与分发

- GitHub Releases 存放已签名 arm64 APK。
- App 启动后可检查 `app/core/update.py` 返回的最新版本。
- 下载到 cache 后用 `FileProvider` + `PackageInstaller` 安装。
- `versionCode` 与 `versionName` 统一由 Gradle 管理，并与 Python `__version__` 校验一致。
- 签名 keystore 备份到安全位置；丢失后无法覆盖升级。

## 5. 构建与 CI

### 5.1 本地构建

```bash
python scripts/fetch_android_runtime.py --check
cd frontend && pnpm install --frozen-lockfile && pnpm build
cd android && ./gradlew assembleRelease
```

### 5.2 GitHub Actions

新增 `.github/workflows/android.yml`：

1. `ubuntu-latest`
2. 安装 JDK 17、Android SDK 35、Gradle 缓存
3. `python scripts/fetch_kdocs_cli.py --check`
4. `python scripts/fetch_android_runtime.py`
5. `patchelf` 处理 proot 与依赖
6. `pnpm build` 构建前端
7. Chaquopy 构建 Python 代码、跑 `pytest`（Python 3.13）
8. `./gradlew assembleRelease`
9. 上传 artifact：`yikou-light-food-<version>-arm64.apk` + sha256

### 5.3 版本与依赖锁定

- Chaquopy 插件版本、Python 版本、`cryptography`、`openpyxl`、`requests` 全部锁定。
- `kdocs-cli`、`proot`、CA bundle 全部记录 sha256。
- 禁止在 APK 内执行 kdocs-cli `upgrade`，更新随 App 发版。

## 6. 测试与验收

### 6.1 自动化测试

- 继续运行现有 Python `pytest`，桌面/Termux 全部通过。
- Android 侧新增：
  - `WpsRuntimeTest`：命令构造、DNS 解析、错误映射。
  - `AuthCoordinatorTest`：URL 抓取、Custom Tabs Intent、取消流程。
  - `SecureStoreTest`：Keystore 加解密与异常。
  - `PythonBootstrapTest`：Chaquopy 能 import 业务模块、`--self-check` 通过。
- 增加「同一命令对拍」：
  - Termux 与 APK 各跑同一组 WPS 只读命令；
  - 比较 JSON 结构和关键字段。

### 6.2 真机矩阵

必须覆盖：

- Android 10、11、12、13、14、15
- 至少一个 HyperOS / MIUI / ColorOS 等国产 ROM
- 无 VPN、Clash 类 VPN、蜂窝网络、Wi-Fi 切换
- 权限拒绝后再授权
- 横竖屏、后台 30 分钟、屏幕熄灭
- APK 覆盖安装后 token 与配置保留
- 卸载重装后的首次引导

### 6.3 WPS 集成验收

- 只读：
  - `auth status` = true
  - `sheet get-sheets-info`
  - `sheet get-range-data`
  - `drive list-files`
- 写入测试副本：
  - 单元格增量更新
  - 插入行
  - 格式复制
  - 排序
  - 对账后可读回校验
- 授权/额度失败时，前端提示与 Termux 版一致。

### 6.4 长稳测试

- 订单任务跑满一次真实流程，过程中锁屏、切后台、开 VPN。
- 闪时送任务在弱网下验证重试与对账。
- 前台 Service 连续运行 2 小时，观察内存、电量、通知是否正常。

## 7. 里程碑

### M0：兼容层可行性验证

**交付**
- 一个最小 Android 工程，包含 `libproot.so` + `libkdocs_cli.so` + 依赖。
- 在真机上执行 `auth status`、`version`。
- DNS 与 CA 适配代码。

**验收**
- 真机输出 `authenticated: false` 或 true，而不是 SIGSYS / DNS / TLS 错误。
- 记录 Android 版本、targetSdk、是否使用 loader、nativeLibraryDir 方案是否可用。

**停机点**
- 如果原生执行被阻断，切换备选方案：targetSdk 28 + 从 `filesDir` 执行。

### M1：WPS 授权闭环

**交付**
- `WpsRuntime.authorize`。
- `libxdgopen_shim.so` + `AuthCoordinator`。
- token 持久化、`auth status` 验证、退出授权。

**验收**
- 首次拉起浏览器完成授权；
- 杀掉 App 重开后仍为已授权；
- 覆盖安装后仍为已授权；
- 支持重新授权与退出。

### M2：Python/WPS 全链路接入

**交付**
- Chaquopy 工程与 Python 源码接入。
- `app/wps/android_runtime.py`。
- `app/wps/cli.py`、`app/api/bridge.py` 双路径适配。
- `wps_status` / `wps_preview` / `wps_upload` 在 APK 内可用。

**验收**
- 同一份 Excel + 测试副本，APK 与 Termux 输出一致；
- 额度耗尽、网络中断、token 失效文案一致。

### M3：App 主流程可用

**交付**
- 本地 HTTP 服务启动与 token 自动登录。
- WebView 加载现有前端。
- 订单处理、闪时送下单跑通。
- 前台 Service 保活。
- 存储权限、文件浏览适配。

**验收**
- 全新安装后，不接电脑、不进 Termux，完成一次订单处理。
- 屏幕熄灭后任务继续运行，重开 App 可看到进度。

### M4：长期使用与分发

**交付**
- GitHub Releases 构建与签名。
- 应用内更新。
- 首次引导、诊断页、日志导出。
- 许可证与第三方组件清单。
- README 增加 APK 安装说明。

**验收**
- 从 Release 安装，覆盖升级不丢数据；
- 清洁设备按引导完成 WPS 授权；
- 卸载重装走完整首次流程。

## 8. 风险清单

| 风险 | 影响 | 缓解 |
|---|---|---|
| targetSdk 高版本禁止执行 native 库 | 兼容层不可用 | M0 真机验证；备选 targetSdk 28 或改从 filesDir 执行 |
| OEM SELinux / seccomp 更严 | 部分机型 proot 失败 | 真机矩阵测试；错误上报；必要时内置静态 proot |
| DNS 在 VPN/fake-ip 下失效 | WPS 调用全挂 | 优先系统 LinkProperties DNS，fallback 公共 DNS，支持手动 DNS |
| kdocs-cli 内部协议变更 | 云文档同步失效 | 固定版本、随 App 升级、只读对拍、诊断页显示 raw 错误 |
| WPS 风控 / 额度 | 用户无法同步 | 提示 429001/429002；不做规避；文档说明 |
| GPL/LGPL 组件分发合规 | 分发法律风险 | 加 notices、提供源代码获取方式；重新评估 proot 依赖 |
| kdocs-cli 再分发条款不明 | 不能对外分发 | 使用前确认官方 Terms；备选让用户自行提供二进制 |
| 内嵌 Python 包体积/启动速度 | 体验下降 | ABI split、按需提取、启动自检、页面 loading |
| Android 14+ FGS 限制 | 长任务被杀 | 正确申报 foregroundServiceType、通知、权限与电池优化引导 |

## 9. AI 执行约定

给 AI 实现时的硬性规则：

1. **一次只做一个里程碑**，M0 不过不进入 M1。
2. **不改领域逻辑**：`app/order`、`app/ordering`、`app/wps` 的 planner/executor/长流程
   尽量保持不动；如必须改，先加接口和测试。
3. **桌面路径永远保持可用**：所有 Android 分支用 `YIKOU_APP_MODE=android` 隔离。
4. **已有测试必须全绿**：每完成一个模块跑一次：
   - `pytest -q`
   - `cd frontend && pnpm test && pnpm build`
   - `ruff check`（如环境允许）
5. **新增测试优先**：WpsRuntime、AuthCoordinator、SecureStore 必须有单元测试。
6. **不打印敏感信息**：token、密码、授权 code 不进日志。
7. **接口冻结**：`WpsRuntime.run` / `authStatus` / `authorize` 一旦稳定，不再随意改名。
8. **失败可回退**：任何 Android 初始化失败时，APP 至少能启动并展示诊断信息，
   不能白屏。

## 10. 待冻结事项

在 M0/M1 之前需要确定：

- [ ] Chaquopy 最终版本与 Python 版本。
- [ ] targetSdk：35 还是 28（由 M0 真机结果决定）。
- [ ] `libproot.so` 是否能独立运行；是否需要打包 Termux 的 loader。
- [ ] 假 xdg-open shim 的编译方式（NDK CMake 或预编译）。
- [ ] token 目录是否需要 Android Keystore 二次加密。
- [ ] v1 是否确定使用 `MANAGE_EXTERNAL_STORAGE`。
- [ ] APK 签名 keystore 的保管责任人与 CI Secrets 配置。
- [ ] 是否公开源码、第三方组件许可证如何处理。
