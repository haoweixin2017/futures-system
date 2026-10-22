        async function switchToMain() {
            await switchCardToMain(currentSymbol);
        }

        // ---------------- 副图指标: VOL / MACD / OI / KDJ / DMI ----------------
        function emaOf(vals, n) {
            const k = 2 / (n + 1);
            const out = [vals[0]];
            for (let i = 1; i < vals.length; i++) out.push(vals[i] * k + out[i - 1] * (1 - k));
            return out;
        }

        function buildValueMap(times, vals) {
            const m = {};
            times.forEach((t, i) => {
                if (vals[i] !== null && vals[i] !== undefined && !isNaN(vals[i])) m[t] = vals[i];
            });
            return m;
        }

        function renderSubChart() {
            if (!subChart) return;
            subSeriesList.forEach(s => { try { subChart.removeSeries(s); } catch (e) {} });
            subSeriesList = [];
            subValueMap = {};

            const src = (currentPeriod === 'intraday') ? intradayBars : barsData;
            if (!src.length) return;

            const toTime = b => Math.floor(Date.parse(b.bar_time.length === 19 ? b.bar_time + 'Z' : b.bar_time) / 1000);
            const times = src.map(toTime);
            const opens = src.map(b => parseFloat(b.open_price));
            const highs = src.map(b => parseFloat(b.high_price));
            const lows = src.map(b => parseFloat(b.low_price));
            const closes = src.map(b => parseFloat(b.close_price));
            // 成交量字段在 Redis/MySQL 序列化切换时可能是字符串；HistogramSeries 只应接收
            // 有限非负数，否则会出现柱体缺失或自动缩放异常。成交量单位保持为「手」，不做 K/M 换算。
            const vols = src.map(b => {
                let p=b.provenance_json || {};
                if(typeof p==='string'){try{p=JSON.parse(p);}catch(_){p={};}}
                if(b.volume==null || ['UNALLOCATED_GAP','UNVERIFIED_ALLOCATION'].includes(b.volume_quality || p.volume_quality)) return null;
                const v = Number(b.volume);
                return Number.isFinite(v) && v >= 0 ? Math.round(v) : null;
            });
            const ois = src.map(b => parseFloat(b.open_interest) || 0);

            // 主副图共用完整时间槽；缺失分钟也必须占据相同位置。
            const allTimes = currentPeriod === 'intraday' ? intradayPoints.map(p => p.time) : times;
            const sourceIndex = new Map(times.map((t, i) => [t, i]));
            const pad = arr => allTimes.map(t => arr[sourceIndex.get(t)]);

            // 分时模式纵向满格: 创建系列时立即设置边距 (须先于 setData, 否则自动缩放按旧边距计算)
            const applySubMargins = (s) => {
                try { s.priceScale().applyOptions(currentPeriod === 'intraday' ? { scaleMargins: { top: 0, bottom: 0 } } : { scaleMargins: { top: 0.15, bottom: 0.05 } }); } catch (e) {}
            };
            const line = (color, width) => {
                const s = subChart.addLineSeries({ color, lineWidth: width || 1, priceLineVisible: false });
                applySubMargins(s);
                subSeriesList.push(s);
                return s;
            };
            const hist = (options) => {
                const s = subChart.addHistogramSeries({
                    priceLineVisible: false,
                    lastValueVisible: false,
                    ...(options || {}),
                });
                applySubMargins(s);
                subSeriesList.push(s);
                return s;
            };
            // 项目统一规则：涨红、跌绿、平盘中性灰。不能用 >=，否则平盘柱会被误标为上涨。
            const volumeColor = (open, close) => {
                if (!Number.isFinite(open) || !Number.isFinite(close)) return 'rgba(132, 146, 166, 0.72)';
                if (close > open) return 'rgba(239, 68, 68, 0.75)';
                if (close < open) return 'rgba(16, 185, 129, 0.75)';
                return 'rgba(132, 146, 166, 0.72)';
            };
            const pts = vals => allTimes.map((t, i) =>
                (vals[i] === null || vals[i] === undefined || isNaN(vals[i])) ? { time: t } : { time: t, value: vals[i] });

            if (subIndicator === 'VOL') {
                // Keep legitimate large volumes at their actual height. Unknown is a gap.
                // 显式基线为 0：成交量柱始终从零轴起算，避免自动缩放或缺失分钟造成柱体
                // 悬浮；priceFormat 声明为整数「手」，防止字符串量在图表库内部产生非预期格式。
                const s = hist({ base: 0, priceFormat: { type: 'volume', precision: 0, minMove: 1 } });
                s.setData(allTimes.map(t => {
                    const i = sourceIndex.get(t);
                    return i === undefined || vols[i] == null ? { time: t } : {
                        time: t,
                        value: vols[i],
                        color: volumeColor(opens[i], closes[i]),
                    };
                }));
                // Cursor and column use the same verified volume values.
                subValueMap = buildValueMap(times, vols);
            } else if (subIndicator === 'MACD') {
                const e12 = emaOf(closes, 12), e26 = emaOf(closes, 26);
                const dif = e12.map((v, i) => v - e26[i]);
                const dea = emaOf(dif, 9);
                const h = dif.map((v, i) => (v - dea[i]) * 2);
                hist().setData(allTimes.map(t => { const i = sourceIndex.get(t); return i === undefined ? { time: t } : { time: t, value: h[i], color: h[i] >= 0 ? 'rgba(239, 68, 68, 0.75)' : 'rgba(16, 185, 129, 0.75)' }; }));
                line('#facc15', 1).setData(pts(pad(dif)));
                line('#38bdf8', 1).setData(pts(pad(dea)));
                subValueMap = buildValueMap(times, dif);
            } else if (subIndicator === 'OI') {
                line('#facc15', 2).setData(pts(pad(ois)));
                subValueMap = buildValueMap(times, ois);
            } else if (subIndicator === 'KDJ') {
                const n = 9;
                let k = 50, d = 50;
                const K = [], D = [], J = [];
                for (let i = 0; i < closes.length; i++) {
                    const hh = Math.max(...highs.slice(Math.max(0, i - n + 1), i + 1));
                    const ll = Math.min(...lows.slice(Math.max(0, i - n + 1), i + 1));
                    const rsv = hh === ll ? 50 : (closes[i] - ll) / (hh - ll) * 100;
                    k = k * 2 / 3 + rsv / 3;
                    d = d * 2 / 3 + k / 3;
                    K.push(k); D.push(d); J.push(3 * k - 2 * d);
                }
                line('#e2e8f0', 1).setData(pts(pad(K)));
                line('#facc15', 1).setData(pts(pad(D)));
                line('#a855f7', 1).setData(pts(pad(J)));
                subValueMap = buildValueMap(times, K);
            } else if (subIndicator === 'DMI') {
                const n = 14;
                const PDI = [null], MDI = [null], ADX = [null];
                let sTR = 0, sPDM = 0, sMDM = 0, adxPrev = null, dxSum = 0, dxCount = 0;
                for (let i = 1; i < closes.length; i++) {
                    const tr = Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1]));
                    const up = highs[i] - highs[i - 1];
                    const dn = lows[i - 1] - lows[i];
                    const pdm = (up > dn && up > 0) ? up : 0;
                    const mdm = (dn > up && dn > 0) ? dn : 0;
                    if (i <= n) { sTR += tr; sPDM += pdm; sMDM += mdm; }
                    else {
                        sTR = sTR - sTR / n + tr;
                        sPDM = sPDM - sPDM / n + pdm;
                        sMDM = sMDM - sMDM / n + mdm;
                    }
                    if (i < n) { PDI.push(null); MDI.push(null); ADX.push(null); continue; }
                    const p = sTR > 0 ? 100 * sPDM / sTR : 0;
                    const m = sTR > 0 ? 100 * sMDM / sTR : 0;
                    PDI.push(p); MDI.push(m);
                    const dx = (p + m) > 0 ? 100 * Math.abs(p - m) / (p + m) : 0;
                    if (adxPrev === null) {
                        dxSum += dx; dxCount++;
                        if (dxCount === n) { adxPrev = dxSum / n; ADX.push(adxPrev); } else ADX.push(null);
                    } else {
                        adxPrev = (adxPrev * (n - 1) + dx) / n;
                        ADX.push(adxPrev);
                    }
                }
                line('#facc15', 1).setData(pts(pad(PDI)));
                line('#38bdf8', 1).setData(pts(pad(MDI)));
                line('#a855f7', 1).setData(pts(pad(ADX)));
                subValueMap = buildValueMap(times, PDI);
            }

            // 分时模式: 纵向满格 (最高的柱/点刚好顶到面板顶部); 直接设置系列自身价格轴 (图表级设置对系列不生效)
            subSeriesList.forEach(ss => {
                try { ss.priceScale().applyOptions(currentPeriod === 'intraday' ? { scaleMargins: { top: 0, bottom: 0 } } : { scaleMargins: { top: 0.15, bottom: 0.05 } }); } catch (e) {}
            });

            updateSubIndicatorLegend();
        }

        function getSubIndicatorTitle() {
            const names = { VOL: '成交量', MACD: 'MACD(12,26,9)', OI: '持仓量', KDJ: 'KDJ(9,3,3)', DMI: 'DMI(14)' };
            return names[subIndicator] || subIndicator;
        }

        function updateSubIndicatorLegend(time) {
            const label = document.getElementById('sub-values');
            if (!label) return;
            const title = getSubIndicatorTitle();
            let t = time;
            if (t === undefined && currentPeriod === 'intraday' && hoveredOhlcTime !== null) t = hoveredOhlcTime;
            if (t === undefined || t === null) {
                const keys = Object.keys(subValueMap);
                if (!keys.length) { label.innerText = title; return; }
                t = keys[keys.length - 1];
            }
            const v = subValueMap[t];
            if (v === undefined || v === null || isNaN(v)) { label.innerText = title; return; }
            label.innerText = title + ' ' + (Number.isInteger(v) ? v : v.toFixed(2));
        }

        function switchSubIndicator(name) {
            subIndicator = name;
            document.querySelectorAll('.period-btn[data-sub]').forEach(b => {
                b.classList.toggle('sub-ind-active', b.dataset.sub === name);
            });
            renderSubChart();
            requestAnimationFrame(() => renderSubGrid());
        }

        // ---------------- 主图悬浮信息面板: 悬停K线显示 时间/开高低收/涨跌幅/量/持仓 ----------------
        function buildOhlcIndex(bars) {
            ohlcList = bars || [];
            const key = `${currentSymbol}_${currentPeriod}_${ohlcList[ohlcList.length - 1]?.trading_day || ''}`;
            if (key !== ohlcHoverKey) hoveredOhlcTime = null;
            ohlcHoverKey = key;
            ohlcMap = {};
            ohlcList.forEach(b => {
                ohlcMap[Math.floor(Date.parse(b.bar_time.length === 19 ? b.bar_time + 'Z' : b.bar_time) / 1000)] = b;
            });
        }

        function periodName(p) {
            return p === 'intraday' ? '分时' : ({ '1d': '日线', '1m': '1分', '5m': '5分', '30m': '30分', '60m': '60分' }[p] || p);
        }

        function fmtPct(v) { return Number.isFinite(v) ? (v >= 0 ? '+' : '') + v.toFixed(2) + '%' : '--'; }

        // 价格小数位: 由最小变动价位推出; 整数 tick → 0 位
        // 与信息弹窗 priceDec 口径一致
        function priceDecOf(symbol) {
            const prod = (symbol.replace(/[0-9]+$/, '') || '').toUpperCase();
            const spec = SPEC_FALLBACK[prod];
            if (!spec || !spec.tick) return 1;
            return (String(spec.tick).split('.')[1] || '').length;
        }

        function displayPrice(value, symbol) {
            if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return '--';
            return Number(value).toFixed(priceDecOf(symbol));
        }

        // 涨跌计算: 基准为昨结算 (与信息弹窗、交易所口径一致)
        // 返回 {ok, diff, pct, cls} —— 数据不足时 ok=false, 调用方显示 '--'
        function changeOf(inst) {
            const last = parseFloat(inst && inst.last_price);
            const prev = parseFloat(inst && inst.prev_settle);
            if (isNaN(last) || isNaN(prev) || prev === 0) return { ok: false, diff: null, pct: null, cls: 'chg-flat' };
            const diff = last - prev;
            const pct = diff / prev * 100;
            return { ok: true, diff, pct, cls: diff > 0 ? 'chg-up' : (diff < 0 ? 'chg-down' : 'chg-flat') };
        }

        // 卡片涨跌文本: 金额(带符号+精度) / 百分比 / 幅度措辞
        function chgTexts(inst) {
            const c = changeOf(inst);
            if (!c.ok) return { diff: '--', pct: '--', label: '涨跌', cls: 'chg-flat', up: false, down: false };
            const dec = priceDecOf(inst.symbol);
            return {
                diff: (c.diff > 0 ? '+' : '') + c.diff.toFixed(dec),
                pct: (c.pct > 0 ? '+' : '') + c.pct.toFixed(2) + '%',
                label: c.diff > 0 ? '上涨' : (c.diff < 0 ? '下跌' : '持平'),
                cls: c.cls,
                up: c.diff > 0,
                down: c.diff < 0,
            };
        }

        function renderOhlcLegend(time) {
            if (currentPeriod === 'intraday') {
                if (arguments.length) hoveredOhlcTime = time ?? null;
                else if (hoveredOhlcTime !== null) time = hoveredOhlcTime;
            } else hoveredOhlcTime = null;
            updateSubIndicatorLegend(time);
            const el = document.getElementById('ohlc-legend');
            if (!el) return;
            let b = (time !== undefined && time !== null) ? ohlcMap[time] : null;
            if (!b) b = ohlcList[ohlcList.length - 1] || null;
            if (!b) { el.textContent = `${currentSymbol} ${periodName(currentPeriod)}`; return; }

            const idx = ohlcList.indexOf(b);
            const o = parseFloat(b.open_price), h = parseFloat(b.high_price);
            const l = parseFloat(b.low_price), c = parseFloat(b.close_price);
            // 涨跌幅基准: 前一根收盘 (首根用自身开盘)
            const ref = idx > 0 ? parseFloat(ohlcList[idx - 1].close_price) : o;
            const chg = c - ref;
            const pct = ref ? chg / ref * 100 : 0;
            const up = chg >= 0;
            const col = up ? 'var(--accent-up)' : 'var(--accent-down)';
            // 非有限数(缺失/坏点)显示 '--', 不渲染 NaN
            const dec = priceDecOf(currentSymbol);
            const sgn = v => Number.isFinite(v) ? (v >= 0 ? '+' : '') + v.toFixed(dec) : '--';
            const p1 = v => Number.isFinite(v) ? v.toFixed(dec) : '--';
            const t = currentPeriod === '1d'
                ? b.bar_time.slice(0, 10)
                : b.bar_time.slice(5, 16).replace('T', ' ');
            el.replaceChildren();
            const heading = document.createElement('span');
            heading.style.color = '#fff';
            heading.style.fontWeight = 'bold';
            heading.textContent = `${currentSymbol} ${periodName(currentPeriod)}`;
            el.appendChild(heading);
            const add = (label, value, color = col) => {
                const item = document.createElement('span');
                item.textContent = `${label} `;
                const number = document.createElement('b');
                number.style.color = color;
                number.textContent = value;
                item.appendChild(number);
                el.appendChild(item);
            };
            add('时间', t);
            add('开', p1(o)); add('高', p1(h)); add('低', p1(l)); add('收', p1(c));
            add('涨跌', `${sgn(chg)} (${fmtPct(pct)})`);
            add('量', b.volume==null ? '未知（采集缺口）' : String(b.volume), 'var(--text-muted)');
            add('持仓', b.open_interest ? String(Math.round(parseFloat(b.open_interest))) : '--', 'var(--text-muted)');
        }

