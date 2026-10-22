        function aiSessionDisplayActive(batch) {
            const until = Date.parse(batch?.display_until || '');
            return batch?.analysis_target === 'CURRENT_SESSION' && Number.isFinite(until) && Date.now() < until;
        }

        function scheduleAISessionExpiry(batches) {
            clearTimeout(aiSessionExpiryTimer);
            const next = Math.min(...batches.map(batch => Date.parse(batch.display_until || ''))
                .filter(until => Number.isFinite(until) && until > Date.now()));
            if (!Number.isFinite(next)) return;
            const symbol = currentSymbol;
            aiSessionExpiryTimer = setTimeout(() => {
                if (currentSymbol === symbol) loadLatestAIResult(symbol);
            }, next - Date.now() + 50);
        }

        function aiSessionChartDisplayActive(batch) {
            if (!batch || batch.instrument !== currentSymbol || batch.analysis_target !== 'CURRENT_SESSION') return false;
            if (currentPeriod === 'intraday' && intradayDate) return (batch.session_forecast?.trading_day || batch.trading_day) === intradayDate;
            return aiSessionDisplayActive(batch);
        }

        function aiSessionVisiblePrices(visibleRange = null) {
            const run = (aiSessionChartBatch?.runs || []).find(item => item.run_id === aiSessionChartRunId);
            const plan = run?.result?.session_chart;
            if (!plan || !aiSessionChartDisplayActive(aiSessionChartBatch) || currentPeriod !== 'intraday') return [];
            if (visibleRange && (aiChartWallTime(plan.forecast_start_time, true, plan.window_policy === 'RESULT_AVAILABLE_10M') > visibleRange.to ||
                aiChartWallTime(plan.forecast_end_time, false, plan.window_policy === 'RESULT_AVAILABLE_10M') < visibleRange.from)) return [];
            return [plan.reference_price, plan.target_price, plan.invalidation_price, plan.trigger_price]
                .filter(price => price !== null && price !== undefined).map(Number).filter(Number.isFinite);
        }

        function aiChartWallTime(value, roundUp = false, exact = false) {
            const wall = String(value || '').slice(0, 19);
            const milliseconds = Date.parse(wall + 'Z');
            if (!Number.isFinite(milliseconds)) return null;
            if (exact) return milliseconds / 1000;
            return (roundUp ? Math.ceil(milliseconds / 60000) : Math.floor(milliseconds / 60000)) * 60;
        }

        function clearAISessionChartLines(clearBatch = false, hideChoices = true) {
            [aiSessionReferenceSeries, aiSessionTargetSeries, aiSessionInvalidationSeries, aiSessionTriggerSeries]
                .forEach(series => {
                    if (!series) return;
                    series.setData([]);
                    series.applyOptions({visible:false});
                });
            aiSessionStartMarker = null;
            if (intradaySeries) markIntradayHL(intradayBars, bar => aiChartWallTime(bar.bar_time));
            if (hideChoices) {
                document.getElementById('ai-session-time-strip').style.display = 'none';
                document.getElementById('ai-session-line-models').style.display = 'none';
            }
            const meta = document.getElementById('ai-session-line-meta');
            if (meta) meta.textContent = '暂无有效盘时段预测';
            const legend = document.getElementById('ai-session-line-legend');
            if (legend) legend.textContent = '点击“盘时段画线”生成预测';
            if (clearBatch) {
                globalThis.marketSimulator?.clearReference();
                aiSessionChartBatches = []; aiSessionChartBatch = null; aiSessionChartRunId = null;
                aiSessionChartRunByBatch.clear();
            }
        }

        function aiSessionLineState(plan, start) {
            const end = aiChartWallTime(plan.forecast_end_time, false, plan.window_policy === 'RESULT_AVAILABLE_10M');
            const now = (Date.now() + 8 * 3600000) / 1000; // 图表时间轴使用北京时间的墙钟值。
            if (plan.kind === 'PRE_OPEN' && now < start) return '盘前展望，等待开盘';
            if (!['LONG', 'SHORT'].includes(plan.direction)) return '无明确方向';
            if (plan.window_policy === 'RESULT_AVAILABLE_10M') return aiTenMinuteLineState(plan, start, end, now);
            const target = Number(plan.target_price), invalidation = Number(plan.invalidation_price);
            const trigger = plan.trigger_price == null ? NaN : Number(plan.trigger_price);
            const bars = intradayBars.filter(bar => {
                const time = aiChartWallTime(bar.bar_time);
                return time !== null && time >= start && time < end;
            });
            const touchedAt = kind => {
                const price = kind === 'target' ? target : kind === 'invalidation' ? invalidation : trigger;
                if (!Number.isFinite(price)) return null;
                for (const bar of bars) {
                    const high = Number(bar.high_price), low = Number(bar.low_price);
                    const hit = plan.direction === 'LONG'
                        ? (kind === 'invalidation' ? low <= price : high >= price)
                        : (kind === 'invalidation' ? high >= price : low <= price);
                    if (hit) return aiChartWallTime(bar.bar_time);
                }
                return null;
            };
            const targetAt = touchedAt('target'), invalidAt = touchedAt('invalidation');
            if (targetAt !== null && invalidAt !== null && targetAt === invalidAt) return '目标与失效同一分钟触及，待核验';
            if (targetAt !== null && (invalidAt === null || targetAt < invalidAt)) return '目标已命中';
            if (invalidAt !== null && (targetAt === null || invalidAt < targetAt)) return '预测已失效';
            if (now >= end) return '时段已结束，未触及目标或失效线';
            if (touchedAt('trigger') !== null) return '预测已触发，等待结果';
            return Number.isFinite(trigger) ? '等待触发' : '预测进行中';
        }

        function aiTenMinuteLineState(plan, start, end, now) {
            const target = Number(plan.target_price), invalidation = Number(plan.invalidation_price);
            const trigger = plan.trigger_price == null ? null : Number(plan.trigger_price);
            let active = trigger === null;
            let expected = Math.ceil(start / 60) * 60;
            const bars = intradayBars.filter(bar => {
                const time = aiChartWallTime(bar.bar_time);
                return time !== null && time >= start && time + 60 <= Math.min(end, now);
            });
            for (const bar of bars) {
                const time = aiChartWallTime(bar.bar_time);
                if (time !== expected) return '窗口内分钟行情有缺口，待核验';
                expected = time + 60;
                const high = Number(bar.high_price), low = Number(bar.low_price);
                const hit = plan.direction === 'LONG' ? high >= target : low <= target;
                const invalid = plan.direction === 'LONG' ? low <= invalidation : high >= invalidation;
                if (!active) {
                    const triggered = plan.direction === 'LONG' ? high >= trigger : low <= trigger;
                    if (!triggered) {
                        if (invalid) return '进场前已失效';
                        continue;
                    }
                    if (hit || invalid) return '触发与退出价同一分钟触及，待核验';
                    active = true;
                    continue;
                }
                if (hit && invalid) return '目标与失效同一分钟触及，待核验';
                if (hit) return '目标已命中';
                if (invalid) return '预测已失效';
            }
            if (now >= end && expected <= Math.floor(end / 60) * 60 - 60) return '窗口尾部分钟行情缺失，待核验；成交以模拟账本为准';
            if (now >= end) return active ? '窗口到期，按有效报价结算；实际盈亏以模拟账本为准' : '窗口到期，未触发';
            return active ? '预测已触发，等待结果' : '等待触发';
        }

        function renderAISessionLineLegend(plan, start) {
            const direction = {LONG:'偏多', SHORT:'偏空', NEUTRAL:'中性', NO_JUDGMENT:'无法判断'}[plan.direction] || plan.direction;
            const startText = String(plan.forecast_start_time).slice(11,19);
            const endText = String(plan.forecast_end_time).slice(11,19);
            const state = aiSessionLineState(plan, start);
            document.getElementById('ai-session-line-meta').textContent =
                `${plan.kind === 'PRE_OPEN' ? '盘前展望 · ' : plan.window_policy === 'RESULT_AVAILABLE_10M' ? '模型完成后10分钟 · ' : ''}${direction} · ${startText}–${endText} · 当前状态：${state}`;
            const items = [
                ['#cbd5e1', plan.kind === 'PRE_OPEN'
                    ? `基准线 ${plan.reference_price}：盘前参考价（非开盘价），开盘后从这里对照涨跌`
                    : `基准线 ${plan.reference_price}：分析冻结时的参考价，用于对照涨跌`],
            ];
            if (plan.trigger_price != null) items.push([
                '#f59e0b', `触发线 ${plan.trigger_price}：价格${plan.direction === 'LONG' ? '升破' : '跌破'}后，${direction}预测开始生效`,
            ]);
            if (plan.target_price != null) items.push([
                '#34d399', `目标线 ${plan.target_price}：先到这里，预测目标命中`,
            ]);
            if (plan.invalidation_price != null) items.push([
                '#fb7185', `失效线 ${plan.invalidation_price}：先到这里，${direction}判断失效`,
            ]);
            const legend = document.getElementById('ai-session-line-legend');
            legend.replaceChildren(...items.map(([color, text]) => {
                const item = document.createElement('span');
                item.style.cssText = 'display:inline-flex; align-items:center; gap:5px;';
                const line = document.createElement('i');
                line.style.cssText = `display:inline-block; width:22px; border-top:2px dashed ${color};`;
                const label = document.createElement('span'); label.textContent = text;
                item.append(line, label);
                return item;
            }));
        }

        function extendAISessionForecastTimeline(plan, start, end) {
            if (plan.kind !== 'PRE_OPEN' || !intradaySeries || !intradayPoints.length) return false;
            const points = new Map(intradayPoints.map(point => [point.time, point]));
            const size = points.size;
            for (let time = start; time <= end; time += 60) {
                if (!points.has(time)) points.set(time, {time});
            }
            intradayTickLabels.set(start, String(plan.forecast_start_time).slice(11,16));
            intradayTickLabels.set(end, String(plan.forecast_end_time).slice(11,16));
            if (points.size === size) return false;
            intradayPoints.splice(0, intradayPoints.length, ...Array.from(points.values()).sort((a,b) => a.time - b.time));
            intradayRange = {from:-1, to:intradayPoints.length - 1};
            intradaySeries.setData(intradayPoints);
            return true;
        }

        function renderAISessionChartLines(preserveViewport = false) {
            const visibleRange = preserveViewport && chart ? chart.timeScale().getVisibleRange() : null;
            const batch = aiSessionChartBatch;
            const run = (batch?.runs || []).find(item => item.run_id === aiSessionChartRunId);
            const plan = run?.result?.session_chart;
            // 分时数据为 1 分钟粒度；从点击后的第一个完整分钟开始画，避免把点击前的同一分钟算入预测区间。
            const start = plan ? aiChartWallTime(plan.forecast_start_time, true, plan.window_policy === 'RESULT_AVAILABLE_10M') : null;
            const end = plan ? aiChartWallTime(plan.forecast_end_time, false, plan.window_policy === 'RESULT_AVAILABLE_10M') : null;
            const valid = batch && plan && batch.instrument === currentSymbol && currentPeriod === 'intraday'
                && aiSessionChartDisplayActive(batch)
                && start !== null && end !== null && start < end;
            clearAISessionChartLines(false, !valid);
            if (!valid) return;
            const expanded = extendAISessionForecastTimeline(plan, start, end);
            const draw = (series, price) => {
                if (price === null || price === undefined) return;
                price = Number(price);
                if (!series || !Number.isFinite(price) || price <= 0) return;
                series.setData([{time:start, value:price}, {time:end, value:price}]);
                series.applyOptions({visible:true});
            };
            draw(aiSessionReferenceSeries, plan.reference_price);
            draw(aiSessionTargetSeries, plan.target_price);
            draw(aiSessionInvalidationSeries, plan.invalidation_price);
            draw(aiSessionTriggerSeries, plan.trigger_price);
            if (visibleRange) chart.timeScale().setVisibleRange(visibleRange);
            else if (expanded && chart) chart.timeScale().fitContent();
            const markerTimes = Array.from(intradayValuedTimes).filter(time => plan.kind === 'PRE_OPEN'
                ? time >= start && time < end : time <= start);
            if (markerTimes.length) {
                const markerTime = plan.kind === 'PRE_OPEN' ? Math.min(...markerTimes) : Math.max(...markerTimes);
                aiSessionStartMarker = {
                    time:markerTime, position:plan.direction === 'LONG' ? 'belowBar' : 'aboveBar',
                    color:'#c084fc', shape:plan.direction === 'LONG' ? 'arrowUp' : 'arrowDown',
                    text:`${plan.kind === 'PRE_OPEN' ? 'AI盘前参考' : 'AI窗口起点/冻结参考'} ${String(plan.forecast_start_time).slice(11,19)} @ ${plan.reference_price}`, size:1,
                };
                markIntradayHL(intradayBars, bar => aiChartWallTime(bar.bar_time));
            }
            renderAISessionLineLegend(plan, start);
            renderAISessionChartChoices();
        }

        function updateAISessionChoiceArrows(listId) {
            const list = document.getElementById(listId);
            const prev = document.getElementById(`${listId}-prev`);
            const next = document.getElementById(`${listId}-next`);
            const overflow = list.scrollWidth > list.clientWidth + 1;
            prev.hidden = next.hidden = !overflow;
            prev.disabled = list.scrollLeft <= 1;
            next.disabled = list.scrollLeft + list.clientWidth >= list.scrollWidth - 1;
        }

        function scrollAISessionChoices(listId, direction) {
            const list = document.getElementById(listId);
            list.scrollBy({left: direction * Math.max(100, list.clientWidth * 0.8), behavior:'smooth'});
        }

        function keepAISessionChoiceVisible(list) {
            const active = list.querySelector('.is-active');
            if (!active) return;
            const item = active.getBoundingClientRect();
            const view = list.getBoundingClientRect();
            if (item.left < view.left) list.scrollLeft -= view.left - item.left;
            else if (item.right > view.right) list.scrollLeft += item.right - view.right;
        }

        function renderAISessionChartChoices() {
            const times = document.getElementById('ai-session-line-times');
            const timeScroll = times.scrollLeft;
            times.replaceChildren(...aiSessionChartBatches.map(batch => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = `ai-session-choice${batch.batch_id === aiSessionChartBatch?.batch_id ? ' is-active' : ''}`;
                button.textContent = String(batch.snapshot_time).slice(11,19);
                button.setAttribute('aria-pressed', batch.batch_id === aiSessionChartBatch?.batch_id ? 'true' : 'false');
                button.onclick = () => selectAISessionChartBatch(batch.batch_id);
                return button;
            }));
            document.getElementById('ai-session-time-strip').style.display = 'flex';
            const models = document.getElementById('ai-session-line-models');
            const runs = (aiSessionChartBatch?.runs || []).filter(run => run.result?.session_chart);
            const modelList = document.getElementById('ai-session-line-model-list');
            const modelScroll = modelList.scrollLeft;
            modelList.replaceChildren(...runs.map(run => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = `ai-session-choice${run.run_id === aiSessionChartRunId ? ' is-active' : ''}`;
                button.textContent = run.model_display_name;
                button.setAttribute('aria-pressed', run.run_id === aiSessionChartRunId ? 'true' : 'false');
                button.onclick = () => selectAISessionChartRun(run.run_id);
                return button;
            }));
            models.style.display = runs.length ? 'flex' : 'none';
            requestAnimationFrame(() => {
                times.scrollLeft = timeScroll;
                modelList.scrollLeft = modelScroll;
                keepAISessionChoiceVisible(times);
                if (runs.length) keepAISessionChoiceVisible(modelList);
                updateAISessionChoiceArrows('ai-session-line-times');
                if (runs.length) updateAISessionChoiceArrows('ai-session-line-model-list');
            });
        }

        function selectAISessionChartBatch(batchId) {
            globalThis.marketSimulator?.clearReference();
            const batch = aiSessionChartBatches.find(item => item.batch_id === batchId);
            if (!batch) return;
            const runs = (batch.runs || []).filter(run => run.result?.session_chart);
            if (!runs.length) return;
            aiSessionChartBatch = batch;
            const saved = aiSessionChartRunByBatch.get(batchId);
            aiSessionChartRunId = runs.find(run => run.run_id === saved)?.run_id || runs[0].run_id;
            renderAISessionChartLines(true);
        }

        function selectAISessionChartRun(runId) {
            const batch = aiSessionChartBatches.find(item => (item.runs || []).some(run => run.run_id === runId));
            if (!batch) return;
            aiSessionChartBatch = batch;
            aiSessionChartRunId = runId;
            aiSessionChartRunByBatch.set(batch.batch_id, runId);
            globalThis.marketSimulator?.selectReference(runId, batch.batch_id);
            renderAISessionChartLines(true);
        }

        function setAISessionChartBatches(batches, preferredRunId = null, preserveViewport = false) {
            scheduleAISessionExpiry(batches);
            aiSessionChartBatches = batches.filter(batch => batch.analysis_target === 'CURRENT_SESSION'
                && aiSessionChartDisplayActive(batch)
                && (batch.runs || []).some(run => run.result?.session_chart))
                .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
            if (!aiSessionChartBatches.length) { clearAISessionChartLines(true); return; }
            const choices = aiSessionChartBatches.flatMap(batch => (batch.runs || [])
                .filter(run => run.result?.session_chart).map(run => ({batch, run})));
            aiSessionChartRunId = choices.some(item => item.run.run_id === preferredRunId) ? preferredRunId
                : choices.some(item => item.run.run_id === aiSessionChartRunId) ? aiSessionChartRunId
                : choices[0].run.run_id;
            aiSessionChartBatch = choices.find(item => item.run.run_id === aiSessionChartRunId).batch;
            if (globalThis.marketSimulator?.explicitReference?.run !== aiSessionChartRunId
                || globalThis.marketSimulator?.explicitReference?.batch !== aiSessionChartBatch.batch_id) {
                globalThis.marketSimulator?.clearReference();
            }
            aiSessionChartRunByBatch.set(aiSessionChartBatch.batch_id, aiSessionChartRunId);
            renderAISessionChartLines(preserveViewport);
        }

        function setAISessionChartBatch(batch) {
            if (!batch || !(batch.runs || []).some(run => run.result?.session_chart)) return;
            const isNew = !aiSessionChartBatches.some(item => item.batch_id === batch.batch_id);
            const batches = [batch, ...aiSessionChartBatches.filter(item => item.batch_id !== batch.batch_id)];
            const firstRun = batch.runs.find(run => run.result?.session_chart);
            setAISessionChartBatches(batches, isNew ? firstRun.run_id : null);
        }

        function activeAIBatch(symbol = currentSymbol) {
            return aiActiveBatches.get(symbol) || null;
        }

        function syncAIAnalysisButton() {
            const disabled = Boolean(activeAIBatch());
            ['ai-analyze-btn', 'ai-session-chart-btn', 'future-trend-btn'].forEach(id => {
                const button = document.getElementById(id);
                if (button) button.disabled = disabled;
            });
        }

        async function aiErrorMessage(res) {
            try {
                const data = await res.json();
                return typeof data.detail === 'string' ? data.detail : (data.message || `请求失败 (${res.status})`);
            } catch (e) {
                return `请求失败 (${res.status})`;
            }
        }

