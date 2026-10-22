const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = require('./lib_index_source.cjs').readIndexSource();
const code = html.slice(html.indexOf('        function opponentPrice('), html.indexOf('        async function cancelOrder('));
(async () => {
    const modeElements = Object.fromEntries([
        'page-title', 'trade-panel-title', 'trade-mode-banner', 'auto-trade-panel',
        'ai-model-settings-btn', 'ai-history-btn', 'ai-analyze-btn', 'ai-session-chart-btn', 'ai-analysis-results',
        'emergency-close-btn'
    ].map(id => [id, {style:{}, innerText:'', innerHTML:''}]));
    const modeButtons = Array.from({length: 6}, () => ({style:{}, disabled:false}));
    const modeContext = {isLiveMode:true, document:{
        title:'', getElementById:id => modeElements[id], querySelectorAll:() => modeButtons
    }};
    const modeCode = html.slice(html.indexOf('        function applyTradingMode('), html.indexOf('        async function loadLiveStatus('));
    vm.createContext(modeContext); vm.runInContext(modeCode, modeContext); modeContext.applyTradingMode();
    assert.match(modeElements['page-title'].innerText, /实盘交易/);
    assert.match(modeElements['trade-panel-title'].innerText, /闪电交易/);
    assert.equal(modeElements['auto-trade-panel'].style.display, 'none');
    assert.ok(modeButtons.every(button => button.disabled));

    for (const direction of ['BUY', 'SELL']) for (const offset of ['OPEN', 'CLOSE']) {
        const posts = [], alerts = [];
        const context = { currentSymbol: 'SC2611', Date, Number, String, Error, parseInt, isNaN, encodeURIComponent,
            isLiveMode: false, liveReady: false, confirm: () => true,
            document: { getElementById: id => ({ value: id === 'trade-price-mode' ? 'limit' : id === 'trade-vol' ? '1' : '' }) },
            alert: x => alerts.push(x), loadTradeData: () => {},
            fetch: async (url, opts) => {
                if (opts) { posts.push(JSON.parse(opts.body)); return { ok: true, json: async () => ({status:'success', order:{status:'FILLED'}}) }; }
                context.currentSymbol = 'FU2611';
                return { ok: true, json: async () => ({timestamp: new Date().toISOString(), ask_price_1:'795.2', bid_price_1:'795.1'}) };
            }
        };
        vm.createContext(context); vm.runInContext(code, context);
        await context.quickOrder(direction, offset, true);
        assert.equal(alerts.length, 0);
        assert.equal(posts.length, 1);
        assert.equal(posts[0].instrument, 'SC2611');
        assert.equal(posts[0].price, direction === 'BUY' ? '795.2' : '795.1');
        assert.equal(posts[0].offset, offset);
        assert.throws(() => context.opponentPrice({ask_price_1:''}, 'BUY'), /暂无卖一/);
        posts.length = 0;
        context.fetch = async () => ({ok:true,json:async()=>({timestamp:'2020-01-01T00:00:00',ask_price_1:'1',bid_price_1:'1'})});
        await context.quickOrder(direction, offset, true);
        assert.equal(posts.length,0);
        assert.match(alerts[0],/过期/);
    }

    const livePosts = [], liveAlerts = [];
    const liveContext = { currentSymbol: 'FU2611', Date, Number, String, Error, encodeURIComponent,
        isLiveMode: true, liveReady: true, liveProfile: 'simnow', confirm: () => true,
        document: { getElementById: id => ({ value: id === 'trade-price-mode' ? 'opponent' : id === 'trade-vol' ? '1' : '' }) },
        alert: x => liveAlerts.push(String(x)), loadTradeData: () => {}, window: {location: {href: ''}},
        fetch: async (url, opts) => {
            if (!opts) return {ok:true,json:async()=>({timestamp:new Date().toISOString(),ask_price_1:'795.2',bid_price_1:'795.1'})};
            livePosts.push({url, body:JSON.parse(opts.body)});
            return {ok:true,json:async()=>({status:'success',order:{status:'SUBMITTED',order_ref:'LIVE-1'}})};
        }
    };
    vm.createContext(liveContext); vm.runInContext(code, liveContext);
    await liveContext.quickOrder('BUY', 'OPEN', true);
    assert.equal(livePosts.length, 1);
    assert.equal(livePosts[0].url, '/api/v1/trade/live/order');
    assert.equal(livePosts[0].body.price, '795.2');
    assert.match(liveAlerts[0], /实盘委托已发送/);

    livePosts.length = 0; liveAlerts.length = 0;
    liveContext.fetch = async (url, opts) => {
        if (!opts) return {ok:true,json:async()=>({timestamp:new Date().toISOString(),ask_price_1:'795.2',bid_price_1:'795.1'})};
        livePosts.push({url, body:JSON.parse(opts.body)});
        return {ok:true,json:async()=>({status:'rejected',order:{status:'UNKNOWN'}})};
    };
    await liveContext.quickOrder('BUY', 'OPEN', true);
    assert.match(liveAlerts[0], /严禁重复下单/);

    liveContext.liveReady = false;
    livePosts.length = 0; liveAlerts.length = 0;
    await liveContext.quickOrder('BUY', 'OPEN', true);
    assert.equal(livePosts.length, 0);
    assert.match(liveAlerts[0], /柜台尚未就绪/);

    console.log('Paper/live opponent orders, endpoint isolation, readiness gate and stale quote checks passed; no real orders sent');
})().catch(e => {console.error(e);process.exitCode=1;});
