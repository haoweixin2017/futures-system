        function groupEditCandidates() {
            const seen = [];
            groupNames.forEach(g => { if (g && !seen.includes(g)) seen.push(g); });
            if (!seen.includes('默认')) seen.push('默认');
            groupEditExtras.forEach(g => { if (g && !seen.includes(g)) seen.push(g); });
            return seen;
        }

        function openGroupEdit(symbol) {
            const inst = watchlist.find(w => w.symbol === symbol);
            groupEditTarget = symbol;
            groupEditOriginal = groupListOf(inst || {}).slice();
            groupEditSelected = groupEditOriginal.slice();
            groupEditExtras = [];
            groupEditMode = groupEditOriginal.length > 1 ? 'multi' : 'replace';
            const modal = document.getElementById('group-edit-modal');
            if (!modal) return;
            document.getElementById('group-edit-symbol').textContent = symbol;
            document.getElementById('group-edit-name').textContent = (inst && inst.name) || mainLabel(symbol) || '';
            const inp = document.getElementById('group-edit-new');
            inp.value = '';
            renderGroupEditList();
            renderGroupEditMode();
            modal.style.display = 'flex';
            setTimeout(() => inp.focus(), 60);
        }

        // 模式按钮高亮与说明文字 (替换模式点选即唯一归属, 多组模式为勾选叠加)
        function renderGroupEditMode() {
            const box = document.getElementById('group-edit-mode');
            if (!box) return;
            Array.from(box.children).forEach(btn => {
                const on = btn.dataset.mode === groupEditMode;
                btn.className = 'period-btn' + (on ? ' active' : '');
            });
            const modeHint = document.getElementById('group-edit-mode-hint');
            if (modeHint) {
                modeHint.textContent = groupEditMode === 'replace'
                    ? '点选一个分组即改到该分组 (原归属自动移除)'
                    : '勾选可叠加多个分组; 取消勾选则移出该分组';
            }
        }

        function setGroupEditMode(mode) {
            if (mode === groupEditMode) return;
            groupEditMode = mode;
            // 切回单组模式时, 收敛到当前第一个勾选项, 避免残留多选语义
            if (mode === 'replace' && groupEditSelected.length > 1) {
                groupEditSelected = [groupEditSelected[0]];
            }
            if (mode === 'multi' && !groupEditSelected.length) {
                groupEditSelected = groupEditOriginal.slice();
            }
            renderGroupEditList();
            renderGroupEditMode();
        }

        function renderGroupEditList() {
            const wrap = document.getElementById('group-edit-list');
            if (!wrap) return;
            wrap.innerHTML = groupEditCandidates().map(g => {
                const on = groupEditSelected.includes(g);
                const isNew = groupEditExtras.includes(g);
                const encoded = safeInlineValue(g);
                return `<span class="pick-chip ${on ? 'active' : ''}" data-group="${escapeHTML(g)}"
                    onclick="toggleGroupEdit(decodeURIComponent('${encoded}'))">${on ? '✓ ' : ''}${escapeHTML(g)}${isNew ? ' <span style="opacity:0.6;">(新)</span>' : ''}</span>`;
            }).join('');
            const hint = document.getElementById('group-edit-hint');
            if (hint) {
                if (!groupEditSelected.length) {
                    hint.textContent = groupEditMode === 'replace'
                        ? '未选任何分组, 保存后将归入「默认」'
                        : '已从所有分组移出, 保存后将归入「默认」';
                } else {
                    hint.textContent = (groupEditMode === 'replace' ? '将改到: ' : '已选: ') + groupEditSelected.join('、');
                }
            }
        }

        function toggleGroupEdit(g) {
            if (groupEditMode === 'replace') {
                // 替换模式: 点击即成为唯一归属 (再点一次同一项则取消选择)
                groupEditSelected = groupEditSelected.length === 1 && groupEditSelected[0] === g ? [] : [g];
            } else {
                const i = groupEditSelected.indexOf(g);
                if (i >= 0) groupEditSelected.splice(i, 1);
                else groupEditSelected.push(g);
            }
            renderGroupEditList();
        }

        // 输入新分组名称 → 加入候选并自动勾选 (真正落库在保存时由 assign-groups 隐式创建)
        function addGroupEditNew() {
            const inp = document.getElementById('group-edit-new');
            if (!inp) return;
            const name = (inp.value || '').trim();
            if (!name) return;
            if (name.length > 16) { alert('分组名称过长 (1-16 字符)'); return; }
            if (name === '默认') { alert('「默认」分组无需新建'); return; }
            if (!groupNames.includes(name) && !groupEditExtras.includes(name)) groupEditExtras.push(name);
            // 替换模式下新建即视为"改到新分组"; 多组模式下叠加
            if (groupEditMode === 'replace') groupEditSelected = [name];
            else if (!groupEditSelected.includes(name)) groupEditSelected.push(name);
            inp.value = '';
            renderGroupEditList();
        }

        function closeGroupEdit() {
            const modal = document.getElementById('group-edit-modal');
            if (modal) modal.style.display = 'none';
            groupEditTarget = null;
        }

        async function submitGroupEdit() {
            const symbol = groupEditTarget;
            if (!symbol) { closeGroupEdit(); return; }
            const groups = groupEditSelected.slice();
            if (!groups.length) groups.push('默认');   // 至少保留一个分组
            try {
                const res = await fetch('/api/v1/watchlist/assign-groups', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ symbol, groups })
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    alert('修改分组失败: ' + (err.detail || '未知错误'));
                    return;
                }
            } catch (e) { alert('修改分组失败: ' + e); return; }
            closeGroupEdit();
            // 先切到目标分组再刷新: loadWatchlist 结束后会按 activeGroupTab 渲染,
            // 若反过来赋值, 并发的其他 loadWatchlist 可能在赋值后覆盖回旧分组
            activeGroupTab = groups[0];
            await loadWatchlist();
        }

        // 右键菜单定位: 跟随鼠标坐标, 靠近屏幕边缘时自动翻转/钳制避免溢出
        function positionContextMenu(menu, x, y) {
            const pad = 6;   // 距视口边缘的安全间距
            // 先放到屏幕外测量真实尺寸 (菜单内容未知, 不能用固定估值)
            menu.style.left = '-9999px';
            menu.style.top = '-9999px';
            menu.style.right = 'auto';
            menu.style.bottom = 'auto';
            const mw = menu.offsetWidth || 140;
            const mh = menu.offsetHeight || 90;
            const vw = window.innerWidth;
            const vh = window.innerHeight;
            // 水平: 默认在鼠标右侧展开; 若右边放不下则向左翻转; 再不够则贴边钳制
            // 同时保证左缘不越过 pad (鼠标贴左边缘时菜单不贴屏幕边)
            let left = x;
            if (left + mw + pad > vw) {
                const flipped = x - mw;      // 向左翻转 (菜单右缘对齐鼠标)
                left = flipped >= pad ? flipped : Math.max(pad, vw - mw - pad);
            }
            if (left < pad) left = pad;
            // 垂直: 默认在鼠标下方展开; 若下方放不下则向上翻转; 再不够则贴边钳制
            let top = y;
            if (top + mh + pad > vh) {
                const flipped = y - mh;
                top = flipped >= pad ? flipped : Math.max(pad, vh - mh - pad);
            }
            if (top < pad) top = pad;
            menu.style.left = Math.round(left) + 'px';
            menu.style.top = Math.round(top) + 'px';
        }

        // 合约卡片右键菜单: 查看基本信息 / 修改分组 / 重命名 / 删除订阅 (替代卡片上的 × 按钮)
        function showContractMenu(symbol, x, y) {
            closeContractMenu();
            const menu = document.createElement('div');
            menu.id = 'contract-menu';
            menu.style.cssText = 'position:fixed; z-index:130; background:#1b1e24; border:1px solid var(--border-color); border-radius:6px; box-shadow:0 6px 18px rgba(0,0,0,0.5); padding:4px; min-width:130px;';
            menu.innerHTML = `
                <div style="padding:6px 12px; font-size:11px; cursor:pointer; border-radius:4px; color:var(--text-muted); white-space:nowrap;"
                     onmouseenter="this.style.background='#232731'" onmouseleave="this.style.background=''"
                     onclick="event.stopPropagation(); closeContractMenu(); showContractInfo('${symbol}')">查看基本信息</div>
                <div style="padding:6px 12px; font-size:11px; cursor:pointer; border-radius:4px; color:var(--text-muted); white-space:nowrap;"
                     onmouseenter="this.style.background='#232731'" onmouseleave="this.style.background=''"
                     onclick="event.stopPropagation(); closeContractMenu(); openGroupEdit('${symbol}')">修改分组</div>
                <div style="padding:6px 12px; font-size:11px; cursor:pointer; border-radius:4px; color:var(--text-muted); white-space:nowrap;"
                     onmouseenter="this.style.background='#232731'" onmouseleave="this.style.background=''"
                     onclick="event.stopPropagation(); closeContractMenu(); renameContractPrompt('${symbol}')">重命名</div>
                <div style="padding:6px 12px; font-size:11px; cursor:pointer; border-radius:4px; color:#f87171; white-space:nowrap;"
                     onmouseenter="this.style.background='#2b1214'" onmouseleave="this.style.background=''"
                     onclick="event.stopPropagation(); closeContractMenu(); deleteContract('${symbol}')">删除订阅</div>`;
            document.body.appendChild(menu);
            positionContextMenu(menu, x, y);
        }
        function closeContractMenu() {
            const m = document.getElementById('contract-menu');
            if (m) m.remove();
        }

        // 合约基本信息弹窗: 基础属性 + 合约规格 + 实时行情 + 当日区间
        const EXCHANGE_NAMES = { SHFE: '上海期货交易所', DCE: '大连商品交易所', CZCE: '郑州商品交易所', CFFEX: '中国金融期货交易所', INE: '上海国际能源交易中心', GFEX: '广州期货交易所' };
        // 合约规格 (price_tick / volume_multiple) 由 /api/v1/instruments 提供, 失败时回退到本地表
        const SPEC_FALLBACK = {
            FU: { tick: 1, mult: 10 }, SC: { tick: 0.1, mult: 1000 }, BU: { tick: 1, mult: 10 },
            SA: { tick: 1, mult: 20 }, RB: { tick: 1, mult: 10 }, HC: { tick: 1, mult: 10 },
            I: { tick: 0.5, mult: 100 }, JM: { tick: 0.5, mult: 60 }, J: { tick: 0.5, mult: 100 },
            M: { tick: 1, mult: 10 }, Y: { tick: 2, mult: 10 }, P: { tick: 2, mult: 10 },
            A: { tick: 1, mult: 10 }, C: { tick: 1, mult: 10 }, CF: { tick: 5, mult: 5 },
            SR: { tick: 1, mult: 10 }, TA: { tick: 1, mult: 5 }, MA: { tick: 1, mult: 10 },
            FG: { tick: 1, mult: 20 }, RU: { tick: 5, mult: 10 }, AG: { tick: 1, mult: 15 },
            AU: { tick: 0.02, mult: 1000 }, CU: { tick: 10, mult: 5 }, AL: { tick: 5, mult: 5 },
            ZN: { tick: 5, mult: 5 }, NI: { tick: 10, mult: 1 }, IC: { tick: 0.2, mult: 200 },
            IF: { tick: 0.2, mult: 300 }, IH: { tick: 0.2, mult: 300 }, IM: { tick: 0.2, mult: 200 },
        };
        // 交易时段描述 (按交易所/品种)
        function tradingSessionsOf(symbol, exchange) {
            const p = (symbol.replace(/[0-9]+$/, '') || '').toUpperCase();
            if (exchange === 'CFFEX') return '09:30~11:30, 13:00~15:00 (无夜盘)';
            if (['SC'].includes(p)) return '21:00~02:30 夜盘, 09:00~10:15, 10:30~11:30, 13:30~15:00';
            if (['AU', 'AG'].includes(p)) return '21:00~02:30 夜盘, 09:00~10:15, 10:30~11:30, 13:30~15:00';
            if (['CU', 'AL', 'ZN', 'PB', 'NI', 'SN'].includes(p)) return '21:00~01:00 夜盘, 09:00~10:15, 10:30~11:30, 13:30~15:00';
            return '21:00~23:00 夜盘, 09:00~10:15, 10:30~11:30, 13:30~15:00';
        }
        function infoRow(label, value, opts) {
            const o = opts || {};
            const color = o.color || '#e2e8f0';
            const mono = o.mono === false ? '' : 'font-family:monospace;';
            const weight = o.strong ? 'font-weight:600;' : '';
            return `<div style="display:flex; justify-content:space-between; align-items:baseline; padding:5px 0; border-bottom:1px solid rgba(255,255,255,0.05);">
                <span style="font-size:11px; color:var(--text-muted); flex-shrink:0;">${label}</span>
                <span style="font-size:12px; color:${color}; ${mono} ${weight} text-align:right; margin-left:12px; word-break:break-all;">${value}</span>
            </div>`;
        }
        function infoSection(title) {
            return `<div style="font-size:10px; color:#7dd3fc; letter-spacing:1px; margin:12px 0 2px;">${title}</div>`;
        }
        async function showContractInfo(symbol) {
            const modal = document.getElementById('info-modal');
            if (!modal) return;
            const meta = watchlist.find(w => w.symbol === symbol) || {};
            // 先展示已缓存信息 + 加载态, 再异步补齐
            document.getElementById('info-modal-name').textContent = meta.name || mainLabel(symbol) || symbol;
            document.getElementById('info-modal-symbol').textContent = symbol;
            const badge = document.getElementById('info-modal-badge');
            badge.textContent = '';
            badge.style.background = '';
            document.getElementById('info-modal-sub').textContent = '加载中…';
            document.getElementById('info-modal-body').innerHTML = '<div style="padding:16px 0; text-align:center; color:var(--text-muted); font-size:11px;">正在获取合约信息…</div>';
            modal.style.display = 'flex';

            // 三路数据并行: 实时行情 / 合约规格 / 当日 1m 序列(算区间统计)
            const [quoteRes, instRes, barsRes] = await Promise.allSettled([
                fetch(`/api/v1/quotes/${encodeURIComponent(symbol)}`).then(r => r.ok ? r.json() : null),
                fetch('/api/v1/instruments').then(r => r.ok ? r.json() : null),
                fetch(`/api/v1/bars/${encodeURIComponent(symbol)}?period=1m&limit=700`).then(r => r.ok ? r.json() : null),
            ]);
            // 弹窗可能已被用户关闭, 或已切换到别的合约
            if (modal.style.display === 'none') return;
            if (document.getElementById('info-modal-symbol').textContent !== symbol) return;

            const q = quoteRes.status === 'fulfilled' ? quoteRes.value : null;
            const instData = instRes.status === 'fulfilled' ? instRes.value : null;
            const barsData = barsRes.status === 'fulfilled' ? barsRes.value : null;

            // 合约规格: 接口优先, 回退本地表
            let spec = null;
            if (instData && instData.instruments) {
                const hit = instData.instruments.find(x => x.symbol === symbol);
                if (hit && hit.price_tick != null) spec = { tick: hit.price_tick, mult: hit.volume_multiple };
            }
            const prod = (symbol.replace(/[0-9]+$/, '') || '').toUpperCase();
            if (!spec && SPEC_FALLBACK[prod]) spec = { tick: SPEC_FALLBACK[prod].tick, mult: SPEC_FALLBACK[prod].mult };

            const exchange = (q && q.exchange) || meta.exchange || '';
            const name = meta.name || (instData && instData.instruments ? ((instData.instruments.find(x => x.symbol === symbol) || {}).name || '') : '') || mainLabel(symbol) || symbol;
            document.getElementById('info-modal-name').textContent = name;

            // 主力标记
            const dom = meta.dominant_contract;
            if (dom === symbol) {
                badge.textContent = '主力';
                badge.style.background = '#14532d'; badge.style.color = '#86efac';
            } else if (mainLabel(symbol)) {
                badge.textContent = '主连';
                badge.style.background = '#1e3a5f'; badge.style.color = '#7dd3fc';
            } else if (dom) {
                badge.textContent = '次主力';
                badge.style.background = '#3f2d15'; badge.style.color = '#fbbf24';
            } else {
                badge.textContent = '普通';
                badge.style.background = '#2a2e37'; badge.style.color = '#8492a6';
            }
            const exchName = EXCHANGE_NAMES[exchange] || exchange || '未知';
            document.getElementById('info-modal-sub').textContent = exchName + (exchange ? ' · ' + exchange : '');

            // 合约月份解析: 4 位 YYMM (2611 → 2026 年 11 月); 3 位 YMM (701 → 2027 年 01 月, 郑商所);
            // 主连/指数码 (FU0 / IF0 等, 数字仅 1 位 0) 不表示月份
            const mth = (symbol.match(/(\d+)$/) || [])[1] || '';
            let mthText = '--';
            if (mainLabel(symbol)) {
                mthText = '主连 (跟随主力)';
            } else if (mth.length === 4) {
                mthText = '20' + mth.slice(0, 2) + ' 年 ' + mth.slice(2, 4) + ' 月';
            } else if (mth.length === 3) {
                // 郑商所 3 位码 YMM: '701' → 年份末位 7 → 2027 年, 月份 01
                mthText = '202' + mth.slice(0, 1) + ' 年 ' + mth.slice(1, 3) + ' 月';
            } else if (mth.length === 1 || mth.length === 2) {
                mthText = '20' + mth.padStart(2, '0') + ' 年 01 月';
            }

            // 数值格式化
            const num = (v, dec) => (v === null || v === undefined || v === '' || isNaN(parseFloat(v))) ? '--' : Number(parseFloat(v)).toLocaleString('zh-CN', { minimumFractionDigits: dec || 0, maximumFractionDigits: dec || 0 });
            const priceDec = spec && spec.tick ? (String(spec.tick).split('.')[1] || '').length : 1;
            let html = '';

            // ── 实时行情 ──
            html += infoSection('实时行情');
            const last = q ? parseFloat(q.last_price) : null;
            const prevSettle = q ? parseFloat(q.prev_settle) : null;
            let chg = '--', chgPct = '--', chgColor = '#e2e8f0';
            if (last !== null && prevSettle !== null && !isNaN(last) && !isNaN(prevSettle) && prevSettle !== 0) {
                const d = last - prevSettle;
                const p = d / prevSettle * 100;
                chgColor = d > 0 ? '#ef4444' : (d < 0 ? '#10b981' : '#e2e8f0');
                chg = (d > 0 ? '+' : '') + d.toFixed(priceDec);
                chgPct = (p > 0 ? '+' : '') + p.toFixed(2) + '%';
            }
            html += infoRow('最新价', q ? num(q.last_price, priceDec) : '--', { color: chgColor, strong: true });
            html += infoRow('涨跌 / 幅度', chg + '  (' + chgPct + ')', { color: chgColor });
            html += infoRow('昨结算', prevSettle !== null && !isNaN(prevSettle) ? num(prevSettle, priceDec) : '--');
            html += infoRow('今开 / 昨结', (q ? num(q.day_open, priceDec) : '--') + ' / ' + (prevSettle !== null && !isNaN(prevSettle) ? num(prevSettle, priceDec) : '--'));
            html += infoRow('今高 / 今低', (q ? num(q.day_high, priceDec) : '--') + ' / ' + (q ? num(q.day_low, priceDec) : '--'));
            const b1 = q ? num(q.bid_price_1, priceDec) : '--', a1 = q ? num(q.ask_price_1, priceDec) : '--';
            const bv1 = q ? num(q.bid_volume_1, 0) : '--', av1 = q ? num(q.ask_volume_1, 0) : '--';
            html += infoRow('买一 / 卖一', b1 + ' (' + bv1 + ')  /  ' + a1 + ' (' + av1 + ')');
            html += infoRow('持仓量', q ? num(q.open_interest, 0) : (meta.open_interest != null ? Math.round(parseFloat(meta.open_interest)).toLocaleString('zh-CN') : '--'));
            html += infoRow('当日累计成交', q ? num(q.volume, 0) : '--');

            // ── 合约规格 ──
            html += infoSection('合约规格');
            html += infoRow('交易所', exchName);
            html += infoRow('合约代码', symbol);
            html += infoRow('合约月份', mthText);
            html += infoRow('最小变动价位', spec ? spec.tick + ' 元' : '--');
            html += infoRow('合约乘数', spec ? spec.mult + ' 单位/手' : '--');
            if (spec && spec.tick && spec.mult) {
                html += infoRow('一跳价值', (spec.tick * spec.mult) + ' 元/手');
            }
            html += infoRow('交易时段', tradingSessionsOf(symbol, exchange), { mono: false });

            // ── 当日统计 (由 1m 序列计算) ──
            html += infoSection('当日统计 (按 1m 序列)');
            if (barsData && barsData.bars && barsData.bars.length) {
                const bars = barsData.bars;
                // 以最后一根的交易日为准 (夜盘+日盘同属一个交易日)
                const td = bars[bars.length - 1].trading_day;
                const dayBars = bars.filter(b => b.trading_day === td);
                let sumV = 0, sumT = 0, hi = -Infinity, lo = Infinity, hiT = '', loT = '';
                for (const b of dayBars) {
                    sumV += b.volume || 0;
                    sumT += parseFloat(b.turnover) || 0;
                    const h = parseFloat(b.high_price), l = parseFloat(b.low_price);
                    if (h > hi) { hi = h; hiT = (b.bar_time || '').slice(11, 16); }
                    if (l < lo) { lo = l; loT = (b.bar_time || '').slice(11, 16); }
                }
                const nightBars = dayBars.filter(b => (b.bar_time || '').slice(11, 16) >= '20:00' || (b.bar_time || '').slice(11, 16) < '03:00');
                const daySessionBars = dayBars.filter(b => !((b.bar_time || '').slice(11, 16) >= '20:00' || (b.bar_time || '').slice(11, 16) < '03:00'));
                const nv = nightBars.reduce((s, b) => s + (b.volume || 0), 0);
                const dv = daySessionBars.reduce((s, b) => s + (b.volume || 0), 0);
                html += infoRow('交易日', td.slice(0, 4) + '-' + td.slice(4, 6) + '-' + td.slice(6, 8));
                html += infoRow('K线根数', dayBars.length + ' 根 1m');
                html += infoRow('夜盘 / 日盘', nightBars.length + ' / ' + daySessionBars.length + ' 根');
                const volumeText=(items,total)=>items.some(b=>b.volume==null)
                    ? `未知（含${items.filter(b=>b.volume==null).length}根量缺失）` : total.toLocaleString('zh-CN')+' 手';
                html += infoRow('夜盘成交', volumeText(nightBars,nv));
                html += infoRow('日盘成交', volumeText(daySessionBars,dv));
                html += infoRow('当日成交合计', volumeText(dayBars,sumV), { strong: true });
                html += infoRow('当日最高', hi === -Infinity ? '--' : num(hi, priceDec) + '  @' + hiT, { color: '#ef4444' });
                html += infoRow('当日最低', lo === Infinity ? '--' : num(lo, priceDec) + '  @' + loT, { color: '#10b981' });
                if (sumT > 0) {
                    // 成交额单位为元, 换算成亿元更易读
                    const yi = sumT / 1e8;
                    html += infoRow('当日成交额', yi >= 1 ? yi.toFixed(2) + ' 亿元' : (sumT / 1e4).toFixed(2) + ' 万元');
                }
                if (q && q.timestamp) {
                    html += infoRow('行情时间', (q.timestamp || '').replace('T', ' '));
                }
                html += `<div style="font-size:10px; color:var(--text-muted); line-height:1.6; margin-top:8px;">
                    注: 上方「今高/今低」取自实时报价快照, 含集合竞价与逐笔极值; 本段极值由 1m 序列聚合, 两者口径不同, 允许小幅差异。
                </div>`;
            } else {
                html += `<div style="padding:10px 0; font-size:11px; color:var(--text-muted);">暂无当日分钟数据</div>`;
            }
            // ── 分组 ──
            html += infoSection('订阅信息');
            html += infoRow('所属分组', groupListOf(meta).join('、') || '默认', { mono: false });
            html += infoRow('主力合约', dom ? dom : '--');
            html += infoRow('显示名称', meta.name || name, { mono: false });

            document.getElementById('info-modal-body').innerHTML = html;
        }
        function closeInfoModal() {
            const modal = document.getElementById('info-modal');
            if (modal) modal.style.display = 'none';
        }
        // 分组标签右键菜单: 重命名 / 删除分组 (默认分组不可)
        function showGroupMenu(group, x, y) {
            closeContractMenu();
            const isDefault = group === '默认';
            const menu = document.createElement('div');
            menu.id = 'contract-menu';
            menu.style.cssText = 'position:fixed; z-index:130; background:#1b1e24; border:1px solid var(--border-color); border-radius:6px; box-shadow:0 6px 18px rgba(0,0,0,0.5); padding:4px; min-width:130px;';
            let inner = '';
            if (isDefault) {
                inner = '<div style="padding:6px 12px; font-size:11px; color:var(--text-muted); white-space:nowrap;">默认分组不可删除或重命名</div>';
            } else {
                inner = `
                <div style="padding:6px 12px; font-size:11px; cursor:pointer; border-radius:4px; color:var(--text-muted); white-space:nowrap;"
                     onmouseenter="this.style.background='#232731'" onmouseleave="this.style.background=''"
                     onclick="event.stopPropagation(); closeContractMenu(); renameGroupPrompt('${group}')">重命名</div>
                <div style="padding:6px 12px; font-size:11px; cursor:pointer; border-radius:4px; color:#f87171; white-space:nowrap;"
                     onmouseenter="this.style.background='#2b1214'" onmouseleave="this.style.background=''"
                     onclick="event.stopPropagation(); closeContractMenu(); deleteGroupTab('${group}')">删除分组</div>`;
            }
            menu.innerHTML = inner;
            document.body.appendChild(menu);
            positionContextMenu(menu, x, y);
        }
        document.addEventListener('contextmenu', (e) => {
            const tabEl = e.target && e.target.closest ? e.target.closest('.group-tab') : null;
            if (tabEl && tabEl.dataset.group) {
                e.preventDefault();
                e.stopPropagation();
                showGroupMenu(tabEl.dataset.group, e.clientX, e.clientY);
                return;
            }
            const card = e.target && e.target.closest ? e.target.closest('.contract-item') : null;
            if (card && card.dataset.symbol) {
                e.preventDefault();
                e.stopPropagation();
                showContractMenu(card.dataset.symbol, e.clientX, e.clientY);
            } else {
                closeContractMenu();
            }
        });
        document.addEventListener('click', () => closeContractMenu());

        function renameContractPrompt(symbol) {
            openRenameModal('contract', symbol);
        }
        function renameGroupPrompt(g) {
            openRenameModal('group', g);
        }
        let renameKind = 'contract';   // 'contract' | 'group'
        let renameOldName = '';
        function openRenameModal(kind, oldName) {
            renameKind = kind;
            renameOldName = oldName;
            const modal = document.getElementById('rename-modal');
            modal.style.display = 'flex';
            document.getElementById('rename-modal-title').textContent = kind === 'group' ? '重命名分组' : '重命名合约';
            document.getElementById('rename-symbol').textContent = oldName;
            const inp = document.getElementById('rename-input');
            inp.value = oldName;
            setTimeout(() => { inp.focus(); inp.select(); }, 60);
        }
        function closeRenameModal() {
            document.getElementById('rename-modal').style.display = 'none';
        }
        async function submitRenameModal() {
            const newName = document.getElementById('rename-input').value.trim();
            if (!newName) return;
            if (renameKind === 'group') {
                if (newName === renameOldName) { closeRenameModal(); return; }
                try {
                    const res = await fetch('/api/v1/watchlist/group/rename', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ old: renameOldName, new: newName })
                    });
                    if (!res.ok) {
                        const err = await res.json().catch(() => ({}));
                        alert('重命名失败: ' + (err.detail || '未知错误'));
                        return;
                    }
                } catch (e) { alert('重命名失败: ' + e); return; }
                if (activeGroupTab === renameOldName) activeGroupTab = newName;
                closeRenameModal();
                await loadWatchlist();
                return;
            }
            const symbol = document.getElementById('rename-symbol').value;
            try {
                await fetch('/api/v1/watchlist/rename', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ symbol, name: newName })
                });
            } catch (e) { console.error('rename error', e); }
            closeRenameModal();
            await loadWatchlist();
        }

        function calculateEMA(data, period) {
            if (data.length < period) return [];
            const k = 2 / (period + 1);
            let emaData = [];
            let sum = 0;
            for (let i = 0; i < period; i++) sum += data[i].close;
            let prevEma = sum / period;
            emaData.push({ time: data[period - 1].time, value: prevEma });

            for (let i = period; i < data.length; i++) {
                prevEma = (data[i].close * k) + (prevEma * (1 - k));
                emaData.push({ time: data[i].time, value: prevEma });
            }
            return emaData;
        }

        function mainLabel(symbol) {
            const product = symbol.replace(/[0-9]+$/, '');
            return (product && symbol === product + '0') ? product + '主连' : '';
        }

        let activeGroupTab = null;    // 当前展示的分组 (分组标签页模式)
        // 合约所属分组列表 (支持多分组; 后端返回 groups 数组)
        const groupListOf = w => (w.groups && w.groups.length) ? w.groups : [w.group || '默认'];

        function renderContracts() {
            const el = document.getElementById('contract-list');
            // 重渲染前保存分组滚动位置 (避免点击下方合约后视图跳回顶部)
            const prevBody = el.querySelector('.group-body');
            const scrollMemo = prevBody ? prevBody.scrollTop : 0;
            if (!watchlist.length) {
                activeGroupTab = null;
                el.innerHTML = '<div style="color:var(--text-muted); font-size:12px; padding:6px;">暂无订阅合约, 请在顶部搜索框添加</div>';
                return;
            }
            // 分组: 未归组的合约归入"默认"组 (排最后); 同一合约可属于多个分组 (在每个分组都显示)
            const byName = {};
            watchlist.forEach(inst => {
                groupListOf(inst).forEach(g => {
                    (byName[g] = byName[g] || []).push(inst);
                });
            });
            // 标签顺序 = 持久化的分组顺序 (拖动标签排序后保持)
            // 兜底: 卡片上出现但分组列表里没有的名称也要建标签 (分组列表短暂滞后时不丢标签)
            const ordered = groupNames.filter((g, i, arr) => arr.indexOf(g) === i);
            Object.keys(byName).forEach(g => {
                if (!ordered.includes(g)) ordered.push(g);
            });
            if (byName['默认'] && !ordered.includes('默认')) ordered.push('默认');
            // 当前展示的分组 (点击标签切换); 失效时回落到第一个
            if (!activeGroupTab || !ordered.includes(activeGroupTab)) activeGroupTab = ordered[0] || '默认';
            const members = byName[activeGroupTab] || [];
            el.innerHTML = `
                <div id="group-tabs" style="display:flex; flex-wrap:wrap; gap:4px; margin-bottom:8px;">
                    ${ordered.map(g => {
                        const encoded = safeInlineValue(g);
                        return `<div class="group-tab ${g === activeGroupTab ? 'active' : ''}" draggable="true" data-group="${escapeHTML(g)}"
                         onclick="switchGroupTab(decodeURIComponent('${encoded}'))" title="点击查看该分组; 拖动合约到标签可移动分组; 右键重命名/删除">
                        ${escapeHTML(g)} <span style="opacity:0.65;">(${(byName[g] || []).length})</span>
                    </div>`;
                    }).join('')}
                </div>
                <div class="group-body" data-group="${escapeHTML(activeGroupTab)}">${members.map(inst => contractCard(inst)).join('')}</div>`;
            const bodyEl = el.querySelector('.group-body');
            bodyEl.scrollTop = scrollMemo;
            const active = el.querySelector('.contract-item.active');
            if (active) {
                const above = active.offsetTop - bodyEl.offsetTop;
                if (above < bodyEl.scrollTop || above + active.offsetHeight > bodyEl.scrollTop + bodyEl.clientHeight) {
                    bodyEl.scrollTop = Math.max(0, above - bodyEl.clientHeight / 2 + active.offsetHeight / 2);
                }
            }
            wireContractDnD(el);
        }

        function switchGroupTab(g) {
            activeGroupTab = g;
            renderContracts();
        }

        // 删除分组标签: 组内合约回到默认分组 (后端行为)
        async function deleteGroupTab(g) {
            if (g === '默认') return;
            try {
                await fetch('/api/v1/watchlist/group/' + encodeURIComponent(g), { method: 'DELETE' });
            } catch (e) { console.error('delete group error', e); }
            if (activeGroupTab === g) activeGroupTab = null;
            await loadWatchlist();
        }

        // 拖拽合约到分组标签: 加入该分组 (保留其他所属, 多分组叠加)
        async function assignCardToGroup(sym, group) {
            const meta = watchlist.find(w => w.symbol === sym);
            const cur = groupListOf(meta || {}).slice();
            if (cur.includes(group)) { activeGroupTab = group; renderContracts(); return; }
            cur.push(group);
            try {
                await fetch('/api/v1/watchlist/assign-groups', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ symbol: sym, groups: cur })
                });
            } catch (e) { console.error('assign-groups error', e); }
            if (meta) meta.groups = cur;
            if (!groupNames.includes(group)) groupNames.push(group);
            activeGroupTab = group;   // 切到目标分组查看移动结果
            renderContracts();
        }

        function contractCard(inst) {
            const dom = inst.dominant_contract;
            const isDom = dom === inst.symbol;
            const isSynthetic = mainLabel(inst.symbol) !== '';
            const symbol = escapeHTML(inst.symbol);
            const symbolArg = safeInlineValue(inst.symbol);
            const name = escapeHTML(inst.name || mainLabel(inst.symbol));
            // 非主力且非合成主连的卡片显示"切主连"按钮, 点击原地替换为当前主力月份
            const switchBtn = (!isDom && dom && !isSynthetic)
                ? `<button title="切换到当前主力 ${escapeHTML(dom)}" onclick="event.stopPropagation(); switchCardToMain(decodeURIComponent('${symbolArg}'))"
                      style="background:#1e3a5f; color:#7dd3fc; border:none; border-radius:3px; padding:2px 6px; font-size:10px; cursor:pointer;">切主连</button>`
                : '';
            const mainBadge = isDom
                ? '<span class="contract-badge" style="background:#14532d; color:#86efac; padding:1px 5px; border-radius:3px; font-size:10px;">主力</span>'
                : '';
            const chg = chgTexts(inst);   // 涨幅金额 / 幅度 (基准: 昨结算)
            return `
                <div class="contract-item ${inst.symbol === currentSymbol ? 'active' : ''}" draggable="true" data-symbol="${symbol}" onclick="selectSymbol(decodeURIComponent('${symbolArg}'))">
                    <div class="contract-row" style="padding-right:0;">
                        <span class="contract-symbol" style="color:#fff;">${name}</span>
                        ${mainBadge}
                        <span class="price-val" id="price-${symbol}">${escapeHTML(displayPrice(inst.last_price, inst.symbol))}</span>
                    </div>
                    <div class="contract-row" style="font-size:11px; color:var(--text-muted); padding-right:0;">
                        <span style="letter-spacing:0.5px;">${symbol}</span>
                        <span style="display:inline-flex; align-items:center; gap:5px;">
                            <span id="oi-${symbol}">OI: ${inst.open_interest != null ? Math.round(parseFloat(inst.open_interest)) : '--'}</span>
                            ${switchBtn}
                        </span>
                    </div>
                    <div class="contract-row" style="margin-bottom:0; align-items:baseline;">
                        <span style="font-size:10px; color:var(--text-muted);">
                            <span id="chg-${inst.symbol}" class="chg-val ${chg.cls}">${chg.diff}</span>
                            <span style="color:var(--border-color); margin:0 4px;">|</span>
                            <span id="chgpct-${inst.symbol}" class="chg-val ${chg.cls}">${chg.pct}</span>
                        </span>
                        <span style="font-size:10px; color:var(--text-muted);">${chg.label}</span>
                    </div>
                </div>`;
        }

        function addGroupPrompt() {
            // 使用页面内弹窗 (原生 prompt 在内嵌浏览器中可能被屏蔽)
            const modal = document.getElementById('group-modal');
            modal.style.display = 'flex';
            const inp = document.getElementById('group-name-input');
            inp.value = '';
            setTimeout(() => inp.focus(), 60);
        }
        function closeGroupModal() {
            document.getElementById('group-modal').style.display = 'none';
        }
        async function submitGroupModal() {
            const name = document.getElementById('group-name-input').value.trim();
            if (!name) return;
            try {
                const res = await fetch('/api/v1/watchlist/group', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name })
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    alert('创建分组失败: ' + (err.detail || '未知错误'));
                    return;
                }
            } catch (e) { alert('创建分组失败: ' + e); return; }
            closeGroupModal();
            await loadWatchlist();
            activeGroupTab = name;   // 切到新建分组
            renderContracts();
        }

        async function cycleGroup(symbol) {
            // 点击分组标签: 该合约切换到下一个分组
            const inst = watchlist.find(w => w.symbol === symbol);
            const cur = inst ? (inst.group || '默认') : '默认';
            const ordered = [...groupNames.filter(g => g !== '默认'), '默认'];
            const idx = ordered.indexOf(cur);
            const next = ordered[(idx + 1) % ordered.length];
            try {
                await fetch('/api/v1/watchlist/assign', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ symbol, group: next })
                });
            } catch (e) { console.error('assign group error', e); }
            await loadWatchlist();
            activeGroupTab = next;   // 切到目标分组, 让移动后的合约可见
            renderContracts();
        }

        // ---------------- 卡片/分组拖动排序 (HTML5 DnD, 顺序持久化) ----------------
        let dragMode = null;     // 'card' | 'group'
        let dragPayload = null;

        function wireContractDnD(container) {
            container.querySelectorAll('.contract-item').forEach(card => {
                card.addEventListener('dragstart', (e) => {
                    dragMode = 'card';
                    dragPayload = card.dataset.symbol;
                    e.dataTransfer.setData('text/plain', dragPayload);
                    e.dataTransfer.effectAllowed = 'move';
                    card.classList.add('dragging');
                });
                card.addEventListener('dragend', () => {
                    card.classList.remove('dragging');
                    container.querySelectorAll('.drag-over').forEach(x => x.classList.remove('drag-over'));
                    dragMode = null;
                });
                card.addEventListener('dragover', (e) => {
                    if (dragMode !== 'card') return;
                    e.preventDefault();
                    e.stopPropagation();
                    e.dataTransfer.dropEffect = 'move';
                    card.classList.add('drag-over');
                });
                card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
                card.addEventListener('drop', (e) => {
                    if (dragMode !== 'card') return;
                    e.preventDefault();
                    e.stopPropagation();
                    card.classList.remove('drag-over');
                    const sym = e.dataTransfer.getData('text/plain') || dragPayload;
                    if (sym && sym !== card.dataset.symbol) handleCardDrop(sym, card.dataset.symbol);
                });
            });

            container.querySelectorAll('.group-tab').forEach(tab => {
                // 分组标签: 拖动标签到另一标签 = 分组排序
                tab.addEventListener('dragstart', (e) => {
                    dragMode = 'group';
                    dragPayload = tab.dataset.group;
                    e.dataTransfer.setData('text/plain', dragPayload);
                    e.dataTransfer.effectAllowed = 'move';
                    tab.classList.add('dragging');
                });
                tab.addEventListener('dragend', () => {
                    tab.classList.remove('dragging');
                    container.querySelectorAll('.drag-over').forEach(x => x.classList.remove('drag-over'));
                    dragMode = null;
                });
                tab.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    tab.classList.add('drag-over');
                });
                tab.addEventListener('dragleave', () => tab.classList.remove('drag-over'));
                tab.addEventListener('drop', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    tab.classList.remove('drag-over');
                    const g = e.dataTransfer.getData('text/plain') || dragPayload;
                    if (!g) return;
                    if (dragMode === 'group' && g !== tab.dataset.group) {
                        handleGroupDrop(g, tab.dataset.group);   // 标签→标签: 分组排序
                    } else if (dragMode === 'card') {
                        assignCardToGroup(g, tab.dataset.group); // 合约→标签: 移动到该分组
                    }
                });
            });

            // 组体接受合约级拖放 (落点为该组内的其他合约, 由卡片自身处理)
        }

        async function handleCardDrop(dragged, targetSymbol) {
            const target = watchlist.find(w => w.symbol === targetSymbol);
            if (!target) return;
            const draggedMeta = watchlist.find(w => w.symbol === dragged);
            // 多分组: 拖到目标合约上 = 与目标同分组 (集合式覆盖), 顺序就近排列
            const targetGroups = groupListOf(target).slice();
            const draggedGroups = groupListOf(draggedMeta || {}).slice();
            const sameGroups = targetGroups.length === draggedGroups.length && targetGroups.every(g => draggedGroups.includes(g));

            // 全局顺序持久化 (把拖动合约紧邻目标排列)
            const flatOrder = [];
            watchlist.forEach(w => flatOrder.push(w.symbol));

            // 本地模型重排 (方向感知: 向下拖插入目标之后, 向上拖插入目标之前)
            const origDi = flatOrder.indexOf(dragged);
            const origTi = flatOrder.indexOf(targetSymbol);
            flatOrder.splice(origDi, 1);
            const tiAfterRemoval = flatOrder.indexOf(targetSymbol);
            flatOrder.splice(origDi < origTi ? tiAfterRemoval + 1 : tiAfterRemoval, 0, dragged);

            const post = (u, b) => fetch(u, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(b)
            });
            try {
                if (!sameGroups) await post('/api/v1/watchlist/assign-groups', { symbol: dragged, groups: targetGroups });
                await post('/api/v1/watchlist/reorder', { symbols: flatOrder });
            } catch (e) { console.error('drop persist error', e); }
            await loadWatchlist();
        }

        async function handleGroupDrop(dragged, target) {
            if (dragged === target) return;
            const order = groupNames.filter(g => g !== dragged);
            const ti = order.indexOf(target);
            if (ti === -1) order.push(dragged);
            else order.splice(ti, 0, dragged);
            groupNames = order;
            try {
                await fetch('/api/v1/watchlist/group/reorder', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ groups: order })
                });
            } catch (e) { console.error('group reorder error', e); }
            await loadWatchlist();
        }

        async function switchCardToMain(symbol) {
            // 原地换月: 把卡片上的合约替换为该品种当前主力 (如 FU2610 -> FU2611)
            const meta = watchlist.find(w => w.symbol === symbol);
            const target = meta && meta.dominant_contract;
            if (!target || target === symbol) return;
            const wasCurrent = currentSymbol === symbol;
            try {
                await fetch('/api/v1/watchlist/' + encodeURIComponent(symbol), { method: 'DELETE' });
                await fetch('/api/v1/watchlist', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ symbol: target })
                });
            } catch (e) { console.error('switch main error', e); }
            await loadWatchlist();
            if (wasCurrent) {
                // 正在看的合约被换月, 切到新主力
                const m = watchlist.find(w => w.symbol === target);
                selectSymbol(target, m ? m.name : '');
            }
            renderContracts();
        }

        async function deleteContract(symbol) {
            try {
                await fetch('/api/v1/watchlist/' + encodeURIComponent(symbol), { method: 'DELETE' });
            } catch (e) { console.error('unsubscribe error', e); }
            await loadWatchlist();
            if (currentSymbol === symbol) {
                // 删除的是当前合约时, 切换到剩余列表的第一个
                globalThis.invalidateMarketSimulator?.();
                currentSymbol = watchlist.length ? watchlist[0].symbol : '';
                if (currentSymbol) {
                    selectSymbol(currentSymbol);
                } else {
                    document.getElementById('current-contract-header').innerText = '请订阅合约';
                    updateMainSwitchBtn();
                }
            }
            renderContracts();
        }

        async function loadWatchlist() {
            try {
                const res = await fetch('/api/v1/watchlist');
                if (!res.ok) return;
                const data = await res.json();
                watchlist = data.watchlist || [];
                groupNames = (data.groups && data.groups.length) ? data.groups : ['默认'];
                if (!watchlist.some(w => w.symbol === currentSymbol) && watchlist.length) {
                    globalThis.invalidateMarketSimulator?.();
                    currentSymbol = watchlist[0].symbol;
                }
                renderContracts();
            } catch (e) { console.error('loadWatchlist error', e); }
        }

        async function subscribeSymbol(symbol) {
            try {
                await fetch('/api/v1/watchlist', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ symbol: symbol })
                });
            } catch (e) { console.error('subscribe error', e); }
            await loadWatchlist();
        }

        // 搜索框状态: null=展示品种类别, 非空=当前浏览的品种前缀
        let universeCategory = null;

        function renderUniverseContract(c) {
            const sel = pickSelectedSymbol === c.symbol;
            const symbol = escapeHTML(c.symbol);
            const symbolArg = safeInlineValue(c.symbol);
            return `
                <div class="contract-item ${sel ? 'pick-selected' : ''}" style="padding:6px 8px; margin-bottom:4px;" onclick="pickUniverse(decodeURIComponent('${symbolArg}'))">
                    <div class="contract-row" style="margin-bottom:2px;">
                        <span style="font-weight:600; font-size:12px;">${symbol}${c.subscribed ? ' <span style="color:#10b981;">●</span>' : ''}</span>
                        <span style="font-size:11px; color:var(--text-muted);">${escapeHTML(c.exchange)}</span>
                    </div>
                    <div class="contract-row" style="font-size:11px; color:var(--text-muted);">
                        <span>${escapeHTML(c.name || '')}</span>
                        <span>${escapeHTML(displayPrice(c.last_price, c.symbol))}</span>
                    </div>
                </div>
            `;
        }

        function toggleUniverseSection() {
            // 顶部搜索框模式: 展开由聚焦驱动, 此函数保留为空以兼容旧调用
        }

        function showUniverseDropdown(show) {
            const box = document.getElementById('universe-results');
            if (box) box.style.display = show ? 'block' : 'none';
        }

        // 刷新搜索下拉列表 (保持当前视图: 品种浏览或关键词搜索)
        function refreshUniverseList() {
            if (universeCategory) browseUniverseProduct(universeCategory);
            else searchUniverse(lastUniverseKw);
        }
        // 第一步: 点击搜索结果中的合约 → 弹出分组选择弹窗
        async function pickUniverse(symbol) {
            showUniverseDropdown(false);
            openPickGroupModal(symbol);
        }
        // 搜索添加合约弹窗: 第一步选合约, 第二步弹窗选分组 (多选) 后添加
        function openPickGroupModal(symbol) {
            const inst = watchlist.find(w => w.symbol === symbol);
            const modal = document.getElementById('pick-group-modal');
            if (!modal) { console.error('pick-group-modal 缺失, 无法打开添加到监控弹窗'); return; }
            pickSelectedSymbol = symbol;
            modal.style.display = 'flex';
            const symEl = document.getElementById('pick-symbol');
            const nameEl = document.getElementById('pick-name');
            if (symEl) symEl.textContent = symbol;
            if (nameEl) nameEl.textContent = inst && inst.name ? inst.name : '';
            pickTargetGroups = groupListOf(inst || {}).slice();   // 已订阅合约默认勾选当前所属分组
            renderPickChips();
        }
        function renderPickChips() {
            const wrap = document.getElementById('pick-chips');
            if (!wrap) return;
            wrap.innerHTML = (groupNames && groupNames.length ? groupNames : ['默认']).map(g => {
                const on = pickTargetGroups.includes(g);
                return `<span class="pick-chip ${on ? 'active' : ''}" data-group="${escapeHTML(g)}">${on ? '✓ ' : ''}${escapeHTML(g)}</span>`;
            }).join('');
            const hint = document.getElementById('pick-hint');
            if (hint) {
                hint.textContent = pickTargetGroups.length
                    ? '将添加到: ' + pickTargetGroups.join('、')
                    : '未选择分组, 将归入「默认」';
            }
        }
        // 分组芯片点击: 切换勾选 (事件委托, 芯片由 renderPickChips 动态生成)
        function togglePickGroup(g) {
            const i = pickTargetGroups.indexOf(g);
            if (i >= 0) pickTargetGroups.splice(i, 1);
            else pickTargetGroups.push(g);
            renderPickChips();
        }
        document.addEventListener('click', (e) => {
            const chip = e.target && e.target.closest ? e.target.closest('#pick-chips .pick-chip') : null;
            if (!chip) return;
            e.stopPropagation();
            togglePickGroup(chip.dataset.group);
        });
        // 安全取值: 弹窗被移除/未渲染时不抛异常
        function closePickGroupModal() {
            const modal = document.getElementById('pick-group-modal');
            if (modal) modal.style.display = 'none';
            pickSelectedSymbol = null;
        }
        async function confirmPickAdd() {
            const symEl = document.getElementById('pick-symbol');
            const symbol = symEl ? symEl.textContent : '';
            if (!symbol) { closePickGroupModal(); return; }
            const groups = pickTargetGroups.length ? pickTargetGroups.slice() : ['默认'];
            closePickGroupModal();
            showUniverseDropdown(false);
            const u = document.getElementById('universe-search');
            u.value = '';
            lastUniverseKw = '';
            await subscribeSymbol(symbol);
            try {
                await fetch('/api/v1/watchlist/assign-groups', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ symbol, groups })
                });
            } catch (e) { console.error('assign-groups error', e); }
            await loadWatchlist();
            const meta = watchlist.find(w => w.symbol === symbol);
            if (meta) activeGroupTab = groupListOf(meta)[0] || '默认';
            selectSymbol(symbol, meta ? meta.name : '');
            setTimeout(() => { loadWatchlist(); renderContracts(); }, 2500);
            setTimeout(() => { loadWatchlist(); renderContracts(); }, 5000);
        }

        async function loadUniverseCategories() {
            const box = document.getElementById('universe-results');
            try {
                const res = await fetch('/api/v1/universe/categories');
                if (!res.ok) { box.innerHTML = '<div style="color:var(--text-muted); font-size:11px; padding:4px;">类别加载失败</div>'; return; }
                const data = await res.json();
                const exName = { SHFE: '上期所', DCE: '大商所', CZCE: '郑商所', INE: '能源中心', CFFEX: '中金所', GFEX: '广期所' };
                const inp = document.getElementById('universe-search');
                if (inp && !inp.value.trim()) inp.placeholder = `搜索全市场合约 (共 ${data.total} 个品种)`;
                box.innerHTML = data.categories.map(c => `
                    <div class="contract-item" style="padding:5px 8px; margin-bottom:4px; display:flex; justify-content:space-between; align-items:center;" onclick="browseUniverseProduct(decodeURIComponent('${safeInlineValue(c.product)}'))">
                        <span style="font-size:12px; font-weight:600;">${escapeHTML(c.name || c.product)}</span>
                        <span style="font-size:10px; color:var(--text-muted);">${escapeHTML(c.product)} · ${escapeHTML(exName[c.exchange] || c.exchange)} · ${Number(c.count) || 0}个</span>
                    </div>
                `).join('') || '<div style="color:var(--text-muted); font-size:11px; padding:4px;">暂无品种数据, 等待全市场同步</div>';
            } catch (e) {
                console.error('loadUniverseCategories error', e);
                box.innerHTML = '<div style="color:var(--text-muted); font-size:11px; padding:4px;">类别加载失败</div>';
            }
        }

        async function browseUniverseProduct(product) {
            universeCategory = product;
            const box = document.getElementById('universe-results');
            box.innerHTML = '<div style="color:var(--text-muted); font-size:11px; padding:4px;">加载中...</div>';
            try {
                const res = await fetch(`/api/v1/universe?product=${encodeURIComponent(product)}&limit=60`);
                if (!res.ok) { box.innerHTML = '<div style="color:var(--text-muted); font-size:11px; padding:4px;">加载失败</div>'; return; }
                const data = await res.json();
                const meta = data.contracts.find(c => c.symbol !== product) || {};
                const title = meta.name ? `${meta.name} (${product})` : product;
                box.innerHTML = `
                    <div onclick="universeCategory = null; showUniverseCategories();"
                         style="cursor:pointer; font-size:11px; color:#3b82f6; padding:2px 4px 6px;">← 返回类别列表</div>
                    <div style="font-size:11px; color:var(--text-muted); padding:0 4px 6px;">${escapeHTML(title)} 共 ${Number(data.total) || 0} 个活跃合约, 点击订阅</div>
                ` + data.contracts.map(c => renderUniverseContract(c)).join('');
            } catch (e) {
                console.error('browseUniverseProduct error', e);
                box.innerHTML = '<div style="color:var(--text-muted); font-size:11px; padding:4px;">加载失败</div>';
            }
        }

        function showUniverseCategories() {
            loadUniverseCategories();
        }

        async function searchUniverse(kw) {
            const box = document.getElementById('universe-results');
            if (!kw || !kw.trim()) {
                // 无输入: 展示品种类别供选择
                universeCategory = null;
                showUniverseCategories();
                return;
            }
            universeCategory = null;
            try {
                const res = await fetch(`/api/v1/universe?search=${encodeURIComponent(kw.trim())}&limit=25`);
                if (!res.ok) return;
                const data = await res.json();
                if (!data.contracts.length) {
                    box.innerHTML = '<div style="color:var(--text-muted); font-size:11px; padding:4px;">无匹配合约</div>';
                    return;
                }
                box.innerHTML = data.contracts.map(renderUniverseContract).join('');
            } catch (e) { console.error('searchUniverse error', e); }
        }

        function updateMainSwitchBtn() {
            const btn = document.getElementById('btn-switch-main');
            const tag = document.getElementById('main-tag');
            const dominantTag = document.getElementById('dominant-tag');
            if (!btn || !tag || !dominantTag) return;
            const product = currentSymbol.replace(/[0-9]+$/, '');
            const isSyntheticMain = !!product && currentSymbol === product + '0' && currentSymbol !== product;
            const meta = watchlist.find(w => w.symbol === currentSymbol);
            const dom = meta ? meta.dominant_contract : null;
            const isDominant = dom === currentSymbol;

            if (isSyntheticMain) {
                // 浏览合成主连 (JM0 等): 显示主连标签与当前主力
                btn.style.display = 'none';
                tag.style.display = 'inline-block';
                if (dom) {
                    dominantTag.style.display = 'inline-block';
                    dominantTag.innerText = '当前主力: ' + dom;
                } else {
                    dominantTag.style.display = 'none';
                }
            } else if (isDominant) {
                // 已是当前主力合约
                btn.style.display = 'none';
                tag.style.display = 'inline-block';
                tag.innerText = '主力合约';
                tag.style.background = '#14532d';
                tag.style.color = '#86efac';
                dominantTag.style.display = 'none';
            } else {
                // 非主力: 显示切主连按钮 (原地替换为当前主力月份)
                tag.style.display = 'none';
                dominantTag.style.display = 'none';
                btn.style.display = dom ? 'inline-block' : 'none';
                btn.innerText = '切主连';
            }
        }

