const assert = require('node:assert/strict');
const {SimulatorController, applyPreset} = require('../frontend/js/15-market-simulator.js');
const deferred = () => {let resolve; const promise = new Promise(r => resolve=r); return {promise,resolve};};
const snapshot = id => ({snapshot_id:id, instrument:'JM2701', initial_comparison:{tag:'initial'}});
(async () => {
  let context = {instrument:'JM2701', account:'alice'};
  let calls=[], renders=[];
  const controller = new SimulatorController({context:()=>context, render:s=>renders.push({...s}),
    request:(path,body)=>{const d=deferred(); calls.push({path,body,...d}); return d.promise;}});
  const opening=controller.open(); calls[0].resolve(snapshot('one')); await opening;
  assert.equal(controller.state.snapshot.snapshot_id,'one');
  controller.schedule({simulate_price:'old-draft'});
  const resetting=controller.calculate({use_defaults:true});
  calls.at(-1).resolve({snapshot_id:'one',client_sequence:controller.sequence,tag:'reset'}); await resetting;
  const countAfterReset=calls.length;
  await new Promise(r=>setTimeout(r,350));
  assert.equal(calls.length,countAfterReset,'reset must cancel queued input');
  assert.deepEqual(applyPreset({simulate_price:'130',volume_ratio:'3',open_interest_change:9},
    {code:'LOW_VOLUME',price:null,volume_ratio:'0.7'}),
    {simulate_price:'130',volume_ratio:'0.7',open_interest_change:9});
  for(let i=0;i<12;i++) {
    const old=controller.calculate({simulate_price:'100',volume_ratio:'3',open_interest_change:0});
    const newer=controller.calculate({simulate_price:'102',volume_ratio:'3',open_interest_change:1});
    calls.at(-1).resolve({snapshot_id:'one',client_sequence:controller.sequence,tag:'new'}); await newer;
    calls.at(-2).resolve({snapshot_id:'one',client_sequence:controller.sequence-1,tag:'old'}); await old;
    assert.equal(controller.state.result.tag,'new');
  }
  const pending=controller.calculate({}); context={...context,instrument:'FU2701'};
  calls.at(-1).resolve({snapshot_id:'one',client_sequence:controller.sequence,tag:'wrong-symbol'}); await pending;
  assert.notEqual(controller.state.result.tag,'wrong-symbol');
  context={instrument:'JM2701',account:'alice'};
  const slow=controller.calculate({}); controller.close();
  calls.at(-2).resolve({snapshot_id:'one',tag:'closed'}); await slow;
  assert.equal(controller.state.status,'CLOSED');
  assert.equal(controller.explicitReference, null);
  controller.selectReference('run','batch'); assert.equal(controller.referenceForContext(),'run');
  context={instrument:'JM2701',account:'bob'}; assert.equal(controller.referenceForContext(),null);
  console.log('market simulator behavior checks passed');
})().catch(e=>{console.error(e);process.exit(1);});

// Exercise the actual DOM branch, fetch adapter, buttons and local lifecycle.
const fs = require('node:fs'), vm = require('node:vm');
function harness() {
  const elements=new Map(), calls=[], intervals=[];
  function element() {return {value:'',textContent:'',disabled:false,hidden:false,scrollTop:0,children:[],events:{},
    append(...v){this.children.push(...v)},replaceChildren(...v){this.children=v},
    addEventListener(name,fn){this.events[name]=fn}};}
  const el=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)};
  const document={getElementById:el,createElement:element,addEventListener(){}};
  const env={document,currentSymbol:'JM2701',currentUser:{username:'alice',role:'sim'},
    setInterval(fn){intervals.push(fn)},setTimeout,clearTimeout,AbortController,AbortSignal,Date,console,window:{},
    fetch:(url,options)=>{const d=deferred();calls.push({url,options,...d});return d.promise}};
  vm.runInNewContext(fs.readFileSync(require.resolve('../frontend/js/15-market-simulator.js'),'utf8'),env);
  return {env,c:env.window.marketSimulator,calls,intervals,el:id=>el('market-simulator-'+id)};
}
const settle=()=>new Promise(r=>setImmediate(r));
function fixture() {
  const side={score:35,conditions:[]};
  const result={snapshot_id:'one',client_sequence:0,inputs:{use_defaults:true},effective_values:{close_price:'100',volume:1200,open_interest:1000},
    current:{price:'100',volume_ratio:'1.2',oi_change:0,long:side,short:side,state:'NEUTRAL',reward_risk:{ratio:null,reason_code:'NO_REFERENCE_PLAN'}},
    simulated:{price:'100',volume_ratio:'1.2',oi_change:0,long:side,short:side,state:'NEUTRAL',reward_risk:{ratio:null,reason_code:'NO_REFERENCE_PLAN'}},
    changes:[],score_changes:{long:0,short:0},conclusion:{text:'仅为假设'},baseline_stale:false};
  return {snapshot_id:'one',instrument:'JM2701',trading_day:'20261019',quote_at:'2026-10-19T09:17:00+08:00',frozen_at:'2026-10-19T09:17:00+08:00',
    defaults:{simulate_price:'100',volume_ratio:'1.2',open_interest_change:0},
    constraints:{slider_available:false,slider_min:null,slider_max:null,slider_reason:'ATR缺失',tick:'0.5',min_effective_volume_ratio:'1.2'},
    presets:[{code:'HIGH_BREAK',label:'前高一跳',available:true,price:'102',volume_ratio:null}],history_cutoffs:{},quality:{warnings:[]},initial_comparison:result};
}
function respond(call,status,data) {call.resolve({status,ok:status>=200&&status<300,json:async()=>data});}
async function domChecks() {
  const fractionalOI=harness(),decimalDefaults=fixture();
  decimalDefaults.defaults.open_interest_change='-45.000';
  const decimalOpening=fractionalOI.c.open();respond(fractionalOI.calls.at(-1),200,{data:decimalDefaults});await decimalOpening;
  assert.equal(fractionalOI.el('oi').value,'-45','integer OI serialized as Decimal must remain editable');
  fractionalOI.c.close();respond(fractionalOI.calls.at(-1),204,{});await settle();
  const h=harness(),s=fixture();
  const opening=h.c.open();respond(h.calls.at(-1),200,{data:s});await opening;
  assert.equal(h.el('price-range').disabled,true);
  h.c.state.status='COMPUTING';h.c.emit();
  assert.equal(h.el('price-range').disabled,true,'ATR unavailable remains disabled on repeated render');
  h.el('volume').value='1.1996';h.el('presets').children[0].onclick();
  await new Promise(r=>setTimeout(r,330));
  const preset=h.calls.at(-1);assert(preset.url.endsWith('/simulate'),'rounded valid preset reaches server');
  assert.equal(h.el('price').value,'100','draft is not committed before validation');
  respond(preset,200,{data:{...s.initial_comparison,client_sequence:JSON.parse(preset.options.body).client_sequence}});await settle();
  assert.equal(h.el('price').value,'102');assert.equal(h.el('oi').value,0);
  h.el('volume').value='1.1994';h.el('presets').children[0].onclick();await new Promise(r=>setTimeout(r,330));
  const rejected=h.calls.at(-1);respond(rejected,422,{detail:{message:'成交量不足',fields:{observed_volume:1200,minimum_ratio:'1.2'},context:null}});await settle();
  assert.equal(h.el('volume').value,'1.1994');assert(h.el('error').textContent.includes('1200'));
  assert(h.el('error').textContent.includes('1.1994'),'error identifies submitted inputs');
  h.c.selectReference('new-run','new-batch');
  assert(h.el('results').children.some(n=>n.textContent.includes('参考选择已变化')),'reference change prompts update');
  h.el('price').value='105';h.el('drawer').scrollTop=77;h.c.emit();assert.equal(h.el('price').value,'105');assert.equal(h.el('drawer').scrollTop,77);
  h.c.close();respond(h.calls.at(-1),404,{});await settle();assert.equal(h.el('drawer').hidden,true);
  const failing=h.c.open();respond(h.calls.at(-1),422,{detail:{message:'休市',context:{stage:'CREATE',instrument:'JM2701',frozen_at:null}}});await failing;
  assert(h.el('meta').textContent.includes('尚未建立'));assert(h.el('meta').textContent.includes('15'));
  // Errors from a different account/generation cannot overwrite current state.
  const pending=h.c.open();h.env.currentUser={username:'bob',role:'sim'};h.c.close();
  respond(h.calls.findLast(v=>v.url.endsWith('/snapshots')),401,{detail:'请登录'});await pending;assert.equal(h.c.state.status,'CLOSED');
  console.log('market simulator DOM checks passed');
}
domChecks().catch(e=>{console.error(e);process.exitCode=1;});

async function lifecycleChecks() {
  const h=harness(),s=fixture();
  s.constraints.slider_available=true;s.constraints.slider_min='90';s.constraints.slider_max='110';
  s.quality={warnings:['合成测试'],sequences:{current:{count:1,providers:['sina'],sources:['synthetic_fixture'],label_semantics:['START'],coverage:['PARTIAL']}},evaluation_reasons:[{message:'当前持仓未知'}]};
  s.reference_plan={run_id:'old-run',source:'CURRENT_SESSION',source_timeframe:'RESULT_AVAILABLE_10M',direction:'LONG',target:'1320',stop:'1270',completed_at:s.frozen_at,valid_from:s.frozen_at,valid_until:'2026-10-19T09:27:00+08:00'};
  h.c.selectReference('old-run','batch');
  let opening=h.c.open();respond(h.calls.at(-1),200,{data:s});await opening;
  assert(h.el('meta').textContent.includes('PARTIAL'));assert(h.el('meta').textContent.includes('当前持仓未知'));
  assert(h.el('results').children.some(n=>n.textContent.includes('有效')));
  const count=h.calls.length;
  for(const price of ['101','102','103']) {h.el('price').value=price;h.el('price').events.input();}
  assert.equal(h.calls.length,count,'debounce waits before HTTP');
  await new Promise(r=>setTimeout(r,330));assert.equal(h.calls.length,count+1);
  const body=JSON.parse(h.calls.at(-1).options.body);assert.equal(body.simulate_price,'103');
  respond(h.calls.at(-1),422,{detail:{message:'隔离422',fields:{observed_volume:1200,minimum_ratio:'1.2',secret:'must-not-render'},context:{stage:'SIMULATE',instrument:'JM2701',snapshot_id:'one',client_sequence:body.client_sequence,frozen_at:s.frozen_at}}});await settle();
  assert(h.el('error').textContent.includes('已授权基准冻结'));
  assert(!h.el('error').textContent.includes('must-not-render'));
  assert(h.el('results').children.some(n=>n.textContent.includes('上次成功对应')));
  h.c.clearReference();assert(h.el('results').children.some(n=>n.textContent.includes('参考选择已变化')));
  // Missing response context still uses the local input and never confirms authorization.
  const auth=h.c.calculate({simulate_price:'104',volume_ratio:'1.3',open_interest_change:1});
  respond(h.calls.at(-1),403,{detail:{message:'无权',context:{stage:'SIMULATE',instrument:'JM2701',snapshot_id:'one',frozen_at:s.frozen_at}}});await auth;
  assert(h.el('error').textContent.includes('仅关联本地提交'));assert(!h.el('error').textContent.includes('已授权基准冻结'));
  // A slow calculation/error after update cannot replace the newer snapshot.
  const old=h.c.calculate({simulate_price:'105',volume_ratio:'1.3',open_interest_change:0});const oldCall=h.calls.at(-1);
  opening=h.c.open();const newSnapshot={...s,snapshot_id:'two'};respond(h.calls.at(-1),200,{data:newSnapshot});await opening;
  respond(oldCall,422,{detail:{message:'old-generation-error'}});await old;assert.equal(h.c.state.status,'READY');assert.equal(h.c.state.snapshot.snapshot_id,'two');
  h.c.close();respond(h.calls.at(-1),204,{});await settle();
  // Reset restores defaults and cancels queued drafts through the real button.
  opening=h.c.open();respond(h.calls.at(-1),200,{data:s});await opening;
  h.el('price').value='109';h.el('price').events.input();h.el('reset').events.click();
  assert.equal(h.el('price').value,'100');
  const reset=h.calls.at(-1),resetBody=JSON.parse(reset.options.body);assert.equal(resetBody.use_defaults,true);assert.equal(resetBody.simulate_price,undefined);
  respond(reset,200,{data:{...s.initial_comparison,client_sequence:resetBody.client_sequence}});await settle();
  const resetCount=h.calls.length;await new Promise(r=>setTimeout(r,330));assert.equal(h.calls.length,resetCount);
  h.env.currentUser={username:'bob',role:'sim'};h.intervals[0]();assert.equal(h.c.state.status,'CLOSED');respond(h.calls.at(-1),204,{});await settle();
  console.log('market simulator DOM lifecycle checks passed');
}
lifecycleChecks().catch(e=>{console.error(e);process.exitCode=1;});
