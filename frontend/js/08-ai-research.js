        function aiResearchNode(tag, text) {
            const node = document.createElement(tag);
            if (text != null) node.textContent = text;
            return node;
        }

        function aiResearchContractCode(value) {
            const code = String(value || '').trim().toUpperCase();
            return /^[A-Z]{1,3}\d{3,4}$/.test(code) ? code : null;
        }

        async function openAIResearchModal() {
            if (isLiveMode) return;
            document.getElementById('ai-research-modal').style.display = 'flex';
            document.getElementById('ai-research-message').textContent = '正在读取设置…';
            try {
                const models = await fetchAIModels();
                const select = document.getElementById('ai-research-model');
                select.replaceChildren();
                const empty = aiResearchNode('option', '请选择独立研究模型'); empty.value = ''; select.appendChild(empty);
                models.forEach(model => {
                    const option = aiResearchNode('option', `${model.display_name} · ${model.model_name}${!model.enabled || !model.available ? ' · ' + (model.unavailable_reason || '未启用或不可用') : ''}`);
                    option.value = model.model_config_id; option.disabled = !model.enabled || !model.available;
                    select.appendChild(option);
                });
                await reloadAIResearchSettings();
            } catch (e) { document.getElementById('ai-research-message').textContent = e.message; }
        }

        function closeAIResearchModal() {
            document.getElementById('ai-research-modal').style.display = 'none';
            clearTimeout(aiResearchPollTimer);
            clearTimeout(aiResearchSearchTimer);
            ++aiResearchSearchId;
        }

        async function reloadAIResearchSettings() {
            try {
                const res = await fetch('/api/v1/ai/research/settings');
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                aiResearchSettings = await res.json();
                aiResearchDraft = JSON.parse(JSON.stringify(aiResearchSettings));
                aiResearchDraft.contracts = aiResearchDraft.contracts || [];
                document.getElementById('ai-research-enabled').checked = !!aiResearchDraft.enabled;
                document.getElementById('ai-research-ai-enabled').checked = !!aiResearchDraft.ai_enabled;
                document.getElementById('ai-research-model').value = aiResearchDraft.model_config_id || '';
                document.getElementById('ai-research-interval').value = String(aiResearchDraft.interval_minutes || 60);
                document.getElementById('ai-research-search-results').replaceChildren();
                document.getElementById('ai-research-message').textContent = '';
                previewAIResearchSettings();
                await loadAIResearchStatus(currentSymbol);
            } catch (e) { document.getElementById('ai-research-message').textContent = '读取设置失败：' + e.message; }
        }

        function previewAIResearchSettings() {
            if (!aiResearchDraft) return;
            aiResearchDraft.enabled = document.getElementById('ai-research-enabled').checked;
            aiResearchDraft.ai_enabled = document.getElementById('ai-research-ai-enabled').checked;
            aiResearchDraft.model_config_id = document.getElementById('ai-research-model').value || null;
            aiResearchDraft.interval_minutes = Number(document.getElementById('ai-research-interval').value);
            const products = new Set(aiResearchDraft.contracts.map(item => item.instrument.replace(/\d+$/, '')));
            document.getElementById('ai-research-preview').textContent = `配置预览：${aiResearchDraft.contracts.length} 个目标合约，去重后 ${products.size} 个品种及公共宏观，每 ${aiResearchDraft.interval_minutes} 分钟检查。${aiResearchDraft.ai_enabled ? 'AI 辅助已开启：新增复杂正文提取与按冷却补查会用所选模型。' : 'AI 辅助关闭：固定采集不产生任何模型调用。'}保存后生效；预览不会启动任务。`;
            const box = document.getElementById('ai-research-contracts');
            box.style.cssText = 'display:grid; grid-template-columns:repeat(auto-fill, minmax(340px, 1fr)); gap:6px;';
            box.replaceChildren();
            aiResearchDraft.contracts.forEach(item => {
                const row = aiResearchNode('div'); row.className = 'ai-model-row';
                row.style.cssText = 'display:flex; align-items:center; gap:8px; margin:0; padding:7px 9px;';
                const title = aiResearchNode('strong', item.instrument);
                const saved = aiResearchSettings?.contracts?.some(value => value.instrument === item.instrument);
                const overview = aiResearchStatus?.contracts?.find(value => value.instrument === item.instrument);
                const stateText = saved ? ({AVAILABLE:'可用',PARTIAL:'部分可用',MISSING:'资料缺失',EMPTY:'空缓存',NOT_CONFIGURED:'目标未配置'}[overview?.cache_state || overview?.status] || '请查看下方已保存采集状态') : '未保存，仅预览';
                const state = aiResearchNode('span', stateText);
                state.style.cssText = 'flex:1; min-width:0; font-size:10px; color:var(--text-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;';
                state.title = '资料状态：' + stateText;
                const actions = aiResearchNode('div'); actions.style.cssText = 'display:flex; gap:5px; flex:none;';
                const refresh = aiResearchNode('button', '更新该合约'); refresh.className = 'ai-action'; refresh.disabled = !saved; refresh.onclick = () => refreshAIResearch(item.instrument);
                refresh.title = '按已保存的目标合约执行；预览中的改动需先保存';
                const remove = aiResearchNode('button', '移除'); remove.className = 'ai-action'; remove.onclick = () => {
                    aiResearchDraft.contracts = aiResearchDraft.contracts.filter(value => value.instrument !== item.instrument);
                    previewAIResearchSettings();
                };
                actions.append(refresh, remove); row.append(title, state, actions); box.appendChild(row);
            });
        }

        function addAIResearchContracts(codes) {
            if (!aiResearchDraft) return;
            codes.forEach(value => {
                const instrument = aiResearchContractCode(value);
                if (instrument && !aiResearchDraft.contracts.some(item => item.instrument === instrument)) aiResearchDraft.contracts.push({instrument, related:[]});
            });
            previewAIResearchSettings();
        }

        function importAIResearchWatchlist() {
            addAIResearchContracts(watchlist.map(item => item.symbol));
            document.getElementById('ai-research-message').textContent = '已一次性复制具体月份自选合约。后续修改自选不会改变此名单，保存后生效。';
        }

        async function openAIMaterialEntryModal() {
            if (isLiveMode) return;
            if (!aiResearchSettings?.contracts) {
                // 可能未经采集弹窗直接打开: 先拉取已保存设置
                try {
                    const res = await fetch('/api/v1/ai/research/settings');
                    if (res.ok) aiResearchSettings = await res.json();
                } catch (e) { /* 读取失败时目标退化为公共宏观 */ }
            }
            const contracts = aiResearchSettings?.contracts || aiResearchDraft?.contracts || [];
            const names = await aiInstrumentNames(contracts);
            const target = document.getElementById('ai-entry-target');
            target.replaceChildren();
            contracts.forEach(item => {
                const cname = (names[item.instrument] || '').replace(/\d+$/, '');
                const option = document.createElement('option');
                option.value = item.instrument;
                option.textContent = item.instrument + (cname ? ' · ' + cname : '');
                target.appendChild(option);
            });
            const macro = document.createElement('option');
            macro.value = 'macro'; macro.textContent = '公共宏观';
            target.appendChild(macro);
            onAIMaterialEntryTargetChange();
            document.getElementById('ai-entry-message').textContent = '';
            document.getElementById('ai-material-entry-modal').style.display = 'flex';
        }

        function onAIMaterialEntryTargetChange() {
            const target = document.getElementById('ai-entry-target').value;
            document.getElementById('ai-entry-dimension').innerHTML = target === 'macro'
                ? '<option value="macro">宏观（统计局/央行等公共数据）</option>'
                : '<option value="fundamentals">基本面（品种自身供需库存）</option>'
                  + '<option value="events">事件（政策/地缘/突发）</option>'
                  + '<option value="related_markets">相关市场（外部市场资讯）</option>';
        }

        function aiMaterialPeriodValue() {
            // 月份选择器值为 YYYY-MM, 转成资料期惯用的 "YYYY年M月"; 手输的其他格式原样保留
            const value = document.getElementById('ai-entry-period').value.trim();
            if (!/^\d{4}-\d{2}$/.test(value)) return value || null;
            return `${value.slice(0, 4)}年${parseInt(value.slice(5), 10)}月`;
        }

        function closeAIMaterialEntryModal() {
            document.getElementById('ai-material-entry-modal').style.display = 'none';
        }

        async function submitAIMaterialEntry() {
            const message = document.getElementById('ai-entry-message');
            const record = {
                target: document.getElementById('ai-entry-target').value,
                dimension: document.getElementById('ai-entry-dimension').value,
                title: document.getElementById('ai-entry-title').value.trim(),
                body: document.getElementById('ai-entry-body').value.trim(),
                url: document.getElementById('ai-entry-url').value.trim() || null,
                source_name: document.getElementById('ai-entry-source').value.trim() || null,
                published_at: document.getElementById('ai-entry-published').value || null,
                data_period: aiMaterialPeriodValue(),
            };
            if (!record.title || !record.body) { message.textContent = '标题与正文必填。'; return; }
            try {
                const res = await fetch('/api/v1/ai/research/materials/manual', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({records: [record]})});
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                ['ai-entry-title', 'ai-entry-body', 'ai-entry-url', 'ai-entry-source', 'ai-entry-period'].forEach(
                    id => document.getElementById(id).value = '');
                document.getElementById('ai-entry-published').value = '';
                closeAIMaterialEntryModal();
                const note = `录入完成：入库 ${data.inserted} 条${data.duplicates ? `，重复跳过 ${data.duplicates} 条` : ''}。`;
                document.getElementById('ai-research-message').textContent = note;
                if (document.getElementById('ai-materials-modal').style.display !== 'none') {
                    document.getElementById('ai-materials-message').textContent = note;
                    loadAIMaterials();
                }
            } catch (e) { message.textContent = '入库失败：' + e.message; }
        }

        function aiMaterialsQueryParams(extra) {
            const params = new URLSearchParams(extra || {});
            const topic = document.getElementById('ai-materials-topic').value;
            const dimension = document.getElementById('ai-materials-dimension').value;
            const method = document.getElementById('ai-materials-method').value;
            const keyword = document.getElementById('ai-materials-keyword').value.trim();
            if (topic) params.set('topic', topic);
            if (dimension) params.set('dimension', dimension);
            if (method) params.set('method', method);
            if (keyword) params.set('q', keyword);
            const instrument = document.getElementById('ai-materials-instrument').value;
            if (instrument) params.set('instrument', instrument);
            return params;
        }

        async function exportAIMaterials() {
            const message = document.getElementById('ai-materials-message');
            try {
                const params = aiMaterialsQueryParams({limit: '500'});
                const res = await fetch('/api/v1/ai/research/materials/export?' + params);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                if (!data.records || !data.records.length) { message.textContent = '没有可导出的资料。'; return; }
                const blob = new Blob([JSON.stringify(data.records, null, 2)], {type: 'application/json'});
                const link = document.createElement('a');
                link.href = URL.createObjectURL(blob);
                link.download = `资料导出_${new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '')}.json`;
                link.click();
                URL.revokeObjectURL(link.href);
                message.textContent = `已导出 ${data.exported} 条（当前筛选共 ${data.total} 条，单次上限 ${data.limit} 条）。`;
            } catch (e) { message.textContent = '导出失败：' + e.message; }
        }

        function importAIMaterialsFile(input) {
            const file = input.files && input.files[0];
            input.value = '';
            if (!file) return;
            const message = document.getElementById('ai-research-message');
            const reader = new FileReader();
            reader.onload = async () => {
                try {
                    const parsed = JSON.parse(reader.result);
                    const records = Array.isArray(parsed) ? parsed : [parsed];
                    if (!records.length) throw new Error('文件中没有资料记录');
                    const contracts = aiResearchSettings?.contracts || [];
                    records.forEach(record => {
                        if (!record || !record.dimension || !record.title || !record.body)
                            throw new Error('每条记录需要 dimension / title / body 字段');
                        if (!record.target) {
                            const product = String(record.product_code || String(record.target || '').replace(/\d+$/, '')).replace(/\d+$/, '');
                            const hit = contracts.find(c => c.instrument.replace(/\d+$/, '') === product);
                            if (hit) record.target = hit.instrument;
                        }
                        if (!record.target) throw new Error(`记录「${record.title}」缺少 target，且无法按品种 ${record.product_code || '未知'} 从已保存合约解析`);
                    });
                    const res = await fetch('/api/v1/ai/research/materials/manual', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({records})});
                    if (!res.ok) throw new Error(await aiErrorMessage(res));
                    const data = await res.json();
                    message.textContent = `导入完成：入库 ${data.inserted} 条，重复跳过 ${data.duplicates} 条（共 ${records.length} 条）。`;
                    loadAIResearchStatus(currentSymbol).catch(() => {});
                } catch (e) { message.textContent = '导入失败：' + e.message; }
            };
            reader.readAsText(file, 'utf-8');
        }

        function scheduleAIResearchSearch() {
            clearTimeout(aiResearchSearchTimer);
            aiResearchSearchTimer = setTimeout(searchAIResearchContracts, 250);
        }

        async function searchAIResearchContracts() {
            const query = document.getElementById('ai-research-search').value.trim();
            const box = document.getElementById('ai-research-search-results');
            const searchId = ++aiResearchSearchId;
            box.replaceChildren();
            if (!query) return;
            try {
                const res = await fetch(`/api/v1/universe?search=${encodeURIComponent(query)}&limit=60`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                if (searchId !== aiResearchSearchId) return;
                const selected = new Set();
                (data.contracts || []).filter(item => aiResearchContractCode(item.symbol)).forEach(item => {
                    const code = aiResearchContractCode(item.symbol);
                    const label = aiResearchNode('label'); label.style.cssText = 'display:block; font-size:11px; padding:4px;';
                    const check = aiResearchNode('input'); check.type = 'checkbox'; check.value = code; check.disabled = !!item.expired;
                    check.onchange = () => { if (check.checked) selected.add(code); else selected.delete(code); };
                    const text = aiResearchNode('span', ` ${code} · ${item.name || ''}${item.expired ? ' · 已到期，请更换' : ' · 期限按预测时核验'}`);
                    label.append(check, text); box.appendChild(label);
                });
                if (!box.children.length) { box.textContent = '没有匹配的具体月份合约。'; return; }
                const add = aiResearchNode('button', '添加所选采集合约'); add.className = 'ai-action';
                add.onclick = () => addAIResearchContracts([...selected]); box.appendChild(add);
            } catch (e) { if (searchId === aiResearchSearchId) box.textContent = '搜索失败：' + e.message; }
        }

        async function saveAIResearchSettings() {
            if (!aiResearchDraft) return;
            previewAIResearchSettings();
            const message = document.getElementById('ai-research-message');
            if (aiResearchDraft.enabled && !aiResearchDraft.contracts.length) {
                message.textContent = '开启定时采集前必须添加至少一个目标合约；固定采集不要求研究模型。'; return;
            }
            if (aiResearchDraft.ai_enabled && !aiResearchDraft.model_config_id) {
                message.textContent = '开启 AI 辅助研究前必须选择研究模型；关闭 AI 后可仅保存固定采集。'; return;
            }
            const button = document.getElementById('ai-research-save'); button.disabled = true;
            try {
                const res = await fetch('/api/v1/ai/research/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
                    enabled:aiResearchDraft.enabled, ai_enabled:aiResearchDraft.ai_enabled, model_config_id:aiResearchDraft.model_config_id,
                    interval_minutes:aiResearchDraft.interval_minutes, contracts:aiResearchDraft.contracts, config_version:aiResearchSettings.config_version
                })});
                if (res.status === 409) { message.textContent = '配置版本冲突，请点击“刷新已保存设置”后重新编辑；预览尚未保存。'; return; }
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                aiResearchSettings = await res.json(); aiResearchDraft = JSON.parse(JSON.stringify(aiResearchSettings));
                previewAIResearchSettings(); await loadAIResearchStatus(currentSymbol);
                message.textContent = `设置已保存，版本 ${aiResearchSettings.config_version}。${aiResearchSettings.enabled ? '按设置间隔检查' : '定时关闭，可手动更新'}；${aiResearchSettings.ai_enabled ? 'AI 辅助开启' : 'AI 辅助关闭，模型调用为 0'}。`;
            } catch (e) { message.textContent = '保存失败：' + e.message; }
            finally { button.disabled = false; }
        }

        async function refreshAIResearch(instrument = null) {
            const message = document.getElementById('ai-research-message');
            if (!aiResearchSettings?.contracts?.length || (instrument && !aiResearchSettings.contracts.some(item => item.instrument === instrument))) {
                message.textContent = '请先保存采集范围；未保存配置只作预览，不会启动任务。'; return;
            }
            try {
                const res = await fetch('/api/v1/ai/research/refresh', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(instrument ? {instrument} : {})});
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                message.textContent = '已按保存的配置提交更新；相同主题会复用已有任务。更新结束后需重新点击预测，新批次才使用新资料。';
                await loadAIResearchStatus(currentSymbol);
            } catch (e) { message.textContent = '更新失败：' + e.message; }
        }

        async function loadAIResearchStatus(instrument = currentSymbol) {
            const requestId = ++aiResearchStatusId;
            const modalOpen = document.getElementById('ai-research-modal').style.display === 'flex';
            const params = new URLSearchParams();
            if (instrument) params.set('instrument', instrument);
            if (modalOpen) params.set('all_contracts', 'true');
            const res = await fetch('/api/v1/ai/research/status?' + params);
            if (!res.ok) throw new Error(await aiErrorMessage(res));
            const data = await res.json();
            if (requestId !== aiResearchStatusId || instrument !== currentSymbol) return;
            aiResearchStatus = data;
            ['ai-future-cache-status', 'ai-research-home-status'].forEach(id => {
                const box = document.getElementById(id); if (!box) return;
                box.replaceChildren(); appendAIResearchCacheStatus(box, data, instrument);
                if (id === 'ai-future-cache-status') box.style.display = typeof aiFutureMode === 'undefined' || aiFutureMode ? 'block' : 'none';
            });
            let modalStatus = data;
            if (modalOpen) {
                aiResearchStatus = modalStatus;
                renderAIResearchStatus(modalStatus);
                previewAIResearchSettings();
            }
            clearTimeout(aiResearchPollTimer);
            if (document.getElementById('ai-research-modal').style.display === 'flex') aiResearchPollTimer = setTimeout(() => {
                loadAIResearchStatus(currentSymbol).catch(e => { document.getElementById('ai-research-message').textContent = '状态读取失败：' + e.message; });
            }, 5000);
        }

        function appendAIResearchCacheStatus(box, data, instrument) {
            const materials = data.materials || {};
            const cache = {AVAILABLE:'可用', PARTIAL:'部分可用', EMPTY:'空缓存', MISSING:'资料缺失', NOT_CONFIGURED:'目标未配置', STALE:'过期', FAILED:'上次采集失败'}[materials.cache_state || materials.status] || materials.cache_state || materials.status || '尚无可用资料';
            const time = value => String(value || '').replace('T', ' ').slice(0, 19) || '未记录';
            box.appendChild(aiResearchNode('div', `${instrument || ''} · 资料：${cache} · 最近检查 ${time(data.last_check_at || materials.last_check_at)} · 最近成功 ${time(data.last_success_at || materials.last_success_at)} · 下次检查 ${time(data.next_check_at || materials.next_check_at)}`));
            const failed = (data.topics || []).some(topic => ['FAILED','TIMEOUT','INTERRUPTED'].includes(topic.status));
            const running = (data.topics || []).some(topic => ['PENDING','QUEUED','RUNNING'].includes(topic.status));
            const aiState = !data.settings?.ai_enabled ? 'AI辅助关闭'
                : data.settings?.pause_reason ? `AI暂停：${data.settings.pause_reason}`
                : data.model_status?.available === false ? `AI暂停：${data.model_status.reason || '研究模型不可用'}`
                : 'AI辅助开启';
            box.appendChild(aiResearchNode('div', `采集：${failed ? '上次采集失败，有效缓存仍可使用' : running ? '更新中，仅使用已发布缓存' : data.settings?.enabled ? '按间隔检查' : '定时关闭'} · ${aiState}`));
            const colors = {fundamentals:'#22d3ee',macro:'#fb923c',events:'#fbbf24',related_markets:'#818cf8'};
            const labels = {fundamentals:'基本面',macro:'宏观',events:'事件',related_markets:'相关市场'};
            const tags = aiResearchNode('div'); tags.style.cssText = 'display:flex; gap:8px; flex-wrap:wrap; margin:5px 0;';
            Object.keys(labels).forEach(key => {
                const sources = (materials.sources || []).filter(source => source.dimension === key && !source.stale);
                const stale = (materials.background_sources || []).some(source => source.dimension === key) || (materials.sources || []).some(source => source.dimension === key && source.stale);
                const declared = data.contracts?.find(item => item.instrument === instrument)?.dimensions?.[key];
                const warning = ['UNVERIFIED','EXPIRED','STALE'].includes(declared);
                const state = warning ? key === 'macro' && declared === 'UNVERIFIED' ? '最新发布待核验' : '过期/待核验'
                    : sources.length ? (materials.coverage_gaps || []).includes(key) ? '部分可用（覆盖不足）' : '可用' : stale ? key === 'macro' ? '最新发布待核验' : '过期/待核验' : '缺失';
                const span = aiResearchNode('span', `${labels[key]}：${state}`); span.style.color = warning ? '#f59e0b' : sources.length ? colors[key] : stale ? '#f59e0b' : '#9ca3af'; tags.appendChild(span);
            });
            box.appendChild(tags);
            const product = String(instrument || '').replace(/\d+$/, '');
            const configured = data.settings?.contracts?.find(item => item.instrument === instrument)
                || data.settings?.contracts?.find(item => item.instrument.replace(/\d+$/, '') === product);
            if (configured && configured.instrument !== instrument) {
                box.appendChild(aiResearchNode('div', `共享 ${product} 品种资料；具体月份资料仍只用于对应合约。`));
            }
            const lifecycle = [materials.contract_status].filter(Boolean);
            lifecycle.forEach(item => {
                if (item.expired || item.status === 'EXPIRED' || item.status === 'UNKNOWN') box.appendChild(aiResearchNode('div', `${item.instrument || item.symbol || instrument} · ${item.expired || item.status === 'EXPIRED' ? '已到期，请更换；跳过引用' : '期限未核实'}`));
            });
            box.appendChild(aiResearchNode('div', '预测只读取并冻结当前有效缓存；空缓存、未配置、更新中或上次失败均不会启动搜索或等待采集。'));
            if (data.settings?.ai_enabled && data.model_status?.available === false) {
                const detail = aiResearchNode('details'); detail.appendChild(aiResearchNode('summary', '展开研究模型不可用原因'));
                detail.appendChild(aiResearchNode('div', data.model_status.reason || '未配置或不可用（仅影响 AI 阶段）')); box.appendChild(detail);
            }
            const update = aiResearchNode('button', '立即更新资料'); update.className = 'ai-action'; update.onclick = async () => {
                if (!aiResearchSettings) aiResearchSettings = data.settings;
                if (configured) await refreshAIResearch(configured.instrument); else await openAIResearchModal();
            }; box.appendChild(update);
        }

        function renderAIResearchStatus(data) {
            const box = document.getElementById('ai-research-status'); box.replaceChildren();
            (data.channel_policy || []).filter(item => !item.enabled).forEach(item =>
                box.appendChild(aiResearchNode('div', `已停止自动请求：${item.channel_id} · ${item.reason}`)));
            const distribution = data.source_distribution;
            if (distribution) {
                const share = value => value?.eastmoney_percent == null ? '无新增，比例不适用' : `${value.eastmoney_percent}%（${value.total} 条）`;
                box.appendChild(aiResearchNode('div', '本账户全部资料东财占比 · 历史版本：' + share(distribution.historical_versions) + ' · 按网址去重的最新存储正文：' + share(distribution.latest_stored_documents)));
                box.appendChild(aiResearchNode('div', '存储占比包含背景/过期资料；不代表当前有效冻结资料的占比。'));
                (distribution.recent_jobs || []).forEach(job => box.appendChild(aiResearchNode('div', `${job.topic} · ${job.completed_at || '--'} · 新增版本东财占比：${share(job)}`)));
            }
            (data.contracts || []).forEach(contract => {
                const gaps = (contract.coverage_gaps || []).map(d => aiDimensionLabels[d] || d);
                box.appendChild(aiResearchNode('div', `${contract.instrument} · ${gaps.length ? '覆盖不足：' + gaps.join('、') : '四类资料已覆盖'}`));
                const missing = (contract.fundamental_coverage || []).filter(item => item.status !== 'AVAILABLE');
                if (missing.length) box.appendChild(aiResearchNode('div', '自身基本面待补：' + missing.map(item => item.metric).join('、')));
                (contract.fundamental_coverage || []).filter(item => item.status === 'AVAILABLE').forEach(item => {
                    const detail = aiResearchNode('details'); detail.appendChild(aiResearchNode('summary', '基本面资料已取得：' + item.metric));
                    (item.evidence || []).forEach(evidence => detail.appendChild(aiResearchNode('div', `原文统计期：${evidence.data_period || 'UNKNOWN'} · 发布：${evidence.published_at || 'UNKNOWN'}\n${evidence.quote || ''}\n${evidence.url || ''}`)));
                    box.appendChild(detail);
                });
            });
            box.appendChild(aiResearchNode('div', '任务完成只表示检查结束；资料有效性和覆盖是否充分分别判断。'));
            box.appendChild(aiResearchNode('div', `已保存采集：${data.settings?.enabled ? '定时开启' : '定时关闭'} · AI 辅助：${data.settings?.ai_enabled ? '开启' : '关闭（模型调用为 0）'} · 研究模型：${data.model_status?.available ? '可用' : '不可用，仅影响 AI 阶段'}`));
            const modelDetails = aiResearchNode('details'); const title = aiResearchNode('summary', '展开研究模型状态及原因'); modelDetails.appendChild(title);
            modelDetails.appendChild(aiResearchNode('div', data.model_status?.reason || '研究模型配置有效')); box.appendChild(modelDetails);
            const stageLabel = {COMPLETED:'完成', PARTIAL:'部分完成', FAILED:'失败', DISABLED:'关闭',
                SKIPPED_NO_TRIGGER:'未触发', SKIPPED_COOLDOWN:'冷却中', SKIPPED_BUDGET:'预算跳过', PAUSED_MODEL:'暂停'};
            (data.topics || []).forEach(topic => {
                let state = aiResearchTopicViews.get(topic.topic);
                if (!state || state.status !== topic.status || state.jobId !== topic.job_id) { state = {status:topic.status,jobId:topic.job_id,open:false}; aiResearchTopicViews.set(topic.topic,state); }
                const details = aiResearchNode('details'); details.dataset.researchTopic = topic.topic; details.open = state.open;
                details.ontoggle = () => { if (state.status === topic.status) state.open = details.open; };
                const status = topic.outcome === 'NO_NEW_MATERIAL' ? '检查完成，无新增' : {PENDING:'排队中',QUEUED:'排队中',RUNNING:'采集中',COMPLETED:'检查完成',PARTIAL:'部分完成',NO_NEW:'检查完成，无新增',FAILED:'采集失败',INTERRUPTED:'已中断',TIMEOUT:'超时'}[topic.status] || topic.status;
                details.appendChild(aiResearchNode('summary', `${topic.topic} · ${status} · 最近检查 ${topic.completed_at || '--'} · 下次 ${topic.next_check_at || '--'}`));
                const research = topic.research || {};
                const pending = (research.sources_checked || []).filter(s => s.status === 'UNAVAILABLE');
                const failed = (research.sources_checked || []).filter(s => s.status === 'FAILED');
                details.appendChild(aiResearchNode('div', `新增/修订 ${topic.new_material_count || 0} 个版本 · 有效 ${topic.valid_material_count || 0} 条 · 待接入 ${pending.length} 个来源 · 失败 ${failed.length} 个来源`));
                details.appendChild(aiResearchNode('div', `排队 ${topic.queued_at || '--'} · 开始 ${topic.started_at || '--'} · 结束 ${topic.completed_at || '--'}\n` +
                    `固定来源检查 ${stageLabel[research.fixed_status] || research.fixed_status || '未执行'} · 列表/数据入口 ${research.fixed_checks ?? 0} 次 · 规则搜索 ${research.rule_search_count ?? 0} 次\n` +
                    `AI：${research.ai_status ? (stageLabel[research.ai_status] || research.ai_status) : '未启用'} · 实际调用 ${research.ai_call_count ?? topic.call_count ?? 0} 次 · 输入 ${topic.input_tokens || 0} / 输出 ${topic.output_tokens || 0} Token · 耗时 ${topic.latency_ms ?? '--'}ms`));
                if (research.ai_pause_reason) details.appendChild(aiResearchNode('div', 'AI 暂停原因：' + research.ai_pause_reason));
                if (research.ai_trigger_reason) details.appendChild(aiResearchNode('div', 'AI 触发说明：' + research.ai_trigger_reason));
                if (research.ai_stage) details.appendChild(aiResearchNode('div', `扫描：${research.ai_stage.status || '--'} · 开始 ${research.ai_stage.started_at || '--'} · 完成 ${research.ai_stage.completed_at || '--'}`));
                details.appendChild(aiResearchNode('div', `搜索 ${research.search_count || 0} 次 · 阅读 ${research.pages_read || 0} 页 · 模型轮次 ${research.rounds || 0} · 调用 ${topic.call_count || 0} 次`));
                if (topic.error || research.error) details.appendChild(aiResearchNode('div', '采集错误：' + (topic.error || research.error)));
                (research.sources_checked || []).forEach(source => details.appendChild(aiResearchNode('div',
                    `来源 ${source.source_id}：${{COMPLETED:'检查完成',FAILED:'检查失败',UNAVAILABLE:'未接入/受阻',NOT_APPLICABLE:'不适用',SKIPPED_BUDGET:'预算跳过'}[source.status] || source.status}${source.reason ? ' · ' + source.reason : ''}`)));
                (research.events || []).forEach(event => details.appendChild(aiResearchNode('div', `${event.timestamp || ''} · ${event.type || ''} · ${event.status || ''} · ${[event.query,event.title,event.url,event.message].filter(Boolean).join(' · ')}`)));
                box.appendChild(details);
            });
        }

        async function loadAIResearchMaterial(materialId, container) {
            container.textContent = '正在读取保存正文…';
            try {
                const res = await fetch(`/api/v1/ai/research/materials/${encodeURIComponent(materialId)}`);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const material = await res.json();
                container.textContent = `资料版本 ${material.material_id || materialId} · 发布 ${material.published_at || '未知'} · 资料期别 ${material.data_period || '未知'}\n首次获取 ${material.first_fetched_at || '未知'} · 版本获取 ${material.version_fetched_at || material.fetched_at || '未知'} · 内容 hash ${material.content_hash || '未知'}\n\n${material.body || '保存正文为空'}`;
                container.dataset.loaded = 'true';
            } catch (e) { container.textContent = '正文读取失败：' + e.message; }
        }

        let aiMaterialsPage = 1;
        let aiMaterialsRequestId = 0;
        let aiMaterialsHistoryFor = null;

        function queryAIMaterials() {
            aiMaterialsPage = 1; aiMaterialsHistoryFor = null;
            return loadAIMaterials();
        }

        function aiMaterialsMethodLabel(value) {
            return {FIXED_SOURCE:'固定来源', RULE_SEARCH:'规则搜索', AI_DISCOVERY:'AI 发现'}[value] || '未知方式';
        }

        const aiDimensionLabels = {fundamentals:'基本面', macro:'宏观', events:'事件', related_markets:'相关市场'};
        const aiDimensionColors = {fundamentals:'#22d3ee', macro:'#fb923c', events:'#fbbf24', related_markets:'#818cf8'};

        function aiDimensionBadge(dimension) {
            const label = aiDimensionLabels[dimension] || dimension || '未知维度';
            const color = aiDimensionColors[dimension] || '#9ca3af';
            const badge = aiResearchNode('span', label);
            badge.style.cssText = `display:inline-block; padding:0 8px; border-radius:9px; font-size:10px; border:1px solid ${color}; color:${color}; line-height:16px; flex:none;`;
            return badge;
        }

        function aiMaterialRow(item) {
            const row = aiResearchNode('div'); row.className = 'ai-model-row';
            const titleLine = aiResearchNode('div');
            titleLine.style.cssText = 'display:flex; align-items:center; gap:6px;';
            titleLine.appendChild(aiDimensionBadge(item.dimension));
            titleLine.appendChild(aiResearchNode('strong', item.title || '（无标题）'));
            row.appendChild(titleLine);
            const validityLabels = {VALID:'有效', UNVERIFIED:'待核验', SUPERSEDED:'已被替代', BACKGROUND:'背景资料', UNCHECKED:'未选择目标'};
            const relations = {TARGET:'自身', MACRO:'宏观', REFERENCE:'关联参考', OTHER:'未匹配目标'};
            const reasons = {UNKNOWN_PUBLICATION_TIME:'发布时间未知', LATEST_RELEASE_UNVERIFIED:'最新版本待核验',
                SUPERSEDED_OFFICIAL_RELEASE:'官方已有新版本', REPORT_UPDATE_OVERDUE:'报告更新已超过宽限期',
                SUPERSEDED_VERSION:'该网址已有较新保存版本',
                EXPIRED:'资料已过期', NOT_SELECTED_FOR_TARGET:'未选入该目标的有效资料',
                AFTER_CUTOFF_PUBLICATION:'发布晚于截点', AFTER_CUTOFF_FETCH:'获取晚于截点',
                OUTSIDE_APPLICABLE_PERIOD:'不在适用期'};
            row.appendChild(aiResearchNode('div', `${relations[item.relationship] || '未核对关系'} · ${validityLabels[item.validity] || '待核验'}` +
                ((item.validity_reasons || []).length ? ' · ' + item.validity_reasons.map(r => reasons[r] || r).join('、') : '')));
            const time = value => String(value || '').replace('T', ' ').slice(0, 19) || '未知';
            // 发布日期与资料期单独成行、加亮显示; 获取/核验时间保留在次要行
            const dateLine = aiResearchNode('div',
                `发布 ${time(item.published_at)}` +
                (item.data_period && item.data_period !== 'UNKNOWN' ? ` · 资料期 ${item.data_period}` : ''));
            dateLine.style.cssText = 'font-size:12px; font-weight:600; color:#93c5fd; margin:3px 0;';
            row.appendChild(dateLine);
            const meta = aiResearchNode('div',
                `${item.source_name || '未知来源'}` +
                ` · 获取 ${time(item.version_fetched_at)} · 核验 ${time(item.checked_at)}` +
                (item.version_count > 1 ? ` · ${item.version_count} 个版本` : ''));
            meta.style.cssText = 'font-size:10px; color:var(--text-muted); margin:3px 0;';
            row.appendChild(meta);
            const provenance = aiResearchNode('details');
            provenance.appendChild(aiResearchNode('summary', '来源与版本详情'));
            provenance.appendChild(aiResearchNode('div', `${item.source_name || '未知来源'} · ${item.series_id || '未注册序列'} · ${aiMaterialsMethodLabel(item.collection_method)} · hash ${item.content_hash || '未知'}`));
            row.appendChild(provenance);
            (item.facts || []).forEach(fact => row.appendChild(aiResearchNode('div',
                `${fact.metric || ''}：${fact.value ?? ''}${fact.unit || ''} · ${fact.period || '期别未知'} · ${fact.region || '地区未知'}\n原文：${fact.quote || ''}`)));
            if (item.excerpt) {
                const excerpt = aiResearchNode('div', String(item.excerpt).slice(0, 300));
                excerpt.style.cssText = 'font-size:11px; margin:3px 0;';
                row.appendChild(excerpt);
            }
            const urlLine = aiResearchNode('div', String(item.url || ''));
            urlLine.style.cssText = 'font-size:10px; color:var(--text-muted); word-break:break-all;';
            row.appendChild(urlLine);
            const actions = aiResearchNode('div'); actions.style.cssText = 'display:flex; gap:5px;';
            const bodyButton = aiResearchNode('button', '查看正文'); bodyButton.className = 'ai-action';
            const pre = aiResearchNode('pre');
            pre.style.cssText = 'display:none; white-space:pre-wrap; max-height:260px; overflow-y:auto; font-size:11px; margin-top:5px; background:#121418; padding:8px; border-radius:5px;';
            bodyButton.onclick = async () => {
                if (pre.style.display === 'none') {
                    pre.style.display = 'block';
                    if (pre.dataset.loaded !== 'true') await loadAIResearchMaterial(item.material_id, pre);
                    bodyButton.textContent = '收起正文';
                } else {
                    pre.style.display = 'none';
                    bodyButton.textContent = '查看正文';
                }
            };
            actions.appendChild(bodyButton);
            if (item.version_count > 1) {
                const history = aiResearchNode('button', '历史版本'); history.className = 'ai-action';
                history.onclick = () => { aiMaterialsHistoryFor = item.material_id; aiMaterialsPage = 1; return loadAIMaterials(); };
                actions.appendChild(history);
            }
            row.appendChild(actions); row.appendChild(pre);
            return row;
        }

        const aiInstrumentNameCache = new Map();
        async function aiInstrumentNames(contracts) {
            // 代码→中文名: 按品种从全市场合约快照并行拉取一次并缓存, 失败回退空表 (选项退化为纯代码)
            const products = [...new Set((contracts || []).flatMap(item =>
                [item.instrument, ...(item.related || [])]).map(v => String(v || '').replace(/\d+$/, '')).filter(Boolean))];
            const key = products.join(',');
            if (!key) return {};
            if (!aiInstrumentNameCache.has(key)) {
                aiInstrumentNameCache.set(key, Promise.all(products.map(product =>
                    fetch('/api/v1/universe?product=' + encodeURIComponent(product) + '&limit=30')
                        .then(r => r.ok ? r.json() : null).catch(() => null)
                )).then(list => {
                    const map = {};
                    (list || []).forEach(page => (page?.contracts || []).forEach(c => { map[c.symbol] = c.name || ''; }));
                    return map;
                }));
            }
            return aiInstrumentNameCache.get(key);
        }

        function aiProductChineseName(names, product) {
            // 品种中文名: 取任一同品种合约名称并去掉月份数字 (燃油2611 → 燃油)
            const hit = Object.entries(names || {}).find(([symbol]) =>
                new RegExp('^' + product + '\\d').test(symbol));
            return hit ? hit[1].replace(/\d+$/, '') : '';
        }

        function buildAIMaterialTopicOptions(contracts, names) {
            const select = document.getElementById('ai-materials-topic');
            select.replaceChildren();
            const all = aiResearchNode('option', '全部主题'); all.value = ''; select.appendChild(all);
            const macro = aiResearchNode('option', '公共宏观'); macro.value = 'macro'; select.appendChild(macro);
            const seen = new Set();
            (contracts || []).flatMap(item => [item.instrument, ...(item.related || [])]).forEach(instrument => {
                const product = String(instrument || '').replace(/\d+$/, '');
                if (!product || seen.has(product)) return;
                seen.add(product);
                const cname = aiProductChineseName(names, product);
                const option = aiResearchNode('option', product + (cname ? ' ' + cname : '') + '（品种）');
                option.value = 'product:' + product;
                select.appendChild(option);
            });
        }

        async function openAIMaterialsModal() {
            if (isLiveMode) return;
            document.getElementById('ai-materials-modal').style.display = 'flex';
            document.getElementById('ai-materials-keyword').value = '';
            document.getElementById('ai-materials-dimension').value = '';
            document.getElementById('ai-materials-method').value = '';
            aiMaterialsPage = 1;
            aiMaterialsHistoryFor = null;
            let contracts = aiResearchSettings?.contracts;
            if (!contracts) {
                try {
                    const res = await fetch('/api/v1/ai/research/settings');
                    if (res.ok) contracts = (await res.json()).contracts;
                } catch (e) { /* 全部主题仍可用 */ }
            }
            const names = await aiInstrumentNames(contracts);
            buildAIMaterialTopicOptions(contracts || [], names);
            const target = document.getElementById('ai-materials-instrument'); target.replaceChildren();
            const all = aiResearchNode('option', '全部资料（不判断目标适用性）'); all.value = ''; target.appendChild(all);
            (contracts || []).forEach(group => {
                const cname = (names[group.instrument] || '').replace(/\d+$/, '');
                const option = aiResearchNode('option', group.instrument + (cname ? ' · ' + cname : ''));
                option.value = group.instrument; target.appendChild(option);
            });
            target.value = (contracts || []).some(g => g.instrument === currentSymbol) ? currentSymbol : '';
            await loadAIMaterials();
        }

        function closeAIMaterialsModal() {
            aiMaterialsRequestId += 1;
            document.getElementById('ai-materials-modal').style.display = 'none';
        }

        async function loadAIMaterials() {
            const requestId = ++aiMaterialsRequestId;
            const page = aiMaterialsPage;
            const list = document.getElementById('ai-materials-list');
            const message = document.getElementById('ai-materials-message');
            const pager = document.getElementById('ai-materials-pager');
            const params = new URLSearchParams({page: String(page), page_size: '20'});
            const topic = document.getElementById('ai-materials-topic').value;
            const dimension = document.getElementById('ai-materials-dimension').value;
            const method = document.getElementById('ai-materials-method').value;
            const keyword = document.getElementById('ai-materials-keyword').value.trim();
            if (topic) params.set('topic', topic);
            if (dimension) params.set('dimension', dimension);
            if (method) params.set('method', method);
            if (keyword) params.set('q', keyword);
            const instrument = document.getElementById('ai-materials-instrument').value;
            if (instrument) params.set('instrument', instrument);
            if (aiMaterialsHistoryFor) params.set('history_for', aiMaterialsHistoryFor);
            message.textContent = '正在读取资料…';
            list.replaceChildren(); pager.replaceChildren();
            try {
                const res = await fetch('/api/v1/ai/research/materials?' + params);
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                if (requestId !== aiMaterialsRequestId) return;
                list.replaceChildren();
                if (!data.items || !data.items.length) {
                    list.appendChild(aiResearchNode('div', '没有符合条件的资料版本。'));
                } else {
                    data.items.forEach(item => list.appendChild(aiMaterialRow(item)));
                }
                const totalPages = Math.max(1, Math.ceil((data.total || 0) / (data.page_size || 20)));
                pager.replaceChildren();
                const prev = aiResearchNode('button', '上一页'); prev.className = 'ai-action';
                prev.disabled = page <= 1;
                prev.onclick = async () => { aiMaterialsPage = page - 1; await loadAIMaterials(); };
                const next = aiResearchNode('button', '下一页'); next.className = 'ai-action';
                next.disabled = page >= totalPages;
                next.onclick = async () => { aiMaterialsPage = page + 1; await loadAIMaterials(); };
                pager.append(prev, aiResearchNode('span', `第 ${page} / ${totalPages} 页 · 共 ${data.total || 0} 条`), next);
                message.textContent = `${aiMaterialsHistoryFor ? '历史版本' : '最新版本'} · 查询时点 ${data.cutoff ? String(data.cutoff).replace('T', ' ').slice(0, 19) : '当前'} · ${instrument || '未选择目标，未判断适用性'}`;
            } catch (e) { if (requestId === aiMaterialsRequestId) message.textContent = '资料读取失败：' + e.message; }
        }

        const aiAnalysisRequirementDefaults = {
            FUTURE_TREND: '使用已闭合日线、60分钟、15分钟及本批冻结的有效公开资料，分别独立判断未来第3、第5个实际交易日收盘相对冻结参考价的方向。综合技术、量价持仓、基本面、宏观、事件和相关市场资讯，说明各期限的证据完整度、缺失资料、支撑压力、确认及失效条件与替代情景。较长期限缺少关键依据时暂不判断。资料引用必须匹配来源与摘录，未知资料期别明确未知；证据完整度不代表胜率，不自动交易。',
            TODAY: '重点分析当前交易日已发生的量价与持仓变化，给出盘中偏多、偏空、中性或无法判断的结论，并说明关键支撑压力、入场条件、失效条件和风险。不要预测下一交易日开盘。',
            CURRENT_SESSION: '交易中（ACTIVE）：只使用点击前所选连续交易小节内已经发生的量价与持仓数据，预测模型实际完成后10分钟内的走势，先收盘则截止到小节收盘，不引用其他小节补足盘中证据。给出偏多、偏空、中性或无法判断及关键支撑压力；有方向时提供分时图目标价、失效价和可选触发价，证据不足仍应无法判断，不强行设置方向价位。证据截点和小节边界以冻结的session_forecast为准，预测窗口由系统在模型完成后确定；点击前的涨跌只能作证据，不能当作预测命中。',
            NEXT_OPEN: '只预测下一交易日开盘相对当前最新价或最近收盘价的高开、低开、平开或无法判断倾向，并说明量价依据和风险。有夜盘按 21:00，无夜盘按日盘开盘。不要给出今日盘中方向、支撑压力或入场建议。',
        };
        const aiSessionRequirementDefaults = {
            ACTIVE: aiAnalysisRequirementDefaults.CURRENT_SESSION,
            UPCOMING: '尚未开盘（UPCOMING）：使用快照提供的开盘前已闭合历史分钟线、此前交易日日线和盘前参考价，预测所选小节开盘到收盘的走势。允许引用此前小节的历史行情作为盘前证据，不得仅因所选小节尚未产生行情就认为没有证据。给出偏多、偏空、中性或无法判断及关键支撑压力；有方向时提供分时图目标价、失效价和可选触发价，证据不足仍应无法判断，不强行设置方向价位。以冻结的session_forecast为预测窗口；开盘前的涨跌只能作证据，不能当作预测命中，盘前参考价不等于尚未发生的开盘价。',
        };
        const aiLegacySessionRequirements = [
            '交易中（ACTIVE）：只使用点击前所选连续交易小节内已经发生的量价与持仓数据，预测点击时刻到该小节结束的后续走势，不引用其他小节补足盘中证据。给出偏多、偏空、中性或无法判断及关键支撑压力；有方向时提供分时图目标价、失效价和可选触发价，证据不足仍应无法判断，不强行设置方向价位。以冻结的session_forecast为预测窗口；点击前的涨跌只能作证据，不能当作预测命中。',
            '根据所选小节状态区分任务：尚未开盘（UPCOMING）时，使用快照提供的开盘前已闭合历史分钟线、此前交易日日线和盘前参考价，预测所选小节开盘到收盘的走势；允许引用此前小节的历史行情作为盘前证据，不得仅因所选小节尚未产生行情就认为没有证据。交易中（ACTIVE）时，只使用点击前所选连续交易小节内已经发生的量价与持仓数据，预测点击时刻到该小节结束的后续走势，不引用其他小节补足盘中证据。给出偏多、偏空、中性或无法判断及关键支撑压力；有方向时提供分时图目标价、失效价和可选触发价，证据不足仍应无法判断，不强行设置方向价位。以冻结的session_forecast为预测窗口；点击前或开盘前的涨跌只能作证据，不能当作预测命中，盘前参考价不等于尚未发生的开盘价。',
            '只使用点击分析前、所选连续交易小节内已经发生的量价与持仓数据，预测从点击时刻到该小节结束的后续走势。给出偏多、偏空、中性或无法判断，并提供用于分时图的目标价、失效价和可选触发价。不得把点击前已经发生的涨跌当作预测结果，不得引用其他交易小节补足证据。',
        ];
        let aiSessionMode = false;
        let aiFutureMode = false;

