// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, api, copyText, esc, fmt, post, short, spkColor, toast } from './core.js';
import { askText, wsRejectHint, wsRename } from './dialog.js';
import { L, paintPauseUI } from './live.js';
import { mark } from './search.js';
import { liveHint } from './workflow.js';
function subKey(u){return u.t_start+'|'+u.text}
function mergeSubs(rows){
  if(!rows||!rows.length)return 0;
  const idx=new Map();
  L.hist.forEach(function(r){idx.set(subKey(r),r)});
  let added=0;
  rows.forEach(function(r){
    const k=subKey(r);
    const cur=idx.get(k);
    if(cur){
      // 已经渲染过的句子：说话人改过名就把旧行也刷新，不然一条字幕里会同时出现新旧两个名字
      if(cur.speaker_label!==r.speaker_label||cur.speaker_id!==r.speaker_id){
        cur.speaker_label=r.speaker_label;
        cur.speaker_id=r.speaker_id;
      }
    }else{
      L.hist.push(r);idx.set(k,r);added++;
    }
  });
  if(added)L.hist.sort(function(a,b){return a.t_start-b.t_start});
  if(L.hist.length>1200)L.hist=L.hist.slice(-1200);
  return added;
}
var _spkMenu=null;
// 实时字幕里标过的高价值句：只在本场会话内记着；落库后可在工作台/高光榜看到。
var _hlMarked=new Set();
function hlKey(u){return u.id?('i'+u.id):('t'+u.t_start)}
function isMarked(u){return _hlMarked.has(hlKey(u))}
async function toggleMarked(u){
  if(!L.sid)return;
  try{
    const out=await post('/api/highlights/manual',{session_id:L.sid,
      utterance_id:u.id||0,t_start:u.t_start,text:u.text,speaker_label:u.speaker_label});
    if(out.highlighted)_hlMarked.add(hlKey(u));else _hlMarked.delete(hlKey(u));
    paintSubs(L.partial);
    toast(out.highlighted?'已标记高价值':'已取消高价值标记');
  }catch(e){toast('标记失败：'+e.message)}
}
function openSubMenu(ev,u){
  // 同 list.js：右键菜单由宿主提供，网页版走浏览器原生菜单。
  if(!window.LN||!window.LN.menu)return;
  ev.preventDefault();
  let sel='';
  try{sel=String(getSelection()||'').trim()}catch(e){}
  window.LN.menu([
    {label:'改说话人',run:function(){openSpeakerMenu(ev,u)}},
    {label:isMarked(u)?'取消高价值标记':'标记高价值',run:function(){toggleMarked(u)}},
    {sep:true},
    {label:'复制文字',run:function(){copyText(u.text)}},
    {label:sel?('搜索「'+sel.slice(0,14)+'」'):'在全库搜索这句话',run:function(){
      window.LN.search(sel||u.text);
    }}
  ],ev.clientX,ev.clientY);
}
function closeSpeakerMenu(){ if(_spkMenu){_spkMenu.remove();_spkMenu=null} }
function openSpeakerMenu(ev,u){
  closeSpeakerMenu();
  const list=(L.speakers||[]);
  const m=document.createElement('div');
  m.className='spkmenu';
  m.innerHTML='<div class="mh">这句话是谁说的？</div>'
    +list.map(function(p){
        return '<div class="mi" data-spk="'+esc(p.speaker_id)+'" data-label="'+esc(p.label)+'">'
          +'<i class="dot" style="background:'+spkColor(p.speaker_id)+'"></i>'+esc(p.label)+'</div>';
      }).join('')
    +'<div class="mi new" data-spk="__new__">＋ 新建说话人…</div>';
  document.body.appendChild(m);
  _spkMenu=m;
  const w=m.offsetWidth||180, h=m.offsetHeight||200;
  m.style.left=Math.min(ev.clientX, innerWidth-w-12)+'px';
  m.style.top=Math.min(ev.clientY, innerHeight-h-12)+'px';
  $$('.spkmenu .mi').forEach(function(el){
    el.onclick=function(e2){
      e2.stopPropagation();
      const spk=el.dataset.spk;
      closeSpeakerMenu();
      if(spk==='__new__'){
        askText({title:'新建说话人',desc:'给这个人起个名字，这句话会归到他名下。',value:'',placeholder:'例如：嘉宾小林'})
          .then(function(name){ if(name&&name.trim()) doReassign(u,'__new__',name.trim()) });
        return;
      }
      doReassign(u, spk, el.dataset.label);
    };
  });
  setTimeout(function(){ document.addEventListener('mousedown',closeSpeakerMenu,{once:true}) },0);
}
async function doReassign(u, spk, label){
  try{
    const body={session_id:L.sid, speaker_id:spk, label:label};
    if(u.id)body.utterance_id=u.id; else body.t_start=u.t_start;
    const r=await post('/api/utterances/reassign',body);
    if(!r.ok){ toast(r.error||'改判失败'); return }
    L.hist.forEach(function(x){
      if((u.id&&x.id===u.id)||(!u.id&&x.t_start===u.t_start&&x.text===u.text)){
        x.speaker_id=r.speaker_id; x.speaker_label=r.label;
      }
    });
    paintSubs(L.partial);
    toast('已改为「'+r.label+'」');
  }catch(e){ toast('改判失败：'+e.message) }
}
function paintSubs(partial){
  const el=$('#lpList');
  const atBottom=el.scrollHeight-el.scrollTop-el.clientHeight<40;
  const rows=L.hist.map(function(u){
    return '<div class="lp-u'+(isMarked(u)?' hlmark':'')+'" data-uid="'+(u.id||'')+'" data-t="'+u.t_start+'">'
      +'<span class="lp-t">'+short(u.t_start)+'</span>'
      +'<span class="lp-s" style="color:'+spkColor(u.speaker_id)+'" title="点一下改判这句话是谁说的">'
      +esc(u.speaker_label)+'</span>'
      +'<span class="lp-x">'+esc(u.text)+'</span></div>';
  });
  const last=L.hist.length?L.hist[L.hist.length-1].text.trim():'';
  if(partial&&partial.text&&partial.text.trim()!==last){
    rows.push('<div class="lp-u lp-partial" title="这句还没说完，文字会被下一版替换">'
      +'<span class="lp-t">'+short(partial.t_start)+'</span>'
      +'<span class="lp-s">识别中</span>'
      +'<span class="lp-x">'+esc(partial.text)+'…</span></div>');
  }
  const keepTop=atBottom?null:el.scrollTop;
  el.innerHTML=rows.join('')||'<div class="ws-empty">等待说话…<br>主播开口后约 1 秒出字</div>';
  $$('#lpList .lp-s').forEach(function(el){
    el.onclick=function(ev){
      ev.stopPropagation();
      const row=el.closest('.lp-u');
      const uid=row.dataset.uid?Number(row.dataset.uid):null;
      const t=Number(row.dataset.t);
      const cur=L.hist.filter(function(x){return (uid&&x.id===uid)||(!uid&&x.t_start===t)})[0]||{};
      openSpeakerMenu(ev,cur);
    };
  });
  $$('#lpList .lp-u').forEach(function(el){
    el.oncontextmenu=function(ev){
      const uid=el.dataset.uid?Number(el.dataset.uid):null;
      const t=Number(el.dataset.t);
      const cur=L.hist.filter(function(x){return (uid&&x.id===uid)||(!uid&&x.t_start===t)})[0]||{};
      openSubMenu(ev,cur);
    };
  });
  if(atBottom)el.scrollTop=el.scrollHeight;
  else if(keepTop!==null)el.scrollTop=keepTop;
  $('#wsCount').textContent=(L.total||L.hist.length)+' 句';
}
async function loadOlderSubs(){
  if(!L.sid||!L.hasMore||L.loading)return;
  const first=L.hist[0];
  if(!first)return;
  const el=$('#lpList');
  L.loading=true;
  try{
    const d=await api('/api/live/utterances?id='+encodeURIComponent(L.sid)
      +'&before_t='+first.t_start+'&limit=120');
    const rows=d.rows||[];
    if(rows.length<120)L.hasMore=false;
    const seen=new Set(L.hist.map(subKey));
    const older=rows.filter(function(r){return !seen.has(subKey(r))});
    if(!older.length)return;
    const keep=el.scrollHeight-el.scrollTop;
    L.hist=older.concat(L.hist);
    paintSubs(null);
    el.scrollTop=el.scrollHeight-keep;
  }catch(e){}
  finally{L.loading=false}
}
function renderLive(d){
  // 暂停状态以本地为准（立刻反馈），轮询只用来纠正别处改过的状态
  if(!L.pausePending&&Date.now()-(L.pauseAt||0)>1200&&typeof d.paused==='boolean'&&d.paused!==L.paused){
    L.paused=d.paused;paintPauseUI();
  }
  $('#lpTimer').textContent=fmt(d.elapsed||0);
  $('#miniTimer').textContent=fmt(d.elapsed||0);
  const st=d.error?'出错':!d.running?'已结束':L.paused?'已暂停':d.speech?'正在说话…':'监听中';
  $('#lpState').textContent=st;$('#wsSub').textContent=L.paused?'已暂停':'直播中';
  paintPauseUI();
  const silent=Math.max(0,(d.elapsed||0)-(d.speech_s??d.recognized_s??0));
  $('#lpStats').textContent=`已转写 ${Math.round(d.recognized_s||0)}s · 人声 ${Math.round(d.speech_s||0)}s · 静音 ${Math.round(silent)}s${d.dropped?` · 丢弃 ${d.dropped}`:''}`;
  const sp=d.speakers||[];
  L.speakers=sp;
  const rec=Math.max(1,d.recognized_s||0);
  $('#wsMetrics').innerHTML=[
    `<span class="ws-metric">语速 <b>${((d.chars||0)/rec).toFixed(1)}</b> 字/秒</span>`,
    `<span class="ws-metric">提问 <b>${d.questions||0}</b> 次</span>`,
    `<span class="ws-metric" title="输入缓冲块 / ASR 定稿排队">队列 <b>${d.queued||0}</b>${d.asr&&d.asr.pending_finals?`+${d.asr.pending_finals}`:''}</span>`,
    `<span class="ws-metric">打点 <b>${(d.marks||L.marks||[]).length}</b></span>`,
  ].join('');
  const tot=sp.reduce((a,p)=>a+(p.seconds||0),0)||1;
  const spName=p=>p.person_name||p.label;
  $('#wsRatio').innerHTML=sp.length
    ? `<span class="bar">${sp.map(p=>`<i style="flex:${Math.max(0.5,p.seconds||0)};background:${spkColor(p.speaker_id)}"></i>`).join('')}</span>
       <span>${sp.map(p=>`${esc(spName(p))} ${Math.round((p.seconds||0)/tot*100)}%`).join(' · ')}</span>`
    : '';
  $('#lpSpeakers').innerHTML=sp.map(p=>{
    const q=p.person_state==='cand'?'（疑似）':'';
    const sc=p.person_score!=null?' · 声纹 '+p.person_score:'';
    const nm=spName(p);
    const no=(p.person_state==='cand'||p.person_state==='auto')
      ?`<b class="ws-no" data-no="${esc(p.speaker_id)}" title="不是他：只清掉这次判断，不写底库、不注册">×</b>`:'';
    return `<span class="chip" data-spk="${esc(p.speaker_id)}" data-label="${esc(nm)}" title="点一下改成真实身份（如 主播小A / 嘉宾林总）${sc}"><i class="dot" style="background:${spkColor(p.speaker_id)}"></i>${esc(nm)}${q} ${fmt(p.seconds).replace(/^00:/,'')} · ${p.count}句${no}</span>`;
  }).join('');
  $$('#lpSpeakers .chip').forEach(e=>e.onclick=()=>wsRename(e.dataset.spk,e.dataset.label));
  $$('#lpSpeakers .ws-no').forEach(e=>e.onclick=(ev)=>{ev.stopPropagation();wsRejectHint(e.dataset.no)});
  if(d.marks)L.marks=d.marks;
  renderMarks();
  L.total=Number(d.n_utterances||0)||(L.total||0);
  mergeSubs(d.utterances||[]);
  paintSubs(d.partial);
  $('#wsErr').textContent=d.error||L.err||'';
  liveHint(d);
}
function renderMarks(){
  $('#wsMarks').innerHTML=(L.marks||[]).map(m=>
    `<span class="ws-mark" title="${esc(m.note||'打点')}">📍 ${short(m.t)}</span>`).join('');
}
async function livePause(){
  if(!L.active||!L.sid)return;
  const want=!L.paused;
  L.paused=want;L.pauseAt=Date.now();L.pausePending=true;
  paintPauseUI();
  if(want)$('#lpLevel').style.width='0%';
  $('#lpState').textContent=want?'已暂停（不计时）':'监听中';
  try{
    await post('/api/live/pause',{session_id:L.sid,paused:want});
    toast(want?'已暂停：这段不会被记录':'已继续');
  }catch(e){
    L.paused=!want;L.pauseAt=Date.now();paintPauseUI();
    toast('暂停失败：'+e.message);
  }finally{L.pausePending=false}
}
async function liveMark(){
  if(!L.active||!L.sid)return;
  try{
    const d=await post('/api/live/mark',{session_id:L.sid,note:''});
    L.marks=d.marks||[];
    renderMarks();
    toast('已打点 '+short((d.mark||{}).t||0)+'（回看时可在时间轴上方看到）');
  }catch(e){toast('打点失败：'+e.message)}
}
/* ---- 改名：自绘弹层，替掉原生 prompt() ---- */

export { _spkMenu, closeSpeakerMenu, doReassign, liveMark, livePause, loadOlderSubs, mergeSubs, openSpeakerMenu, paintSubs, renderLive, renderMarks, subKey };
