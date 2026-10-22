const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = require('./lib_index_source.cjs').readIndexSource();
for (const src of require('./lib_index_source.cjs').frontendJsSources()) new vm.Script(src);

const scaleCode = html.slice(
    html.indexOf('        function intradayAutoscale()'),
    html.indexOf('        function initChart()')
);
const scaleContext = {
    intradayBaseline: 100,
    intradayPriceMin: 90,
    intradayPriceMax: 110,
    intradayRange: { from: -1, to: 10 },
    intradayPoints: [{ time: 1, value: 90 }, { time: 5, value: 101 },
        { time: 6, value: 102 }, { time: 10, value: 110 }],
    logical: { from: -1, to: 10 },
    visible: { from: 5, to: 6 },
    aiSessionVisiblePrices: range => range ? [101.5] : [101.5],
};
scaleContext.chart = { timeScale: () => ({
    getVisibleLogicalRange: () => scaleContext.logical,
    getVisibleRange: () => scaleContext.visible,
}) };
vm.createContext(scaleContext);
vm.runInContext(scaleCode, scaleContext);
const normal = scaleContext.intradayAutoscale().priceRange;
assert.equal((normal.minValue + normal.maxValue) / 2, 100);
scaleContext.logical = { from: 4, to: 7 };
const zoomed = scaleContext.intradayAutoscale().priceRange;
assert.ok(zoomed.maxValue - zoomed.minValue < normal.maxValue - normal.minValue);
assert.ok(zoomed.minValue < 101 && zoomed.maxValue > 102);
assert.doesNotMatch(html, /intraday-zoom-in|changeIntradayZoom/);
assert.match(html, /chart\.applyOptions\(\{ handleScroll: true, handleScale: true \}\);\s*if \(subChart\) subChart\.applyOptions\(\{ handleScroll: true, handleScale: true \}\);\s*const nPts = pricePoints\.length/);
assert.match(html, /timeScale: \{ visible: false, fixRightEdge: false \}/);
assert.match(html, /timeScale: \{ visible: true, fixRightEdge: false \}/);
assert.match(html, /intradayRange = \{ from: -1, to: nPts - 1 \}/);

const renderCode = html.slice(
    html.indexOf('        function renderAISessionChartLines('),
    html.indexOf('        function selectAISessionChartRun(')
);
assert.match(renderCode, /currentPeriod === 'intraday'/);
assert.match(renderCode, /batch\.instrument === currentSymbol/);
assert.match(renderCode, /aiSessionChartDisplayActive\(batch\)/);
assert.match(renderCode, /series\.setData\(\[\{time:start, value:price\}, \{time:end, value:price\}\]\)/);
assert.match(renderCode, /getVisibleRange\(\)/);
assert.match(renderCode, /setVisibleRange\(visibleRange\)/);
assert.doesNotMatch(renderCode, /createPriceLine/);
assert.match(html, /setAISessionChartBatch\(batch\)/);
assert.match(html, /session_chart/);
assert.match(html, /id="ai-session-chart-btn"[\s\S]*?盘时段画线/);
assert.match(html, /id="ai-session-chart-btn"[^>]*onclick="openAISessionModal\(\)"/);
assert.match(html, /analysis\/history\?instrument=\$\{encodeURIComponent\(symbol\)\}&page_size=100/);
assert.match(html, /setAISessionChartBatches\(items, null, background\)/);
assert.match(html, /id="ai-session-line-legend"/);
assert.match(html, /id="ai-session-line-times"/);
assert.match(html, /id="ai-session-line-models"/);
assert.doesNotMatch(html, /id="ai-session-line-model"/);
assert.match(html, /基准线 .*分析冻结时的参考价/);
assert.match(html, /触发线 .*预测开始生效/);
assert.match(html, /目标线 .*预测目标命中/);
assert.match(html, /失效线 .*判断失效/);
assert.match(html, /当前状态：\$\{state\}/);
const rightPanelStart = html.indexOf('<div class="panel right-panel">');
const toolbarStart = html.indexOf('<div class="analysis-toolbar">', rightPanelStart);
const rightPanelTop = html.slice(rightPanelStart, toolbarStart);
assert.ok(rightPanelStart >= 0 && toolbarStart > rightPanelStart);
assert.match(rightPanelTop, /id="ai-session-line-control"/);
assert.match(rightPanelTop, /AI盘时段预测线说明/);
assert.match(rightPanelTop, /暂无有效盘时段预测/);
assert.match(rightPanelTop, /点击“盘时段画线”生成预测/);
assert.match(rightPanelTop, /display:none;[\s\S]*?id="ind-day-hl"/);
assert.doesNotMatch(rightPanelTop, /关键指标与支撑压力/);
const chartAreaStart = html.indexOf('id="main-chart-area"');
assert.ok(html.indexOf('id="ai-session-line-control"') > rightPanelStart);
assert.ok(html.indexOf('id="ai-session-line-control"') > chartAreaStart);

const choiceCode = html.slice(
    html.indexOf('        function updateAISessionChoiceArrows('),
    html.indexOf('        function activeAIBatch(')
);
assert.doesNotMatch(choiceCode, /scrollIntoView/);
const element = () => ({style:{}, children:[], scrollWidth:0, clientWidth:0, scrollLeft:0,
    replaceChildren(...items) { this.children = items; },
    scrollBy({left}) { this.scrollLeft += left; },
    querySelector(selector) { return this.children.find(item => item.className?.includes(selector.slice(1))); },
    getBoundingClientRect() { return this.rect || {left:0, right:100}; },
    setAttribute(name, value) { this[name] = value; }});
const times = element(), models = element(), modelList = element(), timeStrip = element();
const timePrev = element(), timeNext = element(), modelPrev = element(), modelNext = element();
const elements = {
    'ai-session-line-times':times, 'ai-session-line-models':models,
    'ai-session-line-model-list':modelList, 'ai-session-time-strip':timeStrip,
    'ai-session-line-times-prev':timePrev, 'ai-session-line-times-next':timeNext,
    'ai-session-line-model-list-prev':modelPrev, 'ai-session-line-model-list-next':modelNext,
};
const choiceContext = {
    currentSymbol: 'FU2611', currentPeriod:'intraday', intradayDate:null, aiSessionChartBatches: [],
    aiSessionChartBatch: null, aiSessionChartRunId: null,
    aiSessionChartRunByBatch: new Map(),
    document: { getElementById: id => elements[id], createElement: () => element() },
    requestAnimationFrame: callback => callback(),
    aiSessionDisplayActive: () => true, scheduleAISessionExpiry: () => {},
    clearAISessionChartLines: () => { throw new Error('valid choices must not be cleared'); },
    renderAISessionChartLines: () => choiceContext.renderAISessionChartChoices(),
};
vm.createContext(choiceContext);
const chartDisplayCode = html.slice(html.indexOf('        function aiSessionChartDisplayActive('),
    html.indexOf('        function aiSessionVisiblePrices('));
vm.runInContext(chartDisplayCode, choiceContext);
vm.runInContext(choiceCode, choiceContext);
const batch = (id, time) => ({
    batch_id:id, instrument:'FU2611', analysis_target:'CURRENT_SESSION',
    created_at:`2026-09-28T${time}`, snapshot_time:`2026-09-28T${time}`,
    session_target:{status:'ACTIVE', end_time:'2099-09-28T15:00:00'},
    runs:[{run_id:`run-${id}`, model_display_name:'同一模型', result:{session_chart:{reference_price:4000}}}],
});
const first = batch('first', '13:37:51'), second = batch('second', '13:40:33');
choiceContext.setAISessionChartBatches([first, second]);
assert.equal(choiceContext.aiSessionChartRunId, 'run-second');
assert.equal(times.children.length, 2);
assert.equal(times.children[0].textContent, '13:40:33');
assert.equal(timeStrip.style.display, 'flex');
assert.equal(models.style.display, 'flex');
assert.equal(modelList.children.length, 1);
assert.equal(modelList.children[0].textContent, '同一模型');
times.children[1].onclick();
assert.equal(choiceContext.aiSessionChartBatch.batch_id, 'first');
assert.equal(choiceContext.aiSessionChartRunId, 'run-first');
choiceContext.setAISessionChartBatch(second);
assert.equal(choiceContext.aiSessionChartRunId, 'run-first');
choiceContext.setAISessionChartBatch({...batch('pending', '13:41:00'), runs:[]});
assert.equal(choiceContext.aiSessionChartRunId, 'run-first');
choiceContext.setAISessionChartBatch(batch('third', '13:42:00'));
assert.equal(choiceContext.aiSessionChartRunId, 'run-third');
const multi = {...batch('multi', '13:45:00'), runs:[
    {run_id:'run-a', model_display_name:'ChatGPT', result:{session_chart:{reference_price:4000}}},
    {run_id:'run-b', model_display_name:'Gemini', result:{session_chart:{reference_price:4001}}},
]};
choiceContext.setAISessionChartBatch(multi);
assert.equal(times.children[0].textContent, '13:45:00');
assert.equal(models.style.display, 'flex');
assert.equal(modelList.children.length, 2);
modelList.children[1].onclick();
assert.equal(choiceContext.aiSessionChartRunId, 'run-b');
times.children[1].onclick();
times.children[0].onclick();
assert.equal(choiceContext.aiSessionChartRunId, 'run-b');
assert.equal(modelList.children[1]['aria-pressed'], 'true');
times.scrollWidth = 400; times.clientWidth = 150; times.scrollLeft = 0;
choiceContext.updateAISessionChoiceArrows('ai-session-line-times');
assert.equal(timePrev.hidden, false);
assert.equal(timePrev.disabled, true);
assert.equal(timeNext.disabled, false);
choiceContext.scrollAISessionChoices('ai-session-line-times', 1);
assert.ok(times.scrollLeft > 0);
times.scrollLeft = 250;
choiceContext.updateAISessionChoiceArrows('ai-session-line-times');
assert.equal(timeNext.disabled, true);
times.scrollWidth = 150; times.scrollLeft = 0;
choiceContext.updateAISessionChoiceArrows('ai-session-line-times');
assert.equal(timePrev.hidden, true);
assert.equal(timeNext.hidden, true);
const active = times.children.find(item => item.className.includes('is-active'));
times.rect = {left:0, right:100}; active.rect = {left:120, right:170};
choiceContext.keepAISessionChoiceVisible(times);
assert.equal(times.scrollLeft, 70);

const viewportCode = html.slice(
    html.indexOf('        function renderAISessionChartLines('),
    html.indexOf('        function updateAISessionChoiceArrows(')
);
const savedRange = {from:100, to:200}, rangeCalls = [], lineCalls = [];
const timeScale = {getVisibleRange:() => savedRange, setVisibleRange:r => rangeCalls.push(r)};
const series = {setData:data => lineCalls.push(data), applyOptions:() => {}};
const viewportContext = {
    chart:{timeScale:() => timeScale}, currentSymbol:'FU2611', currentPeriod:'intraday', intradayDate:null,
    aiSessionChartBatch:{instrument:'FU2611', analysis_target:'CURRENT_SESSION', session_target:{status:'ACTIVE', end_time:'2099-09-28T15:00:00'},
        runs:[{run_id:'model', result:{session_chart:{forecast_start_time:'start', forecast_end_time:'end',
            reference_price:4000, target_price:4020, invalidation_price:3990}}}]},
    aiSessionChartRunId:'model', aiChartWallTime:value => value === 'start' ? 100 : 200,
    aiSessionReferenceSeries:series, aiSessionTargetSeries:series,
    aiSessionInvalidationSeries:series, aiSessionTriggerSeries:series,
    intradayValuedTimes:new Set(), clearAISessionChartLines:() => {},
    aiSessionDisplayActive:() => true,
    renderAISessionLineLegend:() => {}, renderAISessionChartChoices:() => {},
    extendAISessionForecastTimeline:() => false,
};
vm.createContext(viewportContext);
vm.runInContext(chartDisplayCode, viewportContext);
vm.runInContext(viewportCode, viewportContext);
viewportContext.renderAISessionChartLines(true);
assert.equal(rangeCalls.length, 1);
assert.equal(rangeCalls[0], savedRange);
assert.equal(lineCalls.length, 3);

const displayCode = html.slice(
    html.indexOf('        function aiSessionDisplayActive('),
    html.indexOf('        function aiSessionVisiblePrices(')
);
const cutoff = Date.parse('2026-09-28T20:55:00');
const displayContext = {currentSymbol:'FU2611', currentPeriod:'intraday', intradayDate:null,
    Date:{parse:Date.parse, now:() => cutoff - 1}, clearTimeout:() => {}, setTimeout:() => {}};
vm.createContext(displayContext);
vm.runInContext(displayCode, displayContext);
const expiredAt = {analysis_target:'CURRENT_SESSION', display_until:'2026-09-28T20:55:00'};
assert.equal(displayContext.aiSessionDisplayActive(expiredAt), true);
displayContext.Date.now = () => cutoff;
assert.equal(displayContext.aiSessionDisplayActive(expiredAt), false);
const historical = {...expiredAt, instrument:'FU2611', trading_day:'20260928'};
assert.equal(displayContext.aiSessionChartDisplayActive(historical), false);
displayContext.intradayDate='20260928';
assert.equal(displayContext.aiSessionChartDisplayActive(historical), true);
assert.equal(displayContext.aiSessionChartDisplayActive({...historical,trading_day:'20260929'}), false);
assert.equal(displayContext.aiSessionChartDisplayActive({...historical,instrument:'BU2611'}), false);
// 历史日期的时间、模型选择只取当天，已过期线仍画在原预测区间。
choiceContext.intradayDate='20260928';
choiceContext.aiSessionDisplayActive=()=>false;
choiceContext.setAISessionChartBatches([
    {...first,trading_day:'20260928'}, {...second,trading_day:'20260929'}]);
assert.equal(times.children.length,1);
assert.equal(choiceContext.aiSessionChartBatch.batch_id,'first');
viewportContext.intradayDate='20260928';
viewportContext.aiSessionDisplayActive=()=>false;
viewportContext.aiSessionChartBatch.analysis_target='CURRENT_SESSION';
viewportContext.aiSessionChartBatch.trading_day='20260928';
lineCalls.length=0;
viewportContext.renderAISessionChartLines();
assert.equal(lineCalls.length,3);
assert.equal(lineCalls[0][0].time,100);
assert.equal(lineCalls[0][1].time,200);
const priceCode=html.slice(html.indexOf('        function aiSessionVisiblePrices('),
    html.indexOf('        function aiChartWallTime('));
vm.runInContext(priceCode,viewportContext);
assert.deepEqual(Array.from(viewportContext.aiSessionVisiblePrices()),[4000,4020,3990]);
viewportContext.intradayDate='20260929';
lineCalls.length=0;
viewportContext.renderAISessionChartLines();
assert.equal(lineCalls.length,0);
assert.equal(viewportContext.aiSessionVisiblePrices().length,0);

// 盘前线使用所选小节，不把盘前行情或下一小节算作已命中。
const stateCode = html.slice(html.indexOf('        function aiSessionLineState('),
    html.indexOf('        function renderAISessionLineLegend('));
const preOpenStart = Date.parse('2026-10-09T21:00:00Z') / 1000;
const preOpenEnd = preOpenStart + 7200;
const stateContext = {aiChartWallTime: value => Date.parse(value.slice(0,19) + 'Z') / 1000,
    Date:{now:() => Date.parse('2026-10-09T20:50:00+08:00')},
    intradayBars:[{bar_time:'2026-10-09T15:00:00', high_price:5000, low_price:3000}]};
vm.createContext(stateContext);
vm.runInContext(stateCode, stateContext);
const preOpenPlan = {kind:'PRE_OPEN', direction:'LONG', reference_price:4000,
    target_price:4020, invalidation_price:3990, trigger_price:null,
    forecast_start_time:'2026-10-09T21:00:00', forecast_end_time:'2026-10-09T23:00:00'};
assert.equal(stateContext.aiSessionLineState(preOpenPlan, preOpenStart), '盘前展望，等待开盘');
stateContext.Date.now = () => Date.parse('2026-10-10T09:00:00+08:00');
stateContext.intradayBars.push({bar_time:'2026-10-10T09:00:00', high_price:5000, low_price:3000});
assert.equal(stateContext.aiSessionLineState(preOpenPlan, preOpenStart), '时段已结束，未触及目标或失效线');

// 可选价位为空时不能误画 0 元线。
viewportContext.intradayDate = '20260928';
viewportContext.aiSessionChartBatch.runs[0].result.session_chart.trigger_price = null;
lineCalls.length = 0;
viewportContext.renderAISessionChartLines();
assert.equal(lineCalls.length, 3);
assert.ok(lineCalls.every(data => data.every(point => point.value > 0)));

const timelineCode = html.slice(html.indexOf('        function extendAISessionForecastTimeline('),
    html.indexOf('        function renderAISessionChartLines('));
const timelineUpdates = [];
const timelineContext = {intradaySeries:{setData: data => timelineUpdates.push(data)},
    intradayPoints:[{time:preOpenStart - 3600, value:4000}], intradayTickLabels:new Map(),
    intradayRange:{from:-1, to:0}};
vm.createContext(timelineContext);
vm.runInContext(timelineCode, timelineContext);
assert.equal(timelineContext.extendAISessionForecastTimeline(preOpenPlan, preOpenStart, preOpenEnd), true);
assert.equal(timelineContext.intradayPoints.length, 122);
assert.equal(timelineContext.intradayPoints.filter(point => point.value !== undefined).length, 1);
assert.equal(timelineContext.intradayTickLabels.get(preOpenStart), '21:00');
assert.equal(timelineContext.intradayTickLabels.get(preOpenEnd), '23:00');
assert.equal(timelineContext.intradayRange.to, 121, '拖动边界必须包含盘前预测的小节');
assert.equal(timelineContext.extendAISessionForecastTimeline(preOpenPlan, preOpenStart, preOpenEnd), false);
assert.equal(timelineUpdates.length, 1, '重复刷新不重复增加分钟槽');
// 用实际渲染器验证盘前四线均被画出，且全部限于开收盘窗口。
viewportContext.intradayDate = '20261012';
viewportContext.aiSessionChartBatch = {instrument:'FU2611', analysis_target:'CURRENT_SESSION',
    trading_day:'20261009', session_forecast:{kind:'PRE_OPEN',trading_day:'20261012'},
    runs:[{run_id:'model', result:{session_chart:{...preOpenPlan,trigger_price:4005}}}]};
viewportContext.aiChartWallTime = value => Date.parse(value.slice(0,19) + 'Z') / 1000;
viewportContext.intradaySeries = timelineContext.intradaySeries;
viewportContext.intradayPoints = [{time:preOpenStart - 3600,value:4000}];
viewportContext.intradayTickLabels = new Map();
viewportContext.intradayRange = {from:-1,to:0};
timeScale.fitContent = () => {};
vm.runInContext(timelineCode, viewportContext);
lineCalls.length = 0;
viewportContext.renderAISessionChartLines();
assert.equal(lineCalls.length,4);
assert.ok(lineCalls.every(data => data[0].time === preOpenStart && data[1].time === preOpenEnd));
displayContext.intradayDate = '20261012';
assert.equal(displayContext.aiSessionChartDisplayActive({...historical,
    session_forecast:{kind:'PRE_OPEN', trading_day:'20261012'}}), true);
displayContext.intradayDate = '20260928';
assert.equal(displayContext.aiSessionChartDisplayActive({...historical,
    session_forecast:{kind:'PRE_OPEN', trading_day:'20261012'}}), false);
console.log('AI current-session lines are bounded to the active intraday segment');

// 每个模型各自的秒级十分钟窗口；同柱先后未知不能伪造命中。
const wallCode = html.slice(html.indexOf('        function aiChartWallTime('), html.indexOf('        function clearAISessionChartLines('));
vm.runInContext(wallCode, viewportContext);
const tenStart = Date.parse('2026-10-13T09:16:23Z') / 1000;
const tenPlan = {direction:'LONG',window_policy:'RESULT_AVAILABLE_10M',reference_price:99,trigger_price:100,
    target_price:110,invalidation_price:90,forecast_start_time:'2026-10-13T09:16:23+08:00',forecast_end_time:'2026-10-13T09:26:23+08:00'};
viewportContext.aiSessionChartBatch = {instrument:'FU2611',analysis_target:'CURRENT_SESSION',trading_day:'20261013',
    runs:[{run_id:'model',result:{session_chart:tenPlan}}]};
viewportContext.intradayDate='20261013';
lineCalls.length=0;
viewportContext.renderAISessionChartLines(true);
assert.ok(lineCalls.every(data=>data[0].time===tenStart && data[1].time===tenStart+600));
assert.equal(rangeCalls.at(-1),savedRange);
stateContext.Date.now = ()=>Date.parse('2026-10-13T09:27:00+08:00');
const minute=(time,high,low)=>({bar_time:`2026-10-13T09:${time}:00`,high_price:high,low_price:low});
stateContext.intradayBars=[minute('16',120,80),minute('17',105,99),minute('18',111,101),minute('27',120,80)];
assert.equal(stateContext.aiSessionLineState(tenPlan,tenStart),'目标已命中');
stateContext.intradayBars=[minute('17',111,99)];
assert.match(stateContext.aiSessionLineState(tenPlan,tenStart),/同一分钟.*待核验/);
stateContext.intradayBars=Array.from({length:9},(_,i)=>minute(String(17+i),105,99));
stateContext.intradayBars.push(minute('26',120,80));
assert.match(stateContext.aiSessionLineState(tenPlan,tenStart),/窗口到期.*有效报价结算/);
stateContext.intradayBars=Array.from({length:9},(_,i)=>minute(String(17+i),99,95));
assert.equal(stateContext.aiSessionLineState(tenPlan,tenStart),'窗口到期，未触发');

stateContext.intradayBars=[minute('17',105,99)];
assert.match(stateContext.aiSessionLineState(tenPlan,tenStart),/尾部分钟行情缺失/);
