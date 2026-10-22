        function parseServerTime(ts) {
            // 后端事件/成交时间为 UTC 无时区字符串, 补 Z 后由浏览器转本地时间
            if (!ts) return new Date();
            const s = String(ts);
            return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + 'Z');
        }

        async function loadEvents() {
            try {
                const res = await fetch('/api/v1/events?limit=20');
                if (!res.ok) return;
                const data = await res.json();
                const box = document.getElementById('events-container');
                if (data.events && data.events.length > 0) {
                    box.innerHTML = data.events.map(ev => {
                        let sevClass = ev.severity === 'WARNING' ? 'warning' : (ev.severity === 'ERROR' ? 'error' : '');
                        let timeStr = parseServerTime(ev.event_time).toLocaleTimeString();
                        return `
                            <div class="event-item ${sevClass}">
                                <div class="event-header">
                                    <strong>[${escapeHTML(ev.severity)}] ${escapeHTML(ev.event_type)}</strong>
                                    <span class="event-time">${escapeHTML(timeStr)}</span>
                                </div>
                                <div class="event-msg">${escapeHTML(ev.message)}</div>
                            </div>
                        `;
                    }).join('');
                } else {
                    box.innerHTML = `<div style="color:var(--text-muted); font-size:12px; padding:6px;">暂无事件记录</div>`;
                }
            } catch (e) {
                console.error("fetch events error", e);
            }
        }

        function appendLiveEvent(ev) {
            const box = document.getElementById('events-container');
            let sevClass = ev.severity === 'WARNING' ? 'warning' : (ev.severity === 'ERROR' ? 'error' : '');
            let timeStr = parseServerTime(ev.timestamp || Date.now()).toLocaleTimeString();
            const itemHtml = `
                <div class="event-item ${sevClass}">
                    <div class="event-header">
                        <strong>[${escapeHTML(ev.severity || 'INFO')}] ${escapeHTML(ev.event_type || 'EVENT')}</strong>
                        <span class="event-time">${escapeHTML(timeStr)}</span>
                    </div>
                    <div class="event-msg">${escapeHTML(ev.message || '')}</div>
                </div>
            `;
            box.insertAdjacentHTML('afterbegin', itemHtml);
            if (box.children.length > 20) {
                box.removeChild(box.lastChild);
            }
        }

        // ---------------- 左侧数据面板页签: 事件 / 成交明细 / 大单监控 ----------------
        let leftTab = 'events';

        function showLeftTab(t) {
            leftTab = t;
            ['events', 'tape', 'big'].forEach(x => {
                const pane = document.getElementById('tab-' + x);
                if (pane) pane.style.display = (x === t) ? 'block' : 'none';
                const btn = document.getElementById('ltab-btn-' + x);
                if (btn) btn.classList.toggle('active', x === t);
            });
            refreshLeftTab();
        }

        function refreshLeftTab() {
            if (leftTab === 'events') loadEvents();
            else if (leftTab === 'tape') loadTape();
            else loadBigOrders();
        }

        function tapeDirColor(d) {
            return d === '多开' ? '#ef4444' : d === '空开' ? '#10b981'
                : d === '多平' ? '#f59e0b' : d === '空平' ? '#a855f7' : '#8492a6';
        }

        async function loadTape() {
            if (!currentSymbol) return;
            const requestedSymbol = currentSymbol;
            const requestedPeriod = currentPeriod;
            const requestedDate = intradayDate;
            try {
                // 分时切到历史日期时, 显示该日分钟级明细 (由 1m K线推算)
                const dateParam = (currentPeriod === 'intraday' && intradayDate) ? `&date=${intradayDate}` : '';
                const limitParam = dateParam ? 500 : 60;   // 历史日期取全天分钟明细
                const res = await fetch(`/api/v1/tape/${encodeURIComponent(requestedSymbol)}?limit=${limitParam}${dateParam}`);
                if (!res.ok) return;
                const data = await res.json();
                if (requestedSymbol !== currentSymbol || requestedPeriod !== currentPeriod
                        || requestedDate !== intradayDate || leftTab !== 'tape') return;
                const box = document.getElementById('tape-container');
                const items = data.items || [];
                if (!items.length) {
                    box.innerHTML = `<div style="color:var(--text-muted); font-size:12px; padding:6px;">暂无成交明细 — 交易时段行情推送后自动生成 (大单阈值 ${data.big_order_threshold} 手)</div>`;
                    return;
                }
                const cap = data.granularity === '1m'
                    ? `<div style="font-size:10px; color:#f59e0b; padding:2px 6px;">历史日期 ${data.date} · 分钟级明细 (由 1 分钟K线推算)</div>`
                    : '';
                const dirColor = d => d === '多开' ? '#ef4444' : d === '空开' ? '#10b981' : d === '多平' ? '#f59e0b' : d === '空平' ? '#a855f7' : '#8492a6';
                box.innerHTML = cap +
                    '<div style="display:flex; justify-content:space-between; font-size:10px; color:var(--text-muted); padding:2px 6px; border-bottom:1px solid var(--border-color);"><span>时间</span><span>价格</span><span>现手</span><span>方向</span><span>增仓</span></div>' +
                    items.map(t => `
                    <div style="display:flex; justify-content:space-between; font-size:11px; font-family:monospace; padding:3px 6px; border-bottom:1px solid #232731;">
                        <span style="color:var(--text-muted);">${escapeHTML(t.time)}</span>
                        <span>${escapeHTML(t.price)}</span>
                        <span style="font-weight:bold;">${escapeHTML(t.volume)}</span>
                        <span style="color:${dirColor(t.direction)};">${escapeHTML(t.direction)}</span>
                        <span style="color:var(--text-muted);">${t.oi_change > 0 ? '+' : ''}${Math.round(t.oi_change)}</span>
                    </div>`).join('');
            } catch (e) {}
        }

        async function loadBigOrders() {
            if (!currentSymbol) return;
            const requestedSymbol = currentSymbol;
            const requestedPeriod = currentPeriod;
            const requestedDate = intradayDate;
            try {
                // 只显示当前查看合约的大单; 分时历史日期时为分钟级明细
                const dateParam = (currentPeriod === 'intraday' && intradayDate) ? `&date=${intradayDate}` : '';
                const res = await fetch(`/api/v1/bigorders?instrument=${encodeURIComponent(requestedSymbol)}&limit=80${dateParam}`);
                if (!res.ok) return;
                const data = await res.json();
                if (requestedSymbol !== currentSymbol || requestedPeriod !== currentPeriod
                        || requestedDate !== intradayDate || leftTab !== 'big') return;
                const box = document.getElementById('bigorders-container');
                const items = data.items || [];
                if (!items.length) {
                    box.innerHTML = `<div style="color:var(--text-muted); font-size:12px; padding:6px;">${escapeHTML(requestedSymbol)} 暂无大单 — 单笔现手 ≥ ${100} 手时自动记录 (切合约自动跟随)</div>`;
                    return;
                }
                const cap = data.granularity === '1m'
                    ? '<div style="font-size:10px; color:#f59e0b; padding:2px 6px;">历史日期 · 分钟级明细</div>'
                    : '';
                box.innerHTML = cap +
                    '<div style="display:flex; justify-content:space-between; font-size:10px; color:var(--text-muted); padding:2px 6px; border-bottom:1px solid var(--border-color);"><span>时间</span><span>价格</span><span>现手</span><span>方向</span><span>金额(万)</span></div>' +
                    items.map(t => `
                    <div style="display:flex; justify-content:space-between; font-size:11px; font-family:monospace; padding:3px 6px; border-bottom:1px solid #232731;">
                        <span style="color:var(--text-muted);">${escapeHTML(t.time)}</span>
                        <span>${escapeHTML(t.price)}</span>
                        <span style="font-weight:bold;">${escapeHTML(t.volume)}</span>
                        <span style="color:${tapeDirColor(t.direction)};">${escapeHTML(t.direction)}</span>
                        <span style="color:#facc15;">${(t.amount / 10000).toFixed(0)}万</span>
                    </div>`).join('');
            } catch (e) {}
        }

