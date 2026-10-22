# Codex 只读行情接入

实施日期：2026-10-07，北京时间。范围为本机 Codex 按需查询系统现有新浪研究行情。

## 使用方法

在 Codex 中直接说：

- “读取 FU2611 最近 200 根 60 分钟 K 线，结合最近 100 根日线分析趋势。”
- “查询 BU2611 在 20260930 交易日的 1 分钟 K 线。”
- “先搜索燃料油的具体合约，再看成交量、持仓量和规则信号。”
- “查看 FU0 主连历史，和 FU2611 分开分析。”

Codex 应先确认合约代码，再调用工具，分析中注明北京时间、交易日、数据截止时间和质量限制。不要自动将中文品种名解释为某个月份合约，也不能混用 FU、BU 或主连与月份合约。

主服务需启动并监听 `127.0.0.1:8000`。休市可读已保存历史，数据未更新时不会自动制造新 K 线。接口没有对应历史时返回缺失或错误，不会自动新增订阅、回补或下单。

Codex 配置保存后，已经运行的聊天是否能刷新工具由客户端版本决定。若工具列表尚无 `futures_market`，重新启动 Codex 并开始新聊天；重新启动 Codex 不需要重启期货服务。当前项目中的聊天也可先使用下文的本机查询命令。

## 工具清单

| 工具 | 参数/输出 |
| --- | --- |
| `get_futures_bars` | `instrument`、`period`、`limit`、`trading_day`、`closed_only`、`include_provenance`；返回 OHLCV、成交额、持仓、变化、闭合状态和质量 |
| `get_futures_quote` | 指定合约最新快照、原始时间戳、距当前时间、盘中陈旧/休市状态 |
| `get_futures_indicators` | 指定合约/周期的系统指标与报价时间背景；空 VWAP 不转为 0 |
| `get_futures_signal` | 客观规则信号、数值解释、报价时间背景；信号触发时间与报价时间分别看 |
| `search_futures_contracts` | 按代码或中文名称查元数据，附订阅状态，不修改订阅 |
| `get_futures_bar_dates` | 该合约、周期已有的交易日列表 |
| `get_futures_feed_status` | 采集线程状态、休市判定、最近采集时间；不能据此判断每个合约都是实时 |

K 线周期支持 `1m/5m/15m/30m/60m/1d`，最多 1000 根，默认 200 根。交易日参数为 `YYYYMMDD`，夜盘自然日期不一定等于交易日。`closed_only=true` 默认排除形成中柱体；默认输出来源摘要节省模型上下文，`include_provenance=true` 返回上游原有完整来源字段。

## 数据与质量含义

- 价格保留上游原始精度，不转换成浮点后再四舍五入。
- `retrieved_at` 为查询时间，`latest_bar_end_time` 为返回窗口中已闭合 K 线的最大结束时间；两者不能混淆。
- `closure_status` 为 `CLOSED/FORMING`。已知 `END` 时间标签按终点闭合；`START` 按周期及小节结束时间闭合；未知标签按起点保守估计并警告。日线使用来源关闭时间或交易日收盘时间。
- `freshness=MARKET_CLOSED` 表示当前不在该合约交易时段，不能据此声称行情更新。盘中数据过旧会标 `STALE`。
- `quality.missing_labels` 只检查返回窗口内相同时间标签口径的连续小节，以及 2026 年日历内的日线缺失；正常休息不算缺口，不覆盖跨小节和全部源端历史。空列表不证明连续性或来源完整。
- `coverage_status=UNVERIFIED` 保留来源覆盖未全面核实的边界。时间标签混用、未知来源、源端字段缺失、年度日历超范围分别警告。
- 原系统一些 MySQL 行情响应未携带来源字段时，适配器保留 UNKNOWN；不能倒填来源。当前工具不修改后端或历史库。
- 合约、周期、交易日不一致，OHLC、成交量、持仓或成交额存在非法/非有限数值时拒绝返回分析数据。
- 2026 年之外的日历未在此接入中新增核验。未来年份的交易时段/日历判断不能等同于已核验的全年交易所日历。

## 代码与运行关系

```text
Codex
  └─ MCP stdio 子进程：.venv-mcp/Scripts/python.exe codex_mcp/server.py
       └─ MarketClient：固定的本机 GET 行情接口
            └─ 原 FastAPI 服务：run.py → backend.main:app
                 └─ Redis 近端行情 + MySQL 历史
```

- `codex_mcp/server.py`：MCP SDK 注册 7 个工具、生成 schema、处理 stdio 协议。
- `codex_mcp/market_client.py`：HTTP 请求、参数/合约校验、时间与闭合判定、局部缺口与数据状态。
- `codex_mcp/requirements.txt`：独立 SDK 依赖，固定 `mcp==1.30.0`。
- `.venv-mcp/`：独立虚拟环境，已加入 `.gitignore`；后端 `.venv/` 和 `requirements.txt` 不变。
- `tests/test_codex_mcp.py`：本机 HTTP fixture 核心契约测试，不需要数据库、CTP 或公网。
- `tests/test_codex_mcp_protocol.py`：真实 MCP 子进程握手、schema 与调用测试，HTTP 指向测试 fixture。

适配器只允许带端口的 loopback HTTP 地址、关闭代理并拒绝重定向，只调用固定行情 GET 路由。它不读取账号密码或模型密钥、不加载交易引擎、不提供任意 URL/SQL/命令工具。已有指标 GET 可能维护系统指标缓存，不属于业务交易写入。

Codex 启动 MCP 时工作目录可以是其他项目，入口会按自身文件位置定位本项目。期货服务与 MCP 分开启动/停止；MCP 不自动启动期货服务。

## 安装与配置

在项目根目录执行：

```powershell
.venv\Scripts\python.exe -m venv .venv-mcp
.venv-mcp\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -r codex_mcp\requirements.txt
codex mcp add futures_market --env PYTHONUTF8=1 --env PYTHONIOENCODING=utf-8 --env FUTURES_MCP_BASE_URL=http://127.0.0.1:8000 -- "D:\期货实时交易系统\.venv-mcp\Scripts\python.exe" "D:\期货实时交易系统\codex_mcp\server.py"
codex mcp get futures_market --json
```

若 PyPI 下载较慢，可使用已核验可访问的 Python 包镜像安装同一固定版本，不修改全局 pip 源、不关闭 TLS 校验。

Codex 全局配置位于 `C:\Users\haowe\.codex\config.toml`，接入项名称为 `futures_market`，可在其他本机 Codex 聊天中使用。修改前的配置副本保存在项目被忽略的 `backups/codex-mcp-config-before.toml`，其中可能包含原配置的敏感值，只在本机保留。

仅移除此接入项：

```powershell
codex mcp remove futures_market
```

不要用整份旧配置覆盖后来新增的 Codex 设置。

## 当前聊天的本机查询入口

尚未刷新 MCP 工具的当前项目聊天可运行：

```powershell
.venv\Scripts\python.exe -c "from codex_mcp.market_client import MarketClient; import json; print(json.dumps(MarketClient().bars('FU2611', '60m', 200), ensure_ascii=False))"
```

与 MCP 工具使用同一适配器和质量规则，只读本机接口，不依赖 MCP SDK。

## 验证命令

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codex_mcp
.venv-mcp\Scripts\python.exe -m unittest tests.test_codex_mcp tests.test_codex_mcp_protocol
.venv-mcp\Scripts\python.exe scripts\verify_codex_mcp.py
```

最后一项通过真实 MCP 客户端启动适配器，读取本机现有数据并保存证据；不会提交模型任务、交易指令、订阅或回补。服务启动本身仍按系统既有设置运行后台采集。

验收记录见 `docs/evidence/2026-10-07-codex-mcp/acceptance.json`。本机 HTTP、MCP 协议和配置读回是不同验证层；未刷新工具的已有聊天不算已实际调用新 MCP 工具。
