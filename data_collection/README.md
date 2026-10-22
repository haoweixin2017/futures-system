# 仓单与每日主力行情采集

独立于现有后端，当前交付默认关闭线上来源。实现仓单/行情分开采集、三表落库、离线解析、校验拒绝、断点恢复、幂等替换和定时轮询。

**现有 DCE 研究通道已验证 JD/LH 官方仓单；本模块沿用同一官方协议和真实响应样例，尚未执行新的线上采集验收。郑商所、CFMMC 自动下载和第三方日行情入口尚未核实，不能把离线测试称为已接通官网。**

## 目录

```text
data_collection/
  config.example.yaml       # 默认关闭线上；复制成被忽略的config.yaml
  config.py                 # 配置、交易日历、合约映射
  cli.py                    # run/backfill/self-check/schedule
  scheduler.py              # 日期范围、恢复、更新轮询、通道隔离
  transport.py              # robots、限流、重试、响应上限、告警
  storage.py                # SQLite幂等写入、来源合并、日志
  validators.py             # 勾稽、参考对账、非负/3σ、单位转换
  collectors/
    base.py                 # fetch/parse/validate/upsert接口
    czce_receipt.py
    dce_receipt.py
    market.py               # 可配置第三方JSON日行情，默认未启用
  parsers/
    common.py               # 配置选择器和表格字段、总计层级校验
    czce.py
    dce.py                  # 官方JSON优先、HTML备用
  db/schema.sql             # 主表、采集日志表、品种映射表
  tests/fixtures/           # 官方DCE JSON + 明确标注的合成HTML/日历/行情
  tests/test_pipeline.py
  tests/test_runtime.py
  requirements.txt
  acceptance.md
```

## 部署与首次运行

在项目根目录执行，所有相对配置文件路径相对于 YAML 所在目录。独立程序不由现有 `start.bat` 启动，也不会修改现有资料任务或生产数据库。

```powershell
.venv\Scripts\python.exe -m pip install -r data_collection/requirements.txt
Copy-Item data_collection/config.example.yaml data_collection/config.yaml
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/config.yaml self-check
```

默认自检输出 `DISABLED`，表示线上通道未开启，不表示官网验收通过。先用离线配置练习：

```powershell
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/tests/fixtures/demo.yaml self-check
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/tests/fixtures/demo.yaml run --date 2026-09-30 --dry-run
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/tests/fixtures/demo.yaml run --date 2026-09-30
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/tests/fixtures/demo.yaml run --date 2026-09-30
```

离线演示写入独立且被忽略的 `data_collection/runtime/demo.sqlite3`，不写现有服务库。第二次输出 `SKIPPED_COMPLETE`。加 `--force` 会重新读取，幂等替换同日仓单快照，清除已撤回的明细；行情字段保留。

`--dry-run` 输出行数、校验状态与错误，不改主表、映射表或日志表；有既有库时只读取其历史。线上 dry-run 仍会请求来源并更新独立限流状态文件，不发送告警。退出码：0无失败，1通道失败/拒绝，2配置/自检失败。

## 配置正式来源

1. 提供交易所公布的完整交易日清单 YAML：`source` 填原文链接或本地证据路径，`fixture_only: false`，明确 `start/end/dates`，日期用引号。范围之外报错，不猜测周一至周五都是交易日。样例日历只用于离线测试，线上模式会拒绝。
2. DCE：配置中已有此前核实的 `/dceapi` 地址、鉴权请求和仓单请求。`DCE_API_KEY`、`DCE_API_SECRET` 引用环境变量，也可使用项目被忽略的 `.env`。本模块不导入 CTP 或启动服务。只启用已有样例验证的 JD/LH；扩展品种前保存对应真实响应并增加解析测试。HTTP为该官方接口已核实的例外，其开关单独配置，其他源默认要求HTTPS。
3. CZCE：由用户核实官网仓单日报链接并保存一份正常正文；更新 `base_url/request`、日期选择器、表格选择器、列映射和原始单位。当前 `czce.html` 是合成样例，配置中的选择器未被证实适用于官网，不要直接开启。
4. 日行情：`market.enabled` 独立于仓单开关，填写第三方每日行情请求和JSON字段映射。上游必须给出明确主力标识；重复主力、日期不符、合约品种不符会拒绝，不用任意合约猜主力。行情源鉴权可直接在配置请求头引用环境变量。
5. 对账：将已核验 CFMMC 同日期、同品种总量保存为 `validation.reference_file` 指向的JSON，程序自动匹配校验。当前未实现未经核实的 CFMMC 网页下载。没有参考值输出 `UNVERIFIED`；若必须对账后入库，设 `require_reference: true`，缺参考值也拒绝。
6. 告警：设置配置所指环境变量 `RECEIPT_ALERT_WEBHOOK`。发送通用JSON，需接收端支持此格式；程序只发送日期、交易所、品种、通道、错误码与校验结果。没有配置或发送失败也会落日志。

参考值格式（下面仅演示字段，不是真实CFMMC数据）：

```json
[{"trade_date":"2026-09-30","exchange":"CZCE","variety":"SR","unit":"吨","value":120,"source":"已核实的原文链接"}]
```

## 校验和单位口径

- 每份报告要求一个品种 `TOTAL`。`GROUP`、`DETAIL` 和 `MARKET` 全部保留，但不与TOTAL相加。解析阶段检查集团/分库、总计/顶层之间的三个数量字段，以及每行今日=昨日+增减。缺行、空值、占位符不当成0，官方明确0有效。
- 跨日用日历中的上一交易日总量，检查今日总量=前日总量+当日增减；首次缺前值显示 `UNVERIFIED`。`require_previous: true` 时拒绝缺前值；先导入可核验的基准日再开启此项。
- 对账仅接受同日期/交易所/品种的参考值，保留来源。原单位不一致时只使用已可靠换算的吨值，否则拒绝对账。
- 非负检查昨日/今日数量；负增减合法。3σ使用此前最多60个已入库交易日总量（同原始单位），满 `min_history` 个才判断；常量历史中非零偏离也打标。阈值和最少样本数均在配置里。
- 失败整份品种报告标记 `REJECTED`，错误和校验结果落日志并告警，不向主表写空行/脏行。其他通道继续。
- `warr_yesterday/today/chg` 保留原始数值，`unit_raw` 保留原单位；对应的 `_ton` 列保存可靠换算结果，`unit_ton`为“吨”或NULL。只有单位匹配、正换算系数和证据依据齐全才换算。
- DCE JD/LH 的“手”暂不猜测每手吨数；待官方仓单换算依据确认后配置。原油桶也不能套一个通用密度。`conversion_basis`空或不可靠时吨值为NULL。
- 未公开的仓库类型、等级或转换仓单标志保持空/NULL。DCE原文中的1/2用于集团层级判断，不直接猜作仓库/厂库。`raw_record`保留所有原始行字段，补映射时不丢信息。
- CZCE三位年月码按交易日期的相邻十年解析，距离相同的年份拒绝；DCE四位码按相邻百年解析。解析出的完整年/月另存，不把SR601当成统一六位合约。

## 存储与断点恢复

业务库采用 SQLite 3.24+，当前未实现MySQL后端。主表主键为 `(trade_date, exchange, variety, detail_id)`，表结构见 `db/schema.sql`。品种总计用 `TOTAL`，独立行情行用 `__MARKET__`，行情先到时也能独立入库；随后仓单会合并行情字段。

仓单及行情各自记录 `receipt_source/market_source`、加载时间，行情复制到明细便于查询，**计算全市场成交量或持仓时仅取 `row_type='MARKET'`，不能把所有明细行的行情相加。** 聚合仓单仅取TOTAL。

成功任务会记录检查点；进程崩溃后重跑，同日同通道已成功项跳过，失败项继续。写入已完成但成功日志尚未写下就中断时，重跑仍幂等。改来源/解析器/换算规则或需刷新同日修订时用 `--force`，不会依靠新增重复行保存修订版本。

```powershell
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/config.yaml run --range 2026-09-01 2026-09-30 --exchange DCE
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/config.yaml backfill --range 2026-09-01 2026-09-30 --exchange DCE --dry-run
```

`backfill`使用相同日历和幂等链路，不包含猜测历史入口、补十年数据或商用数据库下载。大跨度补历史可导入经授权的 CnOpenData/Wind/iFinD 数据，须另写对应来源适配和校验。

## 调度与请求约束

```powershell
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/config.yaml schedule
```

常驻进程按北京时间每天 `schedule.time` 触发。向前查看 `lookback_days` 范围内的未完成交易日，假日不新增任务。失败任务每 `poll_seconds` 重查，最多 `max_polls` 轮；已成功任务跳过。耗尽后记录 `POLL_EXHAUSTED`并告警，次日仍会检查范围内未完成项。Windows可用任务计划程序启动此常驻命令；不要同时再创建另一组循环cron。

日历覆盖过期会记录 `SCHEDULE_FAILED`并告警，其他交易所继续。必须补齐官方交易日清单后才能恢复对应交易所；程序不会自行把未知日期当作交易日。

单个请求另有 `network.attempts` 次网络重试和指数退避，429遵守Retry-After。robots拒绝、跳转、页面格式错误不当成功；robots无法取得时不绕过。robots 404表示没有规则文件。响应最多配置的字节数，固定User-Agent，默认不跟随重定向、不使用系统代理。

同一主机请求间隔取8秒、配置间隔、每分钟上限换算间隔、robots要求中的最大值。`network.rate_state`使用独立SQLite运行状态保存上次请求时间并串行更新，多个使用相同状态路径的进程也共享间隔；离线fixture不会创建它。**其他已有采集程序不共享此状态，正式开启前需统一调度或停用重复官方采集。** 调低robots限制前需重新核实规则并清理该状态，不能用频繁清状态绕过限制。

## 更换解析器与已知坑

保存新的真实HTML/JSON到本地fixture，先调整YAML选择器、日期格式、字段和单位映射，再运行 `self-check` 和pytest。若只是列顺序变化，不需要改Python；若出现多级表头、跨行单元格或全新层级，再修改对应解析器并增加真实样例测试。当前HTML解析器要求每个数据行包含完整列，不会猜测合并单元格的继承值。

启动配置检查和离线fixture检查不发网络请求。某个通道的fixture或选择器失效时，该通道记录失败、不请求线上；其余通道继续。线上返回的新结构失效同样记失败，CLI退出码1，不静默写空记录。自检通过仅说明已保存样例可解析，不能预知线上明天是否改版。

合成样例、历史统计月份、没有权限的登录页面、API200外壳、空列表都不能冒充实际有效数据。仓单与实时行情是两条独立通道，CTP/易盛行情不含仓单，本模块不改CTP。

仅供个人研究与内部使用，不提供对外API或再分发；程序化交易需按适用规则报备，并遵守主体资格与频率限制。本模块只采集数据，不执行交易。
