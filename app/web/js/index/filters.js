// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { runClassifyAI } from './classify.js';
import { $, $$, CAT_META, S, api, esc, fmt, spkColor, toast } from './core.js';
import { renderList, renderTimeline } from './list.js';
import { setPlayhead } from './player.js';

function activeFilters(includeTime=true){
  const f={session_id:S.session,categories:[...S.cats],speakers:[...S.spks],q:S.q,
    exclude_low_value:S.exLow||S.onlyHigh,exclude_overlap:S.exOvl,
    min_conf:S.onlyHigh?0.8:null};
  if(includeTime){f.t_from=S.tFrom;f.t_to=S.tTo}
  f.only_high=S.onlyHigh;
  return f;
}
function qs(f){const p=new URLSearchParams();for(const[k,v]of Object.entries(f)){
  if(v==null||v==='')continue;p.set(k,Array.isArray(v)?v.join(','):(typeof v==='boolean'?(v?'1':'0'):String(v)))}
  return p.toString()}
function renderMarksBar(marks){
  const el=$('#marksBar');
  if(!marks||!marks.length){el.classList.remove('show');el.innerHTML='';return}
  el.classList.add('show');
  el.innerHTML='<b>📍 打点</b>'+marks.map(m=>
    `<span class="mk" data-t="${m.t}" title="${esc(m.note||'打点')}">${fmt(m.t)}</span>`).join('');
  $$('#marksBar .mk').forEach(e=>e.onclick=()=>setPlayhead(Number(e.dataset.t),'打点 '+fmt(Number(e.dataset.t))));
}
function renderFilters(){
  const cats=(S.meta.categories||[]).filter(c=>CAT_META[c.category]);
  $('#catChips').innerHTML=cats.map(c=>`<span class="chip" data-c="${c.category}"><i class="dot" style="background:${CAT_META[c.category][1]}"></i>${CAT_META[c.category][0]} ${c.n}</span>`).join('');
  renderAIState();
  const sp=(S.meta.speakers||[]);
  $('#spkChips').innerHTML=sp.map(p=>`<span class="chip" data-s="${esc(p.speaker_id)}" title="verified=${p.verified} ${esc(p.note||'')}"><i class="dot" style="background:${spkColor(p.speaker_id)}"></i>${esc(p.display_name)}${p.verified?'':' ?'} <b style="font-weight:400;opacity:.7">${p.n}</b></span>`).join('');
  $('#renameWho').innerHTML=sp.map(p=>`<option value="${esc(p.speaker_id)}">${esc(p.display_name)}</option>`).join('');
  $('#quickQ').innerHTML=['跳槽','押金','限流','微信','简历','冷暴力'].map(w=>`<span class="chip" data-q="${w}">${w}</span>`).join('');
  $$('#catChips .chip').forEach(e=>e.onclick=()=>{const k=e.dataset.c;S.cats.has(k)?S.cats.delete(k):S.cats.add(k);syncChips();refreshDebounced()});
  $$('#spkChips .chip').forEach(e=>e.onclick=()=>{const k=e.dataset.s;S.spks.has(k)?S.spks.delete(k):S.spks.add(k);syncChips();refreshDebounced()});
  $$('#quickQ .chip').forEach(e=>e.onclick=()=>{$('#q').value=e.dataset.q;S.q=e.dataset.q;syncChips();refresh()});
  syncChips();
}
function syncChips(){
  $$('#catChips .chip').forEach(e=>e.classList.toggle('on',S.cats.has(e.dataset.c)));
  $$('#spkChips .chip').forEach(e=>e.classList.toggle('on',S.spks.has(e.dataset.s)));
  $$('#quickQ .chip').forEach(e=>e.classList.toggle('on',S.q===e.dataset.q));
}
function renderTimePresets(){
  const dur=Math.max(1,Math.round(S.duration));
  const third=dur/3;
  const items=[['整场',0,dur],['前段',0,third],['中段',third,2*third],['后段',2*third,dur]];
  $('#timePresets').innerHTML=items.map(function(x){
    const a=Math.round(x[1]), b=Math.round(x[2]);
    return '<button data-a="'+a+'" data-b="'+b+'" title="'+fmt(a)+' – '+fmt(b)+'">'+x[0]+'</button>';
  }).join('');
  $$('#timePresets button').forEach(function(b){
    b.onclick=function(){
      const a=Number(b.dataset.a);
      S.tFrom=Math.max(0,Math.min(a,S.duration));
      S.tTo=Math.max(S.tFrom,Math.min(S.duration,Number(b.dataset.b)));
      $('#tFrom').value=S.tFrom;$('#tTo').value=S.tTo;
      updateTimeLabel();refresh();
    };
  });
}
function updateTimeLabel(){
  const span=Math.max(0,S.tTo-S.tFrom);
  const whole=(S.tFrom<=0&&S.tTo>=S.duration-1);
  const human=span>=3600?(Math.round(span/3600)+' 小时')
    :span>=60?(Math.floor(span/60)+' 分 '+Math.round(span%60)+' 秒')
    :(Math.round(span)+' 秒');
  $('#timeLabel').textContent=`${fmt(S.tFrom)} – ${fmt(S.tTo)} （${whole?'整场 · ':''}${human}）`;
  $('#filterDesc').textContent=[S.cats.size?`分类:${S.cats.size}`:'',S.spks.size?`说话人:${S.spks.size}`:'',
    S.q?`关键词:「${S.q}」`:'',S.exLow?'剔除低价值':''].filter(Boolean).join(' · ')||'无附加条件';
}
let _t;const refreshDebounced=()=>{clearTimeout(_t);_t=setTimeout(refresh,180)};
async function refresh(){
  if(!S.session)return;
  updateTimeLabel();
  const f=activeFilters();
  try{
    const [d,t]=await Promise.all([
      api('/api/utterances?'+qs({...f,limit:3000})),
      api('/api/timeline?'+qs({...f,bucket:300}))]);
    S.rows=d.rows||[];$('#totalCount').textContent=d.total;
    $('#recCount').textContent=`显示 ${S.rows.length} / ${d.total} 条`;
    renderList();renderTimeline(t.bars||[]);
  }catch(e){toast('查询失败：'+e.message)}
}
function renderAIState(){
  const el=$('#aiState');if(!el)return;
  const ai=(S.meta&&S.meta.ai)||{};
  const labeled=ai.labeled||0,total=ai.total||0;
  if(!labeled){
    el.innerHTML='当前是<b>规则分类</b>（关键词匹配，0 token）';
    return;
  }
  const wait=total-labeled;
  el.innerHTML='已 <b style="color:var(--accent)">AI 标注</b> '+labeled+'/'+total+' 句'
    +((ai.topics||[]).length?(' · '+(ai.topics||[]).length+' 个主题'):'')
    +(wait>0?(' · <b style="color:var(--accent)">'+wait+' 句待标</b>'):'')
    +' · <a id="clsForce" style="cursor:pointer;color:var(--muted)" title="换掉主题集，整场重标一遍（token 最贵）">全部重标</a>';
  const f=$('#clsForce');
  if(f)f.onclick=function(){runClassifyAI(true)};
}

export { _t, activeFilters, qs, refresh, refreshDebounced, renderAIState, renderFilters, renderMarksBar, renderTimePresets, syncChips, updateTimeLabel };
