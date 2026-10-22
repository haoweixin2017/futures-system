        async function fetchAIModels() {
            const res = await fetch('/api/v1/ai/models');
            if (res.status === 401) { window.location.href = '/login'; throw new Error('登录已失效'); }
            if (!res.ok) throw new Error(await aiErrorMessage(res));
            aiModels = (await res.json()).models || [];
            return aiModels;
        }

        async function fetchAIEvaluationStats() {
            const res = await fetch('/api/v1/ai/evaluations/stats');
            if (!res.ok) throw new Error(await aiErrorMessage(res));
            aiEvaluationStats = (await res.json()).models || {};
            return aiEvaluationStats;
        }

        function formatAIAccuracy(modelId, analysisTarget) {
            const stats = aiEvaluationStats[modelId]?.[analysisTarget];
            const metric = (label, value, count, suffix = '%') => `${label}：${value == null ? '--' : value + suffix}（${count || 0}例${(count || 0) < 5 ? '，样本不足' : ''}）`;
            return [metric('方向正确率', stats?.direction_accuracy, stats?.direction_count),
                ...(analysisTarget === 'CURRENT_SESSION' ? [metric('目标先触及率', stats?.target_first_touch_rate, stats?.target_count)] : []),
                metric('人工平均评分', stats?.manual_score, stats?.manual_count, '/100'),
                metric('无法验证比例', stats?.unverifiable_rate, stats?.automatic_count)].join(' · ');

        }

        function aiSessionHistoryState(run) {
            const state = run.session_line_state?.status;
            const plan = run.result?.session_chart;
            if (['PENDING', 'WAITING_TRIGGER'].includes(state) && plan?.window_policy === 'RESULT_AVAILABLE_10M' &&
                Date.now() >= Date.parse(plan.forecast_end_time)) return state === 'WAITING_TRIGGER' ? '10分钟窗口到期，未触发' : '10分钟窗口已到期，等待验证';
            return {HIT:'目标已命中', INVALIDATED:'预测已失效', UNVERIFIABLE:'数据缺口或同分钟双触及，无法验证',
                PENDING:'等待结果', WAITING_TRIGGER:'等待触发', NO_DIRECTION:'无明确方向'}[state] ||
                (['PENDING', 'RUNNING'].includes(run.status) ? '分析中' : run.status);
        }

        function refreshAIModelAccuracyLabels() {
            const target = currentAIAnalysisTarget();
            document.querySelectorAll('.ai-model-accuracy').forEach(el => {
                el.textContent = formatAIAccuracy(el.dataset.modelId, target);
            });
            document.querySelectorAll('.ai-result-accuracy').forEach(el => {
                el.textContent = formatAIAccuracy(el.dataset.modelId, el.dataset.analysisTarget);
            });
        }

        function resetAIModelForm() {
            document.getElementById('ai-model-id').value = '';
            document.getElementById('ai-model-form-title').textContent = '新增模型';
            document.getElementById('ai-model-provider').value = 'openai_compatible';
            document.getElementById('ai-model-name').value = '';
            document.getElementById('ai-model-url').value = '';
            document.getElementById('ai-model-code').value = '';
            document.getElementById('ai-model-key').value = '';
            document.getElementById('ai-model-context').value = '32768';
            document.getElementById('ai-model-output').value = '2000';
            document.getElementById('ai-model-timeout').value = '90';
            document.getElementById('ai-model-temperature').value = '0.1';
            document.getElementById('ai-model-requires-key').checked = true;
            document.getElementById('ai-model-local').checked = false;
            document.getElementById('ai-model-enabled').checked = true;
            document.getElementById('ai-model-form-message').textContent = '';
            syncAIProviderFields();
        }

        function syncAIProviderFields() {
            const provider = document.getElementById('ai-model-provider').value;
            document.getElementById('ai-model-url').placeholder = provider === 'google_gemini'
                ? 'https://generativelanguage.googleapis.com/v1beta'
                : 'https://api.example.com/v1';
        }

        async function openAIModelModal() {
            if (isLiveMode) return;
            document.getElementById('ai-model-modal').style.display = 'flex';
            resetAIModelForm();
            await loadAIModelSettings();
        }

        function closeAIModelModal() {
            document.getElementById('ai-model-modal').style.display = 'none';
        }

        async function loadAIModelSettings() {
            const box = document.getElementById('ai-model-list');
            box.textContent = '正在读取模型...';
            try {
                const models = await fetchAIModels();
                box.replaceChildren();
                if (!models.length) {
                    const empty = document.createElement('div');
                    empty.style.cssText = 'color:var(--text-muted); font-size:11px; padding:8px 0;';
                    empty.textContent = '尚未配置模型，请在下方新增。';
                    box.appendChild(empty);
                    return;
                }
                models.forEach(model => {
                    const row = document.createElement('div');
                    row.className = 'ai-model-row';
                    const head = document.createElement('div');
                    head.style.cssText = 'display:flex; justify-content:space-between; gap:8px;';
                    const title = document.createElement('strong');
                    title.textContent = model.display_name;
                    const state = document.createElement('span');
                    state.style.cssText = `font-size:10px; color:${model.available ? '#10b981' : '#f59e0b'};`;
                    state.textContent = model.available ? '可用' : (model.unavailable_reason || '不可用');
                    head.append(title, state);
                    const detail = document.createElement('div');
                    detail.style.cssText = 'font-size:10px; color:var(--text-muted); margin:4px 0; word-break:break-all;';
                    detail.textContent = `${model.base_url} · ${model.model_name} · Key ${model.has_api_key ? '已配置' : '未配置'} · ${model.last_test_status || '尚未测试'}`;
                    const actions = document.createElement('div');
                    actions.style.cssText = 'display:flex; gap:5px; flex-wrap:wrap;';
                    const buttons = [
                        [model.enabled ? '停用' : '启用', button => toggleAIModelEnabled(model, button)],
                        ['编辑', () => editAIModel(model)],
                        ['测试连接', () => testAIModel(model.model_config_id)],
                        ['清除密钥', () => clearAIModelKey(model.model_config_id)],
                        ['删除', () => deleteAIModel(model.model_config_id), true],
                    ];
                    buttons.forEach(([label, action, danger]) => {
                        const button = document.createElement('button');
                        button.className = 'ai-action' + (danger ? ' ai-danger' : '');
                        button.textContent = label;
                        button.onclick = () => action(button);
                        actions.appendChild(button);
                    });
                    row.append(head, detail, actions);
                    box.appendChild(row);
                });
            } catch (e) {
                box.textContent = e.message;
            }
        }

        function editAIModel(model) {
            document.getElementById('ai-model-id').value = model.model_config_id;
            document.getElementById('ai-model-form-title').textContent = `编辑模型 · Key ${model.has_api_key ? '已配置' : '未配置'}`;
            document.getElementById('ai-model-provider').value = model.provider || 'openai_compatible';
            document.getElementById('ai-model-name').value = model.display_name;
            document.getElementById('ai-model-url').value = model.base_url;
            document.getElementById('ai-model-code').value = model.model_name;
            document.getElementById('ai-model-key').value = '';
            document.getElementById('ai-model-context').value = model.max_context_tokens;
            document.getElementById('ai-model-output').value = model.max_output_tokens;
            document.getElementById('ai-model-timeout').value = model.timeout_seconds;
            document.getElementById('ai-model-temperature').value = model.temperature;
            document.getElementById('ai-model-requires-key').checked = !!model.requires_api_key;
            document.getElementById('ai-model-local').checked = !!model.is_local_service;
            document.getElementById('ai-model-enabled').checked = !!model.enabled;
            document.getElementById('ai-model-form-message').textContent = '';
            syncAIProviderFields();
        }

        async function toggleAIModelEnabled(model, button) {
            button.disabled = true;
            try {
                const res = await fetch(`/api/v1/ai/models/${model.model_config_id}/enabled`, {
                    method:'PATCH', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({enabled:!model.enabled}),
                });
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                if (document.getElementById('ai-model-id').value === model.model_config_id)
                    document.getElementById('ai-model-enabled').checked = !model.enabled;
                await loadAIModelSettings();
            } catch (e) {
                alert('切换模型状态失败：' + e.message);
                button.disabled = false;
            }
        }

        async function saveAIModel() {
            const id = document.getElementById('ai-model-id').value;
            const message = document.getElementById('ai-model-form-message');
            const button = document.getElementById('ai-model-save');
            const body = {
                display_name: document.getElementById('ai-model-name').value.trim(),
                provider: document.getElementById('ai-model-provider').value,
                base_url: document.getElementById('ai-model-url').value.trim(),
                model_name: document.getElementById('ai-model-code').value.trim(),
                api_key: document.getElementById('ai-model-key').value || null,
                requires_api_key: document.getElementById('ai-model-requires-key').checked,
                is_local_service: document.getElementById('ai-model-local').checked,
                max_context_tokens: Number(document.getElementById('ai-model-context').value),
                max_output_tokens: Number(document.getElementById('ai-model-output').value),
                timeout_seconds: Number(document.getElementById('ai-model-timeout').value),
                temperature: Number(document.getElementById('ai-model-temperature').value),
                enabled: document.getElementById('ai-model-enabled').checked,
            };
            if (!body.display_name || !body.base_url || !body.model_name) {
                message.textContent = '请填写显示名称、API 地址和模型标识。';
                return;
            }
            button.disabled = true;
            message.textContent = '正在保存...';
            try {
                const res = await fetch(id ? `/api/v1/ai/models/${id}` : '/api/v1/ai/models', {
                    method: id ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
                });
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                resetAIModelForm();
                await loadAIModelSettings();
            } catch (e) {
                message.textContent = e.message;
            } finally {
                button.disabled = false;
            }
        }

        async function testAIModel(id) {
            if (!confirm('连接测试会向该模型发送最小请求，可能产生少量 Token 费用。继续吗？')) return;
            try {
                const res = await fetch(`/api/v1/ai/models/${id}/test`, {method:'POST'});
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                const data = await res.json();
                alert(`${data.message}${data.latency_ms != null ? `（${data.latency_ms}ms）` : ''}`);
                await loadAIModelSettings();
            } catch (e) { alert('测试失败：' + e.message); }
        }

        async function clearAIModelKey(id) {
            if (!confirm('确定清除这个模型的 API Key？模型会同时停用。')) return;
            try {
                const res = await fetch(`/api/v1/ai/models/${id}/clear-key`, {method:'POST'});
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                await loadAIModelSettings();
            } catch (e) { alert('清除失败：' + e.message); }
        }

        async function deleteAIModel(id) {
            if (!confirm('确定删除这个模型配置和本机凭据？历史分析记录会保留。')) return;
            try {
                const res = await fetch(`/api/v1/ai/models/${id}`, {method:'DELETE'});
                if (!res.ok) throw new Error(await aiErrorMessage(res));
                resetAIModelForm();
                await loadAIModelSettings();
            } catch (e) { alert('删除失败：' + e.message); }
        }

        let aiResearchSettings = null;
        let aiResearchDraft = null;
        let aiResearchStatus = null;
        let aiResearchSearchTimer = null;
        let aiResearchSearchId = 0;
        let aiResearchStatusId = 0;
        let aiResearchPollTimer = null;
        const aiResearchTopicViews = new Map();

