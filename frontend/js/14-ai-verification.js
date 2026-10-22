        // 独立模拟账本：只在用户点击启动后提交计划；打开报告仅执行 GET。
        const aiVerificationCosts = [
            ['commission_per_side', 'commission-per-side', '每边每手手续费'],
            ['slippage_ticks', 'slippage-ticks', '每边滑点跳数'],
            ['model_input_price_per_million', 'input-price', '每百万输入 token 价格'],
            ['model_output_price_per_million', 'output-price', '每百万输出 token 价格'],
        ];
        let aiVerificationOpen = false;
        let aiVerificationModalOwner = '';
        let aiVerificationLoading = false;
        let aiVerificationGeneration = 0;
        let aiVerificationListRequest = 0;
        let aiVerificationDetailRequest = 0;
        let aiVerificationTimer = null;
        let aiVerificationPlans = [];
        let aiVerificationModels = [];
        let aiVerificationSelected = '';
        let aiVerificationData = null;
        let aiVerificationBusy = false;
        let aiVerificationDefaultRequirements = '';
        let aiVerificationRequirementsPlan = '';
        const aiVerificationExpandedTrials = new Map();
        let aiVerificationRefreshFlight = null;
        let aiVerificationListFlight = null;

        function aiVerificationElement(name) { return document.getElementById('ai-verification-' + name); }
        function aiVerificationAllowed() {
            return !isLiveMode && currentUser?.role === 'sim' && (!aiVerificationOpen || aiVerificationModalOwner === aiVerificationOwner());
        }
        function aiVerificationOwner() { return currentUser?.username || ''; }
        function aiVerificationCurrent(generation, owner) {
            return aiVerificationOpen && aiVerificationAllowed() && generation === aiVerificationGeneration && owner === aiVerificationOwner();
        }
        function aiVerificationText(tag, value) {
            const element = document.createElement(tag);
            element.textContent = value == null || value === '' ? '—' : String(value);
            return element;
        }
        function aiVerificationNumber(value, digits = 2) {
            if (value == null || value === '' || !Number.isFinite(Number(value))) return '—';
            return Number(value).toFixed(digits);
        }
        function aiVerificationStatus(value) {
            return {RUNNING:'运行中', STOPPING:'停止中，等待结清', STOPPED:'已停止', COMPLETED:'已完成',
                SUBMITTING:'提交分析中', SUBMITTED:'等待分析结果', ANALYZING:'分析中', WAITING_RESULT:'等待分析结果', WAITING_TRIGGER:'等待触发',
                OPEN:'持仓中', CLOSED:'已退出', SKIPPED:'已跳过', UNVERIFIABLE:'无法验证'}[value] || value || '—';
        }
        function aiVerificationReason(value) {
            const reason = String(value ?? '');
            const translated = {
                STOP_LOSS:'止损退出', TAKE_PROFIT:'止盈退出', SESSION_END:'小节结束退出', STOPPED:'计划停止退出',
                RESULT_TIMEOUT:'10分钟到期结算',
                SESSION_END_EARLY:'收盘提前结算，不计胜负', PRE_ENTRY_INVALIDATED:'进场前失效，计亏损',
                NOT_TRIGGERED:'未触发进场', ENTRY_OUTSIDE_RANGE:'进场价格超出有效范围', MISSING_SESSION_END_QUOTE:'缺少小节结束有效报价',
                INVALID_WINDOW:'预测时间窗口无效', INVALID_TRADE_SPEC:'方向、价位或交易规格无效', INVALID_ENTRY:'进场记录无效',
                INVALID_EXIT_PRICE:'退出价格无效', INVALID_FINANCIAL_RESULT:'收益计算结果无效', MISSED_WINDOW:'错过分析窗口',
            }[reason];
            if (translated) return translated;
            const prefixed = /^(MISSED_WINDOW|STOPPED):\s*(.+)$/.exec(reason);
            if (prefixed && /[\u3400-\u9fff]/.test(prefixed[2])) return prefixed[2];
            return reason;
        }
        async function aiVerificationRequest(url, options) {
            const requestOptions = {cache:'no-store', ...options};
            if ((!options?.method || options.method === 'GET') && typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
                requestOptions.signal = AbortSignal.timeout(10000);
            }
            const res = await fetch(url, requestOptions);
            if (res.status === 401) { closeAIVerificationModal(); window.location.href = '/login'; throw new Error('登录已失效'); }
            const body = await res.json();
            if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '请求失败，请检查输入或稍后重试');
            return body;
        }
        function aiVerificationMessage(value) { aiVerificationElement('message').textContent = value || ''; }
        function syncAIVerificationControls() {
            const selected = !!aiVerificationSelected;
            const active = aiVerificationPlans.some(plan => ['RUNNING', 'STOPPING'].includes(plan.status));
            for (const name of ['instrument', 'model', ...aiVerificationCosts.map(field => field[1])]) {
                aiVerificationElement(name).disabled = selected || aiVerificationBusy || aiVerificationLoading;
            }
            const status = aiVerificationData?.plan?.status;
            const editable = !selected || status === 'STOPPED';
            aiVerificationElement('requirements').readOnly = !editable;
            aiVerificationElement('requirements').disabled = aiVerificationBusy || aiVerificationLoading;
            aiVerificationElement('save-requirements').style.display = selected && editable ? 'inline-block' : 'none';
            aiVerificationElement('save-requirements').disabled = !aiVerificationData || !editable || aiVerificationBusy || aiVerificationLoading;
            aiVerificationElement('prompt-state').textContent = !selected ? '新计划 · 可编辑' : status === 'STOPPED' ? '已停止 · 可编辑' : `${status ? aiVerificationStatus(status) : '加载中'} · 已锁定`;
            aiVerificationElement('prompt-state').dataset.editable = String(editable);
            aiVerificationElement('prompt-help').textContent = !selected ? '为当前模型设置分析要求，创建时随计划保存。交易规则和结果格式由系统约束。' : editable ? '保存后重新启用生效；已提交的分析保留原提示词。' : '仅已停止并结算完成的计划可修改。当前提示词可选中复制。';
            aiVerificationElement('form').style.display = selected ? 'none' : 'block';
            aiVerificationElement('start').disabled = selected || active || aiVerificationBusy || aiVerificationLoading || !aiVerificationModels.length;
            aiVerificationElement('stop').disabled = aiVerificationBusy || aiVerificationData?.plan?.status !== 'RUNNING';
            aiVerificationElement('resume').disabled = aiVerificationBusy || aiVerificationLoading || active || aiVerificationData?.plan?.status !== 'STOPPED';
            aiVerificationElement('copy-restart').disabled = aiVerificationBusy || aiVerificationLoading || active || !['STOPPED', 'COMPLETED'].includes(status);
            aiVerificationElement('export').disabled = !aiVerificationData;
            aiVerificationElement('new').disabled = aiVerificationBusy || aiVerificationLoading;
            aiVerificationElement('active-note').textContent = active ? '已有运行或停止中的计划；结束后才能新建。' : '';
        }
        function renderAIVerificationModels() {
            const select = aiVerificationElement('model');
            select.replaceChildren();
            const placeholder = aiVerificationText('option', aiVerificationModels.length ? '请选择可用模型' : '没有可用模型，请先在模型设置中检查配置及密钥');
            placeholder.value = '';
            select.appendChild(placeholder);
            for (const model of aiVerificationModels) {
                const option = aiVerificationText('option', model.display_name || model.model_name || model.model_config_id);
                option.value = model.model_config_id;
                select.appendChild(option);
            }
            select.value = aiVerificationModels.find(model => /gemini/i.test(model.model_name || model.display_name || ''))?.model_config_id || (aiVerificationModels.length === 1 ? aiVerificationModels[0].model_config_id : '');
            syncAIVerificationControls();
        }
        function renderAIVerificationPlans() {
            const select = aiVerificationElement('plan-select');
            select.replaceChildren();
            const placeholder = aiVerificationText('option', '新建计划');
            placeholder.value = '';
            select.appendChild(placeholder);
            for (const plan of aiVerificationPlans) {
                const option = aiVerificationText('option', `${plan.instrument} · ${plan.model_display_name || plan.model_config_id} · ${aiVerificationStatus(plan.status)} · ${plan.start_day || plan.created_at || plan.plan_id}`);
                option.value = plan.plan_id;
                select.appendChild(option);
            }
            select.value = aiVerificationSelected;
            syncAIVerificationControls();
        }
        function clearAIVerificationReport() {
            aiVerificationData = null;
            aiVerificationExpandedTrials.clear();
            for (const name of ['plan-overview', 'activity', 'plan-meta', 'metrics', 'daily', 'trials']) aiVerificationElement(name).replaceChildren();
            aiVerificationElement('warning').textContent = '新浪采样模拟样本；启动后才开始采集，不补做历史成交。';
            aiVerificationElement('refresh-status').textContent = aiVerificationSelected
                ? '每 2 秒自动刷新 · 正在读取当前计划…' : '每 2 秒自动刷新 · 请选择计划查看结果。';
            syncAIVerificationControls();
        }
        function newAIVerificationPlan() {
            if (!aiVerificationAllowed() || aiVerificationBusy || aiVerificationLoading) return;
            aiVerificationGeneration++;
            aiVerificationSelected = '';
            aiVerificationElement('instrument').value = currentSymbol || 'FU2611';
            for (const [field, name] of aiVerificationCosts) aiVerificationElement(name).value = field === 'commission_per_side' ? '' : '0';
            aiVerificationRequirementsPlan = '';
            aiVerificationElement('requirements').value = aiVerificationDefaultRequirements;
            renderAIVerificationModels();
            renderAIVerificationPlans();
            clearAIVerificationReport();
            aiVerificationMessage('手续费必须明确填写；滑点和模型费用默认 0，所有金额为人民币。');
        }
        async function openAIVerificationModal() {
            if (!aiVerificationAllowed()) return;
            if (aiVerificationOpen) return;
            aiVerificationModalOwner = aiVerificationOwner();
            aiVerificationOpen = true;
            aiVerificationLoading = true;
            aiVerificationGeneration++;
            aiVerificationPlans = [];
            aiVerificationModels = [];
            aiVerificationSelected = '';
            aiVerificationElement('modal').style.display = 'flex';
            aiVerificationElement('account').textContent = `账户：${currentUser.display_name || aiVerificationOwner()}（${aiVerificationOwner()}） · 北京时间 · 金额人民币`;
            aiVerificationElement('instrument').value = currentSymbol || 'FU2611';
            for (const [field, name] of aiVerificationCosts) aiVerificationElement(name).value = field === 'commission_per_side' ? '' : '0';
            aiVerificationRequirementsPlan = '';
            aiVerificationElement('requirements').value = aiVerificationDefaultRequirements;
            clearAIVerificationReport();
            renderAIVerificationPlans();
            renderAIVerificationModels();
            aiVerificationMessage('正在读取可用模型及历史计划…');
            const generation = aiVerificationGeneration, owner = aiVerificationOwner();
            const results = await Promise.allSettled([
                aiVerificationRequest('/api/v1/ai/models'),
                aiVerificationRequest('/api/v1/ai/verification/plans'),
            ]);
            if (!aiVerificationCurrent(generation, owner)) return;
            if (results[0].status === 'fulfilled') {
                aiVerificationModels = (results[0].value.models || []).filter(model => model.enabled === true && model.available === true);
                renderAIVerificationModels();
            }
            if (results[1].status === 'fulfilled') {
                aiVerificationDefaultRequirements = results[1].value.default_requirements || '';
                aiVerificationElement('requirements').value = aiVerificationDefaultRequirements;
                aiVerificationPlans = results[1].value.items || [];
                renderAIVerificationPlans();
            }
            const errors = results.filter(result => result.status === 'rejected').map(result => result.reason.message);
            aiVerificationLoading = false;
            syncAIVerificationControls();
            aiVerificationMessage(errors.join('；') || '手续费必须明确填写；其他成本默认 0，可按需修改。');
            aiVerificationTimer = setInterval(refreshAIVerificationReport, 2000);
            const initial = aiVerificationPlans.find(plan => ['RUNNING', 'STOPPING'].includes(plan.status)) || aiVerificationPlans[0];
            if (initial) await selectAIVerificationPlan(initial.plan_id);
        }
        function closeAIVerificationModal() {
            aiVerificationOpen = false;
            aiVerificationLoading = false;
            aiVerificationGeneration++;
            if (aiVerificationTimer !== null) clearInterval(aiVerificationTimer);
            aiVerificationTimer = null;
            aiVerificationElement('modal').style.display = 'none';
        }
        async function refreshAIVerificationReport() {
            if (!aiVerificationOpen || !aiVerificationAllowed() || aiVerificationBusy || aiVerificationLoading) return;
            if (aiVerificationRefreshFlight?.generation === aiVerificationGeneration) return;
            const generation = aiVerificationGeneration, owner = aiVerificationOwner();
            const flight = {generation}; aiVerificationRefreshFlight = flight;
            const planId = aiVerificationSelected;
            try {
                const listing = (async () => {
                        if (aiVerificationListFlight?.generation === generation) return;
                        const listFlight = {generation}; aiVerificationListFlight = listFlight;
                        const request = ++aiVerificationListRequest;
                        try {
                            const data = await aiVerificationRequest('/api/v1/ai/verification/plans');
                            if (!aiVerificationCurrent(generation, owner) || request !== aiVerificationListRequest) return;
                            aiVerificationPlans = data.items || [];
                            renderAIVerificationPlans();
                        } catch (error) {
                            if (aiVerificationCurrent(generation, owner) && request === aiVerificationListRequest) aiVerificationMessage(error.message);
                        } finally {
                            if (aiVerificationListFlight === listFlight) aiVerificationListFlight = null;
                        }
                    })();
                // 列表维护独立进行；慢列表不能阻止当前报告的后续刷新。
                if (planId) await loadAIVerificationDetail(generation, owner, planId);
                else await listing;
            } finally {
                if (aiVerificationRefreshFlight === flight) aiVerificationRefreshFlight = null;
            }
        }
        async function selectAIVerificationPlan(planId) {
            if (!aiVerificationOpen || !aiVerificationAllowed()) return;
            if (!planId) { newAIVerificationPlan(); return; }
            aiVerificationGeneration++;
            aiVerificationSelected = String(planId);
            clearAIVerificationReport();
            renderAIVerificationPlans();
            syncAIVerificationControls();
            aiVerificationMessage('正在读取选中计划…');
            await loadAIVerificationDetail(aiVerificationGeneration, aiVerificationOwner(), aiVerificationSelected);
        }
        async function loadAIVerificationDetail(generation, owner, planId) {
            const request = ++aiVerificationDetailRequest;
            try {
                const data = await aiVerificationRequest('/api/v1/ai/verification/plans/' + encodeURIComponent(planId));
                if (!aiVerificationCurrent(generation, owner) || planId !== aiVerificationSelected || request !== aiVerificationDetailRequest) return;
                renderAIVerificationReport(data);
                aiVerificationMessage('交易规则与成本已冻结；提示词仅在计划已停止并结算完成后可修改。');
            } catch (error) {
                if (aiVerificationCurrent(generation, owner) && planId === aiVerificationSelected && request === aiVerificationDetailRequest) {
                    aiVerificationMessage(error.message);
                    aiVerificationElement('refresh-status').textContent = '刷新失败，将自动重试；当前保留上次结果。';
                }
            }
        }
        function readAIVerificationDraft() {
            const instrument = aiVerificationElement('instrument').value.trim().toUpperCase();
            const modelId = aiVerificationElement('model').value;
            if (!/^[A-Z]{1,3}\d{3,4}$/.test(instrument)) throw new Error('请填写具体期货合约，例如 FU2611；不可使用主连。');
            if (!aiVerificationModels.some(model => model.model_config_id === modelId)) throw new Error('请选择一个启用且可用的模型。');
            const requirements = aiVerificationElement('requirements').value.trim();
            if (!requirements || requirements.length > 2000) throw new Error('请填写计划提示词，最多 2000 字。');
            const data = {instrument, model_config_id: modelId, analysis_requirements: requirements};
            for (const [field, name, label] of aiVerificationCosts) {
                const text = aiVerificationElement(name).value.trim();
                if (!text || !Number.isFinite(Number(text)) || Number(text) < 0) throw new Error(`请明确填写${label}，须为不小于 0 的有限数字。`);
                data[field] = Number(text);
            }
            return data;
        }
        async function saveAIVerificationRequirements() {
            if (!aiVerificationAllowed() || !aiVerificationSelected || aiVerificationBusy || aiVerificationLoading) return;
            if (aiVerificationData?.plan?.status !== 'STOPPED') { aiVerificationMessage('请先停止计划并等待结算完成，再修改提示词。'); return; }
            const requirements = aiVerificationElement('requirements').value.trim();
            if (!requirements || requirements.length > 2000) { aiVerificationMessage('请填写计划提示词，最多 2000 字。'); return; }
            const generation = aiVerificationGeneration, owner = aiVerificationOwner(), planId = aiVerificationSelected;
            aiVerificationBusy = true;
            syncAIVerificationControls();
            try {
                const result = await aiVerificationRequest('/api/v1/ai/verification/plans/' + encodeURIComponent(planId) + '/requirements', {
                    method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({analysis_requirements:requirements}),
                });
                if (!aiVerificationCurrent(generation, owner) || planId !== aiVerificationSelected) return;
                renderAIVerificationReport(result);
                aiVerificationElement('requirements').value = result.plan.config.analysis_requirements;
                aiVerificationMessage('提示词已保存，从下一次分析生效。');
            } catch (error) {
                if (aiVerificationCurrent(generation, owner)) aiVerificationMessage(error.message);
            } finally {
                aiVerificationBusy = false;
                syncAIVerificationControls();
            }
        }
        async function startAIVerificationPlan() {
            if (!aiVerificationOpen || !aiVerificationAllowed() || aiVerificationBusy || aiVerificationLoading || aiVerificationSelected) return;
            if (aiVerificationPlans.some(plan => ['RUNNING', 'STOPPING'].includes(plan.status))) {
                aiVerificationMessage('已有活跃计划，请停止并结清后再新建。'); return;
            }
            let data;
            try { data = readAIVerificationDraft(); } catch (error) { aiVerificationMessage(error.message); return; }
            const generation = ++aiVerificationGeneration, owner = aiVerificationOwner();
            aiVerificationBusy = true;
            syncAIVerificationControls();
            aiVerificationMessage('正在创建并启动计划…');
            try {
                const result = await aiVerificationRequest('/api/v1/ai/verification/plans', {
                    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data),
                });
                if (!aiVerificationCurrent(generation, owner)) return;
                aiVerificationSelected = result.plan.plan_id;
                aiVerificationPlans = [result.plan, ...aiVerificationPlans.filter(plan => plan.plan_id !== result.plan.plan_id)];
                renderAIVerificationPlans();
                renderAIVerificationReport(result);
                aiVerificationMessage('计划已启动：开盘15分钟后开始，等待行情或交易结果再分析；未成交信号10分钟无结果作废重试，无明确方向5分钟后重试。每次调用计费。');
            } catch (error) {
                if (aiVerificationCurrent(generation, owner)) aiVerificationMessage(error.message);
            } finally {
                aiVerificationBusy = false;
                if (aiVerificationOpen) syncAIVerificationControls();
            }
        }
        async function stopAIVerificationPlan() {
            if (!aiVerificationOpen || !aiVerificationAllowed() || aiVerificationBusy || aiVerificationData?.plan?.status !== 'RUNNING') return;
            const generation = ++aiVerificationGeneration, owner = aiVerificationOwner(), planId = aiVerificationSelected;
            aiVerificationBusy = true;
            syncAIVerificationControls();
            try {
                const result = await aiVerificationRequest('/api/v1/ai/verification/plans/' + encodeURIComponent(planId) + '/stop', {method:'POST'});
                if (!aiVerificationCurrent(generation, owner) || planId !== aiVerificationSelected) return;
                aiVerificationPlans = aiVerificationPlans.map(plan => plan.plan_id === planId ? result.plan : plan);
                renderAIVerificationPlans();
                renderAIVerificationReport(result);
                aiVerificationMessage('停止已提交；已有仓位等待有效报价退出，已发起分析的费用继续结算。');
            } catch (error) {
                if (aiVerificationCurrent(generation, owner) && planId === aiVerificationSelected) aiVerificationMessage(error.message);
            } finally {
                aiVerificationBusy = false;
                if (aiVerificationOpen) syncAIVerificationControls();
            }
        }
        async function resumeAIVerificationPlan() {
            if (!aiVerificationOpen || !aiVerificationAllowed() || aiVerificationBusy || aiVerificationLoading
                    || aiVerificationData?.plan?.status !== 'STOPPED') return;
            const savedRequirements = aiVerificationData.plan.config?.analysis_requirements ?? aiVerificationDefaultRequirements;
            if (aiVerificationElement('requirements').value.trim() !== savedRequirements.trim()) {
                aiVerificationMessage('提示词有未保存的修改，请先保存再重新启用计划。'); return;
            }
            const generation = aiVerificationGeneration, owner = aiVerificationOwner(), planId = aiVerificationSelected;
            aiVerificationBusy = true;
            syncAIVerificationControls();
            try {
                const result = await aiVerificationRequest('/api/v1/ai/verification/plans/' + encodeURIComponent(planId) + '/resume', {method:'POST'});
                if (!aiVerificationCurrent(generation, owner) || planId !== aiVerificationSelected) return;
                aiVerificationPlans = aiVerificationPlans.map(plan => plan.plan_id === planId ? result.plan : plan);
                renderAIVerificationPlans();
                renderAIVerificationReport(result);
                aiVerificationMessage('计划已重新启用，保留历史记录；停止期间不补做分析或成交。');
            } catch (error) {
                if (aiVerificationCurrent(generation, owner) && planId === aiVerificationSelected) aiVerificationMessage(error.message);
            } finally {
                aiVerificationBusy = false;
                if (aiVerificationOpen) syncAIVerificationControls();
            }
        }
        async function copyRestartAIVerificationPlan() {
            if (!aiVerificationOpen || !aiVerificationAllowed() || aiVerificationBusy || aiVerificationLoading
                    || aiVerificationPlans.some(plan => ['RUNNING','STOPPING'].includes(plan.status))
                    || !['STOPPED','COMPLETED'].includes(aiVerificationData?.plan?.status)) return;
            const saved = aiVerificationData.plan.config?.analysis_requirements ?? aiVerificationDefaultRequirements;
            if (aiVerificationElement('requirements').value.trim() !== saved.trim()) {
                aiVerificationMessage('提示词有未保存修改，请先保存再复制。'); return;
            }
            const generation = aiVerificationGeneration, owner = aiVerificationOwner(), planId = aiVerificationSelected;
            aiVerificationBusy = true;
            syncAIVerificationControls();
            try {
                const result = await aiVerificationRequest('/api/v1/ai/verification/plans/' + encodeURIComponent(planId) + '/copy-restart', {method:'POST'});
                if (!aiVerificationCurrent(generation, owner) || planId !== aiVerificationSelected) return;
                aiVerificationSelected = result.plan.plan_id;
                aiVerificationPlans.unshift(result.plan);
                renderAIVerificationPlans();
                renderAIVerificationReport(result);
                aiVerificationMessage('已复制并启动新的20交易日计划，原计划和历史账本保留；使用当前模型配置和自动模拟盘规则。');
            } catch (error) {
                if (aiVerificationCurrent(generation, owner) && planId === aiVerificationSelected) aiVerificationMessage(error.message);
            } finally {
                aiVerificationBusy = false;
                if (aiVerificationOpen) syncAIVerificationControls();
            }
        }
        function aiVerificationTable(container, columns, rows, emptyText) {
            container.replaceChildren();
            if (!rows.length) { container.appendChild(aiVerificationText('p', emptyText)); return; }
            const table = document.createElement('table');
            table.className = 'ai-verification-table';
            const head = document.createElement('thead'), header = document.createElement('tr');
            columns.forEach(([label]) => header.appendChild(aiVerificationText('th', label)));
            head.appendChild(header); table.appendChild(head);
            const body = document.createElement('tbody');
            for (const row of rows) {
                const tr = document.createElement('tr');
                columns.forEach(([, value]) => tr.appendChild(aiVerificationText('td', value(row))));
                body.appendChild(tr);
            }
            table.appendChild(body); container.appendChild(table);
        }
        function aiVerificationTime(value, clockOnly = false) {
            if (!value) return '—';
            const match = String(value).match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/);
            return match ? (clockOnly ? match[2] : `${match[1]} ${match[2]}`) : String(value);
        }
        function aiVerificationQuoteCheck(diagnostic) {
            if (!diagnostic) return '历史记录未保存盘口校验原因';
            const check = diagnostic.rejected_touch || diagnostic.last_check || {};
            const labels = {ACCEPTED:'盘口有效', MISSING_QUOTE:'缺少同交易日盘口', INVALID_QUOTE_TIME:'报价时间无效',
                BEFORE_RESULT:'报价早于或等于模型完成时间', FUTURE_QUOTE:'报价时间晚于扫描时间',
                STALE_QUOTE:'报价过期', MISSING_RECEIPT_TIME:'缺少接收时间', INVALID_RECEIPT_TIME:'接收时间无效',
                STALE_RECEIPT:'接收后超过10秒', ALREADY_PROCESSED:'重复或较早报价', INVALID_BOOK:'买卖盘口无效'};
            return `${labels[check.reason] || check.reason || '待校验'}；源时间 ${aiVerificationTime(check.source_time)}；源延迟 ${aiVerificationNumber(check.source_age_seconds)}秒；接收时间 ${aiVerificationTime(check.received_at)}${diagnostic.rejected_touch ? `；触线价格 ${aiVerificationNumber(check.last_price)}` : ''}`;
        }
        function aiVerificationStack(primary, secondary) {
            const box = document.createElement('div'); box.className = 'ai-verification-stack';
            box.appendChild(aiVerificationText('span', primary));
            if (secondary) {
                const sub = aiVerificationText('small', secondary); box.appendChild(sub);
            }
            return box;
        }
        function appendAIVerificationAIResult(container, result) {
            const section = document.createElement('section'); section.className = 'ai-verification-ai-result';
            section.appendChild(aiVerificationText('strong', 'AI 判断依据（持久化结果）'));
            if (!result) {
                section.appendChild(aiVerificationText('p', '未记录可展示的 AI 输出；成交与费用按原账本展示。'));
                container.appendChild(section); return;
            }
            const runStatus = {SUCCEEDED:'已完成', FAILED:'调用失败', PENDING:'待执行', RUNNING:'运行中', CANCELLED:'已取消'}[result.status] || result.status || '未记录';
            const publication = {REVIEWED:'已复核；是否采用以本次分析状态为准', DRAFT_ONLY:'仅草稿，不用于模拟成交', BLOCKED:'已阻断，不用于模拟成交'}[result.publication_status] || '未记录可用复核结果';
            const review = {SUCCEEDED:'复核通过', FAILED_DRAFT_RETAINED:'复核失败，保留草稿'}[result.review_status] || result.review_status || '未记录';
            section.appendChild(aiVerificationText('p', `模型：${runStatus} · 发布：${publication} · 复核：${review}`));
            const evidence = Array.isArray(result.evidence) ? result.evidence.filter(item => item && typeof item === 'object').map(item =>
                `${aiVerificationTime(item.time)} · ${{LONG:'偏多', SHORT:'偏空', CONTEXT:'背景'}[item.side] || item.side || '—'} · ${item.field || '—'}=${item.value ?? '—'} · ${item.reason || '—'}`).join('\n') : '';
            const fields = [
                ['AI 结论', result.summary],
                ['方向 / 证据完整度（非胜率）', `${{LONG:'做多', SHORT:'做空', NEUTRAL:'中性', NO_JUDGMENT:'无法判断'}[result.direction] || result.direction || '—'} / ${aiVerificationNumber(result.confidence_score, 0)} / 100`],
                ['进场条件', result.entry_condition], ['失效条件', result.invalidation_condition],
                ['价位依据', result.chart_reason],
                ['风险说明', Array.isArray(result.risks) ? result.risks.join('\n') : ''],
                ['行情证据', evidence],
                ['数据质量', result.data_quality ? [result.data_quality.status, ...(Array.isArray(result.data_quality.warnings) ? result.data_quality.warnings : [])].filter(Boolean).join('\n') : ''],
                ...(result.error ? [['输出 / 调用错误', result.error]] : []),
            ];
            const grid = document.createElement('dl'); grid.className = 'ai-verification-ai-grid';
            for (const [label, value] of fields) {
                const item = document.createElement('div');
                if (['AI 结论', '行情证据', '风险说明', '数据质量', '输出 / 调用错误'].includes(label)) item.className = 'ai-verification-ai-wide';
                item.appendChild(aiVerificationText('dt', label)); item.appendChild(aiVerificationText('dd', value)); grid.appendChild(item);
            }
            section.appendChild(grid); container.appendChild(section);
        }
        function renderAIVerificationTrials(rows) {
            const container = aiVerificationElement('trials'); container.replaceChildren();
            if (!rows.length) {
                container.appendChild(aiVerificationText('p', '尚无分析记录；计划会从启动后的下一个分析时间开始。')); return;
            }
            const table = document.createElement('table'); table.className = 'ai-verification-table ai-verification-trial-table';
            const head = document.createElement('thead'), header = document.createElement('tr');
            ['分析时间', '交易日 / 小节', '状态 / 方向', '进场', '退出', '收益 / 费用（元）', '说明'].forEach(label => header.appendChild(aiVerificationText('th', label)));
            head.appendChild(header); table.appendChild(head);
            const body = document.createElement('tbody');
            const orderedRows = [...rows].sort((a, b) => {
                const at = row => row.analysis_due_at || row.state?.submitted_at || row.result_available_at || '';
                return at(b).localeCompare(at(a));
            });
            for (const row of orderedRows) {
                const state = row.state || {}, tr = document.createElement('tr');
                const at = row.analysis_due_at || state.submitted_at || row.result_available_at;
                const stamp = aiVerificationTime(at), parts = stamp.split(' ');
                const reason = aiVerificationReason(row.reason || state.reason || state.exit_reason);
                const timeoutLoss = row.status === 'SKIPPED' && String(row.reason || state.reason || '').split(':')[0] === 'SIGNAL_TIMEOUT';
                const preEntryLoss = row.status === 'SKIPPED' && (row.reason || state.reason) === 'PRE_ENTRY_INVALIDATED';
                const unfilledLoss = timeoutLoss || preEntryLoss;
                const settledTimeout = row.status === 'CLOSED' && state.exit_reason === 'RESULT_TIMEOUT';
                const earlySettlement = row.status === 'CLOSED' && (state.early_session_settlement || state.exit_reason === 'SESSION_END_EARLY');
                const diagnostic = state.quote_diagnostics;
                const timeoutLabel = diagnostic?.rejected_touch ? '触线盘口未通过校验，未成交' : diagnostic?.accepted_count === 0 ? '有效盘口不足，未成交' : diagnostic ? '5分钟未触发，重新分析' : '5分钟未成交，盘口原因未记录';
                const label = state.execution_mode === 'AUTO_PAPER' && timeoutLoss ? timeoutLabel : state.execution_mode === 'AUTO_PAPER' && preEntryLoss ? '未成交信号已失效' : row.status === 'OPEN' && state.settlement_pending ? '已超时，待报价结算' : earlySettlement ? '收盘提前结算，不计胜负' : settledTimeout ? (state.outcome_policy === 'ACTUAL_NET' ? '10分钟结算，按实际净收益' : '旧规则：10分钟结算计亏损') : preEntryLoss ? '进场前失效，计亏损' : timeoutLoss ? '超时计亏损' : aiVerificationStatus(row.status);
                const status = aiVerificationStack(label, {LONG:'做多', SHORT:'做空'}[state.direction] || state.direction || '—');
                status.className += ' ai-verification-status';
                status.dataset.status = row.status || '';
                const timeoutNet = row.model_cost_known === true && row.model_cost != null ? -Number(row.model_cost) : null;
                const live = row.status === 'OPEN' ? row.live_pnl : null;
                const pnl = aiVerificationStack(live ? `浮动 ${live.status === 'LIVE' ? aiVerificationNumber(live.trading_net_pnl) : '待报价'}` : row.status === 'OPEN' ? '浮动 待报价' : unfilledLoss ? `合计 ${aiVerificationNumber(timeoutNet)}` : `净收益 ${aiVerificationNumber(state.trading_net_pnl)}`, `模型 ${row.model_cost_known === false ? '待核实' : aiVerificationNumber(row.model_cost)}`);
                const pnlValue = row.status === 'OPEN' ? live?.trading_net_pnl : state.trading_net_pnl;
                pnl.className += Number(pnlValue) > 0 ? ' ai-verification-positive' : Number(pnlValue) < 0 ? ' ai-verification-negative' : '';
                if (unfilledLoss) pnl.className += ' ai-verification-negative';
                const explanation = aiVerificationText('div', reason); explanation.className = 'ai-verification-reason'; explanation.title = reason;
                const cells = [aiVerificationStack(parts[1] || stamp, parts.length > 1 ? parts[0] : ''),
                    aiVerificationStack(row.session_label || '—', String(row.trading_day || '—').replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3')),
                    status, aiVerificationStack(aiVerificationNumber(state.entry_price), aiVerificationTime(state.entry_time, true)),
                    aiVerificationStack(aiVerificationNumber(state.exit_price), aiVerificationTime(state.exit_time, true)), pnl, explanation];
                cells.forEach(content => { const td = document.createElement('td'); td.appendChild(content); tr.appendChild(td); });
                body.appendChild(tr);
                const detailRow = document.createElement('tr'); detailRow.className = 'ai-verification-detail-row';
                const cell = document.createElement('td'); cell.colSpan = 7;
                const details = document.createElement('details'); details.className = 'ai-verification-details';
                const key = `${aiVerificationOwner()}:${aiVerificationSelected}:${row.trial_id}`;
                details.open = aiVerificationExpandedTrials.get(key) === true;
                details.ontoggle = () => aiVerificationExpandedTrials.set(key, details.open);
                details.appendChild(aiVerificationText('summary', '查看分析与成交详情'));
                const grid = document.createElement('dl'); grid.className = 'ai-verification-detail-grid';
                const fields = [
                    ['盘口校验', aiVerificationQuoteCheck(diagnostic)],
                    ['计划分析时间', aiVerificationTime(row.analysis_due_at)],
                    ['实际提交时间', aiVerificationTime(state.submitted_at)],
                    ['超时截止', aiVerificationTime(state.analysis_deadline_at)],
                    ['行情结果截止', aiVerificationTime(state.signal_deadline_at)],
                    ['未成交等待截止', aiVerificationTime(state.entry_deadline_at)],
                    ['持仓强平截止', aiVerificationTime(state.position_deadline_at)],
                    ['实时估值 / 报价时间', live ? `${aiVerificationNumber(live.trading_net_pnl)} / ${aiVerificationTime(live.quote_time)}` : '—'],
                    ['超时待结算起点', aiVerificationTime(state.settlement_pending_since)],
                    ['后续分析时间', aiVerificationTime(state.next_analysis_due_at)],
                    ['小节开始', aiVerificationTime(row.session_start)], ['小节结束', aiVerificationTime(row.session_end)],
                    ['结果可用时间', aiVerificationTime(row.result_available_at || state.available_at)],
                    ['触发 / 目标 / 失效', [state.trigger_price, state.target_price, state.invalidation_price].map(value => aiVerificationNumber(value)).join(' / ')],
                    ['进场价 / 时间', `${aiVerificationNumber(state.entry_price)} / ${aiVerificationTime(state.entry_time)}`],
                    ['退出价 / 时间', `${aiVerificationNumber(state.exit_price)} / ${aiVerificationTime(state.exit_time)}`],
                    ['退出原因', aiVerificationReason(state.exit_reason)],
                    ['开仓已扣 / 平仓已扣手续费（元）', [state.entry_commission ?? live?.incurred_commission, state.exit_commission].map(value => aiVerificationNumber(value)).join(' / ')],
                    ['交易毛收益 / 手续费 / 净收益（元）', [state.gross_pnl, state.trading_cost, state.trading_net_pnl].map(value => aiVerificationNumber(value)).join(' / ')],
                    ['模型费用（元）', row.model_cost_known === false ? `费用待核实（已知 ${aiVerificationNumber(row.model_cost)}）` : aiVerificationNumber(row.model_cost)],
                    ['完整原因', reason], ['分析记录编号', row.trial_id], ['分析批次编号', row.batch_id], ['模型运行编号', row.run_id],
                ];
                for (const [label, value] of fields) {
                    const item = document.createElement('div');
                    if (label.includes('编号') || label === '完整原因') item.className = 'ai-verification-detail-wide';
                    item.appendChild(aiVerificationText('dt', label)); item.appendChild(aiVerificationText('dd', value)); grid.appendChild(item);
                }
                details.appendChild(grid); appendAIVerificationAIResult(details, row.ai_result);
                cell.appendChild(details); detailRow.appendChild(cell); body.appendChild(detailRow);
            }
            table.appendChild(body); container.appendChild(table);
        }
        function renderAIVerificationReport(data) {
            const panel = aiVerificationElement('panel'), trials = aiVerificationElement('trials');
            const scrollTop = panel.scrollTop || 0, scrollLeft = trials.scrollLeft || 0;
            aiVerificationData = data;
            const plan = data.plan || {}, config = plan.config || {}, report = data.report || {};
            if (aiVerificationRequirementsPlan !== plan.plan_id || plan.status !== 'STOPPED') {
                aiVerificationElement('requirements').value = config.analysis_requirements ?? aiVerificationDefaultRequirements;
                aiVerificationRequirementsPlan = plan.plan_id;
            }
            const monitoring = data.monitoring;
            const unknown = Number(report.model_cost_unknown || 0), unverifiable = Number(report.unverifiable_trials || 0);
            const missed = Number(monitoring?.missed_windows || 0);
            const partial = report.is_complete !== true || unknown > 0 || unverifiable > 0 || missed > 0 || (monitoring && monitoring.complete !== true);
            aiVerificationElement('plan-overview').textContent = plan.plan_id ? `${plan.instrument} · ${plan.model_display_name || plan.model_config_id} · ${aiVerificationStatus(plan.status)}` : '';
            const activity = data.activity || {};
            aiVerificationElement('activity').textContent = [
                `当前：${activity.current_action || '正在读取计划状态'}`,
                `接下来：${activity.next_action || '等待后台确认下一步安排'}`,
                ...(activity.next_at ? [`时间（北京时间）：${aiVerificationTime(activity.next_at)}`] : []),
                ...(activity.pending_cost_count > 0 ? [`另外有 ${activity.pending_cost_count} 次已发起分析等待费用结清。`] : []),
            ].join('\n');
            aiVerificationElement('plan-meta').textContent = [
                `计划：${plan.plan_id} · ${aiVerificationStatus(plan.status)}`,
                `合约：${plan.instrument} · 模型：${plan.model_display_name || plan.model_config_id}`,
                `交易日：${plan.start_day || '待确定'} 至 ${plan.end_day || '待确定'} · 创建：${plan.created_at || '—'} · 停止：${plan.stopped_at || '—'}`,
                `${config.execution_mode === 'AUTO_PAPER' ? '自动模拟盘' : '冻结规则'}：开盘 ${config.analysis_delay_minutes ?? 15} 分钟${config.analysis_schedule_mode === 'RESULT_DRIVEN' ? `${config.analysis_result_trigger === 'TRADE_OUTCOME' ? `，等待行情或交易结果再分析；未成交信号 ${config.signal_timeout_minutes ?? 10} 分钟无结果作废；持仓退出后再分析` : '，有方向立即继续'}；${config.analysis_timeout_minutes ?? 10} 分钟未返回作废重试；无明确方向 ${config.no_direction_retry_minutes ?? 5} 分钟后重试` : config.analysis_interval_minutes ? `，之后每 ${config.analysis_interval_minutes} 分钟` : '，每小节一次'} / ${config.volume ?? 1} 手 / ${config.target_days ?? 20} 交易日 · 乘数 ${config.volume_multiple ?? '—'} / 最小变动 ${config.price_tick ?? '—'}`,
                ...((config.analysis_result_trigger === 'TRADE_OUTCOME' || config.outcome_policy === 'ACTUAL_NET') ? [`持仓结算：从模型完成起 ${config.position_timeout_minutes ?? 10} 分钟仍未止盈/止损，按截止后首笔新鲜盘口结算，${config.outcome_policy === 'ACTUAL_NET' ? '胜负按交易净收益扣本次模型费用判断；零收益不计盈利或亏损，费用未知暂不判胜负。' : '旧规则超时固定计亏损，真实金额保留。'}缺报价则等待；收盘提前结算不计胜负。`] : []),
                ...(config.analysis_rule_effective_at ? [`当前规则生效：${config.analysis_rule_effective_at}；之前记录保留当时规则。`] : []),
                `每边每手手续费 ${aiVerificationNumber(config.commission_per_side)} 元 · 每边滑点 ${aiVerificationNumber(config.slippage_ticks)} 跳 · 每百万输入/输出 token ${aiVerificationNumber(config.model_input_price_per_million)} / ${aiVerificationNumber(config.model_output_price_per_million)} 元`,
                ...(monitoring ? [`监测分析（已记录 / 应监测）：${monitoring.recorded_trials ?? '—'} / ${monitoring.expected_trials ?? '—'} · 已记录交易日：${monitoring.observed_trading_days ?? '—'} / ${monitoring.target_trading_days ?? 20}`] : []),
            ].join('\n');
            aiVerificationElement('warning').textContent = [
                '新浪采样模拟样本；成交依赖实际观测盘口，样本结果不能证明未来盈利。',
                '各小节结束前最后5分钟（含剩余恰好5分钟）不再发起新分析；已发起分析继续结费，已有持仓继续结算。',
                (config.analysis_result_trigger === 'TRADE_OUTCOME' || config.outcome_policy === 'ACTUAL_NET') ? '模型结果完成满10分钟仍未止盈/止损，持仓按截止后首笔新鲜盘口结算；新规则按交易净收益扣本次模型费用判断胜负，历史已结束账本保留原口径。' : '',
                report.is_complete === true && !partial ? '当前已完成样本统计完整。' : '统计尚不完整，收益仅为已知部分，不能当作完整净收益。',
                unknown > 0 ? `费用待核实：${unknown} 次分析用量未知。` : '',
                unverifiable > 0 ? `缺行情无法验证：${unverifiable} 次分析。` : '',
                missed > 0 ? `漏分析窗口：${missed} 次分析，不能算 ${monitoring.target_trading_days ?? 20} 日完整盈利验证。` : '',
            ].filter(Boolean).join(' ');
            const liveReport = data.live || {};
            const liveQuote = liveReport.positions?.[0];
            aiVerificationElement('activity').textContent += (liveReport.open_count > 0 ? `\n实时估值：${liveQuote?.status === 'LIVE' ? `报价 ${aiVerificationTime(liveQuote.quote_time)}；按可平仓价估算，含双边手续费及滑点` : '缺少新鲜有效报价，浮动收益暂不显示'}` : '\n当前空仓，等待下一次分析或触发');
            const metrics = [
                ['实时持仓', liveReport.open_count ?? report.open_count ?? 0],
                ['持仓实时收益（扣已发生开仓费，元）', aiVerificationNumber(liveReport.holding_net_pnl)],
                ['现在平仓预计交易净收益（元）', aiVerificationNumber(liveReport.trading_net_pnl)],
                ['含浮动及模型费用预计净收益（元）', aiVerificationNumber(liveReport.estimated_total_net_pnl)],
                ['已退出交易', report.closed_trades], ['盈利 / 亏损笔数', `${report.wins ?? '—'} / ${report.losses ?? '—'}`],
                ['未成交超时重试次数', report.signal_timeout_count ?? report.signal_timeout_losses ?? 0],
                ['持仓超时结算次数', report.result_timeout_count ?? report.result_timeout_losses ?? 0],
                ['进场前失效次数', report.pre_entry_losses ?? 0],
                ['收盘提前结算次数', report.early_session_settlements ?? 0],
                ['超时待报价结算持仓', report.settlement_pending_count ?? 0],
                ['胜率', `${aiVerificationNumber(report.win_rate_pct)}%`], ['交易毛收益（元）', aiVerificationNumber(report.gross_pnl)],
                ['已发生交易手续费（元）', aiVerificationNumber(report.incurred_trading_cost ?? report.trading_cost)], ['已结算收益扣开仓费及模型费（元）', aiVerificationNumber(report.cash_net_pnl ?? report.total_net_pnl)], ['交易净收益（元）', aiVerificationNumber(report.trading_net_pnl)],
                [unknown > 0 ? '已知模型费用（元）' : '模型费用（元）', aiVerificationNumber(report.model_cost)],
                [partial ? '已知部分合计净收益（元，已实现）' : '已实现合计净收益（元）', aiVerificationNumber(report.total_net_pnl)],
                ['每笔平均交易净收益（元）', aiVerificationNumber(report.average_net_pnl)], ['最大已实现回撤（元）', aiVerificationNumber(report.max_drawdown)],
                ['最长连续亏损', report.max_consecutive_losses], ['跳过分析', report.skipped_trials],
                ['无法验证分析', report.unverifiable_trials], ['费用未知次数', report.model_cost_unknown],
            ];
            const box = aiVerificationElement('metrics'); box.replaceChildren();
            for (const [label, value] of metrics) {
                const card = document.createElement('div'); card.className = 'ai-verification-metric';
                card.appendChild(aiVerificationText('span', label)); card.appendChild(aiVerificationText('strong', value)); box.appendChild(card);
            }
            aiVerificationTable(aiVerificationElement('daily'), [
                ['交易日', row => row.trading_day], ['已退出笔数', row => row.closed_trades], ['交易净收益（元）', row => aiVerificationNumber(row.trading_net_pnl)],
                ['已知模型费用（元）', row => aiVerificationNumber(row.model_cost)],
                [partial ? '已知部分合计（元）' : '合计净收益（元）', row => aiVerificationNumber(row.total_net_pnl)],
            ], report.daily || [], '暂无日汇总。');
            renderAIVerificationTrials(data.trials || []);
            syncAIVerificationControls();
            panel.scrollTop = scrollTop; trials.scrollLeft = scrollLeft;
            aiVerificationElement('refresh-status').textContent = `每 2 秒自动刷新 · 最近更新 ${new Date().toLocaleTimeString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false})}（北京时间）`;
        }
        function exportAIVerificationReport() {
            if (!aiVerificationOpen || !aiVerificationAllowed() || !aiVerificationData) return;
            const blob = new Blob([JSON.stringify(aiVerificationData, null, 2)], {type:'application/json;charset=utf-8'});
            const url = URL.createObjectURL(blob), link = document.createElement('a');
            link.href = url;
            link.download = `ai-verification-${String(aiVerificationData.plan.plan_id).replace(/[^a-zA-Z0-9_-]/g, '_')}.json`;
            document.body.appendChild(link); link.click(); link.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
