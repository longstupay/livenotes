// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, S, api, esc, fmt, post, short, toast } from './core.js';
import { refresh } from './filters.js';
import { L, liveMini, liveShow, liveWs, paintPauseUI } from './live.js';
import { loadAudio } from './player.js';
import { loadMeta, loadSessions } from './session.js';
import { renderLive } from './subs.js';
import { showSummary } from './summary.js';
function liveHint(d){
  var msg='', bad=false;
  if(d.running&&!d.paused){
    if(d.dropped>0){ msg='音频积压：已丢弃 '+d.dropped+' 块，识别可能不完整'; bad=true }
    else if(d.level!=null&&d.level<0.004&&(d.idle_s||0)>12){
      msg='几乎收不到声音：检查标签页是否被静音、音量是否为 0'; bad=true }
    else if((d.idle_s||0)>60){
      msg='已经 '+Math.round(d.idle_s)+' 秒没检测到人声：确认直播还在说话，或 BGM 盖过了人声' }
    else if(d.queued>60){ msg='识别队列积压 '+d.queued+' 句，字幕会延迟' }
    else if(d.asr&&(d.asr.pending_finals||0)>2){
      msg='ASR 排队 '+(d.asr.pending_finals||0)+' 句，字幕会延迟' }
  }
  var el=$('#wsHint');
  el.textContent=msg;
  el.classList.toggle('show',!!msg);
  el.classList.toggle('bad',bad);
}
async function livePoll(){
  if(!L.active||!L.sid)return;
  try{
    const d=await api('/api/live/status?id='+encodeURIComponent(L.sid));
    renderLive(d);$('#lpErr').textContent=d.error||L.err||'';
  }catch(e){$('#lpErr').textContent='状态获取失败：'+e.message}
  finally{if(L.active)L.poll=setTimeout(livePoll,700)}
}
/* ---------------- 收尾状态机：结束保存期间锁住整个工作台 ---------------- */
function wfStep(k,state,note){
  const li=document.querySelector('#wfSteps li[data-k="'+k+'"]');
  if(!li)return;
  const map={pending:'等待',running:'进行中',done:'完成',fail:'失败',skip:'跳过',later:'后台生成中'};
  li.className=state||'';
  const b=li.querySelector('b');
  if(b)b.textContent=note!=null?note:(map[state]||'');
}
function wfOpen(title,sub){
  $('#wfTitle').textContent=title;
  if(sub!=null)$('#wfSub').textContent=sub;
  $('#wfActions').innerHTML='';
  $('#liveWs').classList.add('finishing');
}
function wfClose(){ $('#liveWs').classList.remove('finishing') }
let _wfTimer=null,_wfT0=0,_wfBg=null,_wfGen=0;
function wfTimer(on){
  clearInterval(_wfTimer);
  if(!on)return;
  _wfT0=Date.now();
  const el=$('#wfTime'); if(el)el.textContent='0.0s';
  _wfTimer=setInterval(function(){
    const e=$('#wfTime');
    if(!e){clearInterval(_wfTimer);return}
    e.textContent=((Date.now()-_wfT0)/1000).toFixed(1)+'s';
  },100);
}
function wfActions(btns){
  $('#wfActions').innerHTML=btns.map(function(b,i){
    return '<button data-i="'+i+'" class="'+(b.primary?'primary':'')+'">'+esc(b.label)+'</button>';
  }).join('');
  $$('#wfActions button').forEach(function(el){
    el.onclick=function(){
      const b=btns[Number(el.dataset.i)];
      if(b&&b.onClick)b.onClick();
    };
  });
}
function setLiveLocked(v){ $('#liveWs').classList.toggle('locked',v) }
function teardownCapture(){
  try{L.stream&&L.stream.getTracks().forEach(function(t){t.stop()})}catch(e){}
  try{L.ctx&&L.ctx.close()}catch(e){}
  const v=$('#lpVideo');
  if(v){v.srcObject=null;v.style.display='none'}
  $('#wsVideo').classList.remove('has-video');
  L.stream=L.ctx=L.node=L.src=null;
  const lv=$('#lpLevel'); if(lv)lv.style.width='0%';
  L.paused=false;L.pausePending=false;
  if(typeof paintPauseUI==='function')paintPauseUI();
  clearTimeout(L.poll);
}
function closeFinish(){
  wfClose();setLiveLocked(false);
  liveShow(false);liveWs(false);liveMini(false);
  L.phase='idle';
  const b=$('#lpStop'); if(b){b.disabled=false;b.textContent='结束并保存'}
  $('#lpState')&&($('#lpState').textContent='监听中');
}
async function liveStop(){
  if(L.phase==='stopping'){ toast('正在保存，别急，等它跑完'); return }
  if(!L.sid&&!L.active)return;
  if(!confirm('结束并保存本场？\n\n录音落盘大约 1 秒就好；重聚类、起标题、生成总结会在后台继续跑，不影响你查看。\n保存是服务端做的，中途关掉页面也不会丢。'))return;
  const sid=L.sid;
  L.phase='stopping';
  teardownCapture();                       // 先断采集，避免继续写入
  setLiveLocked(true);
  wfOpen('正在保存本场…','保存由服务端完成，可以先不动，也可以关掉页面——不会丢。');
  wfStep('save','running'); wfStep('tidy','pending'); wfStep('sum','pending');
  wfTimer(true);
  const btn=$('#lpStop'); btn.disabled=true; btn.textContent='保存中…';
  $('#lpState').textContent='正在保存…';
  let d=null;
  try{
    d=await post('/api/live/stop',{session_id:sid});
  }catch(e){
    wfTimer(false);
    wfStep('save','fail',e.message);
    wfOpen('保存失败','录音还留在服务端临时目录，点「重试保存」会再提交一次。');
    L.phase='error';
    wfActions([
      {label:'重试保存',primary:true,onClick:function(){ retryStop(sid) }},
      {label:'先关掉',onClick:function(){ closeFinish() }}
    ]);
    return;
  }
  L.sid='';L.active=false;
  wfStep('save','done',(d.n_utterances||0)+' 句');
  await finishAfterStop(sid,d);
}
async function retryStop(sid){
  L.phase='stopping';
  wfOpen('正在重试保存…','');
  wfStep('save','running');wfStep('tidy','pending');wfStep('sum','pending');
  wfTimer(true);
  try{
    const d=await post('/api/live/stop',{session_id:sid});
    wfStep('save','done',(d.n_utterances||0)+' 句');
    L.sid='';L.active=false;
    await finishAfterStop(sid,d);
  }catch(e){
    wfTimer(false);wfStep('save','fail',e.message);
    wfOpen('还是失败','录音仍在临时目录，可以过一会儿重试，或者先关掉（服务端会在下次启动时自动捞回）。');
    L.phase='error';
    wfActions([{label:'重试保存',primary:true,onClick:function(){ retryStop(sid) }},
               {label:'先关掉',onClick:function(){ closeFinish() }}]);
  }
}
async function finishAfterStop(sid,d){
  const saveLi=document.querySelector('#wfSteps li[data-k="save"]');
  if(saveLi&&saveLi.className!=='done')wfStep('save','done',(d.n_utterances||0)+' 句');
  const n=d.n_utterances||0;
  const dur=Math.round((d.meta&&d.meta.session&&d.meta.session.duration_s)||d.elapsed||0);
  const seed=(d.meta&&d.meta.session&&d.meta.session.title)||'';

  /* 落盘即完成：这一步只花 1 秒左右，没理由让用户陪着后台两步干等 30 秒。
     以前是等轮询跑满 45 次（31.5s）才说「已保存」，而且没等到就直接标「跳过」，
     实际上是后台还在跑，文案在骗人。 */
  wfTimer(false);
  L.phase='done';
  $('#wfMeta').textContent='已保存 · '+n+' 句 · '+fmt(dur);
  wfOpen('已保存','录音已经落盘，这一场可以正常查看了。下面两步在后台跑，跑完自己会变绿。');
  wfActions([
    {label:'查看这一场',primary:true,onClick:function(){ closeFinish() }},
    {label:'关闭',onClick:function(){ closeFinish() }}
  ]);
  wfStep('tidy','running','后台生成中');
  wfStep('sum','running','后台排队中');
  toast('已保存：'+n+' 句 · '+fmt(dur));

  bgFinish(sid,seed,0,++_wfGen);   // 不 await：只是更新那两行状态，不挡任何人
  refreshAfterSave(sid);           // 不 await
}

/* 后台两步的轮询：只更新状态，不再决定「保不保存」。 */
function bgFinish(sid,seed,round,gen){
  clearTimeout(_wfBg);
  const MAX_ROUNDS=100;        // 100 × 0.9s ≈ 90 秒；服务端总超时 75 秒，够覆盖
  _wfBg=setTimeout(async function(){
    if(gen!==_wfGen)return;    // 又结束了一场，这次的结果别再往面板上写
    if(L.active)return;        // 用户已经开了下一场
    let tidy=false,sum=false,title=seed;
    try{
      const s=await api('/api/sessions');
      const cur=(s.sessions||[]).filter(function(x){return x.id===sid})[0];
      if(cur&&cur.title&&cur.title!==seed){ tidy=true; title=cur.title }
    }catch(e){}
    if(!sum){
      try{
        const r=await api('/api/summary?session_id='+encodeURIComponent(sid)+'&scope=content');
        if(r&&r.ok)sum=true;
      }catch(e){}
    }
    const short=String(title).length>16?(String(title).slice(0,15)+'…'):String(title);
    if(tidy)wfStep('tidy','done',short);
    if(sum){
      if(!tidy)wfStep('tidy','done',short&&short!==seed?short:'未改标题');
      wfStep('sum','done');
      refreshAfterSave(sid,true);   // 总结到了，把逐字稿/总结面板刷新一下
      return;
    }
    if(round+1>=MAX_ROUNDS){
      if(!tidy)wfStep('tidy','later','后台仍在跑');
      wfStep('sum','later','后台仍在跑');
      return;
    }
    bgFinish(sid,seed,round+1,gen);
  },900);
}

/* 保存后刷新视图。quiet=true 时只在用户还停在这一场才刷，避免抢走别人的画面。 */
async function refreshAfterSave(sid,quiet){
  if(quiet&&S.session&&S.session!==sid)return;
  try{
    await loadSessions(sid);await loadMeta();loadAudio();await refresh();
    S.mode='content';S.sumCache={};
    $$('#tabs .tab').forEach(function(x){x.classList.toggle('on',x.dataset.m==='content')});
    await showSummary('content');
  }catch(e){}
}

export { _wfTimer, bgFinish, closeFinish, finishAfterStop, liveHint, livePoll, liveStop, refreshAfterSave, retryStop, setLiveLocked, teardownCapture, wfActions, wfClose, wfOpen, wfStep, wfTimer };
