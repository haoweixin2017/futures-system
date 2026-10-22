# AI 盘时段成交量误拦修复

2026-10-22（北京时间）。用户报告 FU2701 报价截至 14:20:15、K 线截至 14:19，因 `UNVERIFIED_MINUTE_VOLUME` 未请求模型。

## 证据与原因

只读核查批次 `f6ec2d34-6504-456a-bdf1-ba5ff9c1c496`：目标 `CURRENT_SESSION`，所选日盘第三节 13:30—15:00；冻结 `market.bars_1m` 为 13:30—14:19 的 50 根分钟柱，无 `volume=null`，无分钟缺口。原质量仍为 `can_judge=false`、`blocking_reasons=[UNVERIFIED_MINUTE_VOLUME]`。报价延迟约 14.64 秒，`STALE_QUOTE` 仅为警告，不是该次阻断原因。

当天数据库的 FU2701 09:20 柱有 `UNALLOCATED_GAP`：前一源观测 2026-10-21 22:59:59，后一源观测 2026-10-22 09:20:03，跨断档累计差 64414 手，分钟归属未知。该真实缺口标记应保留。

`build_snapshot()` 在筛选所选小节前令 `quality_bars=bars_1m`，筛选后仍校验原全天列表，导致范围外的上午未知量持续拦截下午分析。不是模型失败，也不是下午数据缺量。

## 修复与边界

质量校验改为使用最终发送的 `market.bars_1m`；ACTIVE、UPCOMING 和全天请求各按其实际证据集合检查。所选范围内未知量继续阻断，TODAY/NEXT_OPEN 仍保留全天范围。报价和分钟新鲜度阈值保持原值。

新增 `quality.unverified_volume_bars`，保留问题柱的合约、1m 周期、北京时间标签、质量标记与缺口信息；页面审计区显示前 5 根及总数。历史冻结快照、旧结果和真实未知量不改写。

## 验证

- 新回归先复现“下午 50 根健康柱仍被上午未知量拦截”，修复后通过；并检查全天仍阻断、所选小节内部未知量仍阻断。
- `.venv\Scripts\python.exe -m unittest tests.test_ai_reliability tests.test_ai_analysis tests.test_volume_quality tests.test_ai_session_window tests.test_paper_verification tests.test_paper_verification_engine tests.test_paper_verification_api`：228 项通过。既有盘前测试的时钟为 10:40，原目标 10:30 却标 UPCOMING，已将目标调整为真正未来的 13:30，保留实际发送证据边界断言。
- `node tests/check_ai_reliability.cjs`、`node tests/check_ai_session_lines.cjs`、`node tests/check_ai_verification.cjs`、`node tests/check_volume_quality.cjs` 均通过。
- 对原冻结 50 根柱在原截点只读重验：`can_judge=true`、阻断列表为空，仍有 `STALE_QUOTE` 警告。14:29 的只读新快照亦无阻断；此检查没有提交 AI 请求。
- 按 `stop.bat`、`start.bat` 重启。新进程 21936 于 14:29:48 启动，8000 LISTENING，未登录接口返回 401 JSON，健康接口报告 MySQL/Redis connected、profile=local、feed=sina。

上述为隔离回归、真实冻结数据复核及服务健康验证；没有通过新增生产计划或付费模型调用验收，也未完成真实浏览器视觉验收。历史失败结果仍展示原冻结结论，后续新请求使用修复后的逻辑。
