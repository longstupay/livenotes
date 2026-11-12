// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, S, api, emitHost, esc, fmt, notifyParent, post, toast } from './core.js';
import { askText } from './dialog.js';
import { refresh, renderFilters, renderMarksBar, renderTimePresets, updateTimeLabel } from './filters.js';
import { L } from './live.js';
import { loadAudio } from './player.js';
import { paintTitleBar, resumeRun, showSummary } from './summary.js';
const MODE_NAME={tab:'标签页采集',per_uid:'分轨录制',mixed:'混音兜底',direct:'直取直播流'};
function dtShort(iso){
  const d=new Date(iso||'');if(isNaN(d))return '';
  const p=n=>String(n).padStart(2,'0');
  return `${p(d.getMonth()+1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function dayLabel(iso){
  const d=new Date(iso||'');if(isNaN(d))return '其他';
  const now=new Date(),y=new Date();y.setDate(now.getDate()-1);
  const same=(a,b)=>a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()&&a.getDate()===b.getDate();
  if(same(d,now))return '今天';
  if(same(d,y))return '昨天';
  return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日`;
}
function paintSessionBtn(){
  const s=S.sessions.find(x=>x.id===S.session)||{};
  $('#sessTitle').textContent=s.title||'没有场次';
  $('#sessMeta').textContent=[s.started_at?dtShort(s.started_at):'',
    s.duration_s?fmt(s.duration_s):'',s.n_utterances!=null?`${s.n_utterances} 条`:'']
    .filter(Boolean).join(' · ');
}
async function renameSession(id){
  if(!id)return;
  let groups=[];
  try{
    // 0 次模型调用：候选来自刚才那次总结，读缓存而已
    const sg=await api('/api/sessions/suggest_title?session_id='+encodeURIComponent(id));
    groups=(sg.groups||[]).filter(g=>g.items&&g.items.length);
  }catch(e){}
  const cur=(S.sessions.find(x=>x.id===id)||{}).title||'';
  const v=await askText({
    title:'给这场起个名字',
    desc:'只改显示名，逐字稿、总结、导出都不动。候选来自这次总结，不会再调一次模型。',
    value:cur,
    placeholder:'例如：大棚菜销路：连麦里问最多的三个坑',
    groups:groups});
  if(v==null)return;
  const name=v.trim();
  if(!name)return;
  try{
    await post('/api/sessions/rename',{session_id:id,title:name});
    const r=await api('/api/sessions');
    S.sessions=r.sessions||[];
    renderSessionMenu();paintSessionBtn();
    if(id===S.session){
      await loadMeta();
      // 人工命名即锁定：标题条改成「当前名」，别再劝你采用
      paintTitleBar({title:name,title_alts:[],title_applied:true,
        title_reason:'你改的名字，之后自动总结不会再覆盖'});
    }
    toast('已改名为「'+name+'」');
  }catch(e){toast('改名失败：'+e.message)}
}
function renderSessionMenu(){
  const q=($('#sessQ').value||'').trim().toLowerCase();
  const list=S.sessions.filter(s=>!q||String(s.title).toLowerCase().includes(q)
    ||String(s.started_at||'').includes(q));
  if(!list.length){$('#sessList').innerHTML='<div class="sess-empty">没有匹配的场次</div>';return}
  const groups=[];
  list.forEach(s=>{
    const k=dayLabel(s.started_at);
    let g=groups.find(x=>x.k===k);
    if(!g){g={k,items:[]};groups.push(g)}
    g.items.push(s);
  });
  $('#sessList').innerHTML=groups.map(g=>
    `<div class="sess-day">${esc(g.k)}</div>`+g.items.map(s=>{
      const ast=s.audio_state||(s.audio_path?'ready':'none');
      const atxt=ast==='ready'?'有录音':ast==='missing'?'录音丢失':ast==='live'?'录制中':'无录音';
      return `
      <button class="sess-row ${s.id===S.session?'on':''}" data-id="${esc(s.id)}">
        <span class="sr-top"><b>${esc(s.title)}</b><span class="sr-dur">${fmt(s.duration_s||0)}</span><span class="sr-edit" data-id="${esc(s.id)}" title="重命名这一场">✎</span></span>
        <span class="sr-sub">
          <span>${(dtShort(s.started_at).split(' ')[1])||''}</span><span class="sep">·</span>
          <span>${s.n_utterances||0} 条</span><span class="sep">·</span>
          <span>${MODE_NAME[s.source_mode]||(s.is_demo?'演示数据':'未知来源')}</span>
          <span class="sr-audio ${ast==='ready'?'':'no'}" title="${atxt}">${atxt}</span>
        </span></button>`}).join('')).join('');
  $$('#sessList .sess-row').forEach(b=>b.onclick=()=>selectSession(b.dataset.id));
  $$('#sessList .sr-edit').forEach(e=>e.onclick=ev=>{
    ev.stopPropagation();ev.preventDefault();renameSession(e.dataset.id);
  });
}
function sessOpen(v){
  $('#sess').classList.toggle('open',v);
  if(v){$('#sessQ').value='';renderSessionMenu()}
}
async function selectSession(id){
  if(!id)return;
  if(L.phase==='stopping'){ toast('正在保存上一场，先等它完成'); return }
  sessOpen(false);
  if(id===S.session)return;
  S.session=id;S.hl.clear();S.mode='content';S.sumCache={};
  try{history.replaceState(null,'','?session='+encodeURIComponent(id))}catch(e){}
  $$('#tabs .tab').forEach(x=>x.classList.toggle('on',x.dataset.m==='content'));
  paintSessionBtn();
  await loadMeta();
  loadAudio();
  await refresh();
  await showSummary('content');
  paintSessionBtn();
  notifyParent('ln-session-changed',{session:S.session});
  emitHost('session-change',{session:S.session});
}
async function loadSessions(selectId){
  const {sessions}=await api('/api/sessions');
  S.sessions=sessions||[];
  let id=selectId||S.session;
  if(!id||!S.sessions.some(s=>s.id===id)){
    const best=S.sessions.find(s=>s.audio_state==='ready'&&Number(s.n_utterances)>0)
      ||S.sessions.find(s=>Number(s.n_utterances)>0)||S.sessions[0];
    id=best?best.id:'';
  }
  if(id)S.session=id;
  if(id){try{history.replaceState(null,'','?session='+encodeURIComponent(id))}catch(e){}}
  paintSessionBtn();renderSessionMenu();
  return S.sessions;
}
async function loadMeta(){
  const m=await api('/api/meta?session_id='+encodeURIComponent(S.session));S.meta=m;
  const s=m.session||{};S.duration=Number(s.duration_s)||7200;
  S.tFrom=0;S.tTo=S.duration;S.cats=new Set();S.spks=new Set();S.q='';S.hl.clear();
  ['#tFrom','#tTo','#scrub'].forEach(id=>{$(id).max=S.duration});
  $('#tFrom').value=0;$('#tTo').value=S.duration;$('#q').value='';
  S.playhead=0;
  renderTimePresets();
  const badge=$('#modeBadge');
  badge.textContent=s.source_mode==='per_uid'?'分轨录制 · 身份已确定':
    s.source_mode==='tab'?'标签页实时采集 · 声纹聚类':
    s.source_mode==='direct'?'直取直播流 · 声纹聚类':
    s.source_mode==='mixed'?'混音兜底 · 说话人为聚类标签':'演示数据';
  badge.className='badge'+(s.source_mode==='mixed'?' mixed':
    (s.source_mode==='tab'||s.source_mode==='direct')?' live':'');
  $('#ftsTag').textContent=m.fts_trigram?'全文索引 FTS5 trigram':'全文索引 LIKE 兜底';
  $('#noticeText').textContent=s.source_mode==='per_uid'
    ? '分轨模式：身份由 uid 确定，重叠与分离问题已规避。'
    : s.source_mode==='tab'
    ? '标签页实时采集：只录被选中标签页的音频；说话人由声纹在线聚类，抢话段可能归错，可在左侧改名校正。'
    : s.source_mode==='direct'
    ? '直取直播流：解析流地址后由 ffmpeg 直接拉音频，不加载播放器页面；说话人由声纹在线聚类，抢话段可能归错，可在左侧改名校正。'
    : '观众端只有一路混音流；重叠段已标记并剔除，说话人标签需人工校正。点右上「风险与说明」看完整评估。';
  $('#tlHint').textContent=`未转写 ${Math.round((s.untranscribed_seconds||0)/60)} 分 / 重叠 ${Math.round(s.overlap_seconds||0)} 秒`;
  // 人工标记的高价值句：刷新列表时把星标恢复出来，重算高光也不会丢。
  S.manualHl=new Set();
  try{
    const h=await api('/api/highlights?session_id='+encodeURIComponent(S.session));
    (h.highlights||[]).forEach(function(x){
      if(x.kind==='manual'&&x.utterance_id)S.manualHl.add(Number(x.utterance_id));
    });
  }catch(e){}
  renderFilters();updateTimeLabel();renderMarksBar(m.marks||[]);
  resumeRun();
}

export { MODE_NAME, dayLabel, dtShort, loadMeta, loadSessions, paintSessionBtn, renameSession, renderSessionMenu, selectSession, sessOpen };
