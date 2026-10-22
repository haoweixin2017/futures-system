# AI 盘时段模拟验证实施计划

> 执行技能：superpowers:subagent-driven-development。用户已批准本聊天上一条固定规则的模拟验证方案，当前会话连续实施，不重复请求批准。

**Goal:** 为按账户隔离的盘时段 AI 建立独立模拟账本，定时冻结预测，按结果公布后的可用报价模拟成交并统计全部成本。

**Architecture:** 新增 MySQL 计划、小节试验两张表；每个账户最多一个运行/停止中的计划。主服务每 2 秒扫描；只在用户从页面启动计划后，每小节开盘 15 分钟发起一次既有 CURRENT_SESSION 分析。纯函数处理等待触发、持仓、退出；不调用模拟账户或任何交易下单 API。

**Tech Stack:** Python/FastAPI/SQLAlchemy/MySQL/Redis；原生 HTML/JS；unittest、Node VM。

**Spec:** 本聊天用户批准的方案：FU2611 单合约；每小节15分钟一次；明确方向与触发/目标/失效；结果返回后交易；固定1手最多一仓；小节结束退出；实际费用；先20交易日；保存每笔与跳过原因。

## 全局约束与实施裁定

- 用户工作区已有未提交改动，继续当前 `codex/` 分支；不提交、不覆盖既有改动。冻结 CTP 全部跳过。
- 页面创建计划即明确启动有费用的定时分析，开发验证中不创建生产计划、不发真实模型请求；默认尚无计划。
- 固定 15 分钟、1 手、20 实际交易日。合约和模型、每边每手手续费、每边滑点跳数、每百万输入/输出 token 人民币价格由用户明确填入并冻结；改规则须停止后新建计划。
- 分析窗口限 due 至 due+60秒；错过只记录跳过，绝不补发或回填历史成交。每计划/小节唯一，崩溃重启不会重复模型调用。
- 接收真正调用、方向一致且完成规范校验的结果；复核失败/分歧/无法判断/缺触发/窗口不一致跳过。费用仍计入。
- 结果可用时间用 `run.completed_at`（既有 UTC 转北京时间），而不是快照点击时间。触发只能使用此时之后且当前新鲜的报价；买入参考卖一、卖出参考买一，滑点朝不利方向；跳空止损按可执行报价退出。
- 缺盘口/报价过期不虚构成交；结束时仅使用已观测的最后1分钟有效盘口，缺失标记无法验证。服务停止期间不推算触价先后。
- 报告包含交易净收益、全部模型费用（含跳过分析）、合计净收益、胜率、每笔均值、最大已实现回撤/最长连亏、日汇总、逐小节记录及未验证/费用未知统计。明确为新浪采样模拟；不是盈利证明。

## Task 1：纯成交与统计内核

文件：`backend/ai_analysis/paper_verification_engine.py`、`tests/test_paper_verification_engine.py`。

接口：`advance_trade(state:dict, quote:dict|None, now:datetime, costs:dict)->dict`，返回状态副本；`summarize_trials(rows:list[dict])->dict`。

状态公共字段：`status`（WAITING_TRIGGER/OPEN/CLOSED/SKIPPED/UNVERIFIABLE）、`direction`、`trigger_price/target_price/invalidation_price`、`available_at/session_end`（北京时间字符串）、成交与退出字段、last_quote字段；costs含commission_per_side/slippage_ticks/price_tick/volume_multiple。成交计算每边手续费，model_cost独立由服务传入统计。

- [x] 先写测试证明结果公布前不进场、买卖盘口/不利滑点、价格跳过止损、空价位与陈旧报价、结束缺报价不虚构、净费用/跳过费用/最大回撤。
- [x] 用 `.venv\Scripts\python.exe -m unittest tests.test_paper_verification_engine` 观察失败；实现纯函数并通过。
- [x] 示例断言：参考卖一100，1跳滑点/跳1，LONG成交101；下一盘口买一95跳过止损98，退出94，而非98；每边2手续费、乘数10，交易净收益 `(94-101)*10-4=-74`。

## Task 2：持久化、调度及账户 API

文件：新增 `backend/ai_analysis/paper_verification.py`、`paper_verification_models.py`、`backend/api/paper_verification_routes.py`、`tests/test_paper_verification.py`、`tests/test_paper_verification_api.py`；修改 mysql_client.py、ai_routes.py、routes.py、main.py。

- [x] 新 ORM `ai_paper_verification_plan`（账户/不可变配置/起止日/状态）与 `ai_paper_verification_trial`（plan+session_start唯一、批次/run、state_json、费用/状态/原因）通过既有 init_db create_all 建表。
- [x] 将既有创建分析逻辑提取为 `create_analysis_for_owner(owner,instrument,body)`，原用户 API 继续调用同一函数；定时验证复用容量门槛和凭据脱敏。
- [x] 实现 create_plan(owner,data)、list_plans(owner)、get_report(owner,id)、stop_plan(owner,id)、tick(now=None)。用户仅有一活跃计划，停止后不再分析，已有模拟仓位按下一有效报价退出；已有模型调用费用继续追踪。
- [x] API `/api/v1/ai/verification/plans` GET/POST，`/plans/{id}` GET（完整报告），`/plans/{id}/stop` POST。仅模拟登录账户，自身资源404隔离。
- [x] 固定20交易日期间每小节due记录，调度先持久化小节记录再发起一次分析；重启时不重发已记录的 SUBMITTING。缺行情、模型不可用或容量门槛计入原因。token缺失且价格非零标费用未知。
- [x] 先写隔离库测试：冻结配置/跨账户、due+15/唯一/过时跳过、真实结果返回时间而非冻结时间、NO_JUDGMENT跳过但计成本、停止与重启、模型未知用量、结束报告。stub dispatch，不联网。

## Task 3：页面入口与报告

文件：新增 `frontend/js/14-ai-verification.js`、`tests/check_ai_verification.cjs`；修改 index.html、13-boot.js。

- [x] 工作台右侧新增“模拟验证”入口；弹窗可选择具体合约（默认当前合约）、已有可用模型，明确填写上述四个成本参数，展示固定15分钟/1手/20日及“启动后会自动调用模型并产生费用”。仅按钮动作 POST启动。
- [x] 列表/选中计划的账户、合约、配置、状态和收益统计；逐笔实际成交/退出与原因、日汇总、无法验证/费用未知显著显示。停止和下载 JSON 报告；轮询仅在弹窗打开时GET。
- [x] 加载第14普通脚本，不改前13顺序；live模式隐藏。不自动打开浏览器。
- [x] Node VM先验证日期/合约变化与旧请求隔离、文本转义、模型过滤、成本必填/冻结、GET不会启动、点击启动的精确载荷、未知费用显示。

## Task 4：复核、文档、重启

- [x] 定向代码复核以上状态机与接线；修复重要问题并回归。
- [x] 更新 AGENTS.md、架构导航（14脚本/新表/调度/入口）、README、新增使用与验收说明。
- [x] 运行新增 unittest/JS，以及原 tests.test_ai_analysis、tests.test_ai_reliability 和相关前端检查，跳过所有 CTP 测试。
- [x] 执行 stop.bat / start.bat，核对服务进程比代码新、8000监听、auth/me=401 JSON。核对新表和未授权API=401。不启动真实验证计划；浏览器验收由用户进行。

## 完成记录（2026-10-08 11:12 北京时间）

- Task 1 内核完成，33 项纯函数测试通过。
- Task 2 服务及 API 完成，35 项隔离测试通过；提交异常恢复、费用未知、重启/漏窗、单仓及实际交易日期间均有覆盖。
- Task 3 页面完成，新 Node VM 回归与相关前端回归通过；严格筛选可用模型、账户/旧请求隔离、中文原因、监测与部分收益提示。
- Task 4 定向复核和文档完成；统一 129 项 Python 测试、7 个前端行为检查及差异检查通过。服务已重启，8000/401/数据库新表与生产零计划核验通过。
- 工作区保留未提交改动；不创建生产计划、不发真实模型请求、不进行浏览器验收。具体核验边界见使用与验收文档。
