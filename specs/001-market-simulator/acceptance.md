# 行情模拟器 V1 实施与验收记录

## 实施基线

Git HEAD：`f1784128d81d311d2ea08c5b8882f03f9b26dbe8`。已有改动保留，不读取/修改冻结CTP模块，不运行生产模型或计划。

```text
 M AGENTS.md
 M README.md
 M backend/ai_analysis/orchestrator.py
 M backend/ai_analysis/paper_verification.py
 M backend/ai_analysis/paper_verification_engine.py
 M backend/ai_analysis/research_cache.py
 M backend/ai_analysis/settlement.py
 M backend/ai_analysis/snapshot.py
 M backend/api/ai_routes.py
 M backend/api/paper_verification_routes.py
 M backend/bar_engine/bar_engine.py
 M backend/collector/sina_collector.py
 M backend/storage/history_backfill.py
 M desktop_console.py
 M "docs/2026-10-04-\350\265\204\346\226\231\351\207\207\351\233\206\346\224\271\350\277\233-\345\274\200\345\217\221\350\256\276\350\256\241.md"
 M "docs/2026-10-08-AI\346\250\241\346\213\237\351\252\214\350\257\201\344\275\277\347\224\250\344\270\216\351\252\214\346\224\266.md"
 M "docs/\351\241\271\347\233\256\346\236\266\346\236\204\344\270\216AI\345\257\274\350\210\252.md"
 M frontend/index.html
 M frontend/js/06-ai-session.js
 M frontend/js/07-ai-models.js
 M frontend/js/08-ai-research.js
 M frontend/js/09-ai-analysis.js
 M frontend/js/14-ai-verification.js
 M run.py
 M start.bat
 M tests/check_ai_intraday_date.cjs
 M tests/check_ai_session_lines.cjs
 M tests/check_ai_verification.cjs
 M tests/check_scheduled_research.cjs
 M tests/test_bar_provenance.py
 M tests/test_future_bar_provenance.py
 M tests/test_future_market_quality.py
 M tests/test_future_trend_integration.py
 M tests/test_minute_gap_repair.py
 M tests/test_paper_verification.py
 M tests/test_paper_verification_api.py
 M tests/test_paper_verification_engine.py
 M tests/test_scheduled_research.py
 M tests/test_sina_parser.py
?? .agents/
?? .specify/
?? backend/ai_analysis/session_window.py
?? backend/storage/eastmoney_history.py
?? scripts/startup_dependencies.py
?? specs/
?? tests/test_ai_session_window.py
?? tests/test_eastmoney_history.py
?? tests/test_startup_dependencies.py
```

## 状态

代码、隔离测试、浏览器、真实行情成功/阻断及5名用户验收分别记录，尚未执行的不标为完成。

## 基础阶段

T001～T010完成。新增纯规则用例先因模块缺失失败，180组正式输出在提取前生成固定对照，提取后完全相同。规则矩阵45组；模型/store先失败后通过。命令：`python -m unittest tests.test_signal_rules tests.test_signal_engine_compatibility tests.test_signal_engine tests.test_market_simulator`，9个测试方法通过。未运行有费模型或真实交易。

## 本轮代码交付

- 已落地：纯规则核心及正式LEGACY适配；严格输入及wire对象；有界会话内存store；新浪只读历史/元数据/参考run装载、六项观测证据、时间与尾部缺口门禁；情景、ATR范围、条件分差、毛收益风险比、六类快捷；三端点及第15脚本抽屉；实际11-websocket合约切换、03-contracts直接赋值、02-chart退出及06-ai-session明确参考选择接线。
- 无新增数据表、采集线程或有费模型调用入口；不修改冻结CTP模块。新增`test_signal_engine_compatibility.py`和改造前180组golden固定输出，原test_signal_engine.py保留。
- 输出对象使用models.py的TypedDict白名单，入参使用Pydantic严格校验；store仅保存深拷贝，不向浏览器回传全部历史或内部会话摘要。

## 自动验证（已执行）

2026-10-19，PowerShell项目根目录（PYTHONPATH仅为兼容旧test_indicators的裸test_bootstrap导入）：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'tests')
.\.venv\Scripts\python.exe -m unittest tests.test_indicators tests.test_realtime_analyzer tests.test_realtime_analysis_api tests.test_bar_provenance tests.test_trading_time tests.test_signal_rules tests.test_signal_engine tests.test_signal_engine_compatibility tests.test_market_simulator tests.test_market_simulator_api
node tests/check_market_simulator.cjs
node tests/check_ai_session_lines.cjs
node --check frontend/js/15-market-simulator.js
```

结果：84个Python测试方法通过；Node模拟器行为检查、既有AI画线检查、JS语法检查通过。另验证规则45组合、原正式输出180组、20组不同价量defaults一致性、12组多空毛比值及阈值/失效边界；覆盖证据缺失、缺尾柱、跨休市END、持仓未知矩阵、0值/非法值、store预算/会话/并发/限流回收、HTTP权限/错误context和只读计算不访问行情适配器。

## 独立审查

按requesting-code-review技能进行只读审查，发现4项P2：重置防抖未取消、缩量误改价格、历史尾部缺闭合柱漏检、END柱跨休市。已先用复现测试确认，再修复；审查代理复核4项通过，未发现这些修复范围内新增的重要问题。

## 服务与浏览器（部分已验收）

- 按项目stop.bat/start.bat重启；8000监听，未登录auth/me及新模拟接口均401。批处理在无交互终端中报告timeout重定向提示，但服务实际启动成功，健康检查独立确认。
- 通过已有Edge登录会话刷新`/paper`，按钮可见且抽屉打开，FU2701在2026-10-19下午收盘后明确显示“当前合约未开盘，不能建立新基准”，未伪装实时成功。
- 临时`http://localhost:18009/`隔离合成验收页使用真实三端点/服务/第15脚本，但仅注入合成只读数据和测试身份，不读取生产行情、不修改生产Cookie（不设置Cookie），不调用模型/交易。显著显示“隔离合成样例，不是真实行情”。使用后关闭该临时服务。
- 浏览器实际输入：冻结1280、假设1295、量比1.8、增仓5000；显示多头35→100、模拟成交量1800手、假设总持仓6000、固定目标1320/止损1270的毛收益风险比1.00，以及“不宜按原计划追价”。这是合成页面流程证据，不是真实盘口成功。

## 真实数据限制与待完成任务

只读盘点本机`bar-engine:forming:sina`现有16个检查点，全部缺少`volume_anchor`。本实现不反推累计起点、不写检查点、不扩展采集范围。因此即使开盘，也不能据目前证据宣称真实情景可用；开盘后还可能因历史来源/时间/真实缺口而阻断。这是实测的数据限制，不是已经通过的功能验收。

| 任务 | 状态 | 缺少的证据 |
| --- | --- | --- |
| T017 | 未完成 | 合成成功/阻断样例已完成，但没有带原始累计量起点的真实成功样例 |
| T026 | 未完成 | 服务加载及真实休市阻断已验收，真实盘中成功未验收 |
| T042 | 未完成 | 隔离浏览器流程已验证，5名用户与正式20次计时未开展 |
| T044 | 未完成 | 文档/命令已核对，但上述真实验收未闭环 |

其余41项代码、自动验证、审查和文档任务完成。FR-001～022均有实现/测试映射，SC-003～006的隔离验证已执行，SC-001/002人员与计时及SC-007真实持续行情端到端尚待验收；不能将任务关联覆盖率100%称为真实运行通过率100%。

后续若要让现有真实行情通过，需要独立确认可核验的Sina周期累计量起点证据来源；本轮冻结边界不允许为了放行伪造anchor、改coverage或恢复外部回补。没有现成合法证据时需另行设计采集元数据扩展，不在本轮擅自接入。

## 最终复核

隔离浏览器补验重置：1295/1.8/5000情景提交后点击重置，等待状态变为“计算已更新”，两列均恢复1280/1.2/1，多空分数35/35、分差0；关闭抽屉隐藏并释放临时基准。临时18009服务已停止（无LISTENING），生产8000的auth/me和新模拟端点未登录均401。最后格式检查通过，未变更CTP源码；已恢复原有未改行的换行格式，避免无关整文件diff，Python AST一致。


## Phase 7 收敛实施（2026-10-19）

本节为当前状态，前述41/45与84个测试是第一轮实施的历史记录。本轮T047～T053完成，累计48/54；T017、T026、T042、T044、T046、T054共6项仍未完成。不修改需求质量清单的复选框。

### 来源核查与边界（T046，未完成）

本轮直接只读HGETALL `bar-engine:forming:sina`：16条、16条JSON可解析、0条含volume_anchor。限定范围搜索bar_engine及Sina collector未找到该证据字段提供路径。未将累计量减当前聚合量反推成anchor，未修改采集器、coverage或CTP。实际来源无法满足VOLUME_ANCHOR，故真实盘中成功及完整流程继续阻塞；合成样例保持synthetic=true。需要另行设计并授权来源元数据扩展才能解决该输入限制，本轮不将其擅自纳入采集建设。

### 已完成的修复及验证映射

| 收敛任务 | 变更与验证 |
| --- | --- |
| T047 | API隔离测试对Redis实际命令及SQL add/delete/commit/flush、正式保存、模拟账户获取/驱动、通知、两种模型供应商调用、分析提交、采集启动与回补设置触碰即失败；正常/失败/重置/更新/释放请求计数为0。测试初始化和模块导入在独立测试库中、请求计数之前发生。真实MarketReader也使用只读Redis/SQL存根执行元数据、历史读取和冻结，禁止方法单独计数0。增加跨登录会话、过期、重启、409/429/503、失败不占槽及质量返回检查。DOM用例执行实际render、fetch和按钮事件，覆盖防抖、错误、参考选择、草稿/滚动、切账号、旧响应与释放。 |
| T048 | 错误保留允许的context/fields及本地提交输入；CREATE失败显示15分钟/模拟属性/尚未建立，授权计算失败显示真实冻结上下文。401/403/404不采用响应中的基准context；旧成功结果单独标明输入，错误仍经上下文和序号校验。 |
| T049 | 滑块禁用约束在每次render计算，连续READY/COMPUTING/ERROR不重新启用无效ATR或未知量比滑块；手工输入独立。先加入DOM断言复现false!=true，修复后通过。 |
| T050 | 快捷提议经原simulate接口按Decimal/ROUND_HALF_UP完整校验，匹配当前序号的成功返回才修改价格/量比草稿，失败不改其他字段。均量1000、已成交1200、倍率1.1996采用1200手合法；1.1994采用1199手拒绝。价格/量比预览及持仓保持明确。 |
| T051 | 明确run/batch选择变化或清除时重新渲染提示更新基准；冻结计划不改，展示方向、来源/周期、形成时间、有效窗口；有效/不可用选择都保留冻结所选run ID以判断后续变化。 |
| T052 | quality.sequences以白名单给出current/15m/60m计数、provider、source、时间标签、原始coverage去重摘要；空/不足历史、均量非正和持仓缺失分别说明。界面显示PARTIAL/UNKNOWN及不可评估原因，不将缺值补0。 |
| T053 | 检查点坏JSON/结构/必要字段422 CURRENT_BAR_UNVERIFIED，历史坏时间422 INVALID_HISTORY_TIME、坏数值/结构422 INVALID_HISTORY、未知来源/标签仍保留既有明确422；无效元数据tick返回422 INVALID_PRICE_TICK。真实存储故障503，CREATE冻结时间null、失败槽数0；Python与HTTP用例验证区别。 |

### 最终自动验证

项目根目录执行以下命令；PYTHONPATH仅兼容既有裸test_bootstrap导入：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'tests')
.\.venv\Scripts\python.exe -m unittest tests.test_indicators tests.test_realtime_analyzer tests.test_realtime_analysis_api tests.test_bar_provenance tests.test_trading_time tests.test_signal_rules tests.test_signal_engine tests.test_signal_engine_compatibility tests.test_market_simulator tests.test_market_simulator_api
node tests/check_market_simulator.cjs
node tests/check_ai_session_lines.cjs
node --check frontend/js/15-market-simulator.js
```

Python 91个测试通过；模拟器控制器/DOM/DOM生命周期三组检查通过，既有AI画线与JS语法检查通过。FastAPI TestClient输出已有Starlette/httpx弃用提示，不影响本轮结果；未为此扩张依赖升级。正常信号180组固定输出对照继续通过。DOM存根仅证明脚本行为，不证明真实浏览器布局、用户操作耗时或真实行情可用性。

### 服务加载与待验收（T054，部分完成但不勾选）

按stop.bat/start.bat重启，原PID39520已停止，新监听PID41892，127.0.0.1:8000 LISTENING；auth/me返回401 JSON，snapshots与simulate未登录均401。新进程启动时间2026-10-19 15:35:35，晚于本轮后端代码修改。停止批处理在非交互终端出现timeout输入重定向提示，但停服与新进程监听/HTTP结果均单独确认。没有把健康401当成数据验收。

本轮未开展真实盘中成功、真实行情持续更新端到端、窄屏浏览器现场、正式20次浏览器计时及5名使用者验收。不能以DOM测试或历史合成浏览器记录替代。文档、接口说明与当前统计已同步，T054仍因上述验收保持未完成；用户需Ctrl+F5加载本轮前端。

### FR/SC当前证据核对

| 来源 | 当前证据及限制 |
| --- | --- |
| FR-001～005 | 认证/会话与reader冻结、生命周期/参考冻结和DOM旧响应检查；真实来源成功流程仍受T046限制。 |
| FR-006～009 | 场景纯计算、精确defaults、OI未知矩阵、整数手舍入、OHLC保留与非法输入回归。 |
| FR-010～012、FR-022 | 纯规则阈值矩阵、分项差值与正式入口180组固定兼容对照；未知不放行。 |
| FR-013～014 | ATR范围/tick边界、连续渲染禁用状态、合法手工输入及元数据/非法数值回归。 |
| FR-015 | 后端六类preset与Node草稿事务式提交/失败保留、舍入边界。 |
| FR-016～019 | run归属、固定毛比值阈值/失效、参考选择变化、有效窗口及无计划行为回归。 |
| FR-020～021 | 防抖、代次/序号/对象隔离、错误context、全流程请求副作用哨兵与显式只读reader；无新增交易/有费模型接线。 |
| SC-001 | 5人验收未开展，待完成。 |
| SC-002 | 正式20次浏览器计时未开展，待完成；DOM防抖/顺序验证不替代计时。 |
| SC-003 | ≥20组defaults与规则矩阵、分项差/总差等式隔离回归。 |
| SC-004 | API完整请求生命周期和真实reader存根禁止副作用计数0；实际市场本身更新不作为模拟写入证据。 |
| SC-005 | 多空、价位等号/越界、1.5阈值、过期与无方向纯计算/API回归。 |
| SC-006 | 来源六项证据/缺失、时间/数字/持仓/历史/休市门禁的隔离用例；真实成功不能推定。 |
| SC-007 | 成功/已授权错误/CREATE失败/认证错误及DOM切对象关联已验证；真实持续行情验收仍待完成。 |

未创建新数据表/采集线程，未修改冻结CTP；.specify/extensions.yml不存在，无注册实施钩子。没有提交或覆盖本轮范围外的用户既有改动。


### 独立审查与最终重启复核

requesting-code-review独立只读审查发现1项P2：当前报价/分钟持仓同时为-1时一致性检查曾放行，隔离HTTP可返回200。先补三种已知负持仓组合及真实reader→HTTP失败用例并确认失败，再于SOURCE_COHERENCE增加每个已知持仓均须非负；单方/双方未知及合法0仍允许，源报价持仓未知另行警告。修复后完整91项Python、模拟器Node三组、既有画线及语法再次通过，审查者独立重跑指定2项用例通过并确认P2已解决，未发现该修复范围新增问题。

因后端再次修改，已第二次按stop.bat/start.bat重启：最终PID44300，启动2026-10-19 15:41:34，晚于最新后端文件15:40:47。8000监听，auth/me为401且响应可解析JSON，snapshots/simulate未登录均401。前文PID41892是本轮第一次重启的历史记录。真实来源与人员验收状态不变，仍48/54完成。

### 2026-10-21 FU2701基准失败排查补充

北京时间约13:59只读检查：15m最近256根MySQL记录和Redis记录中，各有一根2026-10-12 09:00旧柱缺少provenance；当前检查点无volume_anchor。前者直接触发BAR_PROVENANCE_UNAVAILABLE，后者独立阻断当前柱核验。未删除记录、猜填标签、反推起点或主动回补。

reader错误已包含合约、周期、原始柱标签的北京时间、MySQL/Redis位置、缺失来源/时间语义及重新采集要求；标签仅用于定位，不代表已核验的柱开始时间。隔离回归31项Python及模拟器Node检查通过。此变更仅改善诊断，真实可用性仍未闭环，完成数保持48/54；需可信来源记录及原始累计量边界证据才能验收成功。
## 2026-10-21 Phase 8/9 实际修复与验收（最新）

- 原始边界真实成功：14:45周期采集到14:44:55新浪源报价，累计量916684；源时间/字段/哈希原样保留。14:47:52报价的当前观测量2124，六项证据均通过；存档`tests/fixtures/market_simulator/live_fu2701_20261021_verified.json`。14:44:22无边界真实负例仅VOLUME_ANCHOR失败，存档同目录`live_fu2701_20261021_blocked.json`。均明确synthetic=false，离线回放不改原始字段。
- 实际已登录浏览器成功创建FU2701基准：冻结14:48:30、源报价14:48:14，闭合15m 256根截至14:45、60m 512根截至14:15；当前量2450，来源/START与END/覆盖PARTIAL及UNKNOWN明确显示。不会因行情更新自动替换冻结结果。
- 真实首分钟发现上一周期higher检查点误参与合成，先复现后修复：只在来源/标签可核验且end<=当前start时排除旧base，同周期或未知base仍校验。另增加取数跨周期结束时的形成中保护，源码修复重启后执行了隔离回归。真实页面还暴露默认OI以-45.000填入整数控件导致编辑报错，已规范为-45并加DOM回归；该修改后的真实价格编辑/重置全流程未在收盘前复验，不冒充完成T026/T046。当前已休市，不能创建新基准属预期。
- 成交量问题：FU2701 11:06被记190474手，停采后恢复时跨缺口累计差分错误集中到首分钟，后续修复保留该值；VOL原显示P98×3削峰还会掩盖真实原数值。已修复归属、质量传播与后续修复保留，去除削峰。390项Python回归通过，含指标、量价雷达、模拟器、模拟验证、AI质量及时间边界；裸bootstrap模块通过测试目录PYTHONPATH执行。
- 独立审查另发现实际跨日量比样本未检查、维护缓存覆盖整份来源两项P2，已分别新增失败回归并修复，独立复核7项volume测试与Node通过，无新增重要问题。维护只追加质量，不改变原source/response_sha256/coverage或raw volume。
- 实际维护已完成：1m11:06原190474、5m11:05原191831、30m/60m11:00原201630，4条DB和4条缓存均标UNVERIFIED_ALLOCATION；原始数值、价格、持仓和来源保留。完整前后审计`docs/audits/2026-10-21-FU2701-volume-gap.json`状态APPLIED，依据见同名md。save_raw_tick=False，无原始完整逐笔可用于精确恢复，不能补造分钟量。
- 新服务PID19768，8000监听及未登录auth/me为401 JSON。实际浏览器刷新后，VOL不再出现假巨柱；点击11:06显示“量 未知（采集缺口）”，OHLC4831/4832/4829/4831、持仓226662保留。实际截图`docs/audits/2026-10-21-FU2701-volume-fixed.png`。
- Node通过：check_volume_quality、check_market_simulator、check_intraday_hover_legend、check_ai_verification、check_ai_session_lines。旧模拟验证测试的脚本数断言14已同步到实际15及13→14→15顺序，未改模拟验证产品行为。

当前64项任务，59项完成；T026/T042/T044/T046/T054仍需完整真实交互、正式计时或人员验收。T017真实正负来源样例已完成；Phase 8/9实现、审计及当前可开展的实际检查完成。历史旧验收数字和旧PID仅代表原时点。未改CTP，未主动调用真实模型、发起交易、重写历史AI快照或模拟终态账本。

