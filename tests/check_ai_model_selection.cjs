const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = require('./lib_index_source.cjs').readIndexSource();
const code = html.slice(
    html.indexOf('        async function openAIAnalysisModal('),
    html.indexOf('        function closeAIAnalysisModal(')
);
const targetCode = html.slice(
    html.indexOf('        let aiSessionMode = false;'),
    html.indexOf('        function aiRequirementStorageKey(')
);

function element() {
    return {
        children: [], style: { cssText: '' }, dataset: {},
        append(...children) { this.children.push(...children); },
        appendChild(child) { this.children.push(child); },
        replaceChildren(...children) { this.children = children; },
        set textContent(value) { this.children = []; this.text = value; },
    };
}

async function renderedModels(models, session = false) {
    const box = element();
    const elements = { 'ai-analysis-modal': element(), 'ai-analysis-models': box,
        'ai-analysis-message': element(), 'ai-session-modal': element(),
        'ai-analysis-card': element(), 'ai-modal-title': element(),
        'ai-analysis-targets': element(), 'ai-analysis-summary': element() };
    const radio = { checked:false, closest:() => element() };
    const context = {
        currentSymbol: 'RB2610', isLiveMode: false, activeAIBatch: () => false,
        document: {
            getElementById: id => elements[id],
            querySelector: () => radio,
            createElement: () => element(),
        },
        syncAIAnalysisTarget: () => {},
        fetchAIModels: async () => models,
        fetch:async () => ({ok:true,json:async () => ({today_available:true})}),
        fetchAIEvaluationStats: async () => {},
        refreshAIModelAccuracyLabels: () => {},
    };
    vm.createContext(context);
    vm.runInContext(targetCode, context);
    vm.runInContext(code, context);
    if (session) await context.openAISessionModal();
    else await context.openAIAnalysisModal();
    return {
        selected: box.children.filter(row => row.children[0]?.checked)
            .map(row => row.children[0].value),
        visible: box.children.map(row => row.children[0].value),
        message: box.text,
        target: context.currentAIAnalysisTarget(),
        modal: session ? elements['ai-session-modal'].style.display
            : elements['ai-analysis-modal'].style.display,
        choices: elements['ai-analysis-targets'].style.display,
    };
}

(async () => {
    const one = {model_config_id:'one', display_name:'模型一', model_name:'m1',
        enabled:true, available:true};
    const two = {model_config_id:'two', display_name:'模型二', model_name:'m2',
        enabled:true, available:true};
    assert.deepEqual((await renderedModels([one])).selected, ['one']);
    assert.deepEqual((await renderedModels([one, two])).selected, []);
    assert.deepEqual((await renderedModels([{...one, available:false}])).selected, []);
    const mixed = await renderedModels([one, {...two, enabled:false, available:false}]);
    assert.deepEqual(mixed.visible, ['one']);
    assert.deepEqual(mixed.selected, ['one']);
    const disabled = await renderedModels([{...one, enabled:false, available:false}]);
    assert.deepEqual(disabled.visible, []);
    assert.match(disabled.message, /暂无启用的模型/);
    const session = await renderedModels([one], true);
    assert.equal(session.target, 'CURRENT_SESSION');
    assert.equal(session.modal, 'flex');
    assert.equal(session.choices, 'none');
    const today = await renderedModels([one]);
    assert.equal(today.target, 'TODAY');
    assert.equal(today.choices, 'grid');
    const choices = html.slice(html.indexOf('id="ai-analysis-targets"'), html.indexOf('id="ai-session-picker"'));
    assert.match(choices, /value="TODAY"/);
    assert.match(choices, /value="NEXT_OPEN"/);
    assert.doesNotMatch(choices, /CURRENT_SESSION/);
    const submitCode = html.slice(
        html.indexOf('        async function startAIAnalysis()'),
        html.indexOf('        function scheduleAIPoll(')
    );
    for (const [sessionMode, expectedTarget, expectedSession] of [
        [false, 'NEXT_OPEN', null], [true, 'CURRENT_SESSION', '2026-09-24T21:00:00'],
    ]) {
        let sent;
        const fields = {
            'ai-analysis-requirements': {value:'分析要求'},
            'ai-session-select': {value:'2026-09-24T21:00:00'},
            'ai-analysis-message': {textContent:''},
            'ai-analysis-start': {disabled:false},
            'ai-use-review-summaries': {checked:false},
            'ai-self-consistency': {checked:false},
        };
        const submitContext = {
            currentSymbol:'RB2610', newClientRequestId:() => 'request-id',
            document:{
                querySelector: () => ({value:'NEXT_OPEN'}),
                querySelectorAll: () => [{value:'one'}],
                getElementById: id => fields[id],
            },
            fetch: async (_url, options) => {
                sent = JSON.parse(options.body);
                return {ok:true, json:async () => ({batch_id:'batch-id'})};
            },
            aiActiveBatches:new Map(), syncAIAnalysisButton:() => {},
            closeAIAnalysisModal:() => {}, renderAIBatch:() => {}, scheduleAIPoll:() => {},
        };
        vm.createContext(submitContext);
        vm.runInContext(targetCode, submitContext);
        vm.runInContext(`aiSessionMode = ${sessionMode}`, submitContext);
        vm.runInContext(submitCode, submitContext);
        await submitContext.startAIAnalysis();
        assert.equal(sent.analysis_target, expectedTarget);
        assert.equal(sent.session_start_time, expectedSession);
    }
    console.log('AI analysis and session drawing open separate target views');
})().catch(error => { console.error(error); process.exitCode = 1; });
