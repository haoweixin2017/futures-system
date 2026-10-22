# Tasks: 行情模拟器 V1

**Input**: `specs/001-market-simulator/` 中的 [spec.md](spec.md)、[plan.md](plan.md)、[research.md](research.md)、[data-model.md](data-model.md)、[契约](contracts/market-simulator.md)、[quickstart.md](quickstart.md)。

**Prerequisites**: 上述设计文件已存在；`.specify/memory/constitution.md`仍为未填写模板，协作边界按项目`AGENTS.md`执行。沿用当前Git分支，不覆盖已有工作区改动。

**Tests**: 规格SC-003～007及技术方案明确要求隔离、规则一致性和异步行为验证，因此生成测试任务；先写应失败的功能测试，再实施。改造前特征测试应在旧实现上通过，提取后同样通过，不强求这类测试先失败。

**Organization**: 原V1及Phase 7共54项，Phase 8/9新增T055～T064，总计64项；当前完成状态以复选框及acceptance.md为准。原Phase 7完成48项；T017/T026/T042/T044/T046/T054因真实来源及人员验收保持未完成；任务清单生成不代表实现、测试或运行验收完成。

## Format: `[ID] [P?] [Story] Description`

- `[P]`表示完成其共同前置后，能够在不同文件中并行处理；不是自动授权启动代理。
- `[US1]`核心比较、`[US2]`固定计划追价比较、`[US3]`快捷情景；基础和最终阶段无故事标签。
- 下方代码路径均以项目根目录为基准，拟新增文件现已落地，尚未完成的真实/人员验收按记录执行；文档引用使用相对路径。
- 同一文件的后续任务必须串行；每批后端变更落实后按AGENTS.md重启及401健康验证，不等最后才加载新代码。CTP冻结、禁止真实模型调用、生产计划不用于测试。
- 每项提交完成标记前，应记录其检查命令或可核对证据；无法完成的真实行情/人员验收保持未完成，不能用固定样例代替。

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: 保留现有工作区并建立可追溯的实施基础，不增依赖或数据库迁移。

- [X] T001 在 `specs/001-market-simulator/acceptance.md` 建立验收记录，记录Git基线及已有改动清单、CTP排除范围、FR/SC映射和待执行状态，明确固定样例/浏览器/真实行情/5名用户验收分别记录；使用 `specs/001-market-simulator/quickstart.md` 的命令及成功标准，不复制凭据或模型原始请求。
- [X] T002 在 `backend/market_simulator/__init__.py` 创建空包，在 `tests/fixtures/market_simulator/README.md` 说明隔离样例来源和脱敏约束；包导入不创建引擎、连接交易账户或启动后台任务，不新增运行依赖。

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: 所有故事共用的规则核心、输入边界及内存快照存储。

- [X] T003 [P] 在 `tests/test_signal_rules.py` 编写≥20组纯规则及严格缺数据用例，固定20根闭合15m加1候选、35/30/20/15权重、70门槛、量比1.5等号、前高低严格突破、增仓0/正/负、EMA相等和60m不足；断言完整数据同规则同分、未知不得伪装默认通过。
- [X] T004 [P] 在 `tests/test_signal_engine_compatibility.py` 及 `tests/fixtures/market_simulator/legacy_signals.json` 为改造前正式入口建立可运行的特征对照，固定LONG_WATCH→SHORT_WATCH→OI_SURGE优先级、至多发布一次、不足21总输入、缺趋势默认通过、量比零基准、ATR回退10、1.5ATR止损、reasons/时刻/价格量化和保存次数；用存根阻断真实保存及跟单。
- [X] T005 在 `backend/signal_engine/rules.py` 提取 `evaluate_rules(candidate, history_15m, history_60m, policy)`，复用静态EMA/ATR，统一完整数据阈值和分值，支持STRICT/LEGACY；条件遵循“passed(bool|null)、points(integer|null)”，方向遵循“score(0～100|null)”和“state(MATCHED/NOT_MATCHED/UNEVALUABLE)”，缺必需条件时STRICT总分null，不导入存储、通知、账户或调度器；依赖T003/T004。
- [X] T006 在 `backend/signal_engine/signal_engine.py` 保持原方法签名并接入LEGACY纯规则返回值，保留T004固定的正式发布优先级、止损、原证据格式及保存路径，不将模拟参数接入正式入口；运行T003/T004对照全部通过后继续。
- [X] T007 在 `backend/market_simulator/models.py` 定义公共输入和规则返回对象，逐字落实“正的有限Decimal字符串，整除tick”“非负有限Decimal字符串”“有符号整数手，0有效”“所有未知字段拒绝”“数值输入最大32个字符、最多12位小数”；`client_sequence`为0至2147483647，`use_defaults=true`与覆盖三字段互斥、false时三字段必填，价格/比值传字符串、未知null，枚举按 `specs/001-market-simulator/data-model.md` 原值实现。
- [X] T008 在 `tests/test_market_simulator.py` 编写内存store及数值模型边界测试，覆盖有限数/字符串长度/小数位/额外字段/互斥模式、会话隔离、冻结副本不可变、失败不占槽、30分钟到期、释放及重启失效、4份/用户及128份/进程/64MiB预算、速率和并发；依赖T007。 另测淘汰前后过期均404、重复DELETE为404、无tombstone及限流状态有界，不能只测有效释放。
- [X] T009 在 `backend/market_simulator/store.py` 实现随机ID、RLock、username+登录会话令牌不可逆摘要绑定和有界内存租期，落实“expires=frozen+30分钟，quote_at不改写”“仅内存内部，不返回客户端”；创建每会话1次/秒、计算10次/秒，超限429不逐出他人快照，非所属404、过期404、有效释放204、重复/过期/不存在释放404且前端均视为清理完成，按需清理不新增后台任务或Redis写入；依赖T008。 按data-model统一SNAPSHOT_UNAVAILABLE：访问先锁内淘汰过期；已释放/不存在/跨会话/重启均404且context=null，不保留身份tombstone，非活跃限流键也清理并计入内存预算。
- [X] T010 执行 `tests/test_signal_rules.py`、`tests/test_signal_engine.py`、`tests/test_market_simulator.py` 对应unittest基础用例，把结果记录到 `specs/001-market-simulator/acceptance.md`；确认纯模块导入无存储/账户/模型副作用，正式对照一致且store隔离通过，基础门禁未通过不得实施故事集成。

**Checkpoint**: 完整输入规则已统一，正式行为兼容，严格缺数据与临时状态隔离均有证据。

## Phase 3: User Story 1 - 同一基准下的假设比较 (Priority: P1) MVP

**Goal**: 从合约抽屉完成冻结、三参数输入、当前/模拟对比、解释、重置与更新；不依赖参考计划或快捷按钮。

**Independent Test**: 有来源证据的固定样例可完成全流程，无覆盖/重置两列一致；真实行情更新不改基准，切对象不被旧响应覆盖，模拟副作用0次。缺参考计划明确显示无法计算RR。

### Tests for User Story 1

- [X] T011 [P] [US1] 在 `tests/test_market_simulator.py` 扩展当前柱构造/历史装载/比较用例，覆盖不抹除OHLC、成交量下限、负总持仓、原手数7/均量3的defaults精确恢复、非零量比舍入、≥15组缺失/零值/来源未知/历史不足；输入深度不变、分项差合计等于总分差，PARTIAL不改COMPLETE。 新增六项观测证据逐项缺失/矛盾及全通过样例（含零成交有效anchor）；新增当前OI缺失、上一柱OI缺失、defaults未知和覆盖拒绝矩阵；新增闭合15m ATR14的±5ATR、单/双侧停板、tick向内对齐、ATR缺失/非正、空/单价位交集及合法手工超滑块范围测试。
- [X] T012 [P] [US1] 在 `tests/test_market_simulator_api.py` 编写三端点契约测试，覆盖401/403/越权404/过期404/422/409/429/503、8KiB请求上限、no-store、三次双读竞争、失败不占槽；mock Redis及数据库全部写方法、正式保存、账户get/驱动、通知、模型、订阅/回补为触碰即失败，simulate阶段连行情读取也不得调用。 核对CREATE未建立时间为null、SIMULATE授权后错误含真实基准context、401/403/快照404无基准内容，解析前错误和非法body不回显；持仓覆盖错误码OI_BASELINE_UNAVAILABLE及重复DELETE/淘汰前后过期统一404均应覆盖。
- [X] T013 [P] [US1] 在 `tests/check_market_simulator.cjs` 编写抽屉状态机行为测试，覆盖300ms防抖、请求失败标旧结果、≥10组乱序/切合约/账号/关闭重开/更新、Abort无效时拒绝旧响应、滚动和草稿保持；不只搜索源代码字符串来证明行为。 新增无结构化context的认证/解析错误、CREATE时间尚未建立、404清理完成的界面测试，均只关联本地generation/sequence，不落地旧结果。

### Implementation for User Story 1

- [X] T014 [US1] 在 `backend/market_simulator/models.py` 完成FrozenSnapshot/BarObservation/ScenarioBar/Comparison：落实“初始schema=1；rules=15m-v1”“不接受主连或其他feed”“上下限可独立null”“与报价匹配；无法证实则阻断”“无覆盖/重置以原手数恢复”，历史有界且保留provenance，`turnover(nullable)`、`hypothetical=true`、`volume>=observed_volume且为整数`，候选不进历史；reference暂为null并保留后续扩展点。 open_interest、previous_closed_oi、defaults.open_interest_change可null；按data-model增加observation_evidence六项及volume_anchor、ATR滑块constraints和nullable ErrorContext，不把OI缺失视为价格/成交量证据失败。
- [X] T015 [US1] 在 `backend/market_simulator/reader.py` 实现仅SELECT/GET的月份合约和已闭合历史装载，显式sina键、15m≤256/60m≤512/当前小节1m≤240，按来源时间标签计算闭合结束和冻结截点，排序去重、检查交易时段内缺口、保留来源和覆盖；不从API反向导入、不混forming/CTP、不调用AI快照或回补；可核验tick缺失则阻断、权威上下限未知则null告警。
- [X] T016 [US1] 在 `backend/market_simulator/reader.py` 加入Q1/S1→历史→S2/Q2最多3次双读，验证源时间/末价/已知持仓/累计量匹配、北京时间交易日/ACTIVE小节/0～25秒新鲜度、当前15m合法槽与首分钟/连续性/OHLC证据；中途初始化量基准不明、缺首分钟/极值或跨小节返回CURRENT_BAR_UNVERIFIED，竞争409，不改源时间/coverage；依赖T015。 严格执行plan的PERIOD_IDENTITY/FIRST_MINUTE/MINUTE_SEQUENCE/VOLUME_ANCHOR/OHLC_VOLUME_MATCH/SOURCE_COHERENCE六项；记录每项passed(true/false/null)、reason及源记录指针，任一false/null阻断，累计量anchor不可反推；已知持仓才做Q/S相等校验，缺失仅告警。
- [X] T017 [US1] 在 `tests/fixtures/market_simulator/observations.json` 整理脱敏的来源可核验成功样例和不完整阻断样例，在 `specs/001-market-simulator/acceptance.md` 记录证据来源；若现有真实输入不能证明首分钟/开盘/极值，保持真实成功验收待完成并记录阻断，不把人工生成样例或仅有PARTIAL字段当作真实充分证据。 按plan示例构造明确标记为合成的全通过及删首分钟/删anchor/累计量不符样例供单元测试，再单独记录真实来源样例；不把合成数据标为真实成功。
- [X] T018 [US1] 在 `backend/market_simulator/service.py` 实现三参数构造和defaults专用分支，保持实际open与已发生high/low，倍率ROUND_HALF_UP到整数手并展示effective_ratio，OI=previous_closed_oi+change且非负；高于滑块范围的合法手工价仍允许、已知停板单侧校验、手工价不静默取整；无均量不填1倍、未知turnover不补0。 实现持仓矩阵：defaults可保留null；上一柱OI缺失时所有覆盖422 OI_BASELINE_UNAVAILABLE；仅当前OI缺失时可用显式变化+已知上一柱OI生成模拟OI，当前仍null。另在创建基准时计算仅闭合15m ATR14，range=freeze±5ATR，裁剪正价及已知停板后min向上/max向下对齐tick，ATR缺失/非正或min>=max则slider_available=false、slider_min/max=null并给reason；不得限制合法手工价。
- [X] T019 [US1] 在 `backend/market_simulator/service.py` 使用STRICT核心计算两列、证据差与无计划结论，落实“changes按direction+code一一对应，两边可评估才给points_delta”“合计严格等于方向score_delta”“缺一边则差值null”，方向枚举LONG/SHORT/NEUTRAL/CONFLICT/UNKNOWN及动作OBSERVE/COMPLETE_DATA等按数据模型；到25秒或离开原小节标baseline_stale但保留原freeze，至30分钟硬过期，不读实时行情；依赖T018。
- [X] T020 [US1] 在 `backend/api/market_simulator_routes.py` 实现 `/api/v1/market-simulator/snapshots` POST、`/simulate` POST、`/snapshots/{snapshot_id}` DELETE，使用现有Cookie认证+role=sim并绑定登录会话，限制Body 8KiB，映射契约状态码及中文detail，成功code=0和no-store，不泄露内部owner/session摘要；当前reference未选时返回null和NO_REFERENCE_PLAN，禁止接收客户端历史/feed/价位计划。 落实契约分阶段ErrorContext表：CREATE的freeze等未建立时间null，已授权有效snapshot错误回服务端context和允许输入，401/403/快照404不回基准数据；JSON解析前错误由前端序号关联，不回显任意body。
- [X] T021 [US1] 在 `backend/api/routes.py` 挂载独立market_simulator路由，确保不改变现有行情、交易、AI路由及单级JS服务限制，API集成测试不因模块导入触发模拟写操作；依赖T020。
- [X] T022 [US1] 在 `frontend/index.html` 增加行情模拟按钮、抽屉、模拟标签、两个明确列名、时间来源/质量、三参数/滑块/整数步进、重置/更新和独立 `market-simulator-*` 样式；在14脚本后追加 `/js/15-market-simulator.js`，RR暂显示未明确选择有效参考计划，不显示未实施快捷按钮。
- [X] T023 [US1] 在 `frontend/js/15-market-simulator.js` 实现加载/计算/失败/关闭状态机、300ms防抖及取消、generation+账号+instrument+snapshot_id+client_sequence全量响应校验，首次/reset走use_defaults、更新清结果并释放旧快照；表格/原因用textContent，合法输入不被轮询覆盖，保留滚动，关闭best-effort释放失败用TTL兜底；依赖T022及契约。 展示slider_available/slider_reason、ATR范围缺失时保留手工输入，OI未知不显示为0；404释放视为完成，基准不可用时提示更新。
- [X] T024 [US1] 在 `frontend/js/03-contracts.js` 的全部当前合约赋值路径及 `frontend/js/02-chart.js` 退出入口接线到模拟会话失效，覆盖删除自选/恢复列表/切账号，递增generation并清输入/旧结果；模拟脚本未加载时原流程仍正常；依赖T023。
- [X] T025 [US1] 执行 `tests/test_market_simulator.py`、`tests/test_market_simulator_api.py`、`tests/check_market_simulator.cjs`，核对≥20组零覆盖/规则、≥15组非法/缺数据、≥10组切换竞态和副作用0次，将US1结果及真实样例可用性分别登记 `specs/001-market-simulator/acceptance.md`，未执行的浏览器验证保持待验收。 将U1/I1/C1/I2/U2修订的证据门禁、持仓矩阵、ATR边界、错误上下文及统一404生命周期逐项纳入结果，不能只以总体覆盖率替代。
- [ ] T026 [US1] 按 `specs/001-market-simulator/quickstart.md` 完成US1重启/8000监听/未登录401及Ctrl+F5浏览器验证，验收打开→修改→解释→重置→真实行情更新不换基准→手动更新→关闭重开；记录来源可核验的真实成功和真实阻断，缺成功证据不得标记端到端通过；此任务不依赖US2/US3。

**Checkpoint**: US1是可独立交付的MVP；RR缺计划和无快捷按钮不影响核心比较。当前柱证据无法核实时合法阻断，不能因此跳过真实成功验收。

## Phase 4: User Story 2 - 固定计划下的追价比较 (Priority: P2)

**Goal**: 用户明确选择已有有效参考run后比较固定目标/止损下的毛收益风险比，缺计划时核心功能继续。

**Independent Test**: 多头1320/1270，入场1280和1300得到4.00和0.67；空头对称、目标/止损等号/过期/无方向/冲突均正确不可用，来源和周期明确显示。

### Tests for User Story 2

- [X] T027 [P] [US2] 在 `tests/test_market_simulator.py` 增加≥12组毛收益风险比用例，覆盖双方向、1.5未舍入阈值、两位显示、等于/跨越目标止损、无方向、冲突和墙上时间过期；固定参考价位始终不随情景移动，不自动补目标、滑点或净收益。
- [X] T028 [P] [US2] 在 `tests/test_market_simulator_api.py` 增加参考run归属、同合约、完成/复核发布/有效窗口校验测试，越权404、所属但不可用不阻断P1，拒绝客户端目标/止损；快照完成后simulate不重新查询run，也不接AI模拟验证plan_id。
- [X] T029 [P] [US2] 在 `tests/check_market_simulator.cjs` 增加明确点击与自动首run/fallback的区别测试，账号/合约/batch变更清选择、选择新run不改既有snapshot，验证无计划/来源周期/毛比值及不追价原因可见。

### Implementation for User Story 2

- [X] T030 [US2] 在 `backend/market_simulator/models.py` 实现FrozenReference/RewardRisk，原样落实“direction只允许LONG/SHORT，target/stop有限正数并满足该方向关系”“完成时间<=冻结时刻，冻结在计划有效窗口内”“status(AVAILABLE/UNAVAILABLE)”“ratio(nullable)”“reason_code(NO_REFERENCE_PLAN/REFERENCE_EXPIRED/DIRECTION_CONFLICT/NO_DIRECTION/INVALID_LEVELS)”“gross=true、fees_included=false”；内部所属账户不外传。
- [X] T031 [US2] 在 `backend/market_simulator/reader.py` 加入可选run_id只读重读与归属/合约/复核/时刻/窗口验证，冻结source/source_timeframe/completed_at/valid_from/valid_until/方向/目标/止损；不存在或越权404，所属但无效返回reference_reason，不猜最新/默认模型，不调用模型或修改run；依赖T030。
- [X] T032 [US2] 在 `backend/market_simulator/service.py` 实现固定目标止损的多空毛收益风险比和结论，未舍入值与1.5比较、显示两位ROUND_HALF_UP；参考过期只禁用比值不改冻结行情，无方向/冲突不判断追价，不用abs把错误价位转成正比；动作使用DO_NOT_CHASE/RR_ACCEPTABLE/PLAN_NOT_APPLICABLE并保留风险文字；依赖T031。
- [X] T033 [US2] 在 `backend/api/market_simulator_routes.py` 接入reference_run_id冻结及白名单计划返回，simulate两列在同次调用判定过期并返回一致原因；保持无计划P1响应和只读隔离，无额外交易账户访问；依赖T032。
- [X] T034 [US2] 在 `frontend/js/06-ai-session.js` 的实际run点击入口记录显式选择凭据、自动选中/fallback清凭据，在 `frontend/js/15-market-simulator.js` 打开/更新时只提交该run_id并显示冻结来源、周期、固定价位、毛比值/未含手续费滑点和不可用原因；选择变化只提示更新基准，不替换旧快照；运行T027～T029并记录 `specs/001-market-simulator/acceptance.md`。

**Checkpoint**: US2对US1的参考计划为空场景保持兼容；US2不依赖快捷按钮，可用固定样例独立验证比值及计划权限。

## Phase 5: User Story 3 - 快捷情景 (Priority: P3)

**Goal**: 六个按钮填入同基准的可解释值，减少手工计算，不隐含增仓或路径假设。

**Independent Test**: 已知前高/前低/tick时逐个核对值，只有放量突破联动倍率；非法停板或低于真实成交量时原输入不变。

### Tests for User Story 3

- [X] T035 [P] [US3] 在 `tests/test_market_simulator.py` 增加六类presets测试，核对前高+tick、前低−tick、突破1.8倍、缩量0.7倍、冻结价±1%及ROUND_HALF_UP，持仓变化保持，缺基准/越停板/低于已发生成交量时禁用且解释。
- [X] T036 [P] [US3] 在 `tests/check_market_simulator.cjs` 增加快捷按钮填值/预览/300ms触发及失败原值不变测试，缺前高低/量比基准时只禁相关按钮，快捷不得重置冻结时间或参考计划。

### Implementation for User Story 3

- [X] T037 [US3] 在 `backend/market_simulator/service.py` 生成同一snapshot下六类presets及可用原因，只用当前冻结历史前20根/冻结价/tick，百分比取整按ROUND_HALF_UP；单独价格按钮保留倍率和持仓输入，放量突破仅联动1.8倍；动态当前输入下的最终合法性仍由simulate复核。
- [X] T038 [US3] 在 `backend/api/market_simulator_routes.py` 的snapshot响应接入presets可用状态/建议值/禁用原因，保持simulate契约不新增隐藏参数，禁用项不伪造价格或量比；依赖T037。
- [X] T039 [US3] 在 `frontend/index.html` 及 `frontend/js/15-market-simulator.js` 加入六个快捷按钮、实际采用值预览、无效回滚，保持其他字段、snapshot及原参考计划；运行T035/T036和既有核心回归后把结果记录 `specs/001-market-simulator/acceptance.md`。

**Checkpoint**: US3不依赖US2；没有参考计划时快捷填值及规则解释独立可用。

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: 验证完整交付并维护入口文档，不能只完成代码就勾选真实验收。

- [X] T040 完成全部故事后，按 `specs/001-market-simulator/quickstart.md` 执行新模块Python/Node回归及 `tests/test_indicators.py`、`tests/test_realtime_analyzer.py`、`tests/test_realtime_analysis_api.py`、`tests/test_bar_provenance.py`、`tests/test_trading_time.py`，涉及既有AI画线选择时追加 `tests/check_ai_session_lines.cjs`；记录命令/结果，禁止用生产模型调用替代隔离测试。
- [X] T041 在 `tests/test_market_simulator_api.py` 及 `specs/001-market-simulator/acceptance.md` 完成全流程副作用、身份会话隔离、4/128份与64MiB预算、到期/释放/限流压力及重启失效复核；确认成功和失败都不写真实数据，敏感Cookie/摘要/原始模型响应不出现在返回或日志。 特别确认无tombstone，内存淘汰前后过期均404、重复清理不阻塞，错误context不泄露失效或其他会话的基准。
- [ ] T042 在 `specs/001-market-simulator/acceptance.md` 记录最终服务重启/健康及浏览器端到端、窄屏横向滚动、旧基准警告/参考过期、20次交互至少19次≤1秒、5名使用者至少4名2分钟内完成的实测；未开展人员或真实行情验证时保留待验收并明确限制，不伪造结果。
- [X] T043 根据实际实现同步 `AGENTS.md`、`docs/项目架构与AI导航.md`、`README.md` 的入口、15脚本加载、模块职责、测试命令和仍未验收事项，核对所有路径；此前每批系统文件变更也要同批检查文档影响，本项是最终一致性复核。
- [ ] T044 在 `specs/001-market-simulator/quickstart.md` 和 `specs/001-market-simulator/acceptance.md` 核对实际命令、接口及证据与plan/contracts/data-model一致，补齐FR/SC逐项结果与当前柱真实成功/阻断证据；若发现设计无法落地则记录差异并修订对应设计后再继续，不静默降门槛。
- [X] T045 对 `specs/001-market-simulator/tasks.md` 逐项回查实施和验收证据，仅为真实完成任务勾选；执行限定本功能文件的diff空白/链接检查，确认无冻结CTP改动、无新增交易执行接线及无覆盖原工作区改动，向用户分别报告代码、隔离测试、浏览器和真实数据验收状态。

## Dependencies & Execution Order

### Phase Dependencies

```text
T001 → T002 → (T003 || T004) → T005 → T006 → T007 → T008 → T009 → T010
  → US1: (T011 || T012 || T013) → T014 → T015 → T016 → T017
          → T018 → T019 → T020 → T021 → T022 → T023 → T024 → T025 → T026
  → US2: (T027 || T028 || T029) → T030 → T031 → T032 → T033 → T034
  → US3: (T035 || T036) → T037 → T038 → T039
  → T040 → T041 → T042 → T043 → T044 → T045
```

默认按P1→P2→P3执行；US2与US3业务上只依赖US1，不互相依赖，但共享service/routes/前端/测试文件，实施不能整阶段无冲突并行。`||`仅表示同一组中不同文件的测试任务可同时编写/执行；通过共同前置后才开放这些机会。

### User Story Dependencies

- US1依赖Phase 1～2，可独立形成MVP。
- US2和US3依赖US1的冻结/计算接口，无彼此功能依赖；各自测试可以独立运行，整个交付优先US2后US3以避免同文件冲突。
- 每个故事先测试→模型→服务→端点→页面集成→证据；US3复用已有模型，不额外造实体。
- T017成功样例缺证据时不能假装完成；可继续验证阻断及其他确定性计算，但T026真实端到端和最终完成声明保持受阻，不能将该风险藏到交付之后。
- 后置文档/验收依赖全部预定故事完成，外部人员验收缺席不等于代码验证失败，但对应任务仍不勾选。

### Parallel Opportunities

仅标出10项测试任务：T003/T004，T011/T012/T013，T027/T028/T029，T035/T036。同文件任务跨阶段串行，不将shared models/service/routes或UI集成标为并行。

## Parallel Example: User Story 1

完成T010后，同时编写T011（`tests/test_market_simulator.py`的构造比较）、T012（`tests/test_market_simulator_api.py`的权限隔离）、T013（`tests/check_market_simulator.cjs`的异步行为）。共同依赖契约已固定；之后按T014起串行集成，同阶段测试不共享真实数据库或账户。

## Parallel Example: User Story 2

完成US1后，同时编写T027的毛比值案例、T028的run权限案例、T029的明确选择界面案例；三个不同文件，之后统一由T030～T034完成后端/前端接线，不同时修改已有US1实现。

## Parallel Example: User Story 3

完成US1且未与US2修改共享文件时，同时编写T035的presets数值案例和T036的按钮交互案例；然后T037～T039串行实现。并行只用于上述两个测试文件，不代表可与US2同时改service/routes/前端。

## Implementation Strategy

### MVP First (User Story 1 Only)

完成T001～T026，验收三参数、冻结基准、解释和隔离。无参考计划显示明确原因；快捷按钮不作为MVP前置。MVP演示必须说明真实当前柱来源是否已验收，不能以人工样例通过替代真实可用性。

### Incremental Delivery

US1核心可用→US2固定计划毛比值→US3快捷填值→最终验证。任何阶段不得通过放宽数据质量、补造字段、触发交易或收费模型来获得“成功”。每批修改按AGENTS维护相关文档及重启，不创建无关重构任务。

## Requirement Coverage & Completeness

| 需求/标准 | 实施与验收任务 |
| --- | --- |
| FR-001 | T007、T012、T015、T020～T024、T026 |
| FR-002 | T012、T019～T023、T025、T041 |
| FR-003 | T014～T017、T020、T023、T026 |
| FR-004 | T011、T018～T019、T022、T025 |
| FR-005 | T008～T009、T013、T023～T026 |
| FR-006～009 | T007、T011、T014～T018、T025 |
| FR-010～012 | T003～T006、T011、T019、T022、T025 |
| FR-013～014 | T007、T011（ATR范围边界）、T015、T018（闭合ATR14范围生成）、T022～T023 |
| FR-015 | T035～T039 |
| FR-016～018 | T027～T034 |
| FR-019 | T019、T027、T032、T034 |
| FR-020 | T013、T023～T025、T029、T036、T042 |
| FR-021 | T008～T009、T012、T016、T020、T025、T041 |
| FR-022 | T003～T006、T010、T025、T040 |
| SC-001～002 | T042（真实人员及计时，不以测试存根代替） |
| SC-003 | T003、T011、T018～T019、T025 |
| SC-004 | T012、T025、T041 |
| SC-005 | T027～T034 |
| SC-006 | T011～T012、T015～T017、T025 |
| SC-007 | T012～T014、T020、T023～T026、T042（分阶段错误上下文） |

完整性核对：45项连续唯一ID，41项完成、4项真实来源/端到端/人员验收待完成；10项[P]仅在不同文件、共同前置完成后可并行；US1 16项、US2 8项、US3 5项；共享基础10项、最终完善6项。模型约束和协议错误明确落实到任务；无新增依赖/数据表/CTP任务。检查清单本身不增加业务验收通过数。

## Notes

- 本次结果为任务清单，converged仅指任务覆盖与格式检查完成，不代表代码已经收敛。
- `.specify/extensions.yml`不存在，before_tasks/after_tasks无注册钩子，不自动创建分支或提交。
- 五项修订及一致性复核已完成，下一步可用`$speckit-implement`按本清单实施；本次不自动进入开发。

- 2026-10-19五项修订已纳入原任务，参见[修订复核记录](checklists/analysis-remediation.md)；ID与45项总数不变，实施状态以checkbox和acceptance.md为准。

## 实施状态（2026-10-19）

代码、固定样例及隔离浏览器流程已落地。T017缺真实成功来源样例，T026缺真实盘中成功，T042缺5名用户及真实计时，T044缺上述真实验收闭环，保持未完成。已实测现有16个新浪检查点均无volume_anchor；实际工作台休市阻断已验证。不得将41项完成称为真实行情模拟器全部验收通过。详见[acceptance.md](acceptance.md)。


## Phase 7: Convergence

**核查日期**：2026-10-19。仅追加本阶段，前文45项任务、原复选框和历史统计保持原样；前文41/45为本阶段追加前的实施快照，不代表新增任务已经完成。追加时本阶段9项均未完成，共54项、41项已勾选、13项待完成。实施后T047～T053已完成，当前48/54项完成，6项外部证据/人员验收未完成。

**核查依据**：spec的22项FR、7项SC（SC-001为人工验收项）、12个用户故事验收场景，plan的6项研究决定及设计约束，以及全部T001～T045；宪章仍为未填写模板，跳过原则审查。发现partial 6项、contradicts 3项；HIGH 3项、MEDIUM 6项，无missing、unrequested或CRITICAL发现。当前未发现需要扩张CTP、采集建设或交易执行范围的依据。

**本次证据**：18个Python测试（`tests.test_signal_rules`、`tests.test_market_simulator`）及`node tests/check_market_simulator.cjs`通过；只读Node DOM存根复现滑块二次渲染重新启用与快捷倍率误拒；只读Python复现1.1996×1000按ROUND_HALF_UP得到1200手，以及损坏检查点JSON/历史空时间抛普通异常。通过现有测试不能替代尚缺的隔离哨兵、DOM回归、真实数据或人员验收。本阶段未运行生产模型、订阅、回补或交易。

### HIGH

- [ ] T046 [US1] 闭环F1：在`backend/market_simulator/reader.py`现有只读输入范围内核查可用原始记录，确认是否存在满足六项观测证据、尤其VOLUME_ANCHOR的真实来源；仅在有真实证据时补充`tests/fixtures/market_simulator/observations.json`的脱敏来源样例及`specs/001-market-simulator/acceptance.md`的来源指针，并依`quickstart.md`验证真实盘中创建→修改→重置→行情变化不替换基准→手动更新→关闭重开与真实不完整阻断，逐项关联原T017/T026/T044。当前验收记录已确认16个检查点无anchor，不能只等开盘便假定能成功；如仍无合法现成证据，记录明确的设计/来源阻塞和需另行设计的事项，本项及真实成功验收保持未完成，但继续执行不依赖真实来源的其他收敛任务。禁止反推/人工补anchor、改coverage、降低门槛或擅自修改采集器/冻结CTP。per FR-003/021、US1/AC1/AC4、plan: 当前柱观测证据判定表、T017/T026/T044 (partial)

- [X] T047 补齐F2：在`tests/test_market_simulator_api.py`以隔离依赖执行创建、修改、重置、更新、失败和释放全流程，将Redis写方法、SQL add/delete/commit/flush、正式信号保存、账户获取/驱动、通知、模型、订阅及回补分别设为触碰即失败，并将导入初始化与请求调用分开计数；补测三端点的跨登录会话、过期/重启、409/429/503、失败不占槽和敏感字段不外泄。扩展`tests/check_market_simulator.cjs`，使用DOM存根或隔离浏览器覆盖实际render/按钮事件、错误、300ms合并输入、账号切换、关闭重开/连续更新、Abort无效、草稿与滚动保持、204/404释放及明确run/自动fallback，不以重复同一种乱序循环替代场景覆盖；新增用例关联本阶段修复，执行结果写`specs/001-market-simulator/acceptance.md`，不得继续以仅控制器测试或静态检查宣称全部界面/副作用验收通过。per FR-002/020/021、SC-004/007、T012/T013/T025/T029/T036/T041 (partial)

- [X] T048 [US1] 修复F3：在`frontend/js/15-market-simulator.js`的request、控制器和render中保留允许的结构化错误context/fields及本地已提交输入关联；CREATE失败明确显示合约、15m、模拟属性与“冻结时间尚未建立”，已授权有效snapshot上的计算错误展示其服务端冻结上下文和本次失败输入，保留的旧成功结果单独标明所属输入。认证、解析前及失效/越权404只使用本地请求身份说明，不声称基准已获授权或泄露内容；所有错误仍受generation/key/snapshot/sequence校验。展示BELOW_OBSERVED_VOLUME的已发生手数和最低有效倍率等允许字段，补充`tests/check_market_simulator.cjs`的创建失败、422、401/403/404及慢错误跨对象回归。per SC-007、FR-020、contracts: 分阶段ErrorContext、T013/T020/T023 (partial)

### MEDIUM

- [X] T049 [US1] 修复F4：调整`frontend/js/15-market-simulator.js`每次render的控件禁用逻辑，不让通用`disabled=!snapshot`覆盖`constraints.slider_available=false`或未知量比的禁用状态；ATR缺失/零、范围空/仅一价位及均量缺失时，在READY→COMPUTING→ERROR/READY、重置等连续渲染后仍禁用对应滑块，手工输入按其独立规则保留。现有106行先统一启用、120/123行只在首次snapshot初始化修正，DOM复现第二次渲染price-range.disabled从true变false；在`tests/check_market_simulator.cjs`加入连续渲染回归。per FR-013、plan: ATR滑块范围、T018/T023 (contradicts)

- [X] T050 [US3] 修复F5：统一`frontend/js/15-market-simulator.js`快捷场景的动态合法性与`backend/market_simulator/service.py`的ROUND_HALF_UP整数手政策，禁止以requested_ratio直接小于observed_volume/avg或浮点epsilon替代实际整数成交量校验；可采用精确十进制预校验或先由服务端预计算、成功后再提交输入草稿，失败时原输入完整保留。复现基准均量1000、已成交1200、原倍率1.1996，价格快捷按钮应允许采用1200手，不能被前端误拒；同时覆盖真正低于1200手、1.8/0.7倍预设、已知停板、当前草稿无效和持仓字段不被改写，补充`tests/check_market_simulator.cjs`及必要Python边界回归。per FR-007/008/015、US3/AC1/AC2、T035/T036/T039 (contradicts)

- [X] T051 [US2] 补齐F6：在`frontend/js/15-market-simulator.js`的selectReference/clearReference和展示逻辑中，当当前显式run/batch选择相对已冻结reference发生变化时显示“参考选择已变化，请更新基准”，不替换既有snapshot或价位，不自动请求模型；当前冻结参考展示来源、周期、方向、形成时间及valid_from/valid_until，并保留无效/过期原因。现有selectReference只赋值、没有通知render，当前参考行只展示形成时间；补充`tests/check_market_simulator.cjs`验证明确点击、清选择/默认fallback、切batch、选择变化及更新后提示消除。per FR-005/016/018、plan: 显式参考run选择、T029/T034 (partial)

- [X] T052 [US1] 补齐F7：在`backend/market_simulator/reader.py`、`backend/api/market_simulator_routes.py`及`frontend/js/15-market-simulator.js`生成并白名单返回、展示当前观测与15m/60m历史的来源、时间标签、原始coverage及质量摘要；当前quality只有通用warnings和空blocking_reasons，而snapshot响应排除了含真实coverage的observed_bar及历史，不能由固定“新浪”文案代表各序列已核实状态。历史不足、均量非正、当前/上一闭合OI缺失等按实际提供可评估性原因，保留PARTIAL/UNKNOWN及null，不补0、不改变六项门禁。扩展`tests/test_market_simulator_api.py`与Node展示回归，核对质量摘要与真实冻结记录一致且不暴露内部会话或原模型内容。per FR-003/007/011、US1/AC1、contracts: quality、T014/T015/T022/T025 (partial)

- [X] T053 [US1] 修复F8：在`backend/market_simulator/reader.py`为检查点JSON及历史时间/数值字段建立显式数据校验，把源记录缺失、畸形或无法证明有效的情况映射为契约规定的422及可核对原因（当前观测不足使用CURRENT_BAR_UNVERIFIED，历史未知标签/时间保留相应明确原因），真实Redis/数据库读取故障继续503，不把所有异常统一包装成行情服务故障。现有251行json.loads(s1)的损坏JSON抛JSONDecodeError、normalize_bar的bar_time=null抛AttributeError，创建接口108～114行捕获后均变503；补充`tests/test_market_simulator.py`及`tests/test_market_simulator_api.py`验证缺字段/坏JSON/非法时间与真实存储异常的区别、CREATE时间保持null及失败不占store槽，不捕获后伪造可用数据。per FR-003/021、SC-006、contracts: 创建错误状态码、T016/T020 (contradicts)

- [ ] T054 闭环F9：在上述可独立修复完成后按`specs/001-market-simulator/quickstart.md`实测并在`acceptance.md`记录窄屏滚动、旧基准/参考过期、20次连续交互耗时及无旧结果覆盖、5名使用者至少4人在2分钟内完成并解释变化；缺真实来源或参与人员时明确保留相应原T042/T044及本项待验收，不伪造结果。逐项核对FR/SC证据，并同步实际状态与命令到`contracts/market-simulator.md`（当前仍写“接口尚未实现”）、quickstart及受影响的AGENTS/架构导航/README实施统计和说明；不得把历史41/45或本轮18个测试通过写成全部功能完成。后端修复按AGENTS重启并独立验证8000监听/401，前端刷新及真实可用性分开记录；收敛阶段未完成项与外部阻塞分别报告。per SC-001/002/007、T026/T042/T043/T044、plan: 验证及文档同步 (partial)

**执行顺序与完成判定**：T046先核实来源阻塞，不能以外部条件未具备阻止其他独立修复；T047提供缺失验证与失败复现，T048～T053按共享文件串行修复并运行相应回归，最后T054汇总验收。人工/真实来源任务仅在证据齐备后完成。旧任务不由converge重写或取消勾选；后续implement只依据实际完成证据更新状态。本次唯一写入为本追加阶段，没有修改规格、方案、契约、业务代码、既有任务或冻结CTP模块。


### Phase 7 实施结果（2026-10-19）

T047～T053已完成并经隔离回归；T046重新只读核查16个检查点仍无volume_anchor，保持来源阻塞；T054文档与健康部分已完成，真实盘中、正式20次浏览器计时、窄屏现场及5人验收仍待开展。原T017/T026/T042/T044保持未完成。当前54项连续唯一，48项已勾选、6项未完成；验收详情见[acceptance.md](acceptance.md)。本次实施修改了功能代码、测试及受影响文档，与前述converge仅追加任务的历史操作分别记录。

独立审查补验：T053范围内补齐当前已知总持仓非负校验，三种负OI组合及真实reader→HTTP422已先复现再修复，未知与0保持；91项Python及Node回归再次通过，复核P2已解决。最终重启PID44300、8000监听/401 JSON健康通过；外部验收任务保持未完成。
## Phase 8：2026-10-21 来源闭环修复（用户明确授权）

- [X] T055 新增真实源报价边界证据、跨周期/跨日/重复/中途启动/重启恢复的失败回归；新增交易分钟END跨休市窗口与旧未知数据保护测试。
- [X] T056 实现sina分支边界证据采集与forming保存恢复，不回算、不更改其他feed及真实OHLCV计算。
- [X] T057 实现只读新浪15m/60m历史标准化、完整窗口校验和当前1m窗口读取，保留原始来源、标签和覆盖；失败不回退缓存。
- [X] T058 完整collector→reader→HTTP隔离成功/失败/无副作用回归，执行相关专项测试。
- [X] T059 同步文档，重启健康验证并进行真实来源检查；真实成功及浏览器验收按实际证据单列，不伪造完成。

## Phase 9：2026-10-21 成交量跨采集缺口修复（用户截图追查）

- [X] T060 复现11:06恢复采集把跨缺口累计量压入首根分钟的错误，验证正常午休不误判、质量标记跨Tick/闭合/高阶合成不丢失。
- [X] T061 修复源差分归属，跨缺口差分另存审计、未知量输出null；指标、量价规则、AI与图表保留未知，不以0或削峰替代。
- [X] T062 对FU2701 11:06及受影响本机高阶柱做可审计质量隔离，保留原数值和来源，不伪造真实分钟量；同步Redis。
- [X] T063 完成采集/存储修复/接口/指标/模拟器/前端回归及独立审查，重启验证实际页面无假柱且未知明确展示。
- [X] T064 同步AGENTS、架构、数据口径和真实验收记录，区分已验证的模拟基准成功与仍待人员/后续开盘复验事项。

