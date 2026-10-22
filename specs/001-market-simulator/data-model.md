# Data Model: 行情模拟器 V1

## 2026-10-21 来源证据补充

新forming字段`source_observation`保留最近一次新接纳的新浪原始报价字段。`volume_anchor`含schema_version=1、kind=LAST_SOURCE_OBSERVATION、boundary=BEFORE_PERIOD、at（周期标签）、value（前一真实源观测累计量）、trading_day、source_at（实际源时间）、source_ref（原始字段规范JSON的SHA256）、source_record（原始字段）、coverage=PARTIAL。source_at须在周期前一分钟内且同交易日，source_record/哈希/累计量须一致；缺字段和旧状态不从volume之和推导。该值是已记录观测的归属边界，非交易所精确整点累计量。

高阶历史只使用本次只读新浪响应：原始`provenance_json.label_semantics=END`、aggregation_clock=TRADING_MINUTES、source_time、request_url、requested_at、response_sha256、coverage=UNKNOWN均保留。规范化对象另加source_bar_time、start_at、end_at、segments；60m可含跨小休/午休的多个交易区间，实际交易分钟累计不含休市。原始存储不改写，不升级coverage。当前15m连续小节观测的六项验证不受历史跨休市规范化影响。

日期：2026-10-19。均为研究会话内存/传输对象，不新增ORM表或迁移；价格/比值使用Decimal，JSON中以十进制字符串传输，整数手数为integer，未知为null。

## FrozenSnapshot

| 字段 | 类型/含义 | 约束 |
| --- | --- | --- |
| snapshot_id | 不可猜随机标识 | 服务端生成 |
| owner/session_digest | 用户和登录会话摘要 | 仅内存内部，不返回客户端 |
| schema_version/rules_version | 固定版本字符串 | 初始schema=1；rules=15m-v1 |
| instrument/feed/timeframe | 规范月份合约/sina/15m | 不接受主连或其他feed |
| trading_day | YYYYMMDD | 与源报价及合法小节一致 |
| frozen_at/quote_at/expires_at | 带+08:00的时间 | expires=frozen+30分钟，quote_at不改写 |
| session_start/session_end | 本连续小节真实起止 | 左闭右开 |
| period_start/period_end | 当前15m槽及小节裁剪后结束 | 不跨小节，不把结束标签误作开始 |
| checkpoint_at/observation_evidence | 原状态时刻/开盘与连续性证据 | 与报价匹配；无法证实则阻断 |
| observed_bar | 当前实际观测柱 | 已发生开/高/低/末价/成交量/期末持仓 |
| histories | 已闭合15m/60m及当前周期1m | 有界、排序去重、保留来源和闭合结束 |
| metadata | tick、乘数、权威上下限及来源 | tick有效且可核验；上下限可独立null |
| defaults | 原价、精确原成交手数、精确量比、持仓差 | 无覆盖/重置以原手数恢复 |
| reference_plan | FrozenReference或null | 冻结时核验，不自动切换 |
| quality | 状态/警告/阻断、各序列截止 | 不把PARTIAL改COMPLETE |

单份内存对象不可变；读取后以独立假设对象计算，不在快照上就地改值。序列数和总预算见plan。失败创建不占槽，成功创建再提交store。

## BarObservation / ScenarioBar

字段：instrument、period、trading_day、start_at、end_at、open/high/low/close、volume、open_interest(nullable)、turnover(nullable)、provenance、coverage、observed_at。FrozenSnapshot中previous_closed_oi及defaults.open_interest_change均可为null；未知持仓不影响已充分验证的价格和成交量证据。

ScenarioBar额外标记hypothetical=true；保留实际open，high=max(observed_high, simulated_close)，low=min(observed_low, simulated_close)。不生成虚构turnover或盘口。volume>=observed_volume且为整数；覆盖情景持仓=previous_closed_oi+input_change，须>=0。没有上一闭合持仓不能以0补足，覆盖返回OI_BASELINE_UNAVAILABLE；use_defaults=true允许保留null并返回未知条件、null总分与差值。若只有当前OI缺失但previous_closed_oi已知，覆盖可由显式整数change构造假设OI，当前列仍null，绝不回填真实OI。候选柱不进入历史数组。

## ScenarioInput

| 字段 | 说明 |
| --- | --- |
| snapshot_id | 当前基准标识 |
| client_sequence | 递增请求号，原样回传；不能代替服务端身份验证 |
| use_defaults | true表示完全使用冻结默认，不接收另外三个字段 |
| simulate_price | 正的有限Decimal字符串，整除tick |
| volume_ratio | 非负有限Decimal字符串，无覆盖时不从显示值回算 |
| open_interest_change | 有符号整数手，0有效 |

所有未知字段拒绝。数值输入最大32个字符、最多12位小数，范围边界最终按合约tick和有效元数据检查，限制只是防超大数计算，不当成交易所限制。倍率换算采用ROUND_HALF_UP，显示requested_ratio与effective_ratio；defaults按原始手数保证零变化。

## RuleEvaluation / Comparison

- condition：code、direction、actual、threshold、weight、passed(bool|null)、points(integer|null)、reason_code。
- direction_result：score(0～100|null)、conditions、state(MATCHED/NOT_MATCHED/UNEVALUABLE)。任一必需条件未知，方向总分null；已知项仍可解释但不生成伪完整总分。
- comparison：snapshot_id、client_sequence、context、inputs、effective_values、current、simulated、changes、conclusion、baseline_stale、quality。
- changes按direction+code一一对应，两边可评估才给points_delta；合计严格等于方向score_delta。缺一边则差值null。
- conclusion：direction(LONG/SHORT/NEUTRAL/CONFLICT/UNKNOWN)、action(OBSERVE/DO_NOT_CHASE/RR_ACCEPTABLE/COMPLETE_DATA/PLAN_NOT_APPLICABLE)、reason_codes、text。
- 当前四项评分最高100且突破多空不能同时成立，因此标准完整数据下双侧同时>=70实际不可达；冲突状态作为防御性处理保留，不造数据以制造不可达验收。

## FrozenReference / RewardRisk

FrozenReference包含run_id、所属账户（内部）、instrument、source、source_timeframe、completed_at、valid_from、valid_until、direction、target、stop、explicit_selection=true。

从用户所属的已完成且已复核发布的盘时段run重读，不接受客户端价位；完成时间<=冻结时刻，冻结在计划有效窗口内。direction只允许LONG/SHORT，target/stop有限正数并满足该方向关系。用户明确点击凭据是界面来源证据，不是授权凭据；授权永远由后端归属校验决定。

RewardRisk包括status(AVAILABLE/UNAVAILABLE)、ratio(nullable)、reason_code(NO_REFERENCE_PLAN/REFERENCE_EXPIRED/DIRECTION_CONFLICT/NO_DIRECTION/INVALID_LEVELS)、direction、entry、target、stop、gross=true、fees_included=false。阈值比较使用原精度，显示两位ROUND_HALF_UP。到期后只禁用该参考计划的比值，不改变冻结行情。

## State Transitions

- 抽屉：CLOSED→LOADING→READY或BLOCKED；READY→COMPUTING→READY或ERROR；ERROR保留并标明上次结果。
- 更新基准：递增generation，释放旧快照并清空结果，再LOADING；失败不能回退伪装旧基准为新基准。
- 快照：CREATED→ACTIVE→RELEASED或EXPIRED；超30分钟、进程重启均需重新打开/更新。
- 变旧：ACTIVE内部可标baseline_stale=true，不修改quote_at；行情过25秒或离开原小节即标记。仍可计算旧情景至硬过期。
- 关闭、切合约、退出/切账号：取消请求、清输入结果与显式参考选择、释放store；响应到达也不得提交。
- 非所属/不同登录会话、过期、已释放、不存在或重启丢失的ID统一404；过期对象先删除后返回404。DELETE仅对当前有效且所属快照返回204，重复DELETE返回404，前端清理对204/404都视为完成。不保留tombstone，容量预算只统计有效快照及有界限流状态。

## ObservationEvidence / Constraints / ErrorContext

- observation_evidence记录每个判定项的passed(true/false/null)、reason、来源记录标识及源时间。仅全部true才可用；见[方案判定表](plan.md#当前柱观测证据判定表)。它证明本机已记录观测的一致性，不证明交易所完整覆盖。
- volume_anchor记录合法周期起点的累计成交量及其时间、来源，或可核验的同交易日边界前后累计量记录；不存在时null并阻断，不从聚合总量反推“证据”。当前成交量为0仅在anchor有效且真实累计差为0时视为合法零值。
- constraints含atr14(nullable)、slider_min/max(nullable)、slider_available、slider_reason、tick及可独立为null的权威upper/lower_limit。ATR复用当前静态算法，只输入闭合15m柱；无效ATR或向内对齐后min>=max时滑块禁用，手工校验独立执行。
- error.context含stage(CREATE/SIMULATE/RELEASE)、instrument(nullable)、timeframe=15m、hypothetical=true、snapshot_id(nullable)、client_sequence(nullable)、frozen_at/quote_at/expires_at(nullable)、inputs(nullable)。授权且找到有效snapshot后取服务端值；CREATE失败frozen_at等均null；401/403及无权/失效snapshot的404不回传任何基准内容。输入只回显已解析的允许字段，不反射任意非法body或Cookie；前端以本地generation/sequence关联解析前错误。
