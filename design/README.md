# design/ 索引（历史与验证资料）

这个目录保存的是**开发过程资料**：方案、真机验证记录、快照与交接文档。
其中提到的 `app/xxx.py` 路径多写于 2026-09 重构之前，已经不再对应现在的文件，
只作证据链和历史参考；当前代码结构请看 [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)。

## 方案与规则（仍然有效）

- `WPS-CLOUD-SYNC-PLAN.md` — WPS 云同步总体方案、列定位与地址排序规则
- `SSS-云端名单导入.md` — 闪时送云端名单的日期口径、地址过滤与拒绝语义
- `DESIGN-WEB.md` — 网页版界面设计说明
- `公网访问现状与域名阻断.md` — Cloudflare Tunnel / 域名阻断证据与切换步骤

## 测试与验证记录

- `SSS-TIMING-FINDINGS.md` — 闪时送列表延迟、预筛与对账实测结论
- `WPS真机验证清单-20260913.md` / `WPS真机验证清单-20260915.md` — WPS 真机验证清单
- `sss-timing-raw/` — 上述时序结论的原始输出
- `M7-ACCEPTANCE.md` — M7 验收记录

## 交接与阶段总结

- `交接文档-WPS云同步.md` — WPS 同步运维/交接
- `工作总结与验证交接-20260912.md` — 阶段总结
- `迭代进展.md` — 多轮迭代日志（含重构前的模块路径）
- `REWRITE-STATUS.md` — 网页版重写状态
- `云端测试文件清理清单.md` — 云端测试副本清理清单

## 快照与素材

- `WPS基准快照.json` / `WPS基线存档.json` / `WPS正式表快照-20260913.json` — 只读快照
- `references/` — Linear / Vercel 视觉参考
- `mocks/` — 早期视觉方向稿、最终稿与截图
- `legacy/DESIGN.md` — 桌面端时代设计文档
