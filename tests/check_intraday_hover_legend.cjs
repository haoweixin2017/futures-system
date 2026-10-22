const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = require('./lib_index_source.cjs').readIndexSource();
const code = [
    html.slice(html.indexOf('        function updateSubIndicatorLegend('),
        html.indexOf('        function switchSubIndicator(')),
    html.slice(html.indexOf('        function buildOhlcIndex('),
        html.indexOf('        function periodName(')),
    html.slice(html.indexOf('        function renderOhlcLegend('),
        html.indexOf('        function onSearchInput(')),
].join('\n');
const element = () => ({children:[], textContent:'', innerText:'', style:{},
    appendChild(child) { this.children.push(child); },
    replaceChildren() { this.children = []; }});
const legend = element(), subValues = element();
const context = {
    currentSymbol:'RB2610', currentPeriod:'intraday', hoveredOhlcTime:null, ohlcHoverKey:'',
    ohlcList:[], ohlcMap:{}, subValueMap:{},
    document:{getElementById:id => id === 'ohlc-legend' ? legend : subValues,
        createElement:() => element()},
    getSubIndicatorTitle:() => '成交量', periodName:() => '分时', priceDecOf:() => 0,
    fmtPct:value => `${value.toFixed(2)}%`,
};
vm.createContext(context);
vm.runInContext(code, context);
const bars = [
    {bar_time:'2026-09-28T14:15:00', trading_day:'20260928', open_price:'100',
        high_price:'101', low_price:'99', close_price:'100', volume:10, open_interest:50},
    {bar_time:'2026-09-28T14:16:00', trading_day:'20260928', open_price:'100',
        high_price:'103', low_price:'100', close_price:'102', volume:20, open_interest:52},
];
const firstTime = Date.parse(bars[0].bar_time + 'Z') / 1000;
const field = name => legend.children.find(item => item.textContent === `${name} `)?.children[0]?.textContent;
context.buildOhlcIndex(bars);
context.renderOhlcLegend(firstTime);
assert.equal(field('时间'), '09-28 14:15');
assert.equal(field('收'), '100');
bars[1].close_price = '104';
context.buildOhlcIndex(bars);
context.renderOhlcLegend();
assert.equal(field('时间'), '09-28 14:15');
assert.equal(field('收'), '100');
context.renderOhlcLegend(undefined);
assert.equal(field('时间'), '09-28 14:16');
assert.equal(field('收'), '104');
context.renderOhlcLegend(firstTime);
context.buildOhlcIndex([{...bars[1], trading_day:'20260929'}]);
assert.equal(context.hoveredOhlcTime, null);
// 2026-10-02 十字线重构后 pointerleave 在箭头回调中清除悬停状态
assert.match(html, /container\.addEventListener\('pointerleave', \(\) => \{[^}]*clearChartHover\(\)/);
assert.match(html, /Date\.now\(\) - lastChartPointerMoveAt > 150/);
console.log('Intraday hover legend stays on the selected minute during live refresh');
