# APK 转型实施状态（对照 APK-PLAN.md）

> 更新时间：2026-09-17。目标是在不重写领域逻辑的前提下，把网页版收敛为
> 自带 kdocs-cli 运行环境的 arm64 APK。

## 结论

代码侧的 M0–M4 结构已经落地，并完成了本地验证（Python + 前端 + Gradle/Kotlin）：

- Python 全量测试：`917 passed`；
- 前端：`pnpm test` 53 项通过，`pnpm build` 通过；
- `scripts/fetch_android_runtime.py` 能从 Termux 仓库下载并校验 proot/libtalloc/
  libandroid-shmem、从官方 CDN 校验 kdocs-cli 2.5.29 linux-arm64、编译 xdg-open shim、
  生成 `runtime-manifest.json`；
- Android 构建侧在本机（Termux + OpenJDK 17 + Gradle 8.11.1 + Android SDK platform 35）
  实测通过：
  - `:app:compileDebugKotlin` / `:app:compileReleaseKotlin` / `:app:compileDebugAndroidTestKotlin`
    全部编译通过；
  - `:app:testDebugUnitTest` 通过（命令构造、DNS 过滤、错误映射、版本比较、filesDir 降级开关）；
  - `:app:lintDebug` 通过（修掉了 FileProvider `android:resource` 前缀错误）；
  - `:app:assembleDebug` 生成了 17 MB 的 arm64 调试 APK（含 Chaquopy Python 3.13 运行时、
    `assets/chaquopy/app.imy` 中的完整 `app/` 源码、`assets/dist`、6 个 kdocs/proot 运行时文件），
    `apksigner` 验证 v2 签名通过。
  - 注：本机只有 Python 3.14，无法执行 Chaquopy 的 host Python 3.13 pip 任务，
    所以本地 APK 内没有 `openpyxl/requests/cryptography` 第三方包；CI 的 Python 3.13
    步骤会补齐。上述 pip 依赖的 android/cp313 wheel 已用 `pip download` 验证可下载。
- 用 Termux 本地运行打包进 `jniLibs` 形态的运行时，已实测：
  - `libproot.so`（patchelf 后 RUNPATH=`$ORIGIN`）可独立运行并输出 `version`；
  - `auth status` 输出合法 JSON；
  - `auth login` 会把授权 URL 写入 `YIKOU_AUTH_URL_FILE`（xdg-open shim 生效）；
  - `auth set-token` 证明 kdocs-cli 在无桌面密钥链时使用
    `XDG_CONFIG_HOME/kdocs-cli/token.enc`，token 可持久化并被 `auth status` 读回。

因此计划第 10 节的三个 M0 阻塞点已有明确答案：

| 待冻结项 | 本地验证结论 | 采用方案 |
|---|---|---|
| proot 是否需要 Termux loader | 静态 kdocs-cli 与动态 xdg shim 在 Termux 上均无需 loader 即可运行；但 loader 已一并打包，设 `PROOT_LOADER` 兜底 | 两者都保留 |
| targetSdk | 仍需真机 nativeLibraryDir 执行结果 | 默认 35；`-PyikouTargetSdk=28` 时 WpsRuntime 会自动把 6 个组件复制到 filesDir/wps/runtime 执行 |
| Chaquopy / Python | 代码与现有 `app/` 兼容 Python 3.13；Chaquopy 17.0.0 官方支持 3.10–3.14 | Chaquopy 17.0.0 + Python 3.13 |

**尚未完成**：在真机上编译并安装 APK，跑通 `nativeLibraryDir` 方案、Custom Tabs
授权闭环和真实订单/闪时送任务。也就是说 M0/M1 的设备验收门还没有关闭，不能在
文档里写成“APK 已可用”。

## 里程碑状态

| 里程碑 | 代码 | 本地/静态验证 | 真机验收 |
|---|---|---|---|
| M0 兼容层可行性 | ✅ `WpsRuntime` + fetch 脚本 + CI | ✅ proot/version/auth status 已跑通 | ⏳ 待安装 APK |
| M1 授权闭环 | ✅ AuthCoordinator + xdg shim + SecureStore | ✅ 授权 URL 落盘、token 落盘/读回 | ⏳ Custom Tabs 待测 |
| M2 Python/WPS 接入 | ✅ Chaquopy + `android_runtime.py` + 双路径适配 | ✅ Python 单测覆盖 JSON 解析/授权轮询 | ⏳ APK 内 kdocs 全链路 |
| M3 App 主流程 | ✅ WebView + TaskService + 文件根适配 + 更新检查 + 等待交互通知 | ✅ 静态测试 + 现有前端契约 | ⏳ 真机全任务 |
| M4 分发/更新 | ✅ GitHub Actions + 原生 updater + README | ✅ 工作流静态检查 | ⏳ 签名/Release |

## 已完成的关键文件

- `app/wps/android_runtime.py`：Python ↔ Kotlin JSON 桥；集中处理 `run_cli`、
  `auth_status`、授权轮询、外链。
- `app/wps/cli.py`：`YIKOU_APP_MODE=android` 时走原生运行时；桌面/Termux 的
  subprocess + proot 路径保持不变。
- `app/api/bridge.py`：授权 worker 按平台分派；Android 外链走 Custom Tabs。
- `app/core/android_store.py`、`app/core/credentials.py`：Android Keystore 后端。
- `app/core/config.py`、`app/web/server.py`、`app/web/fs_browser.py`：
  `YIKOU_DATA_DIR` / `YIKOU_DIST_DIR` / `YIKOU_STORAGE_ROOTS` 适配。
- `android/`：完整 arm64 Gradle 工程（Chaquopy + Kotlin + WebView + 前台 Service +
  WpsRuntime + AuthCoordinator + SecureStore + AppUpdater）。
- `scripts/fetch_android_runtime.py`、`scripts/fetch_kdocs_cli.py --platform`：
  固定版本/固定 sha256 的构建准备。
- `.github/workflows/android.yml`：构建前端 → 下载运行时 → patchelf → assembleRelease →
  上传 APK + sha256。

## 仍需要用户确认 / 操作的事项

1. **APK 签名责任人**：把 release keystore 放到安全位置，并在 GitHub Secrets 配置
   `YIKOU_KEYSTORE_BASE64`（keystore 的 base64）以及
   `YIKOU_KEYSTORE_PASSWORD` / `YIKOU_KEY_ALIAS` / `YIKOU_KEY_PASSWORD`。
   未配置时 CI 产物是 debug 签名，只适合 M0/M1 对拍，不能发布。
2. **targetSdk 最终值**：先跑默认 35。若真机报 `SECCOMP_BLOCKED` / nativeLibraryDir
   拒执行，在 workflow 加 `-PyikouTargetSdk=28`；代码已自动改走
   `filesDir/wps/runtime` 复制执行，不需要再手动改 WpsRuntime。
3. **v1 是否使用 `MANAGE_EXTERNAL_STORAGE`**：当前已按计划取实现。若你对“所有文件
   访问权限”敏感，请告知，我改成 SAF 选择 + App 工作区复制。
4. **token 是否再加一层 Android Keystore 加密**：kdocs-cli 已落到
   `XDG_CONFIG_HOME/kdocs-cli/token.enc`，加上 `allowBackup=false` 已满足 M1；
   二次加密接口在 `SecureStore` 保留，但 v1 未启用。请确认是保持现状还是启用。
5. **是否公开源码 / 第三方组件清单**：依赖里 Chaquopy（MIT）、proot（GPLv2+）、
   kdocs-cli（官方再分发条款）的合规结论需要你拍板。
6. **CI 运行**：本机没有 Android SDK/JDK，无法输出最终 APK。请在 GitHub Actions
   运行 `Android APK` workflow，把 M0 真机结果回填本文件。

## M0 真机验收命令建议

安装 CI 产出的 APK 后，先不要做真实任务，只验证运行时：

1. 打开 App，等待 WebView 加载成功；
2. 进入「云文档同步」，看授权状态是否显示 `已授权/未授权`，而不是 `未找到组件`；
3. 若失败，在 App 启动失败页或日志里找 `errorCode`；
4. 按计划第 6.2 节跑 Android 10–15 与至少一个国产 ROM；
5. 把每个机型的 `nativeLibraryDir` 执行结果、是否有 VPN、DNS/TLS 错误码填回这里。
