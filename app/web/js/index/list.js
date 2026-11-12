// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, CAT_META, S, copyText, esc, fmt, post, spkColor, toast } from './core.js';
import { askText } from './dialog.js';
import { refresh } from './filters.js';
import { focusTimeline, setPlayhead } from './player.js';
import { doSearch } from './search.js';
function renderTimeline(bars){
  const max=Math.max(1,...bars.map(b=>b.total));
  $('#tlBars').innerHTML=bars.map(b=>{
    const h=Math.max(2,b.total/max*100);
    const seg=Object.entries(b.by_category||{}).filter(([k])=>CAT_META[k])
      .map(([k,n])=>'<i style="flex:'+n+';background:'+CAT_META[k][1]+'"></i>').join('');
    const sel=S.tFrom<b.t_to&&S.tTo>b.t_from?'sel':'';
    return `<div class="bucket ${sel} ${b.overlap?'ovl':''}" data-a="${b.t_from}" data-b="${b.t_to}"
      title="${fmt(b.t_from)}–${fmt(b.t_to)} · ${b.total} 条 · 重叠 ${b.overlap}">
      <div class="stack" style="height:${h}%">${seg}</div></div>`}).join('');
  $$('#tlBars .bucket').forEach(e=>e.onclick=()=>{
    S.tFrom=Number(e.dataset.a);S.tTo=Math.min(S.duration,Number(e.dataset.b));
    $('#tFrom').value=S.tFrom;$('#tTo').value=S.tTo;refresh()});
  const band=document.createElement('i'); band.id='tlFocus';
  const head=document.createElement('i'); head.id='tlHead';
  $('#tlBars').appendChild(band); $('#tlBars').appendChild(head);
  if(S.playhead!=null)focusTimeline(S.playhead,null,false);
  const step=1800,labels=[];for(let t=0;t<=S.duration;t+=step)labels.push(fmt(t));
  $('#tlAxis').innerHTML=labels.map(x=>`<span>${x.slice(0,5)}</span>`).join('');
}
function renderList(){
  const box=$('#list');
  if(!S.rows.length){box.innerHTML='<div class="empty">当前条件下没有记录<br>试试放宽时间段或清空关键词</div>';return}
  box.innerHTML=S.rows.map(r=>{
    const c=CAT_META[r.category]||['未分类','#999'];
    const tags=`<span class="tag cat" style="background:${c[1]}">${c[0]}</span>`;
    const low=r.value_tag==='low'?`<span class="tag lowv">低价值${r.low_value_reason?' · '+esc(r.low_value_reason.slice(0,16)):''}</span>`:'';
    const ov=r.overlap?'<span class="tag warn">多人重叠 · ASR 不可信</span>':'';
    const cf=r.asr_conf<0.6?`<span class="tag warn">置信度 ${(r.asr_conf*100).toFixed(0)}%</span>`:'';
    const tp=r.topic?`<span class="tag">${esc(r.topic)}</span>`:'';
    const manual=S.manualHl.has(Number(r.id))?'<span class="tag hi">★ 高价值</span>':'';
    return `<div class="urow ${S.hl.has(r.id)?'hl':''} ${manual?'manual':''} ${r.value_tag==='low'||r.overlap?'low':''}" id="u${r.id}" data-id="${r.id}">
      <div class="tc" data-t="${r.t_start}">${fmt(r.t_start)}</div>
      <div class="spk"><i class="dot" style="background:${spkColor(r.speaker_id)}"></i>${esc(r.speaker_label)}</div>
      <div class="body"><div class="txt">${esc(r.text)}</div><div class="tags">${tags}${tp}${manual}${low}${ov}${cf}</div></div></div>`}).join('');
  $$('#list .tc').forEach(e=>e.onclick=()=>setPlayhead(Number(e.dataset.t)));
  $$('#list .urow').forEach(e=>e.onclick=ev=>{if(!ev.target.classList.contains('tc')){
    const r=S.rows.find(x=>x.id==Number(e.dataset.id));if(r)setPlayhead(r.t_start,r.text)}});
  $$('#list .urow').forEach(e=>e.oncontextmenu=ev=>{
    const r=S.rows.find(x=>x.id==Number(e.dataset.id));
    if(r)openRowMenu(r,ev);
  });
}

/* ---------------- 字幕行右键：改说话人 / 标记高价值 / 搜索 ---------------- */
function speakerItems(r){
  const items=((S.meta&&S.meta.speakers)||[]).map(function(p){
    return {label:p.label||p.display_name||p.speaker_id,
      run:function(){reassignRow(r,p.speaker_id,p.label||p.display_name||p.speaker_id)}};
  });
  items.push({sep:true});
  items.push({label:'新建说话人…',run:function(){
    askText({title:'新建说话人',desc:'给这个人起个名字，这句话会归到他名下。',
      value:'',placeholder:'例如：嘉宾小林'}).then(function(name){
      if(name&&name.trim())reassignRow(r,'__new__',name.trim());
    });
  }});
  return items;
}
async function reassignRow(r,spk,label){
  try{
    const body={session_id:S.session,speaker_id:spk,label:label};
    if(r.id)body.utterance_id=r.id;else body.t_start=r.t_start;
    const out=await post('/api/utterances/reassign',body);
    if(!out.ok){toast(out.error||'改判失败');return}
    await refresh();
    toast('已改为「'+out.label+'」');
  }catch(e){toast('改判失败：'+e.message)}
}
async function toggleManual(r){
  try{
    if(!S.session)return;
    const out=await post('/api/highlights/manual',{session_id:S.session,
      utterance_id:r.id,t_start:r.t_start,text:r.text,speaker_label:r.speaker_label});
    if(out.highlighted)S.manualHl.add(Number(r.id));
    else S.manualHl.delete(Number(r.id));
    renderList();
    toast(out.highlighted?'已标记高价值，可在高光榜查看':'已取消高价值标记');
  }catch(e){toast('标记失败：'+e.message)}
}
function openRowMenu(r,ev){
  // 右键菜单是宿主能力（桌面版用应用内菜单替代 WebView2 原生菜单）；
  // 网页版没有 LN.menu，交给浏览器原生菜单。
  if(!window.LN||!window.LN.menu)return;
  ev.preventDefault();
  let sel='';
  try{sel=String(getSelection()||'').trim()}catch(e){}
  const marked=S.manualHl.has(Number(r.id));
  window.LN.menu([
    {label:'定位到这句话',run:function(){setPlayhead(r.t_start,r.text)}},
    {label:'复制文字',run:function(){copyText(r.text)}},
    {sep:true},
    {label:'改说话人',run:function(){
      window.LN.menu(speakerItems(r),ev.clientX+10,ev.clientY+10);
    }},
    {label:marked?'取消高价值标记':'标记高价值',run:function(){toggleManual(r)}},
    {sep:true},
    {label:sel?('搜索「'+sel.slice(0,14)+'」'):'在全库搜索这句话',run:function(){
      const q=sel||r.text;
      // 桌面壳里顶栏被隐藏，页面自带的搜索框看不见；用 iframe 内的搜索面板。
      if(window.LN&&window.LN.search) window.LN.search(q);
      else{ $('#gq').value=q;doSearch(); }
    }}
  ],ev.clientX,ev.clientY);
}

export { renderList, renderTimeline };
