# Implementation Plan: 行情模拟器 V1

**Branch**: `codex/future-trend-20261001` | **Date**: 2026-10-19 | **Spec**: [spec.md](spec.md)

**Input**: `specs/001-market-simulator/spec.md`；用户要求复用现有规则、隔离行情/信号/账户、不修改冻结CTP模块。

## Summary

在现有合约工作台增加独立抽屉，以服务端冻结的新浪行情和合法15分钟当前柱生成假设收盘情景；复用提取后的15分钟纯规则计算，返回两侧评分、证据差异和固定参考计划毛收益风险比。模拟请求只读取行情及所属账户的已存在分析结果，不发布信号、不访问交易执行对象、不调用模型、不新增订阅或回补。新增进程内临时快照，不新增业务表、Redis模拟缓存或后台任务。

状态：Phase 0研究、Phase 1数据模型/契约/验证指南已完成；代码及隔离验证已实施，真实来源/人员验收未完成。setup-plan输出的BRANCH为`001-market-simulator`，这是脚本从feature目录回退得到的功能标识；实测Git分支仍为上方分支，本次不切换分支。

## Technical Context

**Language/Version**: Python 3.11、现有原生JavaScript；金额和价格使用Decimal。

**Primary Dependencies**: 现有FastAPI/Pydantic、SQLAlchemy、Redis客户端、IndicatorCalculator、FuturesTradingCalendar；不新增运行依赖。

**Storage**: MySQL/Redis仅使用只读方法取原有数据；临时冻结对象存在单进程内存，绑定用户及登录会话，30分钟硬过期、关闭释放，服务重启后失效。

**Testing**: venv中的unittest、FastAPI TestClient、Node `.cjs`行为回归；纯计算及接口使用隔离存根，不用生产计划或真实模型调用。

**Target Platform**: 当前Windows单进程Uvicorn和现有浏览器；业务日期显式Asia/Shanghai，不依赖执行机的系统时区推断。

**Project Type**: 现有单体Web应用上的隔离研究功能。

**Performance Goals**: 300ms防抖后，正常单用户20次操作至少19次在1秒内得到结果或明确错误；快照创建单独显示加载，不计入拖动计算时间。

**Constraints**: 不变更冻结CTP模块；不改正式规则发布行为；缺数据不默认通过；禁止把临时输入交给on_tick、SignalEngine.evaluate_15m_signals或交易/AI调度入口。

**Scale/Scope**: 一次一个月份合约、三个参数、15m+60m规则；每用户至多4个临时快照，全进程至多128个，每份序列限制15m最多256根、60m最多512根、当前小节1m最多240根；总内存预算64MiB，超限拒绝创建，不逐出仍在用的他人基准。

## Constitution Check

*GATE: 在研究前检查，Phase 1完成后再次检查。*

项目宪章仍为初始化占位模板，不把模板条款视为已批准治理规则。依据实际AGENTS.md和用户边界执行以下门禁，未虚构宪章符合性。

| 门禁 | 研究前 | 设计后 | 证据/落实方式 |
| --- | --- | --- | --- |
| CTP冻结 | 通过 | 通过 | 无CTP文件、实例或接口变更；共用规则的正常行为由回归锁定 |
| 行情、交易与有费调用隔离 | 通过 | 通过 | 纯计算模块不导入存储/执行器；读适配器仅GET/SELECT；内存快照不写业务状态 |
| 正式规则兼容 | 通过 | 通过 | LEGACY政策与原发布优先级保留；模拟STRICT政策独立 |
| 数据来源及时间不伪造 | 通过 | 通过 | 保留来源/时间标签/覆盖未知，阻断不可靠当前柱，不编成交额和涨跌停 |
| 文档同步 | 通过 | 通过 | 本次更新AGENTS/架构导航/README中技术方案入口，区分计划文件和已实现入口 |
| 实施后重启及验证 | 适用时执行 | 已纳入指南 | 本次仅文档不重启；实施后台变更后stop/start及401健康验证 |

## Project Structure

### Documentation (this feature)

```text
specs/001-market-simulator/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/market-simulator.md
├── tasks.md
└── checklists/requirements.md
```

`tasks.md`已由后续`$speckit-tasks`生成，原45项加Phase 7的9项共54项；当前48项完成、6项真实来源/人员验收待完成。

### Source Code (repository root)

下列为已实施代码路径；输出对象采用models.py的TypedDict白名单与输入Pydantic校验，服务端store以深拷贝隔离冻结对象。

```text
backend/signal_engine/
├── rules.py                         # 新：纯规则、三态证据、严格/兼容政策
└── signal_engine.py                 # 改：正常入口复用规则，保留发布与副作用
backend/market_simulator/
├── __init__.py                      # 新：空包入口，无初始化动作
├── models.py                        # 新：允许未知的冻结/输入/结果模型
├── reader.py                        # 新：显式新浪只读装载及一致性校验
├── store.py                         # 新：有界、会话隔离的内存快照
└── service.py                       # 新：假设构造、比较、毛收益风险比、结论
backend/api/
├── market_simulator_routes.py       # 新：sim门禁、冻结/计算/释放接口
└── routes.py                        # 改：挂载独立路由
frontend/
├── index.html                       # 改：按钮、抽屉、命名空间样式、追加脚本
└── js/
    ├── 15-market-simulator.js        # 新：状态机、输入、请求、比较展示
    ├── 06-ai-session.js             # 改：明确点击的参考run选择凭据
    ├── 03-contracts.js              # 改：合约变更统一使模拟会话失效
    ├── 02-chart.js                  # 改：退出登录时失效清理
    └── 11-websocket.js              # 改：实际selectSymbol入口使模拟会话失效
 tests/
├── test_signal_rules.py             # 新：纯规则和阈值
├── test_signal_engine_compatibility.py # 新：改造前180组发布兼容固定对照
├── test_market_simulator.py         # 新：纯情景/毛收益风险比/快照生命周期
├── test_market_simulator_api.py     # 新：身份/输入/只读隔离/一致性
└── check_market_simulator.cjs       # 新：输入竞态、生命周期、展示行为
```

**Structure Decision**: 功能保持局部模块，不从API routes反向导入读取函数，不创建另一个行情引擎。规则模块只依赖数值/数据模型和静态指标函数。前端第15脚本在14之后加载，自行绑定事件；现有单级`/js/{filename}`路由可服务该文件，无需放宽文件名规则。

## Phase 0: Research Decisions

完整依据见[research.md](research.md)。

1. 提取一个规则核心，正式入口用LEGACY、模拟用STRICT；不同的是缺失数据政策，完整数据的权重和判断相同。
2. 专用只读reader按冻结截点合并历史，保留provenance；不调用会混入形成柱的现成行情响应或AI快照创建流程。
3. 当前15m需要20根闭合历史+1根候选柱；规格边界表述已统一。ATR需要额外历史时读取上限内更多柱。
4. 临时快照留在服务端内存，浏览器只提交标识和三个假设。没有数据存储写入，也不接受客户端伪造历史。
5. 显式参考run由已有用户点击行为提供；后端重读所属用户结果验证，自动默认run不算选择。
6. 未知涨跌停保持null；无有效最小变动价位阻断，不使用产品规格表的兜底默认值冒充已核实合约规格。

## Phase 1: Design

### 规则提取与兼容边界

`evaluate_rules(candidate, history_15m, history_60m, policy)`返回两个方向的四项条件、证据、分数及OI_SURGE基础条件，不保存任何结果。历史必须已排序且排除候选柱，使用最后20根作为突破和均量窗口。EMA沿用当前算法及两位小数量化，60m序列冻结范围保持两列一致。

正式`evaluate_15m_signals()`签名不变，仍按LONG_WATCH→SHORT_WATCH→OI_SURGE顺序至多发布一个。保留不足21个总输入返回None、缺趋势默认通过、原ATR回退10/止损1.5ATR/原reasons格式/保存次数；以重构前固定样例验证全部输出。模拟不构造正式SignalData，也不调用发布入口。STRICT的条件可为null，受影响方向评分null，不以已通过的部分分数冒充完整分数。

### 冻结与一致性

读取显式`quote:sina:{symbol}`及`bar-engine:forming:sina`，禁止回退未知feed的legacy报价。流程为：报价Q1+原始检查点S1→只读历史/元数据/参考run→检查点S2+报价Q2；Q和S前后均相同，S.time等于报价源时间，分钟末价/持仓/累计量与报价相符，才接受。最多尝试3次，无长等待；竞争失败返回409。

闭合序列按来源时间标签算真实结束，严格不超过冻结截点，排除当前15m柱；校验交易日、品种交易小节、重复冲突及交易时段内真实缺口。未知时间标签不能当START猜测，相关序列不可用。DB与Redis同柱优先采用有效、来源可辨的Redis记录，冲突且无法判断则阻断，绝不重写来源。

当前柱的实际OHLC必须有本周期开始分钟证据；读取本周期已闭合1m加当前分钟，与checkpoint的higher核对。缺首分钟、发现中途首次初始化造成量累计基准不明、跨小节、丢分钟或无法确认开盘/极值时阻断。所有自产柱既有PARTIAL/UNKNOWN标签保留：字段齐全和槽起点相等本身不是完整性证明。若现有数据不足以证明可用，返回CURRENT_BAR_UNVERIFIED，禁止为让功能可用改写coverage；本期不扩展采集建设。这是已确认的数据限制，实施须有一组来源可核验的当前柱成功样例和一组真实不完整阻断样例，否则不得宣称端到端可用。

#### 当前柱观测证据判定表

“实际OHLC”在本功能中指来源可核验的本机已记录观测，不声称覆盖交易所全部成交。reader按以下顺序判断，任一false或null均以422 CURRENT_BAR_UNVERIFIED阻断，并在reasons列出项名；不以PARTIAL标签一律拒绝，也不以PARTIAL或字段存在一律放行。

| 判定项 | 必须取得的记录/关系 | 缺失或不成立时 |
| --- | --- | --- |
| PERIOD_IDENTITY | Q/S及分钟记录同合约、sina、交易日，全部处于合法当前小节和15m槽 | 阻断，不跨小节拼接 |
| FIRST_MINUTE | 首条记录真实start等于period_start，含有限正open/high/low/close及可辨来源/时间语义；不能拿中途Tick取整后的槽标签代替 | 阻断，不生成开盘价 |
| MINUTE_SEQUENCE | 从首分钟至当前分钟，每个已发生合法分钟恰有一条可辨观测；只允许当前分钟未闭合，闭合记录不得越截点 | 缺口/冲突阻断，不补柱 |
| VOLUME_ANCHOR | 原始来源已有周期开始前的累计量基准及明确归属边界，或已核实的交易日零起点；与当前累计量同日、未重置且差值等于逐分钟volume之和 | 来源不存在、边界归属未知、首次中途初始化则阻断，不能从volume和当前累计量反推anchor |
| OHLC_VOLUME_MATCH | 首分钟open、全部分钟high/low极值、最后close及volume之和与checkpoint.higher+minute合成结果逐项相同；所有high>=max(open,close)、low<=min(open,close)、volume>=0 | 任何矛盾或未知阻断 |
| SOURCE_COHERENCE | 前后Q/S均稳定、S.time=Q.timestamp，末价和累计量相符；持仓仅在双方均已知时比较，单方未知留警告，已知但不等则阻断 | 竞争最多3次后409；其他不一致422 |

合成单元样例（不是已采集事实）：period_start=09:15，已知归属该边界前的累计量anchor=100000；09:15/09:16/当前09:17分钟量为100/200/50、当前累计100350，所有分钟与checkpoint OHLC一致，则上述项可全部true，来源coverage仍PARTIAL。删除09:15记录、删除anchor或使当前累计100351，分别必须阻断。真实成功样例只能使用现有真实记录，并保存来源指针；没有anchor不能通过人工补字段制造真实成功，实际可用性保持待验收。

持仓缺失另走可评估性政策，不作为OHLC/成交量证据失败：允许基准和use_defaults比较，缺当前OI或上一柱OI时当前增仓条件和总分null；只有当前OI未知时可用显式假设change和已知上一柱OI生成模拟列。上一柱OI未知时所有use_defaults=false请求返回422 OI_BASELINE_UNAVAILABLE；不覆盖真实未知字段。

时间统一到带`+08:00`的北京时间；源无时区格式仅按现有本地新浪约定解释并标记该约定。新建基准要求active小节、0～25秒源延迟、源时刻不在未来、正确交易日；以RealtimeAnalyzer现有25秒常量为依赖，不复用自动模拟盘30/10秒成交政策。已有基准可继续做旧情景，超过25秒/小节关闭显示基准已旧，30分钟硬过期需更新。每次计算不重新取行情。

### 会话、输入和前端

内存store用随机不可猜ID、RLock及只读冻结副本，绑定username和现有会话令牌的不可逆摘要；不返回令牌/摘要。会话变更或进程重启后不可复用，过期和释放按需清理，不新增后台线程。创建频率每会话每秒最多1次；计算每会话每秒最多10次；拒绝超限返回429，不调用账号交易对象。

store不保留释放/过期tombstone：每次访问先在锁内淘汰过期对象，过期/已释放/不存在/重启丢失/非所属会话统一404 SNAPSHOT_UNAVAILABLE，不承诺区分这些原因。有效本人DELETE为204，其后重复DELETE为404；前端关闭对204及404都认为清理完成，不阻塞退出。限流键也随非活跃窗口淘汰，计入64MiB预算，不随无效ID增长身份记录。

价格/倍率用有限Decimal字符串，持仓整数；保留defaults中的精确观测成交手数。无覆盖或reset走defaults专用分支，避免“显示四舍五入量比再回算”导致当前/模拟不一致。显式倍率按ROUND_HALF_UP得到整数手，校验不低于已成交量。百分比快捷值也按ROUND_HALF_UP对齐tick，滑块上下界向内对齐tick；价格手工输入不静默修正。未知turnover始终null，纯规则不依赖它。

service在创建快照时以闭合15m序列调用IndicatorCalculator.calculate_atr(...,14)，不把当前候选柱加入ATR。raw_min=max(tick,freeze_price-5*ATR,已知lower_limit)，raw_max=min(freeze_price+5*ATR,已知upper_limit)；未知停板不参加该侧交集。下界按tick向上取整，上界向下取整。ATR缺失/非正、非法tick或min>=max时range不可用；非法tick仍按原门禁拒绝创建，其他情形constraints.slider_min/max=null、slider_available=false及中文reason，手工价继续独立校验。测试须涵盖正常范围、单/双侧停板、tick0.5/0.02、ATR缺失/零、交集为空/仅一价位及合法手工价超滑块范围。

错误契约统一带stage和nullable context：仅在授权取得有效基准后回传其服务端身份/冻结时间/对应允许输入；创建前失败不造冻结时间。401/403和快照404不给基准内容，非法body不原样反射。HTTP/JSON解析前错误由前端保存的generation和input_seq关联本地状态，不能因缺服务端context落地到新抽屉。

抽屉维护generation、snapshot_id、input_seq及账号/合约上下文，300ms debounce并取消前请求，所有返回提交前校验全部上下文。关闭、logout、任意合约赋值路径、重新打开和更新基准递增generation，清旧结果；即使网络取消无效也不能落地旧响应。文本用textContent呈现，样式限`market-simulator-*`。

### 参考计划与结论

仅当已有盘时段画线的`selectAISessionChartRun()`确由用户点击时记录明确选择（合约、账户上下文、batch/run）；初始化自动选择、轮询fallback均清除此凭据。冻结时提交run_id而非价格，后端核对run归属、同合约、完成时刻不晚于freeze、已复核发布、明确方向、有效窗口及目标/失效价；非有效盘时段计划不使用。无选择时P1可用、RR为NO_REFERENCE_PLAN。

RR使用固定价位按规格计算；随着墙上时间超出已冻结参考计划有效期，返回REFERENCE_EXPIRED，不重新取或替换计划；保持冻结行情及规则结果不变。方向为中性、冲突或不可评估时不给追价动作。默认结论仅表达条件敏感性，绝不生成触发信号或暗示成交。

### 验证及需求覆盖

| 范围 | 规格 | 计划验证 |
| --- | --- | --- |
| 身份/冻结/隔离 | FR-001～005、021 | API门禁、两次读取一致性、store生命周期、所有写操作触碰即失败 |
| 输入/生成柱/规则 | FR-006～014、022 | ≥20组纯规则与正常发布对照；未知、零值、阈值和舍入 |
| 快捷场景 | FR-015 | 六类按钮、无效回滚、tick取整、下限超过滑块时的提示 |
| 参考计划/RR/结论 | FR-016～019 | ≥12组方向、价位等号、1.5门槛、越权run、过期和无选择 |
| 异步界面 | FR-020 | 慢请求乱序、切换/退出/关闭、多次更新、网络失败及滚动保持 |
| 用户与性能 | SC-001～007 | quickstart列出的隔离集、浏览器计时及5名用户验收，不以文档代替实测 |

参见[data-model.md](data-model.md)、[接口契约](contracts/market-simulator.md)、[quickstart.md](quickstart.md)、[任务清单](tasks.md)。任务拆分及五项一致性修订复核已完成，见[修订记录](checklists/analysis-remediation.md)，可进入实施；代码与隔离验证已实施，真实/人员验收见acceptance.md。

## Complexity Tracking

无须豁免的治理冲突。内存store是隔离冻结数据和限制资源所必需；不采用Redis持久快照，避免研究操作写入真实共享数据面。现有当前柱来源不足时保留阻断，不能为兼容性降低门槛。

实施补充：END柱不能跨休市连续小节；历史最后柱到cutoff之间缺少应闭合周期时阻断。重置先取消输入防抖，缩量仅改倍率不改价格。静态方案不等于真实来源可用，16个现有检查点缺anchor的限制保留。
# 2026-10-21 修复方案补充（优先于原V1数据来源限制）

1. 新增纯模块`backend/common/bar_evidence.py`：新浪源报价边界证据构建/校验与交易分钟历史窗口规范化。原始源时间和标签保留，PARTIAL/UNKNOWN不升级。
2. `MultiPeriodBarEngine`仅在sina分支记录新的上一源报价和15m边界，随forming保存/恢复；不改变既有OHLCV合成、撮合和其他feed逻辑。周期前最后一次真实观测须在前一分钟、同交易日且无累计量倒退；中途启动无anchor。
3. reader高阶历史只读新浪新响应，按已知来源的交易分钟END规则生成start/end/segments，检查完整已闭合序列；本机1m仅读取当前15m窗口需要的分钟，避免无关旧记录阻断。缺当前分钟、冲突、来源未知均不补造。
4. 每个新基准仅获取一次两周期历史，Q/S稳定冻结重试复用本次响应，最终以获取完成后的新Q/S及对应截点冻结；避免重复网络请求，旧形成柱不得闭合提前使用。请求异常明确失败。
5. 先补失败测试，再修复，执行模拟器、来源、交易日历及模拟验证回归；同步AGENTS/架构/验收记录，后端重启验证端口和401；真实来源按实际结果记录。

## 2026-10-21 截图异常成交量修复补充

用户截图已定位FU2701 2026-10-21 11:06分钟记录190474手，服务11:06:34恢复时存在跨交易分钟缺口，修复任务保留了实时来源字段。禁止将该差分认作单分钟实际量，也禁止削峰/按比例摊回或用新浪未经核验的历史v替代。

新增`backend/common/volume_quality.py`统一不可分配分钟量状态：采集仅把同日跨缺口的累计差分另存volume_gap，首分钟保留实际接纳后新增的可观察部分，volume_quality标为UNALLOCATED_GAP；状态在后续Tick、闭合和高阶合成继承。旧错误保留数据库原volume及来源，标为UNVERIFIED_ALLOCATION；API以volume=null、volume_raw及原因返回。规则、指标、AI、图表均不得把该原值当有效量；去除VOL显示层分位数削峰，未知绘制空点并提示。

维护仅针对有明确恢复缺口证据的分钟及由其组成的本机高阶柱，先保存完整备份/原因，再事务写质量字段和同步缓存；不改真实价格、持仓、原成交量字段、不碰终态模拟账本或旧AI冻结结果。后续历史价格修复保留已有量质量标记，不再让自动修复洗掉未知。

