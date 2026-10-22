        function startTradeAutoRefresh() {
            if (tradeRefreshTimer !== null) return;
            tradeRefreshTimer = setInterval(() => {
                if (document.hidden) return;   // 后台标签页不刷, 省资源
                lastTradeRefresh = Date.now();
                loadTradeData();
            }, 3000);
        }

        // tick 触发的刷新做节流: WS 高频推送下每次 tick 都发 4 个请求会形成风暴
        function requestTradeRefresh() {
            const now = Date.now();
            if (now - lastTradeRefresh < 1000) return;
            lastTradeRefresh = now;
            loadTradeData();
        }

        async function loadTradeData() {
            if (isLiveMode) return loadLiveTradeData();
            try {
                // 1. 获取账户资金
                const accRes = await fetch('/api/v1/trade/account');
                if (accRes.ok) {
                    const acc = await accRes.json();
                    document.getElementById('acc-dynamic').innerText = '￥' + parseFloat(acc.dynamic_equity).toLocaleString('zh-CN', {minimumFractionDigits: 2});
                    document.getElementById('acc-available').innerText = '￥' + parseFloat(acc.available).toLocaleString('zh-CN', {minimumFractionDigits: 2});
                    document.getElementById('acc-margin').innerText = '￥' + parseFloat(acc.margin).toLocaleString('zh-CN', {minimumFractionDigits: 2});

                    const pnlVal = parseFloat(acc.floating_pnl);
                    const pnlEl = document.getElementById('acc-floating-pnl');
                    pnlEl.innerText = (pnlVal >= 0 ? '+' : '') + pnlVal.toFixed(2);
                    pnlEl.style.color = pnlVal >= 0 ? 'var(--accent-up)' : 'var(--accent-down)';
                }

                // 2. 获取当前持仓
                const posRes = await fetch('/api/v1/trade/positions');
                if (posRes.ok) {
                    const posData = await posRes.json();
                    const container = document.getElementById('positions-list');
                    if (!posData.positions || posData.positions.length === 0) {
                        container.innerHTML = '<div style="color:var(--text-muted); font-size:11px;">暂无持仓</div>';
                    } else {
                        container.innerHTML = posData.positions.map(p => {
                            const pnl = parseFloat(p.floating_pnl);
                            const pnlColor = pnl >= 0 ? 'var(--accent-up)' : 'var(--accent-down)';
                            const dirBadge = p.direction === 'BUY' ? '<span style="color:#ef4444; font-weight:bold;">多</span>' : '<span style="color:#10b981; font-weight:bold;">空</span>';
                            return '<div style="background:#1b1e24; border:1px solid var(--border-color); border-radius:4px; padding:5px 8px; margin-bottom:4px; font-size:11px; display:flex; justify-content:space-between; align-items:center;">' +
                                '<div><b>' + escapeHTML(p.instrument) + '</b> ' + dirBadge + ' ' + escapeHTML(p.volume) + '手 @ ' + escapeHTML(p.open_price_avg) + '</div>' +
                                '<div style="color:' + pnlColor + '; font-weight:bold;">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(1) + '</div>' +
                            '</div>';
                        }).join('');
                    }
                }

                // 3. 获取活跃挂单 (含最近被拒订单, 避免"下单没成功"的误判)
                const [orderRes, rejectedRes] = await Promise.all([
                    fetch('/api/v1/trade/orders?statuses=SUBMITTED,PARTIAL'),
                    fetch('/api/v1/trade/orders?statuses=REJECTED&limit=5')
                ]);
                if (orderRes.ok && rejectedRes.ok) {
                    const [orderData, rejectedData] = await Promise.all([orderRes.json(), rejectedRes.json()]);
                    const container = document.getElementById('orders-list');
                    const allOrders = orderData.orders || [];
                    const activeOrders = allOrders.filter(o => o.status === 'SUBMITTED' || o.status === 'PARTIAL');
                    // 最近 5 笔被拒订单, 让用户看到"为什么没成交"
                    const rejectedOrders = (rejectedData.orders || []).reverse();
                    const html = [];
                    if (activeOrders.length === 0) {
                        html.push('<div style="color:var(--text-muted); font-size:11px;">暂无未成交挂单</div>');
                    } else {
                        html.push(activeOrders.map(o => {
                            const dirText = o.direction === 'BUY' ? '买' : '卖';
                            const offText = o.offset === 'OPEN' ? '开' : '平';
                            return '<div style="background:#1b1e24; border:1px solid var(--border-color); border-radius:4px; padding:4px 6px; margin-bottom:4px; font-size:11px; display:flex; justify-content:space-between; align-items:center;">' +
                                '<div>' + escapeHTML(o.instrument) + ' ' + dirText + offText + ' ' + escapeHTML(o.volume) + '手 @ ' + escapeHTML(displayPrice(o.price, o.instrument)) + '</div>' +
                                '<button onclick="cancelOrder(decodeURIComponent(\x27' + safeInlineValue(o.order_id) + '\x27))" style="background:#3b82f6; color:#fff; border:none; border-radius:3px; padding:2px 6px; font-size:10px; cursor:pointer;">撤单</button>' +
                            '</div>';
                        }).join(''));
                    }
                    if (rejectedOrders.length > 0) {
                        html.push('<div style="color:#f59e0b; font-size:10px; margin:6px 0 3px;">最近被拒 ' + rejectedOrders.length + ' 笔</div>');
                        html.push(rejectedOrders.map(o => {
                            const dirText = o.direction === 'BUY' ? '买' : '卖';
                            const offText = o.offset === 'OPEN' ? '开' : '平';
                            return '<div style="background:#241b1b; border:1px solid #7f1d1d; border-radius:4px; padding:4px 6px; margin-bottom:4px; font-size:10px;">' +
                                '<div style="color:#fca5a5;">' + escapeHTML(o.instrument) + ' ' + dirText + offText + ' ' + escapeHTML(o.volume) + '手 @ ' + escapeHTML(o.price) + ' <span style="color:#f59e0b;">已拒绝</span></div>' +
                                '<div style="color:var(--text-muted); margin-top:2px;">' + escapeHTML(o.reject_reason || '未提供原因') + '</div>' +
                            '</div>';
                        }).join(''));
                    }
                    container.innerHTML = html.join('');
                }

                // 4. 获取最近成交记录
                const tradeRes = await fetch('/api/v1/trade/trades?limit=8');
                if (tradeRes.ok) {
                    const tradeData = await tradeRes.json();
                    const container = document.getElementById('trades-list');
                    const trades = (tradeData.trades || []).slice(-8).reverse();
                    if (trades.length === 0) {
                        container.innerHTML = '<div style="color:var(--text-muted); font-size:11px;">暂无成交记录</div>';
                    } else {
                        container.innerHTML = trades.map(t => {
                            const dirText = t.direction === 'BUY' ? '买' : '卖';
                            const offText = t.offset === 'OPEN' ? '开' : '平';
                            const dirColor = t.direction === 'BUY' ? '#ef4444' : '#10b981';
                            return '<div style="background:#1b1e24; border:1px solid var(--border-color); border-radius:4px; padding:4px 6px; margin-bottom:4px; font-size:11px; display:flex; justify-content:space-between;">' +
                                '<div><span style="color:' + dirColor + '; font-weight:bold;">' + dirText + offText + '</span> ' + escapeHTML(t.instrument) + ' ' + escapeHTML(t.volume) + '手 @ ' + escapeHTML(displayPrice(t.price, t.instrument)) + '</div>' +
                                '<div style="color:var(--text-muted); font-size:10px;">' + parseServerTime(t.trade_time).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}) + '</div>' +
                            '</div>';
                        }).join('');
                    }
                }
            } catch (err) {
                console.error('loadTradeData error:', err);
            }
        }

        async function loadLiveTradeData() {
            try {
                const [accountRes, orderRes] = await Promise.all([
                    fetch('/api/v1/trade/live/account'),
                    fetch('/api/v1/trade/live/orders')
                ]);
                if (accountRes.status === 401 || orderRes.status === 401) {
                    window.location.href = '/login';
                    return;
                }
                if (accountRes.status === 403 || orderRes.status === 403) {
                    window.location.href = '/paper';
                    return;
                }
                if (!accountRes.ok || !orderRes.ok) return;

                const snapshot = await accountRes.json();
                const orderData = await orderRes.json();
                const money = value => parseFloat(value || 0).toLocaleString('zh-CN', {
                    minimumFractionDigits: 2, maximumFractionDigits: 2
                });
                const account = (snapshot.account || [])[0];
                if (account) {
                    document.getElementById('acc-dynamic').innerText = '￥' + money(account.Balance);
                    document.getElementById('acc-available').innerText = '￥' + money(account.Available);
                    document.getElementById('acc-margin').innerText = '￥' + money(account.CurrMargin);
                    const pnl = parseFloat(account.PositionProfit || 0);
                    const pnlEl = document.getElementById('acc-floating-pnl');
                    pnlEl.innerText = (pnl >= 0 ? '+' : '') + pnl.toFixed(2);
                    pnlEl.style.color = pnl >= 0 ? 'var(--accent-up)' : 'var(--accent-down)';
                } else {
                    ['acc-dynamic', 'acc-available', 'acc-margin', 'acc-floating-pnl'].forEach(id => {
                        document.getElementById(id).innerText = '--';
                    });
                }

                const positions = (snapshot.positions || []).filter(p => Number(p.Position || 0) > 0);
                const positionsBox = document.getElementById('positions-list');
                positionsBox.innerHTML = positions.length ? positions.map(p => {
                    const isLong = p.PosiDirection === '2';
                    const badge = isLong
                        ? '<span style="color:#ef4444; font-weight:bold;">多</span>'
                        : '<span style="color:#10b981; font-weight:bold;">空</span>';
                    return '<div style="background:#1b1e24; border:1px solid var(--border-color); border-radius:4px; padding:5px 8px; margin-bottom:4px; font-size:11px; display:flex; justify-content:space-between; align-items:center;">' +
                        '<div><b>' + escapeHTML(String(p.InstrumentID || '').toUpperCase()) + '</b> ' + badge + ' ' + escapeHTML(p.Position || 0) + '手</div>' +
                        '<div style="color:var(--text-muted);">保证金 ￥' + money(p.UseMargin) + '</div>' +
                    '</div>';
                }).join('') : '<div style="color:var(--text-muted); font-size:11px;">暂无实盘持仓</div>';

                const allOrders = orderData.orders || [];
                const active = allOrders.filter(o => ['SUBMITTED', 'PARTIAL', 'CANCELLING', 'UNKNOWN'].includes(o.status));
                const rejected = allOrders.filter(o => o.status === 'REJECTED').slice(0, 5);
                const orderHtml = [];
                if (!active.length) {
                    orderHtml.push('<div style="color:var(--text-muted); font-size:11px;">暂无未成交实盘挂单</div>');
                } else {
                    orderHtml.push(active.map(o => {
                        const canCancel = ['SUBMITTED', 'PARTIAL'].includes(o.status);
                        const stateColor = o.status === 'UNKNOWN' ? '#f59e0b' : '#e2e8f0';
                        return '<div style="background:#1b1e24; border:1px solid var(--border-color); border-radius:4px; padding:4px 6px; margin-bottom:4px; font-size:11px; display:flex; justify-content:space-between; align-items:center;">' +
                            '<div style="color:' + stateColor + ';">' + escapeHTML(o.instrument) + ' ' + (o.direction === 'BUY' ? '买' : '卖') + (o.offset === 'OPEN' ? '开' : '平') + ' ' + escapeHTML(o.volume) + '手 @ ' + escapeHTML(displayPrice(o.price, o.instrument)) + ' [' + escapeHTML(o.status) + ']</div>' +
                            (canCancel ? '<button onclick="cancelOrder(decodeURIComponent(\x27' + safeInlineValue(o.order_ref) + '\x27))" style="background:#b91c1c; color:#fff; border:none; border-radius:3px; padding:2px 6px; font-size:10px; cursor:pointer;">撤单</button>' : '') +
                        '</div>';
                    }).join(''));
                }
                if (rejected.length) {
                    orderHtml.push('<div style="color:#f59e0b; font-size:10px; margin:6px 0 3px;">最近被拒 ' + rejected.length + ' 笔</div>');
                    orderHtml.push(rejected.map(o => '<div style="background:#241b1b; border:1px solid #7f1d1d; border-radius:4px; padding:4px 6px; margin-bottom:4px; font-size:10px;">' +
                        '<div style="color:#fca5a5;">' + escapeHTML(o.instrument) + ' ' + (o.direction === 'BUY' ? '买' : '卖') + (o.offset === 'OPEN' ? '开' : '平') + ' ' + escapeHTML(o.volume) + '手 @ ' + escapeHTML(o.price) + '</div>' +
                        '<div style="color:var(--text-muted); margin-top:2px;">' + escapeHTML(o.reject_reason || '未提供原因') + '</div></div>').join(''));
                }
                document.getElementById('orders-list').innerHTML = orderHtml.join('');

                const trades = (snapshot.trades || []).slice(-8).reverse();
                document.getElementById('trades-list').innerHTML = trades.length ? trades.map(t => {
                    const buy = t.Direction === '0';
                    return '<div style="background:#1b1e24; border:1px solid var(--border-color); border-radius:4px; padding:4px 6px; margin-bottom:4px; font-size:11px; display:flex; justify-content:space-between;">' +
                        '<div><span style="color:' + (buy ? '#ef4444' : '#10b981') + '; font-weight:bold;">' + (buy ? '买' : '卖') + (t.OffsetFlag === '0' ? '开' : '平') + '</span> ' + escapeHTML(t.InstrumentID) + ' ' + escapeHTML(t.Volume) + '手 @ ' + escapeHTML(displayPrice(t.Price, t.InstrumentID)) + '</div>' +
                        '<div style="color:var(--text-muted); font-size:10px;">' + escapeHTML(t.TradeTime || '') + '</div>' +
                    '</div>';
                }).join('') : '<div style="color:var(--text-muted); font-size:11px;">暂无实盘成交记录</div>';
            } catch (err) {
                console.error('loadLiveTradeData error:', err);
            }
        }

        function setTradePriceMode() {
            const opponent = document.getElementById('trade-price-mode').value === 'opponent';
            const input = document.getElementById('trade-price');
            input.disabled = opponent;
            input.placeholder = opponent ? '买取卖一 · 卖取买一' : '委托价';
            if (opponent) input.value = '';
        }

        function opponentPrice(quote, direction) {
            const price = quote[direction === 'BUY' ? 'ask_price_1' : 'bid_price_1'];
            if (!price || !Number.isFinite(Number(price)) || Number(price) <= 0) {
                throw new Error(direction === 'BUY' ? '暂无卖一报价，无法使用对手价' : '暂无买一报价，无法使用对手价');
            }
            return String(price);
        }

        async function quickOrder(direction, offset, forceOpponent = false) {
            const instrument = currentSymbol;
            const useOpponent = forceOpponent || document.getElementById('trade-price-mode').value === 'opponent';
            let priceInput = document.getElementById('trade-price').value.trim();
            const volumeText = document.getElementById('trade-vol').value.trim();
            const volInput = Number(volumeText);

            if (useOpponent) {
                try {
                    const response = await fetch('/api/v1/quotes/' + encodeURIComponent(instrument));
                    if (!response.ok) throw new Error('无法获取当前买卖报价');
                    const quote = await response.json();
                    const quoteTime = Date.parse(quote.timestamp);
                    if (!Number.isFinite(quoteTime) || Date.now() - quoteTime > 15000) throw new Error('报价已过期，请等待实时行情更新');
                    priceInput = opponentPrice(quote, direction);
                } catch (e) {
                    alert(e.message);
                    return;
                }
            } else if (!priceInput) {
                const cached = watchlist.find(x => x.symbol === instrument);
                if (cached && cached.last_price != null) {
                    priceInput = String(cached.last_price);
                } else {
                    alert('请输入有效委托价');
                    return;
                }
            }

            if (!/^\d+$/.test(volumeText) || !Number.isInteger(volInput) || volInput < 1 || volInput > 50) {
                alert('请输入 1 到 50 之间的整数手数');
                return;
            }
            if (!priceInput || !Number.isFinite(Number(priceInput)) || Number(priceInput) <= 0) {
                alert('请输入大于 0 的有效委托价格');
                return;
            }
            if (isLiveMode && !liveReady) {
                alert('CTP 实盘柜台尚未就绪，系统拒绝发送报单');
                return;
            }

            const payload = {
                instrument: instrument,
                direction: direction,
                offset: offset,
                price: priceInput,
                volume: volInput
            };

            if (isLiveMode) {
                const directionText = direction === 'BUY' ? '买' : '卖';
                const offsetText = offset === 'OPEN' ? '开仓' : '平仓';
                const counterName = liveProfile === 'simnow' ? 'CTP 仿真柜台' : 'CTP 实盘柜台';
                if (!confirm(`【${counterName}闪电下单确认】\n\n合约：${instrument}\n方向：${directionText}${offsetText}\n价格：${priceInput}\n手数：${volInput} 手\n\n该委托将直接发送至${counterName}，是否确认？`)) return;
            }

            try {
                const endpoint = isLiveMode ? '/api/v1/trade/live/order' : '/api/v1/trade/order';
                const res = await fetch(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (res.status === 401) { window.location.href = '/login'; return; }
                if (res.status === 403) {
                    const denied = await res.json().catch(() => ({}));
                    alert('实盘权限受限: ' + (denied.detail || '账号无实盘权限或密码不符合要求'));
                    return;
                }
                const result = await res.json();
                if (isLiveMode && result.order?.status === 'UNKNOWN') {
                    alert('实盘报单状态不确定，请等待柜台对账，严禁重复下单');
                } else if (!res.ok || result.status === 'rejected') {
                    alert((isLiveMode ? '实盘报单被拒绝: ' : '风控拦截报单: ') + (result.order?.reject_reason || result.detail || '未知原因'));
                } else if (isLiveMode) {
                    alert('实盘委托已发送，委托编号: ' + (result.order?.order_ref || '--'));
                }
                loadTradeData();
            } catch (e) {
                alert('提交报单失败: ' + e);
            }
        }

        async function cancelOrder(orderId) {
            if (isLiveMode && !confirm('确定撤销这笔实盘委托吗？')) return;
            try {
                const prefix = isLiveMode ? '/api/v1/trade/live/order/' : '/api/v1/trade/order/';
                const res = await fetch(prefix + encodeURIComponent(orderId), { method: 'DELETE' });
                if (res.status === 401) { window.location.href = '/login'; return; }
                if (res.ok) {
                    loadTradeData();
                } else {
                    const err = await res.json();
                    alert('撤单失败: ' + (err.detail || '未知错误'));
                }
            } catch (e) {
                alert('撤单请求失败: ' + e);
            }
        }



        async function loadAutoConfig() {
            if (isLiveMode) return;
            try {
                const res = await fetch('/api/v1/trade/auto-config');
                if (res.ok) {
                    const data = await res.json();
                    const toggle = document.getElementById('auto-trade-toggle');
                    const statusText = document.getElementById('auto-trade-status');
                    if (toggle && statusText) {
                        toggle.checked = data.enabled;
                        statusText.innerText = data.enabled ? '跟单中' : '未启用';
                        statusText.style.color = data.enabled ? '#10b981' : '#ef4444';
                    }
                }
            } catch (e) {}
        }

        async function toggleAutoTrade(enabled) {
            if (isLiveMode) return;
            try {
                const res = await fetch('/api/v1/trade/auto-config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: enabled, min_score: 80, default_volume: 1, auto_stop_loss_enabled: true })
                });
                if (res.ok) {
                    const statusText = document.getElementById('auto-trade-status');
                    statusText.innerText = enabled ? '跟单中' : '未启用';
                    statusText.style.color = enabled ? '#10b981' : '#ef4444';
                }
            } catch (e) {
                alert('更新自动跟单配置失败: ' + e);
            }
        }


        async function emergencyCloseAll() {
            if (isLiveMode) {
                const token = prompt('【实盘紧急全平】请输入运维安全令牌 (X-Admin-Token)：');
                if (token === null) return;
                const confirmText = prompt('请手动输入确认口令：CONFIRM_CLOSE_ALL');
                if (confirmText !== 'CONFIRM_CLOSE_ALL') {
                    alert('确认口令不正确，已取消实盘全平');
                    return;
                }
                try {
                    const res = await fetch('/api/v1/trade/live/close-all', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json', 'X-Admin-Token': token.trim()},
                        body: JSON.stringify({confirm: 'CONFIRM_CLOSE_ALL', reason: '完整看盘工作台人工紧急全平'})
                    });
                    const data = await res.json();
                    alert(res.ok
                        ? `实盘全平指令已提交：撤单 ${data.cancelled || 0} 笔，平仓委托 ${(data.closed || []).length} 笔`
                        : '实盘全平失败: ' + (data.detail || '拒绝执行'));
                    loadTradeData();
                } catch (e) {
                    alert('实盘全平请求失败: ' + e);
                }
                return;
            }
            if (!confirm('确定要一键市价平掉当前所有模拟持仓吗？')) return;
            try {
                const res = await fetch('/api/v1/trade/close-all', { method: 'POST' });
                const data = await res.json();
                alert(data.message);
                loadTradeData();
            } catch (e) {
                alert('一键全平请求失败: ' + e);
            }
        }

        async function loadFeedStatus() {
            try {
                const res = await fetch('/api/v1/feed-status');
                if (!res.ok) return;
                const s = await res.json();
                const badge = document.getElementById('feed-badge');
                if (!s.running) {
                    badge.innerHTML = '<span class="dot" style="background:#ef4444;"></span> 行情源: 未运行';
                    return;
                }
                const feedNames = { sina: '新浪实时', simulator: '模拟行情', ctp: 'CTP行情' };
                const feedName = feedNames[s.feed] || s.feed;
                if (!s.market_open) {
                    badge.innerHTML = `<span class="dot" style="background:#f59e0b;"></span> ${feedName} · 休市`;
                } else {
                    const age = s.last_tick_age_seconds;
                    const delay = (age != null && age < 60) ? ` · 延迟 ${age}s` : ' · 等待行情';
                    badge.innerHTML = `<span class="dot"></span> ${feedName}${delay}`;
                }
            } catch (e) {}
        }

        async function loadStorageHealth() {
            try {
                const res = await fetch('/api/v1/health');
                const h = await res.json();
                const ok = (tag) => tag === 'connected';
                const color = (v) => ok(v) ? '#10b981' : '#ef4444';
                document.getElementById('storage-badge').innerHTML =
                    `Redis <span style="color:${color(h.redis)};">●</span> MySQL <span style="color:${color(h.mysql)};">●</span>`;
            } catch (e) {
                document.getElementById('storage-badge').innerHTML = '存储: 无法连接';
            }
        }

