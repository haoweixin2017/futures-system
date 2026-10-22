const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Removing the request-generation guard, cost validation or text-only rendering
// must fail these tests. The DOM and HTTP fixtures never call a real model.
class Element {
    constructor(tag = 'div') { this.tagName = tag; this.children = []; this.style = {}; this.dataset = {}; this.value = ''; this.disabled = false; this._text = ''; }
    set textContent(value) { this._text = String(value ?? ''); this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    set innerHTML(value) { throw new Error('Untrusted HTML must never be rendered'); }
    appendChild(child) { this.children.push(child); return child; }
    append(...children) { children.forEach(child => this.appendChild(child)); }
    replaceChildren(...children) { this._text = ''; this.children = children; }
    remove() {}
    click() { this.clicked = true; }
}
function setup() {
    const elements = new Map(), calls = [], timers = new Map(), downloads = [];
    let nextTimer = 0;
    const context = {
        console, isLiveMode: false, currentUser: {username: 'alice', role: 'sim', display_name: '甲'}, currentSymbol: 'FU2611',
        document: {getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
            createElement: tag => new Element(tag), body: new Element('body')},
        window: {location: {}}, Blob: class { constructor(parts) { downloads.push(parts.join('')); } },
        URL: {createObjectURL: () => 'blob:fixture', revokeObjectURL() {}},
        setInterval(fn) { const id = ++nextTimer; timers.set(id, fn); return id; }, clearInterval(id) { timers.delete(id); },
        setTimeout(fn) { fn(); },
        fetch: async (url, options = {}) => {
            calls.push({url, options});
            if (url.endsWith('/models')) return response({models: [
                {model_config_id: 'disabled', enabled: false, available: true, display_name: '已停用'},
                {model_config_id: 'missing-key', enabled: true, available: false, display_name: '缺密钥不可用'},
                {model_config_id: 'missing-availability', enabled: true, display_name: '未确认可用'},
                {model_config_id: 'one', enabled: true, available: true, display_name: '<img src=x onerror=alert(1)>'}]});
            return response({items: [], default_requirements:'默认计划提示词'});
        }
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/js/14-ai-verification.js'), 'utf8'), context);
    return {context, elements, calls, timers, downloads, el: id => context.document.getElementById(id)};
}
function response(body) { return {ok: true, status: 200, json: async () => body}; }
function plan(id, instrument = 'FU2611', status = 'RUNNING') { return {plan_id: id, instrument, status, model_config_id: 'one', model_display_name: '<script>模型</script>',
    config: {commission_per_side: 2, slippage_ticks: 1, model_input_price_per_million: 3, model_output_price_per_million: 4,
        analysis_delay_minutes: 15, analysis_interval_minutes: 10, volume: 1, target_days: 20, price_tick: 1, volume_multiple: 10}, start_day: '20261008', end_day: '20261104'}; }
function detail(id, instrument) { return {plan: plan(id, instrument), report: {is_complete: false, closed_trades: 1, trading_net_pnl: 20,
    model_cost: 0.5, model_cost_unknown: 1, total_net_pnl: 19.5, unverifiable_trials: 1, daily: [{trading_day: '20261008', trading_net_pnl: 20, model_cost: 0.5, total_net_pnl: 19.5, closed_trades: 1}]},
    trials: [{trial_id: id + '-trial', trading_day: '20261008', session_label: '夜盘', status: 'UNVERIFIABLE', reason: '<img src=x onerror=alert(1)>', model_cost_known: false,
        state: {direction: 'LONG', entry_price: 100, exit_price: 102, entry_time: '2026-10-08T21:16:00+08:00', exit_time: '2026-10-08T21:30:00+08:00'}}]}; }
async function main() {
    assert.ok(fs.existsSync(path.join(__dirname, '../frontend/js/14-ai-verification.js')), '模拟验证脚本必须存在');
    const {context: c, calls, timers, el, downloads} = setup();
    await c.openAIVerificationModal();
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, '打开/GET绝不能创建计划');
    assert.equal(el('ai-verification-instrument').value, 'FU2611');
    assert.equal(el('ai-verification-model').children.length, 2, '只列启用且可用模型和提示项');
    assert.equal(el('ai-verification-model').value, 'one', '仅一个可用模型自动选中');
    assert.match(el('ai-verification-model').textContent, /<img/);
    assert.equal(timers.size, 1);
    assert.equal(el('ai-verification-requirements').value, '默认计划提示词');
    for (const field of ['slippage-ticks','input-price','output-price']) assert.equal(el('ai-verification-' + field).value,'0');
    await c.startAIVerificationPlan();
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, '手续费空白不能偷偷变成零');
    assert.match(el('ai-verification-message').textContent, /明确填写/);
    const fields = ['commission-per-side', 'slippage-ticks', 'input-price', 'output-price'];
    for (const bad of ['-1', 'NaN', 'Infinity', ' ']) {
        fields.forEach(field => { el('ai-verification-' + field).value = '0'; });
        el('ai-verification-input-price').value = bad;
        await c.startAIVerificationPlan();
    }
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0);
    fields.forEach((field, i) => { el('ai-verification-' + field).value = String(i); });
    c.fetch = async (url, options = {}) => { calls.push({url, options}); return response(detail('new-plan')); };
    await c.startAIVerificationPlan();
    assert.deepEqual(JSON.parse(calls.at(-1).options.body), {instrument: 'FU2611', model_config_id: 'one', commission_per_side: 0, slippage_ticks: 1,
        model_input_price_per_million: 2, model_output_price_per_million: 3, analysis_requirements:'默认计划提示词'});
    assert.equal(el('ai-verification-requirements').readOnly,true,'运行中提示词只读');
    const beforeLockedSave = calls.length;
    await c.saveAIVerificationRequirements();
    assert.equal(calls.length,beforeLockedSave,'运行中不能发起保存请求');
    const stoppedPrompt = detail('new-plan'); stoppedPrompt.plan.status='STOPPED';
    c.renderAIVerificationReport(stoppedPrompt);
    assert.equal(el('ai-verification-requirements').readOnly,false,'已停止可编辑');
    el('ai-verification-requirements').value = '自定义计划提示词';
    c.renderAIVerificationReport(stoppedPrompt);
    assert.equal(el('ai-verification-requirements').value,'自定义计划提示词','轮询不覆盖正在编辑的提示词');
    c.fetch = async (url, options = {}) => { calls.push({url, options}); const data=detail('new-plan'); data.plan.status='STOPPED'; data.plan.config.analysis_requirements='自定义计划提示词'; return response(data); };
    await c.saveAIVerificationRequirements();
    assert.equal(calls.at(-1).options.method,'PATCH');
    assert.deepEqual(JSON.parse(calls.at(-1).options.body),{analysis_requirements:'自定义计划提示词'});
    assert.equal(el('ai-verification-start').disabled, true, '选中冻结计划不能再次启动');
    assert.equal(el('ai-verification-instrument').disabled, true);
    assert.match(el('ai-verification-warning').textContent, /费用待核实/);
    assert.match(el('ai-verification-warning').textContent, /缺行情无法验证/);
    assert.match(el('ai-verification-metrics').textContent, /已知部分合计净收益/);
    assert.match(el('ai-verification-trials').textContent, /<img/);
    assert.match(el('ai-verification-daily').textContent, /20261008/);
    const monitoringData = {...detail('new-plan'), monitoring: {expected_trials: 8, recorded_trials: 8, missed_windows: 2,
        observed_trading_days: 3, target_trading_days: 20, complete: false}};
    monitoringData.report = {...monitoringData.report, is_complete: true, model_cost_unknown: 0, unverifiable_trials: 0};
    c.renderAIVerificationReport(monitoringData);
    assert.match(el('ai-verification-plan-meta').textContent, /监测分析.*8\s*\/\s*8/);
    assert.match(el('ai-verification-plan-meta').textContent, /之后每 10 分钟/);
    assert.match(el('ai-verification-plan-meta').textContent, /已记录交易日.*3\s*\/\s*20/);
    assert.match(el('ai-verification-warning').textContent, /漏分析窗口.*2/);
    assert.match(el('ai-verification-warning').textContent, /不能算.*20.*完整盈利验证/);
    assert.match(el('ai-verification-metrics').textContent, /已知部分合计净收益/, '监测范围不完整时不能宣称完整净收益');
    const drivenData = detail('driven');
    drivenData.plan.config = {...drivenData.plan.config, analysis_schedule_mode:'RESULT_DRIVEN',
        analysis_timeout_minutes:10, no_direction_retry_minutes:5};
    drivenData.trials[0].state.analysis_deadline_at = '2026-10-08T21:25:00';
    drivenData.trials[0].state.next_analysis_due_at = '2026-10-08T21:20:00';
    c.renderAIVerificationReport(drivenData);
    assert.match(el('ai-verification-plan-meta').textContent, /有方向立即继续.*10 分钟.*5 分钟/);
    drivenData.plan.config.analysis_result_trigger = 'TRADE_OUTCOME';
    c.renderAIVerificationReport(drivenData);
    assert.match(el('ai-verification-plan-meta').textContent, /等待行情或交易结果再分析.*10 分钟.*持仓退出后再分析/);
    assert.match(el('ai-verification-trials').textContent, /21:25:00/);
    assert.match(el('ai-verification-trials').textContent, /21:20:00/);
    drivenData.activity = {current_action:'已发起分析，等待模型返回结果', next_action:'超过截止时间仍未返回则重试',
        next_at:'2026-10-08T21:25:00', pending_cost_count:2};
    c.renderAIVerificationReport(drivenData);
    assert.match(el('ai-verification-activity').textContent,/当前：已发起分析/);
    assert.match(el('ai-verification-activity').textContent,/接下来：超过截止时间/);
    assert.match(el('ai-verification-activity').textContent,/北京时间.*2026-10-08 21:25:00/);
    assert.match(el('ai-verification-activity').textContent,/2 次/);
    const trialTable = el('ai-verification-trials').children[0];
    assert.equal(trialTable.children[0].children[0].children.length, 7, '主表只展示七列常用信息');
    assert.equal(trialTable.children[1].children[0].children.length, 7);
    assert.ok(!trialTable.children[1].children[0].textContent.includes('driven-trial'), '追踪编号不得撑高主记录行');
    const trialDetails = trialTable.children[1].children[1].children[0].children[0];
    assert.equal(trialDetails.open, false, '详细字段默认收起');
    assert.match(trialDetails.textContent, /driven-trial/);
    trialDetails.open = true;
    trialDetails.ontoggle();
    c.renderAIVerificationReport(drivenData);
    assert.equal(el('ai-verification-trials').children[0].children[1].children[1].children[0].children[0].open, true,
        '报告轮询刷新须保留已展开详情');
    const orderedRows = [
        {trial_id:'early', analysis_due_at:'2026-10-08T21:15:00', state:{}},
        {trial_id:'latest', analysis_due_at:'2026-10-09T09:15:00', state:{}},
        {trial_id:'middle', analysis_due_at:'2026-10-08T21:25:00', state:{}},
    ];
    const originalOrder = orderedRows.map(row => row.trial_id);
    c.renderAIVerificationTrials(orderedRows);
    const orderedBody = el('ai-verification-trials').children[0].children[1];
    assert.match(orderedBody.children[0].children[0].textContent, /09:15:00.*2026-10-09/);
    assert.match(orderedBody.children[2].children[0].textContent, /21:25:00/);
    assert.match(orderedBody.children[4].children[0].textContent, /21:15:00/);
    assert.deepEqual(orderedRows.map(row => row.trial_id), originalOrder, '展示排序不改写原始报告');
    c.renderAIVerificationTrials([{trial_id:'timeout', status:'SKIPPED', reason:'SIGNAL_TIMEOUT:未成交',
        model_cost:2, model_cost_known:true, state:{}}]);
    assert.match(el('ai-verification-trials').textContent, /超时计亏损/);
    assert.match(el('ai-verification-trials').textContent, /合计 -2\.00/);
    c.renderAIVerificationTrials([{trial_id:'open-timeout', status:'CLOSED', reason:'RESULT_TIMEOUT',
        model_cost:2, model_cost_known:true, state:{exit_reason:'RESULT_TIMEOUT', trading_net_pnl:6,
            exit_price:102, exit_time:'2026-10-08T21:25:00'}}]);
    assert.match(el('ai-verification-trials').textContent, /旧规则：10分钟结算计亏损/);
    assert.match(el('ai-verification-trials').textContent, /净收益 6\.00/);
    c.renderAIVerificationTrials([{trial_id:'new-timeout',status:'CLOSED',reason:'RESULT_TIMEOUT',
        model_cost:2,model_cost_known:true,state:{exit_reason:'RESULT_TIMEOUT',outcome_policy:'ACTUAL_NET',trading_net_pnl:6}}]);
    assert.match(el('ai-verification-trials').textContent, /10分钟结算，按实际净收益/);
    assert.doesNotMatch(el('ai-verification-trials').textContent, /旧规则|计亏损/);

    c.renderAIVerificationTrials([
        {trial_id:'invalidated',status:'SKIPPED',reason:'PRE_ENTRY_INVALIDATED',model_cost:2,model_cost_known:true,state:{}},
        {trial_id:'early',status:'CLOSED',reason:'SESSION_END_EARLY',state:{exit_reason:'SESSION_END_EARLY',trading_net_pnl:15}},
        {trial_id:'pending',status:'OPEN',state:{settlement_pending:true}},
    ]);
    assert.match(el('ai-verification-trials').textContent,/进场前失效，计亏损/);
    assert.match(el('ai-verification-trials').textContent,/收盘提前结算，不计胜负/);
    assert.match(el('ai-verification-trials').textContent,/已超时，待报价结算/);
    c.renderAIVerificationTrials([{trial_id:'ai-detail', status:'SKIPPED', state:{},
        ai_result:{status:'SUCCEEDED', direction:'LONG', confidence_score:68,
            publication_status:'DRAFT_ONLY', review_status:'FAILED_DRAFT_RETAINED',
            summary:'<img src=x onerror=alert(1)>判断依据', risks:['量能不足'],
            entry_condition:'突破100', invalidation_condition:'跌破90',
            evidence:[{side:'LONG',time:'2026-10-08T10:44:00',field:'close',value:99,reason:'接近压力'}]}}]);
    const aiDetailText = el('ai-verification-trials').textContent;
    for (const text of ['AI 判断依据', '判断依据', '68', '量能不足', '突破100', '跌破90', '接近压力', '草稿']) {
        assert.ok(aiDetailText.includes(text), `AI详情必须显示${text}`);
    }
    c.renderAIVerificationTrials([{trial_id:'old-no-ai', status:'CLOSED', state:{}}]);
    assert.match(el('ai-verification-trials').textContent, /未记录可展示的 AI 输出/);
    const outcomePlan = detail('outcome-plan');
    outcomePlan.plan.config.analysis_schedule_mode = 'RESULT_DRIVEN';
    outcomePlan.plan.config.analysis_result_trigger = 'TRADE_OUTCOME';
    outcomePlan.plan.config.outcome_policy = 'ACTUAL_NET';
    c.renderAIVerificationReport(outcomePlan);
    assert.match(el('ai-verification-plan-meta').textContent, /模型完成起.*10.*分钟.*胜负按交易净收益扣本次模型费用/);
    const autoData = detail('auto-paper');
    autoData.plan.config = {...autoData.plan.config,execution_mode:'AUTO_PAPER',analysis_schedule_mode:'RESULT_DRIVEN',
        analysis_result_trigger:'TRADE_OUTCOME',signal_timeout_minutes:5,position_timeout_minutes:10,outcome_policy:'ACTUAL_NET'};
    autoData.live={open_count:1,holding_net_pnl:22,trading_net_pnl:20,estimated_total_net_pnl:18,
        positions:[{status:'LIVE',quote_time:'2026-10-08T21:25:00'}]};
    autoData.trials=[{trial_id:'auto-open',status:'OPEN',model_cost:2,model_cost_known:true,
        state:{entry_price:100,entry_time:'2026-10-08T21:20:00',entry_deadline_at:'2026-10-08T21:25:00',position_deadline_at:'2026-10-08T21:30:00'},
        live_pnl:{status:'LIVE',trading_net_pnl:20,quote_time:'2026-10-08T21:25:00'}}];
    c.renderAIVerificationReport(autoData);
    assert.match(el('ai-verification-plan-meta').textContent,/自动模拟盘.*未成交信号 5 分钟/);
    assert.match(el('ai-verification-plan-meta').textContent,/持仓结算.*10 分钟/);
    assert.match(el('ai-verification-metrics').textContent,/现在平仓预计交易净收益.*20\.00/);
    assert.match(el('ai-verification-metrics').textContent,/持仓实时收益.*22\.00/);
    assert.match(el('ai-verification-trials').textContent,/浮动 20\.00/);
    assert.match(el('ai-verification-activity').textContent,/实时估值.*报价/);
    autoData.live={open_count:1,trading_net_pnl:null,estimated_total_net_pnl:null,positions:[{status:'STALE_OR_MISSING'}]};
    autoData.trials[0].live_pnl={status:'STALE_OR_MISSING',trading_net_pnl:null};
    c.renderAIVerificationReport(autoData);
    assert.match(el('ai-verification-trials').textContent,/浮动 待报价/);
    assert.match(el('ai-verification-activity').textContent,/缺少新鲜有效报价/);
    const rejectedData = detail('rejected-touch');
    rejectedData.trials = [{trial_id:'touch',status:'SKIPPED',reason:'SIGNAL_TIMEOUT:有效盘口不足',
        state:{execution_mode:'AUTO_PAPER',quote_diagnostics:{accepted_count:0,
            last_check:{reason:'STALE_QUOTE',source_time:'2026-10-08T21:24:00',source_age_seconds:23},
            rejected_touch:{source_time:'2026-10-08T21:24:00',last_price:100,reason:'STALE_QUOTE'}}}}];
    c.renderAIVerificationReport(rejectedData);
    assert.match(el('ai-verification-trials').textContent,/触线盘口未通过校验/);
    assert.match(el('ai-verification-trials').textContent,/报价过期/);
    assert.doesNotMatch(el('ai-verification-trials').textContent,/5分钟未触发/);
    const reasonData = {...detail('new-plan'), trials: [
        {trial_id:'a', status:'SUBMITTED', reason:'MISSED_WINDOW:漏过分析窗口', state:{exit_reason:'STOP_LOSS'}},
        {trial_id:'b', status:'SKIPPED', reason:'NOT_TRIGGERED', state:{exit_reason:'TAKE_PROFIT'}},
        {trial_id:'c', status:'SKIPPED', reason:'ENTRY_OUTSIDE_RANGE', state:{exit_reason:'SESSION_END'}},
        {trial_id:'d', status:'UNVERIFIABLE', reason:'MISSING_SESSION_END_QUOTE', state:{exit_reason:'STOPPED'}},
        {trial_id:'e', status:'SKIPPED', reason:'STOPPED:用户停止计划', state:{}},
        {trial_id:'f', status:'SKIPPED', reason:'UNKNOWN_DIAGNOSTIC_CODE', state:{}},
    ]};
    c.renderAIVerificationReport(reasonData);
    const reasonText = el('ai-verification-trials').textContent;
    assert.match(reasonText, /等待分析结果/);
    assert.match(reasonText, /止损退出/); assert.match(reasonText, /止盈退出/); assert.match(reasonText, /小节结束退出/); assert.match(reasonText, /计划停止退出/);
    assert.match(reasonText, /未触发进场/); assert.match(reasonText, /进场价格超出有效范围/); assert.match(reasonText, /缺少小节结束有效报价/);
    assert.match(reasonText, /漏过分析窗口/); assert.match(reasonText, /用户停止计划/); assert.match(reasonText, /UNKNOWN_DIAGNOSTIC_CODE/);
    assert.doesNotMatch(reasonText, /MISSED_WINDOW:|STOPPED:|STOP_LOSS|TAKE_PROFIT|MISSING_SESSION_END_QUOTE/);
    c.exportAIVerificationReport();
    assert.equal(JSON.parse(downloads.pop()).trials[0].state.exit_reason, 'STOP_LOSS', 'JSON导出保留原始原因代码');
    c.renderAIVerificationReport(detail('new-plan'));
    c.exportAIVerificationReport();
    assert.equal(JSON.parse(downloads[0]).plan.plan_id, 'new-plan');
    const pending = [];
    c.fetch = (url, options = {}) => { calls.push({url, options}); return new Promise(resolve => pending.push({url, resolve})); };
    const first = c.selectAIVerificationPlan('first');
    assert.equal(el('ai-verification-plan-meta').textContent, '', '切换计划应立即移除旧计划报告');
    assert.equal(el('ai-verification-activity').textContent, '', '切换计划不能保留旧的当前安排');
    const second = c.selectAIVerificationPlan('second');
    pending[1].resolve(response(detail('second', 'BU2611'))); await second;
    pending[0].resolve(response(detail('first', 'FU2611'))); await first;
    assert.match(el('ai-verification-plan-meta').textContent, /BU2611/);
    assert.doesNotMatch(el('ai-verification-plan-meta').textContent, /first/);
    const closing = c.selectAIVerificationPlan('closing');
    c.closeAIVerificationModal();
    pending[2].resolve(response(detail('closing'))); await closing;
    assert.equal(timers.size, 0);
    assert.equal(el('ai-verification-plan-meta').textContent, '', '关闭后的请求不应覆盖');
    c.currentSymbol = 'RB2701'; c.fetch = async url => response(url.endsWith('/models') ? {models: [{enabled: true, available: true, model_config_id: 'one'}]} : {items: []});
    await c.openAIVerificationModal();
    assert.equal(el('ai-verification-instrument').value, 'RB2701', '重新打开新计划使用当前合约');
    c.closeAIVerificationModal();
    c.isLiveMode = true;
    const before = calls.length; await c.openAIVerificationModal();
    assert.equal(calls.length, before, 'live模式不可启动GET/POST');

    const history = setup(), h = history.context;
    h.fetch = async (url, options = {}) => {
        history.calls.push({url, options});
        if (url.endsWith('/models')) return response({models: [{enabled: true, available: true, model_config_id: 'one'}, {enabled: true, available: true, model_config_id: 'two'}]});
        if (url.endsWith('/stop')) return response({...detail('running'), plan: plan('running', 'FU2611', 'STOPPING')});
        if (url.endsWith('/plans')) return response({items: [plan('running')]});
        return response(detail('running'));
    };
    await h.openAIVerificationModal();
    assert.equal(history.el('ai-verification-model').value, '', '多个模型必须用户选择');
    assert.equal(history.el('ai-verification-stop').disabled, false);
    await h.stopAIVerificationPlan();
    assert.equal(history.calls.at(-1).url, '/api/v1/ai/verification/plans/running/stop');
    assert.equal(history.calls.at(-1).options.method, 'POST');
    assert.equal(history.el('ai-verification-stop').disabled, true);
    assert.equal(history.el('ai-verification-requirements').readOnly,true,'停止结算中仍锁定');
    assert.match(history.el('ai-verification-plan-meta').textContent, /停止中/);
    const savedOwner = h.currentUser;
    h.currentUser = {username: 'bob', role: 'sim'};
    h.exportAIVerificationReport();
    assert.equal(history.downloads.length, 0, '切换账户后不可导出旧账户已载入的报告');
    h.currentUser = savedOwner;
    h.newAIVerificationPlan();
    assert.equal(history.el('ai-verification-start').disabled, true, '停止中尚未结清，不允许下一活跃计划');
    const posts = history.calls.filter(call => call.options.method === 'POST').length;
    await h.startAIVerificationPlan();
    assert.equal(history.calls.filter(call => call.options.method === 'POST').length, posts);
    await h.refreshAIVerificationReport();
    assert.equal(history.calls.at(-1).url, '/api/v1/ai/verification/plans', '新建草稿轮询仅GET列表');
    h.closeAIVerificationModal();
    const closedCalls = history.calls.length; await h.refreshAIVerificationReport();
    assert.equal(history.calls.length, closedCalls);

    const isolation = setup(), isolated = isolation.context;
    await isolated.openAIVerificationModal();
    let resolveIsolated;
    isolated.fetch = () => new Promise(resolve => { resolveIsolated = resolve; });
    const ownerRequest = isolated.selectAIVerificationPlan('private');
    isolated.currentUser = {username: 'bob', role: 'sim'};
    resolveIsolated(response(detail('private'))); await ownerRequest;
    assert.equal(isolation.el('ai-verification-plan-meta').textContent, '', '旧账户请求不可覆盖新账户');
    isolated.closeAIVerificationModal();

    const loading = setup(), l = loading.context, initialRequests = [];
    l.fetch = url => new Promise(resolve => initialRequests.push({url, resolve}));
    const opening = l.openAIVerificationModal();
    assert.equal(loading.el('ai-verification-new').disabled, true, '初次读取期间禁止重置导致模型列表丢失');
    initialRequests.forEach(({url, resolve}) => resolve(response(url.endsWith('/models') ? {models: [{enabled: true, available: true, model_config_id: 'one'}]} : {items: []})));
    await opening;
    assert.equal(loading.el('ai-verification-new').disabled, false);
    l.closeAIVerificationModal();

    const unavailable = setup();
    unavailable.context.fetch = async url => response(url.endsWith('/models')
        ? {models: [{enabled: true, available: false, model_config_id: 'missing-key'}, {enabled: true, model_config_id: 'unchecked'}]}
        : {items: []});
    await unavailable.context.openAIVerificationModal();
    assert.equal(unavailable.el('ai-verification-model').value, '', '缺密钥或未确认可用的模型不可默认选中');
    assert.equal(unavailable.el('ai-verification-model').children.length, 1);
    assert.match(unavailable.el('ai-verification-model').textContent, /没有可用模型/);
    assert.equal(unavailable.el('ai-verification-start').disabled, true);
    unavailable.context.closeAIVerificationModal();

    const realtime = setup(), rt = realtime.context;
    rt.fetch = async url => response(url.endsWith('/models') ? {models:[]}
        : url.endsWith('/plans') ? {items:[plan('realtime')]} : detail('realtime'));
    await rt.openAIVerificationModal();
    let resolveList;
    const liveDetails = [], liveRequests = [];
    rt.fetch = (url, options) => {
        liveRequests.push({url,options});
        return new Promise(resolve => {
            if (url.endsWith('/plans')) resolveList=resolve;
            else liveDetails.push(resolve);
        });
    };
    realtime.el('ai-verification-panel').scrollTop=240;
    realtime.el('ai-verification-trials').scrollLeft=48;
    const firstRefresh=rt.refreshAIVerificationReport();
    assert.equal(liveDetails.length,1,'慢计划列表不能阻止报告读取');
    await rt.refreshAIVerificationReport();
    assert.equal(liveRequests.length,2,'同一报告请求未结束时不重叠轮询');
    liveDetails[0](response({...detail('realtime'), report:{...detail('realtime').report,total_net_pnl:777}}));
    await firstRefresh;
    assert.match(realtime.el('ai-verification-metrics').textContent,/777.00/);
    assert.equal(realtime.el('ai-verification-panel').scrollTop,240);
    assert.equal(realtime.el('ai-verification-trials').scrollLeft,48);
    assert.match(realtime.el('ai-verification-refresh-status').textContent,/每 2 秒.*最近更新/);
    const secondRefresh=rt.refreshAIVerificationReport();
    assert.equal(liveRequests.length,3,'慢列表期间仍独立继续报告刷新，列表不重复发起');
    liveDetails[1](response({...detail('realtime'),report:{...detail('realtime').report,total_net_pnl:888}}));
    await secondRefresh;
    assert.match(realtime.el('ai-verification-metrics').textContent,/888.00/);
    assert.ok(liveRequests.every(({options})=>options.cache==='no-store'));
    resolveList(response({items:[plan('realtime')]}));
    rt.closeAIVerificationModal();

    const resumeUI = setup(), rc = resumeUI.context;
    await rc.openAIVerificationModal();
    const stopped = detail('stopped'); stopped.plan.status='STOPPED';
    rc.fetch = async (url,options={}) => { resumeUI.calls.push({url,options}); return response(options.method === 'POST' ? detail('stopped') : stopped); };
    await rc.selectAIVerificationPlan('stopped');
    assert.equal(resumeUI.el('ai-verification-resume').disabled,false);
    const originalPrompt=resumeUI.el('ai-verification-requirements').value;
    resumeUI.el('ai-verification-requirements').value='未保存的草稿';
    const beforeResume=resumeUI.calls.length;
    await rc.resumeAIVerificationPlan();
    assert.equal(resumeUI.calls.length,beforeResume,'未保存的提示词阻止重新启用');
    resumeUI.el('ai-verification-requirements').value=originalPrompt;
    await rc.resumeAIVerificationPlan();
    assert.ok(resumeUI.calls.at(-1).url.endsWith('/stopped/resume'));
    assert.equal(resumeUI.calls.at(-1).options.method,'POST');
    assert.equal(resumeUI.el('ai-verification-resume').disabled,true);
    assert.equal(resumeUI.el('ai-verification-requirements').readOnly,true,'重新启用即锁定');
    const completedPrompt=detail('stopped'); completedPrompt.plan.status='COMPLETED';
    rc.renderAIVerificationReport(completedPrompt);
    assert.equal(resumeUI.el('ai-verification-requirements').readOnly,true,'已完成保持只读');
    assert.match(resumeUI.el('ai-verification-message').textContent,/重新启用/);
    assert.equal(resumeUI.el('ai-verification-copy-restart').disabled,false);
    rc.fetch = async (url,options={}) => { resumeUI.calls.push({url,options}); return response(detail('copied-plan')); };
    await rc.copyRestartAIVerificationPlan();
    assert.ok(resumeUI.calls.at(-1).url.endsWith('/stopped/copy-restart'));
    assert.equal(resumeUI.calls.at(-1).options.method,'POST');
    assert.equal(resumeUI.el('ai-verification-copy-restart').disabled,true);
    assert.match(resumeUI.el('ai-verification-message').textContent,/原计划和历史账本保留/);
    const afterCopy = resumeUI.calls.length;
    await rc.copyRestartAIVerificationPlan();
    assert.equal(resumeUI.calls.length,afterCopy,'运行时不能重复复制');
    rc.closeAIVerificationModal();

    const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
    const scripts = [...html.matchAll(/<script src="\/js\/([^"]+)"/g)].map(match => match[1]);
    assert.equal(scripts.length, 15); assert.equal(scripts.at(-1), '15-market-simulator.js');
    assert.equal(scripts.at(-2), '14-ai-verification.js'); assert.equal(scripts.at(-3), '13-boot.js');
    assert.match(html, /启动后.*调用模型.*费用/);
    assert.match(html, /盘前展望也会画线/, '保留本聊天预先修改的说明');
    assert.match(html, /已关注且有实时新浪行情.*不自动订阅或回补/);
    console.log('AI verification: cost validation, frozen plans, safe DOM, GET-only open, stale requests, live guard and report export passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
