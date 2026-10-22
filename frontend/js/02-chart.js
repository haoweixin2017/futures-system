        async function logout() {
            globalThis.invalidateMarketSimulator?.();
            try { await fetch('/api/v1/auth/logout', { method: 'POST' }); } catch (e) {}
            window.location.href = '/login';
        }

        // 时间格式函数 (主图/副图共用): 数据为伪UTC时间戳, getUTC* 读出即北京时间
        function chartTimeFormatter(time) {
            if (time && typeof time === 'object' && time.year) {
                return `${time.year}-${String(time.month).padStart(2, '0')}-${String(time.day).padStart(2, '0')}`;
            }
            const d = new Date(time * 1000);
            const p = n => String(n).padStart(2, '0');
            if (currentPeriod === '1d') {
                return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
            }
            return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
        }

        function chartTickMarkFormatter(time, tickMarkType, locale) {
            if (time && typeof time === 'object' && time.year) {
                return `${time.year}-${String(time.month).padStart(2, '0')}-${String(time.day).padStart(2, '0')}`;
            }
            const d = new Date(time * 1000);
            const p = n => String(n).padStart(2, '0');
            // 分时模式: 仅在每个交易阶段的开始/结束时间出刻度 (日盘首个点标日期)
            if (currentPeriod === 'intraday' && intradayTickLabels) {
                return intradayTickLabels.get(time) || '';
            }
            if (tickMarkType === 0) return `${d.getUTCFullYear()}年`;
            if (tickMarkType === 1) return `${p(d.getUTCMonth() + 1)}月`;
            if (tickMarkType === 2) return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
            return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
        }

        // 主图K线: 在当前可见范围内标注摆动高低点 (高点标最高价, 低点标最低价, 随视图动态重算)
        let lastPivotKey = '';
        function updatePivotMarkers() {
            if (!candleSeries || currentPeriod === 'intraday') return;
            const data = candleSeries.data();
            if (!data.length) return;
            const r = chart.timeScale().getVisibleLogicalRange();
            if (!r) return;
            const key = Math.round(r.from) + '_' + Math.round(r.to) + '_' + data.length;
            if (key === lastPivotKey) return;
            lastPivotKey = key;

            const L = 6;   // 摆动判定: 左右各 6 根K线 (窗口越大标注越少、级别越高)
            const fromI = Math.max(L, Math.ceil(r.from));
            const toI = Math.min(data.length - 1 - L, Math.floor(r.to));
            const markers = [];
            for (let i = fromI; i <= toI; i++) {
                let isHigh = true, isLow = true;
                for (let j = i - L; j <= i + L && (isHigh || isLow); j++) {
                    if (j === i) continue;
                    if (data[j].high >= data[i].high) isHigh = false;
                    if (data[j].low <= data[i].low) isLow = false;
                }
                if (isHigh) markers.push({ time: data[i].time, position: 'aboveBar', color: '#ef4444', shape: 'arrowDown', text: data[i].high.toFixed(priceDecOf(currentSymbol)) });
                if (isLow) markers.push({ time: data[i].time, position: 'belowBar', color: '#10b981', shape: 'arrowUp', text: data[i].low.toFixed(priceDecOf(currentSymbol)) });
            }
            markers.sort((a, b) => a.time - b.time);
            candleSeries.setMarkers(markers);
        }

        function intradayAutoscale() {
            if (intradayBaseline === null || intradayPriceMin === null) return null;
            const b = intradayBaseline;
            const logical = chart?.timeScale().getVisibleLogicalRange();
            const zoomed = intradayRange && logical &&
                logical.to - logical.from < intradayRange.to - intradayRange.from - 1;
            const visible = zoomed ? chart.timeScale().getVisibleRange() : null;
            if (visible) {
                const prices = intradayPoints.filter(point => point.value !== undefined &&
                    point.time >= visible.from && point.time <= visible.to).map(point => point.value);
                prices.push(...aiSessionVisiblePrices(visible));
                if (prices.length) {
                    const low = Math.min(...prices), high = Math.max(...prices);
                    const padding = Math.max((high - low) * 0.06, Math.abs(b) * 0.00001, 0.01);
                    return { priceRange: { minValue: low - padding, maxValue: high + padding } };
                }
            }
            const prices = aiSessionVisiblePrices();
            const half = Math.max(
                Math.abs(intradayPriceMin - b), Math.abs(intradayPriceMax - b),
                ...prices.map(price => Math.abs(price - b)), 0.01) * 1.06;
            return { priceRange: { minValue: b - half, maxValue: b + half } };
        }

        function initChart() {
            const container = document.getElementById('chart-wrapper');
            const subEl = document.getElementById('subchart-wrapper');
            chart = LightweightCharts.createChart(container, {
                layout: {
                    background: { color: '#1b1e24' },
                    textColor: '#8492a6',
                },
                grid: {
                    vertLines: { color: '#2b303c' },
                    horzLines: { color: '#2b303c' },
                },
                localization: {
                    locale: 'zh-CN',
                    timeFormatter: chartTimeFormatter,
                },
                timeScale: {
                    timeVisible: true,
                    secondsVisible: false,
                    tickMarkFormatter: chartTickMarkFormatter,
                },
                // 十字光标: 自由移动 (非磁吸), 虚线样式
                crosshair: {
                    mode: LightweightCharts.CrosshairMode.Magnet,
                    vertLine: { color: '#6b7480', width: 1, style: LightweightCharts.LineStyle.Solid, labelBackgroundColor: '#2a2e37' },
                    horzLine: { color: '#6b7480', width: 1, style: LightweightCharts.LineStyle.Solid, labelBackgroundColor: '#2a2e37' },
                },
                // 分时模式: 原生价格轴全部隐藏, 左右刻度改由覆盖层画在图内 (K线模式仍用右轴)
                leftPriceScale: { visible: false, scaleMargins: { top: 0.08, bottom: 0.05 } },
            });

            candleSeries = chart.addCandlestickSeries({
                upColor: '#ef4444',
                downColor: '#10b981',
                borderUpColor: '#ef4444',
                borderDownColor: '#10b981',
                wickUpColor: '#ef4444',
                wickDownColor: '#10b981',
            });
            // K线区抬升
            candleSeries.priceScale().applyOptions({
                scaleMargins: { top: 0.08, bottom: 0.05 },
            });

            ema20Series = chart.addLineSeries({
                color: '#facc15',
                lineWidth: 1.5,
                priceLineVisible: false,
            });

            ema60Series = chart.addLineSeries({
                color: '#a855f7',
                lineWidth: 1.5,
                priceLineVisible: false,
            });

            // 分时默认以昨结为中轴；滚轮放大后按可见量价及 AI 价位调整价格轴
            intradaySeries = chart.addLineSeries({
                color: '#facc15',
                lineWidth: 2,
                visible: false,
                priceLineVisible: false,
                // 分时十字线由覆盖层绘制; 原生磁吸圆点会在行情重绘时随 Y 轴重映射漂移
                crosshairMarkerVisible: false,
                autoscaleInfoProvider: intradayAutoscale,
            });
            const aiLineOptions = {
                lineWidth: 1, visible: false, priceLineVisible: false,
                lastValueVisible: false, crosshairMarkerVisible: false, priceScaleId: 'left',
                autoscaleInfoProvider: intradayAutoscale,
            };
            aiSessionReferenceSeries = chart.addLineSeries({
                ...aiLineOptions, color:'#cbd5e1', lineStyle:LightweightCharts.LineStyle.Dashed,
            });
            aiSessionTargetSeries = chart.addLineSeries({
                ...aiLineOptions, color:'#34d399', lineWidth:2, lineStyle:LightweightCharts.LineStyle.Dashed,
            });
            aiSessionInvalidationSeries = chart.addLineSeries({
                ...aiLineOptions, color:'#fb7185', lineWidth:2, lineStyle:LightweightCharts.LineStyle.Dotted,
            });
            aiSessionTriggerSeries = chart.addLineSeries({
                ...aiLineOptions, color:'#f59e0b', lineStyle:LightweightCharts.LineStyle.Dashed,
            });

            // K线模式: 可视范围锁定在数据范围内, 左右两侧都不允许拖出数据边界
            let clamping = false;
            chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
                if (clamping || !range) return;
                if (currentPeriod === 'intraday') {
                    if (!intradayRange) return;
                    const width = range.to - range.from;
                    const fullWidth = intradayRange.to - intradayRange.from;
                    let next = null;
                    if (width >= fullWidth) next = intradayRange;
                    else if (range.from < intradayRange.from - 0.5)
                        next = { from: intradayRange.from, to: intradayRange.from + width };
                    else if (range.to > intradayRange.to + 0.5)
                        next = { from: intradayRange.to - width, to: intradayRange.to };
                    if (next && (Math.abs(next.from - range.from) > 0.5 || Math.abs(next.to - range.to) > 0.5)) {
                        clamping = true;
                        chart.timeScale().setVisibleLogicalRange(next);
                        clamping = false;
                    }
                    return;
                }
                const dataLen = candleSeries ? candleSeries.data().length : 0;
                if (!dataLen) return;
                if (range.from < 0 || range.to > dataLen - 1) {
                    clamping = true;
                    const width = range.to - range.from;
                    if (width >= dataLen - 1) {
                        chart.timeScale().setVisibleLogicalRange({ from: 0, to: dataLen - 1 });
                    } else if (range.from < 0) {
                        chart.timeScale().setVisibleLogicalRange({ from: 0, to: width });
                    } else {
                        chart.timeScale().setVisibleLogicalRange({ from: dataLen - 1 - width, to: dataLen - 1 });
                    }
                    clamping = false;
                }
            });

            // 可视范围变化时重算摆动高低点标注和分时图内刻度
            chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
                if (r) updatePivotMarkers();
                try { schedulePctAxis(); scheduleSubGrid(); } catch (e) {}
            });

            // 副图: VOL/MACD/OI/KDJ/DMI 独立窗格, 时间轴与主图双向同步
            subChart = LightweightCharts.createChart(subEl, {
                layout: {
                    background: { color: '#1b1e24' },
                    textColor: '#8492a6',
                },
                grid: {
                    vertLines: { color: '#2b303c' },
                    horzLines: { color: '#2b303c' },
                },
                localization: {
                    locale: 'zh-CN',
                    timeFormatter: chartTimeFormatter,
                },
                timeScale: {
                    timeVisible: true,
                    secondsVisible: false,
                    tickMarkFormatter: chartTickMarkFormatter,
                },
                crosshair: {
                    mode: LightweightCharts.CrosshairMode.Magnet,
                    vertLine: { color: '#6b7480', width: 1, style: LightweightCharts.LineStyle.Solid, labelBackgroundColor: '#2a2e37' },
                    horzLine: { color: '#6b7480', width: 1, style: LightweightCharts.LineStyle.Solid, labelBackgroundColor: '#2a2e37' },
                },
                rightPriceScale: { scaleMargins: { top: 0.15, bottom: 0.05 } },
                handleScroll: true,
                handleScale: true,
            });
            // 副图覆盖层: 外边框 + 分时模式下与主图对齐的网格线 (画布会盖住 CSS 边框, 故用覆盖层绘制)
            subEl.style.position = 'relative';
            const subFrame = document.createElement('div');
            subFrame.id = 'sub-frame';
            subFrame.style.cssText = 'position:absolute; top:0; left:0; right:0; bottom:0; overflow:hidden; pointer-events:none; display:none; z-index:3; font-family:monospace; font-size:11px; letter-spacing:0.2px;';
            subEl.appendChild(subFrame);

            // 时间轴双向同步，分时放大/拖动时主副图保持对齐
            let syncingRange = false;
            chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
                if (syncingRange || !r) return;
                syncingRange = true;
                try { subChart.timeScale().setVisibleLogicalRange(chart.timeScale().getVisibleLogicalRange() || r); } catch (e) {}
                syncingRange = false;
            });
            subChart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
                // 视口变化 → 坐标区间变化 → 重算 Y 轴刻度 (含分时模式, 但当前分支仅用于双向同步)
                scheduleSubGrid();
                if (syncingRange || !r) return;
                syncingRange = true;
                try {
                    chart.timeScale().setVisibleLogicalRange(r);
                    if (currentPeriod === 'intraday')
                        subChart.timeScale().setVisibleLogicalRange(chart.timeScale().getVisibleLogicalRange() || r);
                } catch (e) {}
                syncingRange = false;
            });

            // 十字线联动 + 悬停K线信息面板
            let syncingCross = false;
            // 分时留白时刻 (无成交的时段/未来时段): 无价格可标, 十字线/覆盖层/NaN 标签都应隐藏
            const intradayBlankTime = (t) => currentPeriod === 'intraday' && t !== undefined && t !== null
                && intradayValuedTimes.size > 0 && !intradayValuedTimes.has(t);
            let lastChartPointerMoveAt = 0;
            const markChartPointerMove = () => { lastChartPointerMoveAt = Date.now(); };
            // 鼠标是否真的悬在主图/副图内: 十字线回调也可能由另一图的联动或行情重绘异步触发, 需据此区分
            let pointerOverMain = false;
            let pointerOverSub = false;
            const clearChartHover = () => { if (currentPeriod === 'intraday') renderOhlcLegend(undefined); };
            container.addEventListener('pointermove', markChartPointerMove, true);
            subEl.addEventListener('pointermove', markChartPointerMove, true);
            container.addEventListener('pointerenter', () => { pointerOverMain = true; }, true);
            container.addEventListener('pointerleave', () => { pointerOverMain = false; clearChartHover(); });
            subEl.addEventListener('pointerenter', () => { pointerOverSub = true; }, true);
            subEl.addEventListener('pointerleave', () => { pointerOverSub = false; clearChartHover(); });
            const renderCrosshairLegend = time => {
                // 行情重绘也可能触发十字线回调；鼠标没动时保留原先悬停的分钟。
                if (currentPeriod === 'intraday' && hoveredOhlcTime !== null
                    && Date.now() - lastChartPointerMoveAt > 150) renderOhlcLegend();
                else renderOhlcLegend(time);
            };
            chart.subscribeCrosshairMove((param) => {
                if (intradayBlankTime(param && param.time)) {
                    try { chart.clearCrosshairPosition(); } catch (e) {}
                    try { subChart.clearCrosshairPosition(); } catch (e) {}
                    updateCrossChips(null);
                    renderCrosshairLegend(undefined);
                    return;
                }
                renderCrosshairLegend(param && param.time !== undefined ? param.time : undefined);
                if (syncingCross || !param || !param.time) { try { subChart.clearCrosshairPosition(); } catch (e) {} return; }
                const s = subSeriesList[0];
                if (!s) return;
                const v = subValueMap[param.time];
                // 该分钟无副图数值 (日盘留白区): 清除副图十字线, 避免残留旧位置造成偏移
                if (v === undefined) { try { subChart.clearCrosshairPosition(); } catch (e) {} return; }
                syncingCross = true;
                try { subChart.setCrosshairPosition(v, param.time, s); } catch (e) {}
                syncingCross = false;
            });
            subChart.subscribeCrosshairMove((param) => {
                // 鼠标不在副图上时, 本回调由主图联动/行情重绘触发 (异步, syncingCross 拦不住):
                // 不能清主图十字线/接管覆盖层, 否则悬停主图时十字线会自己消失或被带跑;
                // 鼠标已离开两个图时, 用空参数回调清理残留十字线与覆盖层
                if (!pointerOverSub) {
                    if (!pointerOverMain && (!param || !param.time)) {
                        try { chart.clearCrosshairPosition(); } catch (e) {}
                        try { updateCrossChips(null); } catch (e) {}
                    }
                    return;
                }
                if (syncingCross || !param || !param.time) { try { chart.clearCrosshairPosition(); } catch (e) {} try { updateCrossChips(null); } catch (e) {} return; }
                // 分时模式: 鼠标在副图内时清主图原生十字线, 改由覆盖层绘制主图联动十字线;
                // 鼠标在副图内的相对高度等比映射到主图, 主图 Y 轴横线随鼠标显示
                if (currentPeriod === 'intraday') {
                    try { chart.clearCrosshairPosition(); } catch (e) {}
                    if (intradayBlankTime(param.time)) { updateCrossChips(null); renderCrosshairLegend(undefined); return; }
                    renderCrosshairLegend(param.time);
                    let yMain = null;
                    if (param.point) {
                        const subH = subEl.clientHeight || 1;
                        const mainH = container.clientHeight;
                        yMain = Math.min(Math.max(param.point.y / subH, 0), 1) * mainH;
                    }
                    updateCrossChips({ time: param.time }, yMain);
                    return;
                }
                // K线模式: 用K线系列联动
                renderCrosshairLegend(param.time);
                const c = (candleSeries.data() || []).find(p => p.time === param.time);
                if (!c) { try { chart.clearCrosshairPosition(); } catch (e) {} return; }
                syncingCross = true;
                try { chart.setCrosshairPosition(c.close, param.time, candleSeries); } catch (e) {}
                syncingCross = false;
            });

            // 十字线: 分时模式左右读数；行情信息固定在图表外，不遮挡曲线
            chart.subscribeCrosshairMove((param) => {
                updateCrossChips(intradayBlankTime(param && param.time) ? null : param);
            });

            window.addEventListener('resize', () => {
                chart.resize(container.clientWidth, container.clientHeight);
                subChart.resize(subEl.clientWidth, subEl.clientHeight);
                schedulePctAxis();
                scheduleSubGrid();
                updateAISessionChoiceArrows('ai-session-line-times');
                updateAISessionChoiceArrows('ai-session-line-model-list');
            });

            // 分时模式刻度覆盖层 (自绘: 左侧价格 / 右侧百分比, 红涨绿跌)
            container.style.position = 'relative';
            const pctAxis = document.createElement('div');
            pctAxis.id = 'pct-axis';
            pctAxis.style.cssText = 'position:absolute; top:0; left:0; right:0; bottom:0; overflow:hidden; pointer-events:none; display:none; z-index:4; font-family:monospace; font-size:10px;';
            container.appendChild(pctAxis);
            // 十字光标读数层 (水平线左端价格 / 右端涨跌幅)
            const crossLayer = document.createElement('div');
            crossLayer.id = 'cross-layer';
            crossLayer.style.cssText = 'position:absolute; top:0; left:0; right:0; bottom:0; overflow:hidden; pointer-events:none; display:none; z-index:6; font-family:monospace; font-size:10px;';
            container.appendChild(crossLayer);
            // 初始创建时画布可能按含边框尺寸测量 (比内容宽 2px 盖住右边框), 立即按内容尺寸校正
            requestAnimationFrame(() => {
                chart.resize(container.clientWidth, container.clientHeight);
                subChart.resize(subEl.clientWidth, subEl.clientHeight);
            });
        }

        function intradaySessions(symbol, day, bars) {
            const product = (symbol.match(/^[A-Za-z]+/) || [''])[0].toUpperCase();
            const midnight = Date.parse(`${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}T00:00:00Z`) / 1000;
            const at = (base, hm) => base + Number(hm.slice(0, 2)) * 3600 + Number(hm.slice(3)) * 60;
            const sessions = [];
            const add = (base, start, end) => {
                const session = { start: at(base, start), end: at(base, end) + (end < start ? 86400 : 0), startLabel: start, endLabel: end };
                sessions.push(session);
                return session;
            };
            const barTimes = bars.map(b => Date.parse(b.bar_time.length === 19 ? b.bar_time + 'Z' : b.bar_time) / 1000);
            const hasBar = s => barTimes.some(t => t >= s.start && t < s.end);
            if (['IF', 'IH', 'IC', 'IM', 'T', 'TF', 'TS', 'TL'].includes(product)) {
                add(midnight, '09:30', '11:30');
                add(midnight, '13:00', ['T', 'TF', 'TS', 'TL'].includes(product) ? '15:15' : '15:00');
                return sessions;
            }
            const night0230 = ['SC', 'AU', 'AG'];
            const night0100 = ['CU', 'AL', 'ZN', 'PB', 'NI', 'SN', 'SS', 'BC', 'AO'];
            const night2300 = ['RB', 'HC', 'FU', 'BU', 'RU', 'SP', 'BR', 'LU', 'NR', 'A', 'B', 'C', 'CS', 'M', 'Y', 'P', 'L', 'V', 'PP', 'J', 'JM', 'I', 'EG', 'EB', 'PG', 'SR', 'CF', 'CY', 'OI', 'RM', 'TA', 'MA', 'FG', 'SA', 'PF', 'PX', 'SH'];
            const nightEnd = night0230.includes(product) ? '02:30' : night0100.includes(product) ? '01:00' : night2300.includes(product) ? '23:00' : null;
            if (nightEnd) {
                // 优先使用当日夜盘实际日期，避免把周五夜盘放到周日。
                const nightBar = bars.find(b => b.bar_time.slice(11, 13) >= '21' && Date.parse(b.bar_time.slice(0, 10) + 'T00:00:00Z') / 1000 < midnight);
                let nightDate = nightBar ? Date.parse(nightBar.bar_time.slice(0, 10) + 'T00:00:00Z') / 1000 : midnight - 86400;
                if (!nightBar) while ([0, 6].includes(new Date(nightDate * 1000).getUTCDay())) nightDate -= 86400;
                const nightSession = add(nightDate, '21:00', nightEnd);
                if (!hasBar(nightSession)) sessions.pop();
            }
            add(midnight, '09:00', '10:15');
            add(midnight, '10:30', '11:30');
            add(midnight, '13:30', '15:00');
            return sessions;
        }

        // 休市两侧在压缩时间轴上相邻，只绘制一条边界线和一个组合标签。
        function intradayTicks(ts) {
            const ticks = [];
            for (const [t, label] of intradayTickLabels || []) {
                const x = ts.timeToCoordinate(t);
                if (x !== null && x >= -1) ticks.push({ x: Math.max(x, 0), label });
            }
            ticks.sort((a, b) => a.x - b.x);
            const merged = [];
            for (const tick of ticks) {
                const prev = merged[merged.length - 1];
                if (prev && tick.x - prev.x < 8) {
                    prev.x = (prev.x + tick.x) / 2;
                    prev.label = tick.label; // 休市交界优先显示后一个时段的开盘时间
                } else merged.push({ ...tick });
            }
            return merged;
        }

        let pctAxisRaf = null;
        function schedulePctAxis() {
            if (pctAxisRaf !== null) return;
            pctAxisRaf = requestAnimationFrame(() => {
                pctAxisRaf = null;
                renderPctAxis();
            });
        }

        // 分时模式图内刻度: 左侧价格 / 右侧涨跌幅 (以昨结为 0%), 红正绿负
        function renderPctAxis() {
            const el = document.getElementById('pct-axis');
            if (!el) return;
            if (currentPeriod !== 'intraday' || !intradayBaseline || intradayPriceMin === null) { el.style.display = 'none'; return; }
            const S = intradayBaseline;
            const toPct = p => (p - S) / S * 100;
            const height = el.clientHeight;
            const width = el.clientWidth;
            const top = toPct(intradaySeries.coordinateToPrice(0));
            const bottom = toPct(intradaySeries.coordinateToPrice(height));
            if (!Number.isFinite(top) || !Number.isFinite(bottom)) return;
            const step = niceStep(top - bottom, 8);
            const digits = Math.max(2, Math.min(5, Math.ceil(-Math.log10(step))));
            const priceDigits = Math.max(priceDecOf(currentSymbol),
                Math.min(4, Math.ceil(-Math.log10(step * S / 100))));
            const fmtP = p => p.toFixed(priceDecOf(currentSymbol) === 0 ? 0 : priceDigits);
            let html = '';
            for (let k = Math.ceil(bottom / step); k <= Math.floor(top / step); k++) {
                let pct = k * step;
                if (Math.abs(pct) < step * 0.001) pct = 0;
                const price = S * (1 + pct / 100);
                const y = intradaySeries.priceToCoordinate(price);
                if (y === null || y < 8 || y > height - 30) continue;
                const color = pct > 0 ? '#ef4444' : (pct < 0 ? '#10b981' : '#8492a6');
                const txt = (pct > 0 ? '+' : '') + pct.toFixed(digits) + '%';
                // 背景横向网格线: 与左右价格/百分比刻度对齐
                html += `<div style="position:absolute; left:0; right:0; top:${y.toFixed(1)}px; border-top:1px solid #2b303c;"></div>`;
                html += `<div style="position:absolute; right:5px; top:${(y - 7).toFixed(1)}px; color:${color}; line-height:14px; text-shadow:0 0 3px #1b1e24;">${txt}</div>`;
                // 0% 处 (昨结基准): 数字显示在左缘, 不带文字标签
                if (pct !== 0) {
                    html += `<div style="position:absolute; left:5px; top:${(y - 7).toFixed(1)}px; color:#8492a6; line-height:14px; text-shadow:0 0 3px #1b1e24;">${fmtP(price)}</div>`;
                } else {
                    html += `<div style="position:absolute; left:5px; top:${(y - 7).toFixed(1)}px; color:#8492a6; line-height:14px; text-shadow:0 0 3px #1b1e24;">${fmtP(S)}</div>`;
                }
            }
            // 底部时间轴: 在各阶段开收盘时间画竖网格线与刻度
            // 紧贴左缘的开盘刻度不画线 (容器边框线即最左线), 只保留标签
            if (intradayTickLabels) {
                const ts = chart.timeScale();
                let labelRight = -4;
                for (const { x, label } of intradayTicks(ts)) {
                    // 紧贴左缘/右缘的刻度不画线 (容器边框即最外侧线), 只保留标签
                    if (x >= 8 && x <= width - 8) html += `<div style="position:absolute; left:${x.toFixed(1)}px; top:0; bottom:0; border-left:1px solid #2b303c;"></div>`;
                    const labelLines = label.split('/');
                    const labelWidth = Math.max(...labelLines.map(text => text.length)) * 6 + 4;
                    const lx = Math.min(Math.max(x - labelWidth / 2, 2), width - labelWidth - 2);
                    if (lx < labelRight + 4) continue;
                    labelRight = lx + labelWidth;
                    html += `<div style="position:absolute; left:${lx.toFixed(1)}px; bottom:3px; width:${labelWidth}px; text-align:center; color:#8492a6; line-height:12px; text-shadow:0 0 3px #1b1e24;">${labelLines.join('<br>')}</div>`;
                }
            }
            el.innerHTML = html;
            el.style.display = 'block';
        }

        // 副图 Y 轴刻度: 取「nice number」步长 (1/2/2.5/5/10 x 10^n), 使刻度值为整数且间隔均匀
        function niceStep(span, targetCount) {
            if (!(span > 0) || !(targetCount > 0)) return 1;
            const raw = span / targetCount;
            const mag = Math.pow(10, Math.floor(Math.log10(raw)));
            const norm = raw / mag;
            let mult;
            if (norm <= 1) mult = 1;
            else if (norm <= 2) mult = 2;
            else if (norm <= 2.5) mult = 2.5;
            else if (norm <= 5) mult = 5;
            else mult = 10;
            return mult * mag;
        }

        // 副图数值格式化: 按量级自适应小数位, 千位加分隔符便于对照读数
        function fmtSubAxis(v, step) {
            // 浮点累加误差归零: 步长 0.05 累加可能产生 3.5e-17 之类残差
            if (Math.abs(v) < step * 1e-6) v = 0;
            const dec = step >= 1 ? 0 : (step >= 0.1 ? 1 : 2);
            const body = Math.abs(v).toFixed(dec);
            // 千位分隔 (整数部分每 3 位加逗号)
            const parts = body.split('.');
            const grouped = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
            const text = parts[1] ? grouped + '.' + parts[1] : grouped;
            // 归零后的值不再显示负号
            return (v < 0 ? '-' : '') + text;
        }

        // 副图刻度刷新入口: 同一帧内多次触发只重绘一次 (缩放/滚动时高频回调)
        let subGridRaf = null;
        function scheduleSubGrid() {
            if (subGridRaf !== null) return;
            subGridRaf = requestAnimationFrame(() => {
                subGridRaf = null;
                try { renderSubGrid(); } catch (e) {}
            });
        }

        // 副图: 外边框 + 与主图对齐的时间竖网格线 + 左缘数值刻度与横向网格线
        function renderSubGrid() {
            const el = document.getElementById('sub-frame');
            if (!el) return;
            if (!subChart) { el.style.display = 'none'; return; }
            const isIntraday = currentPeriod === 'intraday';
            // 尺寸读取: clientHeight 在某些布局状态下为 0, 回退到父容器尺寸, 保证刻度不因测量失败而消失
            let h = el.clientHeight, w = el.clientWidth;
            if ((!h || !w) && el.parentElement) {
                h = h || el.parentElement.clientHeight;
                w = w || el.parentElement.clientWidth;
            }
            if (!h || !w) { el.style.display = 'none'; return; }
            let html = '<div style="position:absolute; top:0; left:0; right:0; bottom:0; border:1px solid var(--border-color);"></div>';
            // 竖向: 阶段边界时间 (与主图对齐); 仅分时模式有阶段网格
            if (isIntraday && intradayTickLabels) {
                const ts = subChart.timeScale();
                for (const { x } of intradayTicks(ts)) {
                    // 紧贴左/右缘的刻度不画线 (边框即最外侧线)
                    if (x >= 8 && x <= w - 8) html += `<div style="position:absolute; left:${x.toFixed(1)}px; top:0; bottom:0; border-left:1px solid #2b303c;"></div>`;
                }
            }

            // 横向 Y 轴刻度: 以首个系列的 priceToCoordinate 反算可视区间, 用 nice step 均匀铺刻度
            const s = subSeriesList[0];
            let plotted = false;
            if (s) {
                // 反算当前视口对应的数值上下界 (坐标 0 在顶部, h 在底部)
                let vTop = null, vBot = null;
                try { vTop = s.coordinateToPrice(0); vBot = s.coordinateToPrice(h); } catch (e) {}
                if (vTop !== null && vBot !== null && isFinite(vTop) && isFinite(vBot) && vTop > vBot) {
                    const span = vTop - vBot;
                    const labelLeft = 5;
                    const minGap = 22;    // 相邻刻度最小像素间距 (防重叠)
                    const edgePad = 12;   // 刻度线距面板上/下缘的最小留白
                    // 文字半高: 保证标签完整落在面板内
                    const clampText = edgePad;
                    // 目标刻度数: 按面板高度自适应, 保证相邻刻度间距 >= minGap
                    const target = Math.max(2, Math.min(6, Math.floor(h / 26)));
                    // 边界裁剪会损失刻度, 故多算一档候选 (step 取较细一级), 让面板尽量填满
                    let step = niceStep(span, target);
                    const cand = () => {
                        const out = [];
                        const first = Math.ceil(vBot / step) * step;
                        for (let v = first; v <= vTop + step * 1e-6; v += step) out.push(v);
                        return out;
                    };
                    // 用当前 step 试算可用刻度数 (落在留白区间内); 少于目标数则改用更细一档
                    const countFit = (st) => {
                        let n = 0, lastY = null;
                        const first = Math.ceil(vBot / st) * st;
                        for (let v = first; v <= vTop + st * 1e-6; v += st) {
                            const y = h - (v - vBot) / span * h;
                            if (y < clampText || y > h - clampText) continue;
                            if (lastY !== null && Math.abs(y - lastY) < minGap) continue;
                            lastY = y; n++;
                        }
                        return n;
                    };
                    const finer = niceStep(span, target * 2);
                    if (finer < step && countFit(finer) > countFit(step)) step = finer;
                    // 首个 >= vBot 的刻度, 沿步长向上铺到 vTop
                    const ticks = cand();
                    // 文字宽度预算: 给左侧留出刻度标签空间, 避免数字压住图形
                    let maxLabelW = 0;
                    const labelHtmls = ticks.map(v => {
                        const t = fmtSubAxis(v, step);
                        maxLabelW = Math.max(maxLabelW, t.length * 6.2);
                        return t;
                    });
                    // 文字底色遮罩: 让刻度数字浮于柱体之上时仍清晰可读 (不可用实底色块遮住图形)
                    const maskW = Math.min(maxLabelW + 8, w * 0.34);
                    let lastY = null;
                    ticks.forEach((v, idx) => {
                        let y = null;
                        try { y = s.priceToCoordinate(v); } catch (e) {}
                        if (y === null || y === undefined || !isFinite(y)) return;
                        // 超出绘图区域 (含文字半高) 的刻度丢弃, 防止数字溢出面板
                        if (y < clampText || y > h - clampText) return;
                        // 与上一刻度过近则跳过, 防止数字重叠
                        if (lastY !== null && Math.abs(y - lastY) < minGap) return;
                        lastY = y;
                        // 横向网格线: 分时模式满宽 (与主图阶段网格对齐); K线模式只画左侧引导段, 避免与图表自带横网格叠乱
                        if (isIntraday) {
                            html += `<div style="position:absolute; left:0; right:0; top:${y.toFixed(1)}px; border-top:1px solid #2b303c;"></div>`;
                        } else {
                            html += `<div style="position:absolute; left:0; width:${(maskW + 6).toFixed(0)}px; top:${y.toFixed(1)}px; border-top:1px solid #343a47;"></div>`;
                        }
                        // 左对齐刻度文字, 垂直居中于刻度线; 双层 text-shadow 保证压在柱体上也清晰
                        html += `<div style="position:absolute; left:${labelLeft}px; top:${(y - 6).toFixed(1)}px; width:${maskW.toFixed(0)}px; color:#a8b3c2; line-height:12px; white-space:nowrap; text-shadow:0 0 3px #1b1e24,0 0 4px #1b1e24,0 0 6px #1b1e24;">${labelHtmls[idx]}</div>`;
                    });
                    plotted = ticks.length > 0;
                }
            }
            // 兜底: priceToCoordinate 不可用 (系列未就绪) 时, 至少给出 最高/中/最低 三个参考值
            if (!plotted && s) {
                const data = (s.data() || []).filter(p => p.value !== undefined);
                if (data.length) {
                    let vmax = 0, vmin = 0;
                    for (const p of data) { const v = p.value; if (v > vmax) vmax = v; if (v < vmin) vmin = v; }
                    const low = vmin < 0 ? vmin : 0;
                    const mid = (vmax + low) / 2;
                    const fmt = v => fmtSubAxis(v, Math.abs(vmax - low) / 3);
                    html += `<div style="position:absolute; left:5px; top:3px; color:#8492a6; line-height:12px; white-space:nowrap; text-shadow:0 0 3px #1b1e24;">${fmt(vmax)}</div>`;
                    html += `<div style="position:absolute; left:5px; top:${((h - 12) / 2).toFixed(1)}px; color:#8492a6; line-height:12px; white-space:nowrap; text-shadow:0 0 3px #1b1e24;">${fmt(mid)}</div>`;
                    html += `<div style="position:absolute; left:5px; top:${(h - 15).toFixed(1)}px; color:#8492a6; line-height:12px; white-space:nowrap; text-shadow:0 0 3px #1b1e24;">${fmt(low)}</div>`;
                }
            }
            el.innerHTML = html;
            el.style.display = 'block';
        }

        // 分时基准价: 最新交易日优先用行情快照的昨结 (真实结算价), 历史日期回退前一交易日收盘
        async function getBaseline(sym, day) {
            const key = sym + '_' + day;
            if (prevCloseCache[key] !== undefined) return prevCloseCache[key];
            let v = null;
            try {
                const q = await (await fetch(`/api/v1/quotes/${sym}`)).json();
                if (q && q.trading_day === day && q.prev_settle) v = parseFloat(q.prev_settle);
            } catch (e) {}
            if (v === null) {
                try {
                    const res = await fetch(`/api/v1/bars/${sym}?period=1d&limit=10`);
                    if (res.ok) {
                        const bars = (await res.json()).bars || [];
                        for (let i = bars.length - 1; i >= 0; i--) {
                            if (bars[i].trading_day < day) { v = parseFloat(bars[i].close_price); break; }
                        }
                    }
                } catch (e) {}
            }
            prevCloseCache[key] = v;
            return v;
        }

        // 十字光标: 竖线吸附分钟点 (与时间一致); 横线跟随鼠标纵向位置,
        // 左端显示横线位置对应的价格, 右端为相对昨结的涨跌幅 (红涨绿跌);
        // yOverride: 鼠标在副图内时, 按副图内的相对高度映射出主图的横线位置
        // 触摸设备上移动手指时 param.point.y 不可靠(可能固定在图表顶部),
        // 横线读数改为磁吸"所选分钟在曲线上的价格位置"; 桌面鼠标仍自由跟随
        function crossChipY(param) {
            if (param && param.point && !window.matchMedia('(pointer: coarse)').matches) {
                return param.point.y;
            }
            if (param && param.time !== undefined) {
                const v = intradayValueByTime.get(param.time);
                if (v !== undefined) {
                    try {
                        const y = intradaySeries.priceToCoordinate(v);
                        if (y !== null && y !== undefined) return y;
                    } catch (e) {}
                }
            }
            return param && param.point ? param.point.y : null;
        }

        function updateCrossChips(param, yOverride) {
            const el = document.getElementById('cross-layer');
            if (!el) return;
            if (currentPeriod !== 'intraday' || !param || intradayBaseline === null || !intradaySeries) { el.style.display = 'none'; return; }
            const chip = 'background:#2a2e37; border:1px solid #4c525c; border-radius:3px; padding:0 4px; line-height:14px;';
            const fmtP = p => p.toFixed(priceDecOf(currentSymbol) === 0 ? 0 : (p >= 1000 ? 1 : 2));
            let html = '';
            // 横线: 触摸端磁吸所选分钟价格, 桌面端跟随鼠标; 悬停副图时 y 由副图等比映射
            const y = (yOverride !== null && yOverride !== undefined) ? yOverride : crossChipY(param);
            if (y !== null && y !== undefined) {
                html += `<div style="position:absolute; left:0; right:0; top:${(+y).toFixed(1)}px; border-top:1px solid #6b7480;"></div>`;
                const price = intradaySeries.coordinateToPrice(+y);
                // coordinateToPrice 在数据/量程未就绪的瞬间可能返回 NaN, 非有限数不出牌
                if (Number.isFinite(price) && intradayBaseline) {
                    const pct = (price - intradayBaseline) / intradayBaseline * 100;
                    const pctN = Math.abs(pct) < 0.005 ? 0 : pct;
                    const col = pctN > 0 ? '#ef4444' : (pctN < 0 ? '#10b981' : '#8492a6');
                    html += `<div style="position:absolute; left:2px; top:${(+y - 8).toFixed(1)}px; ${chip} color:#e2e8f0;">${fmtP(price)}</div>` +
                        `<div style="position:absolute; right:2px; top:${(+y - 8).toFixed(1)}px; ${chip} color:${col};">${(pctN > 0 ? '+' : '') + pctN.toFixed(2)}%</div>`;
                }
            }
            // 竖线读数: 吸附到的分钟时间 (原生竖线已磁吸到该点)
            if (param.time) {
                let x = null;
                try { x = chart.timeScale().timeToCoordinate(param.time); } catch (e) {}
                if (x !== null && x !== undefined) {
                    x = Math.max(x, 0);
                    const d = new Date(param.time * 1000);
                    const tTxt = `${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')} ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
                    html += `<div style="position:absolute; left:${x.toFixed(1)}px; top:0; bottom:0; border-left:1px solid #6b7480;"></div>`;
                    html += `<div style="position:absolute; left:${Math.max(2, x - 34).toFixed(1)}px; bottom:2px; ${chip} color:#e2e8f0;">${tTxt}</div>`;
                }
            }
            el.innerHTML = html;
            el.style.display = 'block';
        }

        // ── 修改分组 (右键菜单入口) ──
        // 两种模式:
        //   替换(默认) - 勾选即为唯一归属, 点选新分组立刻完成"改到该分组"
        //   多组       - 在保留原归属的基础上叠加/取消, 用于一合约同时属于多个分组
        let groupEditTarget = null;      // 当前编辑的合约代码
        let groupEditSelected = [];      // 已勾选的分组名
        let groupEditExtras = [];        // 本次会话新增的候选分组 (尚未保存到后端)
        let groupEditMode = 'replace';   // 'replace' | 'multi'
        let groupEditOriginal = [];      // 打开时的原始归属 (用于模式切换时回退)

        // 弹窗内所有候选分组 = 后端已知分组 ∪ 本次新增候选, 去重且保持先后顺序
