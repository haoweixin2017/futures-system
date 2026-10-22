# Quickstart & Validation: 行情模拟器 V1

## 2026-10-21 来源修复验证补充

执行`.venv\Scripts\python.exe -m unittest tests.test_simulator_source_evidence tests.test_market_simulator tests.test_market_simulator_api tests.test_bar_provenance`及`node tests/check_market_simulator.cjs`。新测试实际运行源报价→bar engine→保存/重启恢复→reader→HTTP创建/两种模拟/释放，并断言模拟不写行情存储、不再次取数；缺锚点/哈希篡改/缺闭合柱必须阻断。

重启后Ctrl+F5；新版本首次在15m周期中途运行时无锚点属预期，须等采集器实际记录下一周期前一分钟的源报价，再在首分钟收到新源报价。检查点中的source_at应是真实较早源时间，不能等于人工填入的整点；未形成真实证据不得宣称成功。创建可等待45秒以读取新浪两周期历史。source新鲜度仍25秒，超时请待源端新报价后手动更新；不改宽门槛。

日期：2026-10-19。代码及隔离验证已实施，实际执行结果见acceptance.md；以下为可复现指南，不代表未开展的真实/人员验收已经通过。数据结构和协议以[data-model.md](data-model.md)、[接口契约](contracts/market-simulator.md)为准。

## 1. 前置条件

- 项目目录`D:\期货实时交易系统`，使用现有`.venv\Scripts\python.exe`及Node；unittest，不要求安装pytest。
- 读AGENTS.md，保持CTP冻结。隔离用例沿用tests/test_bootstrap，确保替换存储、账户、模型、通知、订阅与回补依赖，禁止连接生产账户或创建收费计划。
- 准备至少20根已闭合15m、60根60m、合法当前柱及元数据的固定样例；另准备缺首分钟、检查点与报价竞争、未知来源/时间标签等失败样例。当前柱成功样例必须有可核验开盘/极值证据，不能把PARTIAL改成完整来源。
- 浏览器验收采用现有模拟用户登录，不在文档、终端或记录中复制凭据。交易日/数据截止使用北京时间。

## 2. 隔离自动验证（实施后）

在项目根目录执行：

```powershell
Set-Location -LiteralPath 'D:\期货实时交易系统'
.\.venv\Scripts\python.exe -m unittest tests.test_signal_rules tests.test_signal_engine tests.test_signal_engine_compatibility tests.test_market_simulator tests.test_market_simulator_api
node tests/check_market_simulator.cjs
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_indicators.py
.\.venv\Scripts\python.exe -m unittest tests.test_realtime_analyzer tests.test_realtime_analysis_api tests.test_bar_provenance tests.test_trading_time
```

期望：纯规则/模拟/API/前端全部通过；已有正式信号输出与改造前固定样例在类型、评分、reasons、止损、时刻、优先级、保存次数上完全相同。测试失败时先诊断，不能通过降低严格门槛或修改冻结历史绕过。

验收样例规模：≥20组规则/零覆盖/重置，≥12组参考计划，≥15组缺数据/非法输入，≥10组异步切换；覆盖多空、量比1.5上下、前高/低等号、持仓0/正/负、EMA相等、LEGACY缺趋势与STRICT未知的差异。标准四条件下双侧>=70不可达，冲突分支以注入评估对象测试，不制造不可能行情。

特别校验：原成交量7、均量3等有无限小数量比的defaults必须原手数恢复；实际倍率1.49999和1.50000按未舍入值判断；手工价格不自动改tick；已发生量超过滑块3倍时允许手工合法输入并显示最低倍率。

## 3. 副作用和权限验收

- 在隔离测试中，将Redis全部写方法、数据库add/delete/commit/flush、通知、账户获取/驱动、正式信号保存、模型、订阅和回补设置为调用即失败；现有模块导入初始化与请求动作分开计数，不能把生产依赖初始化当作模拟允许路径。
- 运行打开、重复修改、重置、更新、失败及释放全过程；确认仅临时store变化，输入快照深拷贝前后相同，副作用调用0次。
- 未登录401，非sim403，用户B及同用户另一登录会话无法使用用户A快照；越权run 404；非法字段422；过期/重复释放/进程重启均404、有效释放204且前端均视为清理完成；限额429不挤掉别人快照。
- 验证新建快照只读；simulate不再访问行情/模型库。真实市场本身仍会更新，不能将生产共享表“前后完全不变”作为隔离证据，应以mock调用边界和隔离对象作因果证明。

## 4. 实施后的服务加载

后端实施改动后必须按项目约定重启；Phase 7已执行，监听和401结果见acceptance.md：

```powershell
Set-Location -LiteralPath 'D:\期货实时交易系统'
.\stop.bat
.\start.bat
netstat -ano | findstr ':8000'
curl.exe -i http://127.0.0.1:8000/api/v1/auth/me
```

期望8000监听且未登录auth/me返回401 JSON。随后Ctrl+F5加载第15脚本。监听和401仅证明服务加载，不能替代模拟器数据质量或功能验收。

## 5. 浏览器端到端（实施后记录证据）

1. 交易中打开数据充分的月份合约，点击行情模拟；检查北京时间、真实报价时间、历史截止、来源/质量及模拟标签。休市不创建新基准，明确说明；不要为验收改系统时间。
2. 不改值和重置，两列相同且差值0；拖动三个参数后显示全部实际采用值、四项阈值及净分差。
3. 用前高一跳、前低一跳、1.8倍突破、0.7倍缩量和±1%按钮核对实际值；不能低于已成交量或越已知停板，无效按钮不更改输入。
4. 真实行情更新时保持同一snapshot和freeze时间；报价过25秒或原小节关闭提示基准已旧。更新基准后清空原比较，旧请求即使返回也不复活。
5. 明确点击已有有效盘时段run，更新基准验证来源及固定目标/止损；无有效run显示无法计算。1320/1270多头样例的1280/1300入场分别4.00/0.67，应在隔离固定数据里验证，不能写生产计划或改现有模型结果。
6. 延迟请求后切合约、切run、退出、关闭重开、连续更新，核对无错对象覆盖；窄屏表格可滚动，计算不跳滚动、不覆盖输入。
7. 使用能核验开盘的来源成功计算一次；实际不完整来源返回CURRENT_BAR_UNVERIFIED。如果真实环境缺乏前者，记录“真实数据可用性未验收”，不得因固定样例通过而宣称线上可用。
8. 记录连续20次输入从停止到可见结果/错误的耗时，至少19次≤1秒；5名熟悉系统的用户至少4人在2分钟内完成并说出一项变化原因。没有开展该验收时保持“待验收”。

## 6. 验收记录

后续记录时间、Git版本、合约/周期/交易日、源时间/冻结截止、来源/质量、实际执行命令、结果和截图位置；区分固定样例、浏览器交互、真实行情可用性及用户性能验收。不得填写未执行的通过结论。

文档检查可使用`git diff --check -- AGENTS.md README.md docs/项目架构与AI导航.md`；新建specs文档另查占位符、相对引用及行尾空白。

## 7. 五项修订的必验矩阵

- U1：按[观测证据判定表](plan.md#当前柱观测证据判定表)逐项验证，合成全通过样例保留PARTIAL标签；缺首分钟、缺原始累计量anchor、累计量不符分别阻断。合成成功不等于真实来源可用，真实记录不足时保持真实验收待完成。
- I1：当前OI/上一柱OI各自缺失以及均缺失；创建和defaults允许未知评分，上一柱OI缺失的覆盖必须422 OI_BASELINE_UNAVAILABLE；仅当前OI缺失而上一柱已知的覆盖可以生成假设OI，不能回填真实列。
- C1：仅用闭合15m ATR14，验证±5ATR、单侧/双侧停板、tick0.5/0.02向内取整、ATR缺失/零/负、交集空/单价位禁用滑块、合法手工价超滑块范围。
- I2：CREATE前失败冻结时间null，已授权有效基准上的计算错误context完整，401/403及失效/越权404不泄露基准；解析前错误用前端本地请求序号识别。
- U2：有效DELETE204、重复DELETE404、过期清理前后均404、跨会话/重启404；关闭对204/404都结束，不保留无限身份记录或限流状态。


## 8. Phase 7 定向回归

`tests.test_market_simulator`新增源记录时间/数值/元数据异常及整数手舍入边界；`tests.test_market_simulator_api`新增真实reader只读存根、全流程副作用失败哨兵、状态码/会话/重启及quality摘要；`node tests/check_market_simulator.cjs`实际执行DOM render、按钮和fetch适配器，覆盖禁用滑块连续渲染、快捷成功后提交草稿、失败上下文与旧结果、参考选择变化/有效窗口、300ms合并输入、切账号、旧响应及204/404释放。DOM存根通过不能替代真实浏览器布局、正式计时或5人验收。当前T046/T054及原4项外部验收保持待完成。
