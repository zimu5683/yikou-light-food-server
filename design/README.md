# design/ 索引（历史过程资料）

> **当前行为只看 [README.md](../README.md) 与 [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)。**
> 本目录保存的是开发过程资料（方案、验证记录、快照），只在追证据、查历史决策时翻。
> 其中出现的 app/xxx.py 之类路径多写于 2026-09 重构之前，已不再对应现在的文件；
> 不要照着这里改代码，也不要把当前行为写进这里。

## 仍然有效的方案与规则

- [WPS-CLOUD-SYNC-PLAN.md](WPS-CLOUD-SYNC-PLAN.md) — WPS 云同步总体方案、列定位与地址排序规则
- [SSS-云端名单导入.md](SSS-云端名单导入.md) — 闪时送云端名单的日期口径、地址过滤与拒绝语义
- [APK-PLAN.md](APK-PLAN.md) / [APK-STATUS.md](APK-STATUS.md) — Android APK 方案与状态
- [DESIGN-WEB.md](DESIGN-WEB.md) — 网页版界面设计说明
- [公网访问现状与域名阻断.md](公网访问现状与域名阻断.md) — Cloudflare Tunnel / 域名阻断证据与切换步骤

## 历史归档

阶段总结、迭代日志、真机验证清单、早期重写状态、快照与视觉素材等已陆续移入
[archive/](archive/)；只在追证据时看，不要当作当前行为依据。

> 本文件与仓库内其他文档的引用由 [tests/test_doc_references.py](../tests/test_doc_references.py) 看守。
