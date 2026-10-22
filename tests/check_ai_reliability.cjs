const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = require('./lib_index_source.cjs').readIndexSource();
for (const src of require('./lib_index_source.cjs').frontendJsSources()) new vm.Script(src);
class Element {
    constructor() { this.children = []; this.style = {}; this.textContent = ''; }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.append(child); }
    replaceChildren(...children) { this.children = children; }
    text() { return [this.textContent, ...this.children.map(child => child.text())].join('\n'); }
}
const elements = new Map();
const context = {currentPeriod:'5m', intradayDate:null, document: {
    getElementById: id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    createElement: () => new Element(),
}, fmtTradingDay: day => day, directionText: direction => direction, nextOpenDirectionText: direction => direction};
vm.createContext(context);
const start = html.indexOf('        function renderAIBatch(');
const end = html.indexOf('\n        async function ', start);
vm.runInContext(html.slice(start, end), context);
const run = {model_display_name: '测试模型', status: 'SUCCEEDED', direction: 'LONG',
    review_status: 'FAILED_DRAFT_RETAINED', review_error: '超时', used_review_summary_ids: [],
    review_summary_calls: [{call_type: 'SYNTHESIS', summary_ids: []}], result: {
        evidence: [{side: 'LONG', time: '2026-09-28T10:35:00', field: 'close', value: 3500, reason: '成交价证据'}],
        risks: ['波动风险'], entry_condition: '站稳3500', invalidation_condition: '跌破3480',
        consensus: {status: 'MAJORITY', agreed: 2, total: 3, valid: 3}}};
context.renderAIBatch({analysis_target: 'TODAY', instrument: 'RB2610', trading_day: '20260928',
    quote_time: '2026-09-28T10:36:00', quality: {market_state: 'TRADING', warnings: ['STALE_QUOTE']},
    use_review_summaries: true, planned_review_summary_count: 1, runs: [run, {...run, direction: 'SHORT'}]});
const text = elements.get('ai-result-list').text();
for (const expected of ['行情截至', '10:36:00', 'STALE_QUOTE', 'close=3500', '波动风险', '跌破3480',
    '复核失败，保留草稿', '总结因上下文限制未发送', '方向一致 2/3', '多模型方向分歧']) {
    assert.ok(text.includes(expected), `Missing visible text: ${expected}`);
}
console.log('AI reliability cards display evidence, risks, quality, review failure, actual usage and disagreement');
context.renderAIBatch({analysis_target:'TODAY', instrument:'FU2701', trading_day:'20261022',
    quality:{market_state:'TRADING', warnings:['UNVERIFIED_MINUTE_VOLUME'], unverified_volume_bars:[
        {instrument:'FU2701', period:'1m', bar_time:'2026-10-22T13:30:00', volume_quality:'UNALLOCATED_GAP'}]},
    runs:[{...run, review_status:'NOT_REQUESTED'}]});
const blockedText = elements.get('ai-result-list').text();
assert.ok(blockedText.includes('分钟成交量归属未核实'));
assert.ok(blockedText.includes('FU2701 1m 2026-10-22T13:30:00'));
assert.ok(blockedText.includes('UNALLOCATED_GAP'));
