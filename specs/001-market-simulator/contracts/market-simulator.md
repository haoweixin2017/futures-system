# Contract: 行情模拟器 V1

版本：1；日期：2026-10-19。接口已实现并经隔离回归；真实盘中成功与人员验收仍未闭环，见../acceptance.md。所有路径以`/api/v1/market-simulator`为前缀，沿用现有Cookie认证；仅role=sim可访问，服务端固定sina，不接收feed、任意规则版本、原始历史或目标/止损价。

## 通用约定

- JSON字段snake_case；时间ISO8601带+08:00；Decimal为字符串，未知值null，手数整数。请求额外字段返回422。
- 成功返回`{"code":0,"data":...}`；响应`Cache-Control: no-store`，不记录Cookie或完整分析结果到日志。
- 业务错误返回`{"detail":{"code":"错误枚举","message":"中文说明","fields":{},"reasons":[],"context":null}}`，不能仅以code=0返回一份旧结果。context形状和分阶段返回规则见下表；认证框架或JSON解析错误未返回结构化context时，前端仍按本地请求generation/sequence识别，不能展示为成功。
- 非登录401、非sim403；跨用户或不同登录会话的snapshot/run返回404；过期、已释放、不存在和重启丢失的snapshot统一404 SNAPSHOT_UNAVAILABLE，不使用410或长期身份标记。所有创建/计算/释放操作验证现有会话；写入的是临时研究内存，不改变任何真实业务对象。
- Body上限8KiB，字符串数值至多32字符和12位小数，client_sequence为0至2147483647；非法数、NaN/Infinity、非法tick均422。模拟数值约束以[data-model.md](../data-model.md)为准。

| 失败阶段 | context内容 | 不得返回的内容 |
| --- | --- | --- |
| CREATE，已认证且已校验合约 | stage=CREATE、instrument、timeframe=15m、hypothetical=true、已解析允许inputs；snapshot_id及冻结/报价/到期时间null | 不把请求开始时间当已建立freeze，不反射非法字段 |
| SIMULATE，已授权且取得有效基准 | stage=SIMULATE、服务端instrument/timeframe/snapshot_id/frozen_at/quote_at/expires_at、hypothetical=true、client_sequence及已解析允许inputs | 不回显Cookie、内部owner/session摘要、任意原始body |
| RELEASE，有效所属基准但业务操作失败 | stage=RELEASE及已授权基准context，inputs=null | 不返回其他账户内容 |
| 401/403或快照404；解析前失败 | context=null；前端只展示本地提交对象及错误，不称服务端已确认基准 | 不透露基准是否曾存在、归属和时间 |

## POST /snapshots

2026-10-21来源更新：创建只读请求新浪15m/60m原始历史（不写库、不回补），END按交易分钟网格解释并保留源标签/响应哈希，不混用本机高阶缓存。1m当前窗口仍读取本机观测；volume_anchor须有完整原始源报价、实际源时间、哈希及同日边界，旧字段不补造。历史取数失败返回503 HISTORY_FETCH_FAILED_15M/60M；缺柱422 HISTORY_GAP；当前证据不足422 CURRENT_BAR_UNVERIFIED。Q/S重试复用本次已读取的历史响应，并按最终新报价截点重新筛选闭合柱；取数后重验25秒新鲜度。浏览器创建超时45秒，simulate仍10秒且不重新取数。外部接口响应字段与账户隔离不变。

打开或更新基准。创建过程只读行情、元数据及可选已存在参考run，成功后写进程临时store。

Request：

```json
{
  "instrument": "JM2701",
  "reference_run_id": null
}
```

reference_run_id可省略；只有前端明确点击现有盘时段run时传入。后端重读归属和有效性，不凭浏览器声称的身份或价格信任。run_id越权/不存在返回404；所属但不可用的run返回成功基准并令reference_plan=null及reference_reason，P1继续。

Response data必含：

| 字段 | 含义 |
| --- | --- |
| snapshot_id/schema_version/rules_version | 唯一基准、协议及规则版本 |
| instrument/feed/timeframe/trading_day | 规范合约/sina/15m/交易日 |
| frozen_at/quote_at/expires_at | 冻结、真实源报价及内存租期 |
| history_cutoffs | 15m/60m已闭合截止及数量 |
| session/period | 原小节及当前柱合法开始、结束 |
| defaults | simulate_price、volume_ratio、observed_volume、open_interest_change |
| constraints | tick、atr14、slider_min/max、slider_available/slider_reason、upper/lower_limit、min_effective_volume_ratio及各未知原因；只用闭合15m ATR14，±5ATR后裁剪有效停板/正价且按tick向内取整；无ATR/非正ATR或min>=max禁用滑块并返回null上下界，手工输入仍可用 |
| presets | 六类快捷按钮的可用状态、建议价格/倍率及不可用原因 |
| reference_plan/reference_reason | 白名单来源、时间、方向、目标、止损或null/原因；不返回原请求或密钥 |
| current | STRICT当前情景评分和逐项证据 |
| quality | warnings、blocking_reasons、observed/current历史覆盖和来源 |

不存在有效开盘/极值/实际成交量等构造必要数据时422（CURRENT_BAR_UNVERIFIED/CURRENT_BAR_MISSING）；休市或陈旧/未来/未知报价时间422（MARKET_CLOSED/STALE_QUOTE/INVALID_QUOTE_TIME）；交易日冲突422；最多3次双读仍不一致409（SNAPSHOT_RACE）。必需历史不足允许返回可用输入基准，但受影响current.score=null，quality明确原因；若缺均量则volume_ratio=null且依赖量比的计算阻断，不能默认1倍。容量或速率超限429。存储读取失败503；失败不分配快照。

元数据tick不可核验返回422（INVALID_PRICE_TICK）；涨跌停未知不阻断，但limits=null并告警。禁止静默读取CTP或legacy未知feed。

当前柱严格执行[方案判定表](../plan.md#当前柱观测证据判定表)六项，失败reasons给出证据项名。保留PARTIAL/UNKNOWN，不声称全交易所成交覆盖；没有累计量anchor不能反推或补造。若仅当前/上一柱持仓缺失，可成功创建，observed_bar.open_interest、previous_closed_oi及defaults.open_interest_change按实际可知性为null，current两侧总分null；不填0。已知Q/S持仓矛盾仍阻断。

## POST /simulate

Request（覆盖）：

```json
{
  "snapshot_id": "随机标识",
  "client_sequence": 7,
  "use_defaults": false,
  "simulate_price": "1295",
  "volume_ratio": "1.8",
  "open_interest_change": 5000
}
```

Request（无覆盖或重置）：

```json
{
  "snapshot_id": "随机标识",
  "client_sequence": 8,
  "use_defaults": true
}
```

use_defaults=true与其他三个覆盖字段互斥；false时三个字段均必填。客户端应使用defaults分支发送首次计算和reset，避免量比显示舍入破坏零覆盖一致性。

计算只读取已存的冻结对象及当前时钟，不访问行情Redis/数据库，也不重新查模型结果。不更新规则或目标价。每个返回包含与本次输入对应的有效数值；current与simulated在同一调用中判定参考计划是否过期。

Response data：

| 字段 | 结构 |
| --- | --- |
| snapshot_id/client_sequence/rules_version | 请求关联标识 |
| context | instrument、timeframe、trading_day、frozen_at、quote_at、expires_at |
| inputs/effective_values | 请求三个假设及实际使用的price、volume、ratio、oi_change、OI总量 |
| current/simulated | price、volume_ratio、oi_change、long、short、state、reward_risk |
| long/short（嵌套） | score(integer或null)、state、conditions[] |
| condition（嵌套） | code、actual、threshold、passed(bool或null)、weight、points或null、reason_code |
| changes | direction、condition_code、current_points、simulated_points、points_delta；方向总差独立提供 |
| conclusion | direction、action、reason_codes、text |
| baseline_stale/quality | 旧基准标记、真实覆盖及质量说明 |

reward_risk对象遵从data-model；缺计划不虚构比值。若输入倍率换算低于真实成交量返回422（BELOW_OBSERVED_VOLUME），fields包含最低有效倍率及已发生手数。假设OI为负422（NEGATIVE_OPEN_INTEREST）；价格越已知停板422（OUTSIDE_PRICE_LIMIT）；价格未对齐tick422（INVALID_PRICE_TICK）。无均量而仍提交倍率返回422（VOLUME_BASELINE_UNAVAILABLE）。

use_defaults=true允许持仓未知并正常200，两列该条件/总分/差值为null，已知其他条件仍可展示；上一闭合OI缺失时，use_defaults=false统一422 OI_BASELINE_UNAVAILABLE。仅当前OI缺失而上一柱OI已知时，显式整数变化可生成模拟OI及模拟评分，current仍未知、差值null。不完整历史/趋势的可评估结果正常200，但受影响score=null并明确原因；不进行部分总分外推。无方向、冲突、价位无效或参考过期不会把规则结果变成接口失败。

## DELETE /snapshots/{snapshot_id}

本人同登录会话当前有效快照释放返回204；重复释放、已过期、不存在、跨用户/不同会话及服务重启丢失的ID统一404 SNAPSHOT_UNAVAILABLE，context=null。过期对象在锁内先淘汰，不保留tombstone。关闭使用best-effort，对204/404均视为清理完成，不阻塞退出或合约切换；网络失败由TTL回收。效果上重复清理不产生额外操作，但不承诺重复请求同一状态码。

## UI Contract

- 第15脚本加载后绑定抽屉入口和生命周期，不依赖13-boot已执行前的注册时序。
- 持续显示模拟模式；两列名称严格区分当前未闭合情景与假设收盘；使用分值/条件变化色，涨跌方向不等同好坏。
- 价格/量比滑块、手工输入、持仓整数步进、重置、更新基准、六个快捷按钮；基准旧提示保留quote_at与freeze时间。
- 所有响应提交前检查generation、账号、instrument、snapshot_id、client_sequence；更新基准期间不展示旧结果为新结果。
- 显式run点击凭据随账号/合约/batch/默认fallback变化清空，打开时freeze其来源；模拟期间用户切换参考run不改变已有快照，需要更新基准。
- 快捷按钮的值来自同一快照；若无效不改字段；不能设置超过已知涨跌停或低于已发生量的场景。
- 数字/时间/不可用原因及条件用文本节点渲染；计算响应不清空输入草稿、展开状态或滚动位置。

## Side-effect Contract

允许：原业务数据SELECT/GET/HGET/HGETALL/LRANGE、内存临时对象创建/计算/释放、普通HTTP访问日志。

禁止：模拟请求写Redis/MySQL、写行情或信号、创建/驱动行情引擎、账户get/on_tick/on_signal、发通知/下单、模型调用、订阅/回补、修改历史或AI计划。权限与隔离测试须分别mock这些能力为触碰即失败；不能仅靠“页面没有下单按钮”证明隔离。


## Phase 7 落实说明

- quality.sequences.current/15m/60m包含count、providers、sources、label_semantics、coverage去重摘要，空序列count=0，不推定完整覆盖；quality.evaluation_reasons包含code/message，明确历史不足、非正均量及当前/上一柱持仓未知。来源原值PARTIAL/UNKNOWN保持，不返回完整历史。
- selected_reference_run_id保存冻结时提交的显式选择ID（包括所属但不可用的选择），用于辨别之后的选择变化；授权仍以既有run所属校验为准。前端提示更新且不替换旧snapshot，并展示有效参考的方向和有效窗口。
- 快捷操作300ms防抖后由simulate校验整个提议，仍使用原三参数契约；只有匹配当前请求的成功响应才提交输入草稿。失败保留原草稿，并展示本次失败输入和允许的context/fields，旧成功结果单独标明所属输入。
- 无效检查点JSON/结构或当前必要字段返回422 CURRENT_BAR_UNVERIFIED；历史记录格式/数值错误422 INVALID_HISTORY，时间缺失或无效422 INVALID_HISTORY_TIME，来源或标签未知仍422 BAR_PROVENANCE_UNAVAILABLE；真实存储访问异常503 MARKET_READ_FAILED。失败不占用快照槽。

- SOURCE_COHERENCE同时要求双方各自已知的总持仓非负；任一负值即422 CURRENT_BAR_UNVERIFIED。双方/单方未知仍按既有矩阵处理，源报价持仓未知保留警告，不伪造相等校验。
