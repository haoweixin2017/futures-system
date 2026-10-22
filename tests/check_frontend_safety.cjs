const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const indexHtml = require('./lib_index_source.cjs').readIndexSource();
const liveHtml = fs.readFileSync(path.join(__dirname, '../frontend/live.html'), 'utf8');

for (const html of [indexHtml, liveHtml]) {
    const helperCode = html.slice(
        html.indexOf('        function escapeHTML('),
        html.indexOf('        //', html.indexOf('        function safeInlineValue('))
    );
    const context = {String, encodeURIComponent};
    vm.createContext(context);
    vm.runInContext(helperCode, context);
    assert.equal(context.escapeHTML(`<img src=x onerror='boom'>`),
        '&lt;img src=x onerror=&#39;boom&#39;&gt;');
    assert.doesNotMatch(context.safeInlineValue(`x');alert(1)//`), /['"<>]/);
}

assert.match(indexHtml, /escapeHTML\(inst\.name \|\| mainLabel\(inst\.symbol\)\)/);
assert.match(indexHtml, /switchGroupTab\(decodeURIComponent\('\$\{encoded\}'\)\)/);
assert.match(indexHtml, /escapeHTML\(ev\.message\)/);
assert.match(indexHtml, /escapeHTML\(o\.reject_reason \|\| '未提供原因'\)/);
assert.match(liveHtml, /escapeHTML\(w\.name \|\| ''\)/);
assert.match(liveHtml, /cancelLiveOrder\(decodeURIComponent/);

const raceFunctions = [
    ['loadIntradayDates', 'function intradayPick'],
    ['loadSignal', 'let analysisBusy'],
    ['loadTape', 'async function loadBigOrders'],
    ['loadBigOrders', 'function connectWebSocket'],
];
for (const [name, next] of raceFunctions) {
    const code = indexHtml.slice(
        indexHtml.indexOf(`        async function ${name}(`),
        indexHtml.indexOf(`        ${next}`, indexHtml.indexOf(`        async function ${name}(`))
    );
    assert.match(code, /const requestedSymbol = currentSymbol/);
    assert.match(code, /requestedSymbol !== currentSymbol/);
}

assert.match(fs.readFileSync(path.join(__dirname, '../run.py'), 'utf8'), /host='127\.0\.0\.1'/);
const compose = fs.readFileSync(path.join(__dirname, '../docker-compose.yml'), 'utf8');
assert.match(compose, /127\.0\.0\.1:8000:8000/);
assert.doesNotMatch(compose, /- "6379:6379"|- "3306:3306"/);

console.log('Frontend output escaping, contract-switch guards, and local-only bindings passed');
