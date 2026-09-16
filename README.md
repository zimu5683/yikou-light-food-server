# 一口轻食（网页版）

这是一个**网页版专用**的订单处理服务：Python 提供 HTTP 接口 + React/TypeScript/Tailwind 前端，
可跑在手机（Termux）上当服务器，其他人用浏览器访问。账号密码不会写入源码；运行主机上的
密码通过系统密钥环（`keyring`：Windows Credential Manager / macOS Keychain /
Linux SecretService）保存，没有可用密钥环时退化为每次运行手动输入。

> **架构说明**：本项目原先同时提供桌面原生窗口版（pywebview）与浏览器自动化备用模式
> （Playwright）。两者已**全部移除**，只保留纯接口模式：
> - 桌面窗口层、PyInstaller 打包、三平台发布流水线、自动更新器 —— 已删；
> - Playwright 浏览器模式与页面定位器（`locators.json`）—— 已删。
>
> 现在只有一条路径：**HTTP 调用平台接口**。

此项目是本人自用，代码功能不完善，还有许多需要改进的地方，项目公开，大家也可以以我项目为基础开发出更完整功能的项目。

程序包含三个任务模式：

- **订单处理**：登录管理后台，读取最新订单并写入排单 Excel；
- **云文档同步**：把本地排单表的内容增量写入 WPS 云端的排单表（见下文）；
- **闪时送下单**：从独立的《闪时送.xlsx》读取订单（午餐/晚餐两表），在闪时送平台逐单创建预约单。

固定使用**纯接口模式**：直接调用平台 HTTP 接口完成登录、读单和下单，不启动任何浏览器。

## 网页版（手机 / 服务器当主机）

不想开原生窗口，或想把跑任务的那台机器当服务器、用**其它设备**（电脑、平板、另一台手机）
打开网址来操作时，用网页版：

```bash
python run.py --web                  # 默认监听 0.0.0.0:8756（同一 WiFi 下其它设备可访问）
python run.py --web --port 9000      # 换端口
python run.py --web --host 127.0.0.1 # 只允许本机访问
python run.py --web --new-token      # 轮换访问令牌
```

启动后终端会列出可直接点开的网址（含访问令牌），例如：

```
本机访问：   http://127.0.0.1:8756/?token=XXXX
其它设备访问：http://10.103.85.74:8756/?token=XXXX  [wlan0]
```

把「其它设备访问」那条网址在电脑/平板上打开即可。网址里的 `?token=` 只要带一次，
之后会被浏览器记住；令牌本身保存在用户配置目录的 `web_token`。

### 它是怎么跑的

- 页面由 `app/web_server.py` 提供：静态前端 + `POST /api/<方法名>`，业务逻辑全在
  `app/bridge.py`；事件是「Python 追加 + 前端按 cursor 轮询」，因此不需要 WebSocket；
- **选择 Excel 文件**用服务器端文件浏览器：任务在主机上跑，Excel 也在主机上，
  所以浏览并选择的是**主机上的路径**，而不是打开网页那台设备的文件；
- 没有原生窗口，标题栏不提供最小化/最大化。

### 账号与访问审批

网页版等于把这台机器的完整操作权限交给「能进得来的人」——它能读取管理后台密码、
使用 WPS 云文档授权、并真实下单。因此除了令牌，还有一套**账号体系**：整站
（包括首页和全部静态资源）都要求登录，未登录一律跳 `/login`。

- **注册申请**：访客在登录页填账号密码提交申请，进入待审批队列；
- **管理员审批**：`approved` 之前**密码正确也登不进来**；管理员在 `/admin` 点同意/拒绝；
- **邀请码**：管理员可生成邀请码，凭码注册直接通过，省一轮人工审批；
- **会话**：登录成功发 `HttpOnly` 会话 Cookie（30 天）；访问 `/api/*` 靠会话，
  失效/被拒/改密后旧会话立即失效。

管理员账号用命令行维护（密码不会写进源码，磁盘上只存 PBKDF2 哈希）：

```bash
python run.py --web --create-admin            # 交互式创建第一个管理员
python run.py --web --create-admin --username you@example.com   # 指定账号名
python run.py --web --reset-admin-password    # 忘记密码时重置
python run.py --web --list-users              # 列出账号与审批状态
```

账号数据落在用户配置目录的 `users.json`（手机上是 `~/.config/yikou-light-food/`）。

### 安全须知（重要）

- **令牌只对本地/局域网直连有效**：`?token=` 是忘记管理员密码时的自救入口，一旦请求
  经网关（带 `CF-Connecting-IP` / `X-Forwarded-*`）进来就**一律失效**，不能绕过审批；
- 默认只监听局域网；要对外发布请走下面的 Cloudflare Tunnel，**不要做端口映射**
  （手机在运营商 CGNAT 后面，本来也映射不通）；
- 服务自身只提供 HTTP（无 TLS）。公网入口的 HTTPS 由 Cloudflare 边缘终结，
  所以隧道部署下浏览器地址栏是有效证书；纯局域网 HTTP 理论上可被嗅探；
- 管理页（`/admin`）建议再叠一层 Cloudflare Access：设好 `YIKOU_ACCESS_TEAM_DOMAIN`
  与 `YIKOU_ACCESS_AUD` 后，该页会校验 `Cf-Access-Jwt-Assertion` 的签名（只看明文
  请求头是可以伪造的，所以必须验签）。

### 部署到公网（Android / Termux + Cloudflare Tunnel）

> ⚠️ **已知阻断（2026-09-16 实测）**：当前使用的域名 `zimu5683.kdns.fr`
> **在中国大陆被 SNI 阻断** —— 服务端与隧道完全正常，但国内直连打不开，
> 必须走代理才能访问。已逐一排除所有免费域名方案（noip / ddns.net / duckdns /
> eu.org / afraid 等同类后缀均在过滤名单内）。
> **换一个未被阻断的域名即可解决，服务端无需改动。**
> 完整证据链、已排除方案与换域名步骤见
> [`design/公网访问现状与域名阻断.md`](design/公网访问现状与域名阻断.md)。

手机在 CGNAT 后面没有公网 IP，用 Cloudflare Tunnel 由手机主动向 Cloudflare 建出站
连接，公网请求沿该连接回源，因此不需要公网 IP、不需要端口映射、也不需要 DDNS。

服务用 runit 托管（崩溃自动拉起，开机自动启动）：

```bash
termux-wake-lock                     # 防止息屏后被挂起
sv status yikou-light-food           # 查看状态
sv restart yikou-light-food           # 重启
tail -f $LOGDIR/sv/yikou-light-food/current   # 实时日志
```

开机自启依赖 **Termux:Boot**（F-Droid 安装，必须**手动打开一次**才会生效），
脚本在 `~/.termux/boot/start-services`。厂商省电策略、电池优化、自启动白名单都要
手动放开，否则撑不过一天。

#### 手机上开着 VPN / 代理时：必须用 http2

这是实测踩到并解决的坑。`cloudflared` 默认用 **QUIC（UDP 7844）** 连 Cloudflare
边缘，而手机上的 VPN/代理（Clash、Surge 一类，特征是 tun0 网卡 + `198.18.0.0/15`
fake-ip）会破坏 UDP，症状是：

```
UDP Connectivity  region1.v2.argotunnel.com  FAIL   QUIC connection failed
TCP Connectivity  region1.v2.argotunnel.com  FAIL   HTTP/2 connection is blocked or unreachable
ERROR: Allow outbound QUIC traffic on port 7844 or use HTTP2.
```

隧道会一直 `inactive`，公网访问得到 **Cloudflare 1033**。注意那些 precheck 的
**TCP / API 项是假警报**（`api.cloudflare.com` 明明能用，隧道 API 也建成功了），
真正的问题只在 QUIC。

解决办法：**在服务的 run 脚本里给 cloudflared 加 `--protocol http2`**。

```sh
# /data/data/com.termux/files/usr/var/service/cloudflared/run
exec cloudflared tunnel run --protocol http2 --token-file "$HOME/.cloudflared/token"
```

改完 `sv restart cloudflared`，日志里会出现
`Registered tunnel connection ... protocol=http2`，隧道变 `healthy`。

⚠️ **两个容易踩的坑**（都实际踩过）：

1. **不要指望 `~/.cloudflared/config.yml`**。包自带的 run 脚本不传 `--config`，
   写在那里的 `protocol` 不会被读取。参数要直接写在命令行上。
2. **YAML 里冒号后面必须有空格**。写成 `protocol:http2` 是不合法的，cloudflared
   会直接报 `Invalid config` 起不来。

另外，`pkg upgrade` 可能覆盖包自带的 run 脚本、把 `--protocol http2` 冲掉
（症状：服务显示在跑，但公网 1033）。用 `~/fix-http2.sh` 可一键重新应用。

日志里 `ip=198.18.0.x` 是 VPN 代理的转发地址，属**正常现象**，不要去"修"它。

### 手机（Termux）上长期当服务器的建议

```bash
pkg install termux-api        # 可选
termux-wake-lock              # 防止息屏后 Termux 被系统挂起（服务会跟着断）
termux-setup-storage          # 首次需要读取手机存储里的 Excel 时执行，并在弹窗里允许
```

不执行 `termux-setup-storage` 时文件浏览器打不开 `/sdcard`，会直接提示这条命令。

## 开发

```bash
python -m venv .venv
python -m pip install -r requirements.txt
cd frontend && pnpm install && pnpm build && cd ..   # 生成 frontend/dist/index.html
python run.py                                        # 无参数即启动网页服务
```

前端改动后必须重新 `pnpm build`，否则服务仍在提供旧的 `frontend/dist/index.html`。

### 在 Termux（Android）上开发

不需要 GTK/WebKitGTK，也不需要任何浏览器，因此在手机上是完整可跑的：

```bash
pkg install python nodejs-lts git proot
python -m venv --system-site-packages .venv
.venv/bin/pip install openpyxl keyring requests
pkg install python-cryptography                             # cryptography 没有 Android 轮子，用 Termux 包
cd frontend && pnpm install && pnpm build && cd ..           # 前端产物 frontend/dist/index.html
.venv/bin/python run.py                                      # 启动网页服务
```

已知限制：`ruff==0.15.22` 在 Android 上没有可安装的包，可用 `pkg install ruff` 代替，
但版本不同、默认规则集比 CI 更严，以 CI 结果为准。

### Termux 上的 kdocs-cli（云文档同步 / 闪时送云端名单）

kdocs-cli 的 `linux-arm64` 版本是**静态链接**的 aarch64 二进制，能在 Android 上直接运行
（`scripts/fetch_kdocs_cli.py` 或手动下载官方包放到 `vendor/kdocs-cli/kdocs-cli`）：

```bash
curl -L -o k.tar.gz https://wpsai.wpscdn.cn/skillhub/pro/v2.5.29/releases/kdocs-cli-2.5.29-linux-arm64.tar.gz
sha256sum -c vendor/kdocs-cli/checksums.txt      # 校验
tar xzf k.tar.gz -C vendor/kdocs-cli && chmod +x vendor/kdocs-cli/kdocs-cli
```

但**静态链接**意味着它不经过 Termux 对绝对路径的重写，在 Android 上必然踩两个坑，
程序已自动处理（见 `app/wps_cloud.py` 的 `termux_cli_runtime()`）：

| 症状 | 原因 | 处理 |
|---|---|---|
| `lookup ... on [::1]:53: connection refused` | 读不到 `/etc/resolv.conf`（Android 的 `/etc` 只读且没有它） | 用 `proot -b $PREFIX/etc/resolv.conf:/etc/resolv.conf` 包一层 |
| `x509: certificate signed by unknown authority` | 读不到 `/etc/ssl/certs/ca-certificates.crt` | `SSL_CERT_FILE=$PREFIX/etc/tls/cert.pem` |
| `SIGSYS: bad system call`（`auth login`） | Android seccomp 拦截 `faccessat2` | 同样靠 proot 接管系统调用 |

因此 Termux 上 **`pkg install proot` 是云文档功能的硬依赖**。

授权：界面上的「去授权」会跑 `kdocs-cli auth login`，把授权链接打进日志，在手机浏览器里
打开确认即可。Android 没有系统密钥链，CLI 会自动退化为加密文件
（`~/.config/kdocs-cli/token.enc`）。若已在电脑上授权过，也可以用
`kdocs-cli auth set-token <token>` 直接导入 Token。

## 云文档同步（WPS 云端排单表）

「云文档同步」页签把本地《排单.xlsx》的内容**增量写入**协作者维护的 WPS 云端排单表，
不需要整表覆盖，因此云端的公式、自定义排序、字体和列宽都不会被破坏。

### 它做什么

对本地排单表的每个子表（东湖/衣锦/医学院 × 中餐/晚餐）：

1. 在云端表里按内容定位**目标日期列**（只比对「月.日」，忽略星期文字）；
   **只在「电话列 ~ 类型列」之间的日期区里找** —— 备注右侧协作者写的
   「9.14 周一」是标记，不会被当成日期列，也不会被写进任何数字；
2. 按【名字 + 电话】找到客户所在行；
3. 旧客户：把**目标日期格写 1**，**总餐次写成与本地「餐次」一致**；
4. 新客户：
   1. 把这一批新客户**统一插到第 3 行与第 4 行之间**（插入的行会自动继承上方格式）；
   2. 填名字/地址/电话/类型/餐种，并按模板行刷底色（经济餐照抄模板、豪华餐整行金黄）；
   3. 按**列B 的地址顺序重排整张表**（见下），新客户落在自己地址组里；
   4. 再写目标日期格 = 1、总餐次、`已出餐 = SUM(日期列)`、`剩余餐 = 总餐次 − 已出餐`；
5. 可选：在备注列右侧第 3 列写协作者的**通讯记号**（周日 1、周一 2 … 周六 7）。
   备注+2 那格是协作者自己的日期标记，程序只读不写。

**总餐次写的是绝对值而不是累加值**，所以同一天重复上传不会翻倍——
云端已经是目标状态时，程序一个格子都不会写。

### 地址顺序（列B 的排列规则）

新增客户时程序会把整张表按列B 重排。顺序在「云文档同步」页签的**地址排序**里配置：
一行一个地址，从上到下就是顺序；**留空 = 按地址升序排列**（医学院用这种，
`医2号` 会排在 `医10号` 前面）。

| 子表 | 默认顺序 |
|---|---|
| 东湖中餐 / 东湖晚餐 | 小、大西、A1~A6、b1~b12、C1~C12、D1~D12 |
| 衣锦中餐 / 衣锦晚餐 | 外卖柜、校门口 |
| 医学院中餐 / 医学院晚餐 | （留空）按地址升序 |

- 清单里没有的地址（例如「学三」「碳汇楼」）**统一排到表格最后面**，并在预览里列出来；
- 匹配地址时忽略大小写和空格（本地写「B5」会按清单落成「b5」）；
- 同一个地址组里，**原有客户在前、本次新增的在后**；
- 排序只覆盖「第 3 行 ~ 最后一个有姓名的行」，表尾的合计/说明不会被移动；
- 排序用的是**云端原地排序**（保留格式与公式）+ 一列临时排序键，排完即删；
- 排序完成后程序会**回读列A~C 重新定位每个人的新行号**再写数据，绝不按预测行号硬写。


### 目标日期怎么算

网站 21:00 截止、程序通常在晚上运行，因此：

| 运行时刻 | 写入云端哪一列 |
|---|---|
| 20:00 ~ 次日 10:00 | **运行日 + 1 天** |
| 其它时刻 | 运行日 |

窗口起止小时可在配置里调整（`wps_target_hour_start` / `wps_target_hour_end`）。

### 首次使用

1. 「云文档同步」页签 → **去授权**，在浏览器里用你的 WPS 账号确认一次；
   token 由 CLI 存进系统密钥链，约一年内无需重复授权；
2. 保持**测试模式**（默认开启），点「预览」确认要改的内容；
3. 确认无误后点「确认上传」，去云端核对结果；
4. 核对通过后关闭测试模式，正式启用。

**测试模式**下每张正式表都会被替换为对应的测试副本（`vendor` 之外，云端名称以「测试-」开头），
所以测试期间绝不会碰正式排单表；测试模式也不写协作者通讯记号。

### 安全边界

- 上传前必须先「预览」，确认按钮在预览成功前不可点；
- 找不到目标日期列 → 不写、不建列，只提示（通常是协作者还没加当天的列）；
- 写入后逐格回读校验（含"这个行号上确实是这个人"），校验不通过则报告并不更新本地账本；
- 排序失败 → 把插进去的新行整块删掉，云端恢复原状；
  **排序成功之后**的一步失败则不再删行（新行已散落到各地址组，删行会删错人），
  只报告并提示重新上传——重复执行是安全的；
- 本地账本在用户配置目录的 `wps_sync_state.json`，只用于留痕与状态展示；
- 任何失败都只写日志，**不会影响本地排单任务**。

### 组件来源

云文档读写依赖金山官方 CLI `kdocs-cli`（无 PyPI 包，只能随包分发）。
构建脚本会调用 `scripts/fetch_kdocs_cli.py` 按平台下载并用官方 `checksums.txt`
校验 sha256；运行时按「打包内置 → 仓库 vendor → 程序同目录 → PATH」顺序查找，
也可以在页签里手动指定路径。详情见 `vendor/kdocs-cli/README.md`。

## 数据与安全

配置、定位器配置与失败快照保存在用户配置目录（Windows：`%APPDATA%\yikou-light-food`），Excel 文件只在用户选择的位置读写。运行前会创建 `backups/` 时间戳备份。请不要将真实 Excel、日志、密码或浏览器缓存提交到 Git。

旧版脚本保存在 `legacy_一口轻食.py`，仅作参考，不是新程序的运行入口。

## 闪时送下单

切到「闪时送下单」页签后填写：闪时送网址、账号、密码、订单 Excel 文件（云端模式下是**留档文件**，可留空），以及下单时的「商品名称」与「常用地址」默认值。

### 名单来源

- **云端当天名单（默认）**：每次「开始下单」（含干跑/预检）之前，程序从 WPS 云端的
  《东湖午餐9月.xlsx》（内部名「东湖中餐」）与《东湖晚餐9月.xlsx》（「东湖晚餐」）里，
  取出**当天那一列**标了 `1` 的人，姓名/地址/电话直接进内存用于下单（不再从 Excel 读单）。
  - 「当天」的口径：**运行时刻 20:00 之后 → 识别次日**；其余时刻（含次日 00:00~10:00 的清晨）
    → 识别运行日。例：9.15 20:00 ~ 9.16 10:00 之间运行，识别的都是 `9.16`，下 9.16 的中午/晚上的单。
  - **地址是「大西」或「小」的人不下单**（含「小西」「带空格」等写法，忽略空格与大小写），其余地址照常下单。
  - 某张表**没有当天日期列**（例如周末不做晚餐）→ 那一餐不下单、另一餐照常，日志写明原因。
  - **下单前核对日期**：识别到的日期必须等于本次实际使用的送达日期（午餐 11:00 / 晚餐 17:00，
    当天 16 点后顺延次日）；不一致（例如 16:00~20:00 之间运行）→ **拒绝下单**，不登录、不提交。
  - 名单会**留档**写进《闪时送.xlsx》的 `午餐`/`晚餐` 两表（先清空 A:C 第 3 行起，电话按文本写入），
    并在 **E1** 写云端该日期列的表头原文（形如 `9.16 周三`）。留档失败只告警，不影响下单。
  - 云端读不到（未授权 / 缺 `kdocs-cli` / 当日额度用尽 / 表头异常）或要下单的人数据不完整
    （缺姓名、地址，或电话不是 11 位）→ **拒绝下单**并说明原因，提示改用「本地 Excel」人工下单。
    报错会**点名是哪个单元格、现在是什么值**（如「东湖中餐 第 106 行 刘卓雅：C106 现在是「0」，
    不是 11 位手机号」），照着去云端改即可；改完仍报错时先确认改的是云文档本身、且已保存同步
    （共享文档只有查看权限时改动不会生效）。
  - 每次下单固定 **4 次只读调用**（每张表 2 次），与云同步共用金山接口的每日额度（约 150~200 次，次日 08:00 恢复）。
  - 页签上的「读取云端当天名单」按钮可以只读取 + 留档（不下单），用于下单前先核对人数与日期。
- **本地 Excel**：旧行为，直接读《闪时送.xlsx》的名单（人工准备名单时的兜底），不做云端读取。
  - Excel 格式：`午餐`、`晚餐`两个工作表，第 1 行表头、第 2 行占位，从第 3 行开始为 A=姓名、B=门牌号、C=电话、D=送达时间（D 列暂不使用，送达时间由程序按规则计算；云端模式留档时 E1 = 当天日期）。
- 只读自检（不写云端、不写本地文件）：`python -m app.main --sss-import-check` 会打印
  `kdocs-cli` 路径/授权状态、生效目标表、当天日期、两张表当天列标 1 的人数与地址过滤后的人数。
- **登录需手动完成**：闪时送登录有图形验证码。程序会在页面内弹出验证码小窗，输入后自动完成登录并逐单下单。
- 下单默认采用 **at-least-once + 对账确认**：平台抓包报文未发现客户端幂等字段，因此不会向未知 schema 强塞字段。程序在下单前后查询站内订单、校验完整订单指纹和批次时间窗口，网络异常或超时不会自动重发 POST；如果平台后续确认支持幂等字段，可在配置中设置 `sss_idempotency_field` 启用稳定 `client_request_id`。
- 闪时送密码使用独立凭据名 `yikou-light-food-sss`，与管理后台账号密码互不覆盖。
