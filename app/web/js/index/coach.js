// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, S, api, esc, fmt, toast } from './core.js';
import { renderList } from './list.js';
import { setPlayhead } from './player.js';
async function coach(){
  if(!S.session)return;
  $('#sumBody').innerHTML='<div class="empty">正在计算主播画像…</div>';
  try{const d=await api('/api/coach?session_id='+encodeURIComponent(S.session));renderCoach(d)}
  catch(e){$('#sumBody').innerHTML=`<div class="empty">生成失败：${esc(e.message)}</div>`}
}
async function paintCross(){
  const box=$('#crossBox');
  if(!box)return;
  box.innerHTML='<div class="empty" style="padding:10px">正在统计跨场次…</div>';
  try{
    const c=await api('/api/coach/cross');
    if(!c.labels||!c.labels.length){ box.innerHTML=''; return }
    const rows=c.labels.slice(0,8).map(x=>
      '<tr><td>'+esc(x.label)+'</td><td>'+x.sessions+' 场</td><td>'+fmt(x.seconds)+'</td>'
      +'<td>'+x.speed+' 字/分</td><td>'+x.question_per_min+' 次/分</td><td>'+x.filler_rate+'%</td></tr>').join('');
    box.innerHTML='<div class="sec-t" style="padding:6px 0 6px">跨场次（按标签聚合 '+c.scanned_sessions+' 场）</div>'
      +'<table class="crosst"><thead><tr><th>标签</th><th>场次</th><th>时长</th><th>语速</th><th>提问</th><th>口癖率</th></tr></thead>'
      +'<tbody>'+rows+'</tbody></table>'
      +'<div class="caveats"><div>· '+esc(c.caveat)+'</div></div>';
  }catch(e){ box.innerHTML='' }
}
function renderCoach(d){
  if(d.error){$('#sumBody').innerHTML=`<div class="empty">${esc(d.error)}</div>`;return}
  const host=d.host||{},guest=d.guest||{},g=d.grade||'C';
  const gcol=g==='A'?'A':g==='B'?'B':'C';
  const tiles=[['总评',Math.round((d.overall||0)*100)+' 分'],['逐字覆盖率',Math.round((d.coverage||0)*100)+'%'],
    ['主播发言',((host.seconds||0)/60).toFixed(1)+' 分'],['嘉宾发言',((guest.seconds||0)/60).toFixed(1)+' 分'],
    ['换手',(d.switches||0)+' 次'],['口癖',(d.fillers||0)+' 次']];
  const dims=(d.dimensions||[]).map(x=>{
    const pct=Math.round((x.score||0)*100),col=x.score>=.7?'var(--ok)':x.score>=.4?'#d48806':'var(--danger)';
    const ev=(x.evidence||[]).length?`<div class="quote">${x.evidence.map(e=>`<a class="evt" data-t="${e.t}">${fmt(e.t)}</a> ${esc(e.text)}`).join('<br>')}</div>`:'';
    return `<div class="dim"><div class="dim-h"><span>${esc(x.label)}</span><b>${esc(x.value)}</b></div>
      <div class="dim-bar"><i style="width:${pct}%;background:${col}"></i></div>
      <div class="dim-hint">${esc(x.hint)}</div>${ev}</div>`}).join('');
  const hits=Object.entries(d.script_hits||{}).map(([k,arr])=>
    `<div class="hit"><b>${esc((d.script_labels||{})[k]||k)}</b> · 命中 ${arr.length} 段
      <div class="quote">${arr.map(e=>`<a class="evt" data-t="${e.t}">${fmt(e.t)}</a> ${esc(e.text)}`).join('<br>')}</div></div>`).join('')
    ||'<div class="empty" style="padding:18px">没有命中话术词表</div>';
  $('#sumBody').innerHTML=`
    <div class="coach-h"><div class="grade ${gcol}">${g}</div>
      <div class="who"><b>主播：${esc(host.label||'未识别')}</b><br>${host.utterances||0} 句 · ${host.chars||0} 字 · 自动识别，可左侧改名校正</div></div>
    <div class="stats">${tiles.map(t=>`<span class="stat"><b>${esc(t[1])}</b> ${esc(t[0])}</span>`).join('')}</div>
    <div class="sec-t" style="padding:0 12px 6px">维度（条越长越好，点时间码回听原文）</div>
    ${dims}
    <div class="sec-t" style="padding:4px 12px 6px">话术命中</div>
    ${hits}
    <div id="crossBox" style="padding:0 12px 8px"></div>
    <div class="caveats">${(d.caveats||[]).map(c=>`<div>· ${esc(c)}</div>`).join('')}</div>`;
  paintCross();
  $$('#sumBody .evt').forEach(a=>a.onclick=()=>setPlayhead(Number(a.dataset.t),'定位 '+fmt(Number(a.dataset.t))));
}
async function showEvidence(ev){
  if(!ev||!ev.length)return;
  const ids=ev.map(e=>e.id);S.hl=new Set(ids);
  let rows=ev;
  try{
    const d=await api('/api/evidence?session_id='+encodeURIComponent(S.session)+'&ids='+ids.join(','));
    rows=d.rows||ev;
    $('#evidenceBox').innerHTML=`<div class="evidence-box"><div class="et">
      <span>🔎 证据回溯 · ${rows.length} 条原始发言</span><a id="evClose" style="cursor:pointer;color:var(--accent)">关闭</a></div>
      ${rows.map(r=>`<div style="font-size:11.5px;margin:4px 0;line-height:1.6">
        <b style="color:var(--accent);font-family:monospace">${fmt(r.t_start)}</b>
        <span style="color:var(--muted)">${esc(r.speaker_label)}：</span>${esc(r.text)}</div>`).join('')}</div>`;
    $('#evClose').onclick=()=>{$('#evidenceBox').innerHTML='';S.hl.clear();renderList()};
  }catch(e){toast('回溯失败：'+e.message)}
  renderList();
  setPlayhead(ev[0].t_start,ev[0].quote);
  const first=document.getElementById('u'+ids[0]);
  if(first)first.scrollIntoView({block:'center',behavior:'smooth'});
  else $('#listScroll').scrollTop=0;
  toast('已定位到 '+rows.length+' 条原始发言');
}
/* ---------------- 回放播放器：全程只有一个 <audio>，杜绝多段音频叠着播 ---------------- */

export { coach, paintCross, renderCoach, showEvidence };
