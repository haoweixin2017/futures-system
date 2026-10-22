const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = require('./lib_index_source.cjs').readIndexSource();
const stateCode = html.slice(
    html.indexOf('        const aiActiveBatches = new Map();'),
    html.indexOf('        async function aiErrorMessage(')
);
const pollCode = html.slice(
    html.indexOf('        function scheduleAIPoll('),
    html.indexOf('        function directionText(')
);

const button = {disabled:false};
const rendered = [];
const timers = [];
const responses = {
    'batch-a': {batch_id:'batch-a', status:'COMPLETED'},
    'batch-b': {batch_id:'batch-b', status:'RUNNING'},
};
const context = {
    Map, Boolean, Error,
    document: {getElementById: id => id === 'ai-analyze-btn' ? button : {textContent:''}},
    renderAIBatch: batch => rendered.push(batch.batch_id),
    aiErrorMessage: async res => `HTTP ${res.status}`,
    fetch: async url => {
        const id = url.split('/').pop();
        return {ok:true, json:async () => responses[id]};
    },
    setTimeout: callback => { timers.push(callback); return timers.length; },
    clearTimeout: () => {},
};
vm.createContext(context);

(async () => {
    await vm.runInContext(`(async () => {
        let currentSymbol = 'A';
        ${stateCode}
        ${pollCode}
        aiActiveBatches.set('A', {batchId:'batch-a', batch:{batch_id:'batch-a'}, pollTimer:null});
        syncAIAnalysisButton();
        if (!document.getElementById('ai-analyze-btn').disabled) throw new Error('A should be disabled');
        currentSymbol = 'B';
        syncAIAnalysisButton();
        if (document.getElementById('ai-analyze-btn').disabled) throw new Error('B should be enabled');
        aiActiveBatches.set('B', {batchId:'batch-b', batch:{batch_id:'batch-b'}, pollTimer:null});
        syncAIAnalysisButton();
        if (!document.getElementById('ai-analyze-btn').disabled) throw new Error('B should be disabled after start');
        await pollAIBatch('A');
        if (aiActiveBatches.has('A')) throw new Error('completed A should be removed');
        if (!aiActiveBatches.has('B')) throw new Error('running B should remain active');
        await pollAIBatch('B');
    })()`, context);

    assert.deepEqual(rendered, ['batch-b']);
    assert.ok(timers.length >= 1, 'running contract should schedule its own next poll');
    assert.match(html, /openAIAnalysisModal\(\)[\s\S]*?activeAIBatch\(\)/);
    assert.match(html, /aiActiveBatches\.set\(symbol, \{batchId:batch\.batch_id/);
    console.log('AI analysis state is isolated per contract and concurrent polling passed');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
