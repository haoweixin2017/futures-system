# 定时公开资料采集 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans task-by-task with test-driven-development and verification-before-completion. Independent adapters may be delegated; preserve existing working-tree changes.

**Goal:** 按已确认方案实现用户独立的定时资料采集、直接关联配置，以及完全不现场搜索的未来趋势预测。

**Architecture:** 三张新表保存配置、任务和不可变资料版本。独立单线程调度器认领数据库队列，复用受限研究工具；预测提交时冻结当前有效资料及关联行情，前端沿用现有折叠展示。

**Tech Stack:** Python / FastAPI / SQLAlchemy / MySQL / Redis / unittest；原生 HTML/JavaScript。

**Spec:** `docs/superpowers/specs/2026-10-02-scheduled-public-research-design.md`

## Global Constraints

- CTP、实盘下单、自动交易、全市场爬取、自动发现关联、自动换月和其他分析类型不在本次设计范围内。
- 初始关闭；间隔15/30/60/120分钟；直接关联0至5个，禁止自身和未知合约。
- 每主题90秒、3轮研究、8次搜索、12次读取；宏观公共共用，品种归并。
- 基本面14自然日，事件/相关市场7自然日；宏观核验不得超过2倍间隔。
- 外部有效来源最多24条、每维度6条、每摘录2000字符；关联最多60根闭合60分钟K线，额外前值基准。
- owner隔离、配置版本409冲突、任务租约、重启中断、不补跑遗漏轮次。
- 后端改动后执行stop.bat/start.bat，验证8000监听及auth/me的401 JSON。

### Task 1: 持久化配置与资料选择

**Files:** `backend/models/database.py`, `backend/ai_analysis/research_cache.py`, `migrations/versions/20261002_scheduled_research.py`, `tests/test_scheduled_research.py`

**Interfaces:** `get_settings(owner)`, `save_settings(owner, data)`, `freeze_materials(owner, instrument, cutoff, targets=None)`, `get_status(owner, instrument=None)`, `get_material(owner, material_id)`；设置结构为`{enabled, model_config_id, interval_minutes, contracts:[{instrument, related:[]}], config_version}`。

- [x] 先写并运行失败测试：FU2611/FU2612归并，方向关联、并发版本冲突、未知代码、自身及第6个关联拒绝、用户隔离。
- [x] 新增配置/任务/资料表；任务`active_key`在排队/运行期唯一；资料`owner+url_hash+content_hash`唯一。新增幂等迁移。
- [x] 配置条件更新，主题集合为`macro, product:FU, product:SC, product:BU`。冻结只选已发布任务，筛选发布时间/版本获取时间/期限/时效，合并旧有效资料，限额排序和截断计数。
- [x] 运行`.venv\Scripts\python.exe -m unittest tests.test_scheduled_research`。

### Task 2: 明确主题的研究工具

**Files:** `backend/ai_analysis/web_research.py`, `tests/test_web_research.py`

**Interfaces:** 扩展`run_research(..., topic=None, dimensions=None)`；资料保留`body, content_hash, macro_series, latest_release, latest_verified_at, product_code, applicable_contracts`及实际工具日志。

- [x] 先写失败测试：宏观主题不能调用品种维度，品种不能研究宏观，正文改动但摘录相同仍有不同hash。
- [x] 限定工具schema/提示词/补查范围；完整正文先hash再摘录，宏观序列核验保存时间；不支持工具明确暂停，无静默换模型。
- [x] 运行`.venv\Scripts\python.exe -m unittest tests.test_web_research`。

### Task 3: 调度与发布

**Files:** `backend/ai_analysis/research_scheduler.py`, `backend/main.py`, `tests/test_scheduled_research.py`

**Interfaces:** `enqueue_refresh(owner, instrument=None)`, `tick(now=None)`, `run_job(job_id)`, `recover_interrupted()`, `start()/stop()`。主题键与Task 1一致。

- [x] 先写失败测试：重叠请求去重、失败从完成时间推迟、旧版本/关闭期间不发布、重启中断和单次到期排队。
- [x] 用唯一active_key和条件更新认领；模型快照不保存API key，实际调用审计写任务JSON；运行结果只在当前版本/范围/允许发布时发布。无新版本标记检查完成无新增。
- [x] 生命周期启动/停止独立队列。后台不依赖网页登录，不改变行情订阅。
- [x] 运行Task 1和调度测试。

### Task 4: 接口和预测冻结

**Files:** `backend/api/ai_routes.py`, `backend/ai_analysis/snapshot.py`, `backend/ai_analysis/orchestrator.py`, `backend/ai_analysis/future_trend.py`, `tests/test_scheduled_research_integration.py`

**Interfaces:** 四个方案接口，`freeze_materials`返回`{sources,background_sources,missing_dimensions,coverage_gaps,research,config_version,related_contracts,cache_state,hash,...}`；在提交预测前写入`public_materials`和`related_market_snapshots`，再计算整个批次hash。

- [x] 先写失败测试：预测不发网络研究、owner隔离、409、关联行情闭合过滤及缺少持仓前值、历史冻结不随配置变化。
- [x] 增加设置/更新/状态/正文API（仅sim）。替换未来趋势现场研究阶段；每模型共享已冻结输入，其他分析类型及旧历史展示兼容。
- [x] 提示词禁止将关联技术/持仓证据当目标自身，使用标签按已校验引用计算。
- [x] 运行未来趋势/联网研究/新接口集成测试。

### Task 5: 首页交互与结果展示

**Files:** `frontend/index.html`, `tests/check_scheduled_research.cjs`

- [x] 新增模型设置旁资料采集入口，支持独立模型/频率、多选添加具体合约、一次性自选导入、逐行直接关联、保存和按组刷新。
- [x] 未保存仅预览；版本冲突刷新提示。预测前显示检查/成功/下次时间及维度状态，结果标记已提供和实际引用，关联缺行情明确显示。
- [x] 正文、来源、时间、工具统计与错误折叠展示；旧现场研究记录仍可查看，任务结束默认收起。
- [x] 用Node运行真实JS行为测试和全部已有前端检查。

### Task 6: 验收与部署

- [x] 对照方案12条条件审查，修复发现的问题，执行新测试和AI回归测试。
- [x] 做一次真实公开页面读取并记录正文/日期/hash结果；实际模型调用需使用已有有效配置，报告真实完成或外部阻塞。
- [x] 重启后端并验证8000监听、auth/me 401 JSON；此前已完成隔离页面验证，用户要求停止后续浏览器操作，主服务页面最终验收由用户完成。
- [x] 在方案旁保存验收记录，明确本地测试、真实外网和浏览器结果；不提交用户原有改动、不启用定时配置。


## 完成记录

195项Python测试、12项Node前端检查通过；真实宏观研究任务PARTIAL，产出7份有效正文；后端已重启，生产采集配置仍为0。详见 [实施与验证记录](../validation/2026-10-02-scheduled-public-research.md)。
