# 一口轻食 Web 前端

React 19 + TypeScript + Vite 8 + Tailwind 4，构建为单文件产物供
`app/web/server.py` 静态提供。

## 开发

```bash
pnpm install
pnpm dev        # Vite 本地开发；无后端时会退化为 mock 数据
pnpm build      # 产物写入 frontend/dist/index.html
pnpm test       # Node 内置 test runner，覆盖 lib/ 纯函数与 bridge 事件去重
pnpm lint       # oxlint
```

注意：Python 服务读取的是 `frontend/dist/index.html`，改完前端后必须重新
`pnpm build`；仓库根目录的 CI 也会执行 build/lint/test。

## 目录

- `src/lib/bridge.ts` — HTTP/桥接传输、事件分发与全部接口类型
- `src/lib/` — 格式化、主题、日志面板几何与扩散圆心等纯函数与 hooks
- `src/hooks/useApp.tsx` — 全局状态与后端事件到 React 的唯一入口
- `src/components/` — 业务组件；`components/ui/` 为无领域 UI 原子
- `src/App.tsx` — 手机/平板/桌面三种响应式布局

## 浏览器交互检查与变异检查

```bash
pnpm build
node scripts/browser-interaction-check.mjs      # 合成 mock 后端 + 本机 Headless Chrome，退出码非 0 表示有断言失败
SCENARIO=uncertain-operation-as-success node scripts/mutation-check.mjs   # 只跑 FE-1（R6-8）变异
node scripts/mutation-check.mjs                 # 全部变异场景
```

- `scripts/browser-interaction-check.mjs`：只服务 `dist/index.html`，`/api/*` 全部是脚本内合成 JSON
  与请求计数；不连真实 WPS/闪时送、不读系统凭据、不写正式文件。断言基于真实渲染与真实点击。
- `scripts/mutation-check.mjs`：把 `frontend/` 复制到临时目录（`node_modules` 走符号链接），在副本里
  注入变异并重新构建，要求「指定断言必须变成 FAIL、控制组必须仍然 PASS」，用来证明断言不是空转。
- FE-1（R6-8：uncertain 不得被渲染成“已完成”）的缺口、断言清单与三段式证据：
  `docs/OPTIMIZATION-R6-8-FE1-BROWSER-GATE.md`。

## 手机端日志：悬浮按钮 + 水波扩散

手机布局（<640px）下日志面板由右下角悬浮按钮 `components/LogFab.tsx` 开合，
展开时用**圆形 clip-path 从触发按钮的圆心扩散**：

- `lib/reveal.ts` — 纯函数：圆心到面板四角的最大距离（半径）、裁剪值、降级判定；
- `lib/useLogReveal.ts` — 开合状态机：谁触发、圆心在哪、第几次展开；
- `lib/useDockHeight.ts` — 实测底部操作栏高度，悬浮按钮据此浮在操作栏上方。

触发点：悬浮按钮；操作栏的「开始处理/开始下单」（点击时只记圆心，任务真的
起来才展开）；云文档的「确认上传」。平板/桌面仍是日志常驻分栏，不受影响。

接口字段与 Python 返回值的契约由仓库根目录 `tests/test_frontend_contract.py`
反向校验，改字段名时两边要一起改。
