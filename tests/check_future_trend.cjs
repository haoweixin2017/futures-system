const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = require('./lib_index_source.cjs').readIndexSource();
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
for (const src of require('./lib_index_source.cjs').frontendJsSources()) new vm.Script(src);
assert.doesNotMatch(extract('id="ai-analysis-targets"', 'id="ai-session-picker"'), /value="FUTURE_TREND"/);
assert.match(html, /id="future-trend-btn"[^>]*onclick="openFutureTrendModal\(\)"/);
assert.ok(html.indexOf('id="future-trend-panel"') < html.indexOf('id="ai-session-line-control"'));
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.dataset = {}; this.textContent = ''; }
    setAttribute(name, value) { this[name] = value; }
    set innerHTML(_) { throw new Error('External/model text must never use innerHTML'); }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.append(child); }
    replaceChildren(...children) { this.children = children; this.textContent = ''; }
    text() { return [this.textContent, ...this.children.map(child => child.text())].join('\n'); }
    all() { return [this, ...this.children.flatMap(child => child.all())]; }
}
const elements = new Map();
const context = {URL, setInterval:()=>1, clearInterval:()=>{}, isLiveMode:false, activeAIBatch:()=>null, appendAIEvaluation:()=>{}, appendAIConversation:()=>{}, currentSymbol:'RB2610', currentPeriod:'5m', intradayDate:null,
    document: {createElement:tag=>new Element(tag), querySelector:()=>({value:'TODAY'}),
        getElementById:id=>{ if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }},
    localStorage:{getItem:()=>null}, refreshAIReviewOption:()=>{}, refreshAIModelAccuracyLabels:()=>{},
    refreshAIInstructionPreview:()=>{}, fmtTradingDay:day=>day, directionText:direction=>direction};
vm.createContext(context);
vm.runInContext(extract('        function appendAIFutureEvidence(', '        function appendAIHistorySummary('), context);
vm.runInContext(extract('        const aiAnalysisRequirementDefaults =', '        function aiRequirementStorageKey('), context);
vm.runInContext(extract('        function aiRequirementStorageKey(', '        function aiReviewStorageKey('), context);
vm.runInContext('aiFutureMode = true;', context);
vm.runInContext(extract('        function syncAIAnalysisTarget(', '        // 历史详情面板'), context);
assert.equal(context.currentAIAnalysisTarget(), 'FUTURE_TREND');
context.syncAIAnalysisTarget();
assert.match(elements.get('ai-analysis-summary').textContent, /日线.*60.*15/);
assert.doesNotMatch(elements.get('ai-analysis-summary').textContent, /今日全部.*1 分钟/);
assert.match(elements.get('ai-analysis-requirements').value, /3.*5/);
assert.doesNotMatch(elements.get('ai-analysis-requirements').value, /第7|第15/);
assert.doesNotMatch(elements.get('ai-analysis-summary').textContent, /7 \/ 15/);
context.localStorage.getItem = () => '分别判断未来第3、第5、第7、第15个实际交易日';
context.syncAIAnalysisTarget();
assert.equal(elements.get('ai-analysis-requirements').value, '分别判断未来第3、第5个实际交易日');
context.localStorage.getItem = () => null;
vm.runInContext('aiFutureMode = false; aiSessionMode = true;', context);
assert.equal(context.currentAIAnalysisTarget(), 'CURRENT_SESSION');
assert.match(extract('        const aiHistoryTargets =', '        function newClientRequestId('), /key: 'FUTURE_TREND', label: '未来趋势'/);
vm.runInContext(extract('        function appendAIFutureMaterials(', '        async function showAIRaw('), context);
const malicious = '<img src=x onerror=alert(1)>';
const batch = {analysis_target:'FUTURE_TREND', instrument:'RB2610', trading_day:'20261001', status:'SUCCEEDED',
    future_targets:[{horizon_days:3,trading_day:'20261014',reference_price:3500,bar_time:'2026-10-01T15:00:00'},
        {horizon_days:5,trading_day:'20261016',reference_price:3500,bar_time:'2026-10-01T15:00:00'}],
    public_materials:{status:'PARTIAL', missing_dimensions:['macro'], errors:['资料抓取超时'], sources:[
        {source_id:'s1',title:malicious,url:'https://example.com/source',source_kind:'OFFICIAL',content_type:'EXCERPT',excerpt:malicious,stale:true},
        {source_id:'s2',title:'不安全链接',url:'javascript:alert(1)',content_type:'BODY',published_at:'2026-10-01',fetched_at:'2026-10-01T10:00:00',data_period:'2026年9月'}]},
    runs:[{model_display_name:'模型',status:'SUCCEEDED',direction:'LONG',confidence_score:70,
        result:{future_metrics:{3:{rating:'CORRECT',reference_price:3500,realized_price:3520,delta:20,max_rise_pct:2,max_fall_pct:-1,sample_count:25,sample_status:'RECORDED_BARS'}},
        summary:malicious,confidence_summary:malicious,horizon_forecasts:[
            {horizon_days:3,trading_day:'wrong-date',direction:'LONG',confidence_score:80,summary:'三日预测',entry_condition:'突破',invalidation_condition:'跌破'},
            {horizon_days:5,trading_day:'wrong-date',direction:'SHORT',confidence_score:50,summary:'五日预测'}],
            dimensions:{technical:{status:'AVAILABLE',summary:malicious,citations:[{source_id:'s1',quote:malicious}]},
                volume_oi:{status:'PARTIAL',summary:'持仓证据不完整'}, macro:{status:'MISSING'}},
            alternative_scenario:malicious,evidence:[],risks:[],consensus:{status:'HORIZON_MAJORITY',total:3,valid:3,directions:{3:'LONG',5:'SHORT'}}}}]};
context.renderAIBatch(batch);
const output = elements.get('future-trend-list');
// The right sidebar must expose usage without showing the evidence until expanded.
const sidebarDimensions = elements.get('future-trend-summary').all().filter(node => node.tag === 'details' && node.className === 'ai-history-dimension');
assert.equal(sidebarDimensions.length, 6);
assert.ok(sidebarDimensions.every(node => !node.open));
assert.deepEqual(sidebarDimensions.map(node => node.children[0].text().trim()), [
    '技术 · 已用到', '量价持仓 · 部分用到', '基本面 · 未用到', '宏观 · 未用到', '事件 · 未用到', '相关市场 · 未用到']);
assert.ok(sidebarDimensions.every(node => !node.children[0].text().includes(malicious)));
assert.ok(sidebarDimensions[0].text().includes(malicious));
assert.match(sidebarDimensions[0].text(), /背景资料.*不能作为当前方向依据/);
const sidebarLinks = sidebarDimensions[0].all().filter(node => node.tag === 'a');
assert.equal(sidebarLinks[0].href, 'https://example.com/source');
const materialDetails = output.all().find(node => node.tag === 'details' && node.dataset.publicMaterials);
assert.ok(materialDetails && !materialDetails.open, 'Full source information starts collapsed');
assert.match(elements.get('future-trend-summary').text(), /未来第 3 个交易日.*20261014/);
assert.match(elements.get('future-trend-summary').text(), /未来第 5 个交易日.*20261016/);
assert.ok(!elements.has('ai-result-list'), 'Future result must not occupy the general AI panel');
const text = output.text();
for (const expected of ['未来第3个交易日','未来第5个交易日','20261014','20261016','3500','三日预测','五日预测',
    'LONG','SHORT','技术','量价','基本面','宏观','事件','相关市场','未用到','替代情景',malicious,
    '发布：未知','抓取：未知','资料期别：未知','检索摘要','正文','陈旧','资料抓取超时','macro','3520','25','已记录行情']) {
    assert.ok(text.includes(expected), `Missing future trend text: ${expected}`);
}
assert.ok(!text.includes('undefined/'), 'Future consensus must not display a missing aggregate agreement count');
assert.ok(!text.includes('wrong-date'), 'Target dates must use the frozen batch targets');
const links = output.all().filter(node => node.tag === 'a');
assert.equal(links.length, 2);
assert.equal(links[0].href, 'https://example.com/source');
assert.equal(links[0].rel, 'noopener noreferrer');
assert.ok(text.includes('待到期'), 'Unsettled fifth horizon remains visible');
const backgroundList = new Element();
context.appendAIFutureMaterials(backgroundList, {public_materials:{status:'MISSING', sources:[],
    freshness_policy:{fundamentals_days:14}, missing_dimensions:['fundamentals'],
    background_sources:[{source_id:'S99',title:malicious,excerpt:'旧库存背景',stale:true}]}});
assert.match(backgroundList.text(), /14 天.*7 天/);
assert.match(backgroundList.text(), /不能作为当前方向依据/);
assert.match(backgroundList.text(), /缺少近期可用公开资料/);
const collapsed = backgroundList.all().find(n=>n.tag==='details');
assert.ok(collapsed, 'Old materials must be in a collapsed background section');
assert.ok(!collapsed.open);
assert.match(collapsed.text(), /旧库存背景/);
const modelBatch = {...batch, runs:[
    {...batch.runs[0], model_config_id:'A', model_display_name:'Model A'},
    {...batch.runs[0], model_config_id:'B', model_display_name:'Model B', result:{...batch.runs[0].result,
        horizon_forecasts:batch.runs[0].result.horizon_forecasts.map(f=>({...f, summary:'B forecast'}))}}
]};
context.renderFutureTrendBatch(modelBatch);
let modelButtons = elements.get('future-trend-summary').all().filter(n=>n.tag==='button');
assert.equal(modelButtons.length, 2, 'Multiple models need buttons');
assert.equal(modelButtons[0]['aria-pressed'], 'true');
assert.equal(modelButtons[1]['aria-pressed'], 'false');
assert.ok(!elements.get('future-trend-summary').all().some(n=>n.tag==='select'));
assert.doesNotMatch(elements.get('future-trend-list').text(), /Model B|B forecast/);
modelButtons[1].onclick();
modelButtons = elements.get('future-trend-summary').all().filter(n=>n.tag==='button');
assert.equal(modelButtons[1]['aria-pressed'], 'true');
assert.equal(modelButtons[0]['aria-pressed'], 'false');
assert.match(elements.get('future-trend-summary').text(), /B forecast/);
assert.doesNotMatch(elements.get('future-trend-list').text(), /Model A/);
context.renderFutureTrendBatch(modelBatch);
assert.match(elements.get('future-trend-summary').text(), /B forecast/, 'Polling preserves the selected model');
context.renderFutureTrendBatch({...modelBatch, instrument:'FU2611'});
assert.doesNotMatch(elements.get('future-trend-summary').text(), /B forecast/);
context.renderFutureTrendBatch(batch);

const progressBatch = {...modelBatch, batch_id:'research-live', status:'RUNNING', public_materials:{status:'MISSING', sources:[],
    research:{status:'RUNNING', model_display_name:'Research Model', model_name:'actual-model', provider:'test-provider',
        rounds:2, search_count:2, pages_read:1, events:[
            {type:'SEARCH',query:malicious,status:'SUCCEEDED'},
            {type:'READ',title:'正文读取',url:'https://example.com/read',status:'SUCCEEDED'},
            {type:'READ',title:malicious,url:'javascript:alert(1)',status:'FAILED'},
            {type:'MODEL',message:'模型输出不等于工具执行'}]}}};
context.renderFutureTrendBatch(progressBatch);
assert.match(elements.get('future-trend-summary').text(), /检索中.*Research Model.*actual-model/);
assert.match(elements.get('future-trend-summary').text(), /搜索 2 次.*阅读 1 页/);
assert.match(elements.get('future-trend-summary').text(), /最近工具动作.*正文读取|最近工具动作.*<img/);
assert.match(elements.get('future-trend-list').text(), /公开资料检索进行中/);
assert.doesNotMatch(elements.get('future-trend-list').text(), /缺少近期可用公开资料/);
let processDetails = elements.get('future-trend-list').all().find(n=>n.tag==='details' && n.dataset.researchProcess);
assert.ok(processDetails && !processDetails.open, 'Tool process is collapsed by default');
const visibleText = node => [node.textContent, ...(node.tag === 'details' && !node.open
    ? node.children.slice(0, 1) : node.children).map(visibleText)].join('\n');
const completedResearch = {...progressBatch, public_materials:{...progressBatch.public_materials,
    research:{...progressBatch.public_materials.research, status:'COMPLETED'}}};
context.renderFutureTrendBatch(completedResearch);
let overview = elements.get('future-trend-summary').all().find(n => n.dataset.researchOverview);
assert.ok(overview && !overview.open, 'Completed research starts collapsed');
assert.match(visibleText(elements.get('future-trend-summary')), /检索完成/);
assert.doesNotMatch(visibleText(elements.get('future-trend-summary')), /Research Model|最近工具动作|搜索 2 次/);
overview.open = true; overview.ontoggle();
assert.match(overview.children[0].textContent, /点击收起/);
assert.match(visibleText(elements.get('future-trend-summary')), /Research Model.*actual-model/);
assert.match(visibleText(elements.get('future-trend-summary')), /最近工具动作/);
context.renderFutureTrendBatch(completedResearch);
assert.ok(elements.get('future-trend-summary').all().find(n => n.dataset.researchOverview).open, 'Polling keeps the user-expanded overview');
context.renderFutureTrendBatch({...completedResearch, batch_id:'new-research'});
assert.ok(!elements.get('future-trend-summary').all().find(n => n.dataset.researchOverview).open, 'A new research batch starts collapsed');
context.renderFutureTrendBatch({...batch, runs:[{status:'UNSTRUCTURED', error:'Citation does not support this dimension'}]});
assert.match(elements.get('future-trend-summary').text(), /引用维度不匹配.*未形成有效预测/);
assert.doesNotMatch(elements.get('future-trend-summary').text(), /Citation does not support/);
context.renderFutureTrendBatch(progressBatch);
assert.match(visibleText(elements.get('future-trend-summary')), /Research Model.*actual-model/);
processDetails = elements.get('future-trend-list').all().find(n=>n.tag==='details' && n.dataset.researchProcess);
assert.ok(processDetails.text().includes(malicious));
assert.equal(processDetails.all().filter(n=>n.tag==='a').length, 1, 'Unsafe event links remain plain text');
processDetails.open = true; processDetails.ontoggle();
context.renderFutureTrendBatch(progressBatch);
assert.ok(elements.get('future-trend-list').all().find(n=>n.dataset.researchProcess).open, 'Polling preserves expanded process');
elements.get('future-trend-summary').all().filter(n=>n.tag==='button')[1].onclick();
assert.ok(elements.get('future-trend-list').all().find(n=>n.dataset.researchProcess).open, 'Model switching preserves expanded process');
context.renderFutureTrendBatch({...progressBatch,batch_id:'research-next',runs:[],future_targets:[3,5,7,15].map(days=>({horizon_days:days,trading_day:'20261028'})),public_materials:{status:'MISSING',sources:[],research:{status:'PENDING',search_count:0,pages_read:0,events:[]}}});
assert.match(elements.get('future-trend-summary').text(), /等待检索/);
for (const days of [3,5,7,15]) assert.ok(elements.get('future-trend-summary').text().includes(`未来第 ${days} 个交易日`), 'Frozen horizon cards visible before model runs exist');
assert.match(elements.get('future-trend-list').text(), /缺失维度：待检索确认/);
assert.ok(!elements.get('future-trend-list').all().find(n=>n.dataset.researchProcess).open, 'New batch starts collapsed');
context.renderFutureTrendBatch({...progressBatch,public_materials:{status:'MISSING',sources:[],research:{status:'UNAVAILABLE',search_count:0,pages_read:0,events:[{type:'MODEL',message:'我已搜索'}]}}});
assert.match(elements.get('future-trend-summary').text(), /检索不可用/);
assert.match(elements.get('future-trend-list').text(), /未执行公开搜索或网页阅读.*外部资料缺失/);
assert.doesNotMatch(elements.get('future-trend-summary').text(), /最近工具动作/);
const unsafeSourceList = new Element();
context.appendAIFutureMaterials(unsafeSourceList, {public_materials:{sources:[
    {url:'https://user:secret@example.com/a',title:'凭证链接'}, {url:'http://example.com/a',title:'明文链接'}]}});
assert.equal(unsafeSourceList.all().filter(n=>n.tag==='a').length, 0);
context.renderFutureTrendBatch(batch);

const fourDayBatch = {...batch,
    future_targets:[3,5,7,15].map(days=>({horizon_days:days,trading_day:'20261028'})),
    runs:[{...batch.runs[0], result:{...batch.runs[0].result,
        horizon_forecasts:[3,5,7,15].map(days=>({horizon_days:days,direction:'LONG',
            confidence_score:60,summary:`${days} day independent forecast`}))}}]};
context.renderFutureTrendBatch(fourDayBatch);
for (const days of [3,5,7,15]) {
    assert.ok(elements.get('future-trend-summary').text().includes(`未来第 ${days} 个交易日`));
    assert.ok(elements.get('future-trend-list').text().includes(`${days} day independent forecast`));
}
const fallbackBatch = {...modelBatch, runs:[
    {...modelBatch.runs[0], result:null, status:'UNSTRUCTURED'}, modelBatch.runs[1]]};
context.renderFutureTrendBatch({...fallbackBatch, instrument:'OTHER'});
assert.match(elements.get('future-trend-summary').text(), /B forecast/, 'Default to first available result');
context.renderFutureTrendBatch(batch);

async function checkFlow() {
    context.showAIAnalysisModal = async modal => { modal.style.display='flex'; };
    vm.runInContext(extract('        async function openFutureTrendModal(', '        async function showAIAnalysisModal('), context);
    await context.openFutureTrendModal();
    assert.equal(elements.get('ai-modal-title').textContent,'未来趋势预测');
    assert.equal(elements.get('ai-analysis-targets').style.display,'none');
    assert.equal(context.currentAIAnalysisTarget(),'FUTURE_TREND');
    vm.runInContext('aiFutureMode = false; aiSessionMode = false;', context);
    assert.equal(context.currentAIAnalysisTarget(),'TODAY');
    // Durations come from each prediction rather than a fixed three/five label.
    const custom = {...batch, future_targets:[{horizon_days:7,trading_day:'20261020'}],
        runs:[{model_display_name:'模型', result:{horizon_forecasts:[{horizon_days:7,direction:'LONG',summary:'七日判断'}]}}]};
    context.renderFutureTrendBatch(custom);
    assert.match(elements.get('future-trend-summary').text(), /未来第 7 个交易日/);
    assert.doesNotMatch(elements.get('future-trend-summary').text(), /未来第 [35] 个交易日/);
    context.currentPeriod = 'intraday'; context.intradayDate = '20260901';
    context.renderAIBatch(batch);
    assert.match(elements.get('future-trend-summary').text(), /三日预测/);
    context.currentPeriod = '5m'; context.intradayDate = null;
    vm.runInContext('aiFutureTrendModels.clear();', context);
    const older = {...modelBatch, batch_id:'old-models', created_at:'2026-10-01T15:00:00', snapshot_time:'2026-10-01T15:00:00'};
    const newer = {...modelBatch, batch_id:'new-B', created_at:'2026-10-02T15:00:00', snapshot_time:'2026-10-02T15:00:00',
        future_targets:[{horizon_days:3,trading_day:'20261021'}],
        public_materials:{sources:[{source_id:'s1',title:'B new source',url:'https://example.com/new',excerpt:'B new source'}]},
        runs:[{...modelBatch.runs[1],run_id:'B-new',result:{...modelBatch.runs[1].result,
            horizon_forecasts:[{horizon_days:3,direction:'SHORT',summary:'B latest forecast'}]}}]};
    let requestedPages = [];
    context.fetch = async url => { requestedPages.push(url); return {ok:true,json:async()=>({items:[newer,older]})}; };
    await context.loadLatestFutureTrend('RB2610');
    assert.equal(requestedPages.length,1);
    assert.ok(requestedPages.every(url=>!url.includes('trading_day=')), 'Future forecast should survive date navigation');
    let buttons = elements.get('future-trend-summary').all().filter(n=>n.tag==='button');
    assert.equal(buttons.length,2, 'Models absent from the latest batch keep their own latest forecast');
    buttons.find(n=>n.textContent==='Model B').onclick();
    assert.match(elements.get('future-trend-summary').text(), /2026-10-02 15:00:00.*20261021.*B latest forecast/s);
    assert.match(elements.get('future-trend-list').text(), /B new source/);
    buttons = elements.get('future-trend-summary').all().filter(n=>n.tag==='button');
    buttons.find(n=>n.textContent==='Model A').onclick();
    assert.match(elements.get('future-trend-summary').text(), /2026-10-01 15:00:00.*20261014.*三日预测/s);
    assert.doesNotMatch(elements.get('future-trend-list').text(), /B new source|B latest forecast/);
    context.renderAIBatch({...newer,runs:[{...newer.runs[0],status:'RUNNING',result:null}]});
    assert.match(elements.get('future-trend-summary').text(), /三日预测/, 'Polling another model preserves the selection');
    elements.get('future-trend-summary').all().find(n=>n.tag==='button' && n.textContent==='Model B').onclick();
    assert.match(elements.get('future-trend-summary').text(), /预测中/);
    context.renderAIBatch({...newer,runs:[{...newer.runs[0],status:'UNSTRUCTURED',result:null,error:'latest model failure'}]});
    assert.match(elements.get('future-trend-summary').text(), /latest model failure/);
    context.renderAIBatch(older);
    assert.match(elements.get('future-trend-summary').text(), /latest model failure/, 'An old batch must not replace a newer failure');
    let resolveHistory;
    context.activeAIBatch = () => ({batch:newer});
    context.fetch = () => new Promise(resolve=>{ resolveHistory=resolve; });
    const activeLoad = context.loadLatestFutureTrend('RB2610');
    context.renderAIBatch({...newer,runs:[{...newer.runs[0],result:{...newer.runs[0].result,
        horizon_forecasts:[{horizon_days:3,direction:'LONG',summary:'new polling result'}]}}]});
    resolveHistory({ok:true,json:async()=>({items:[{...newer,runs:[{...newer.runs[0],status:'PENDING',result:null}]},older]})});
    context.activeAIBatch = () => null;
    await activeLoad;
    assert.match(elements.get('future-trend-summary').text(), /new polling result/, 'History loading must not overwrite a newer polling update');
    let finish;
    context.fetch = () => new Promise(resolve=>{ finish=resolve; });
    const stale = context.loadLatestFutureTrend('RB2610');
    context.currentSymbol = 'FU2611';
    elements.get('future-trend-summary').textContent = '新合约';
    finish({ok:true,json:async()=>({items:[batch]})});
    await stale;
    assert.equal(elements.get('future-trend-summary').textContent,'新合约');
    context.currentSymbol = 'RB2610';

    context.aiHistoryDetailEl = new Element();
    context.fetchAIEvaluationStats = async () => {};
    let historyRequests = 0;
    context.fetch = async () => { historyRequests++; return {ok:true, json:async()=>({current_trading_day:'20261001', items:[
        {...batch,snapshot_time:'2026-10-01T15:00:00',model_count:1},
        {analysis_target:'TODAY',trading_day:'20261001',snapshot_time:'2026-10-01T14:00:00',model_count:1,status:'SUCCEEDED'}]})}; };
    vm.runInContext(extract('        const aiHistoryTargets =', '        function newClientRequestId('), context);
    vm.runInContext('aiFutureMode = true; aiSessionMode = false;', context);
    await context.openAIHistoryModal();
    let historyButtons = elements.get('ai-history-categories').children;
    assert.deepEqual(historyButtons.map(button=>button.textContent), ['今日走势（1）','盘时段（0）','明日开盘（0）','未来趋势（1）']);
    assert.equal(historyButtons[3]['aria-pressed'], 'true', 'History opens at the current analysis type');
    assert.match(elements.get('ai-history-list').text(), /15:00:00/);
    assert.doesNotMatch(elements.get('ai-history-list').text(), /14:00:00/);
    context.aiHistoryDetailEl.style.display = 'block';
    historyButtons[0].onclick();
    assert.equal(context.aiHistoryDetailEl.style.display, 'none', 'Category switching closes the previous detail');
    assert.match(elements.get('ai-history-list').text(), /14:00:00/);
    assert.doesNotMatch(elements.get('ai-history-list').text(), /15:00:00/);
    assert.equal(elements.get('ai-history-categories').children[0]['aria-pressed'], 'true');
    elements.get('ai-history-categories').children[1].onclick();
    assert.match(elements.get('ai-history-list').text(), /暂无盘时段分析记录/);
    assert.equal(elements.get('ai-history-categories').children.length, 4, 'Empty categories keep all category buttons');
    assert.equal(historyRequests, 1, 'Category switching reuses the loaded history');
    elements.get('ai-history-today-only').checked = true;
    await context.loadAIHistory();
    assert.equal(elements.get('ai-history-categories').children[1]['aria-pressed'], 'true', 'Date filtering keeps the selected category');
    context.document.querySelectorAll = () => [{value:'model-id'}];
    elements.get('ai-analysis-requirements').value = '分别分析3日和5日';
    context.aiActiveBatches = new Map();
    context.newClientRequestId = () => 'future-request';
    context.syncAIAnalysisButton = () => {};
    context.closeAIAnalysisModal = () => {};
    context.scheduleAIPoll = () => {};
    let request;
    context.fetch = async (_url, options) => {
        request = JSON.parse(options.body);
        assert.match(elements.get('ai-analysis-message').textContent, /检索公开资料/);
        return {ok:true,json:async()=>({...batch,batch_id:'future-batch'})};
    };
    vm.runInContext('aiFutureMode = true; aiSessionMode = false;', context);
    vm.runInContext(extract('        async function startAIAnalysis(', '        function scheduleAIPoll('), context);
    await context.startAIAnalysis();
    assert.equal(request.analysis_target, 'FUTURE_TREND');
    assert.equal(request.session_start_time, null);
    assert.equal(request.analysis_requirements, '分别分析3日和5日');
    assert.ok(context.aiActiveBatches.has('RB2610'));
    console.log('Future trend submit, history category switching, labels, frozen dual forecasts, dimensions, safe sources and partial settlement passed');
}
checkFlow().catch(error=>{ console.error(error); process.exitCode=1; });
