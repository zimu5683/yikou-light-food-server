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
- `src/lib/` — 格式化、主题、日志抽屉几何等纯函数与 hooks
- `src/hooks/useApp.tsx` — 全局状态与后端事件到 React 的唯一入口
- `src/components/` — 业务组件；`components/ui/` 为无领域 UI 原子
- `src/App.tsx` — 手机/平板/桌面三种响应式布局

接口字段与 Python 返回值的契约由仓库根目录 `tests/test_frontend_contract.py`
反向校验，改字段名时两边要一起改。
