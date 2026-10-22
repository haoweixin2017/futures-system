const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = require('./lib_index_source.cjs').readIndexSource();
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.style = {}; this.dataset = {}; this.value = ''; this.checked = false; this._text = ''; this.open = false; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text; }
    set innerHTML(_) { throw new Error('External text must use textContent'); }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.append(child); }
    replaceChildren(...children) { this.children = children; this._text = ''; }
    setAttribute(name, value) { this[name] = value; }
    text() { return [this._text, ...this.children.map(child => child.text())].join('\n'); }
    all() { return [this, ...this.children.flatMap(child => child.all())]; }
}
const visibleText = node => [node.textContent, ...(node.tag === 'details' && !node.open ? node.children.slice(0, 1) : node.children).map(visibleText)].join('\n');
const elements = new Map();
const el = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const calls = [];
let serverSettings = {enabled:false, ai_enabled:false, model_config_id:null, interval_minutes:60, contracts:[], config_version:0};
let status = {settings:serverSettings, topics:[], contracts:[], channel_policy:[{channel_id:'CZCE',enabled:false,reason:'入口拒绝访问'}], materials:{cache_state:'EMPTY', sources:[], missing_dimensions:['fundamentals','macro','events','related_markets']}, model_status:{available:false, reason:'未配置研究模型'}};
let conflict = false;
const malicious = '<img src=x onerror=alert(1)>';
const context = {URL, URLSearchParams, currentSymbol:'FU2611', isLiveMode:false, watchlist:[{symbol:'FU2611'},{symbol:'BU2611'}],
    document:{getElementById:el,createElement:tag=>new Element(tag)}, setTimeout:()=>1, clearTimeout:()=>{},
    fetchAIModels:async()=>[{model_config_id:'research',display_name:malicious,model_name:'r',enabled:true,available:true}],
    aiErrorMessage:async response=>(await response.json()).detail,
    fetch:async(url, options = {})=>{
        calls.push({url,options});
        if (url.includes('/universe?')) return {ok:true,json:async()=>({contracts:[{symbol:'SC2611',name:malicious},{symbol:'FU0',name:'连续'}]})};
        if (url.endsWith('/settings') && options.method === 'PUT') {
            if (conflict) return {ok:false,status:409,json:async()=>({detail:'版本冲突'})};
            serverSettings = {...JSON.parse(options.body),config_version:1}; status.settings = serverSettings;
            return {ok:true,json:async()=>serverSettings};
        }
        if (url.endsWith('/settings')) return {ok:true,json:async()=>serverSettings};
        if (url.includes('/status')) return {ok:true,json:async()=>status};
        if (url.includes('/materials/')) return {ok:true,json:async()=>({material_id:'v1',body:malicious,published_at:'2026-10-01',version_fetched_at:'2026-10-02'})};
        if (url.endsWith('/refresh')) return {ok:true,json:async()=>({topics:[]})};
        throw new Error('Unexpected request ' + url);
    }};
vm.createContext(context);
const settingsCode = extract('        let aiResearchSettings =', '        const aiAnalysisRequirementDefaults =');
vm.runInContext(settingsCode, context);

async function check() {
    assert.equal(typeof context.openAIResearchModal, 'function', '资料采集 settings must expose working UI');
    await context.openAIResearchModal();
    assert.equal(el('ai-research-modal').style.display, 'flex');
    assert.equal(el('ai-research-enabled').checked, false);
    assert.equal(el('ai-research-ai-enabled').checked, false, 'AI assistance defaults to off');
    assert.equal(Number(el('ai-research-interval').value), 60);
    assert.equal(el('ai-research-model').value, '', 'Research model must not borrow the selected prediction model');
    assert.match(el('ai-research-model').text(), /<img/);
    context.importAIResearchWatchlist();
    context.watchlist.push({symbol:'RB2610'});
    assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(aiResearchDraft.contracts)',context)), [
        {instrument:'FU2611',related:[]},{instrument:'BU2611',related:[]}]);
    el('ai-research-search').value = '原油';
    await context.searchAIResearchContracts();
    assert.match(calls.at(-1).url, /universe\?search=/);
    assert.equal(el('ai-research-search-results').all().filter(n=>n.tag==='input').length, 1, 'Only named delivery contracts may be added');
    assert.match(el('ai-research-search-results').text(), /SC2611/);
    const choice = el('ai-research-search-results').all().find(n=>n.tag==='input');
    choice.checked = true; choice.onchange();
    el('ai-research-search-results').all().find(n=>n.tag==='button').onclick();
    // setAIResearchRelated 已随 f6e82d5 移除关联合约参考功能一并下线
    const before = vm.runInContext('JSON.stringify(aiResearchDraft.contracts)',context);
    assert.ok(!calls.some(call=>['PUT','POST'].includes(call.options.method)), 'Unsaved edits must never save or enqueue');
    await context.refreshAIResearch('FU2611');
    assert.ok(!calls.some(call=>call.url.endsWith('/refresh')), 'An unsaved group cannot be refreshed');

    // 开启定时采集 + AI 辅助：需要研究模型，ai_enabled 显式提交
    el('ai-research-model').value = 'research';
    el('ai-research-enabled').checked = true;
    el('ai-research-ai-enabled').checked = true;
    await context.saveAIResearchSettings();
    const put = calls.find(call=>call.options.method==='PUT');
    const saved = JSON.parse(put.options.body);
    assert.equal(saved.config_version,0);
    assert.equal(saved.model_config_id,'research');
    assert.equal(saved.enabled,true);
    assert.equal(saved.ai_enabled,true, 'AI switch must be submitted explicitly');
    await context.refreshAIResearch('FU2611');
    assert.deepEqual(JSON.parse(calls.find(call=>call.url.endsWith('/refresh')).options.body),{instrument:'FU2611'});
    conflict = true;
    await context.saveAIResearchSettings();
    assert.match(el('ai-research-message').text(), /版本.*刷新/);
    conflict = false;

    // 无研究模型的固定模式可直接保存（AI 关闭）
    el('ai-research-model').value = '';
    el('ai-research-ai-enabled').checked = false;
    await context.saveAIResearchSettings();
    const fixedBody = JSON.parse(calls.filter(call=>call.options.method==='PUT').at(-1).options.body);
    assert.equal(fixedBody.model_config_id, null, 'Fixed mode saves without a research model');
    assert.equal(fixedBody.ai_enabled, false);
    assert.equal(fixedBody.enabled, true);

    // AI 开启但未选模型：明确错误，不发起保存
    el('ai-research-ai-enabled').checked = true;
    const putCountBefore = calls.filter(call=>call.options.method==='PUT').length;
    await context.saveAIResearchSettings();
    assert.match(el('ai-research-message').text(), /研究模型/);
    assert.equal(calls.filter(call=>call.options.method==='PUT').length, putCountBefore,
        'Invalid AI combination must not reach the API');
    el('ai-research-ai-enabled').checked = false;

    status = {...status,last_check_at:'2026-10-02T19:00:00',last_success_at:'2026-10-02T18:00:00',next_check_at:'2026-10-02T20:00:00',
        settings:{...serverSettings, ai_enabled:true},
        model_status:{available:true, reason:null},
        topics:[{topic:'macro',status:'COMPLETED',research:{fixed_status:'COMPLETED',fixed_checks:2,rule_search_count:0,
            ai_status:'COMPLETED',ai_call_count:0,input_tokens:0,output_tokens:0,search_count:0,pages_read:2,rounds:0,
            sources_checked:[{source_id:'NBS_LATEST_RELEASE',status:'COMPLETED'},
                             {source_id:'SHFE_WAREHOUSE_DAILY',status:'UNAVAILABLE',reason:'前端脚本加载（'+malicious+'）'},
                             {source_id:'EIA_WPSR_SUMMARY',status:'NOT_APPLICABLE'}],
            events:[{type:'READ',title:malicious}]},error:malicious}],
        materials:{cache_state:'PARTIAL',sources:[{dimension:'fundamentals'}],missing_dimensions:['macro','events','related_markets']}};
    await context.loadAIResearchStatus('FU2611');
    assert.match(visibleText(el('ai-research-status')), /已停止自动请求：CZCE.*入口拒绝访问/);
    const preview = el('ai-future-cache-status');
    assert.match(visibleText(preview), /最近检查.*19:00.*最近成功.*18:00.*下次检查.*20:00/);
    assert.match(visibleText(preview), /不会.*搜索.*等待/);
    assert.match(visibleText(preview), /AI辅助开启/);
    assert.doesNotMatch(visibleText(preview), /搜索 2 次/);
    const researchDetails = el('ai-research-status').all().find(node=>node.dataset.researchTopic);
    researchDetails.open = true; researchDetails.ontoggle();
    const stageText = visibleText(el('ai-research-status'));
    assert.match(stageText, /固定来源检查 完成/, 'Stage stats stay visible');
    assert.match(stageText, /实际调用 0 次/, 'Zero real calls must be shown as 0');
    assert.match(stageText, /AI辅助开启|AI：完成/);
    assert.match(stageText, /来源 SHFE_WAREHOUSE_DAILY：未接入\/受阻/, 'Blocked sources are labelled, not faked');
    assert.match(stageText, /来源 EIA_WPSR_SUMMARY：不适用/);
    status.source_distribution = {historical_versions:{total:3,eastmoney_percent:66.67},latest_stored_documents:{total:2,eastmoney_percent:50},recent_jobs:[{topic:'product:FU',total:0,eastmoney_percent:null}]};
    status.contracts = [{instrument:'FU2611',coverage_gaps:['fundamentals'],fundamental_coverage:[{metric:'船燃需求',status:'MISSING'},{metric:'进口/供应',status:'AVAILABLE',evidence:[{data_period:'9月以来',quote:malicious,url:'https://finance.sina.com.cn/'}]}]}];
    await context.loadAIResearchStatus('FU2611');
    const distributionText = el('ai-research-status').text();
    assert.match(distributionText, /历史版本：66.67%/);
    assert.match(distributionText, /最新存储正文：50%/);
    assert.match(distributionText, /无新增，比例不适用/);
    assert.match(distributionText, /自身基本面待补：船燃需求/);
    assert.match(distributionText, /原文统计期：9月以来/);
    assert.match(distributionText, /<img/);
    researchDetails.open = false; researchDetails.ontoggle();
    status.topics[0].status = 'COMPLETED';
    await context.loadAIResearchStatus('FU2611');
    assert.equal(el('ai-research-status').all().find(node=>node.dataset.researchTopic).open,false,'Research completion collapses logs');
    // AI 暂停时固定状态照常展示
    status.settings = {...serverSettings, ai_enabled:true, pause_reason:'研究模型未执行工具调用'};
    status.topics[0].status = 'FAILED';
    status.topics[0].research = {...status.topics[0].research, ai_status:'PAUSED_MODEL', ai_pause_reason:'研究模型未执行工具调用'};
    await context.loadAIResearchStatus('FU2611');
    const pausedText = visibleText(el('ai-future-cache-status')) + visibleText(el('ai-research-status'));
    assert.match(pausedText, /AI暂停：研究模型未执行工具调用/);
    assert.match(pausedText, /上次采集失败/);
    assert.match(pausedText, /上次采集失败，有效缓存仍可使用/,
        'Collection state remains normal while AI is paused');

    vm.runInContext(extract('        function appendAIFutureEvidence(', '        function appendAIHistorySummary('),context);
    const batch = {instrument:'FU2611',related_contracts:['SC2611','BU2611'],related_market_snapshots:[
        {instrument:{symbol:'SC2611'},quality:{status:'AVAILABLE'}},{instrument:{symbol:'BU2611'},quality:{status:'MISSING'}}],
        public_materials:{cache_state:'PARTIAL',config_version:1,related_contracts:['SC2611','BU2611'],research:{status:'CACHED',last_check_at:'2026-10-02T19:00:00',jobs:[
            {topic:'product:SC',status:'COMPLETED',call_count:3,input_tokens:120,output_tokens:50,latency_ms:900,research:{search_count:2,pages_read:1,rounds:3,events:[{type:'READ',title:malicious}]}}]},sources:[
            {source_id:'s1',material_id:'v1',dimension:'related_markets',reference_contracts:['SC2611'],url:'javascript:alert(1)',title:malicious},
            {source_id:'s2',material_id:'v2',dimension:'fundamentals',reference_contracts:['BU2611']}],missing_dimensions:['macro']}};
    const evidence = new Element();
    context.appendAIFutureEvidence(evidence,{used_dimensions:['technical','related_markets'],used_related_contracts:['SC2611'],
        dimensions:{technical:{status:'AVAILABLE',summary:'自身行情'},related_markets:{status:'AVAILABLE',citations:[{source_id:'s1',quote:malicious}],
            market_evidence:[{instrument:'SC2611',time:'2026-10-01T15:00:00',field:'quote.last_price',value:620,reason:malicious}]}}},batch);
    assert.match(visibleText(evidence), /SC2611.*已用|已用.*SC2611/);
    assert.match(visibleText(evidence), /BU2611.*已提供但未引用/);
    assert.doesNotMatch(visibleText(evidence), /BU2611.*已用/);
    assert.match(visibleText(evidence), /BU2611.*行情缺失/);
    assert.doesNotMatch(visibleText(evidence), /<img/);
    assert.equal(evidence.all().filter(node=>node.tag==='a').length,0,'Unsafe source links stay text');
    assert.match(evidence.text(), /引用行情：SC2611.*quote.last_price.*620/);
    assert.match(visibleText(evidence), /宏观.*缺失/);
    const unverified = new Element();
    context.appendAIFutureEvidence(unverified,{dimensions:{related_markets:{status:'AVAILABLE',citations:[{source_id:'unknown'}]}}},batch);
    assert.doesNotMatch(visibleText(unverified), /SC2611.*已用|已用.*SC2611/);
    const limited = new Element();
    context.appendAIFutureEvidence(limited,{used_dimensions:['related_markets'],used_related_contracts:['SC2611'],
        dimensions:{related_markets:{status:'MISSING',summary:'引用原油资料但证据仍不足',citations:[{source_id:'s1',quote:'原油资料'}]}}},batch);
    assert.match(visibleText(limited), /相关市场.*部分用到/, 'Verified citation use stays visible even when the model declares insufficient evidence');
    vm.runInContext(extract('        function appendAIFutureMaterials(', '        function appendAIFutureResult('),context);
    const materials = new Element(); context.appendAIFutureMaterials(materials,batch);
    assert.doesNotMatch(visibleText(materials), /<img|javascript:/);
    assert.match(visibleText(materials), /最近检查.*19:00:00/);
    assert.match(materials.text(), /调用 3 次.*输入 120.*输出 50/);
    assert.doesNotMatch(visibleText(materials), /调用 3 次/);
    const disabled = new Element();
    const stoppedBatch = JSON.parse(JSON.stringify(batch));
    stoppedBatch.public_materials.collection_enabled = false;
    stoppedBatch.public_materials.research.jobs[0].next_check_at = '2026-10-02T20:00:00';
    context.appendAIFutureMaterials(disabled,stoppedBatch);
    assert.match(visibleText(disabled), /下次检查 定时关闭/);
    assert.doesNotMatch(visibleText(disabled), /20:00:00/);
    const bodyButton = materials.all().find(node=>node.tag==='button'&&/正文/.test(node.textContent));
    assert.ok(bodyButton,'Frozen material exposes a saved-body action');
    await bodyButton.onclick();
    assert.match(materials.text(), /<img/);
    assert.ok(calls.some(call=>call.url==='/api/v1/ai/research/materials/v1'));
    const sharedBox = new Element('div');
    context.appendAIResearchCacheStatus(sharedBox, {...status, settings:serverSettings}, 'FU2701');
    assert.match(sharedBox.text(),/共享 FU 品种资料/);
    await sharedBox.all().find(node=>node.tag==='button' && node.textContent==='立即更新资料').onclick();
    assert.equal(JSON.parse(calls.filter(call=>call.url.endsWith('/refresh')).at(-1).options.body).instrument,'FU2611');
    console.log('Scheduled research draft/save/version, directed contracts, hybrid AI switch, stage stats, cache preview, verified usage and safe frozen-body behavior passed');
}
check().catch(error=>{console.error(error);process.exitCode=1;});
