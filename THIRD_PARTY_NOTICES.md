# 第三方材料与数据来源

根目录 MIT 许可证适用于本项目原创代码与文档，不改变第三方代码、依赖、商标或行情数据的权利。

- **GitHub Spec Kit**：`.specify/` 中的工作流、脚本与模板，以及 `.agents/skills/speckit-*` 中的相关材料，源于 [github/spec-kit](https://github.com/github/spec-kit)，包含本项目中文适配。保留 MIT 原始声明，见 [third_party/spec-kit-LICENSE](third_party/spec-kit-LICENSE)。
- **TradingView Lightweight Charts 4.1.3**：前端通过 CDN 加载，来源为 [tradingview/lightweight-charts](https://github.com/tradingview/lightweight-charts/tree/v4.1.3)，遵循其 Apache-2.0 许可证及 NOTICE/归属要求。图表库不是本项目原创代码，使用和再分发应保留其要求的声明。
- **Python、数据库与容器依赖**：`requirements.txt`、`codex_mcp/requirements.txt`（如存在）、Dockerfile 与 Compose 中引用的依赖分别遵循上游许可证；本仓库 MIT 不替代这些许可证。`openctp-ctp` 及底层柜台接口的使用须遵循其各自授权。
- **行情与公开资料**：新浪、东方财富、交易所等来源的数据、网页及接口由各自权利人提供。项目不授予数据再分发权，不保证接口长期可用。测试夹具仅用于验证解析与质量语义，不能视为商业数据授权；部署者自行核实访问和使用条件。

公开发布不包含 `接口/` 中的厂商 SDK、示例证书，也不包含 `docs/evidence/` 的采集网页与运行证据、浏览器验证原始输出、个人配置、账户、数据库或密钥。本机这些文件保持原状，完整历史备份仍为私有。历史设计或验收文档中的本地证据路径可能仅在维护者环境存在，不能据此宣称公开版本已通过真实行情或实盘验收。
