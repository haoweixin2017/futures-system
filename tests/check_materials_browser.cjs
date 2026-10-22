const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = require('./lib_index_source.cjs').readIndexSource();
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
function makeStyle() {
    const style = {};
    Object.defineProperty(style, 'cssText', {
        set(v) {
            String(v ?? '').split(';').forEach(part => {
                const idx = part.indexOf(':');
                if (idx > 0) style[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
            });
        },
        get() {
            return Object.entries(style).filter(([k]) => k !== 'cssText')
                .map(([k, v]) => `${k}: ${v}`).join('; ');
        },
    });
    return style;
}
class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.style = makeStyle(); this.dataset = {}; this.value = ''; this.checked = false; this.disabled = false; this._text = ''; }
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
const visibleText = node => [node.textContent, ...node.children.map(visibleText)].join('\n');
const elements = new Map();
const el = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const calls = [];
const malicious = '<img src=x onerror=alert(1)>';
let listPayload = {items: [], total: 0, page: 1, page_size: 20};
const context = {URL, URLSearchParams, currentSymbol: 'FU2611', isLiveMode: false, aiResearchSettings: null,
    document: {getElementById: el, createElement: tag => new Element(tag)},
    setTimeout: () => 1, clearTimeout: () => {},
    aiErrorMessage: async response => (await response.json()).detail,
    fetch: async (url, options = {}) => {
        calls.push({url, options});
        if (url.endsWith('/research/settings')) {
            return {ok: true, json: async () => ({enabled: false, ai_enabled: false, model_config_id: null,
                contracts: [{instrument: 'FU2611', related: ['SC2611']}, {instrument: 'BU2611', related: []}],
                config_version: 1})};
        }
        if (url.includes('/research/materials?')) return {ok: true, json: async () => listPayload};
        if (/\/research\/materials\/[^/?]+$/.test(url)) {
            return {ok: true, json: async () => ({material_id: url.split('/').pop(), body: malicious,
                published_at: '2026-10-01', version_fetched_at: '2026-10-02', content_hash: 'abc'})};
        }
        throw new Error('Unexpected request ' + url);
    }};
vm.createContext(context);
vm.runInContext(extract('        let aiResearchSettings =', '        const aiAnalysisRequirementDefaults ='), context);

async function check() {
    assert.equal(typeof context.openAIMaterialsModal, 'function', '资料查询 modal must expose working UI');
    // 列表数据：两条资料，一条含恶意标题/URL，一条修订两版
    listPayload = {page: 1, page_size: 20, total: 2, items: [
        {material_id: 'm1', title: malicious, url: 'javascript:alert(1)', dimension: 'macro',
         series_id: 'NBS:PMI', data_period: 'UNKNOWN', collection_method: 'FIXED_SOURCE',
         source_name: '国家统计局', published_at: '2026-09-30T10:00:00',
         version_fetched_at: '2026-10-02T19:00:00+08:00', excerpt: '官方发布正文摘录',
         content_hash: 'h1', version_count: 1},
        {material_id: 'm2', title: '燃料油库存周报', url: 'https://finance.eastmoney.com/a/1.html',
         dimension: 'fundamentals', series_id: null, data_period: '2026年9月',
         collection_method: 'RULE_SEARCH', source_name: '东方财富',
         published_at: '2026-10-01', version_fetched_at: '2026-10-02T18:00:00+08:00',
         excerpt: '燃料油库存下降，供应减少。', content_hash: 'h2', version_count: 2}]};
    await context.openAIMaterialsModal();
    assert.equal(el('ai-materials-modal').style.display, 'flex');
    const topicOptions = el('ai-materials-topic').children.map(option => option.value);
    assert.deepEqual(topicOptions, ['', 'macro', 'product:FU', 'product:SC', 'product:BU'], '主题选项含直接关联品种');
    assert.equal(el('ai-materials-instrument').value, 'FU2611');
    const listText = visibleText(el('ai-materials-list'));
    assert.match(listText, /燃料油库存周报/);
    assert.match(listText, /国家统计局 · NBS:PMI/);
    assert.match(listText, /固定来源/);
    assert.match(listText, /规则搜索/);
    assert.match(listText, /2 个版本/, '同一 URL 的版本数可见');
    assert.match(listText, /官方发布正文摘录/);
    assert.match(listText, /<img/, '恶意标题按纯文本原样展示（textContent 渲染，不解析 HTML）');
    assert.equal(el('ai-materials-list').all().filter(node => node.tag === 'a').length, 0,
        '外部 URL 不生成超链接');
    assert.ok(listText.includes('javascript:alert(1)'), 'URL 以纯文本展示，不可点击');
    // 维度彩色标签：每行标题前的 badge，颜色与图例一致
    const rows = el('ai-materials-list').children;
    const macroBadge = rows[0].all().find(node => node.tag === 'span' && node.textContent === '宏观');
    assert.equal(macroBadge.style.color, '#fb923c', '宏观维度标签用橙色');
    const fundamentalsBadge = rows[1].all().find(node => node.tag === 'span' && node.textContent === '基本面');
    assert.equal(fundamentalsBadge.style.color, '#22d3ee', '基本面维度标签用青色');
    assert.ok(listText.includes('维度图例') || html.includes('维度图例：'), '弹窗内提供维度图例');
    assert.match(html, /维度图例：/);
    assert.match(html, /基本面（品种自身供需库存）/, '图例说明每个维度的含义');
    // 相关市场图例已随 f6e82d5 移除关联合约参考功能一并下线

    // 打开正文：走既有 /materials/{id} 接口，正文按纯文本渲染
    const bodyButton = rows[1].all().find(node => node.tag === 'button' && /查看正文/.test(node.textContent));
    await bodyButton.onclick();
    const pre = rows[1].all().find(node => node.tag === 'pre');
    assert.match(pre.textContent, /<img/, '正文以纯文本展示（含原始内容，不解析 HTML）');
    assert.ok(calls.some(call => call.url === '/api/v1/ai/research/materials/m2'), '复用既有正文接口');

    // 过滤参数与分页
    el('ai-materials-topic').value = 'product:FU';
    el('ai-materials-dimension').value = 'fundamentals';
    el('ai-materials-method').value = 'RULE_SEARCH';
    el('ai-materials-keyword').value = '库存';
    await context.loadAIMaterials();
    const query = calls.filter(call => call.url.includes('/research/materials?')).at(-1).url;
    assert.match(query, /topic=product%3AFU/);
    assert.match(query, /dimension=fundamentals/);
    assert.match(query, /method=RULE_SEARCH/);
    assert.match(query, /q=%E5%BA%93%E5%AD%98/);
    assert.match(query, /page=1&page_size=20/);

    listPayload = {...listPayload, total: 45};
    await context.loadAIMaterials();
    const pager = el('ai-materials-pager');
    const buttons = pager.all().filter(node => node.tag === 'button');
    assert.equal(buttons[0].disabled, true, '第一页禁用上一页');
    assert.equal(buttons[1].disabled, false);
    await buttons[1].onclick();
    assert.match(calls.filter(call => call.url.includes('/research/materials?')).at(-1).url, /page=2/);
    assert.match(visibleText(pager), /第 2 \/ 3 页 · 共 45 条/);
    await context.queryAIMaterials();
    assert.match(calls.filter(call => call.url.includes('/research/materials?')).at(-1).url, /page=1/);
    const historyButton = el('ai-materials-list').all().find(node => node.tag === 'button' && node.textContent === '历史版本');
    await historyButton.onclick();
    assert.match(calls.filter(call => call.url.includes('/research/materials?')).at(-1).url, /history_for=m2/);
    await context.queryAIMaterials();
    assert.ok(!calls.filter(call => call.url.includes('/research/materials?')).at(-1).url.includes('history_for'));

    // 空列表提示
    listPayload = {page: 1, page_size: 20, total: 0, items: []};
    await context.loadAIMaterials();
    assert.match(visibleText(el('ai-materials-list')), /没有符合条件的资料版本/);
    const originalFetch = context.fetch;
    const pending = [];
    context.fetch = async () => new Promise(resolve => pending.push(resolve));
    const oldRequest = context.loadAIMaterials();
    const newRequest = context.queryAIMaterials();
    pending[1]({ok: true, json: async () => ({page: 1, page_size: 20, total: 0, items: []})});
    await newRequest;
    pending[0]({ok: true, json: async () => ({page: 1, page_size: 20, total: 1,
        items: [{material_id: 'old', title: '过时查询结果'}]})});
    await oldRequest;
    assert.ok(!visibleText(el('ai-materials-list')).includes('过时查询结果'));
    context.fetch = originalFetch;
    console.log('Materials browser: owner filters, version counts, text-only rendering, body via existing endpoint and pagination passed');
}
check().catch(error => {console.error(error); process.exitCode = 1;});
