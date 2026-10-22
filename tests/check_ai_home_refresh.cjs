const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../frontend/js/09-ai-analysis.js'), 'utf8');
const elements = new Map(), renders = [], requests = [], timers = [], chartUpdates = [];
const panel = {style:{display:'block'},scrollTop:240,closest(){return this;}};
const c = {
    currentSymbol:'FU2701',currentPeriod:'intraday',intradayDate:null,currentUser:{username:'sim'},isLiveMode:false,
    document:{hidden:false,activeElement:null,addEventListener(){},getElementById(id){
        if(id==='ai-analysis-results') return panel;
        if(!elements.has(id)) elements.set(id,{style:{},textContent:'',replaceChildren(){}});
        return elements.get(id);
    }},
    setInterval(fn,ms){timers.push({fn,ms});return timers.length;},
    activeAIBatch(){return null;}, aiSessionDisplayActive(batch){return batch.analysis_target==='CURRENT_SESSION';},
    clearAISessionChartLines(){throw new Error('Background refresh must keep existing lines while loading');},
    renderAIBatch(batch,target){assert.equal(target.preserveChart,true);renders.push(batch.batch_id);},
    setAISessionChartBatches(items,run,preserve){chartUpdates.push(preserve);},fmtTradingDay(value){return value;},
};
const data = id => ({current_trading_day:'20261012',items:[{batch_id:id,instrument:'FU2701',trading_day:'20261012',analysis_target:'CURRENT_SESSION'}]});
c.fetch = async (url,options)=>{requests.push({url,options});return {ok:true,json:async()=>data('first')};};
vm.createContext(c);
vm.runInContext(source.slice(source.indexOf('        let aiHomeRefreshTimer')), c);
(async()=>{
    c.startAIHomeAutoRefresh(); c.startAIHomeAutoRefresh();
    assert.equal(timers.length,1);assert.equal(timers[0].ms,2000);
    await c.refreshAIHomeResults();await c.refreshAIHomeResults();
    assert.deepEqual(renders,['first'],'Unchanged results must not redraw or reset controls');
    assert.equal(panel.scrollTop,240);
    assert.deepEqual(chartUpdates,[true],'Refresh must preserve the selected chart viewport');
    let resolve;
    c.fetch=(url,options)=>{requests.push({url,options});return new Promise(done=>{resolve=done;});};
    const waiting=c.refreshAIHomeResults();
    const count=requests.length;await c.refreshAIHomeResults();
    assert.equal(requests.length,count,'Do not overlap an unfinished refresh');
    assert.equal(panel.style.display,'block');
    resolve({ok:true,json:async()=>data('second')});await waiting;
    assert.deepEqual(renders,['first','second']);
    const switched=c.refreshAIHomeResults();c.currentSymbol='BU2611';
    resolve({ok:true,json:async()=>data('stale')});await switched;
    assert.equal(renders.length,2,'Old contract response must not overwrite new view');
    c.currentSymbol='FU2701';c.intradayDate='20261009';
    const historical=c.refreshAIHomeResults();
    assert.match(requests.at(-1).url,/trading_day=20261009/);
    c.intradayDate='20261012';resolve({ok:true,json:async()=>data('old-day')});await historical;
    assert.equal(renders.length,2,'Old viewed-day response must be discarded');
    c.document.hidden=true;const before=requests.length;await c.refreshAIHomeResults();
    assert.equal(requests.length,before);
    c.document.hidden=false;c.intradayDate=null;
    const accountRequest=c.refreshAIHomeResults();c.currentUser={username:'another'};
    resolve({ok:true,json:async()=>data('old-account')});await accountRequest;
    assert.equal(renders.length,2,'Old account response must not overwrite the current view');
    c.isLiveMode=true;const liveCount=requests.length;await c.refreshAIHomeResults();
    assert.equal(requests.length,liveCount);c.isLiveMode=false;
    c.fetch=async()=>({ok:false});await c.refreshAIHomeResults();
    assert.equal(panel.style.display,'block','Failed polling must keep previous results');
    assert.ok(requests.every(r=>r.options.cache==='no-store'));
    console.log('AI home refresh: 2s polling, no flicker, unchanged render, scroll, contract/day guards and hidden pause passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
