# Phase 0 Research: 行情模拟器 V1

日期：2026-10-19。依据本工作区源码静态读取；未读取冻结CTP实现、未运行真实模型、未触碰交易状态。使用两个只读研究代理分别核对规则及快照/前端，结论经主任务汇总；没有修改业务代码。

## R1 规则提取与生产兼容

**Decision**: 新建纯规则核心，完整输入统一权重和阈值；STRICT返回三态证据，LEGACY维持正式缺数据政策。正常入口保留单信号发布和副作用，模拟永远不进入该入口。

**Rationale**: `backend/signal_engine/signal_engine.py:31`构造即获取Redis，`:34`计算入口会调用`:150`的保存函数，后续写库、通知、模拟跟单。当前权重35/30/20/15、门槛70，前20根窗口排除候选柱；60m不足时原趋势默认通过，必须避免重构时改变正式行为。

**Alternatives considered**: 直接调用SignalEngine不满足隔离；复制一套规则会漂移；直接调用RealtimeAnalyzer不匹配15分钟规则。`backend/indicator_engine/indicators.py:12`及`:22`的静态EMA/ATR可复用，但compute_all_indicators的前20极值和量比包含最新柱，不能拿它替代排除候选柱的窗口。

## R2 当前柱与冻结一致性

**Decision**: reader读取原始检查点、两次报价/检查点和闭合历史；一致才冻结，最多3次。当前周期证据不充分则阻断，不扩建采集。

**Rationale**: `backend/bar_engine/live_bars.py:7`可合并15m形成柱，但不暴露检查点时间；`backend/bar_engine/bar_engine.py:170`保存minute/higher/time/累计量等；`:325`至`:337`先写报价后写checkpoint，存在短暂竞争。构造引擎会启动writer、预热，on_tick会驱动模拟账户，均不可复用为情景输入。

中途首次Tick初始化的OHLC和零量不等于完整周期（bar_engine.py:79起），自产provenance为PARTIAL（:245起）。因此“有OHLC字段”不能作为来源充分证明。实现必须识别首分钟和连续性证据；CURRENT_BAR_UNVERIFIED是合法、必须验收的结果。当前静态证据不能证明任一生产合约此刻满足要求，运行可用性留待实施验收。

**Alternatives considered**: 只调用forming_bar看不到原检查点时刻；仅两次报价相同仍可能对应旧形成柱；直接读取或推送假Tick有副作用；修改源时间/补柱违反规格。

## R3 历史与元数据

**Decision**: 局部只读loader合并DB和Redis，保留provenance/标签，计算真实闭合结束并筛选截点。15m实际规则输入是20根已闭合历史+1根候选柱；规格边界文字同步澄清，不改变算法。

**Rationale**: `backend/api/routes.py:83`的读取方式可借鉴，但转换会丢来源字段；现有bars接口会合并形成柱。`backend/models/market_data.py:67`的BarData要求成交额等必填，而模拟不应补造未知字段。产品规格存在默认乘数10/tick1的兜底，不能把兜底作为已核实合约规格。新浪Tick没有权威涨跌停字段。

**Alternatives considered**: 调AI快照创建可能外部抓取；现有报价/柱直接转全局BarData可能把未知填零；根据昨结估算涨跌停没有权威比例依据。

## R4 服务端临时快照

**Decision**: 进程内限额store，30分钟硬过期、关闭释放、会话隔离，所有模拟请求只用snapshot_id。

**Rationale**: 同一基准比较需要不可变数据。单进程应用无需分布式状态；服务器控制序列和规则版本可防止客户端伪造输入背景。限制4份/用户、128份/进程、64MiB总预算；过载明确429。模拟只读原则针对真实业务数据，临时研究内存可创建/释放。

**Alternatives considered**: 每次重新取行情失去可比性；客户端回传全部历史无法信任；Redis快照增加共享写入和过期清理复杂性；签名全快照令牌会放大每次拖动请求。

## R5 已有参考计划的明确选择

**Decision**: 利用已有画线run点击入口记录显式选择，只传run_id；后端核验所属账户及时间/方向/价位，缺失则返回RR不可用。

**Rationale**: `frontend/js/06-ai-session.js:295`的selectAISessionChartRun是实际点击入口，但`:284`和`:304`附近也会自动选择首run；`aiSessionChartRunId`非空不能证明用户选择。`:24`可定位已有选中run，原目标/失效价来自其chart_plan。不能将AI自动模拟盘的plan_id混为参考交易计划。

**Alternatives considered**: 猜最新/首个模型违背明确选择要求；前端直接提交止损/目标可被伪造；新增手工计划编辑扩大V1范围。

## R6 前端生命周期和交付

**Decision**: 第15脚本及局部样式；generation+input_seq+snapshot_id+账户/合约共同防旧响应。关闭/切换/退出释放内存快照，释放失败靠TTL兜底。

**Rationale**: `frontend/index.html`当前加载01至14脚本；`backend/api/routes.py:1573`只允许单级合法js文件名，新脚本符合。`frontend/js/02-chart.js:1`处理logout，`03-contracts.js`存在多处currentInstrument赋值，不能只覆盖列表点击。DOM文本使用textContent，防止证据说明进入HTML解释。

**Alternatives considered**: 仅AbortController无法防已完成请求回调；只校验合约不能防同合约不同快照乱序；将大块逻辑继续塞入现有AI脚本增加耦合。

## R7 时间、阈值和数值

**Decision**: 新建冻结沿用新浪研究报价25秒门槛；期货日期使用北京时间、品种交易小节左闭右开。计算用Decimal，默认值按原手数保持，输入倍率才ROUND_HALF_UP换算。

**Rationale**: `backend/indicator_engine/realtime_analyzer.py:41`为SINA_STALE_SECONDS，自动模拟盘30秒/10秒是成交政策而非此功能政策。显示量比舍入再回算会破坏零覆盖一致性。`backend/common/trading_time.py:216`及analysis_session_options支持品种时段。

**Alternatives considered**: 浏览器时间或主机时区会错配夜盘；强行刷新报价时间伪造新鲜度；浮点误差会影响tick、1.5阈值和整数手数。

## Research Completion

技术选择均已决定，无待决定的技术项。真实数据是否足以构造可核验当前柱、浏览器性能及用户验收是实施验证项，不冒充已知运行事实。研究不请求网络搜索：本次集成事实以本地项目源码为主，未引入新的外部依赖。

## 2026-10-19 Remediation Decisions

本轮按用户明确授权处理U1/I1/C1/I2/U2，文档修订不代表已实施：

- Decision：以plan六项观测证据为唯一通过标准，保留实际来源覆盖标签；缺少可核验累计量起点时阻断，不补造来源。Rationale：本机记录一致性可判定，交易所逐笔完整覆盖不能由轮询证明。Alternatives considered：全部PARTIAL放行/全部拒绝、以人工计算anchor冒充原始证据均不采用。
- Decision：持仓允许未知基准/defaults；覆盖必须有上一柱OI；当前列未知不会被假设值补写。Rationale：分离真实未知与用户假设，仍能校验假设总持仓非负。Alternatives considered：缺持仓一律0或一律拒绝整个页面不采用。
- Decision：明确闭合ATR14滑块算法和任务；错误分CREATE/SIMULATE/RELEASE及认证/解析前场景定义nullable context。Rationale：实现和验收不再依赖隐含默认值。Alternatives considered：缺ATR强填常数、创建失败伪造冻结时间不采用。
- Decision：不存在/过期/已释放/跨会话统一404，无tombstone；关闭对204/404均完成。Rationale：一致、低内存且不泄露生命周期；不能在删除身份记录后仍承诺辨别本人已释放。Alternatives considered：额外长期tombstone及不确定时返回410均不采用。
## 2026-10-21 来源修复依据

用户明确授权补齐采集边界和历史时间语义，仍禁止猜填旧数据、反推起点或关闭门禁。旧源问题不是单根旧柱：本机START时钟高阶柱与新浪END交易分钟高阶柱混存，不能按标签相等去重或用END减自然60分钟推断开始。

只读取得FU2701新浪原始15m/60m各1023根；2026-10-21的60m源标签依次为10:00、11:15、14:15，符合日盘各小节拼接交易分钟的END网格。11:15对应10:00–10:15与10:30–11:15；14:15对应11:15–11:30与13:30–14:15，最后15:00仅45个交易分钟。新响应记录aggregation_clock=TRADING_MINUTES与源标签、URL、请求时间、原文响应SHA256；旧记录不补该字段。本轮真实15m256根、60m512根在该源网格的闭合/连续性检查通过，60m中174根跨休市；不代表成交覆盖COMPLETE。

累计量采用周期前一分钟内最后一笔真实源观测，嵌入原始报价、实际源时间和内容哈希，仅证明已记录观测的量差，不声称精确交易所整点累计量或完整成交覆盖。跨交易日、累计量回退、中途首次初始化、无边界/缺分钟继续阻断。原始较早源时间须显式保留，不能把前笔源时间改成周期整点。

