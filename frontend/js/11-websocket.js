        function connectWebSocket() {
            const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${proto}//${window.location.host}/api/v1/ws/market`;
            const dot = document.getElementById('ws-dot');
            const text = document.getElementById('ws-text');

            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                dot.classList.add('connected');
                text.innerText = "WS实时已连接";
            };

            ws.onmessage = (evt) => {
                try {
                    const msg = JSON.parse(evt.data);
                    if (msg.event === "TICK") {
                        const tick = msg.data;
                        const priceEl = document.getElementById(`price-${tick.instrument}`);
                        const oiEl = document.getElementById(`oi-${tick.instrument}`);
                        if (priceEl) priceEl.innerText = displayPrice(tick.last_price, tick.instrument);
                        if (oiEl) oiEl.innerText = "持仓: " + Math.round(parseFloat(tick.open_interest));
                        // 同步缓存, 避免重渲染后价格丢失
                        const w = watchlist.find(x => x.symbol === tick.instrument);
                        if (w) {
                            w.last_price = tick.last_price;
                            if (tick.open_interest != null) w.open_interest = tick.open_interest;
                            // 昨结算缺失时补齐 (首次订阅的合约可能还没拿到快照)
                            if (w.prev_settle == null && tick.prev_settle != null) w.prev_settle = tick.prev_settle;
                        }
                        // 涨跌金额/幅度随行情实时刷新
                        if (w) {
                            const c = chgTexts(w);
                            const dEl = document.getElementById(`chg-${tick.instrument}`);
                            const pEl = document.getElementById(`chgpct-${tick.instrument}`);
                            if (dEl) { dEl.innerText = c.diff; dEl.className = 'chg-val ' + c.cls; }
                            if (pEl) { pEl.innerText = c.pct; pEl.className = 'chg-val ' + c.cls; }
                        }
                        requestTradeRefresh();
                        if (tick.instrument === currentSymbol) { updateChartPrice(tick); refreshLiveChart(); }
                    } else if (msg.event === "BAR_CLOSE" && (msg.period === currentPeriod || (currentPeriod === 'intraday' && msg.period === '1m')) && msg.data.instrument === currentSymbol) {
                        loadBars();
                        loadIndicators();
                    } else if (msg.event === "SYSTEM_EVENT") {
                        appendLiveEvent(msg.data);
                        if (msg.data.event_type === "SIGNAL_TRIGGER") {
                            loadSignal();
                        }
                    }
                } catch (e) {}
            };

            ws.onclose = () => {
                dot.classList.remove('connected');
                text.innerText = "WebSocket 已断开";
                setTimeout(connectWebSocket, 3000);
            };
        }

        function selectSymbol(sym, name) {
            if (sym !== currentSymbol) globalThis.invalidateMarketSimulator?.();
            clearTimeout(aiSessionExpiryTimer);
            clearAISessionChartLines(true);
            currentSymbol = sym;
            pendingFit = true;
            intradayDate = null;   // 切合约回到最新分时
            loadIntradayDates();
            const meta = watchlist.find(w => w.symbol === sym);
            const displayName = name || (meta ? meta.name : '') || mainLabel(sym);
            document.getElementById('current-contract-header').innerText = displayName ? `${displayName} ${sym}` : sym;
            updateMainSwitchBtn();
            renderContracts();
            loadBars();
            loadIndicators();
            loadSignal();
            loadAnalysis();
            syncAIAnalysisButton();
            document.getElementById('ai-analysis-results').style.display = 'none';
            loadLatestAIResult(sym);
            loadLatestFutureTrend(sym);
            if (leftTab === 'tape') loadTape();       // 切合约时刷新成交明细
            if (leftTab === 'big') loadBigOrders();   // 大单监控跟随当前合约
        }

        function switchPeriod(p) {
            currentPeriod = p;
            pendingFit = true;
            if (p === 'intraday') {
                intradayDate = null;   // 切回分时默认显示最新交易日
                intradayViewKey = null;
                loadIntradayDates();
            }
            document.querySelectorAll('.period-btn[data-period]').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.period === p);
            });
            loadBars();
            loadLatestAIResult(currentSymbol);
            if (p !== 'intraday') loadIndicators();
            renderAISessionChartLines();
        }



        // ---------------- 交易执行与持仓资金交互逻辑 ----------------
        // ⚠️ 订单/持仓列表必须独立定时刷新, 不能只依赖行情 tick 驱动:
        // 下单走的是独立 HTTP 请求, 而列表刷新若挂在 WS tick 上, 那么
        //   (1) WS 断线期间 (onclose 到重连成功, 至少 3 秒, 且服务器不可达时无限重试)
        //   (2) 非交易时段 / 订阅合约无推送
        // 列表就会永久冻结在打开页面时的快照 —— 用户下单后"看不到订单"。
        let tradeRefreshTimer = null;
        let lastTradeRefresh = 0;

