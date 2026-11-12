// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, S, api, fmt, short, toast } from './core.js';
const P={audio:null,ready:false,meta:false,dur:0,rate:1,drag:false,seekTo:0,state:''};
const RATES=[1,1.25,1.5,2];
const audioUrl=(sid,dur)=>`/api/audio?session_id=${encodeURIComponent(sid)}&t_start=0&t_end=${encodeURIComponent(dur||0)}`;
// 服务端是否支持 Range（新版整流返回 206）。只探一次，老服务端就退回「整段切片」地址。
let AUDIO_STREAM=null;
async function detectStream(sid){
  if(AUDIO_STREAM!==null)return AUDIO_STREAM;
  try{
    const r=await fetch('/api/audio?session_id='+encodeURIComponent(sid),{headers:{Range:'bytes=0-1'}});
    AUDIO_STREAM=(r.status===206);
    try{r.body&&r.body.cancel&&r.body.cancel()}catch(e){}
  }catch(e){AUDIO_STREAM=false}
  return AUDIO_STREAM;
}
function initPlayer(){
  const a=new Audio();
  a.preload='metadata';
  P.audio=a;
  a.addEventListener('loadedmetadata',()=>{
    P.meta=true;
    P.dur=(Number.isFinite(a.duration)&&a.duration>0)?a.duration:(S.duration||P.dur);
    if(P.seekTo){try{a.currentTime=P.seekTo}catch(e){}}
    $('#scrub').max=Math.max(1,P.dur);
    paintProgress(a.currentTime||0,P.dur);
  });
  a.addEventListener('play',()=>paintPlay(true));
  a.addEventListener('pause',()=>paintPlay(false));
  a.addEventListener('ended',()=>paintPlay(false));
  a.addEventListener('timeupdate',()=>{
    if(P.drag)return;
    S.playhead=a.currentTime;
    paintProgress(a.currentTime,P.dur||S.duration);
  });
  a.addEventListener('error',()=>{
    if(!P.ready||!a.getAttribute('src'))return;
    paintPlay(false);
    $('#pNote').textContent='这一段音频读不出来（文件可能已被移除）';
  });
}
async function loadAudio(){
  const a=P.audio;if(!a)return;
  const sid=S.session;
  a.pause();a.removeAttribute('src');a.load();
  P.meta=false;P.dur=0;P.seekTo=0;S.playhead=0;P.ready=false;
  paintPlay(false);
  const s=S.sessions.find(x=>x.id===sid)||{};
  // 后端给的 audio_state 才是事实：ready / missing / none / live。
  // 老服务端没有这个字段时才退回看 audio_path，避免网页版直接打不开。
  const state=s.audio_state||(s.audio_path?'ready':'none');
  P.state=state;
  const has=state==='ready';
  P.ready=has;
  $('#btnPlay').disabled=!has;$('#scrub').disabled=!has;$('#pRate').disabled=!has;
  if(!has){
    $('#pNote').textContent=state==='missing'
      ? '录音文件已被清理，逐字稿还在'
      : state==='live'
      ? '这一场还在录制，停止并收尾后才能回放'
      : '本场没有原始音频，只能按时间码看文字';
    paintUnavailable();
    return;
  }
  P.dur=S.duration||0;
  $('#pNote').textContent='拖动进度条或点时间码定位';
  paintProgress(0,S.duration);
  const stream=await detectStream(sid);
  if(S.session!==sid)return;                       // 探测期间又切了场次，让新的那次接管
  a.src=stream?('/api/audio?session_id='+encodeURIComponent(sid)):audioUrl(sid,S.duration);
  a.playbackRate=P.rate;
  a.load();
  // 右键菜单「从开头播放」会带 autoplay=1；被浏览器策略拦下就提示，不假装已播。
  try{
    if(new URLSearchParams(location.search).get('autoplay')==='1'){
      a.play().catch(()=>{$('#pNote').textContent='浏览器拦截了自动播放，点播放键开始'});
    }
  }catch(e){}
}
function paintUnavailable(){
  const sc=$('#scrub');
  if(sc){sc.value=0;sc.style.setProperty('--p','0%')}
  $('#pCur').textContent='00:00';
  $('#pDur').textContent='—';
  paintPlay(false);
}
function paintPlay(on){
  $('#playIcon').textContent=on?'⏸':'▶';
  $('#btnPlay').classList.toggle('playing',on);
  $('#btnPlay').title=on?'暂停（空格）':'播放（空格）';
}
function paintProgress(t,dur){
  if(P.state&&P.state!=='ready'){
    const long=(P.dur||S.duration||0)>=3600;
    $('#pCur').textContent=long?fmt(t||0):short(t||0);
    $('#pDur').textContent='—';
    return;
  }
  const d=Math.max(1,dur||P.dur||S.duration||1);
  const p=Math.max(0,Math.min(1,(t||0)/d));
  const sc=$('#scrub');
  if(!P.drag)sc.value=Math.max(0,Math.min(d,t||0));
  sc.style.setProperty('--p',(p*100).toFixed(2)+'%');
  const tm=v=>d<3600?short(v):fmt(v);      // 不到 1 小时就不显示小时位
  $('#pCur').textContent=tm(t||0);
  $('#pDur').textContent=tm(d);
}
function playAudio(){
  const a=P.audio;
  if(!P.ready){
    toast(P.state==='missing'?'本场录音文件已被清理':
      P.state==='live'?'这一场还在录制，停止后才能回放':'本场没有原始音频');
    return;
  }
  if(!a.paused){a.pause();return}
  if(a.ended||(P.dur&&a.currentTime>=P.dur-0.2)){S.playhead=0;P.seekTo=0}
  P.seekTo=S.playhead;
  if(P.meta){try{a.currentTime=S.playhead}catch(e){}}
  a.play().catch(()=>toast('播放失败：文件可能损坏或格式不支持'));
}
/* 把时间轴对焦到某个时刻（或某段区间）：画播放头 + 高亮区间 */
function focusTimeline(t0,t1,keep){
  const el=$('#tlBars'); if(!el)return;
  const dur=Math.max(1,S.duration);
  const a=Math.max(0,Math.min(dur,Number(t0)||0));
  const b=Math.max(a+0.5,Math.min(dur,t1==null?a+Math.max(8,dur*0.01):Number(t1)||a));
  const head=$('#tlHead'), band=$('#tlFocus');
  if(head){head.style.display='block';head.style.left=(a/dur*100)+'%'}
  if(band){band.style.display='block';band.style.left=(a/dur*100)+'%';
    band.style.width=Math.max(0.25,(b-a)/dur*100)+'%'}
  $$('#tlBars .bucket').forEach(function(x){
    const ba=Number(x.dataset.a), bb=Number(x.dataset.b);
    x.classList.toggle('focus', b>ba && a<bb);
  });
  const hit=$('#tlBars .bucket.focus');
  if(hit&&el.scrollWidth>el.clientWidth){
    try{hit.scrollIntoView({block:'nearest',inline:'center'})}catch(e){}
  }
  const tag=$('#tlFocusTag');
  if(tag){ tag.textContent='◉ '+fmt(a)+'–'+fmt(b); tag.title='当前对焦：'+fmt(a)+' 到 '+fmt(b) }
}
/* 定位时把记录列表滚到那句、并闪一下，解决"不知道打到哪一句" */
function focusUtterance(t){
  if(!S.rows||!S.rows.length)return null;
  let best=null;
  for(var i=0;i<S.rows.length;i++){
    const r=S.rows[i];
    if(r.t_start<=t+0.05&&t<=r.t_end+0.05){best=r;break}
  }
  if(!best){
    best=S.rows.reduce(function(a,b){
      return Math.abs(b.t_start-t)<Math.abs(a.t_start-t)?b:a});
  }
  const el=document.getElementById('u'+best.id);
  if(!el)return best;
  try{el.scrollIntoView({block:'center',behavior:'smooth'})}catch(e){}
  el.classList.remove('focusrow');
  void el.offsetWidth;
  el.classList.add('focusrow');
  setTimeout(function(){ el.classList.remove('focusrow') },2200);
  return best;
}
function setPlayhead(t,note,autoplay,t1){
  t=Math.max(0,Math.min(P.dur||S.duration,t||0));
  S.playhead=t;P.seekTo=t;
  if(P.ready&&P.audio&&P.meta){try{P.audio.currentTime=t}catch(e){}}
  paintProgress(t);
  focusTimeline(t,t1);
  focusUtterance(t);
  if(note)$('#pNote').textContent=String(note).slice(0,80);
  if(autoplay)playAudio();
}

/* ---------------- 实时监听：标签页音频 -> 本地 ASR ---------------- */

export { AUDIO_STREAM, P, RATES, audioUrl, detectStream, focusTimeline, focusUtterance, initPlayer, loadAudio, paintPlay, paintProgress, playAudio, setPlayhead };
