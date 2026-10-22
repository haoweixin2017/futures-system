const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../frontend/live.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);

const priceCode = html.slice(
    html.indexOf('        function opponentPrice('),
    html.indexOf('        // 加载实盘资金与持仓')
);
const orderCode = html.slice(
    html.indexOf('        async function liveOrder('),
    html.indexOf('        // 开关 1 (软)')
);

function buildContext({direction, mode = 'opponent', price = '', volume = '1', timestamp = new Date().toISOString()}) {
    const posts = [];
    const alerts = [];
    const values = {
        'live-symbol': 'FU2611',
        'live-vol': volume,
        'live-price': price,
        'live-price-mode': mode,
    };
    const context = {
        Date, Number, String, JSON, Error, encodeURIComponent,
        liveReady: true,
        document: { getElementById: id => ({value: values[id] || ''}) },
        alert: message => alerts.push(String(message)),
        confirm: () => true,
        loadLiveOrders: async () => {},
        loadLiveAccount: async () => {},
        fetch: async (url, options) => {
            if (!options) {
                return {
                    ok: true,
                    json: async () => ({timestamp, ask_price_1: '795.2', bid_price_1: '795.0'}),
                };
            }
            posts.push(JSON.parse(options.body));
            return {ok: true, json: async () => ({status: 'success', order: {status: 'SUBMITTED', order_ref: '1'}})};
        },
    };
    vm.createContext(context);
    vm.runInContext(priceCode + orderCode, context);
    return {context, posts, alerts, direction};
}

(async () => {
    for (const direction of ['BUY', 'SELL']) {
        const test = buildContext({direction});
        await test.context.liveOrder(direction, 'OPEN');
        assert.equal(test.alerts.length, 1);
        assert.match(test.alerts[0], /成功报送/);
        assert.equal(test.posts.length, 1);
        assert.equal(test.posts[0].price, direction === 'BUY' ? '795.2' : '795.0');
    }

    const stale = buildContext({direction: 'BUY', timestamp: '2020-01-01T00:00:00Z'});
    await stale.context.liveOrder('BUY', 'OPEN');
    assert.equal(stale.posts.length, 0);
    assert.match(stale.alerts[0], /报价已过期/);

    const badVolume = buildContext({direction: 'BUY', mode: 'limit', price: '795.2', volume: '1.5'});
    await badVolume.context.liveOrder('BUY', 'OPEN');
    assert.equal(badVolume.posts.length, 0);
    assert.match(badVolume.alerts[0], /整数手数/);

    const badPrice = buildContext({direction: 'BUY', mode: 'limit', price: '-1'});
    await badPrice.context.liveOrder('BUY', 'OPEN');
    assert.equal(badPrice.posts.length, 0);
    assert.match(badPrice.alerts[0], /有效委托价格/);

    console.log('Live page syntax, opponent quote semantics, freshness and form validation passed; no real orders sent');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
