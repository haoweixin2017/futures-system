        function currentAIAnalysisTarget() {
            return aiFutureMode ? 'FUTURE_TREND' : aiSessionMode ? 'CURRENT_SESSION'
                : document.querySelector('input[name="ai-analysis-target"]:checked')?.value || 'TODAY';
        }

        function aiRequirementStorageKey(target) {
            return `ai-analysis-requirements-${String(currentSymbol || '').toUpperCase()}-${target}`;
        }

        function aiReviewStorageKey(target) {
            return `ai-use-review-summaries-${String(currentSymbol || '').toUpperCase()}-${target}`;
        }

        let aiInstructionPreviewTimer = null;
        let aiReviewOptionRequest = 0;

        function saveAIAnalysisRequirements() {
            const target = currentAIAnalysisTarget();
            localStorage.setItem(aiRequirementStorageKey(target), document.getElementById('ai-analysis-requirements').value);
            clearTimeout(aiInstructionPreviewTimer);
            aiInstructionPreviewTimer = setTimeout(refreshAIInstructionPreview, 250);
        }

        function saveAIReviewPreference() {
            const target = currentAIAnalysisTarget();
            const checkbox = document.getElementById('ai-use-review-summaries');
            localStorage.setItem(aiReviewStorageKey(target), checkbox.checked ? 'true' : 'false');
            refreshAIInstructionPreview();
        }

        async function refreshAIReviewOption() {
            const requestNo = ++aiReviewOptionRequest;
            const requestSymbol = currentSymbol;
            const target = currentAIAnalysisTarget();
            const sessionStart = target === 'CURRENT_SESSION'
                ? document.getElementById('ai-session-select').value : '';
            const checkbox = document.getElementById('ai-use-review-summaries');
            const text = document.getElementById('ai-review-match-text');
            checkbox.disabled = true;
            text.textContent = '正在匹配可用总结...';
            try {
                const query = new URLSearchParams({analysis_target:target});
                if (sessionStart) query.set('session_start_time', sessionStart);
                const res = await fetch(`/api/v1/ai/analysis/review-context/${encodeURIComponent(currentSymbol)}?${query}`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                if (requestNo !== aiReviewOptionRequest || requestSymbol !== currentSymbol
                        || target !== currentAIAnalysisTarget()) return;
                const saved = localStorage.getItem(aiReviewStorageKey(target));
                checkbox.disabled = !data.used_count;
                checkbox.checked = Boolean(data.used_count) && (saved === null || saved === 'true');
                text.textContent = data.used_count
                    ? `已匹配${data.matched_count}条，最多发送${data.used_count}条；完整内容可在下方指令预览中查看`
                    : data.matched_count ? '匹配到总结，但内容超过本次总结Token限制' : '暂无与当前合约、分析类型和时段匹配的已启用总结';
            } catch (e) {
                checkbox.checked = false;
                text.textContent = '总结匹配失败：' + e.message;
            }
            refreshAIInstructionPreview();
        }

        async function refreshAIInstructionPreview() {
            const details = document.getElementById('ai-instruction-preview');
            if (!details.open) return;
            const box = document.getElementById('ai-instruction-preview-content');
            const analysisTarget = currentAIAnalysisTarget();
            const analysisRequirements = document.getElementById('ai-analysis-requirements').value.trim();
            const sessionStartTime = analysisTarget === 'CURRENT_SESSION'
                ? document.getElementById('ai-session-select').value || null : null;
            box.textContent = '正在读取完整指令...';
            try {
                const res = await fetch('/api/v1/ai/analysis/prompt-preview', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({analysis_target:analysisTarget, analysis_requirements:analysisRequirements,
                        instrument:currentSymbol, session_start_time:sessionStartTime,
                        use_review_summaries:document.getElementById('ai-use-review-summaries').checked})
                });
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                box.textContent = [
                    '【系统强制指令】\n' + data.system_instruction,
                    '【本合约可调要求】\n' + (data.analysis_requirements || '未填写'),
                    '【上下文过长时的分块指令】\n' + data.segment_instruction,
                    '【模型返回身份不一致时的纠错指令】\n' + data.correction_instruction,
                    '【行情数据】\n' + data.payload_note,
                    '【历史复盘总结】\n' + (data.review_context.enabled
                        ? `本次启用；匹配${data.review_context.matched_count}条，实际发送${data.review_context.used_count}条\n${JSON.stringify(data.review_context.summaries, null, 2)}`
                        : `本次不启用；当前匹配${data.review_context.matched_count}条`),
                ].join('\n\n');
                if (data.session_target) {
                    const session = data.session_target;
                    box.textContent += `\n\n【目标连续交易小节】\n${session.label} · ${session.start_time.replace('T',' ')} 至 ${session.end_time.replace('T',' ')} · ${session.status === 'ACTIVE' ? '交易中' : '即将开始'}`;
                }
            } catch (e) {
                box.textContent = '读取失败：' + e.message;
            }
        }

        async function openAIAnalysisModal() {
            if (isLiveMode || !currentSymbol || activeAIBatch()) return;
            aiSessionMode = false;
            aiFutureMode = false;
            const symbol = currentSymbol;
            const todayOption = document.querySelector('input[name="ai-analysis-target"][value="TODAY"]');
            let reason = '';
            try {
                const response = await fetch(`/api/v1/ai/analysis/availability/${encodeURIComponent(symbol)}`);
                if (!response.ok) throw new Error(await aiErrorMessage(response));
                const availability = await response.json();
                todayOption.disabled = !availability.today_available;
                reason = availability.today_reason || '';
            } catch (e) {
                todayOption.disabled = true;
                reason = '暂时无法确认今日交易时段：' + e.message;
            }
            if (symbol !== currentSymbol) return;
            todayOption.closest('label').style.opacity = todayOption.disabled ? '0.5' : '';
            const modal = document.getElementById('ai-analysis-modal');
            modal.appendChild(document.getElementById('ai-analysis-card'));
            document.getElementById('ai-modal-title').textContent = 'AI 分析';
            document.getElementById('ai-analysis-targets').style.display = 'grid';
            document.querySelector(`input[name="ai-analysis-target"][value="${todayOption.disabled ? 'NEXT_OPEN' : 'TODAY'}"]`).checked = true;
            await showAIAnalysisModal(modal);
            if (reason) document.getElementById('ai-analysis-message').textContent = reason;
        }

        async function openAISessionModal() {
            if (isLiveMode || !currentSymbol || activeAIBatch()) return;
            aiSessionMode = true;
            aiFutureMode = false;
            const modal = document.getElementById('ai-session-modal');
            modal.appendChild(document.getElementById('ai-analysis-card'));
            document.getElementById('ai-modal-title').textContent = '盘时段画线';
            document.getElementById('ai-analysis-targets').style.display = 'none';
            await showAIAnalysisModal(modal);
        }

        async function openFutureTrendModal() {
            if (isLiveMode || !currentSymbol || activeAIBatch()) return;
            aiSessionMode = false;
            aiFutureMode = true;
            const modal = document.getElementById('ai-future-modal');
            modal.appendChild(document.getElementById('ai-analysis-card'));
            document.getElementById('ai-modal-title').textContent = '未来趋势预测';
            document.getElementById('ai-analysis-targets').style.display = 'none';
            await showAIAnalysisModal(modal);
        }

        async function showAIAnalysisModal(modal) {
            const box = document.getElementById('ai-analysis-models');
            syncAIAnalysisTarget();
            const cacheStatus = document.getElementById('ai-future-cache-status');
            if (cacheStatus) cacheStatus.style.display = aiFutureMode ? 'block' : 'none';
            if (aiFutureMode && typeof loadAIResearchStatus === 'function') {
                loadAIResearchStatus(currentSymbol).catch(e => { if (cacheStatus) cacheStatus.textContent = '资料状态读取失败：' + e.message + '。预测不会现场搜索或等待采集。'; });
            }
            document.getElementById('ai-analysis-message').textContent = '';
            box.textContent = '正在读取模型...';
            modal.style.display = 'flex';
            try {
                const models = await fetchAIModels();
                await fetchAIEvaluationStats().catch(() => { aiEvaluationStats = {}; });
                box.replaceChildren();
                if (!models.length) {
                    box.textContent = '尚未配置模型。请先关闭此窗口并进入“模型设置”。';
                    return;
                }
                const enabledModels = models.filter(model => model.enabled);
                if (!enabledModels.length) {
                    box.textContent = '暂无启用的模型，请到“模型设置”中启用。';
                    return;
                }
                enabledModels.forEach(model => {
                    const label = document.createElement('label');
                    label.className = 'ai-model-row';
                    label.style.cssText += 'display:flex; gap:8px; align-items:flex-start; cursor:pointer;';
                    const check = document.createElement('input');
                    check.type = 'checkbox'; check.value = model.model_config_id;
                    check.className = 'ai-model-select'; check.disabled = !model.available;
                    check.checked = enabledModels.length === 1 && model.available;
                    const text = document.createElement('span'); text.style.minWidth = '0';
                    const title = document.createElement('span');
                    title.textContent = `${model.display_name} · ${model.model_name}${model.available ? '' : ` · ${model.unavailable_reason}`}`;
                    const accuracy = document.createElement('span');
                    accuracy.className = 'ai-model-accuracy'; accuracy.dataset.modelId = model.model_config_id;
                    accuracy.style.cssText = 'display:block; margin-top:3px; font-size:10px; color:#93c5fd;';
                    text.append(title, accuracy); label.append(check, text);
                    box.appendChild(label);
                });
                refreshAIModelAccuracyLabels();
            } catch (e) { box.textContent = e.message; }
        }

        function closeAIAnalysisModal() {
            document.getElementById('ai-analysis-modal').style.display = 'none';
            document.getElementById('ai-session-modal').style.display = 'none';
            document.getElementById('ai-future-modal').style.display = 'none';
            document.getElementById('ai-analysis-modal').appendChild(document.getElementById('ai-analysis-card'));
            aiSessionMode = false;
            aiFutureMode = false;
        }

        async function loadAISessionOptions() {
            const select = document.getElementById('ai-session-select');
            select.innerHTML = '<option value="">正在读取合约交易时段...</option>';
            try {
                const res = await fetch(`/api/v1/ai/analysis/session-options/${encodeURIComponent(currentSymbol)}`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const sessions = (await res.json()).sessions || [];
                select.replaceChildren();
                sessions.forEach(session => {
                    const option = document.createElement('option');
                    option.value = session.start_time;
                    option.dataset.status = session.status;
                    option.disabled = session.status === 'PASSED';
                    const state = session.status === 'PASSED' ? '已结束，不可分析'
                        : session.status === 'ACTIVE' ? '交易中' : '未开盘 · 盘前展望';
                    option.textContent = `${session.label} · ${session.start_time.replace('T',' ')}–${session.end_time.slice(11,16)} · ${state}`;
                    select.appendChild(option);
                });
                const firstAvailable = Array.from(select.options).find(option => !option.disabled);
                if (firstAvailable) select.value = firstAvailable.value;
                syncAISessionSelection();
            } catch (e) {
                select.innerHTML = `<option value="">读取失败：${e.message}</option>`;
            }
        }

        function currentAISessionRequirementDefault() {
            const select = document.getElementById('ai-session-select');
            const option = select?.options?.[select.selectedIndex];
            return option?.dataset.status === 'UPCOMING'
                ? aiSessionRequirementDefaults.UPCOMING : aiSessionRequirementDefaults.ACTIVE;
        }

        function isAISessionDefaultRequirement(value) {
            const text = String(value ?? '').trim();
            return [...Object.values(aiSessionRequirementDefaults), ...aiLegacySessionRequirements].includes(text);
        }

        function syncAISessionDefaultRequirement() {
            if (currentAIAnalysisTarget() !== 'CURRENT_SESSION') return;
            const input = document.getElementById('ai-analysis-requirements');
            if (!isAISessionDefaultRequirement(input.value)) return;
            const next = currentAISessionRequirementDefault();
            if (input.value === next) return;
            input.value = next;
            localStorage.setItem(aiRequirementStorageKey('CURRENT_SESSION'), next);
        }

        function syncAISessionSelection() {
            syncAISessionDefaultRequirement();
            const select = document.getElementById('ai-session-select');
            const option = select.options[select.selectedIndex];
            if (option?.value) {
                const tail = option.dataset.status === 'UPCOMING'
                    ? '未开盘 · 以开盘前已闭合数据做盘前展望'
                    : '只发送所选时段数据';
                document.getElementById('ai-analysis-summary').textContent = `合约：${currentSymbol} · ${option.textContent} · ${tail}`;
            }
            refreshAIReviewOption();
            refreshAIInstructionPreview();
        }

        function syncAIAnalysisTarget() {
            const analysisTarget = currentAIAnalysisTarget();
            document.getElementById('ai-analysis-data-notice').textContent = analysisTarget === 'FUTURE_TREND'
                ? '远程模型会接收已闭合多周期行情和公开资料，可能产生调用费用；预测不会自动下单。'
                : '远程模型会接收当前合约当日全部 1 分钟数据，可能产生调用费用；AI 结果不会自动下单。';
            const label = analysisTarget === 'FUTURE_TREND'
                ? '分别预测未来第 3 / 5 个实际交易日收盘相对冻结参考价的方向'
                : analysisTarget === 'NEXT_OPEN'
                ? '只预测下一交易日开盘（有夜盘按 21:00，无夜盘按日盘开盘）'
                : analysisTarget === 'CURRENT_SESSION'
                    ? '请选择一个尚未结束的连续交易小节'
                    : '只分析当前交易日盘中走势，不包含明日预测';
            const dataLabel = analysisTarget === 'FUTURE_TREND'
                ? '使用闭合日线、60分钟、15分钟数据 · 读取本批冻结的有效公开资料'
                : '使用今日全部可用 1 分钟数据';
            document.getElementById('ai-analysis-summary').textContent = `合约：${currentSymbol} · ${label} · ${dataLabel}`;
            document.getElementById('ai-session-picker').style.display = analysisTarget === 'CURRENT_SESSION' ? 'block' : 'none';
            let saved = localStorage.getItem(aiRequirementStorageKey(analysisTarget));
            if (analysisTarget === 'CURRENT_SESSION' && saved !== null && isAISessionDefaultRequirement(saved)) {
                saved = currentAISessionRequirementDefault();
                localStorage.setItem(aiRequirementStorageKey(analysisTarget), saved);
            }
            if (analysisTarget === 'FUTURE_TREND' && saved !== null) {
                saved = saved.replace('第3、第5、第7、第15个实际交易日', '第3、第5个实际交易日');
            }
            document.getElementById('ai-analysis-requirements').value = saved === null
                ? (analysisTarget === 'CURRENT_SESSION' ? currentAISessionRequirementDefault()
                    : aiAnalysisRequirementDefaults[analysisTarget]) : saved;
            if (analysisTarget === 'CURRENT_SESSION') loadAISessionOptions();
            else refreshAIReviewOption();
            refreshAIModelAccuracyLabels();
            refreshAIInstructionPreview();
        }

        // 历史详情面板: 引用常驻节点 (会随"查看"移动到所点行的下方, 刷新列表时暂时脱离文档)
        const aiHistoryDetailEl = document.getElementById('ai-history-detail');
        const aiHistoryTargets = [
            { key: 'TODAY', label: '今日走势' },
            { key: 'CURRENT_SESSION', label: '盘时段' },
            { key: 'NEXT_OPEN', label: '明日开盘' },
            { key: 'FUTURE_TREND', label: '未来趋势' },
        ];
        let aiHistoryTarget = 'TODAY';
        let aiHistoryItems = null;

        function closeAIHistoryDetail() {
            aiHistoryDetailEl.style.display = 'none';
            document.querySelectorAll('#ai-history-list .ai-history-active')
                .forEach(r => r.classList.remove('ai-history-active'));
        }

        async function openAIHistoryModal() {
            if (isLiveMode) return;
            const modal = document.getElementById('ai-history-modal');
            modal.style.display = 'flex';
            document.getElementById('ai-history-today-only').checked = false;
            aiHistoryDetailEl.style.display = 'none';
            document.getElementById('ai-history-symbol').textContent = currentSymbol || '--';
            aiHistoryTarget = currentAIAnalysisTarget();
            await loadAIHistory();
        }

        function fmtTradingDay(value) {
            const s = String(value || '');
            return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}年${s.slice(4, 6)}月${s.slice(6, 8)}日` : s;
        }

        async function loadAIHistory() {
            const box = document.getElementById('ai-history-list');
            const todayOnly = document.getElementById('ai-history-today-only').checked;
            aiHistoryItems = null;
            renderAIHistory();
            if (!currentSymbol) { box.textContent = '请先选择合约，再查看该合约的 AI 分析历史。'; return; }
            try {
                await fetchAIEvaluationStats().catch(() => {});
                const res = await fetch(`/api/v1/ai/analysis/history?instrument=${encodeURIComponent(currentSymbol)}&page_size=100&today_only=${todayOnly}`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                document.getElementById('ai-history-today-label').textContent =
                    `仅显示当前交易日（${fmtTradingDay(data.current_trading_day)}）`;
                aiHistoryItems = data.items || [];
                renderAIHistory();
            } catch (e) { box.textContent = e.message; }
        }

        function aiHistoryStatusText(status) {
            return {CREATED:'等待执行', PENDING:'等待执行', RUNNING:'分析中', COMPLETED:'已完成',
                PARTIAL_FAILED:'部分失败', SUCCEEDED:'已完成', PARTIAL:'部分完成',
                FAILED:'执行失败', TIMEOUT:'执行超时', HTTP_ERROR:'请求失败', UNSTRUCTURED:'无结构化结果'}[status] || status || '状态未记录';
        }

        function appendAIFutureEvidence(container, result, batch) {
            const appendLine = (parent, content, className = '') => {
                const line = document.createElement('div'); line.className = className;
                if (Array.isArray(content)) content.forEach(([value, color = '']) => {
                    const span = document.createElement('span'); span.className = color;
                    span.textContent = value; line.appendChild(span);
                });
                else line.textContent = content;
                parent.appendChild(line);
            };
            const evidence = document.createElement('div'); evidence.className = 'ai-history-evidence';
            const frozen = batch.public_materials || {};
            const scheduled = frozen.cache_state != null || frozen.config_version != null;
            const availableSources = (frozen.sources || []).filter(source => !source.stale);
            const related = batch.related_contracts || frozen.related_contracts || [];
            const references = source => (source.reference_contracts || related.filter(code =>
                (source.applicable_contracts || []).includes(code) || source.product_code === code.replace(/\d+$/, ''))).filter(code => related.includes(code));
            const quotedSources = Object.entries(result?.dimensions || {}).flatMap(([key, dimension]) =>
                (dimension.citations || []).map(citation => availableSources.find(source => source.source_id === citation.source_id && source.dimension === key)).filter(Boolean));
            const usedRelated = Array.isArray(result?.used_related_contracts) ? result.used_related_contracts.filter(code => related.includes(code))
                : [...new Set(quotedSources.flatMap(references))];
            const providedRelated = new Set(availableSources.flatMap(references));
            const snapshots = Array.isArray(batch.related_market_snapshots) ? batch.related_market_snapshots : Object.values(batch.related_market_snapshots || {});
            snapshots.forEach(snapshot => {
                const code = typeof snapshot.instrument === 'string' ? snapshot.instrument : snapshot.instrument?.symbol;
                if (snapshot.quality?.status === 'AVAILABLE' || snapshot.quality?.status === 'PARTIAL') providedRelated.add(code);
            });
            if (scheduled) {
                const labels = {technical:'技术', volume_oi:'量价持仓', fundamentals:'基本面', macro:'宏观', events:'事件', related_markets:'相关市场'};
                appendLine(evidence, '本批已提供资料：' + ([...new Set(availableSources.map(source => labels[source.dimension] || source.dimension))].join('、') || '无有效外部资料'));
                related.forEach(code => {
                    const snapshot = snapshots.find(item => (typeof item.instrument === 'string' ? item.instrument : item.instrument?.symbol) === code);
                    appendLine(evidence, `${code} · ${usedRelated.includes(code) ? '已用（校验引用）' : providedRelated.has(code) ? '已提供但未引用' : '关联已配置，资料缺失'}${!snapshot || ['MISSING','UNAVAILABLE'].includes(snapshot.quality?.status) ? ' · 行情缺失' : snapshot.quality?.status === 'PARTIAL' ? ' · 行情部分缺失' : ''}`, 'ai-history-meta');
                });
            }
            appendLine(evidence, '采用的信息（点击维度展开）', 'ai-history-price');
            if (!result?.dimensions || !Object.keys(result?.dimensions).length) {
                appendLine(evidence, '采用信息未记录', 'ai-history-muted');
            } else {
                const sources = [...(batch.public_materials?.sources || []), ...(batch.public_materials?.background_sources || [])];
                [['technical','技术'], ['volume_oi','量价持仓'], ['fundamentals','基本面'],
                 ['macro','宏观'], ['events','事件'], ['related_markets','相关市场']].forEach(([key, label]) => {
                    const dimension = result?.dimensions[key];
                    const verifiedUse = Array.isArray(result?.used_dimensions) ? result.used_dimensions.includes(key)
                        : ['technical','volume_oi'].includes(key) ? ['AVAILABLE','PARTIAL'].includes(dimension?.status)
                        : quotedSources.some(source => source.dimension === key);
                    const status = scheduled ? verifiedUse ? dimension?.status === 'AVAILABLE' ? 'AVAILABLE' : 'PARTIAL' : 'MISSING' : dimension?.status || 'MISSING';
                    const statusClass = {AVAILABLE:'ai-history-success', PARTIAL:'ai-history-neutral'}[status] || 'ai-history-muted';
                    const details = document.createElement('details'); details.className = 'ai-history-dimension';
                    const summary = document.createElement('summary');
                    const name = document.createElement('span'); name.className = statusClass;
                    name.style.color = ['AVAILABLE','PARTIAL'].includes(status)
                        ? {technical:'#60a5fa',volume_oi:'#c084fc',fundamentals:'#22d3ee',macro:'#fb923c',events:'#fbbf24',related_markets:'#818cf8'}[key]
                        : ['STALE','UNVERIFIED'].includes(status) ? '#f59e0b' : '#9ca3af';
                    name.textContent = `${label} · ${{AVAILABLE:'已用到', PARTIAL:'部分用到', MISSING:'未用到', STALE:'过期未用', UNVERIFIED:'待核验未用'}[status] || '未记录'}`;
                    if (scheduled && !['technical','volume_oi'].includes(key) && !verifiedUse) {
                        const provided = availableSources.some(source => source.dimension === key);
                        const old = (frozen.background_sources || []).some(source => source.dimension === key);
                        name.textContent += provided ? ' · 已提供但未引用' : old ? ' · 过期/待核验' : ' · 缺失';
                        if (old && !provided) name.style.color = '#f59e0b';
                    }
                    summary.appendChild(name); details.appendChild(summary);
                    appendLine(details, dimension?.summary || '资料缺失');
                    (dimension?.citations || []).forEach(citation => {
                        const source = sources.find(item => item.source_id === citation.source_id);
                        const row = document.createElement('div'); row.className = 'ai-history-source';
                        if (!source) appendLine(row, `[${citation.source_id || '--'}] 来源未记录`, 'ai-history-neutral');
                        else {
                            if (source.stale || (batch.public_materials?.background_sources || []).includes(source))
                                appendLine(row, '背景资料 · 不能作为当前方向依据', 'ai-history-neutral');
                            let safeUrl = null;
                            try {
                                const url = new URL(source.url);
                                if (url.protocol === 'https:' && !url.username && !url.password) safeUrl = url.href;
                            } catch (_) {}
                            const title = document.createElement(safeUrl ? 'a' : 'span');
                            title.textContent = `[${source.source_id}] ${source.title || '未命名资料'}`;
                            if (safeUrl) { title.href = safeUrl; title.target = '_blank'; title.rel = 'noopener noreferrer'; }
                            row.appendChild(title);
                            appendLine(row, `来源URL：${source.url || '未记录'}`, 'ai-history-meta');
                            appendLine(row, [[`来源：${source.source_name || '未知'} · `],
                                [`发布：${source.published_at && source.published_at !== 'UNKNOWN' ? source.published_at : '未知'}`, 'ai-history-date'],
                                [` · 资料期别：${source.data_period && source.data_period !== 'UNKNOWN' ? source.data_period : '未知'}`]], 'ai-history-meta');
                            if (source.material_id) {
                                const sourceRefs = references(source);
                                appendLine(row, `资料版本：${source.material_id} · 版本获取：${source.version_fetched_at || source.fetched_at || '未知'}${sourceRefs.length ? ' · 历史参考合约：' + sourceRefs.join('、') : ''}`, 'ai-history-meta');
                                const body = document.createElement('pre'); body.style.cssText = 'white-space:pre-wrap; max-height:320px; overflow:auto;';
                                const read = document.createElement('button'); read.className = 'ai-action'; read.textContent = '查看保存正文';
                                read.onclick = () => loadAIResearchMaterial(source.material_id, body); row.append(read, body);
                            }
                        }
                        appendLine(row, `引用摘录：${citation.quote || '未记录'}`);
                        details.appendChild(row);
                    });
                    (dimension?.market_evidence || []).forEach(item => appendLine(details,
                        `引用行情：${item.instrument || '未知关联'} · ${item.time || '时点未知'} · ${item.field || '字段未知'} = ${item.value ?? '--'} · ${item.reason || '作用未说明'}`, 'ai-history-source'));
                    evidence.appendChild(details);
                });
            }
            container.appendChild(evidence);
        }

        function aiAnalysisErrorText(error) {
            return error === 'Citation does not support this dimension'
                ? '引用维度不匹配：模型使用了其他信息维度的来源，本次未形成有效预测。'
                : error;
        }

        function appendAIHistorySummary(container, batch) {
            const target = batch.analysis_target || 'TODAY';
            const ratingText = rating => ({CORRECT:'正确', PARTIAL:'部分正确', WRONG:'错误',
                UNVERIFIABLE:'无法验证'}[rating] || rating || '尚无记录');
            const ratingClass = rating => ({CORRECT:'ai-history-success', WRONG:'ai-history-error',
                PARTIAL:'ai-history-neutral', UNVERIFIABLE:'ai-history-muted'}[rating] || 'ai-history-neutral');
            const directionClass = direction => ({LONG:'ai-history-up', UP:'ai-history-up',
                SHORT:'ai-history-down', DOWN:'ai-history-down', NEUTRAL:'ai-history-neutral',
                FLAT:'ai-history-neutral'}[direction] || 'ai-history-muted') + ' ai-history-direction';
            const dateTime = value => String(value || '').replace('T', ' ').slice(0, 16) || '未记录';
            const appendLine = (parent, content, className = '') => {
                const line = document.createElement('div'); line.className = className;
                if (Array.isArray(content)) content.forEach(([value, color = '']) => {
                    const span = document.createElement('span'); span.className = color;
                    span.textContent = value; line.appendChild(span);
                });
                else line.textContent = content;
                if (className === 'ai-history-summary') line.title = content;
                parent.appendChild(line);
                return line;
            };
            const frozenTargets = batch.future_targets || [];
            const appendFrozenTarget = (parent, item) => appendLine(parent, [
                [`第${item.horizon_days}个交易日 · ${fmtTradingDay(item.trading_day) || '目标日未记录'}收盘`, 'ai-history-date'],
                [' · 冻结参考价 '], [item.reference_price ?? '--', 'ai-history-price']], 'ai-history-meta');
            if (target === 'CURRENT_SESSION') {
                const session = batch.session_target;
                appendLine(container, session ? `交易时段：${session.label || '盘时段'} · ${dateTime(session.start_time)}–${dateTime(session.end_time)}` : '交易时段未记录', 'ai-history-date');
                if (batch.session_forecast) appendLine(container, `预测区间：${dateTime(batch.session_forecast.start_time)}–${dateTime(batch.session_forecast.end_time)}`, 'ai-history-date');
            } else if (target === 'NEXT_OPEN') {
                const opening = batch.next_open_target;
                appendLine(container, opening ? `目标开盘：${fmtTradingDay(opening.trading_day)} · ${opening.label || (opening.session === 'NIGHT' ? '夜盘' : '日盘')} · ${dateTime(opening.start_time)}` : '目标开盘日期未记录', 'ai-history-date');
            }
            if (!(batch.runs || []).length) {
                if (target === 'FUTURE_TREND') frozenTargets.forEach(item => appendFrozenTarget(container, item));
                appendLine(container, ['CREATED', 'PENDING', 'RUNNING'].includes(batch.status) ? '等待模型预测结果' : '暂无模型结果');
            }
            (batch.runs || []).forEach(run => {
                const model = document.createElement('div'); model.className = 'ai-history-model';
                appendLine(model, `${run.model_display_name || '模型'} · ${aiHistoryStatusText(run.status)}`, 'ai-history-model-name');
                const result = run.result;
                if (!result) {
                    if (target === 'FUTURE_TREND') frozenTargets.forEach(item => appendFrozenTarget(model, item));
                    appendLine(model, aiAnalysisErrorText(run.error) || (['PENDING', 'RUNNING'].includes(run.status) ? '等待模型预测结果' : '暂无结构化预测结果'));
                } else if (target === 'FUTURE_TREND') {
                    const horizons = frozenTargets.length ? frozenTargets : result.horizon_forecasts || [];
                    if (!horizons.length) appendLine(model, '测算日期与分期限预测未记录');
                    horizons.forEach(item => {
                        const forecast = result.horizon_forecasts?.find(f => f.horizon_days === item.horizon_days);
                        const metric = result.future_metrics?.[String(item.horizon_days)];
                        const horizon = document.createElement('div'); horizon.className = 'ai-history-horizon';
                        const frozen = frozenTargets.find(t => t.horizon_days === item.horizon_days);
                        appendLine(horizon, `第${item.horizon_days}个交易日 · ${fmtTradingDay(frozen?.trading_day) || '目标日未记录'}收盘`, 'ai-history-date');
                        appendLine(horizon, forecast ? [['预测：'], [directionText(forecast.direction), directionClass(forecast.direction)],
                            [` · 证据完整度 ${forecast.confidence_score ?? '--'}/100`]] : '暂无该期限预测', 'ai-history-prediction');
                        if (forecast?.summary) appendLine(horizon, forecast.summary, 'ai-history-summary');
                        const verification = [['冻结参考价 '], [frozen?.reference_price ?? metric?.reference_price ?? '--', 'ai-history-price'], [' · 自动验证：']];
                        if (metric) verification.push([ratingText(metric.rating), ratingClass(metric.rating)],
                            [' · 实际收盘 '], [metric.realized_price ?? '--', 'ai-history-actual'], [' · 价差 '],
                            [metric.delta == null ? '--' : (metric.delta >= 0 ? '+' : '') + metric.delta,
                                metric.delta == null ? 'ai-history-muted' : metric.delta > 0 ? 'ai-history-up' : metric.delta < 0 ? 'ai-history-down' : 'ai-history-neutral']);
                        else verification.push(['待到期 / 尚无验证记录', 'ai-history-neutral']);
                        appendLine(horizon, verification, 'ai-history-verification');
                        model.appendChild(horizon);
                    });
                    appendAIFutureEvidence(model, result, batch);
                } else {
                    const forecast = target === 'NEXT_OPEN' ? result.next_open_forecast : result;
                    const rawDirection = target === 'NEXT_OPEN' ? forecast?.direction : run.direction || forecast?.direction;
                    const direction = target === 'NEXT_OPEN' ? nextOpenDirectionText(rawDirection) : directionText(rawDirection);
                    appendLine(model, [['预测：'], [direction, directionClass(rawDirection)],
                        [` · 证据完整度 ${forecast?.confidence_score ?? run.confidence_score ?? '--'}/100`]], 'ai-history-prediction');
                    if (forecast?.summary) appendLine(model, forecast.summary, 'ai-history-summary');
                    if (target === 'CURRENT_SESSION' && result.session_chart) {
                        const plan = result.session_chart;
                        if (plan.window_policy === 'RESULT_AVAILABLE_10M') appendLine(model,
                            `完成后10分钟：${String(plan.forecast_start_time).slice(11,19)}–${String(plan.forecast_end_time).slice(11,19)}（先收盘则提前截止）`, 'ai-history-meta');
                        const prices = [['基准 '], [plan.reference_price ?? '--', 'ai-history-price'],
                            [' · 目标 '], [plan.target_price ?? '--', 'ai-history-actual'],
                            [' · 失效 '], [plan.invalidation_price ?? '--', 'ai-history-error']];
                        if (plan.trigger_price != null) prices.push([' · 触发 '], [plan.trigger_price, 'ai-history-neutral']);
                        appendLine(model, prices, 'ai-history-meta');
                    } else if (target === 'TODAY') {
                        appendLine(model, [['支撑 '], [(result.support_levels || []).join(' / ') || '--', 'ai-history-down'],
                            [' · 压力 '], [(result.resistance_levels || []).join(' / ') || '--', 'ai-history-up']], 'ai-history-meta');
                    }
                    if (target === 'CURRENT_SESSION') {
                        const rate = aiEvaluationStats[run.model_config_id]?.CURRENT_SESSION?.hit_rate;
                        const stateClass = {HIT:'ai-history-success', INVALIDATED:'ai-history-error', PENDING:'ai-history-neutral'}[run.session_line_state?.status] || 'ai-history-muted';
                        appendLine(model, [[aiSessionHistoryState(run), stateClass], [' · 目标命中率 '],
                            [rate ? `${rate.percentage}%（${rate.hit_count}/${rate.decided_count}）` : '暂无已判定预测', rate ? 'ai-history-actual' : 'ai-history-muted']], 'ai-history-verification');
                    }
                    appendLine(model, [['自动验证：'], [ratingText(result.settlement_metrics?.direction_rating),
                        ratingClass(result.settlement_metrics?.direction_rating)]], 'ai-history-verification');
                }
                if (run.evaluation) appendLine(model, [['记录评分：'], [ratingText(run.evaluation.rating), ratingClass(run.evaluation.rating)],
                    [run.evaluation.score == null ? ' · 不计入正确率' : ` · ${run.evaluation.score}分`]], 'ai-history-verification');
                container.appendChild(model);
            });
        }

        function renderAIHistory() {
            const box = document.getElementById('ai-history-list');
            const categories = document.getElementById('ai-history-categories');
            aiHistoryDetailEl.style.display = 'none';
            box.replaceChildren();
            categories.replaceChildren();
            const grouped = {};
            (aiHistoryItems || []).forEach(batch => {
                const key = aiHistoryTargets.some(group => group.key === batch.analysis_target)
                    ? batch.analysis_target : 'TODAY';
                (grouped[key] || (grouped[key] = [])).push(batch);
            });
            aiHistoryTargets.forEach(group => {
                const button = document.createElement('button');
                button.className = `ai-action${group.key === aiHistoryTarget ? ' ai-primary' : ''}`;
                button.textContent = `${group.label}${aiHistoryItems ? `（${(grouped[group.key] || []).length}）` : ''}`;
                button.setAttribute('aria-pressed', String(group.key === aiHistoryTarget));
                button.disabled = aiHistoryItems === null;
                button.onclick = () => { aiHistoryTarget = group.key; renderAIHistory(); };
                categories.appendChild(button);
            });
            if (aiHistoryItems === null) { box.textContent = '正在读取...'; return; }
            const group = aiHistoryTargets.find(group => group.key === aiHistoryTarget);
            const list = grouped[group.key] || [];
            if (!list.length) { box.textContent = `合约 ${currentSymbol} 暂无${group.label}分析记录。`; return; }
            list.forEach(batch => {
                const row = document.createElement('div'); row.className = 'ai-model-row ai-history-row';
                const text = document.createElement('div'); text.className = 'ai-history-row-text';
                const title = document.createElement('div'); title.className = 'ai-history-title';
                title.textContent = `${batch.instrument || currentSymbol} · ${group.label} · ${aiHistoryStatusText(batch.status)} · ${batch.model_count ?? batch.runs?.length ?? 0}模型`;
                const meta = document.createElement('div'); meta.className = 'ai-history-meta';
                meta.textContent = `分析时间：${String(batch.snapshot_time || '').replace('T', ' ').slice(0,19) || '未记录'} · 交易日：${fmtTradingDay(batch.trading_day) || '未记录'}`;
                text.append(title, meta);
                appendAIHistorySummary(text, batch);
                const button = document.createElement('button'); button.className = 'ai-action'; button.textContent = '查看';
                button.onclick = () => {
                    // 详情面板插到当前行下方; 再点同一行收起, 换行则移动位置
                    const isOpen = aiHistoryDetailEl.style.display === 'block'
                        && aiHistoryDetailEl.previousElementSibling === row;
                    if (isOpen) { closeAIHistoryDetail(); return; }
                    box.querySelectorAll('.ai-history-row')
                        .forEach(r => r.classList.remove('ai-history-active'));
                    row.classList.add('ai-history-active');
                    row.after(aiHistoryDetailEl);
                    renderAIBatch(batch, {
                        panelId:'ai-history-detail', metaId:'ai-history-detail-meta',
                        requirementsId:'ai-history-detail-requirements', listId:'ai-history-detail-list',
                        allowEvaluation:true
                    });
                    aiHistoryDetailEl.style.display = 'block';
                    aiHistoryDetailEl.scrollIntoView({behavior:'smooth', block:'start'});
                };
                row.append(text, button); box.appendChild(row);
            });
        }

        function closeAIHistoryModal() {
            document.getElementById('ai-history-modal').style.display = 'none';
        }

        function newClientRequestId() {
            if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
                const r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 3 | 8);
                return v.toString(16);
            });
        }

        async function startAIAnalysis() {
            const ids = Array.from(document.querySelectorAll('.ai-model-select:checked')).map(el => el.value);
            const analysisTarget = currentAIAnalysisTarget();
            const analysisRequirements = document.getElementById('ai-analysis-requirements').value.trim();
            const sessionStartTime = analysisTarget === 'CURRENT_SESSION'
                ? document.getElementById('ai-session-select').value : null;
            const message = document.getElementById('ai-analysis-message');
            if (!ids.length) { message.textContent = '请至少选择一个可用模型。'; return; }
            if (analysisTarget === 'CURRENT_SESSION' && !sessionStartTime) {
                message.textContent = '请选择一个尚未结束的交易时段。'; return;
            }
            const button = document.getElementById('ai-analysis-start');
            button.disabled = true;
            message.textContent = analysisTarget === 'FUTURE_TREND' ? '正在冻结行情与已缓存资料；预测不会现场检索公开资料或等待采集…' : '正在冻结行情快照...';
            const symbol = currentSymbol;
            const futureTimer = analysisTarget === 'FUTURE_TREND' ? startFutureTrendTimer(symbol) : null;
            try {
                const res = await fetch(`/api/v1/ai/analysis/${encodeURIComponent(symbol)}`, {
                    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
                        client_request_id:newClientRequestId(), model_config_ids:ids,
                        analysis_target:analysisTarget, analysis_requirements:analysisRequirements,
                        session_start_time:sessionStartTime,
                        use_review_summaries:document.getElementById('ai-use-review-summaries').checked,
                        self_consistency_votes:document.getElementById('ai-self-consistency')?.checked ? 3 : 1,
                        allow_insufficient_data:false
                    })
                });
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const batch = await res.json();
                if (futureTimer) futureTimer.batchId = batch.batch_id;
                aiActiveBatches.set(symbol, {batchId:batch.batch_id, batch, pollTimer:null});
                syncAIAnalysisButton();
                closeAIAnalysisModal();
                renderAIBatch(batch);
                scheduleAIPoll(symbol);
            } catch (e) {
                if (futureTimer) finishFutureTrendTimer(symbol);
                message.textContent = e.message;
            } finally {
                button.disabled = false;
            }
        }

        function scheduleAIPoll(symbol) {
            const active = activeAIBatch(symbol);
            if (!active) return;
            clearTimeout(active.pollTimer);
            active.pollTimer = setTimeout(() => pollAIBatch(symbol), 1000);
        }

        async function pollAIBatch(symbol) {
            const active = activeAIBatch(symbol);
            if (!active) return;
            try {
                const res = await fetch(`/api/v1/ai/analysis/batches/${active.batchId}`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const batch = await res.json();
                active.batch = batch;
                if (currentSymbol === symbol) renderAIBatch(batch);
                if (['COMPLETED','PARTIAL_FAILED','FAILED','INTERRUPTED'].includes(batch.status)) {
                    if (batch.analysis_target === 'FUTURE_TREND' && aiFutureTrendTimers.get(symbol)?.batchId === batch.batch_id) finishFutureTrendTimer(symbol);
                    clearTimeout(active.pollTimer);
                    aiActiveBatches.delete(symbol);
                    if (currentSymbol === symbol) syncAIAnalysisButton();
                    return;
                }
                scheduleAIPoll(symbol);
            } catch (e) {
                if (currentSymbol === symbol && (currentPeriod !== 'intraday' || !intradayDate
                    || (active.batch.session_forecast?.trading_day || active.batch.trading_day) === intradayDate)) {
                    document.getElementById(active.batch.analysis_target === 'FUTURE_TREND' ? 'future-trend-meta' : 'ai-batch-meta').textContent = '状态更新失败：' + e.message;
                }
                scheduleAIPoll(symbol);
            }
        }

        function directionText(direction) {
            return {LONG:'偏多', SHORT:'偏空', NEUTRAL:'中性', NO_JUDGMENT:'无法判断'}[direction] || direction || '--';
        }

        function nextOpenDirectionText(direction) {
            return {UP:'高开倾向', DOWN:'低开倾向', FLAT:'平开倾向', NO_JUDGMENT:'无法判断'}[direction] || direction || '--';
        }

        function aiEvaluationText(evaluation) {
            if (!evaluation) return '你的评价：待验证';
            const label = {CORRECT:'正确', PARTIAL:'部分正确', WRONG:'错误', UNVERIFIABLE:'无法验证'}[evaluation.rating] || evaluation.rating;
            return `你的评价：${label}${evaluation.score == null ? ' · 不计入正确率' : ` · ${evaluation.score}分`}`;
        }

        function appendAIEvaluation(card, run, analysisTarget) {
            const box = document.createElement('div'); box.className = 'ai-evaluation-box';
            const accuracy = document.createElement('div');
            accuracy.className = 'ai-result-accuracy';
            accuracy.dataset.modelId = run.model_config_id;
            accuracy.dataset.analysisTarget = analysisTarget;
            accuracy.style.cssText = 'font-size:10px; color:#93c5fd;';
            accuracy.textContent = formatAIAccuracy(run.model_config_id, analysisTarget);
            const state = document.createElement('div');
            state.style.cssText = 'font-size:10px; color:var(--text-muted); margin-top:3px;';
            state.textContent = aiEvaluationText(run.evaluation);
            box.append(accuracy, state);
            if (run.evaluation) {
                const locked = document.createElement('div');
                locked.style.cssText = 'font-size:10px; color:#94a3b8; margin-top:5px;';
                locked.textContent = run.evaluation.comment
                    ? `评价说明：${run.evaluation.comment} · 已保存，不可修改或取消`
                    : '评分已保存，不可修改或取消';
                box.appendChild(locked);
                card.appendChild(box);
                return;
            }
            const controls = document.createElement('div'); controls.className = 'ai-evaluation-controls';
            const rating = document.createElement('select');
            [['','选择评分'],['CORRECT','正确 · 100分'],['PARTIAL','部分正确 · 50分'],
             ['WRONG','错误 · 0分'],['UNVERIFIABLE','无法验证 · 不计入']].forEach(([value, label]) => {
                const option = document.createElement('option'); option.value = value; option.textContent = label;
                rating.appendChild(option);
            });
            rating.value = run.evaluation?.rating || '';
            const comment = document.createElement('input');
            comment.maxLength = 1000; comment.placeholder = '评价说明（可选）';
            comment.value = run.evaluation?.comment || '';
            const save = document.createElement('button'); save.className = 'ai-action'; save.textContent = '保存评价';
            save.onclick = async () => {
                if (!rating.value) { state.textContent = '请先选择评分。'; return; }
                save.disabled = true; state.textContent = '正在保存评价...';
                try {
                    const res = await fetch(`/api/v1/ai/analysis/runs/${run.run_id}/evaluation`, {
                        method:'PUT', headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({rating:rating.value, comment:comment.value.trim()})
                    });
                    if (!res.ok) throw new Error(await aiErrorMessage(res));
                    const data = await res.json();
                    run.evaluation = data.evaluation;
                    aiEvaluationStats[run.model_config_id] ||= {};
                    aiEvaluationStats[run.model_config_id][analysisTarget] = data.stats;
                    refreshAIModelAccuracyLabels();
                    box.remove();
                    appendAIEvaluation(card, run, analysisTarget);
                    appendAIReviewSummary(card, run, analysisTarget);
                } catch (e) {
                    state.textContent = '保存失败：' + e.message;
                } finally {
                    save.disabled = false;
                }
            };
            controls.append(rating, comment, save); box.appendChild(controls); card.appendChild(box);
        }

        let aiReviewCurrent = null;

        async function openAIReviewModal(run, analysisTarget, summary = null, reviewBox = null) {
            aiReviewCurrent = {run, analysisTarget, summary, reviewBox};
            document.getElementById('ai-review-source').textContent =
                `${run.model_display_name} · ${analysisTarget === 'FUTURE_TREND' ? '未来趋势' : analysisTarget === 'NEXT_OPEN' ? '明日开盘' : analysisTarget === 'CURRENT_SESSION' ? '盘时段' : '今日走势'} · ${aiEvaluationText(run.evaluation)}`;
            document.getElementById('ai-review-guidance').value = summary?.user_guidance || run.evaluation?.comment || '';
            document.getElementById('ai-review-scope').value = summary?.scope || 'CONTRACT';
            document.getElementById('ai-review-message').textContent = '';
            const select = document.getElementById('ai-review-model');
            select.innerHTML = '<option value="">正在读取模型...</option>';
            document.getElementById('ai-review-modal').style.display = 'flex';
            try {
                const models = await fetchAIModels();
                select.replaceChildren();
                models.filter(model => model.available).forEach(model => {
                    const option = document.createElement('option');
                    option.value = model.model_config_id;
                    option.textContent = `${model.display_name} · ${model.model_name}`;
                    select.appendChild(option);
                });
                const preferred = summary?.generator_model_config_id || run.model_config_id;
                if (Array.from(select.options).some(option => option.value === preferred)) select.value = preferred;
                if (!select.options.length) document.getElementById('ai-review-message').textContent = '没有可用模型，无法生成复盘总结。';
            } catch (e) { document.getElementById('ai-review-message').textContent = e.message; }
        }

        function closeAIReviewModal() {
            document.getElementById('ai-review-modal').style.display = 'none';
            aiReviewCurrent = null;
        }

        async function submitAIReviewSummary() {
            if (!aiReviewCurrent) return;
            const guidance = document.getElementById('ai-review-guidance').value.trim();
            const modelId = document.getElementById('ai-review-model').value;
            const message = document.getElementById('ai-review-message');
            if (!modelId) { message.textContent = '请选择可用的总结模型。'; return; }
            if (['WRONG','PARTIAL'].includes(aiReviewCurrent.run.evaluation?.rating) && !guidance && !aiReviewCurrent.run.evaluation?.comment) {
                message.textContent = '错误或部分正确的评分必须填写复盘依据。'; return;
            }
            const button = document.getElementById('ai-review-submit');
            button.disabled = true; message.textContent = '正在提交复盘总结任务...';
            try {
                const path = aiReviewCurrent.summary
                    ? `/api/v1/ai/review-summaries/${aiReviewCurrent.summary.summary_id}/regenerate`
                    : `/api/v1/ai/analysis/runs/${aiReviewCurrent.run.run_id}/review-summaries`;
                const res = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
                    generator_model_config_id:modelId,
                    scope:document.getElementById('ai-review-scope').value,
                    user_guidance:guidance
                })});
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const created = await res.json();
                const reviewBox = aiReviewCurrent.reviewBox;
                const run = aiReviewCurrent.run;
                const analysisTarget = aiReviewCurrent.analysisTarget;
                closeAIReviewModal();
                if (reviewBox) await refreshAIReviewBox(reviewBox, run, analysisTarget);
            } catch (e) {
                message.textContent = e.message;
            } finally { button.disabled = false; }
        }

        function reviewSummaryText(summary) {
            if (!summary) return '';
            const lines = [
                `结论：${summary.verdict || '--'}`,
                `用户确认结果：${summary.user_verified_outcome || '--'}`,
                `正确部分：${(summary.correct_points || []).join('；') || '无'}`,
                `错误部分：${(summary.incorrect_points || []).join('；') || '无'}`,
                `可复用经验：${(summary.reusable_lessons || []).join('；') || '无'}`,
                `以后检查：${(summary.future_checklist || []).join('；') || '无'}`,
                `不可复用事实：${(summary.non_reusable_facts || []).join('；') || '无'}`,
            ];
            return lines.join('\n');
        }

        async function appendAIReviewSummary(card, run, analysisTarget) {
            if (!run.evaluation) return;
            const box = document.createElement('div'); box.className = 'ai-review-box';
            box.textContent = '正在读取复盘总结...';
            card.appendChild(box);
            await refreshAIReviewBox(box, run, analysisTarget);
        }

        async function refreshAIReviewBox(box, run, analysisTarget) {
            if (!box?.isConnected) return;
            try {
                const res = await fetch(`/api/v1/ai/analysis/runs/${run.run_id}/review-summaries`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const items = (await res.json()).items || [];
                box.replaceChildren();
                const head = document.createElement('div'); head.style.fontWeight = '600';
                head.textContent = 'AI复盘总结'; box.appendChild(head);
                if (!items.length) {
                    const hint = document.createElement('div');
                    hint.style.cssText = 'font-size:10px; color:var(--text-muted); margin-top:4px;';
                    hint.textContent = '只有你主动选择的回答才会生成总结。';
                    const create = document.createElement('button'); create.className = 'ai-action';
                    create.style.marginTop = '6px'; create.textContent = '生成复盘总结';
                    create.onclick = () => openAIReviewModal(run, analysisTarget, null, box);
                    box.append(hint, create); return;
                }
                items.forEach(item => {
                    const section = document.createElement('div'); section.className = 'ai-conversation-turn';
                    const meta = document.createElement('div');
                    meta.style.cssText = 'font-size:10px; color:#93c5fd;';
                    meta.textContent = `第${item.revision_no}版 · ${item.status} · ${item.scope === 'PRODUCT' ? '同品种' : '当前合约'}`;
                    section.appendChild(meta);
                    if (item.summary) {
                        const content = document.createElement('div'); content.className = 'ai-review-summary';
                        content.textContent = reviewSummaryText(item.summary); section.appendChild(content);
                    } else if (item.error) {
                        const error = document.createElement('div'); error.style.cssText = 'color:#fca5a5; margin-top:5px;';
                        error.textContent = item.error; section.appendChild(error);
                    } else if (['PENDING','RUNNING'].includes(item.status)) {
                        const waiting = document.createElement('div'); waiting.style.cssText = 'color:var(--text-muted); margin-top:5px;';
                        waiting.textContent = 'AI正在生成复盘总结...'; section.appendChild(waiting);
                    }
                    const actions = document.createElement('div'); actions.style.cssText = 'display:flex; gap:6px; flex-wrap:wrap; margin-top:6px;';
                    if (['DRAFT','DISABLED'].includes(item.status)) {
                        const activate = document.createElement('button'); activate.className = 'ai-action ai-primary'; activate.textContent = '确认启用';
                        activate.onclick = () => changeAIReviewStatus(item.summary_id, 'activate', box, run, analysisTarget);
                        actions.appendChild(activate);
                    }
                    if (item.status === 'ACTIVE') {
                        const disable = document.createElement('button'); disable.className = 'ai-action'; disable.textContent = '停用总结';
                        disable.onclick = () => changeAIReviewStatus(item.summary_id, 'disable', box, run, analysisTarget);
                        actions.appendChild(disable);
                    }
                    if (!['PENDING','RUNNING'].includes(item.status)) {
                        const regenerate = document.createElement('button'); regenerate.className = 'ai-action'; regenerate.textContent = '重新生成';
                        regenerate.onclick = () => openAIReviewModal(run, analysisTarget, item, box);
                        const raw = document.createElement('button'); raw.className = 'ai-action'; raw.textContent = '查看总结原始记录';
                        raw.onclick = () => showAIReviewRaw(item.summary_id, section, raw);
                        actions.append(regenerate, raw);
                    }
                    section.appendChild(actions); box.appendChild(section);
                    if (['PENDING','RUNNING'].includes(item.status)) setTimeout(() => pollAIReviewSummary(item.summary_id, box, run, analysisTarget), 1000);
                });
            } catch (e) { box.textContent = '复盘总结读取失败：' + e.message; }
        }

        async function pollAIReviewSummary(summaryId, box, run, analysisTarget = 'TODAY') {
            if (!box?.isConnected) return;
            try {
                const res = await fetch(`/api/v1/ai/review-summaries/${summaryId}`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const item = await res.json();
                if (['PENDING','RUNNING'].includes(item.status)) {
                    setTimeout(() => pollAIReviewSummary(summaryId, box, run, analysisTarget), 1000); return;
                }
                await refreshAIReviewBox(box, run, analysisTarget);
            } catch (e) { box.textContent = '复盘总结状态更新失败：' + e.message; }
        }

        async function changeAIReviewStatus(summaryId, action, box, run, analysisTarget) {
            try {
                const res = await fetch(`/api/v1/ai/review-summaries/${summaryId}/${action}`, {method:'POST'});
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                await refreshAIReviewBox(box, run, analysisTarget);
            } catch (e) { alert(e.message); }
        }

        async function showAIReviewRaw(summaryId, section, button) {
            button.disabled = true;
            try {
                const res = await fetch(`/api/v1/ai/review-summaries/${summaryId}?include_raw=true`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                const pre = document.createElement('pre'); pre.className = 'ai-raw-record';
                pre.textContent = `请求：\n${JSON.stringify(data.request, null, 2)}\n\n响应：\n${data.raw_response_text || data.error || ''}`;
                section.appendChild(pre); button.remove();
            } catch (e) { alert(e.message); button.disabled = false; }
        }

        async function appendAIConversation(card, run) {
            const box = document.createElement('div'); box.className = 'ai-conversation';
            box.textContent = '正在读取追问对话...';
            card.appendChild(box);

            const render = data => {
                box.replaceChildren();
                const title = document.createElement('div');
                title.style.cssText = 'font-weight:600; font-size:11px;';
                title.textContent = `追问对话 · ${data.turn_count}/${data.max_turns}轮`;
                const notice = document.createElement('div');
                notice.style.cssText = 'font-size:10px; color:#f59e0b; margin-top:3px;';
                notice.textContent = '只使用原分析快照、原回答和本段历史问答，不读取最新行情；追问不计入正确率。';
                box.append(title, notice);
                (data.followups || []).forEach(item => {
                    const turn = document.createElement('div'); turn.className = 'ai-conversation-turn';
                    const question = document.createElement('div');
                    question.style.cssText = 'color:#93c5fd; font-size:11px;';
                    question.textContent = `你：${item.attach_latest ? '【含最新行情】' : ''}${item.question}`;
                    const answer = document.createElement('div');
                    answer.style.cssText = 'margin-top:5px; color:#e2e8f0; font-size:11px;';
                    answer.textContent = item.status === 'SUCCEEDED'
                        ? `AI：${item.answer || ''}`
                        : `${item.status === 'PENDING' || item.status === 'RUNNING' ? 'AI 正在回答...' : `回答失败：${item.error || item.status}`}`;
                    turn.append(question, answer); box.appendChild(turn);
                });

                const active = (data.followups || []).find(item => ['PENDING','RUNNING'].includes(item.status));
                const disabledReason = !data.available ? (data.unavailable_reason || '原模型当前不可用')
                    : data.turn_count >= data.max_turns ? '已达到20轮上限，请重新发起AI分析'
                    : active ? '上一条追问仍在执行' : '';
                const compose = document.createElement('div'); compose.className = 'ai-conversation-compose';
                const input = document.createElement('textarea');
                input.maxLength = 2000; input.placeholder = disabledReason || '针对这个模型的原分析继续追问（最多2000字）';
                const attachLabel = document.createElement('label');
                attachLabel.style.cssText = 'grid-column:1 / -1; display:flex; gap:6px; align-items:center; font-size:10px; color:var(--text-muted); cursor:pointer;';
                attachLabel.innerHTML = '<input type="checkbox" class="ai-attach-latest">发送时附带最新行情（提问时刻的报价与最近1分钟K线，适合问"现在/最新"类问题）';
                const send = document.createElement('button'); send.className = 'ai-action ai-primary';
                send.textContent = '发送追问';
                input.disabled = Boolean(disabledReason); send.disabled = Boolean(disabledReason);
                const state = document.createElement('div');
                state.style.cssText = 'grid-column:1 / -1; min-height:14px; color:var(--text-muted); font-size:10px;';
                state.textContent = disabledReason;
                send.onclick = async () => {
                    const question = input.value.trim();
                    if (!question) { state.textContent = '请输入追问内容。'; return; }
                    input.disabled = true; send.disabled = true; state.textContent = '正在提交追问...';
                    try {
                        const res = await fetch(`/api/v1/ai/analysis/runs/${run.run_id}/follow-ups`, {
                            method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
                                question,
                                attach_latest_market: attachLabel.querySelector('.ai-attach-latest').checked
                            })
                        });
                        if (!res.ok) throw new Error(await aiErrorMessage(res));
                        const followup = await res.json();
                        await pollAIFollowUp(followup.followup_id, run, box);
                    } catch (e) {
                        state.textContent = e.message; input.disabled = false; send.disabled = false;
                    }
                };
                compose.append(input, attachLabel, send, state); box.appendChild(compose);
                if (active) setTimeout(() => pollAIFollowUp(active.followup_id, run, box), 1000);
            };

            try {
                const res = await fetch(`/api/v1/ai/analysis/runs/${run.run_id}/conversation`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                render(await res.json());
            } catch (e) { box.textContent = '追问对话读取失败：' + e.message; }
        }

        async function pollAIFollowUp(followupId, run, box) {
            try {
                const res = await fetch(`/api/v1/ai/analysis/follow-ups/${followupId}`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const followup = await res.json();
                if (['PENDING','RUNNING'].includes(followup.status)) {
                    setTimeout(() => pollAIFollowUp(followupId, run, box), 1000);
                    return;
                }
                const card = box.parentElement;
                box.remove();
                if (card) await appendAIConversation(card, run);
            } catch (e) {
                box.textContent = '追问状态更新失败：' + e.message;
            }
        }

        function appendAIFutureMaterials(list, batch) {
            const materials = batch.public_materials || {};
            const box = document.createElement('div'); box.className = 'ai-result-card';
            box.style.gridColumn = '1 / -1';
            const head = document.createElement('div');
            const scheduled = materials.cache_state != null || materials.config_version != null;
            const researching = ['PENDING', 'RUNNING'].includes(materials.research?.status);
            const historicalRefs = batch.related_contracts || materials.related_contracts || [];
            head.textContent = scheduled
                ? `本批冻结资料：${{AVAILABLE:'可用',PARTIAL:'部分可用',EMPTY:'空缓存',MISSING:'缺失',NOT_CONFIGURED:'目标未配置',STALE:'过期'}[materials.cache_state] || materials.cache_state || materials.status || '未记录'} · 配置版本 ${materials.config_version ?? '未知'}${historicalRefs.length ? ' · 历史冻结关联 ' + historicalRefs.join('、') : ''}`
                : `公开资料：${researching ? '检索进行中' : materials.status || '未记录'} · 缺失维度：${researching ? '待检索确认' : (materials.missing_dimensions || []).join('、') || '无已报告缺失'}`;
            box.appendChild(head);
            if (scheduled) {
                const timing = document.createElement('div');
                const jobs = materials.research?.jobs || [];
                const successes = jobs.filter(job => job.published && job.valid_material_count > 0).map(job => job.completed_at).filter(Boolean).sort();
                const next = jobs.map(job => job.next_check_at).filter(Boolean).sort();
                const nextCheck = materials.collection_enabled === false ? '定时关闭' : materials.next_check_at || next[0] || '--';
                timing.textContent = `最近检查 ${materials.last_check_at || materials.research?.last_check_at || '--'} · 最近成功 ${materials.last_success_at || successes.at(-1) || '--'} · 下次检查 ${nextCheck} · 预测不搜索或等待采集`;
                box.appendChild(timing);
                const tags = document.createElement('div'); tags.style.cssText = 'display:flex; flex-wrap:wrap; gap:8px; margin-top:5px;';
                [['fundamentals','基本面','#22d3ee'],['macro','宏观','#fb923c'],['events','事件','#fbbf24'],['related_markets','相关市场','#818cf8']].forEach(([key,label,color]) => {
                    const available = (materials.sources || []).some(source => source.dimension === key && !source.stale);
                    const stale = [...(materials.background_sources || []), ...(materials.sources || []).filter(source => source.stale)].some(source => source.dimension === key);
                    const tag = document.createElement('span'); tag.style.color = available ? color : stale ? '#f59e0b' : '#9ca3af';
                    tag.textContent = `${label}：${available ? '已提供' : stale ? '过期/待核验' : '缺失'}`; tags.appendChild(tag);
                });
                box.appendChild(tags);
            }
            const content = document.createElement('details'); content.dataset.publicMaterials = 'true';
            const summary = document.createElement('summary'); summary.style.cssText = 'cursor:pointer; color:#7dd3fc;';
            summary.textContent = scheduled ? '展开本批保存正文、来源和采集详情' : '展开公开资料与检索详情';
            content.appendChild(summary); box.appendChild(content);
            if (typeof appendAIFutureResearch === 'function') appendAIFutureResearch(content, batch, true);
            if (materials.freshness_policy) {
                const policy = document.createElement('div');
                policy.textContent = '资料时效：库存、供需近 14 天；事件、相关市场近 7 天；宏观取最新官方发布。按发布日期筛选，抓取时间不代表资料更新时间。';
                content.appendChild(policy);
            }
            (materials.errors || []).forEach(error => {
                const line = document.createElement('div'); line.style.color = '#f59e0b';
                const toolName = {search_public_web:'网页搜索', read_public_page:'网页阅读', latest_official_macro:'官方宏观资料'}[error?.tool];
                line.textContent = `资料检索错误：${toolName ? toolName + ' · ' : ''}${typeof error === 'string' ? error : error.reason || JSON.stringify(error)}`;
                content.appendChild(line);
            });
            function appendSource(source, container) {
                const row = document.createElement('div');
                row.style.cssText = 'font-size:11px; margin-top:8px; white-space:pre-wrap;';
                let safeUrl = null;
                try {
                    const parsed = new URL(source.url);
                    if (parsed.protocol === 'https:' && !parsed.username && !parsed.password) safeUrl = parsed.href;
                } catch (_) {}
                const title = document.createElement(safeUrl ? 'a' : 'span');
                title.textContent = `[${source.source_id || '--'}] ${source.title || '未命名资料'}`;
                if (safeUrl) { title.href = safeUrl; title.target = '_blank'; title.rel = 'noopener noreferrer'; }
                const details = document.createElement('div');
                details.textContent = `来源：${source.source_name || '未知'} · ${source.source_kind || '未知'} · 范围：${source.scope || '未知'} · 维度：${source.dimension || '未知'}\n`
                    + `来源URL：${source.url || '未记录'}\n`
                    + `发布：${source.published_at || '未知'} · 抓取：${source.fetched_at || '未知'} · 资料期别：${source.data_period || '未知'}\n`
                    + `${source.content_type === 'EXCERPT' || source.content_type === 'SEARCH_EXCERPT' ? '检索摘要' : source.content_type === 'BODY' || source.content_type === 'FULL_TEXT' ? '正文摘录' : '内容类型未知'}${source.stale ? ' · 陈旧资料' : ''}：${source.excerpt || '无摘录'}`;
                row.append(title, details); container.appendChild(row);
                if (source.material_id) {
                    const version = document.createElement('div');
                    version.textContent = `资料版本 ${source.material_id} · 版本获取 ${source.version_fetched_at || source.fetched_at || '未知'} · 适用合约 ${(source.applicable_contracts || []).join('、') || '品种/公共资料'} · 参考合约 ${(source.reference_contracts || []).join('、') || '目标自身/公共资料'}`;
                    const body = document.createElement('pre'); body.style.cssText = 'white-space:pre-wrap; max-height:320px; overflow:auto;';
                    const read = document.createElement('button'); read.className = 'ai-action'; read.textContent = '查看保存正文';
                    read.onclick = () => loadAIResearchMaterial(source.material_id, body); row.append(version, read, body);
                }
            }
            const current = (materials.sources || []).filter(source => !source.stale);
            const background = [...(materials.background_sources || []), ...(materials.sources || []).filter(source => source.stale)];
            current.forEach(source => appendSource(source, content));
            if (background.length) {
                const group = document.createElement('details');
                const summary = document.createElement('summary');
                summary.textContent = `背景资料（${background.length} 条）· 过期或发布时间未知，不能作为当前方向依据`;
                group.appendChild(summary);
                background.forEach(source => appendSource(source, group));
                content.appendChild(group);
            }
            if (!current.length) {
                const ongoing = ['PENDING', 'RUNNING'].includes(materials.research?.status);
                const empty = document.createElement('div');
                empty.textContent = scheduled ? '缺少近期可用公开资料，本批按有效行情分析；预测不会现场搜索或等待采集。更新资料后请重新点击预测。'
                    : ongoing ? '公开资料检索进行中，来源和缺失维度尚未确定。' : '缺少近期可用公开资料，不能据此宣称完整覆盖。';
                content.appendChild(empty);
            }
            list.appendChild(box);
        }

        function appendAIFutureResearch(container, batch, showProcess = false) {
            const research = batch.public_materials?.research;
            if (!research) return;
            if (batch.public_materials?.cache_state != null || batch.public_materials?.config_version != null) {
                const details = document.createElement('details'); details.dataset.researchOverview = 'true';
                const summary = document.createElement('summary'); summary.style.cssText = 'cursor:pointer; color:#7dd3fc;';
                summary.textContent = `缓存采集：${{CACHED:'已冻结',COMPLETED:'检查完成',PARTIAL:'部分完成',FAILED:'上次失败',RUNNING:'更新中',PENDING:'排队中',EMPTY:'未采集',NOT_CONFIGURED:'目标未配置',UNAVAILABLE:'研究模型不可用'}[research.status] || research.status || '冻结缓存'}（展开采集详情）`;
                details.appendChild(summary);
                if (research.jobs) {
                    (research.jobs || []).forEach(job => {
                        const audit = job.research || {};
                        const line = document.createElement('div'); line.style.cssText = 'font-size:11px; margin-top:5px; white-space:pre-wrap;';
                        line.textContent = `${job.topic} · ${job.status} · 配置版本 ${job.config_version ?? '--'} · 检查 ${job.completed_at || '--'}\n研究模型 ${[audit.model_display_name || audit.model_config_id,audit.model_name,audit.provider].filter(Boolean).join(' · ') || '未记录'} · 搜索 ${audit.search_count || 0} 次 · 阅读 ${audit.pages_read || 0} 页 · 轮次 ${audit.rounds || 0} · 调用 ${job.call_count || 0} 次 · 输入 ${job.input_tokens || 0} / 输出 ${job.output_tokens || 0} Token · 采集耗时 ${job.latency_ms ?? '--'}ms`;
                        details.appendChild(line);
                        if (job.error || audit.error) { const error = document.createElement('div'); error.textContent = '采集错误：' + (job.error || audit.error); details.appendChild(error); }
                        if (showProcess) (audit.events || []).forEach(event => {
                            const row = document.createElement('div'); row.style.cssText = 'font-size:11px; white-space:pre-wrap;';
                            row.textContent = `${event.timestamp || ''} · ${event.type || ''} · ${event.status || ''} · ${[event.query,event.title,event.url,event.message].filter(Boolean).join(' · ')}`; details.appendChild(row);
                        });
                    });
                    if (!research.jobs.length) { const empty = document.createElement('div'); empty.textContent = '本批未取得已完成采集记录，预测没有现场搜索或阅读。'; details.appendChild(empty); }
                    container.appendChild(details); return;
                }
                const line = document.createElement('div');
                line.textContent = `研究模型：${[research.model_display_name || research.model_config_id,research.model_name,research.provider].filter(Boolean).join(' · ') || '未记录'} · 搜索 ${research.search_count || 0} 次 · 阅读 ${research.pages_read || 0} 页 · 轮次 ${research.rounds || 0} · 调用 ${research.call_count || 0} 次 · 输入 ${research.input_tokens || 0} / 输出 ${research.output_tokens || 0} Token · 采集耗时 ${research.latency_ms ?? '--'}ms`;
                details.appendChild(line);
                if (research.error) { const error = document.createElement('div'); error.textContent = '采集错误：' + research.error; details.appendChild(error); }
                if (showProcess) (research.events || []).forEach(event => {
                    const row = document.createElement('div'); row.style.cssText = 'font-size:11px; white-space:pre-wrap; margin-top:5px;';
                    row.textContent = `${event.timestamp || ''} · ${event.type || ''} · ${event.status || ''}\n${[event.query,event.title,event.url,event.message].filter(Boolean).join(' · ')}`; details.appendChild(row);
                });
                container.appendChild(details); return;
            }
            const status = {PENDING:'等待检索', RUNNING:'检索中', COMPLETED:'检索完成', PARTIAL:'检索部分完成',
                UNAVAILABLE:'检索不可用', TIMEOUT:'检索超时', SKIPPED:'检索已跳过', INTERRUPTED:'检索已中断'}[research.status] || research.status || '未记录';
            const identity = [research.model_display_name || research.model_config_id, research.model_name, research.provider].filter(Boolean).join(' · ') || '尚未选定';
            const searches = Number(research.search_count) || 0, pages = Number(research.pages_read) || 0;
            const state = document.getElementById('future-trend-summary').dataset;
            const batchKey = batch.batch_id || `${batch.instrument || ''}:${batch.snapshot_time || ''}`;
            if (state.researchBatchId !== batchKey) { state.researchBatchId = batchKey; state.researchOpen = 'false'; state.researchOverviewOpen = 'false'; }
            if (state.researchStatus !== research.status) { state.researchStatus = research.status; state.researchOverviewOpen = 'false'; }
            let overview = container;
            if (!['PENDING', 'RUNNING'].includes(research.status)) {
                overview = document.createElement('details'); overview.dataset.researchOverview = 'true';
                overview.style.cssText = 'font-size:11px; margin:6px 0; overflow-wrap:anywhere;';
                overview.open = state.researchOverviewOpen === 'true';
                overview.ontoggle = () => {
                    summary.textContent = `公开资料研究：${status}（点击${overview.open ? '收起' : '展开'}）`;
                    if (state.researchBatchId === batchKey && state.researchStatus === research.status)
                        state.researchOverviewOpen = String(overview.open);
                };
                const summary = document.createElement('summary');
                summary.style.cssText = 'cursor:pointer; color:#7dd3fc;';
                summary.textContent = `公开资料研究：${status}（点击${overview.open ? '收起' : '展开'}）`;
                overview.appendChild(summary); container.appendChild(overview);
            }
            const line = document.createElement('div');
            line.style.cssText = 'font-size:11px; margin:6px 0; white-space:pre-wrap;';
            line.textContent = `公开资料研究：${status} · 检索模型：${identity} · 搜索 ${searches} 次 · 阅读 ${pages} 页 · 模型轮次 ${Number(research.rounds) || 0}`;
            overview.appendChild(line);
            const events = research.events || [];
            const last = events.filter(event => ['SEARCH', 'READ'].includes(event.type)).slice(-1)[0];
            if (last) {
                const action = document.createElement('div');
                action.textContent = `最近工具动作：${last.type === 'SEARCH' ? '搜索' : '阅读'} · ${last.query || last.title || last.url || '--'} · ${last.status || '已执行'}`;
                overview.appendChild(action);
            }
            if (!showProcess) return;
            if (!['PENDING', 'RUNNING'].includes(research.status) && !searches && !pages) {
                const gap = document.createElement('div');
                gap.textContent = '未执行公开搜索或网页阅读，外部资料缺失；模型预测如继续，仅能使用现有行情及已记录证据。';
                container.appendChild(gap);
            }
            if (research.error) {
                const error = document.createElement('div'); error.textContent = `检索错误：${research.error}`; container.appendChild(error);
            }
            const details = document.createElement('details');
            details.dataset.researchProcess = 'true';
            details.open = state.researchOpen === 'true';
            details.ontoggle = () => { if (state.researchBatchId === batchKey) state.researchOpen = String(details.open); };
            const summary = document.createElement('summary'); summary.textContent = `检索过程（${events.length} 条记录）`;
            details.appendChild(summary);
            events.forEach(event => {
                const row = document.createElement('div'); row.style.cssText = 'font-size:11px; margin-top:6px; white-space:pre-wrap;';
                const kind = {SEARCH:'工具搜索', READ:'工具阅读', FILTER:'资料筛选剔除', FALLBACK:'已切换备用搜索', ERROR:'执行错误', MODEL:'模型输出（非工具执行）'}[event.type] || '记录';
                row.textContent = `${event.timestamp || ''} · ${kind} · ${event.status || ''} · ${event.dimension || ''}\n${[event.query, event.title, event.message].filter(Boolean).join(' · ')}`;
                if (event.url) {
                    let safeUrl = null;
                    try { const url = new URL(event.url); if (url.protocol === 'https:' && !url.username && !url.password) safeUrl = url.href; } catch (_) {}
                    const link = document.createElement(safeUrl ? 'a' : 'span'); link.textContent = event.url;
                    if (safeUrl) { link.href = safeUrl; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
                    row.appendChild(link);
                }
                details.appendChild(row);
            });
            container.appendChild(details);
        }

        function appendAIFutureResult(card, run, batch) {
            const result = run.result;
            const box = document.createElement('div');
            box.style.cssText = 'font-size:11px; margin-top:8px; white-space:pre-wrap;';
            const confidence = document.createElement('div');
            confidence.textContent = '证据完整度分数不代表胜率，缺失资料请结合下方维度和来源查看';
            box.appendChild(confidence);
            const daysList = [...new Set((batch.future_targets?.length ? batch.future_targets : result.horizon_forecasts || []).map(item => item.horizon_days))];
            daysList.forEach(days => {
                const target = (batch.future_targets || []).find(item => item.horizon_days === days);
                const forecast = (result.horizon_forecasts || []).find(item => item.horizon_days === days);
                const row = document.createElement('div'); row.className = 'ai-open-forecast';
                const title = document.createElement('strong');
                title.textContent = `未来第${days}个交易日 · ${target?.trading_day ? fmtTradingDay(target.trading_day) : '目标日未记录'}收盘 · 冻结参考价 ${target?.reference_price ?? '--'} · 验证行情时点 ${target?.bar_time || '未知'}`;
                const prediction = document.createElement('div');
                prediction.textContent = forecast
                    ? `${directionText(forecast.direction)} · 证据完整度 ${forecast.confidence_score ?? '--'}/100\n${forecast.summary || ''}\n确认条件：${forecast.entry_condition || '--'}；失效条件：${forecast.invalidation_condition || '--'}`
                    : '模型未提供该期限预测';
                const metric = result.future_metrics?.[String(days)];
                const settlement = document.createElement('div');
                settlement.style.cssText = 'font-size:10px; color:#93c5fd; margin-top:4px;';
                settlement.textContent = metric
                    ? `自动验证：${{CORRECT:'正确', PARTIAL:'部分正确', WRONG:'错误', UNVERIFIABLE:'无法验证'}[metric.rating] || metric.rating || '待验证'} · 参考价 ${metric.reference_price ?? '--'} · 目标收盘 ${metric.realized_price ?? '--'} · 价差 ${metric.delta ?? '--'}\n`
                        + `已记录行情样本：${metric.sample_count ?? 0}根 · 最高变动 ${metric.max_rise_pct ?? '--'}% · 最低变动 ${metric.max_fall_pct ?? '--'}% · 验证时间 ${metric.evaluated_at || '未记录'}\n`
                        + '仅反映已记录行情，不代表缺失样本下的完整期间极值。'
                    : '自动验证：待到期 / 尚无目标收盘验证记录';
                row.append(title, prediction, settlement); box.appendChild(row);
            });
            appendAIFutureEvidence(box, result, batch);
            const alternative = document.createElement('div'); alternative.style.marginTop = '6px';
            alternative.textContent = `替代情景：${result.alternative_scenario || '未提供'}`;
            box.appendChild(alternative); card.appendChild(box);
        }

        const aiFutureTrendTimers = new Map();
        let aiFutureTrendTimerInterval = null;

        function updateFutureTrendTimer() {
            const box = document.getElementById('future-trend-timer');
            const timing = aiFutureTrendTimers.get(currentSymbol);
            box.style.display = timing ? 'block' : 'none';
            if (!timing) { box.textContent = ''; return; }
            const seconds = Math.max(0, Math.floor(((timing.finishedAt ?? Date.now()) - timing.startedAt) / 1000));
            const duration = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
            box.textContent = `${timing.finishedAt === null ? '已用' : '本次耗时'} ${duration}`;
        }

        function startFutureTrendTimer(symbol) {
            const timing = {startedAt:Date.now(), finishedAt:null, batchId:null};
            aiFutureTrendTimers.set(symbol, timing);
            updateFutureTrendTimer();
            if (aiFutureTrendTimerInterval === null) aiFutureTrendTimerInterval = setInterval(updateFutureTrendTimer, 1000);
            return timing;
        }

        function finishFutureTrendTimer(symbol) {
            const timing = aiFutureTrendTimers.get(symbol);
            if (!timing || timing.finishedAt !== null) return;
            timing.finishedAt = Date.now();
            updateFutureTrendTimer();
            if (![...aiFutureTrendTimers.values()].some(value => value.finishedAt === null)) {
                clearInterval(aiFutureTrendTimerInterval);
                aiFutureTrendTimerInterval = null;
            }
        }

        const aiFutureTrendModels = new Map();
        let aiFutureTrendLoadId = 0;

        function rememberFutureTrendBatch(batch, initialModels = null) {
            if (!aiFutureTrendModels.has(batch.instrument)) aiFutureTrendModels.set(batch.instrument, new Map());
            const models = aiFutureTrendModels.get(batch.instrument);
            (batch.runs || []).forEach((run, index) => {
                const key = run.model_config_id || run.run_id || String(index);
                const previous = models.get(key);
                if (initialModels && previous?.batch.batch_id === batch.batch_id && previous !== initialModels.get(key)) return;
                const time = batch.created_at || batch.snapshot_time || '';
                const previousTime = previous?.batch.created_at || previous?.batch.snapshot_time || '';
                if (!previous || previous.batch.batch_id === batch.batch_id || time > previousTime) {
                    models.set(key, {batch, run});
                }
            });
            return [...models.values()].sort((a, b) =>
                (b.batch.created_at || b.batch.snapshot_time || '').localeCompare(a.batch.created_at || a.batch.snapshot_time || ''));
        }

        function renderFutureTrendBatch(batch, selectedModelId = null, modelRecords = null) {
            const box = document.getElementById('future-trend-summary');
            const records = modelRecords || (batch.runs || []).map(run => ({batch, run}));
            const runs = records.map(record => record.run);
            const modelKey = (run, index) => run.model_config_id || run.run_id || String(index);
            const previous = box.dataset.instrument === batch.instrument ? box.dataset.modelId : null;
            const wanted = selectedModelId ?? previous;
            let index = runs.findIndex((run, i) => modelKey(run, i) === wanted);
            if (index < 0) index = Math.max(0, runs.findIndex(run => run.result));
            const selected = runs[index];
            batch = records[index]?.batch || batch;
            box.dataset.instrument = batch.instrument;
            box.dataset.modelId = selected ? modelKey(selected, index) : '';
            box.replaceChildren();
            const meta = document.createElement('div');
            meta.style.cssText = 'font-size:10px; margin-bottom:6px;';
            meta.textContent = `${batch.instrument} · 分析于 ${(batch.snapshot_time || '').replace('T', ' ')} · ${batch.status}`;
            box.appendChild(meta);
            appendAIFutureResearch(box, batch);
            if (runs.length > 1) {
                const models = document.createElement('div');
                models.style.cssText = 'display:flex; flex-wrap:wrap; gap:5px; margin-bottom:6px;';
                models.setAttribute('role', 'group');
                models.setAttribute('aria-label', '选择未来趋势模型');
                runs.forEach((run, i) => {
                    const button = document.createElement('button');
                    const key = modelKey(run, i);
                    button.type = 'button';
                    button.className = key === box.dataset.modelId ? 'ai-action ai-primary' : 'ai-action';
                    button.setAttribute('aria-pressed', String(key === box.dataset.modelId));
                    button.textContent = run.model_display_name || '模型';
                    button.title = `最新测算：${(records[i].batch.snapshot_time || '').replace('T', ' ')} · ${run.status || ''}`;
                    button.onclick = () => renderFutureTrendBatch(batch, key, records);
                    models.appendChild(button);
                });
                box.appendChild(models);
            }
            (selected ? [selected] : [{status:'PENDING'}]).forEach(run => {
                const model = document.createElement('div');
                model.textContent = run.model_display_name || '模型';
                model.style.marginTop = '6px';
                if (selected) box.appendChild(model);
                const targets = batch.future_targets?.length ? batch.future_targets : run.result?.horizon_forecasts || [];
                targets.forEach(target => {
                    const forecast = run.result?.horizon_forecasts?.find(f => f.horizon_days === target.horizon_days);
                    const row = document.createElement('div');
                    row.style.cssText = 'border:1px solid #334155; border-radius:5px; padding:7px; margin-top:5px;';
                    const title = document.createElement('strong');
                    title.textContent = `未来第 ${target.horizon_days} 个交易日 · ${fmtTradingDay(target.trading_day)}收盘`;
                    const prediction = document.createElement('div');
                    prediction.style.color = forecast?.direction === 'LONG' ? '#ef4444' : forecast?.direction === 'SHORT' ? '#10b981' : '#f59e0b';
                    prediction.textContent = forecast
                        ? `${directionText(forecast.direction)} · 证据完整度 ${forecast.confidence_score ?? '--'}/100`
                        : aiAnalysisErrorText(run.error) || (run.status === 'PENDING' ? '等待预测…' : run.status === 'RUNNING' ? '预测中…' : '暂无该期限判断');
                    const summary = document.createElement('div');
                    summary.style.cssText = 'margin-top:3px; white-space:pre-wrap;';
                    summary.textContent = forecast?.summary || '';
                    row.append(title, prediction, summary);
                    box.appendChild(row);
                });
            });
            if (selected?.result) appendAIFutureEvidence(box, selected.result, batch);
            document.getElementById('future-trend-details').style.display = 'block';
            renderAIBatch({...batch, runs:selected ? [selected] : []}, {panelId:'future-trend-result', metaId:'future-trend-meta',
                requirementsId:'future-trend-requirements', listId:'future-trend-list', allowEvaluation:true});
        }

        async function loadLatestFutureTrend(symbol) {
            updateFutureTrendTimer();
            if (isLiveMode || !symbol) return;
            if (typeof loadAIResearchStatus === 'function') loadAIResearchStatus(symbol).catch(e => {
                const state = document.getElementById('ai-research-home-status');
                if (state && currentSymbol === symbol) state.textContent = '资料状态读取失败：' + e.message;
            });
            const loadId = ++aiFutureTrendLoadId;
            const initialModels = new Map(aiFutureTrendModels.get(symbol));
            const box = document.getElementById('future-trend-summary');
            box.replaceChildren();
            box.textContent = `${symbol} · 正在读取未来趋势…`;
            document.getElementById('future-trend-details').style.display = 'none';
            const active = activeAIBatch(symbol);
            if (active?.batch.analysis_target === 'FUTURE_TREND') {
                renderAIBatch(active.batch);
            }
            try {
                const res = await fetch(`/api/v1/ai/analysis/future/latest?instrument=${encodeURIComponent(symbol)}`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                if (currentSymbol !== symbol || loadId !== aiFutureTrendLoadId) return;
                (data.items || []).forEach(batch => rememberFutureTrendBatch(batch, initialModels));
                const current = activeAIBatch(symbol)?.batch;
                if (current?.analysis_target === 'FUTURE_TREND') rememberFutureTrendBatch(current);
                const records = [...(aiFutureTrendModels.get(symbol)?.values() || [])];
                if (records.length) {
                    renderFutureTrendBatch(records[0].batch, null, records);
                    return;
                }
                box.textContent = `${symbol} · 暂无未来趋势预测，点击右上方按钮开始`;
            } catch (e) {
                if (currentSymbol === symbol && loadId === aiFutureTrendLoadId) box.textContent = '未来趋势读取失败：' + e.message;
            }
        }

        function renderAIBatch(batch, target = {}) {
            if (!target.panelId && batch.analysis_target === 'FUTURE_TREND') {
                if (batch.instrument === currentSymbol) renderFutureTrendBatch(batch, null, rememberFutureTrendBatch(batch));
                return;
            }
            const viewedDay = currentPeriod === 'intraday' ? intradayDate : null;
            if (!target.panelId && viewedDay && (batch.session_forecast?.trading_day || batch.trading_day) !== viewedDay) return;
            if (!target.panelId && !viewedDay && batch.analysis_target === 'CURRENT_SESSION'
                && batch.display_until && !aiSessionDisplayActive(batch)) {
                document.getElementById('ai-analysis-results').style.display = 'none';
                return;
            }
            if (!target.panelId && !target.preserveChart && !viewedDay && batch.analysis_target === 'CURRENT_SESSION') setAISessionChartBatch(batch);
            const panel = document.getElementById(target.panelId || 'ai-analysis-results');
            panel.style.display = 'block';
            const analysisTarget = ['TODAY','CURRENT_SESSION','NEXT_OPEN','FUTURE_TREND'].includes(batch.analysis_target) ? batch.analysis_target : 'TODAY';
            const targetLabel = analysisTarget === 'FUTURE_TREND' ? '未来趋势' : analysisTarget === 'NEXT_OPEN' ? '明日开盘'
                : analysisTarget === 'CURRENT_SESSION' ? '盘时段' : '今日走势';
            const sessionText = batch.session_target
                ? ` · ${batch.session_target.start_time.slice(11,16)}–${batch.session_target.end_time.slice(11,16)}` : '';
            document.getElementById(target.metaId || 'ai-batch-meta').textContent = `${batch.instrument} · ${targetLabel}${sessionText} · ${fmtTradingDay(batch.session_forecast?.trading_day || batch.trading_day)} · ${batch.bar_count}根 · ${batch.status}`;
            const requirements = document.getElementById(target.requirementsId || 'ai-batch-requirements');
            requirements.style.cssText = 'font-size:10px; color:var(--text-muted); background:#172033; border:1px solid #334155; border-radius:5px; padding:7px 8px; margin-bottom:7px; white-space:pre-wrap;';
            const reviewText = batch.use_review_summaries
                ? `已启用复盘总结；匹配${batch.matched_review_summary_count || 0}条，已发送${batch.used_review_summary_count || 0}条（各模型发送情况见卡片）`
                : '本次未启用复盘总结';
            requirements.textContent = `本次分析要求：${batch.analysis_requirements || '使用该模式默认要求'}\n复盘总结：${reviewText}`;
            const list = document.getElementById(target.listId || 'ai-result-list');
            list.replaceChildren();
            if (analysisTarget === 'FUTURE_TREND') appendAIFutureMaterials(list, batch);
            const comparisons = (batch.runs || []).filter(run => run.result);
            if (comparisons.length > 1) {
                const comparison = document.createElement('div');
                comparison.style.cssText = 'grid-column:1 / -1; font-size:11px; padding:8px; background:#172033;';
                const directions = new Set(comparisons.map(run => run.direction));
                comparison.textContent = analysisTarget === 'FUTURE_TREND'
                    ? '多模型分期限预测：' + comparisons.map(run => `${run.model_display_name} `
                        + (batch.future_targets || []).map(target => target.horizon_days).map(days => `${days}日 ${directionText(run.result.horizon_forecasts?.find(item => item.horizon_days === days)?.direction)}`).join(' / ')).join(' ｜ ')
                    : `${directions.size > 1 ? '多模型方向分歧' : '多模型方向一致'}：` + comparisons.map(run =>
                        `${run.model_display_name} ${analysisTarget === 'NEXT_OPEN' ? nextOpenDirectionText(run.direction) : directionText(run.direction)}`).join(' ｜ ');
                list.appendChild(comparison);
            }
            (batch.runs || []).forEach(run => {
                const card = document.createElement('div');
                card.className = 'ai-result-card';
                const head = document.createElement('div');
                head.className = 'ai-result-head';
                const name = document.createElement('strong'); name.textContent = run.model_display_name;
                const state = document.createElement('span'); state.style.cssText = 'font-size:10px; color:var(--text-muted);';
                state.textContent = `${run.status}${analysisTarget === 'CURRENT_SESSION' ? ` · ${aiSessionHistoryState(run)}` : ''}${run.legacy_compatible ? ' · 旧格式兼容展示' : ''}${run.execution_mode ? ` · ${run.execution_mode}` : ''}${run.call_count ? ` · ${run.call_count}次调用` : ''}`;
                head.append(name, state); card.appendChild(head);
                const audit = document.createElement('div');
                audit.style.cssText = 'font-size:10px; color:#93c5fd; margin:5px 0; white-space:pre-wrap;';
                const warnings = [...new Set([...(batch.quality?.warnings || []), ...(run.result?.data_quality?.warnings || [])])];
                const reviewState = {SUCCEEDED:'复核通过', FAILED_DRAFT_RETAINED:'复核失败，保留草稿', SKIPPED_DISAGREEMENT:'方向分歧，未复核选边', NOT_REQUESTED:'数据检查拦截，未请求模型'}[run.review_status]
                    || (run.status === 'UNSTRUCTURED' ? '结果校验失败，未进入复核' : '复核状态未记录');
                const vote = run.result?.consensus;
                const finalCalls = (run.review_summary_calls || []).filter(call => /^(SINGLE|SYNTHESIS|REVIEW)/.test(call.call_type));
                const omitted = batch.use_review_summaries && batch.planned_review_summary_count > 0
                    && finalCalls.length > 0 && finalCalls.every(call => !call.summary_ids.length);
                const volumeIssues = batch.quality?.unverified_volume_bars || [];
                const volumeDetail = volumeIssues.length
                    ? '\n分钟成交量归属未核实：' + volumeIssues.slice(0, 5).map(item =>
                        `${item.instrument || batch.instrument} ${item.period || '1m'} ${item.bar_time}（${item.volume_quality}）`).join('；')
                        + (volumeIssues.length > 5 ? `；共${volumeIssues.length}根` : '')
                    : '';
                audit.textContent = `行情截至：${batch.quote_time || '--'} · K线截至：${batch.quality?.last_closed_bar_time || '--'}\n`
                    + `数据状态：${batch.quality?.market_state === 'CLOSED' ? '正常休市（使用历史行情）' : '盘中'}${warnings.length ? ' · 警告：' + warnings.join('、') : ''}\n`
                    + reviewState + (run.review_error ? '：' + run.review_error : '')
                    + (vote ? vote.status === 'HORIZON_MAJORITY'
                        ? ` · 分期限复核：${Object.entries(vote.directions || {}).map(([days, direction]) => `${days}日 ${directionText(direction)}`).join(' / ')}（有效票${vote.valid}/${vote.total}）`
                        : ` · ${vote.status === 'DISAGREEMENT' ? '方向分歧' : '方向一致'} ${vote.agreed}/${vote.total}（有效票${vote.valid}）` : '')
                    + `\n复盘总结已发送：${(run.used_review_summary_ids || []).length}条`
                    + (omitted ? ' · 总结因上下文限制未发送' : '') + volumeDetail;
                card.appendChild(audit);

                if (run.result) {
                    let legacyNote = null;
                    if (run.legacy_compatible) {
                        legacyNote = document.createElement('div');
                        legacyNote.style.cssText = 'font-size:10px; color:#f59e0b; margin-top:4px;';
                        legacyNote.textContent = '旧记录采用兼容格式展示；原始响应和原审计状态未修改。';
                    }
                    if (analysisTarget === 'NEXT_OPEN') {
                        const forecast = run.result.next_open_forecast;
                        const forecastBox = document.createElement('div'); forecastBox.className = 'ai-open-forecast';
                        if (forecast) {
                        const forecastHead = document.createElement('div'); forecastHead.className = 'ai-open-forecast-head';
                        const targetText = document.createElement('span');
                        targetText.textContent = `${forecast.target_session === 'NIGHT' ? '下一交易日夜盘' : '下一交易日日盘'} ${batch.next_open_target?.start_time || forecast.open_time || '--'} 开盘`;
                        const prediction = document.createElement('span');
                        prediction.style.color = forecast.direction === 'UP' ? '#ef4444' : forecast.direction === 'DOWN' ? '#10b981' : '#f59e0b';
                        prediction.textContent = `${nextOpenDirectionText(forecast.direction)} · 证据完整度 ${forecast.confidence_score ?? '--'}/100`;
                        const forecastSummary = document.createElement('div'); forecastSummary.className = 'ai-open-forecast-summary';
                        forecastSummary.textContent = forecast.summary || '模型未提供预测说明';
                        const basis = document.createElement('div'); basis.style.cssText = 'font-size:10px; color:var(--text-muted); margin-top:5px;';
                        basis.style.whiteSpace = 'pre-wrap';
                        basis.textContent = '依据：\n' + (forecast.basis || []).map(item => typeof item === 'string' ? item : `${item.side} · ${item.time} · ${item.field}=${item.value}：${item.reason}`).join('\n');
                        const risks = document.createElement('div'); risks.style.cssText = 'font-size:10px; color:#f59e0b; margin-top:4px;';
                        risks.textContent = `风险：${JSON.stringify(forecast.risk_factors || [])}`;
                        forecastHead.append(targetText, prediction); forecastBox.append(forecastHead, forecastSummary, basis, risks);
                        } else {
                            forecastBox.textContent = '模型未返回下一交易日开盘预测';
                        }
                        if (legacyNote) card.appendChild(legacyNote);
                        card.appendChild(forecastBox);
                    } else {
                        const title = document.createElement('div');
                        title.style.cssText = `font-weight:bold; color:${run.direction === 'LONG' ? '#ef4444' : run.direction === 'SHORT' ? '#10b981' : '#f59e0b'};`;
                        title.textContent = `${directionText(run.direction)} · 证据完整度 ${run.confidence_score}/100`;
                        const summary = document.createElement('div'); summary.style.cssText = 'font-size:11px; margin-top:4px;'; summary.textContent = run.result.summary || '';
                        const levels = document.createElement('div'); levels.style.cssText = 'font-size:10px; color:var(--text-muted); margin-top:4px;';
                        levels.textContent = `支撑 ${JSON.stringify(run.result.support_levels || [])} · 压力 ${JSON.stringify(run.result.resistance_levels || [])}`;
                        const conditions = document.createElement('div'); conditions.style.cssText = 'font-size:10px; color:#cbd5e1; margin-top:4px;';
                        conditions.textContent = `条件：${run.result.entry_condition || '--'}；失效：${run.result.invalidation_condition || '--'}`;
                        card.append(title, summary);
                        if (legacyNote) card.appendChild(legacyNote);
                        const evidence = document.createElement('div');
                        evidence.style.cssText = 'font-size:11px; margin-top:6px; white-space:pre-wrap;';
                        evidence.textContent = '多空依据：\n' + (run.result.evidence || []).map(item => typeof item === 'string' ? item
                            : `${{LONG:'看多', SHORT:'看空', CONTEXT:'背景'}[item.side] || '依据'} · ${item.time} · ${item.field}=${item.value}：${item.reason}`).join('\n');
                        const risks = document.createElement('div');
                        risks.style.cssText = 'font-size:11px; color:#f59e0b; margin-top:5px; white-space:pre-wrap;';
                        risks.textContent = '风险：' + (run.result.risks || []).join('；');
                        card.append(evidence, risks, levels, conditions);
                        if (analysisTarget === 'FUTURE_TREND') appendAIFutureResult(card, run, batch);
                        if (analysisTarget === 'CURRENT_SESSION' && run.result.session_chart) {
                            const plan = run.result.session_chart;
                            const chartLine = document.createElement('div');
                            chartLine.style.cssText = 'font-size:10px; color:#c084fc; margin-top:5px;';
                            chartLine.textContent = `分时画线：${String(plan.forecast_start_time).replace('T',' ').slice(5,16)}–${String(plan.forecast_end_time).replace('T',' ').slice(11,16)} · 基准 ${plan.reference_price}`
                                + (plan.target_price == null ? '' : ` · 目标 ${plan.target_price}`)
                                + (plan.invalidation_price == null ? '' : ` · 失效 ${plan.invalidation_price}`)
                                + (plan.trigger_price == null ? '' : ` · 触发 ${plan.trigger_price}`);
                            card.appendChild(chartLine);
                        }
                    }
                } else {
                    const msg = document.createElement('div'); msg.style.cssText = 'font-size:11px; color:var(--text-muted);';
                    msg.textContent = aiAnalysisErrorText(run.error) || (run.status === 'PENDING' ? '等待执行...' : run.status === 'RUNNING' ? '模型分析中...' : '暂无结构化结果');
                    card.appendChild(msg);
                }
                if (target.allowEvaluation && run.result && ['SUCCEEDED','UNSTRUCTURED'].includes(run.status)) {
                    appendAIEvaluation(card, run, analysisTarget);
                    if (run.evaluation) appendAIReviewSummary(card, run, analysisTarget);
                }
                if (target.allowEvaluation && ['SUCCEEDED','UNSTRUCTURED'].includes(run.status)) {
                    appendAIConversation(card, run);
                }
                if (['SUCCEEDED','UNSTRUCTURED','HTTP_ERROR','TIMEOUT'].includes(run.status)) {
                    const rawButton = document.createElement('button'); rawButton.className = 'ai-action'; rawButton.style.marginTop = '6px'; rawButton.textContent = '查看本地原始记录';
                    rawButton.onclick = () => showAIRaw(run.run_id, card, rawButton);
                    card.appendChild(rawButton);
                }
                list.appendChild(card);
            });
        }

        async function showAIRaw(runId, card, button) {
            button.disabled = true;
            try {
                const res = await fetch(`/api/v1/ai/analysis/runs/${runId}?include_raw=true`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                const pre = document.createElement('pre');
                pre.className = 'ai-raw-record';
                pre.textContent = (data.calls || []).map(call => `#${call.sequence_no} ${call.call_type}\n请求:\n${JSON.stringify(call.request, null, 2)}\n响应:\n${call.raw_response_text || call.error || ''}`).join('\n\n');
                card.appendChild(pre); button.remove();
            } catch (e) { alert(e.message); button.disabled = false; }
        }

        let aiHomeRefreshTimer = null;
        let aiHomeRefreshFlight = null;
        let aiHomeRefreshSequence = 0;
        let aiHomeRefreshFingerprint = null;

        function aiHomeViewKey() {
            const owner = typeof currentUser === 'undefined' ? null : currentUser;
            return JSON.stringify([owner, isLiveMode, currentSymbol, currentPeriod,
                currentPeriod === 'intraday' ? intradayDate : null]);
        }

        function startAIHomeAutoRefresh() {
            if (aiHomeRefreshTimer !== null) return;
            aiHomeRefreshTimer = setInterval(refreshAIHomeResults, 2000);
            document.addEventListener('visibilitychange', () => {
                if (!document.hidden) refreshAIHomeResults();
            });
        }

        async function refreshAIHomeResults() {
            if (document.hidden || isLiveMode || !currentSymbol) return;
            const key = aiHomeViewKey();
            if (aiHomeRefreshFlight?.key === key) return;
            const flight = {key};
            aiHomeRefreshFlight = flight;
            try { await loadLatestAIResult(currentSymbol, {background: true}); }
            finally { if (aiHomeRefreshFlight === flight) aiHomeRefreshFlight = null; }
        }

        async function loadLatestAIResult(symbol, {background = false} = {}) {
            if (isLiveMode || !symbol) return;
            const viewedDay = currentPeriod === 'intraday' ? intradayDate : null;
            const key = aiHomeViewKey();
            const sequence = ++aiHomeRefreshSequence;
            const panel = document.getElementById('ai-analysis-results');
            if (!background) {
                aiHomeRefreshFingerprint = null;
                panel.style.display = 'none';
                clearAISessionChartLines();
            }
            const active = activeAIBatch(symbol);
            const visibleActive = active && active.batch.analysis_target !== 'FUTURE_TREND' && (!viewedDay || (active.batch.session_forecast?.trading_day || active.batch.trading_day) === viewedDay);
            if (!background && visibleActive && currentSymbol === symbol) renderAIBatch(active.batch);
            try {
                // 盘时段结果可跨交易日保留至下一次开盘前五分钟。
                const dateParam = viewedDay ? `&trading_day=${encodeURIComponent(viewedDay)}` : '';
                const options = {cache: 'no-store'};
                if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') options.signal = AbortSignal.timeout(10000);
                const res = await fetch(`/api/v1/ai/analysis/history?instrument=${encodeURIComponent(symbol)}&page_size=100${dateParam}`, options);
                if (!res.ok) return;
                const data = await res.json();
                const items = data.items || [];
                if (sequence !== aiHomeRefreshSequence || key !== aiHomeViewKey() || currentSymbol !== symbol) return;
                const latest = visibleActive ? active.batch : items.find(batch => batch.analysis_target !== 'FUTURE_TREND' && (viewedDay ? (batch.session_forecast?.trading_day || batch.trading_day) === viewedDay
                        : batch.analysis_target === 'CURRENT_SESSION'
                            ? aiSessionDisplayActive(batch) : batch.trading_day === data.current_trading_day));
                const fingerprint = JSON.stringify([key, data, latest, items.map(batch =>
                    batch.analysis_target === 'CURRENT_SESSION' ? aiSessionDisplayActive(batch) : null)]);
                if (background && fingerprint === aiHomeRefreshFingerprint) return;
                if (background && panel.contains?.(document.activeElement)
                    && ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
                const scrollPanel = panel.closest('.right-panel');
                const scrollTop = scrollPanel?.scrollTop;
                if (background || !visibleActive) {
                    if (latest) renderAIBatch(latest, {preserveChart: background});
                    else if (viewedDay) {
                        document.getElementById('ai-analysis-results').style.display = 'block';
                        document.getElementById('ai-batch-meta').textContent = `${symbol} · ${fmtTradingDay(viewedDay)}`;
                        document.getElementById('ai-batch-requirements').textContent = '';
                        document.getElementById('ai-result-list').replaceChildren();
                        document.getElementById('ai-result-list').textContent = '该交易日暂无 AI 分析记录';
                    }
                    else document.getElementById('ai-analysis-results').style.display = 'none';
                }
                setAISessionChartBatches(items, null, background);
                if (background && scrollPanel) scrollPanel.scrollTop = scrollTop;
                aiHomeRefreshFingerprint = fingerprint;
            } catch (e) {}
        }

