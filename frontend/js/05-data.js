        function onSearchInput() {
            clearTimeout(searchTimer);
            const u = document.getElementById('universe-search');
            lastUniverseKw = u.value;
            showUniverseDropdown(true);   // 输入时确保下拉框展开
            searchTimer = setTimeout(() => searchUniverse(lastUniverseKw), 350);
        }

        let barReqSeq = 0;   // K线请求序号: 快速切换合约时丢弃过期响应

        function updateChartPrice(tick) {
            if (tick.instrument !== currentSymbol || (currentPeriod === 'intraday' && intradayDate)) return;
            const bars = currentPeriod === 'intraday' ? intradayBars : barsData;
            const bar = bars[bars.length - 1];
            if (!bar || bar.is_closed !== false || bar.trading_day !== tick.trading_day) return;
            const price = Number(tick.last_price);
            if (!Number.isFinite(price) || price <= 0) return;
            const time = Math.floor(Date.parse(bar.bar_time.length === 19 ? bar.bar_time + 'Z' : bar.bar_time) / 1000);
            const tickTime = Math.floor(Date.parse(tick.timestamp.length === 19 ? tick.timestamp + 'Z' : tick.timestamp) / 1000);
            const duration = currentPeriod === 'intraday' ? 60 : parseInt(currentPeriod) * 60;
            if (currentPeriod !== '1d' && (tickTime < time || tickTime >= time + duration)) return;
            bar.close_price = String(price);
            bar.high_price = String(Math.max(Number(bar.high_price), price));
            bar.low_price = String(Math.min(Number(bar.low_price), price));
            bar.open_interest = tick.open_interest;
            if (currentPeriod === 'intraday') {
                intradayPoints = intradayPoints.map(p => p.time === time ? { time, value: price } : p);
                intradaySeries.setData(intradayPoints);
            } else {
                candleSeries.update({ time, open: Number(bar.open_price), high: Number(bar.high_price), low: Number(bar.low_price), close: price });
            }
            renderOhlcLegend();
        }

        let liveRefreshBusy = false;
        let lastLiveRefresh = 0;
        async function refreshLiveChart() {
            if (document.hidden || !currentSymbol || liveRefreshBusy || Date.now() - lastLiveRefresh < 1000) return;
            // 历史回看保持原交易日；只有最新行情跟随 Tick 更新。
            if (currentPeriod === 'intraday' && intradayDate) return;
            liveRefreshBusy = true;
            lastLiveRefresh = Date.now();
            try { await loadBars(); } finally { liveRefreshBusy = false; }
        }

        async function loadBars() {
            if (currentPeriod === 'intraday') {
                await loadIntraday();
                return;
            }
            const seq = ++barReqSeq;
            try {
                // 日线加载全部历史 (接口上限 1000 根), 默认视图展示最近 90 个交易日; 其余周期 200 根
                const limit = currentPeriod === '1d' ? 1000 : 200;
                const r = await fetchBarsForChart(`/api/v1/bars/${currentSymbol}?period=${currentPeriod}&limit=${limit}`);
                if (!r.ok) return;                 // 具体原因已由 fetchBarsForChart 提示
                if (seq !== barReqSeq) return;     // 已有更新的请求, 丢弃本次结果
                const data = r.data;
                if (seq !== barReqSeq) return;
                if (!data.bars || data.bars.length === 0) {
                    console.warn('[图表] 返回 0 根 K线', currentSymbol, currentPeriod);
                    showChartNotice('该周期暂无K线数据<br><span style="font-size:11px;">' + currentSymbol + ' · ' + currentPeriod + '</span>', 'warn');
                    return;
                }
                showChartNotice(null);             // 有数据则清除提示
                {
                    // 恢复K线模式可见性, 并清空分时系列数据 (隐藏系列的数据点仍会污染时间轴)
                    candleSeries.applyOptions({ visible: true });
                    ema20Series.applyOptions({ visible: true });
                    ema60Series.applyOptions({ visible: true });
                    candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.05 } });
                    intradaySeries.applyOptions({ visible: false });
                    intradaySeries.setData([]);
                    intradaySeries.setMarkers([]);   // 清除分时高低点标注
                    if (intradayPriceLine) { try { intradaySeries.removePriceLine(intradayPriceLine); } catch (e) {} intradayPriceLine = null; }
                    document.getElementById('chart-legend').style.display = 'flex';
                    document.getElementById('intraday-legend').style.display = 'none';
                    // K线模式: 恢复右价格轴, 隐藏百分比轴覆盖层
                    candleSeries.applyOptions({ priceScaleId: 'right' });
                    ema20Series.applyOptions({ priceScaleId: 'right' });
                    ema60Series.applyOptions({ priceScaleId: 'right' });
                    intradaySeries.applyOptions({ priceScaleId: 'right' });
                    intradayBaseline = null;
                    chart.applyOptions({ crosshair: { vertLine: { visible: true }, horzLine: { visible: true } }, rightPriceScale: { visible: true }, leftPriceScale: { visible: false }, timeScale: { visible: true, fixRightEdge: false }, grid: { vertLines: { visible: true }, horzLines: { visible: true } } });
                    if (subChart) subChart.applyOptions({ rightPriceScale: { visible: true }, timeScale: { visible: true, fixRightEdge: false }, grid: { vertLines: { visible: true }, horzLines: { visible: true } } });
                    const pctEl = document.getElementById('pct-axis');
                    if (pctEl) pctEl.style.display = 'none';
                    // K线模式同样显示副图 Y 轴刻度 (由 renderSubGrid 绘制), 不再整体隐藏覆盖层
                    requestAnimationFrame(() => { try { renderSubGrid(); } catch (e) {} });
                    // K线模式恢复交互 (左右边界由 clamp 监听器锁定)
                    chart.applyOptions({ handleScroll: true, handleScale: true });
                    if (subChart) subChart.applyOptions({ handleScroll: true, handleScale: true });
                    subChart.applyOptions({ handleScroll: true, handleScale: true });
                    const candles = data.bars.map(b => ({
                        // 后端为北京时间无时区字符串, 追加 Z 使图表按 UTC 格式化后与本地钟面一致
                        time: Math.floor(Date.parse(b.bar_time.length === 19 ? b.bar_time + 'Z' : b.bar_time) / 1000),
                        open: parseFloat(b.open_price),
                        high: parseFloat(b.high_price),
                        low: parseFloat(b.low_price),
                        close: parseFloat(b.close_price),
                    }));
                    const candleTick = SPEC_FALLBACK[(currentSymbol.replace(/[0-9]+$/, '') || '').toUpperCase()]?.tick;
                    candleSeries.applyOptions({ priceFormat: { type: 'price', precision: priceDecOf(currentSymbol), minMove: candleTick || 0.1 } });
                    candleSeries.setData(candles);
                    updatePivotMarkers();

                    // 绘制 EMA 均线
                    const ema20 = calculateEMA(candles, 20);
                    const ema60 = calculateEMA(candles, 60);
                    ema20Series.setData(ema20);
                    ema60Series.setData(ema60);

                    // 刷新副图指标
                    barsData = data.bars;
                    buildOhlcIndex(barsData);
                    renderOhlcLegend();
                    renderSubChart();
                    // 周期/合约切换后: 日线默认展示最近 90 个交易日 (可往左拖看全部历史), 其余周期贴合全量
                    if (pendingFit) {
                        pendingFit = false;
                        const lastIdx = Math.max(1, candles.length - 1);
                        const fromIdx = (currentPeriod === '1d' && candles.length > 90) ? candles.length - 90 : 0;
                        chart.timeScale().setVisibleLogicalRange({ from: fromIdx, to: lastIdx });
                        if (subChart) subChart.timeScale().setVisibleLogicalRange({ from: fromIdx, to: lastIdx });
                    }
                }
            } catch (e) {
                console.error("fetch bars error", e);
                showChartNotice('K线渲染出错<br><span style="font-size:11px;">' + (e && e.message ? e.message : e) + '</span>', 'error');
            }
        }

        // 当日分时: 默认取最新交易日; 指定日期时从历史库取该交易日全部分钟线
        // 分时图标注当日最高/最低点 (标注在对应 1m K线上, 显示极值价)
        function markIntradayHL(dayBars, toTime) {
            if (!intradaySeries) return;
            if (!dayBars.length) { intradaySeries.setMarkers(aiSessionStartMarker ? [aiSessionStartMarker] : []); return; }
            let hi = dayBars[0], lo = dayBars[0];
            for (const b of dayBars) {
                if (parseFloat(b.high_price) > parseFloat(hi.high_price)) hi = b;
                if (parseFloat(b.low_price) < parseFloat(lo.low_price)) lo = b;
            }
            const markers = [
                { time: toTime(hi), position: 'aboveBar', color: '#ef4444', shape: 'arrowDown', text: '高 ' + parseFloat(hi.high_price).toFixed(priceDecOf(currentSymbol)), size: 1 },
                { time: toTime(lo), position: 'belowBar', color: '#10b981', shape: 'arrowUp', text: '低 ' + parseFloat(lo.low_price).toFixed(priceDecOf(currentSymbol)), size: 1 },
            ];
            if (aiSessionStartMarker) markers.push(aiSessionStartMarker);
            markers.sort((a, b) => a.time - b.time);
            intradaySeries.setMarkers(markers);
        }

        // 图表区提示条: 接口异常 / 数据为空时给出可见反馈, 避免"空白图表 + 无任何提示"
        // kind: 'info' | 'warn' | 'error'
        function showChartNotice(msg, kind) {
            const el = document.getElementById('chart-notice');
            if (!el) return;
            if (!msg) { el.style.display = 'none'; return; }
            const color = kind === 'error' ? '#ef4444' : (kind === 'warn' ? '#fbbf24' : 'var(--text-muted)');
            const border = kind === 'error' ? 'rgba(239,68,68,0.5)' : (kind === 'warn' ? 'rgba(251,191,36,0.5)' : 'var(--border-color)');
            el.style.color = color;
            el.style.borderColor = border;
            el.innerHTML = msg;
            el.style.display = 'block';
        }

        // 带状态说明的取数封装: 区分「网络/HTTP 错误」与「数据为空」, 并把真实原因暴露到界面与控制台
        async function fetchBarsForChart(url) {
            let res;
            try {
                res = await fetch(url);
            } catch (netErr) {
                console.error('[分时/图表] 网络请求失败', url, netErr);
                showChartNotice('无法连接行情服务<br><span style="font-size:11px;">请确认服务已启动 (端口 8000)，然后刷新页面</span>', 'error');
                return { ok: false, reason: 'network' };
            }
            if (res.status === 401) {
                console.warn('[分时/图表] 会话已过期', url, res.status);
                showChartNotice('登录会话已过期<br><span style="font-size:11px;">请重新登录后再查看图表</span>', 'warn');
                return { ok: false, reason: 'unauthorized' };
            }
            if (!res.ok) {
                console.error('[分时/图表] 接口返回异常', url, res.status, res.statusText);
                showChartNotice(
                    '行情数据加载失败 (HTTP ' + res.status + ')<br>' +
                    '<span style="font-size:11px; word-break:break-all;">' + url + '</span>',
                    'error'
                );
                return { ok: false, reason: 'http', status: res.status };
            }
            let data;
            try {
                data = await res.json();
            } catch (jsonErr) {
                console.error('[分时/图表] 响应解析失败', url, jsonErr);
                showChartNotice('行情数据格式异常，无法解析<br><span style="font-size:11px;">详情见浏览器控制台 (F12)</span>', 'error');
                return { ok: false, reason: 'json' };
            }
            return { ok: true, data };
        }

        async function loadIntraday() {
            const seq = ++barReqSeq;
            try {
                let res;
                const url = intradayDate
                    ? `/api/v1/bars/${currentSymbol}?period=1m&date=${intradayDate}&limit=1000`
                    : `/api/v1/bars/${currentSymbol}?period=1m&limit=700`;
                const r = await fetchBarsForChart(url);
                if (!r.ok) return;                 // 具体原因已由 fetchBarsForChart 提示
                if (seq !== barReqSeq) return;
                const data = r.data;
                if (seq !== barReqSeq) return;
                const bars = data.bars || [];
                if (!bars.length) {
                    // 明确告知"接口正常但无数据", 并给出当日/所选日期上下文
                    const scope = intradayDate
                        ? (intradayDate.slice(0, 4) + '-' + intradayDate.slice(4, 6) + '-' + intradayDate.slice(6, 8))
                        : '最新交易日';
                    console.warn('[分时/图表] 返回 0 根 1m K线', url);
                    showChartNotice('该区间暂无分钟数据<br><span style="font-size:11px;">' + currentSymbol + ' · ' + scope + '</span>', 'warn');
                    return;
                }
                showChartNotice(null);             // 有数据则清除提示

                candleSeries.applyOptions({ visible: false });
                ema20Series.applyOptions({ visible: false });
                ema60Series.applyOptions({ visible: false });
                intradaySeries.applyOptions({ visible: true });
                // 分时模式无成交量柱, 分时线占用更多纵向空间
                candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
                // 清空K线模式数据, 避免其时间点污染分时时间轴
                candleSeries.setData([]);
                ema20Series.setData([]);
                ema60Series.setData([]);
                candleSeries.setMarkers([]);   // 分时模式清除高低点标注
                document.getElementById('chart-legend').style.display = 'none';
                document.getElementById('intraday-legend').style.display = 'flex';

                // 分时模式: 原生价格轴全部隐藏, 左右刻度改由覆盖层画在图内 (同花顺风格);
                // 底部时间轴同样自绘 (仅阶段开收盘刻度); 背景网格线由覆盖层按刻度绘制, 关闭原生网格;
                // 副图隐藏右轴与时间轴, 使主副图窗格等宽对齐
                intradaySeries.applyOptions({ priceScaleId: 'left' });
                // 分时模式原生十字线整体隐藏: 行情重绘会让原生磁吸线按新刻度重新吸附,
                // 鼠标不动横线也会漂移; 分时只保留覆盖层十字线 (稳定吸附在鼠标位置)
                chart.applyOptions({ crosshair: { vertLine: { visible: false }, horzLine: { visible: false } }, leftPriceScale: { visible: false }, rightPriceScale: { visible: false }, timeScale: { visible: false, fixRightEdge: false }, grid: { vertLines: { visible: false }, horzLines: { visible: false } } });
                if (subChart) subChart.applyOptions({ rightPriceScale: { visible: false }, timeScale: { visible: false, fixRightEdge: false }, grid: { vertLines: { visible: false }, horzLines: { visible: false } } });

                if (!bars.length) return;
                // 以最新一根 1m 的交易日为"当日" (夜盘+白盘归同一交易日)
                const day = intradayDate || bars[bars.length - 1].trading_day;
                // 取昨结基准价 (真实结算价优先, 回退昨收; 结果有缓存)
                // 必须在渲染/fitContent 之前 await, 否则回调落在 fitContent 动画中会打断视口
                const baseline = await getBaseline(currentSymbol, day);
                if (seq !== barReqSeq) return;
                intradayBaseline = baseline;
                const dayBars = intradayDate ? bars : bars.filter(b => b.trading_day === day);
                const daySel = document.getElementById('intraday-day');
                if (daySel) {
                    // 下拉选项 = 可用交易日 + 当前所选 (历史标"历史", 实时最新标"最新/含夜盘")
                    const prev = daySel.value;
                    const opts = [];
                    const seen = new Set();
                    const all = (intradayDates.length ? intradayDates : [day]).slice();
                    if (!all.includes(day)) all.push(day);
                    all.sort();
                    for (const d of all) {
                        if (seen.has(d)) continue;
                        seen.add(d);
                        const label = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
                        opts.push(`<option value="${d}" ${d === (intradayDate || day) ? 'selected' : ''}>${label}</option>`);
                    }
                    daySel.innerHTML = opts.join('');
                    if (prev && prev !== daySel.value) { /* 选项已按选择同步 */ }
                }
                const toTime = b => Math.floor(Date.parse(b.bar_time.length === 19 ? b.bar_time + 'Z' : b.bar_time) / 1000);

                const pricePoints = [];
                intradayPriceMin = null; intradayPriceMax = null;
                for (const b of dayBars) {
                    const t = toTime(b);
                    const c = parseFloat(b.close_price);
                    if (intradayPriceMin === null || c < intradayPriceMin) intradayPriceMin = c;
                    if (intradayPriceMax === null || c > intradayPriceMax) intradayPriceMax = c;
                    pricePoints.push({ time: t, value: c });
                }
                // 日盘保留全天时间线；夜盘仅在确有K线时占据坐标。
                const sessions = intradaySessions(currentSymbol, day, dayBars);
                intradayTickLabels = new Map();
                const priceMap = new Map(pricePoints.map(p => [p.time, p]));
                for (const session of sessions) {
                    intradayTickLabels.set(session.start, session.startLabel);
                    intradayTickLabels.set(session.end, session.endLabel);
                    if (session.startLabel === '21:00' && session.end - session.start > 4 * 3600) {
                        for (let t = session.start + 3600; t < session.end - 1800; t += 3600) {
                            intradayTickLabels.set(t, new Date(t * 1000).toISOString().slice(11, 16));
                        }
                    }
                    for (let t = session.start; t <= session.end; t += 60) {
                        if (!priceMap.has(t)) priceMap.set(t, { time: t });
                    }
                }
                pricePoints.splice(0, pricePoints.length, ...Array.from(priceMap.values()).sort((a, b) => a.time - b.time));
                intradayValuedTimes = new Set(pricePoints.filter(p => p.value !== undefined).map(p => p.time));
                intradayValueByTime = new Map(pricePoints.filter(p => p.value !== undefined).map(p => [p.time, p.value]));
                const viewKey = `${currentSymbol}_${day}`;
                const previousRange = intradayViewKey === viewKey ? chart.timeScale().getVisibleLogicalRange() : null;
                intradayRange = null;
                intradayPoints = pricePoints;
                const intradayTick = SPEC_FALLBACK[(currentSymbol.replace(/[0-9]+$/, '') || '').toUpperCase()]?.tick;
                intradaySeries.applyOptions({ priceFormat: { type: 'price', precision: priceDecOf(currentSymbol), minMove: intradayTick || 0.1 } });
                intradaySeries.setData(intradayPoints);
                markIntradayHL(dayBars, toTime);

                // 刷新副图指标 (分时模式下基于当日 1m 数据)
                intradayBars = dayBars;
                buildOhlcIndex(dayBars);
                renderOhlcLegend();
                renderSubChart();
                renderAISessionChartLines();

                // 中轴线: 昨结基准价 (虚线), 标签画在图内而非坐标轴上
                if (intradayPriceLine) { try { intradaySeries.removePriceLine(intradayPriceLine); } catch (e) {} intradayPriceLine = null; }
                if (baseline !== null) {
                    intradayPriceLine = intradaySeries.createPriceLine({
                        price: baseline,
                        color: '#f59e0b',
                        lineWidth: 1,
                        lineStyle: LightweightCharts.LineStyle.Dashed,
                        axisLabelVisible: false,
                        title: '',
                    });
                }

                // 默认展示全天；滚轮缩放、按住拖动，主副图同步且不越过交易时段边界。
                chart.applyOptions({ handleScroll: true, handleScale: true });
                if (subChart) subChart.applyOptions({ handleScroll: true, handleScale: true });
                const nPts = pricePoints.length;
                intradayRange = { from: -1, to: nPts - 1 };
                intradayViewKey = viewKey;
                let visibleRange = intradayRange;
                if (previousRange && previousRange.to - previousRange.from < nPts) {
                    const width = previousRange.to - previousRange.from;
                    const from = Math.max(-1, Math.min(previousRange.from, nPts - 1 - width));
                    visibleRange = { from, to: from + width };
                }
                chart.timeScale().setVisibleLogicalRange(visibleRange);
                if (subChart) subChart.timeScale().setVisibleLogicalRange(visibleRange);
                // fitContent 的坐标下一帧才生效, 等一帧再绘制刻度覆盖层 (否则读到旧坐标导致网格错位)
                requestAnimationFrame(() => { renderPctAxis(); renderSubGrid(); });

                // 日期导航状态
                const prevBtn = document.getElementById('intraday-prev');
                const nextBtn = document.getElementById('intraday-next');
                const latestBtn = document.getElementById('intraday-latest');
                if (prevBtn && nextBtn && latestBtn) {
                    // 未选历史日期时视为最新交易日 (列表末尾), 保证 ◀▶ 可用
                    const idx = intradayDate ? intradayDates.indexOf(intradayDate) : intradayDates.length - 1;
                    prevBtn.disabled = !intradayDates.length || idx <= 0;
                    nextBtn.disabled = !intradayDates.length || idx >= intradayDates.length - 1;
                    latestBtn.style.display = intradayDate ? 'inline' : 'none';
                }
            } catch (e) {
                console.error("load intraday error", e);
                showChartNotice('分时图渲染出错<br><span style="font-size:11px;">' + (e && e.message ? e.message : e) + '</span>', 'error');
            }
        }

        // 分时日期导航: 可用交易日列表 (含当前实时交易日)
        async function loadIntradayDates() {
            if (!currentSymbol) return;
            const requestedSymbol = currentSymbol;
            try {
                const d = await (await fetch(`/api/v1/bars/${encodeURIComponent(requestedSymbol)}/dates?period=1m`)).json();
                if (requestedSymbol !== currentSymbol) return;
                const days = new Set(d.dates || []);
                // 当前实时交易日可能尚未入库, 从最近一根 Redis 1m 补充
                const raw = await (await fetch(`/api/v1/bars/${encodeURIComponent(requestedSymbol)}?period=1m&limit=1`)).json();
                if (requestedSymbol !== currentSymbol) return;
                if (raw.bars && raw.bars.length) days.add(raw.bars[raw.bars.length - 1].trading_day);
                intradayDates = Array.from(days).sort();
                // 分时模式下刷新日期下拉选项
                if (currentPeriod === 'intraday' && !intradayDate) loadIntraday();
            } catch (e) {
                intradayDates = [];
            }
        }

        function intradayPick(d) {
            // 下拉选择交易日: 空值 = 回到最新
            if (!d) { intradayLatest(); return; }
            if (d === intradayDate) return;
            intradayDate = d;
            pendingFit = true;
            loadIntraday();
            loadLatestAIResult(currentSymbol);
            if (leftTab === 'tape') loadTape();
            if (leftTab === 'big') loadBigOrders();
        }

        function intradayShift(dir) {
            if (!intradayDates.length) return;
            let idx = intradayDate ? intradayDates.indexOf(intradayDate) : intradayDates.length - 1;
            if (idx === -1) idx = intradayDates.length - 1;
            const next = idx + dir;
            if (next < 0 || next > intradayDates.length - 1) return;
            intradayDate = intradayDates[next];
            pendingFit = true;
            loadIntraday();
            loadLatestAIResult(currentSymbol);
            if (leftTab === 'tape') loadTape();       // 成交明细跟随分时日期
            if (leftTab === 'big') loadBigOrders();
        }

        function intradayLatest() {
            intradayDate = null;
            pendingFit = true;
            loadIntraday();
            loadLatestAIResult(currentSymbol);
            if (leftTab === 'tape') loadTape();
            if (leftTab === 'big') loadBigOrders();
        }

        async function loadIndicators() {
            const indPeriod = currentPeriod === 'intraday' ? '1m' : currentPeriod;
            try {
                const res = await fetch(`/api/v1/indicators/${currentSymbol}?period=${indPeriod}`);
                if (!res.ok) return;
                const ind = await res.json();
                document.getElementById('ind-day-hl').innerText = `${ind.day_high || '--'} / ${ind.day_low || '--'}`;
                document.getElementById('ind-prev-hl').innerText = `${ind.prev_high_20 || '--'} / ${ind.prev_low_20 || '--'}`;
                document.getElementById('ind-atr').innerText = ind.atr14 || '--';
                document.getElementById('ind-vwap').innerText = ind.vwap ?? '成交额数据不足';
            } catch (e) {}
        }

        // 客观规则信号: 后端枚举 → 中文显示 (鼠标悬停 title 保留英文原码备查)
        const SIGNAL_TYPE_TEXT = {
            LONG_WATCH: '多头观察', SHORT_WATCH: '空头观察', OI_SURGE: '持仓异动',
            SIGNAL_TRIGGER: '信号触发', NONE: '无信号',
        };
        const SIGNAL_RULE_TEXT = {
            BREAK_PREVIOUS_HIGH: '突破前高', BREAK_PREVIOUS_LOW: '跌破前低',
            VOLUME_EXPANSION: '放量', VOLUME_CONFIRM: '量能确认',
            OPEN_INTEREST_INCREASE: '持仓增加', OI_SURGE: '持仓异动',
            TREND_ALIGNMENT_60M: '60分钟趋势同向',
        };

        async function loadSignal() {
            const requestedSymbol = currentSymbol;
            try {
                const res = await fetch(`/api/v1/signals/${encodeURIComponent(requestedSymbol)}`);
                if (!res.ok) return;
                const data = await res.json();
                if (requestedSymbol !== currentSymbol) return;
                const sigTypeEl = document.getElementById('sig-type');
                const sig = data.signal || "NONE";
                sigTypeEl.innerText = SIGNAL_TYPE_TEXT[sig] || sig;
                sigTypeEl.title = sig === 'NONE' ? '' : sig;
                sigTypeEl.className = sig.includes("LONG") ? "sig-long" : (sig.includes("SHORT") ? "sig-short" : "sig-oi");
                document.getElementById('sig-score').innerText = (data.rule_score || 0) + "/100";

                const reasonsBox = document.getElementById('reasons-container');
                if (data.reasons && data.reasons.length > 0) {
                    reasonsBox.innerHTML = data.reasons.map(r => `
                        <div class="reason-box">
                            <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                                <strong title="${r.rule}">${SIGNAL_RULE_TEXT[r.rule] || r.rule}</strong>
                                <span class="${r.passed ? 'tag-passed' : 'tag-failed'}">${r.passed ? '符合' : '未达标'}</span>
                            </div>
                            <div style="color:var(--text-muted)">当前值: ${r.current !== undefined ? r.current : '--'} | 阈值: ${r.threshold !== undefined ? r.threshold : '--'}</div>
                        </div>
                    `).join('');
                } else {
                    reasonsBox.innerHTML = `<div class="reason-box" style="color:var(--text-muted);">暂无触发规则</div>`;
                }
            } catch (e) {
                console.error("fetch signal error", e);
            }
        }

        let analysisBusy = false;
        const ANALYSIS_STATUS_TEXT = {
            WARMING_UP: '开盘预热中',
            INSUFFICIENT_DATA: '历史样本不足',
            STALE_DATA: '行情已陈旧，暂停新信号',
            NO_TRADING: '当前合约无有效成交',
            UNSUPPORTED_INSTRUMENT: '暂不支持该合约'
        };
        const ANALYSIS_STATE_TEXT = {
            PREVIEW: '形成中预警', PENDING: '待确认', CONFIRMED: '已确认',
            INVALIDATED: '已失效', EXPIRED: '已过期'
        };

        function analysisDirectionColor(score) {
            return score >= 60 ? '#ef4444' : (score <= 40 ? '#10b981' : '#f59e0b');
        }

        async function loadAnalysis() {
            if (isLiveMode || !currentSymbol || analysisBusy || document.hidden) return;
            analysisBusy = true;
            const requestedSymbol = currentSymbol;
            const statusEl = document.getElementById('analysis-status');
            const readyEl = document.getElementById('analysis-ready');
            try {
                const res = await fetch(`/api/v1/analysis/${encodeURIComponent(requestedSymbol)}`);
                if (requestedSymbol !== currentSymbol) return;
                if (res.status === 401) { window.location.href = '/login'; return; }
                if (!res.ok) {
                    statusEl.style.display = 'block';
                    statusEl.textContent = res.status === 404 ? '该合约暂无可分析行情' : '量价分析暂不可用';
                    readyEl.style.display = 'none';
                    return;
                }
                const body = await res.json();
                const data = body.data || {};
                const status = data.analysis_status || 'INSUFFICIENT_DATA';
                const qt = data.quote_time ? String(data.quote_time).replace('T', ' ').slice(5, 19) : '--';
                const feedLabel = data.feed === 'sina' ? '新浪' : (data.feed || '--');
                const sourceDelay = status === 'MARKET_CLOSED' || data.data_latency_seconds == null
                    ? '' : ` · 源端延迟 ${data.data_latency_seconds}秒`;
                document.getElementById('analysis-data-time').textContent = `${feedLabel} · ${qt}${sourceDelay}`;
                if (status === 'MARKET_CLOSED') {
                    statusEl.style.display = 'none';
                    readyEl.style.display = 'none';
                    return;
                }
                if (status !== 'READY') {
                    statusEl.style.display = 'block';
                    statusEl.textContent = `${ANALYSIS_STATUS_TEXT[status] || status}：${data.status_message || '暂不生成信号'}`;
                    readyEl.style.display = 'none';
                    return;
                }
                statusEl.style.display = 'none';
                readyEl.style.display = 'block';
                const weekly = data.weekly || {};
                const intraday = data.intraday || {};
                const volume = data.volume || {};
                const weekEl = document.getElementById('analysis-week');
                const dayEl = document.getElementById('analysis-day');
                weekEl.textContent = weekly.label || '--';
                dayEl.textContent = intraday.label || '--';
                weekEl.style.color = analysisDirectionColor(Number(weekly.score || 50));
                dayEl.style.color = analysisDirectionColor(Number(intraday.score || 50));
                document.getElementById('analysis-week-score').textContent = `${weekly.score ?? '--'}分`;
                document.getElementById('analysis-day-score').textContent = `${intraday.score ?? '--'}分 · ${intraday.confidence || '--'}置信度`;
                document.getElementById('analysis-volume-ratio').textContent = volume.volume_ratio == null ? '基准不足' : `${volume.volume_ratio}倍 (${volume.baseline_quality || '--'})`;
                const oi = Number(volume.open_interest_change);
                document.getElementById('analysis-oi-change').textContent = Number.isFinite(oi) ? `${oi > 0 ? '+' : ''}${oi}手` : '--';

                const signal = data.signal || data.preview_signal;
                const signalBox = document.getElementById('analysis-signal-box');
                const nameEl = document.getElementById('analysis-signal-name');
                const stateEl = document.getElementById('analysis-signal-state');
                if (!signal) {
                    signalBox.style.borderColor = '#3b4555';
                    nameEl.style.color = '#e2e8f0';
                    nameEl.textContent = '当前无满足规则的信号';
                    stateEl.textContent = '--';
                    document.getElementById('analysis-signal-action').textContent = '等待新的已闭合5分钟K线';
                    document.getElementById('analysis-signal-risk').textContent = '';
                } else {
                    const sideColor = signal.side === 'BUY' ? '#ef4444' : '#10b981';
                    signalBox.style.borderColor = signal.state === 'PENDING' ? '#f59e0b' : sideColor;
                    nameEl.style.color = sideColor;
                    nameEl.textContent = signal.name || signal.code;
                    stateEl.textContent = `${ANALYSIS_STATE_TEXT[signal.state] || signal.state} · 置信度${signal.confidence_label || '--'}`;
                    document.getElementById('analysis-signal-action').textContent = signal.action_text || '--';
                    document.getElementById('analysis-signal-risk').textContent =
                        `触发 ${signal.trigger_price} · 失效 ${signal.invalidation_price} · 目标参考 ${signal.target_price} · 收益风险比 ${signal.reward_risk_ratio}`;
                }
            } catch (e) {
                statusEl.style.display = 'block';
                statusEl.textContent = '量价分析请求失败';
                readyEl.style.display = 'none';
            } finally {
                analysisBusy = false;
            }
        }

        let aiModels = [];
        const aiActiveBatches = new Map();
        let aiEvaluationStats = {};

