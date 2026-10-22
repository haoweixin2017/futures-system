const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = require('./lib_index_source.cjs').readIndexSource();
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));

class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.style = {}; this.dataset = {}; this.textContent = ''; }
    setAttribute(name, value) { this[name] = value; }
    set innerHTML(_) { throw new Error('History must render model text safely'); }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.append(child); }
    replaceChildren(...children) { this.children = children; this.textContent = ''; }
    all() { return [this, ...this.children.flatMap(child => child.all())]; }
    text() { return [this.textContent, ...this.children.map(child => child.text())].join(''); }
}
const elements = new Map();
const context = {URL, currentSymbol:'RB2610', aiEvaluationStats:{}, document:{
    createElement:tag => new Element(tag),
    getElementById:id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
}};
vm.createContext(context);
vm.runInContext(extract('        function aiSessionHistoryState(', '        function refreshAIModelAccuracyLabels('), context);
vm.runInContext(extract('        function directionText(', '        function appendAIEvaluation('), context);
vm.runInContext(extract('        const aiHistoryDetailEl =', '        function newClientRequestId('), context);
const malicious = '<img src=x onerror=alert(1)>';
const base = {instrument:'RB2610', trading_day:'20261009', snapshot_time:'2026-10-08T21:15:00',
    created_at:'2026-10-08T21:15:01', status:'COMPLETED', model_count:1};
const future = {...base, analysis_target:'FUTURE_TREND', future_targets:[
    {horizon_days:3, trading_day:'20261014', reference_price:3500},
    {horizon_days:5, trading_day:'20261016', reference_price:3500},
    {horizon_days:7, trading_day:'20261020', reference_price:3500},
], runs:[{model_display_name:'模型 A', status:'SUCCEEDED', result:{
    horizon_forecasts:[
        {horizon_days:3, trading_day:'wrong-date', direction:'LONG', confidence_score:80, summary:'三日看多'},
        {horizon_days:5, trading_day:'wrong-date', direction:'SHORT', confidence_score:55, summary:'五日回落'},
        {horizon_days:7, direction:'NO_JUDGMENT', confidence_score:0, summary:'资料缺失'},
    ], future_metrics:{3:{rating:'CORRECT', realized_price:3520, delta:20},
        7:{rating:'UNVERIFIABLE', realized_price:null, delta:null}},
}}, {model_display_name:malicious, status:'SUCCEEDED', result:{horizon_forecasts:[
    {horizon_days:3, direction:'SHORT', confidence_score:60, summary:malicious},
]}}]};
function render(batch, target = batch.analysis_target) {
    context.batch = batch; context.historyTarget = target;
    vm.runInContext('aiHistoryItems = [batch]; aiHistoryTarget = historyTarget; renderAIHistory();', context);
    return elements.get('ai-history-list').text();
}

// Catches hidden horizon results, wrong dates from the model, and prematurely settled horizons.
let text = render(future);
for (const expected of ['2026-10-08 21:15:00', '模型 A', '2026年10月14日', '2026年10月16日',
    '2026年10月20日', '偏多', '偏空', '无法判断', '三日看多', '五日回落', '80/100',
    '3500', '3520', '+20', '正确', '待到期', '无法验证', malicious]) assert.ok(text.includes(expected), expected);
assert.doesNotMatch(text, /wrong-date|undefined|NaN/);
assert.equal(elements.get('ai-history-list').children.length, 1, 'Models remain grouped in one batch');

// Adopted materials follow each model's citations, rather than all retrieved sources.
const adopted = {...future, public_materials:{sources:[
    {source_id:'S1', title:'库存周报', source_name:'行业机构', dimension:'fundamentals',
        published_at:'2026-10-07', data_period:'2026年10月第1周', url:'https://example.com/report', excerpt:'库存减少十万吨'},
    {source_id:'S2', title:'宏观发布', dimension:'macro', url:'javascript:alert(1)'},
    {source_id:'UNUSED', title:'未被采用的资料', dimension:'events'},
], background_sources:[{source_id:'OLD', title:'过期库存', stale:true, dimension:'fundamentals'}]}, runs:[
    {...future.runs[0], result:{...future.runs[0].result, dimensions:{
        technical:{status:'AVAILABLE', summary:'日线站上均线', citations:[]},
        volume_oi:{status:'PARTIAL', summary:'持仓数据不完整', citations:[]},
        fundamentals:{status:'AVAILABLE', summary:'去库支撑价格', citations:[{source_id:'S1', quote:'库存减少十万吨'}]},
        macro:{status:'MISSING', summary:'最新宏观资料缺失', citations:[]},
    }}},
    {...future.runs[1], result:{...future.runs[1].result, dimensions:{
        macro:{status:'PARTIAL', summary:malicious, citations:[{source_id:'S2', quote:malicious}]},
        fundamentals:{status:'MISSING', summary:'只有旧资料', citations:[{source_id:'OLD', quote:'旧库存'}, {source_id:'UNKNOWN', quote:'无法匹配来源'}]},
    }}},
]};
text = render(adopted);
for (const expected of ['采用的信息', '技术', '量价持仓', '基本面', '宏观', '事件', '相关市场',
    '日线站上均线', '持仓数据不完整', '去库支撑价格', '库存周报', '行业机构',
    '2026-10-07', '2026年10月第1周', '库存减少十万吨', '最新宏观资料缺失',
    '缺失', '背景资料', '不能作为当前方向依据', '来源未记录', '资料期别：未知', malicious]) assert.ok(text.includes(expected), expected);
assert.ok(!text.includes('未被采用的资料'), 'Retrieved but uncited source is not presented as adopted');
const models = elements.get('ai-history-list').children[0].children[0].children.filter(n => n.className === 'ai-history-model');
const dimensions = models[0].all().filter(n => n.tag === 'details');
assert.equal(dimensions.length, 6, 'Each evidence dimension has its own disclosure');
assert.ok(dimensions.every(n => !n.open), 'Detailed evidence starts collapsed');
const dimensionLabels = dimensions.map(n => n.children[0].text()).join(' / ');
for (const label of ['技术 · 已用到', '量价持仓 · 部分用到', '基本面 · 已用到', '宏观 · 未用到']) assert.ok(dimensionLabels.includes(label), label);
assert.ok(!dimensionLabels.includes('日线站上均线'), 'Summary labels contain no detailed evidence');
assert.equal(dimensions[0].children[0].tag, 'summary');
assert.ok(dimensions[0].text().includes('日线站上均线'), 'Evidence remains available inside the disclosure');
assert.ok(models[0].text().includes('库存周报'));
assert.ok(!models[1].text().includes('库存周报'), 'Citation belongs only to the citing model');
const links = elements.get('ai-history-list').all().filter(n => n.tag === 'a');
assert.equal(links.length, 1, 'Unsafe URLs never become active links');
assert.equal(links[0].href, 'https://example.com/report');
assert.equal(links[0].rel, 'noopener noreferrer');
text = render(future);
assert.match(text, /采用信息未记录/);

// Catches losing the daily direction, levels, or independently calculated automatic rating.
text = render({...base, analysis_target:'TODAY', runs:[{model_display_name:'日线模型', status:'SUCCEEDED',
    direction:'LONG', confidence_score:70, evaluation:{rating:'WRONG', score:0}, result:{
        summary:'上涨条件已满足', support_levels:[3450], resistance_levels:[3550],
        settlement_metrics:{direction_rating:'CORRECT'},
    }}]});
for (const expected of ['2026年10月09日', '日线模型', '偏多', '70/100', '上涨条件已满足',
    '3450', '3550', '自动验证：正确', '记录评分：错误']) assert.ok(text.includes(expected), expected);

// Catches substituting the analysis day/time for the next opening's frozen target.
text = render({...base, analysis_target:'NEXT_OPEN', next_open_target:{trading_day:'20261012',
    start_time:'2026-10-09T21:00:00', session:'NIGHT', label:'下一交易日夜盘', open_time:'21:00'}, runs:[{
    model_display_name:'开盘模型', status:'SUCCEEDED', result:{next_open_forecast:{
        open_time:'wrong-time', direction:'DOWN', confidence_score:65, summary:'低开观察'},
        settlement_metrics:{direction_rating:'UNVERIFIABLE'}},
}]});
for (const expected of ['2026年10月12日', '2026-10-09 21:00', '夜盘', '低开倾向', '65/100',
    '低开观察', '无法验证']) assert.ok(text.includes(expected), expected);
assert.doesNotMatch(text, /wrong-time/);

// Catches hiding the session's target prices or conflating target hits with direction accuracy.
context.aiEvaluationStats = {session:{CURRENT_SESSION:{hit_rate:{percentage:50, hit_count:1, decided_count:2}}}};
text = render({...base, analysis_target:'CURRENT_SESSION', session_target:{label:'夜盘第一节',
    start_time:'2026-10-08T21:00:00', end_time:'2026-10-08T23:00:00'},
    session_forecast:{start_time:'2026-10-08T21:15:00', end_time:'2026-10-08T23:00:00'}, runs:[{
    model_config_id:'session', model_display_name:'盘时模型', status:'SUCCEEDED', direction:'SHORT', confidence_score:75,
    session_line_state:{status:'HIT'}, result:{summary:'短线回落', session_chart:{
        reference_price:3500, target_price:3480, invalidation_price:3525, trigger_price:3495},
        settlement_metrics:{direction_rating:'WRONG'}},
}]});
for (const expected of ['夜盘第一节', '21:00', '23:00', '偏空', '3500', '3480', '3525', '3495',
    '目标已命中', '50%', '1/2', '自动验证：错误']) assert.ok(text.includes(expected), expected);

assert.match(text, /交易时段：夜盘第一节 · 2026-10-08 21:00–2026-10-08 23:00/);
assert.match(text, /预测区间：2026-10-08 21:15–2026-10-08 23:00/);

// Real batch statuses differ from individual model-run statuses.
for (const [status, label] of [['CREATED', '等待执行'], ['COMPLETED', '已完成'], ['PARTIAL_FAILED', '部分失败']]) {
    text = render({...base, analysis_target:'TODAY', status, runs:[]});
    assert.ok(elements.get('ai-history-list').children[0].children[0].children[0].text().includes(label), status);
    if (status === 'CREATED') assert.match(text, /等待模型预测结果/);
}

// Catches old records, research-stage batches, and failed runs crashing or implying a prediction.
text = render({...base, analysis_target:'FUTURE_TREND', status:'RUNNING', runs:[],
    future_targets:future.future_targets});
assert.match(text, /2026年10月14日/);
assert.match(text, /等待模型/);
text = render({...future, status:'RUNNING', runs:[{model_display_name:'执行模型', status:'RUNNING', result:null}]});
assert.match(text, /2026年10月14日/);
assert.match(text, /等待模型/);
text = render({...base, analysis_target:'TODAY', runs:[{model_display_name:'失败模型', status:'TIMEOUT', error:'网关超时'}]});
assert.match(text, /网关超时/);
assert.doesNotMatch(text, /偏多|偏空/);
text = render({...base, snapshot_time:null, analysis_target:'FUTURE_TREND', runs:[{
    model_display_name:'旧模型', status:'UNSTRUCTURED', result:{horizon_forecasts:[
        {horizon_days:5, trading_day:'20261016', direction:'NEUTRAL', summary:'旧预测'},
    ]}}, {model_display_name:'空结果', status:'UNSTRUCTURED', result:{}}]});
assert.match(text, /目标日未记录/);
assert.match(text, /旧预测/);
assert.doesNotMatch(text, /undefined|NaN/);
console.log('AI history: frozen dates, per-model forecasts, partial settlement, four categories and legacy/error states passed');
