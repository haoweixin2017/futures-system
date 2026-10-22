/* One draw-session, one immutable server baseline. No trading endpoints. */
(function (root) {
    'use strict';
    class SimulatorController {
        constructor({context, request, render}) {
            this.context = context; this.request = request; this.render = render;
            this.generation = 0; this.sequence = 0; this.explicitReference = null;
            this.state = {status:'CLOSED', snapshot:null, result:null, error:null};
        }
        key() { const c=this.context(); return `${c.account}\n${c.instrument}`; }
        emit() { this.render(this.state); }
        selectReference(run, batch) { this.explicitReference={run,batch,key:this.key()}; this.emit(); }
        clearReference() { this.explicitReference=null; this.emit(); }
        referenceForContext() { return this.explicitReference?.key===this.key() ? this.explicitReference.run : null; }
        release(id) { if (id) Promise.resolve(this.request(`/snapshots/${encodeURIComponent(id)}`,null,'DELETE')).catch(()=>{}); }
        invalidate() {
            clearTimeout(this.timer); this.abort?.abort(); this.generation++; this.sequence++;
            this.release(this.state.snapshot?.snapshot_id);
            this.state={status:'CLOSED',snapshot:null,result:null,error:null};
        }
        close() { this.invalidate(); this.clearReference(); this.emit(); }
        async open() {
            const reference=this.referenceForContext(), selected=this.explicitReference;
            this.invalidate(); this.explicitReference=selected?.key===this.key() ? selected : null;
            const generation=this.generation, key=this.key(), ctx=this.context();
            this.state.status='LOADING'; this.emit();
            try {
                const data=await this.request('/snapshots',{instrument:ctx.instrument,reference_run_id:reference});
                if (this.generation!==generation || key!==this.key()) { this.release(data.snapshot_id); return; }
                this.state={status:'READY',snapshot:data,result:data.initial_comparison,error:null}; this.emit();
            } catch (error) {
                if (this.generation===generation && key===this.key()) {
                    this.state.status='BLOCKED'; this.state.error=error.message;
                    this.state.failure=this.failure(error,'CREATE',{instrument:ctx.instrument,reference_run_id:reference}); this.emit();
                }
            }
        }
        failure(error, stage, inputs) {
            const s=this.state.snapshot, ctx=error.context;
            const authorized=![401,403,404].includes(error.status) && ctx?.stage===stage
                && ctx.instrument===this.context().instrument
                && (stage==='CREATE' ? ctx.frozen_at==null : ctx.snapshot_id===s?.snapshot_id && ctx.client_sequence===this.sequence);
            return {stage,inputs:{...inputs},context:authorized?ctx:null,fields:error.fields || {}};
        }
        schedule(input, commit) {
            clearTimeout(this.timer); this.abort?.abort(); this.sequence++;
            this.state.status='COMPUTING'; this.state.error=null; this.state.failure=null; this.emit();
            this.timer=setTimeout(()=>this.calculate(input,commit),300);
        }
        async calculate(input, commit) {
            clearTimeout(this.timer);
            const snapshot=this.state.snapshot;
            if (!snapshot) return;
            this.abort?.abort(); this.abort=new AbortController();
            const generation=this.generation, key=this.key(), seq=++this.sequence, sid=snapshot.snapshot_id;
            this.state.status='COMPUTING'; this.state.error=null; this.state.failure=null; this.emit();
            const current=()=>generation===this.generation && key===this.key() && sid===this.state.snapshot?.snapshot_id && seq===this.sequence;
            try {
                const data=await this.request('/simulate',{snapshot_id:sid,client_sequence:seq,...input},'POST',this.abort.signal);
                if (!current() || data.snapshot_id!==sid || data.client_sequence!==seq) return;
                if(commit) commit();
                this.state.status='READY'; this.state.result=data; this.emit();
            } catch (error) {
                if (current() && error.name!=='AbortError') {
                    this.state.status='ERROR'; this.state.error=error.message;
                    this.state.failure=this.failure(error,'SIMULATE',input); this.emit();
                }
            }
        }
    }
    function applyPreset(input, preset) {
        const values={...input};
        if (preset.price!=null) values.simulate_price=preset.price;
        if (preset.volume_ratio!=null) values.volume_ratio=preset.volume_ratio;
        return values;
    }
    if (typeof module!=='undefined' && module.exports) module.exports={SimulatorController,applyPreset};
    if (typeof document==='undefined') return;
    const el=id=>document.getElementById(`market-simulator-${id}`);
    const getContext=()=>({instrument:currentSymbol,account:currentUser?.username || '', role:currentUser?.role});
    async function request(path,body,method='POST',signal) {
        const timeout=new AbortController();
        const timer=setTimeout(()=>timeout.abort(),path==='/snapshots' ? 45000 : 10000);
        const combined=signal && typeof AbortSignal.any==='function' ? AbortSignal.any([signal,timeout.signal]) : timeout.signal;
        try {
            const response=await fetch('/api/v1/market-simulator'+path,{method,credentials:'same-origin',cache:'no-store',
                headers:{'Content-Type':'application/json'},body:body ? JSON.stringify(body) : undefined,signal:combined});
            if (method==='DELETE' && (response.status===204 || response.status===404)) return null;
            const data=await response.json().catch(()=>({}));
            if (!response.ok) {
                let message=typeof data.detail==='string' ? data.detail : data.detail?.message;
                const reasons=data.detail?.reasons || [];
                if (reasons.length) message+='；'+reasons.map(r=>r.reason).join('；');
                const error=new Error(message || `请求失败 (${response.status})`);
                error.status=response.status;
                if (![401,403,404].includes(response.status)) {
                    error.context=data.detail?.context;
                    const fields=data.detail?.fields || {};
                    error.fields={observed_volume:fields.observed_volume,minimum_ratio:fields.minimum_ratio};
                }
                throw error;
            }
            return data.data;
        } finally { clearTimeout(timer); }
    }
    let renderedSnapshot=null;
    const display=v=>v===null || v===undefined ? '无法计算/未知' : String(v);
    const text=(tag,value)=>{const n=document.createElement(tag); n.textContent=value; return n;};
    const fmtTime=v=>v ? new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '尚未建立';
    function inputValues() {
        const oi=Number(el('oi').value);
        return {simulate_price:el('price').value,volume_ratio:el('volume').value,
            open_interest_change: /^-?\d+$/.test(el('oi').value) && Number.isSafeInteger(oi) ? oi : null};
    }
    function render(state) {
        const drawer=el('drawer'); if (!drawer) return;
        drawer.hidden=state.status==='CLOSED';
        if (state.status==='CLOSED') { renderedSnapshot=null; el('results').replaceChildren(); return; }
        const scroll=drawer.scrollTop;
        el('title').textContent=getContext().instrument+' · 行情模拟';
        el('status').textContent=({LOADING:'正在建立冻结基准',BLOCKED:'无法建立基准',COMPUTING:'计算中，以下为上次结果',ERROR:'本次失败，以下为旧结果',READY:'计算已更新'})[state.status] || '';
        el('error').textContent=state.error || '';
        const failure=state.failure;
        if(failure) {
            const ctx=failure.context;
            el('error').textContent+=`\n本次失败：${getContext().instrument} · 15分钟 · 模拟情景\n提交输入 ${JSON.stringify(failure.inputs)}\n${ctx?.frozen_at ? '已授权基准冻结 '+fmtTime(ctx.frozen_at) : failure.stage==='CREATE' ? '冻结时间尚未建立' : '仅关联本地提交，未确认服务端基准'}`;
            if(failure.fields.observed_volume!=null) el('error').textContent+=`\n已发生 ${failure.fields.observed_volume}手；最低有效倍率 ${display(failure.fields.minimum_ratio)}`;
        }
        const snapshot=state.snapshot;
        for (const id of ['price','volume','oi','reset']) el(id).disabled=!snapshot;
        el('price-range').disabled=!snapshot?.constraints.slider_available;
        el('volume-range').disabled=snapshot?.defaults.volume_ratio==null;
        if (!snapshot) {
            renderedSnapshot=null;
            for (const id of ['price','volume','oi']) el(id).value='';
            for (const id of ['meta','stale','range-note']) el(id).textContent='';
            if(state.status==='BLOCKED') el('meta').textContent=`${getContext().instrument} · 15分钟 · 模拟情景 · 冻结时间尚未建立`;
            el('presets').replaceChildren(); el('results').replaceChildren();
        }
        if (snapshot && renderedSnapshot!==snapshot.snapshot_id) {
            renderedSnapshot=snapshot.snapshot_id;
            const d=snapshot.defaults,c=snapshot.constraints;
            el('price').value=d.simulate_price;
            el('volume').value=d.volume_ratio==null ? '' : Number(d.volume_ratio).toFixed(12).replace(/\.?0+$/,'');
            el('oi').value=typeof d.open_interest_change==='string'
                ? d.open_interest_change.replace(/\.0+$/,'') : d.open_interest_change ?? '';
            el('price').step=c.tick; el('price-range').step=c.tick;
            el('price-range').disabled=!c.slider_available;
            if(c.slider_available) {el('price-range').min=c.slider_min;el('price-range').max=c.slider_max;el('price-range').value=d.simulate_price;}
            el('range-note').textContent=c.slider_reason || `滑块 ${c.slider_min}～${c.slider_max}，步长 ${c.tick}；合法手工价格可超出滑块范围`;
            el('volume-range').disabled=d.volume_ratio==null;
            el('presets').replaceChildren();
            for(const preset of snapshot.presets || []) {
                const b=text('button',preset.label);b.type='button';b.disabled=!preset.available;
                b.title=preset.reason || `${preset.price==null?'保持当前价格':'采用价格 '+preset.price}；${preset.volume_ratio==null?'保持量比':'量比 '+preset.volume_ratio}；持仓变化保持`;
                b.onclick=()=>{
                    const values=applyPreset(inputValues(),preset);
                    // Validate the whole proposal with server Decimal/ROUND_HALF_UP.
                    // Commit only if the same draft and baseline are still current.
                    controller.schedule(values,()=>{
                        el('price').value=values.simulate_price;el('volume').value=values.volume_ratio;
                    });
                }; el('presets').append(b);
            }
            const quality=Object.entries(snapshot.quality.sequences || {}).map(([p,q])=>`${p==='current'?'当前观测':p}：${q.count}根，来源 ${q.providers.join('/')} / ${(q.sources || ['UNKNOWN']).join('/')}，时间标签 ${q.label_semantics.join('/')}，覆盖 ${q.coverage.join('/')}`).join('\n');
            el('meta').textContent=`交易日 ${snapshot.trading_day} · 15分钟 · 新浪\n冻结 ${fmtTime(snapshot.frozen_at)} · 源报价 ${fmtTime(snapshot.quote_at)}\n历史截止 ${Object.entries(snapshot.history_cutoffs).map(([p,v])=>`${p}: ${fmtTime(v.closed_until)} (${v.count}根)`).join('；')}\n${snapshot.quality.warnings.join('；')}\n${quality}\n${(snapshot.quality.evaluation_reasons || []).map(v=>v.message).join('；')}`;
        }
        const r=state.result;
        if(r) {
            el('stale').textContent=r.baseline_stale ? '基准已旧：结果仅针对冻结情景，需要时点击更新基准。' : '';
            const target=el('results');target.replaceChildren();
            if(state.status==='ERROR' || state.status==='COMPUTING') target.append(text('p',`上次成功对应：价格 ${display(r.simulated.price)} · 量比 ${display(r.simulated.volume_ratio)} · 持仓变化 ${display(r.simulated.oi_change)}`));
            const table=document.createElement('table');const head=document.createElement('tr');
            for(const title of ['项目','当前情景（冻结观测，未闭合）','模拟情景（假设本周期收盘）']) head.append(text('th',title)); table.append(head);
            const stateName={NEUTRAL:'条件不足',LONG:'假设满足多头观察',SHORT:'假设满足空头观察',CONFLICT:'多空冲突，观察',UNKNOWN:'不可评估'};
            const rows=[['价格',r.current.price,r.simulated.price],['量比',r.current.volume_ratio,r.simulated.volume_ratio],['持仓变化',r.current.oi_change,r.simulated.oi_change],
                ['多头评分',r.current.long.score,r.simulated.long.score],['空头评分',r.current.short.score,r.simulated.short.score],
                ['状态',stateName[r.current.state] || '不可评估',stateName[r.simulated.state] || '不可评估'],['毛收益风险比',r.current.reward_risk.ratio,r.simulated.reward_risk.ratio]];
            for(const row of rows){const tr=document.createElement('tr');row.forEach(v=>tr.append(text('td',display(v))));table.append(tr);} target.append(table);
            target.append(text('p',`本次采用：价格 ${display(r.effective_values.close_price)} · 成交 ${r.effective_values.volume}手 · 假设总持仓 ${display(r.effective_values.open_interest)}`));
            const names={BREAK_PREVIOUS_HIGH:'突破前高',BREAK_PREVIOUS_LOW:'跌破前低',VOLUME_EXPANSION:'成交量倍率',OPEN_INTEREST_INCREASE:'持仓增加',TREND_ALIGNMENT_60M:'60分钟趋势'};
            for(const side of ['long','short']){
                target.append(text('strong',`${side==='long'?'多头':'空头'}规则变化 ${display(r.score_changes[side])}`));
                for(const c of r.simulated[side].conditions) {
                    const before=r.current[side].conditions.find(v=>v.code===c.code);
                    const diff=r.changes.find(v=>v.direction===side && v.condition_code===c.code);
                    const status=v=>v==null?'不可评估':v?'满足':'不满足';
                    target.append(text('p',`${names[c.code] || c.code}：当前 ${display(before?.actual)} → 假设 ${display(c.actual)}；阈值 ${display(c.threshold)}；${status(before?.passed)} → ${status(c.passed)}；${display(before?.points)} → ${display(c.points)}分（变化 ${display(diff?.points_delta)}）`));
                }
            }
            const reference=snapshot.reference_plan;
            if(controller.referenceForContext()!==(snapshot.selected_reference_run_id ?? reference?.run_id ?? null)) target.append(text('p','参考选择已变化，请更新基准；当前仍使用冻结计划。'));
            target.append(text('p',reference ? `参考 ${reference.source} / ${reference.source_timeframe} · 方向 ${reference.direction} · 目标 ${reference.target} · 止损 ${reference.stop} · 形成 ${fmtTime(reference.completed_at)} · 有效 ${fmtTime(reference.valid_from)}～${fmtTime(reference.valid_until)}` : '未明确选择有效参考计划，毛收益风险比无法计算'));
            const rrReason={NO_REFERENCE_PLAN:'未明确选择有效参考计划',REFERENCE_EXPIRED:'参考计划已过期',DIRECTION_CONFLICT:'情景方向与原计划冲突',NO_DIRECTION:'暂无可用的明确方向',INVALID_LEVELS:'当前价格已不满足原计划目标和止损范围'};
            target.append(text('p',`毛收益风险比未扣手续费和滑点；${rrReason[r.simulated.reward_risk.reason_code] || ''}`));
            target.append(text('strong',r.conclusion.text));
        }
        drawer.scrollTop=scroll;
    }
    const controller=new SimulatorController({context:getContext,request,render});
    root.marketSimulator=controller;
    root.invalidateMarketSimulator=()=>controller.close();
    el('open')?.addEventListener('click',()=>{if(getContext().role==='sim')controller.open();});
    el('close')?.addEventListener('click',()=>controller.close());
    el('update')?.addEventListener('click',()=>controller.open());
    el('reset')?.addEventListener('click',()=>{
        renderedSnapshot=null;render(controller.state);controller.calculate({use_defaults:true});
    });
    for(const id of ['price','volume','oi'])el(id)?.addEventListener('input',()=>controller.schedule(inputValues()));
    el('price-range')?.addEventListener('input',()=>{el('price').value=el('price-range').value;controller.schedule(inputValues());});
    el('volume-range')?.addEventListener('input',()=>{el('volume').value=el('volume-range').value;controller.schedule(inputValues());});
    let lastContext=controller.key();
    setInterval(()=>{
        const key=controller.key();
        if(key!==lastContext){lastContext=key;controller.close();}
        if(el('open'))el('open').hidden=getContext().role!=='sim';
        const s=controller.state.snapshot;
        if(s && (Date.now()-Date.parse(s.quote_at)>25000 || Date.now()>=Date.parse(s.session_end))) el('stale').textContent='基准已旧：仍为冻结情景，可点击更新基准。';
    },1000);
    document.addEventListener('keydown',event=>{if(event.key==='Escape')controller.close();});
})(typeof window!=='undefined'?window:globalThis);
