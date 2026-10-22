# 仓单与日行情独立采集 Implementation Plan

> **For agentic workers:** 使用 writing-plans 与 test-driven-development，在当前已授权任务内逐项执行和复核。

**Goal:** 交付配置驱动、可离线验收、幂等且按通道隔离的仓单/行情采集程序。

**Architecture:** 在 `data_collection/` 独立实现，不导入现有服务启动或 CTP。SQLite 三表存储，仓单明细与主力日行情分别采集并按日期/品种合并；保留总计、集团与叶子明细层级。默认配置不启用未经核实的线上入口。

**Tech Stack:** Python 3.11、httpx、PyYAML、BeautifulSoup、sqlite3、pytest。

**Spec:** 本聊天用户给出的八项交付物与硬性约束，以及用户批准的五项口径修正。

## Global Constraints

- 所有接口地址、鉴权模板、选择器和字段映射均从 YAML 读取，秘密引用环境变量/本地 .env。
- 官网请求间隔至少8秒，失败指数退避、429 Retry-After、robots、User-Agent、响应上限；测试不打线上。
- 显式交易日历驱动，不推测工作日等于交易日；样例日历只能用于 fixture 模式。
- 无权威换算依据时吨值为空；总计/集团/明细不重复求和。
- 断网、超时、限流和结构变化必须落日志；失败品种不入主表，其他通道继续。
- CTP、郑商所既有通道、现有服务数据库与用户脏文件保持现状。

## Tasks

- [x] 配置与解析：新增 `data_collection/config.example.yaml`、`config.py`、`parsers/`、`tests/fixtures/`；先写明确总计、层级、日期、缺字段、未知单位的离线测试，验证失败后实现。
- [x] 存储与校验：新增 `db/schema.sql`、`storage.py`、`validators.py`；以重复执行、市场先到/仓单先到、历史勾稽、参考对账、异常拒绝测试驱动。
- [x] 采集与传输：新增 `collectors/base.py`、`czce_receipt.py`、`dce_receipt.py`、`market.py`、`transport.py`；用 httpx MockTransport 验证断网/超时/429、robots、共享限流与最终日志，不发真实官网请求。
- [x] 编排与运行：新增 `scheduler.py`、`cli.py`；实现 run/backfill/dry-run/self-check、日历范围、恢复、更新轮询、失败告警、通道隔离；CLI 子进程检查 dry-run 无数据库文件和退出码。
- [x] 交付：新增 README、独立 requirements、验收记录；执行独立 pytest、离线 CLI、自检、既有研究模块回归。仅新独立模块无需重启现有后端。

## Acceptance Commands

```powershell
.venv\Scripts\python.exe -m pytest data_collection/tests -q
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/config.example.yaml self-check
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/tests/fixtures/demo.yaml run --date 2026-09-30 --dry-run
```

真实接入验收另行依赖正式交易日历、用户提供的郑商所正常正文、CFMMC 同口径可访问资料以及主力日行情数据源。离线样例不能作为这些入口已经接通的证据。
