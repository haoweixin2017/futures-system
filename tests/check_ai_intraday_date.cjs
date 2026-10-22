const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = require('./lib_index_source.cjs').readIndexSource();
function extract(start, end) { return html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start))); }
async function main() {
    const elements = new Map(), calls = [], rendered = [], chartBatches = [];
    const context = {
        currentSymbol:'RB2610', currentPeriod:'intraday', intradayDate:'20260928',
        intradayDates:['20260928','20260929','20260930'], leftTab:'analysis',
        pendingFit:false, isLiveMode:false, activeAIBatch:()=>null,
        aiSessionDisplayActive:()=>false, setAISessionChartBatches:items=>chartBatches.push(items),
        clearAISessionChartLines:()=>{},
        loadIntraday:()=>{}, fmtTradingDay:day=>day,
        renderAIBatch:batch=>rendered.push(batch.batch_id),
        document:{getElementById:id=>{
            if (!elements.has(id)) elements.set(id,{style:{},textContent:'',closest(){return null;},replaceChildren(){ this.textContent=''; }});
            return elements.get(id);
        }},
        fetch:async url=>{ calls.push(url); return {ok:true,json:async()=>({
            current_trading_day:'20260930',items:[
                {batch_id:'old',trading_day:'20260928',analysis_target:'CURRENT_SESSION'},
                {batch_id:'current',trading_day:'20260930',analysis_target:'TODAY'}
            ]})}; }
    };
    vm.createContext(context);
    vm.runInContext(extract('        let aiHomeRefreshTimer', '        function parseServerTime('), context);
    await context.loadLatestAIResult('RB2610');
    assert.ok(calls[0].includes('trading_day=20260928'));
    assert.equal(rendered.at(-1),'old');
    assert.equal(chartBatches.length,1, 'historical records must refresh chart choices');
    const pending = [];
    context.fetch = url=>new Promise(resolve=>pending.push(resolve));
    const first = context.loadLatestAIResult('RB2610');
    context.intradayDate='20260929';
    pending.shift()({ok:true,json:async()=>({items:[{batch_id:'stale',trading_day:'20260928'}]})});
    await first;
    assert.ok(!rendered.includes('stale'));
    context.fetch=async()=>({ok:true,json:async()=>({items:[]})});
    await context.loadLatestAIResult('RB2610');
    assert.ok(elements.get('ai-result-list').textContent.includes('暂无'));
    vm.runInContext(extract('        function intradayPick(', '        async function loadIndicators('), context);
    let refreshed=0;
    context.loadLatestAIResult=()=>refreshed++;
    context.intradayPick('20260928'); context.intradayShift(1); context.intradayLatest();
    assert.equal(refreshed,3);
    // 真正的渲染器也必须拦住当前分析轮询覆盖历史，并允许过期历史记录展示。
    vm.runInContext(extract('        function renderAIBatch(', '        async function showAIRaw('), context);
    context.intradayDate='20260928';
    elements.get('ai-analysis-results').style.display='sentinel';
    context.renderAIBatch({trading_day:'20260930',analysis_target:'TODAY'});
    assert.equal(elements.get('ai-analysis-results').style.display,'sentinel');
    context.renderAIBatch({instrument:'RB2610',trading_day:'20260928',analysis_target:'CURRENT_SESSION',display_until:'expired',runs:[]});
    assert.equal(elements.get('ai-analysis-results').style.display,'block');
    elements.get('ai-analysis-results').style.display='sentinel';
    context.renderAIBatch({instrument:'RB2610',trading_day:'20260927',analysis_target:'CURRENT_SESSION',
        session_forecast:{kind:'PRE_OPEN',trading_day:'20260928'},display_until:'expired',runs:[]});
    assert.equal(elements.get('ai-analysis-results').style.display,'block', '夜盘盘前结果也按目标交易日展示');
    assert.match(elements.get('ai-batch-meta').textContent, /20260928/);
    // 按所选小节状态切换默认要求，只迁移默认文案，保留自定义内容。
    const storage = new Map();
    const storageKey = 'requirements-FU2611-CURRENT_SESSION';
    const legacyDefault = '只使用点击分析前、所选连续交易小节内已经发生的量价与持仓数据，预测从点击时刻到该小节结束的后续走势。给出偏多、偏空、中性或无法判断，并提供用于分时图的目标价、失效价和可选触发价。不得把点击前已经发生的涨跌当作预测结果，不得引用其他交易小节补足证据。';
    Object.assign(context, {currentAIAnalysisTarget:()=> 'CURRENT_SESSION',
        aiRequirementStorageKey:()=>storageKey,
        localStorage:{getItem:key=>storage.has(key)?storage.get(key):null, setItem:(key,value)=>storage.set(key,value)},
        loadAISessionOptions:()=>{}, refreshAIReviewOption:()=>{}, refreshAIInstructionPreview:()=>{},
        refreshAIModelAccuracyLabels:()=>{}});
    vm.runInContext(extract('        const aiAnalysisRequirementDefaults =', '        let aiSessionMode ='),context);
    vm.runInContext(extract('        function currentAISessionRequirementDefault(', '        function syncAISessionSelection('),context);
    vm.runInContext(extract('        function syncAISessionSelection(', '        function syncAIAnalysisTarget('),context);
    vm.runInContext(extract('        function syncAIAnalysisTarget(', '        // 历史详情面板:'),context);
    context.syncAIAnalysisTarget();
    const updatedDefault = elements.get('ai-analysis-requirements').value;
    assert.match(updatedDefault, /交易中.*所选.*小节/);
    assert.doesNotMatch(updatedDefault,/UPCOMING|尚未开盘|盘前参考价/);
    storage.set(storageKey,legacyDefault);
    context.syncAIAnalysisTarget();
    assert.equal(elements.get('ai-analysis-requirements').value,updatedDefault);
    assert.equal(storage.get(storageKey),updatedDefault,'迁移保存过的旧默认要求');
    const mixedLegacy = vm.runInContext('aiLegacySessionRequirements[0]',context);
    storage.set(storageKey,mixedLegacy);
    context.syncAIAnalysisTarget();
    assert.equal(elements.get('ai-analysis-requirements').value,updatedDefault,'迁移盘前盘中混合的旧默认要求');
    elements.set('ai-session-select',{options:[{value:'upcoming',dataset:{status:'UPCOMING'},textContent:'晚盘'}],selectedIndex:0});
    context.syncAISessionSelection();
    const upcomingDefault = elements.get('ai-analysis-requirements').value;
    assert.match(upcomingDefault,/尚未开盘.*开盘前.*历史/);
    assert.doesNotMatch(upcomingDefault,/ACTIVE|交易中|不引用其他小节补足盘中证据/);
    storage.set(storageKey,mixedLegacy);
    context.syncAIAnalysisTarget();
    assert.equal(elements.get('ai-analysis-requirements').value,upcomingDefault,'旧混合文案按所选未开盘状态迁移');
    elements.get('ai-session-select').options[0].dataset.status = 'ACTIVE';
    context.syncAISessionSelection();
    assert.equal(elements.get('ai-analysis-requirements').value,updatedDefault);
    storage.set(storageKey,'请重点说明成交量与持仓的限制，不自动交易。');
    context.syncAIAnalysisTarget();
    assert.equal(elements.get('ai-analysis-requirements').value,storage.get(storageKey),'保留自定义要求');
    elements.get('ai-session-select').options[0].dataset.status = 'UPCOMING';
    context.syncAISessionSelection();
    assert.equal(elements.get('ai-analysis-requirements').value,storage.get(storageKey),'切换小节也保留自定义要求');
    console.log('AI selected trading day, empty state, stale response, navigation and poll isolation passed');
}
main().catch(error=>{ console.error(error); process.exitCode=1; });
