        let currentSymbol = "JM2701";
        let currentPeriod = "intraday";
        let chart = null;
        let candleSeries = null;
        let ema20Series = null;
        let ema60Series = null;
        let intradaySeries = null;   // 当日分时线
        let aiSessionReferenceSeries = null;
        let aiSessionTargetSeries = null;
        let aiSessionInvalidationSeries = null;
        let aiSessionTriggerSeries = null;
        let aiSessionChartBatches = [];
        let aiSessionChartBatch = null;
        let aiSessionChartRunId = null;
        let aiSessionExpiryTimer = null;
        const aiSessionChartRunByBatch = new Map();
        let aiSessionStartMarker = null;
        let intradayPriceLine = null;
        let intradayBaseline = null; // 分时基准价 (前一交易日收盘, 即"昨结"中轴线)
        let intradayPriceMin = null; // 当日分时价格范围 (百分比轴刻度用)
        let intradayPriceMax = null;
        let intradayTickLabels = null; // 分时时间轴刻度 (仅交易阶段开始/结束时间)
        let intradayPoints = [];  // 保留完整日盘与实际夜盘的分钟时间槽
        let intradayValuedTimes = new Set(); // 有成交价的分钟; 留白槽无值, 十字线不应在上面显示
        let intradayValueByTime = new Map(); // 分钟 -> 成交价 (触摸端横线读数贴值定位用)
        let intradayRange = null;      // 分时全天边界，滚动和拖动不能越界
        let intradayViewKey = null;    // 同一合约/交易日刷新时保留放大后的视口
        let pickSelectedSymbol = null; // 搜索结果中选中的合约 (先选合约再选分组)
        let pickTargetGroups = ['默认']; // 目标分组 (支持多选)
        let lastUniverseKw = '';       // 搜索关键词 (选中态重渲染用)
        const prevCloseCache = {};   // symbol_day -> 基准价
        let intradayDate = null;     // 分时查看的交易日 (null = 最新)
        let intradayDates = [];      // 可用交易日列表 (升序)
        let subChart = null;         // 副图 (VOL/MACD/OI/KDJ/DMI)
        let subSeriesList = [];      // 副图当前系列 (切换指标时清理)
        let subIndicator = 'VOL';    // 当前副图指标
        let barsData = [];           // 最近一次加载的周期K线
        let intradayBars = [];       // 最近一次加载的当日 1m K线
        let subValueMap = {};
        let subLegendMap = {};        // 副图首个系列的 time->value (十字线联动)
        let ohlcList = [];           // 主图当前数据 (K线周期或分时 1m), 供悬浮信息面板
        let ohlcMap = {};            // time -> bar 索引
        let hoveredOhlcTime = null;
        let ohlcHoverKey = '';
        let pendingFit = true;       // 周期/合约切换后需要重新贴合数据范围 (实时刷新不干预)
        let ws = null;
        const isLiveMode = window.location.pathname === '/live';
        let liveReady = false;
        let liveProfile = '';

        function escapeHTML(value) {
            return String(value ?? '').replace(/[&<>"']/g, ch => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            })[ch]);
        }

        function safeInlineValue(value) {
            return encodeURIComponent(String(value ?? '')).replace(/'/g, '%27');
        }

        // 动态状态: 订阅列表与全市场合约 (不再写死在页面里)
        let watchlist = [];       // [{symbol, name, exchange, last_price, open_interest}]
        let groupNames = [];         // 订阅分组名称 (来自后端)
        let searchTimer = null;

        // 登录状态 (模拟交易页)
        let currentUser = null;

        async function initUser() {
            try {
                const res = await fetch('/api/v1/auth/me');
                if (res.status === 401) {
                    window.location.href = '/login';
                    return false;
                }
                const data = await res.json();
                currentUser = data.user;
            } catch (e) {
                window.location.href = '/login';
                return false;
            }
            if (isLiveMode && !currentUser.can_live_trade) {
                window.location.href = '/paper';
                return false;
            }
            renderUserBadge();
            applyTradingMode();
            if (currentUser.role !== 'sim') {
                document.getElementById('ai-model-settings-btn').style.display = 'none';
                document.getElementById('ai-history-btn').style.display = 'none';
                document.getElementById('ai-analyze-btn').style.display = 'none';
                document.getElementById('ai-session-chart-btn').style.display = 'none';
                document.getElementById('ai-analysis-results').style.display = 'none';
            }
            return true;
        }

        function applyTradingMode() {
            if (!isLiveMode) return;
            document.title = '实盘交易 - 国内期货实时监控系统';
            document.getElementById('page-title').innerText = '实盘交易 · 国内期货实时监控与客观规则分析';
            document.getElementById('trade-panel-title').innerText = '实盘账户与闪电交易';
            document.getElementById('trade-mode-banner').innerHTML =
                '<span id="live-mode-label" style="color:#f87171; font-weight:bold;">■ CTP 柜台交易</span>' +
                '<span id="live-ready-text" style="color:#f59e0b; font-size:11px;"> — 正在检查柜台连接、结算确认与对账状态...</span>';
            document.getElementById('auto-trade-panel').style.display = 'none';
            document.getElementById('ai-model-settings-btn').style.display = 'none';
            document.getElementById('ai-history-btn').style.display = 'none';
            document.getElementById('ai-analyze-btn').style.display = 'none';
            document.getElementById('ai-session-chart-btn').style.display = 'none';
            document.getElementById('ai-analysis-results').style.display = 'none';
            document.getElementById('emergency-close-btn').innerText = '⚠ 一键紧急全平所有实盘持仓';
            setLiveOrderButtons(false);
        }

        function setLiveOrderButtons(enabled) {
            if (!isLiveMode) return;
            document.querySelectorAll('.trade-order-btn').forEach(btn => {
                btn.disabled = !enabled;
                btn.style.opacity = enabled ? '' : '0.4';
                btn.style.cursor = enabled ? 'pointer' : 'not-allowed';
            });
        }

        async function loadLiveStatus() {
            if (!isLiveMode) return;
            try {
                const res = await fetch('/api/v1/trade/live/status');
                if (res.status === 401) { window.location.href = '/login'; return; }
                if (res.status === 403) { window.location.href = '/paper'; return; }
                const status = await res.json();
                liveProfile = status.profile || '';
                liveReady = res.ok && !!status.ready;
                const isSimulation = liveProfile === 'simnow';
                const modeLabel = document.getElementById('live-mode-label');
                if (modeLabel) modeLabel.innerText = isSimulation ? '■ CTP 仿真交易' : '■ CTP 实盘交易';
                if (isSimulation) {
                    document.title = 'CTP仿真交易 - 国内期货实时监控系统';
                    document.getElementById('page-title').innerText = '正式账号 · CTP仿真看盘与交易';
                    document.getElementById('trade-panel-title').innerText = '仿真账户与闪电交易';
                }
                const text = document.getElementById('live-ready-text');
                if (text) {
                    text.innerText = liveReady
                        ? ` — 柜台已就绪，账号 ${status.account || '--'}；闪电下单将直接发送至交易所`
                        : ` — ${status.blocking_reason || status.sync_error || '柜台未连接或尚未完成结算确认与对账，实盘下单已锁定'}`;
                    text.style.color = liveReady ? '#10b981' : '#f59e0b';
                }
                setLiveOrderButtons(liveReady);
            } catch (e) {
                liveReady = false;
                setLiveOrderButtons(false);
                const text = document.getElementById('live-ready-text');
                if (text) text.innerText = ' — 柜台状态请求失败，实盘下单已锁定';
            }
        }

        function renderUserBadge() {
            const badge = document.getElementById('user-badge');
            const roleText = { sim: '模拟用户', live: '正式用户', admin: '管理员' }[currentUser.role] || currentUser.role;
            const roleColor = currentUser.can_live_trade ? '#10b981' : '#38bdf8';
            // display_name 与角色文案相同时(如都是"正式用户")只显示一次, 避免出现"正式用户 · 正式用户"
            const roleLabel = currentUser.display_name === roleText
                ? roleText
                : `${currentUser.display_name} · ${roleText}`;
            badge.innerHTML = `<span style="color:${roleColor}; font-weight:bold;">${roleLabel}</span>` +
                ` <span onclick="logout()" style="cursor:pointer; color:#ef4444; margin-left:4px;">退出</span>`;
            const nav = document.getElementById('nav-live-link');
            if (isLiveMode) {
                nav.href = '/paper';
                nav.innerText = '模拟交易页 →';
                nav.style.display = 'inline-block';
            } else if (currentUser.can_live_trade) {
                nav.style.display = 'inline-block';
            }
        }

