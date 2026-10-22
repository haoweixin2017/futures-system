        window.onload = async () => {
            const authed = await initUser();
            if (!authed) return;

            const verificationButton = document.getElementById('ai-verification-btn');
            if (verificationButton) verificationButton.style.display = !isLiveMode && currentUser?.role === 'sim' ? '' : 'none';

            if (isLiveMode) {
                loadLiveStatus();
                setInterval(loadLiveStatus, 5000);
            } else {
                loadAutoConfig();
            }
            loadTradeData();
            startTradeAutoRefresh();   // 订单/持仓独立定时刷新, 不依赖 WS 行情
            startAIHomeAutoRefresh();  // 首页 AI 结果独立刷新，包含后台模拟验证分析
            loadWatchlist().then(() => {
                renderContracts();
                initChart();
                // 刷新后同步标题/指标/信号与当前选中合约 (否则会残留初始 JM2701 文案)
                if (watchlist.length) {
                    selectSymbol(currentSymbol);
                } else {
                    updateMainSwitchBtn();
                }
            });
            loadEvents();
            loadFeedStatus();
            loadStorageHealth();
            connectWebSocket();
            setInterval(loadFeedStatus, 5000);
            setInterval(refreshLiveChart, 2000);
            setInterval(loadStorageHealth, 15000);
            setInterval(loadAnalysis, 3000);
            setInterval(() => { if (leftTab === 'tape') loadTape(); }, 2000);
            setInterval(() => { if (leftTab === 'big') loadBigOrders(); }, 5000);
            document.getElementById('universe-search').addEventListener('input', onSearchInput);
            // 顶部搜索框: 聚焦展开下拉 (无输入显示品种类别), 失焦/点击外部收起
            const uSearch = document.getElementById('universe-search');
            const openUniverse = () => { showUniverseDropdown(true); if (!uSearch.value.trim()) showUniverseCategories(); };
            uSearch.addEventListener('focus', openUniverse);
            uSearch.addEventListener('mousedown', openUniverse);   // 已聚焦时 focus 不再触发, mousedown 兜底
            uSearch.addEventListener('blur', () => setTimeout(() => {
                // 焦点移入搜索组件内部 (如"添加到"分组选择器) 时不收起
                const top = document.getElementById('universe-top');
                const ae = document.activeElement;
                if (!top || !ae || !top.contains(ae)) showUniverseDropdown(false);
            }, 200));
            // 新建分组弹窗: 回车提交, Esc 取消
            document.getElementById('group-name-input').addEventListener('keydown', (e) => {
                if (e.key === 'Enter') submitGroupModal();
                if (e.key === 'Escape') closeGroupModal();
            });
            // 重命名弹窗: 回车提交, Esc 取消
            document.getElementById('rename-input').addEventListener('keydown', (e) => {
                if (e.key === 'Enter') submitRenameModal();
                if (e.key === 'Escape') closeRenameModal();
            });
            // 添加到监控弹窗: 点击遮罩或 Esc 关闭
            const pickModalEl = document.getElementById('pick-group-modal');
            if (pickModalEl) {
                pickModalEl.addEventListener('mousedown', (e) => {
                    if (e.target === pickModalEl) closePickGroupModal();
                });
            }
            // 修改分组弹窗: 点击遮罩或 Esc 关闭
            const groupEditEl = document.getElementById('group-edit-modal');
            if (groupEditEl) {
                groupEditEl.addEventListener('mousedown', (e) => {
                    if (e.target === groupEditEl) closeGroupEdit();
                });
            }
            // 合约信息弹窗: 点击遮罩或 Esc 关闭
            const infoModalEl = document.getElementById('info-modal');
            if (infoModalEl) {
                infoModalEl.addEventListener('mousedown', (e) => {
                    if (e.target === infoModalEl) closeInfoModal();
                });
            }
            for (const id of ['ai-model-modal', 'ai-analysis-modal', 'ai-session-modal', 'ai-future-modal', 'ai-history-modal', 'ai-review-modal', 'ai-verification-modal']) {
                const modal = document.getElementById(id);
                if (modal) modal.addEventListener('mousedown', (e) => {
                    if (e.target === modal) {
                        if (id === 'ai-model-modal') closeAIModelModal();
                        else if (id === 'ai-analysis-modal' || id === 'ai-session-modal' || id === 'ai-future-modal') closeAIAnalysisModal();
                        else if (id === 'ai-history-modal') closeAIHistoryModal();
                        else if (id === 'ai-verification-modal') closeAIVerificationModal();
                        else closeAIReviewModal();
                    }
                });
            }
            document.addEventListener('keydown', (e) => {
                if (e.key !== 'Escape') return;
                const m = document.getElementById('info-modal');
                if (m && m.style.display === 'flex') closeInfoModal();
                const ge = document.getElementById('group-edit-modal');
                if (ge && ge.style.display === 'flex') closeGroupEdit();
                const pk = document.getElementById('pick-group-modal');
                if (pk && pk.style.display === 'flex') closePickGroupModal();
                const am = document.getElementById('ai-model-modal');
                if (am && am.style.display === 'flex') closeAIModelModal();
                const aa = document.getElementById('ai-analysis-modal');
                if (aa && aa.style.display === 'flex') closeAIAnalysisModal();
                const as = document.getElementById('ai-session-modal');
                if (as && as.style.display === 'flex') closeAIAnalysisModal();
                const af = document.getElementById('ai-future-modal');
                if (af && af.style.display === 'flex') closeAIAnalysisModal();
                const ah = document.getElementById('ai-history-modal');
                if (ah && ah.style.display === 'flex') closeAIHistoryModal();
                const ar = document.getElementById('ai-review-modal');
                if (ar && ar.style.display === 'flex') closeAIReviewModal();
                const av = document.getElementById('ai-verification-modal');
                if (av && av.style.display === 'flex') closeAIVerificationModal();
            });
            // 点击下拉框内的类别/合约时不让输入框失焦 (否则 blur 会先收起下拉, click 落空)
            document.getElementById('universe-results').addEventListener('mousedown', (e) => {
                e.preventDefault();
            });
            document.addEventListener('click', (e) => {
                const top = document.getElementById('universe-top');
                // 用 composedPath 判断: 类别点击会同步替换下拉内容导致目标节点游离, contains 会误判
                const inside = top && e.composedPath().some(n => n === top);
                if (!inside) showUniverseDropdown(false);
            });
        };
