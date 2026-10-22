# Future Trend Implementation Plan

> **For agentic workers:** Execute inline with superpowers:executing-plans, task by task.

**Goal:** 自动查询公开资料，生成可追溯的 3/5 交易日多空分析与验证。

**Architecture:** 独立 future_trend 模块负责资料、期限及结果约束；通过现有快照和执行器接入。已有 JSON 字段保存资料和双周期指标，不增加数据库表。

**Tech Stack:** Python / httpx / stdlib HTMLParser / FastAPI / unittest / 原生 JavaScript。

**Spec:** docs/superpowers/specs/2026-10-01-future-trend-design.md

## Global Constraints

- 不修改 CTP；不下载软件；不改动此前未提交工作。
- 未来判断只用于研究；复盘 opt-out 不发送任何总结。
- 修改 backend 后直接重启 stop.bat/start.bat 并验证端口和 401。

## Task 1: 期限、资料与快照

Files: backend/ai_analysis/future_trend.py, snapshot.py; tests/test_future_trend.py。

- [x] 用 unittest 验证国庆后第 3/5 日日期、资料去重/未来发布日期排除、时间字段和抓取失败。
- [x] 实现 targets(instrument, now, reference_day, reference_price)、collect_materials(name, now)，均返回可序列化 dict。
- [x] build_snapshot 新增 FUTURE_TREND 分支，冻结闭合三周期行情和资料；运行新测试。

## Task 2: 分析契约和后端接口

Files: future_trend.py, provider.py, orchestrator.py, api/ai_routes.py。

- [x] 测试双期限方向、伪造引用/不完整维度/错日期拒绝。
- [x] parse_analysis_json 校验两项 horizon_forecasts 与 dimensions，接入独立提示词，保留证据验证。
- [x] 请求枚举和批次输出增加目标及公开资料，容量不足明确 CONTEXT_LIMIT；运行 AI 回归。

## Task 3: 双周期验证

Files: settlement.py; tests/test_future_trend.py。

- [x] 测试第 3 日已到期、第 5 日未到期和目标分钟缺失。
- [x] 保存 future_metrics，分别到期验证；第 5 日完成后聚合评分，保留人工评价。
- [x] 运行结算及 AI 回归。

## Task 4: 页面和交付

Files: frontend/index.html; tests/check_future_trend.cjs。

- [x] 新增入口、历史分组、两个期限卡片、维度、来源链接及自动验证结果。
- [x] 节点测试验证新类型、安全文本渲染及现有分时行为。
- [x] 实测公开资料入口，运行相关 unittest 与五项现有前端检查。
- [x] 重启服务并验证端口/401，交付实现与验收边界。

Validation: 85 AI/calendar/future unittest tests and 7 market improvement tests passed; six frontend Node checks passed. Public NBS and Eastmoney endpoints were exercised. Backend restarted and health verification recorded in the task. Real model invocation and browser acceptance remain unverified.
