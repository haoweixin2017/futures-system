# 项目协作约定

## AI 快速识别项目

- 项目架构与代码导航见 [docs/项目架构与AI导航.md](docs/项目架构与AI导航.md)（2026-10-21 更新）。进入项目先读本文约定，再读架构文档的项目概况、数据流及按任务定位表，只展开当前任务相关源码和测试。
- 快速定位：`run.py` → `backend/main.py`；行情/模拟交易/页面 API 在 `backend/api/routes.py`，AI/公开资料 API 在 `backend/api/ai_routes.py`，独立模拟验证 API 在 `backend/api/paper_verification_routes.py`；主工作台为 `frontend/index.html` + 按顺序加载的 `frontend/js/01-core.js` 至 `15-market-simulator.js`。
- 主系统为单进程 FastAPI + Redis + MySQL；`data_collection/` 是独立 CLI/SQLite 采集模块，不由主服务启动。架构、入口、表结构或运行方式变更后同步更新架构文档。
- 架构文档属于源码快照，实际运行状态和功能验收需另行核实；开发范围以下面的 CTP 冻结约定为准。

## 文件改动后的文档维护

- 开源发布（2026-10-22）：原创代码采用根目录 `LICENSE`（MIT），第三方声明见 `THIRD_PARTY_NOTICES.md`，贡献与安全反馈入口为 `CONTRIBUTING.md`、`SECURITY.md`。公开版使用独立、无私有历史的源码快照，排除 `接口/`、`docs/evidence/`、浏览器验证原始输出、`real_ai_verify.py` 和 `real_ai_verify_result.txt` 等本机材料；本地与私有备份保留原文件，不改动冻结 CTP。公开提交使用 GitHub noreply 身份，发布前复查文件清单与凭据。历史文档的本机证据引用不代表公开分发或新环境验收。

- 每次新增、修改、删除或重命名系统文件后，必须检查是否影响 `AGENTS.md` 中的目录、文件路径、模块入口、开发范围、运行方式或测试约定；涉及的内容必须在同一次改动中同步更新，无需用户再次提醒。
- 如同时影响项目架构、模块职责、数据流、表结构、前端脚本加载顺序或代码定位，必须同步更新 `docs/项目架构与AI导航.md`；README 中相关说明或链接受影响时也一并维护。
- 完成前核对文档与实际文件一致，检查引用路径和命令，避免保留已失效的入口或约定。

## 功能规格与 Spec Kit

- Spec Kit 工作流基础文件位于 `.specify/`，项目技能位于 `.agents/skills/`；功能规格在 `specs/`，当前功能由 `.specify/feature.json` 定位。初始化生成的 `.specify/memory/constitution.md` 仍为未填写模板，不视为已批准宪章。
- 行情模拟器 V1 需求见 [specs/001-market-simulator/spec.md](specs/001-market-simulator/spec.md)，技术方案见 [specs/001-market-simulator/plan.md](specs/001-market-simulator/plan.md)，实施任务见 [specs/001-market-simulator/tasks.md](specs/001-market-simulator/tasks.md)，同目录包含研究、数据模型、契约、验证指南及需求清单。现已增加独立行情模拟抽屉和 `/api/v1/market-simulator` 接口；入口为 `backend/api/market_simulator_routes.py`、`backend/market_simulator/` 及第15前端脚本。正式规则共用 `backend/signal_engine/rules.py`，模拟不发布信号或写业务数据；不把它与行情源 `simulator` 或 AI 自动模拟盘混同。真实来源/人员验收以[acceptance.md](specs/001-market-simulator/acceptance.md)为准（Phase 7历史完成48/54项，Phase 8来源修复后共59项；最新真实来源及人员验收见记录）。
- 行情模拟器文档五项一致性修订及复核记录见 [specs/001-market-simulator/checklists/analysis-remediation.md](specs/001-market-simulator/checklists/analysis-remediation.md)；实施时按最新的观测证据表、持仓未知矩阵、ATR范围、错误上下文及快照统一404规则验收，文档闭环不代表真实行情已可用。
- 行情模拟器 Phase 7 收敛：快捷场景经服务端精确校验成功后才提交草稿，错误保留请求关联与允许字段；当前/历史质量摘要保留来源、时间标签和原始coverage，已知总持仓非负，单方未知不伪造一致性验证。缺累计量起点仍阻断，真实来源及人员验收见 acceptance.md；专项回归仍使用模拟器 Python/Node 三文件。

## Codex 只读行情接入

- 需要按需读取 K 线、报价、指标、规则信号或搜索合约时，优先使用 `futures_market` MCP 工具；使用说明见 [docs/Codex只读行情接入.md](docs/Codex只读行情接入.md)。
- MCP 入口 `codex_mcp/server.py`，HTTP/质量逻辑 `codex_mcp/market_client.py`，独立环境 `.venv-mcp/`；它不加载交易引擎，也不启动期货服务。默认读取已闭合 K 线，分析须注明具体合约、周期、交易日、北京时间、数据截止和质量警告。
- 当前聊天未刷新 MCP 工具时，可用 `.venv\Scripts\python.exe` 调用 `codex_mcp.market_client.MarketClient`，同样只读本机 HTTP；不要改用交易凭据或主动回补来取数。
- 专项验证：`.venv\Scripts\python.exe -m unittest tests.test_codex_mcp`；协议验证用 `.venv-mcp\Scripts\python.exe -m unittest tests.test_codex_mcp_protocol`。这些测试不需要主系统数据库；真实取数验证见 `scripts/verify_codex_mcp.py`。

## 开发范围(2026-10-01)

- **CTP 相关代码已停止开发**(`backend/trade_engine/ctp_trader.py`、`backend/collector/ctp_collector.py`、`ctp_flow/` 及所有 CTP 实盘下单/对账/重连逻辑),保持现状即可。代码审查、检查、重构一律**跳过这些模块**:不主动提改进建议、不为它们补测试、不修改它们,除非用户明确要求。

## 改动后必须重启服务(无需询问)

- Windows 启动依赖检查统一在 `run.py` 调用 `scripts/startup_dependencies.py`：先检查配置的 MySQL/Redis TCP 端口，本机不可连接时启动同名 Windows 服务并等待端口，权限不足弹出 UAC，仅依赖服务启动命令提权；远程配置不操作本机服务。拒绝授权、服务缺失或超时终止启动，不跳过依赖。`start.bat` 与桌面控制台均通过该入口；桌面控制台最长等待 420 秒，子进程退出立即报错。隔离回归：`.venv\Scripts\python.exe -m unittest tests.test_startup_dependencies`；端口就绪不代替数据库认证、协议或后端健康验收。

- 后端进程通过 `run.py` 以 uvicorn 启动(`reload=False`),**没有热重载**。修改 `backend/`、`migrations/` 或任何被后端进程加载的代码后,必须直接重启服务,不要征求用户确认。
- 重启方式:项目根目录执行 `stop.bat`,再执行 `start.bat`(内部用 .venv 启动,监听 8000;venv 启动的进程命令行显示为 `D:\Python311\python.exe` 属正常现象)。
- 判断是否需要重启:比较服务进程的启动时间与 `backend/` 最新文件的修改时间(如 `powershell "Get-CimInstance Win32_Process -Filter \"ProcessId=<PID>\" | Select CreationDate"`);进程早于文件修改时间即需重启。
- 重启后验证:`netstat -ano | findstr ":8000"` 有 LISTENING,且 `curl http://127.0.0.1:8000/api/v1/auth/me` 返回 401 JSON(未登录响应即健康)。
- 只改 `frontend/*.html` 时无需重启,提醒用户 Ctrl+F5 强刷页面即可。

## 其他

- AI 盘时段质量校验（2026-10-22）：`snapshot.py:build_snapshot()` 对最终选入 `market.bars_1m` 的同一证据集合调用 `market_quality()`；ACTIVE 只校验所选小节，UPCOMING 校验实际盘前上下文，TODAY/NEXT_OPEN 保留全天检查。范围外未知量不得拦截所选小节，范围内未知量仍阻断；`quality.unverified_volume_bars` 保留合约、周期、时间、质量及缺口，`09-ai-analysis.js` 显示定位信息。历史冻结快照不改写。回归：`.venv\Scripts\python.exe -m unittest tests.test_ai_reliability tests.test_ai_analysis tests.test_volume_quality`、`node tests/check_ai_reliability.cjs`。
- 新浪实时快照 `sina_collector.py:fetch_sina_quotes()` 使用独立 `ProxyHandler({})` 直连 `hq.sinajs.cn`，避免系统代理出口返回403；保留HTTPS证书校验、User-Agent/Referer、分批及超时，不安装全局opener，不改变AI/资料请求代理。回归：`.venv\Scripts\python.exe -m unittest tests.test_sina_parser`。403诊断需对比同机代理与直连响应，服务健康不代表盘口新鲜。
- 未来趋势行情冻结：`snapshot.py:_load_future_bars()` 仅在新建 FUTURE_TREND 分析时直接读取新浪日线、60m、15m历史序列，保留 DAILY_CLOSE/END 标签、请求时间/响应哈希及 UNKNOWN 覆盖；不与本机 START 合成柱混用，不写行情库/Redis。只冻结截点前已闭合且交易日合法的柱；某周期取数异常以 `HISTORY_FETCH_FAILED_*` 阻断，不降级使用部分覆盖缓存，真实连续缺口仍阻断。历史结果和报告 GET 不重新取数或改写。回归：`.venv\Scripts\python.exe -m unittest tests.test_future_bar_provenance tests.test_future_market_quality tests.test_future_trend_integration`。
- 自动模拟盘新浪盘口采用 `SINA_SOURCE_30S_RECEIPT_10S`：`bar_engine.py:on_tick()` 仅在接纳新源时间的新浪 Tick 时写入本机 `received_at`，重复报价不续期。`paper_verification_engine.py` 接受源时间距扫描最多30秒且接收最多10秒的盘口；无接收时间的旧缓存仍限源时间10秒。模型完成前/截止后、跨交易日及无效买卖盘口保持拒绝，成交和估值使用同一校验；当前活动 AUTO_PAPER 自动采用，旧终态不改写。未成交记录保存 `quote_diagnostics`（拒绝原因、时间、延迟及被拒触线证据），页面区分未触发、有效盘口不足和触线盘口被拒；旧记录明确原因未记录。回归沿用模拟验证三模块、`tests.test_bar_provenance` 和 `node tests/check_ai_verification.cjs`。
- 模拟验证触发与边界：接收新模型结果时 LONG 触发价须大于冻结 `session_forecast.reference_price`，SHORT 须小于；缺少有效参考价或已越过/等于参考价的触发条件跳过，不伪造成交。`paper_verification_engine.py:advance_trade()` 统一处理未成交截止边界（自动模拟盘5分钟，旧规则10分钟），扫描稍迟时先处理截止内仍新鲜且尚未处理的盘口，截止后报价不能补造进场；`force_close` 优先取消未成交信号为 `STOPPED`，不因停止处理延迟计超时亏损。报告 GET 批量读取所属账户、同合约/模型的持久化结果，以 `ai_result` 白名单返回结论、证据完整度、条件、风险、证据和复核状态，不含配置、请求或原始响应；逐次详情用文本 DOM 展示，草稿明确不用于成交。创建说明、冻结概要和报告统一包含模型完成起10分钟持仓结算及实际净收益胜负规则。历史已结束账本不改写，沿用模拟验证 Python 三模块与 `node tests/check_ai_verification.cjs` 回归。
- 自动模拟盘规则 1.5：当前 Gemini 计划可用 `paper_verification.py:upgrade_auto_paper_rules()` 显式升级，仅更新配置和活动信号/持仓，终态账本不改写、不调用模型；新建默认采用该规则。`entry_deadline_at=模型完成+5分钟`、`position_deadline_at=模型完成+10分钟` 分离，进场后不被5分钟截止误平仓；无有效结果等5分钟，最多一仓、退出后再分析，开盘15分钟后开始和末5分钟暂停不变。AUTO_PAPER未成交重试/进场前失效只扣实际模型费，不计交易胜负；历史旧口径保留。`get_report()` 只读返回 `live` 及逐笔 `live_pnl`，`paper_verification_engine.py:mark_position()` 按新鲜同交易日可平仓盘口、不利滑点和双边手续费估值；合计浮动估值加已实现账本时不重复扣模型费，缺新鲜报价显示待报价，不写成交或调用模型。前端每2秒刷新，优先选Gemini，浮动与已实现分开展示。沿用模拟验证三模块和Node回归。
- 模拟验证实时安排：`paper_verification.py:get_report()` 返回只读 `activity`（当前动作、下一步、关联北京时间、待结费用数量），由实际 trial 状态及交易日历计算，不调用模型、不创建任务。区分待首次/后续分析、待模型返回、待进场、持仓观察、超时待报价、末 5 分钟暂停、停止结清与结束；过期但未被扫描处理的记录明确显示待后台处理，不假装仍正常等待。`14-ai-verification.js` 在顶部活动卡显示，随报告每 2 秒刷新，切换计划清空旧安排。回归沿用模拟验证三模块和 `node tests/check_ai_verification.cjs`。
- 公开资料按同品种共享：`research_cache.py:_collection_group()` 在冻结资料和查询状态时优先精确合约，再匹配采集设置中的同品种目标；因此配置一个 FU 合约采集后，其他有效 FU 月份合约可直接读同一 `product:FU` 缓存，无需另加目标。`scope=contract` 仍严格检查 `applicable_contracts`，保留账户、时间截点、有效期和到期限制，不引用其他品种、不恢复关联合约功能。读取不新增采集任务或模型调用；页面共享缓存的手动更新使用已配置的同品种采集目标。回归：`.venv\Scripts\python.exe -m unittest tests.test_scheduled_research tests.test_scheduled_research_integration tests.test_hybrid_research tests.test_research_credibility`、`node tests/check_scheduled_research.cjs`。
- 模拟验证各小节结束前最后 5 分钟（含恰好剩余 5 分钟）停止发起新分析；`paper_verification.py` 同时检查计划时间和实际扫描/提交时间，适用于当前运行计划，无需重建。末 5 分钟不生成新的应监测窗口或漏分析告警；截止前计划但扫描延迟到截止后的记录以 `SESSION_CUTOFF` 跳过、费用为 0。既有分析继续追踪结果/费用，持仓继续按止盈/止损、10 分钟结算及收盘规则处理。
- `TRADE_OUTCOME` 持仓从模型实际结果完成时起满 10 分钟仍未触及止盈/止损，按截止后首笔新鲜盘口结算（多仓卖价、空仓买价，沿用滑点/手续费），退出原因 `RESULT_TIMEOUT`，生成真实退出记录并继续下一次分析。新活动记录使用 `outcome_policy=ACTUAL_NET`，胜负按交易净收益扣本次模型费用判断；零收益不计盈利或亏损，仍参与已评估分母并打断连亏，费用未知暂不判胜负。缺新鲜报价等待，不用旧价/回补价格伪造成交；无有效分析结果不参与此结算。历史已结束账本不改写。
- 模拟验证胜负统计包含已核实平仓交易（排除收盘提前结算）、`SIGNAL_TIMEOUT` 未成交超时和 `PRE_ENTRY_INVALIDATED` 进场前失效亏损；未成交记录仅扣实际已知模型费用，不伪造成交或交易亏损额。`RESULT_TIMEOUT` 新活动记录按实际净收益判断；历史终态未带 `ACTUAL_NET` 的超时记录保留固定亏损口径。`SESSION_END_EARLY` 指结果完成起不足 10 分钟即收盘，真实结算金额保留但不计胜负；读取历史 `SESSION_END` 也按冻结结果时间识别此情况，不改写账本。无有效分析结果及模型请求超时不计胜负。胜率分母为参与胜负评估的上述记录，连亏按退出/信号截止/失效观测时间排序，交易平均收益仍按全部已核实闭合交易。逐次分析与逐笔成交展示按分析时间倒序，展示排序不改写原始报告或账本。定向回归沿用模拟验证 Python 三模块及 `node tests/check_ai_verification.cjs`。
- 10 分钟边界：恰好截止的有效盘口先判断止盈/止损，再超时结算；截止后才触及的仍记 `RESULT_TIMEOUT`。缺截止后新鲜报价时保持 `OPEN`，记录 `settlement_pending` 与截止时间，报告显示“已超时，待报价结算”，不允许新分析挤占持仓；有效结算后清除此标记。
- 首页右侧 AI 结果由 `09-ai-analysis.js:startAIHomeAutoRefresh()` 每 2 秒只读刷新，`13-boot.js` 启动接线；隐藏页面暂停，结果不变不重绘，保留滚动、图表选择及视口，旧合约/日期/账户请求不能覆盖当前状态。回归：`node tests/check_ai_home_refresh.cjs`；前端脚本改动无需重启服务，Ctrl+F5 加载。
- 1分钟历史仅用新浪（2026-10-15）：`backend/storage/history_backfill.py:repair_minute_gaps()` 的启动、订阅及周期补缺均停用东方财富备用请求，返回 `fallback_skipped=DISABLED`；新浪失败向上抛错，无数据保留真实缺口。既有东方财富历史与字段来源保留，新浪修正价位时仍保留既有真实成交量/成交额，AI继续提示 `FALLBACK_1M_SOURCE_EASTMONEY`。`eastmoney_history.py` 及备用内核仅保留为历史兼容代码，不接入运行补缺。回归：`.venv\Scripts\python.exe -m unittest tests.test_eastmoney_history tests.test_bar_provenance tests.test_ai_reliability tests.test_trading_time tests.test_startup_backfill`；原补缺用发现模式 `test_minute_gap_repair.py`。
- 分钟连续性检查使用 `backend/common/trading_time.py:is_trading_time_for()` 的左闭右开交易时段；11:30 是上午收盘边界，不是开始标签 1 分钟柱的预期时间。午休边界及真实缺口回归：`.venv\Scripts\python.exe -m unittest tests.test_ai_reliability tests.test_trading_time`。
- AI 盘时段模拟验证：`backend/ai_analysis/paper_verification.py` 负责账户隔离、持久化与每 2 秒扫描，`paper_verification_engine.py` 为纯成交/统计内核，`paper_verification_models.py` 定义两张独立研究账本表，`frontend/js/14-ai-verification.js` 为页面入口。仅模拟用户明确点击“创建并启动”或“重新启用”后启用有费用的模型分析；固定每小节开盘 15 分钟后开始；明确方向返回后等待行情或交易结果，自动模拟盘未成交信号从模型完成起满 5 分钟未触发作废重试；持仓退出后再分析，无明确方向/无有效结果等 5 分钟，实际发起满 10 分钟未返回也作废重试；收盘停止，1 手、最多一仓、20 个实际交易日。trial 按 `(plan_id, analysis_due_at)` 唯一；新结果使旧的未成交信号失效，已持仓按原规则退出；新计划 `analysis_schedule_mode=RESULT_DRIVEN`、`analysis_result_trigger=TRADE_OUTCOME`、规则 1.5、`execution_mode=AUTO_PAPER`、`signal_timeout_minutes=5`、`position_timeout_minutes=10`、`outcome_policy=ACTUAL_NET`；超时 trial 保留费用追踪，迟到结果仅计费；`schedule_history` 保留规则切换前的监测范围；显式切换当前计划用 `upgrade_trade_outcome_schedule()`，旧待返回/未成交信号作废、迟到仅结费，持仓保留原退出规则。旧计划保留各自冻结的频率。只采用模型完成后新鲜新浪盘口，不自动订阅/回补，不调用既有模拟账户下单或 CTP。使用说明见 `docs/2026-10-08-AI模拟验证使用与验收.md`。定向验证：`.venv\Scripts\python.exe -m unittest tests.test_paper_verification_engine tests.test_paper_verification tests.test_paper_verification_api`、`node tests/check_ai_verification.cjs`；不得用生产计划或真实模型调用代替隔离测试。
- 模拟验证重新启用：`POST /api/v1/ai/verification/plans/{plan_id}/resume` 仅恢复所属用户已结清的 `STOPPED` 计划；保持原20交易日截止、模型配置、提示词、成本和历史账本，拒绝其他活跃计划、未结费用及模型配置漂移。`resume_history` 记录停止/恢复时间，`schedule_history` 保留恢复前运行范围，停止区间不计漏分析、不补发，旧重试不在恢复后复活。页面按钮按状态启用；恢复不延长周期，已完成或过期须新建。沿用模拟验证 Python/Node 回归。
- 模拟验证计划提示词：默认要求使用分析冻结时刻、所选连续交易小节和 `session_forecast` 限定证据/预测窗口；历史支撑压力可作参考，冻结前突破不算触发或命中。更新默认文案不覆盖已保存的计划提示词。创建时将 `analysis_requirements` 保存到计划配置，`dispatch_analysis()` 每次读取当前计划提示词；旧计划缺失时沿用默认要求。`PATCH /api/v1/ai/verification/plans/{plan_id}/requirements` 仅允许所属且已结清的 STOPPED 计划修改后续分析要求，其他状态拒绝，保留修改历史，已提交快照不变。页面使用深色提示词卡片与状态标记，运行中、停止结算中及已完成只读可复制；已停止时轮询不覆盖编辑草稿，未保存修改须先保存才能重新启用。新建计划可填写提示词。手续费必须填写，滑点与模型输入/输出价格默认 0，可自定义；旧计划成本不自动改写。回归包含上述模拟验证 Python/Node 测试。
- 模拟验证报告展示：`14-ai-verification.js:renderAIVerificationTrials()` 使用 7 列概要表，完整时间、价位、费用及追踪编号在整行详情中展开；面板打开时每 2 秒独立刷新当前报告，列表慢请求不阻断结果、同一请求不重叠；轮询保留展开状态及滚动位置，显示最近更新时间，读取禁用缓存并有 10 秒超时。样式限定在 `frontend/index.html` 的 `ai-verification-*` 类中，面板统一纵向滚动，窄屏允许表格横向滑动。前端回归：`node tests/check_ai_verification.cjs`；仅展示改动无需重启服务。
- 盘中10分钟窗口：`backend/ai_analysis/session_window.py:chart_for_completion()` 用每个模型数据库实际完成时间（UTC，秒精度）生成北京起点与 `min(完成+10分钟, 小节收盘)` 终点，保留 `evidence_cutoff_time` 与 `session_end_time`。普通盘中结果/历史展示和模拟接收统一窗口，分钟核验只用完整窗口内柱，同分钟触发与退出先后不明须待核验，缺尾柱不伪装未命中。普通AI到期使用分钟采样评估方向，不代表模拟盘口成交。当前活动模拟记录自动标记 `ACTUAL_NET`，终态账本不改写；旧计划频率和自定义提示词保留。回归加入 `tests.test_ai_session_window`。
- 盘时段画线（含未开盘小节的盘前展望）：快照/价位规范在 `backend/ai_analysis/snapshot.py`、`orchestrator.py`，图表在 `frontend/js/06-ai-session.js`。盘前线从所选小节开盘画到收盘，基准是冻结的盘前参考价，不能当作开盘价；历史按冻结目标交易日查询，价位线不触发交易。回归：`node tests/check_ai_session_lines.cjs` 及 `tests.test_ai_analysis`。
- 盘时段默认分析要求在 `frontend/js/08-ai-research.js`，加载和旧默认文案迁移在 `09-ai-analysis.js`：默认文案随所选小节切换，UPCOMING 只显示盘前要求，ACTIVE 只显示盘中要求；盘前可使用冻结的开盘前历史证据，盘中仅使用所选小节点击前证据，不要混用范围。只迁移与旧默认文案一致的浏览器保存内容，保留自定义要求。回归：`node tests/check_ai_intraday_date.cjs`。
- 测试用 venv 的 unittest 运行:`.venv\Scripts\python.exe -m unittest tests.test_ai_analysis`(pytest 未安装)。
- `tests/test_trading_safety.py` 等裸 `import test_bootstrap` 的模块需用发现模式运行:`python -m unittest discover -s tests -p "test_trading_safety.py"`。


<!-- 2026-10-14：成交费用与复制计划 -->
- 模拟验证交易手续费为每边每手人民币固定金额，仅实际触发成交后记录 `entry_commission`，平仓记录 `exit_commission`，总计双边；等待触发、无方向及未成交跳过不扣交易手续费，模型费独立计费。只读报告 `incurred_trading_cost` 包含持仓已发生开仓费，`cash_net_pnl` 在原已结算净收益上扣除该费用；`live.holding_net_pnl` 只扣开仓费，原 `live.trading_net_pnl` 为含预计平仓费的平仓估值，预计合计不重复扣费。缺报价仍展示已发生费用，历史终态不改写。
- `POST /api/v1/ai/verification/plans/{plan_id}/copy-restart` / `paper_verification.copy_restart_plan()` 仅复制所属且已结清的 STOPPED/COMPLETED 计划，账户有 RUNNING/STOPPING 时拒绝。复制合约、模型选择、已保存提示词与成本参数，按当前模型配置及自动模拟盘规则创建新20实际交易日空账本，保存 `copied_from_plan_id`，原计划和历史不变。页面“复制并重新开始计划”拒绝未保存提示词；“重新启用”仍恢复原周期。回归沿用模拟验证 Python 三模块及 `node tests/check_ai_verification.cjs`，禁止生产新计划或真实模型调用验收。

- 行情模拟器回归：`.venv\Scripts\python.exe -m unittest tests.test_signal_rules tests.test_signal_engine_compatibility tests.test_market_simulator tests.test_market_simulator_api`、`node tests/check_market_simulator.cjs`。原始检查点缺 `volume_anchor` 时保持 CURRENT_BAR_UNVERIFIED；不得反推或补造起点证据来放行。
- 模拟器 `BAR_PROVENANCE_UNAVAILABLE` 错误包含合约、周期、北京时间标签、MySQL/Redis位置和缺失项。排查时按记录定位，不能猜填旧柱来源、删除旧柱绕过校验或把错误提示修复当作真实基准验收；FU2701 的真实阻断记录见 `specs/001-market-simulator/acceptance.md` 的2026-10-21排查补充。
- 模拟器Phase 8来源修复（2026-10-21用户授权）：`backend/common/bar_evidence.py`提供纯边界证据及交易分钟网格；`bar_engine.py`仅为sina新接纳报价保存`source_observation`和`volume_anchor`，保留真实源时间/累计量/内容哈希，不反推旧检查点。锚点是周期前一分钟内最后一笔源观测，非交易所精确边界量，coverage仍PARTIAL。reader创建基准时只读新浪15m/60m新历史响应，保留END标签/请求时间/响应哈希，按日/夜各自累计交易分钟规范化跨休市区间；不混合本机高阶缓存、不回写旧数据、不回补。1m仅读取当前15m窗口；缺首分钟、缺锚点、重置、冲突及缺柱继续阻断。创建可等待45秒，后续模拟不重新取数。新增回归：`.venv\Scripts\python.exe -m unittest tests.test_simulator_source_evidence`，完整验收见功能目录acceptance.md。

- 成交量归属（2026-10-21）：`backend/common/volume_quality.py`统一质量判断。sina采集跨缺失交易分钟时，累计差分另存`volume_gap`，不压入恢复首分钟；`UNALLOCATED_GAP`/`UNVERIFIED_ALLOCATION`在后续Tick、闭合、高阶合成及历史价格修复中保留。API未知量为null，原值`volume_raw`仅审计；均量、量比实际选样、VWAP、正式规则及AI不得采用未知量。VOL不削峰、不把未知当0，光标显示未知；分钟统计含未知时不报完整合计。`scripts/quarantine_volume_allocation.py`默认只读，仅对有证据的指定分钟及本机高阶柱加质量状态，先备份、事务写库、WATCH只同步质量并清派生缓存，不自动猜测异常。实际处置见`docs/audits/2026-10-21-FU2701-volume-gap.md`。回归：`.venv\Scripts\python.exe -m unittest tests.test_volume_quality`、`node tests/check_volume_quality.cjs`；裸bootstrap模块采用发现模式或设置测试目录PYTHONPATH。
