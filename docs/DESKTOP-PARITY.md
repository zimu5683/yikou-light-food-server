# 网页版 vs 桌面版：功能差异与补齐记录

对比基线：桌面仓库 `zimu5683/yikou-light-food-desktop`（v3.5.1）
与当前网页服务端（`zimu5683/yikou-light-food-server`）。

## 结论先说

- 三个任务模式（订单处理 / 云文档同步 / 闪时送下单）网页端都已具备。
- 桌面版 v3.5.1 中**唯一能直接应用到网页端的功能修复**是 WPS
  “排序区探测 / 东湖中餐排不了序”，已在 2026-09 移植并补测试。
- 其余桌面版独有能力属于原生桌面运行时或自动更新链路，网页端**有意不移植**：
  网页端本身就是实时改代码 + `git pull` 更新，而且它跑在手机服务器上，
  没有原生窗口和可执行文件替换场景。

## 已补齐的网页端功能

### WPS 排序区探测（来自桌面 v3.5.1）

问题：WPS `sheetsInfo.colTo` 被整列/到最右列操作污染后可能返回 `16383`。
旧网页实现直接把它当“最后使用列”，算出辅助列 `16385`，超过
`MAX_SORT_COL` 后排序被静默跳过 —— 表现就是“东湖中餐排不了序”。

已在网页端移植：

- `SORT_PROBE_WIDTH = 40`
- `effective_last_col()`：识别被污染的 used range，超界时退回已读范围
- `content_last_col()`：以实际读到的内容修正最后使用列
- `probe_sort_area()`：排序前/排序后探测辅助列右侧是否有内容
- `apply_plan()`：
  - 排序前右侧有内容 → **回滚新插入行**并报错，宁可不动也不排错位
  - 排序后右侧出现内容 → 停止后续写入并提示重新上传
- `_build_sheet_plan()`：
  - 宽度按“used range 与真实内容”共同决定
  - 关闭排序 / 表里暂无数据行时给出明确 warning
- `apply_plan()` 结果新增 `sort_skipped` 字段，日志会显式说明“本次未排序”

对应测试（从桌面版移植回归）：

- `test_bogus_used_range_does_not_disable_sorting`
- `test_sort_refuses_when_content_sits_past_the_scan_range`
- `test_probe_skips_the_helper_column_itself`

反证：临时把 `planner.py` 回退到修复前版本，新增测试确实报红：
`AssertionError: 应当排序；warnings=[...表格宽度异常...辅助列 16385]`；
恢复修复后全绿。

## 网页端已有、桌面版没有的能力

| 能力 | 说明 |
|---|---|
| HTTP 服务器 | 手机/Termux 当主机，其他设备浏览器访问 |
| 账号体系 | 注册申请 → 管理员审批 → 邀请码 → 会话 |
| 角色权限 | 普通用户方法白名单 + 敏感配置字段裁剪；后端强制 |
| Cloudflare Access | `/admin` 可选 JWT 验签 |
| 服务器端文件浏览器 | 选的是跑任务主机上的 Excel 路径，而不是客户端文件 |
| 移动端日志抽屉 | 手机端底部抽屉，高度可拖拽并记忆 |
| 双通道更新检查 | 网页仓库才提示“网页版更新”；桌面仓库只发独立提示 |
| Termux kdocs-cli 适配 | proot + `SSL_CERT_FILE`，Android 上可直接跑 WPS CLI |

## 桌面版有、网页端有意不移植的能力

| 桌面能力 | 不移植原因 |
|---|---|
| pywebview 原生窗口 | 网页运行在浏览器里，没有原生窗口可控制 |
| 最小化/最大化/关闭/标题栏拖拽 | 浏览器标签页由操作系统/浏览器管理 |
| 原生文件选择/保存对话框 | 换成服务器端文件浏览器（语义更正确） |
| 内置 Chromium + Playwright 浏览器模式 | 网页服务端纯 HTTP 接口即可；浏览器模式数百 MB，且 Android 上无必要 |
| 页面 locators / 浏览器改版适配 | 同上，纯接口模式不依赖页面结构 |
| 自动更新：签名清单 / bspatch / 替换二进制 / 健康标记 | 网页端是源码实时修改 + `git pull`；自动替换代码风险高，且你已明确不需要 |
| Windows/macOS/Linux 打包流水线 | 网页端部署方式不同 |
| Wayland/X11 中文输入法适配 | 桌面 GUI 专属问题 |

## 仍需继续对比的地方

这次只补了桌面 v3.5.1 里能直接复用的业务修复。后续桌面版若继续发布新版本，
对比方法可以固定为：

```bash
# 1) 拉取桌面版源码
curl -L -o ~/yikou-desktop.tar.gz \
  https://codeload.github.com/zimu5683/yikou-light-food-desktop/tar.gz/refs/heads/main
mkdir -p ~/.cache/yikou-light-food-desktop
tar xzf ~/yikou-desktop.tar.gz -C ~/.cache/yikou-light-food-desktop --strip-components=1

# 2) 对比业务函数名（排除 browser/locator/updater/window）
#    重点看 desktop-only 且非桌面专有的函数
```

网页端有 `tests/test_architecture_boundaries.py` 锁死分层，任何移植都不会破坏架构边界。
