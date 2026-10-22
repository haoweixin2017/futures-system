# 仓单与日行情采集验收记录

日期：2026-10-06。范围：独立 `data_collection/` 模块，不修改现有后端、CTP 或服务业务库。本次所有解析与网络故障测试使用本地 fixture / MockTransport，没有请求交易所官网。

## 已验证

| 项目 | 结果与证据 |
| --- | --- |
| 独立测试 | `python -m pytest data_collection/tests -q`：44 passed，包含解析、单位、层级、勾稽、对账拒绝、3σ、网络故障、robots、限流、调度和通道隔离 |
| 既有资料模块回归 | 12 个 unittest 模块共237项，全部通过；未运行CTP测试 |
| 离线自检 | CZCE仓单SR、行情SR、DCE仓单JD/LH均 `PASS_OFFLINE`；DCE行情禁用 |
| dry-run | 输出行数和校验状态；首次运行未创建业务库或限流状态库；已有库不改行/日志也有测试覆盖 |
| 实际演示库写入 | 三张业务表；主表13行，CZCE仓单3行、独立行情1行、DCE JD5行、LH4行 |
| 重复运行 | 四项成功检查点均跳过，主表仍13行 |
| 强制重采 | 四项再次成功，幂等替换，主表仍13行 |
| 来源与单位 | 仓单和行情来源分别保存；SR总计合并SR701演示行情；JD/LH原单位“手”，吨值NULL |
| 故障恢复 | 断网、超时、429均测试恢复及重试耗尽；最终失败落日志；一个通道失败不阻止其他通道入库 |
| 更新轮询 | 模拟报告日期未更新后更新；轮询耗尽落日志；日历过期落调度失败日志并继续其他交易所 |
| 请求约束 | robots拒绝不取正文；crawl-delay作用于重试；跨客户端实例持久限流；跳转不跟随，凭据不进入日志 |

机器可读CLI证据：`../docs/evidence/2026-10-06-receipt-pipeline/cli-acceptance.json`。同目录保存各次自检、dry-run、写入、重复和强制运行的完整JSON输出。演示数据库在被忽略的 `runtime/demo.sqlite3`，不属于服务库。

新模块测试输出见同目录 `pytest.txt`；既有回归结果与命令见 `regression.txt`。

## 验收边界

- 默认示例的线上开关全部关闭，`DISABLED`不等于线上自检通过。
- DCE使用既有官方真实JSON作为解析样例，沿用此前已核实的官方协议。本次没有对新模块进行真实联网、token有效期或长期限流验收，也未验证更多品种。
- CZCE HTML、日历与行情fixture是明确标注的合成样例。尚需正常官方正文、真实选择器与正式交易日历才能开启线上采集。
- CFMMC对账规则已实现并测试；参考值由已核验的本地JSON提供，自动下载来源尚未确认。缺参考显示 `UNVERIFIED`，不能称对账通过。
- 日行情适配器已实现并测试，真实第三方来源尚未配置。主力标识必须来自明确规则或上游字段。
- 无可靠单位换算依据时保留原值，吨值为空。未公开的仓库类型、等级、转换仓单标识不能据层级编码推测。
- 首次无前日值显示 `UNVERIFIED`，3σ历史不足显示 `INSUFFICIENT_HISTORY`。本次演示入库不代表所有校验已有完整数据；可在正式配置开启严格前值/对账要求。
- 当前存储实现为SQLite，未实现MySQL适配；未接入现有页面，未做浏览器验收或长期常驻运行验收。
- 告警调用及失败落日志有覆盖；真实webhook未配置和外部投递未验收。
- 正式启用前需协调旧的交易所采集任务，共用限流状态或统一调度，避免两套程序重复请求。

## 复验命令

在项目根目录运行：

```powershell
.venv\Scripts\python.exe -m pytest data_collection/tests -q
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/tests/fixtures/demo.yaml self-check
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/tests/fixtures/demo.yaml run --date 2026-09-30 --dry-run
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/tests/fixtures/demo.yaml run --date 2026-09-30
.venv\Scripts\python.exe -m data_collection.cli --config data_collection/tests/fixtures/demo.yaml run --date 2026-09-30 --force
```
