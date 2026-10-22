# 国内期货实时分析系统 (Futures Realtime Analysis System)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

面向国内商品期货的行情研究与模拟交易项目，提供多周期 K 线、可追溯规则信号、AI 证据冻结、公开资料研究及独立模拟验证账本。当前开发重点为新浪行情与模拟盘；项目不隶属于任何期货公司，不构成投资建议，不承诺收益，实盘模块冻结且未完成真实柜台验收。

这是首次公开源码快照，未将私有开发仓库的提交历史带入公开版。项目使用量、社区采用程度和外部验收尚无独立统计；公开代码不代表获得任何活动资格或官方认可。

## 开源与参与

- 原创代码采用 [MIT 许可证](LICENSE)，依赖、行情数据和厂商接口的权利见 [第三方声明](THIRD_PARTY_NOTICES.md)。
- 报告问题或提交修改请阅读 [贡献指南](CONTRIBUTING.md)；敏感漏洞请阅读 [安全反馈](SECURITY.md)。
- 公开版不包含厂商 SDK、账户密钥、数据库、运行原文和完整私有历史。引用的历史证据可能仅保留在维护者本机。
- `.specify/feature.json` 是按工作副本生成的当前功能指针，不随源码分发；首次使用 Spec Kit 时选择 `specs/001-market-simulator`，不要把缺失的本机指针误判为功能规格缺失。

## Windows 首次运行

准备 Python 3.11、MySQL 和 Redis，在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config/settings.example.yaml config/settings.yaml
```

按本机环境修改 `config/settings.yaml` 的数据库地址，保留 `system.feed: sina`。在根目录本地创建 `.env`，填写自己的 `FUTURES_MYSQL_PASSWORD`、`FUTURES_SESSION_SECRET`，不要提交这些文件。会话密钥可在本机通过 Python `secrets.token_urlsafe(32)` 生成。MySQL 和 Redis 应已安装并配置为对应本机服务，或配置为可连接的远程服务；`start.bat` 不会安装依赖。

```powershell
.\.venv\Scripts\python.exe scripts/create_user.py add researcher sim
.\start.bat
```

按提示设置自己的登录密码，启动后访问 `http://127.0.0.1:8000`。首次初始化、运行依赖和健康核对见下文及 [AGENTS.md](AGENTS.md)。本段是运行指南，不代表在全新机器上完成了安装验收；AI 功能需另配模型，启用分析会产生相应费用。

项目结构与 AI 快速定位：[项目架构与 AI 导航](docs/项目架构与AI导航.md)。开发范围、CTP 冻结和服务重启约定见 [AGENTS.md](AGENTS.md)。

在 Codex 聊天中按合约、周期、根数查询 K 线：[Codex 只读行情接入](docs/Codex只读行情接入.md)。

行情模拟器 V1：[需求规格](specs/001-market-simulator/spec.md)、[技术方案](specs/001-market-simulator/plan.md)、[实施任务](specs/001-market-simulator/tasks.md)、[质量清单](specs/001-market-simulator/checklists/requirements.md)。当前已增加“行情模拟”抽屉和隔离计算接口，含Phase 8/9的64项任务，原始边界、历史时间语义及成交量归属修复的实际验收情况见[验收记录](specs/001-market-simulator/acceptance.md)；方案同目录包含研究、数据模型、接口契约和验证指南，当前功能由 `.specify/feature.json` 定位，五项一致性问题已按授权修订并复核，见[修订记录](specs/001-market-simulator/checklists/analysis-remediation.md)；固定样例测试与真实行情可用性分别验收；缺少原始观测起点时显示阻断原因。成交量跨采集缺口时显示未知，不削峰或猜量；实际修复与审计见[成交量归属记录](docs/audits/2026-10-21-FU2701-volume-gap.md)。

AI 盘时段独立模拟验证：[使用与验收说明](docs/2026-10-08-AI模拟验证使用与验收.md)。模拟用户在工作台右侧点击“模拟验证”，填写实际手续费、滑点和模型价格，再点击“创建并启动”；随后按冻结规则运行 20 个实际交易日：每小节开盘 15 分钟后开始，有方向返回即继续，无明确方向等 5 分钟，发起满 10 分钟未返回作废重试，收盘停止。打开面板只读取报告，启动后才自动调用模型并产生费用，账本与现有模拟交易账户独立。

基于 **Python 3.11 + 免费实时行情接口 + Redis + MySQL 8.0 + FastAPI + TradingView Lightweight Charts** 的国内商品期货多周期实时行情分析与客观规则信号系统。

---

## 1. 核心架构与功能

1. **多行情源自适应接入** (`system.feed` 配置项):
   - `sina` (默认): 新浪财经免费实时快照批量轮询 (~0.8s), 覆盖沪深大郑能源广六大商品交易所; 东方财富接口同步全市场约 1000+ 活跃合约列表 (每 5 分钟刷新, 带镜像域名轮换)。
   - `simulator`: 内置高保真模拟行情发生器, 用于开发与演示。
   - `ctp`: 中信期货 CTP 柜台行情 (预留, 需要仿真/实盘账号时启用)。
   - 订阅列表 (`watchlist`) 保存在 Redis, 支持运行时通过 API/页面动态增删, 新订阅合约自动回补历史 K 线。
2. **历史数据自动回补**:
   - 日线为合约自上市以来全历史; 1m 约最近 3 个交易日、5m 约 3 周、15m 约 2 个月、30m 约 4 个月、60m 约 8 个月 (受免费数据源深度限制)。
   - 幂等写入 MySQL (ON DUPLICATE KEY UPDATE), 可随时重复执行; 触发方式: 启动自动补全 / 页面订阅新合约 / 管理接口 / `scripts/import_history.py`。
3. **多周期 K 线实时引擎**:
   - 支持 `1m / 5m / 15m / 30m / 60m / 1d` 实时与跨交易日合成。
   - **日线 (1d) 跨交易日缝合机制**: 夜盘 (21:00) 与次日白盘 (15:00) 归集于同一期货交易日。
   - K 线查询自动合并 MySQL 历史与 Redis 实时近端, 页面可完整展示历史行情。
4. **客观技术指标与规则信号引擎**:
   - 计算 EMA(5, 10, 20, 60)、ATR(14)、VWAP、20周期均量与前高前低、开盘区间。VWAP 限同一交易日；新浪数据缺少可核实成交额时显示“成交额数据不足”。
   - 提供多头观察 (`LONG_WATCH`)、空头观察 (`SHORT_WATCH`)、持仓异动 (`OI_SURGE`) 规则。
   - 所有信号严格给出客观数值追溯 (如放量倍数、突破点位、增仓数量), 杜绝主观臆测与虚假胜率。
5. **登录与角色权限 (双交易页面)**:
   - 用户清单存于 `config/users.yaml` (PBKDF2 密码哈希), 管理命令: `python scripts/create_user.py add/passwd/drop/list`。
   - 登录页提供两个入口: **模拟用户登录**(进入 `/paper` 模拟交易页) 与 **实盘交易登录**(进入 `/live` 实盘交易页); 后端校验入口与账号角色一致性, 不匹配则拒绝 (如模拟账号走实盘入口)。
   - 角色权限: `sim` 模拟用户仅可模拟交易 (访问 `/live` 会被弹回 `/paper`, 实盘 API 返回 403); `live` 正式用户可模拟 + 实盘交易; `admin` 另含系统维护权限。
   - 会话为 HttpOnly Cookie + HMAC 签名令牌 (7 天有效), 未登录访问任何交易页自动跳转 `/login`。
   - 实盘报单经过双重门禁: 服务端角色校验 (403) + CTP 柜台就绪门禁 (未连接时页面按钮禁用、接口一律拒绝, 绝不误发)。
6. **实时网页监控面板 (Web UI)**:
   - 适配 PC 与手机端响应式布局; WebSocket 实时推送 Tick、K 线闭合与系统事件。
   - 全市场合约搜索订阅 (约 1000+ 合约)、行情源状态与休市提示、模拟盘账户与成交记录展示。
   - 首页右侧 AI 分析结果每 2 秒自动读取更新，包含后台模拟验证生成的分析；保留滚动和图表选择，隐藏页面暂停读取。
7. **模拟盘交易 (Paper Trading)**:
   - 限价报单/撤单/一键全平、条件止损止盈、信号自动跟单 (默认关闭)。
   - 按用户名隔离账户、委托、条件单、配置及资金曲线；账户状态同步写入 MySQL `trading_state`，启动时恢复。
   - 挂单纳入持仓上限；拒绝非正价格；浮动盈亏计入可用资金，日亏和撤单计数按交易日重置。
   - 事前硬风控: 单笔手数、持仓上限、保证金充足性、自成交防范、撤单频次、日亏 5% 熔断。
8. **实盘交易通道 (仅正式用户)**:
   - `POST /api/v1/trade/live/order` 走 CTP 柜台, 需 `live`/`admin` 角色登录。
   - 报单门禁: 角色校验、默认弱密码拦截、结算确认、资金/持仓/委托/成交对账；不确定报单不自动重发。
   - CTP 柜台在 `system.feed: ctp` 时启用; 已实现委托/成交回报持久化、结算确认及启动/重连对账；真实柜台验收尚未完成。
9. **ChatGPT / Codex 只读调用工具 (Tools API)**:
   - 提供符合 OpenAI Function Calling 标准的 Manifest 清单。
   - 端点: `get_futures_quote`, `get_futures_bars`, `get_futures_signal`, `explain_futures_signal`。
10. **生产与容器化部署**:
   - 完整 `Dockerfile` 与 `docker-compose.yml`, 一键拉起 FastAPI、Redis、MySQL 8.0。

---

## 2. 快速上手

Windows 桌面控制台：双击项目根目录的 `桌面控制台.bat`，可查看本机服务状态和日志、启动/停止/重启服务及打开网页。点击右上角 X 会收起到右下角通知区域，单击托盘图标可恢复；点击“退出控制台”才会关闭窗口，服务继续运行。原有 `start.bat`、`stop.bat` 仍可单独使用。

Windows 启动时自动检查配置的 MySQL、Redis 端口；本机依赖未就绪时尝试启动同名 Windows 服务，必要时弹出管理员授权提示，请允许。取消授权、服务未安装或启动超时会终止启动并显示原因；远程数据库配置只检查连接，不启动本机服务。桌面控制台启动等待最多 420 秒，详细信息见日志；批处理的“已发起启动”不代表服务已经就绪。

### 2.0 敏感配置（必读）

密码、令牌与签名密钥**不再写入 `settings.yaml`**，统一放在项目根目录的 `.env`（已被 `.gitignore` 忽略）：

| 环境变量 | 用途 | 必填 |
|---|---|---|
| `FUTURES_MYSQL_PASSWORD` | MySQL 密码 | 是 |
| `FUTURES_CTP_USER_ID` / `FUTURES_CTP_PASSWORD` | CTP 资金账号/密码 | 启用 ctp 时 |
| `FUTURES_MAINTENANCE_TOKEN` | 维护接口 `X-Admin-Token` | 用维护接口时 |
| `FUTURES_SESSION_SECRET` | 会话 HMAC 签名密钥 | 建议（不设则回退 `config/session_secret.key`） |
| `FUTURES_CORS_ORIGINS` | 允许跨域来源，逗号分隔 | 非本机访问时 |

环境变量优先级最高，可覆盖 `settings.yaml` 中同名项。首次可复制 `.env` 模板并按需填写：

```powershell
# 生成一个强维护令牌
& ".\.venv\Scripts\python.exe" -c "import secrets;print(secrets.token_urlsafe(24))"
```

**注意**：`maintenance_token` 留空时，维护类接口（`/api/v1/maintenance/*`）一律返回 503 拒绝（fail-closed），不会再回落到一个公开的默认令牌。

### 2.1 本地虚拟环境运行 (Windows)

```powershell
# 1. 激活 Python 3.11 虚拟环境
.\.venv\Scripts\Activate.ps1

# 2. 启动服务 (包含 Web UI 与后台行情流)
.\.venv\Scripts\uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

打开浏览器访问：`http://127.0.0.1:8000`

> 若从非本机地址访问，需在 `.env` 中把该地址加入 `FUTURES_CORS_ORIGINS`，否则跨域请求会被拒绝。

### 2.2 单元测试套件 (独立随机 MySQL 测试库与空闲 Redis 库)

```powershell
& ".\.venv\Scripts\python.exe" -m unittest discover -s tests
```

### 2.3 历史数据回补 (可选命令行)

```powershell
& ".\.venv\Scripts\python.exe" scripts\import_history.py               # 回补配置文件关注合约
& ".\.venv\Scripts\python.exe" scripts\import_history.py JM2701 SA501  # 指定合约
```

---

## 3. Docker 容器化部署

```bash
docker-compose up -d --build
```

---

## 4. API 路由参考

- `GET /` : 按角色分流入口 (模拟用户 → `/paper`, 正式用户 → `/live`, 未登录 → `/login`)
- `GET /login` : 登录页面
- `GET /paper` : 模拟交易页面 (已登录用户可用)
- `GET /live` : 实盘交易页面 (仅 live/admin, 模拟用户自动弹回 `/paper`)
- `POST /api/v1/auth/login` {"username","password"} : 登录 (设置会话 Cookie)
- `POST /api/v1/auth/logout` : 退出登录
- `GET /api/v1/auth/me` : 当前用户与角色
- `GET /api/v1/health` : 系统健康检查（MySQL 与 Redis 联通性）
- `GET /api/v1/instruments` : 监控合约清单
- `GET /api/v1/universe?search=&exchange=&limit=&offset=` : 全市场活跃合约搜索 (约 1000+)
- `GET /api/v1/watchlist` : 当前实时订阅列表
- `POST /api/v1/watchlist` {"symbol": "SA501"} : 订阅合约 (自动回补历史)
- `DELETE /api/v1/watchlist/{symbol}` : 取消订阅
- `GET /api/v1/feed-status` : 行情源运行状态 (类型/延迟/休市判定)
- `GET /api/v1/quotes/{instrument}` : 最新 Tick 行情快照
- `GET /api/v1/bars/{instrument}?period=15m&limit=200` : 多周期 K 线 (自动合并历史与实时)
- `GET /api/v1/indicators/{instrument}?period=15m` : 关键技术指标与支撑压力位
- `GET /api/v1/signals/{instrument}` : 客观规则信号与评分
- `GET /api/v1/signals/{instrument}/explanation` : 规则条件追溯依据与数值详情
- `GET /api/v1/events?limit=20` : 系统事件与告警日志
- `POST /api/v1/maintenance/backfill` {"symbol": "JM2701"} : 手动回补历史 (需 X-Admin-Token)
- `GET /api/v1/tools/manifest` : ChatGPT / Codex 工具清单与参数定义 (需登录)
- `WS  /api/v1/ws/market` : WebSocket 实时全量数据流广播

> `/api/v1/tools/*` 四个只读工具端点已统一要求登录态（与其余业务接口一致），不再匿名开放。

## 2026-09-13 修复与验收

详细记录见 [开发修复验收记录](docs/2026-09-13-修复验收.md)。当前采用单进程部署；真实 CTP 交易仍需柜台与交易时段验收，不能用本地测试代替。

修改本机账号密码（交互输入，密码不写进命令行）：

```powershell
& ".\.venv\Scripts\python.exe" scripts\create_user.py passwd live
```

默认弱密码账号仍可登录查看状态，但禁止实盘报单。账户配置文件和 CTP 凭据未在本轮修改。
