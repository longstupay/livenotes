// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { runClassifyAI } from './classify.js';
import { coach } from './coach.js';
import { $, $$, S, api, notifyParent, post, toast } from './core.js';
import { closeDlg, dlgOpen, expDownload, paintCineSub, toggleCineSub, wsCinema, wsFont, wsFull } from './dialog.js';
import { refresh, refreshDebounced, syncChips, updateTimeLabel } from './filters.js';
import { L, liveMini, liveOpen, liveShow, liveStart, liveWs } from './live.js';
import { P, RATES, initPlayer, loadAudio, paintProgress, playAudio, setPlayhead } from './player.js';
import { doSearch, hideGres } from './search.js';
import { loadMeta, loadSessions, renderSessionMenu, selectSession, sessOpen } from './session.js';
import { SET, applyState, closeSettings, fetchModels, newProfile, openSettings, probeModels, profileAction, saveSettings, testLLM } from './settings.js';
import { liveMark, livePause, loadOlderSubs } from './subs.js';
import { aiAnalyze, showSummary, summarize } from './summary.js';
import { liveStop } from './workflow.js';

// 搜索框防抖计时器：只有 bindUI 用，放在这里避免依赖顺序
var _gqTimer=null;

async function init(){
  bindUI();
  initPlayer();
  // 桌面壳切场次时不再重载 iframe：父窗口发消息，这里原地切。
  window.addEventListener('message',function(e){
    const d=e.data||{};
    if(d.type==='ln-open-session'&&d.session){
      selectSession(String(d.session)).then(function(){
        if(d.t!=null)setPlayhead(Number(d.t)||0,'已定位到搜索位置');
        if(d.autoplay)setPlayhead(0,'',true);
        // 哪怕场次没变也回执一次：父窗口靠它确认 iframe 真的切过去了。
        notifyParent('ln-session-changed',{session:S.session});
      }).catch(function(err){toast('切换场次失败：'+err.message)});
    }else if(d.type==='ln-run-search'&&d.q){
      $('#gq').value=String(d.q);doSearch();
    }
  });
  // 支持 ?session=xxx 直接打开某一场（便于收藏/分享链接）
  const params=new URLSearchParams(location.search);
  const want=params.get('session')||'';
  const wantT=parseFloat(params.get('t')||'');
  if(params.has('settings')) openSettings();
  await loadSessions(want||undefined);
  await loadMeta();
  loadAudio();
  await refresh();
  await showSummary('content');
  if(!isNaN(wantT)&&wantT>0){
    setTimeout(function(){ try{ setPlayhead(wantT,'搜索定位') }catch(e){} },600);
  }
  notifyParent('ln-ready',{page:'workbench',session:S.session});
}

/* ---------------- 场次切换器（替代原生 select） ---------------- */
function bindUI(){
  $('#tFrom').oninput=e=>{
    S.tFrom=Math.max(0,Math.min(Number(e.target.value),S.tTo));
    updateTimeLabel();refreshDebounced()};
  $('#tTo').oninput=e=>{
    S.tTo=Math.min(S.duration,Math.max(Number(e.target.value),S.tFrom));
    updateTimeLabel();refreshDebounced()};
  $('#resetTime').onclick=()=>{S.tFrom=0;S.tTo=S.duration;$('#tFrom').value=0;$('#tTo').value=S.duration;refresh()};
  $('#resetCat').onclick=()=>{S.cats.clear();syncChips();refresh()};
  $('#btnClassifyAI').onclick=function(){runClassifyAI(false)};
  $('#clearQ').onclick=()=>{S.q='';$('#q').value='';syncChips();refresh()};
  $('#q').oninput=e=>{S.q=e.target.value.trim();syncChips();refreshDebounced()};
  ['exLow','exOvl','onlyHigh'].forEach(id=>$('#'+id).onchange=e=>{
    S[{exLow:'exLow',exOvl:'exOvl',onlyHigh:'onlyHigh'}[id]]=e.target.checked;refresh()});
  $('#toggleSpk').onclick=()=>{if(S.spks.size)S.spks.clear();else(S.meta.speakers||[]).forEach(p=>S.spks.add(p.speaker_id));syncChips();refresh()};
  $('#btnReset').onclick=()=>{S.cats.clear();S.spks.clear();S.q='';S.exLow=true;S.exOvl=false;S.onlyHigh=false;
    $('#q').value='';$('#exLow').checked=true;$('#exOvl').checked=false;$('#onlyHigh').checked=false;
    $('#resetTime').onclick();syncChips()};
  $('#btnRename').onclick=async()=>{
    const name=$('#renameTo').value.trim();if(!name)return toast('请输入昵称');
    await post('/api/speakers/rename',{session_id:S.session,speaker_id:$('#renameWho').value,name});
    $('#renameTo').value='';await loadMeta();await refresh();toast('已标记身份为「'+name+'」')};
  $('#btnRecluster').onclick=async()=>{
    if(!S.session)return;
    const btn=$('#btnRecluster'),old=btn.textContent;
    btn.disabled=true;btn.textContent='重分中…';
    try{
      const k=Number($('#reclusterK').value)||0;
      const d=await post('/api/speakers/recluster',{session_id:S.session,k});
      S.spks.clear();
      await loadMeta();await refresh();
      S.sumCache={};
      S.mode==='coach'?coach():showSummary(S.mode);
      toast('已重分为 '+d.k+' 人（轮廓 '+d.silhouette+'，改写 '+d.changed+' 条）');
    }catch(e){toast('重新分人失败：'+e.message)}
    finally{btn.disabled=false;btn.textContent=old}};
  $$('#tabs .tab').forEach(b=>b.onclick=()=>{$$('#tabs .tab').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');S.mode=b.dataset.m;showSummary(S.mode)});
  $('#btnSum').onclick=()=>S.mode==='coach'?coach():summarize(S.mode);
  $('#btnAIAnalyze').onclick=aiAnalyze;
  /* 回放：拖动只移动播放头；播放/暂停同一颗按钮，图标跟着真实状态走 */
  $('#btnPlay').onclick=playAudio;
  $('#scrub').oninput=e=>{
    const t=Number(e.target.value);P.drag=true;S.playhead=t;P.seekTo=t;
    if(P.ready&&P.meta){try{P.audio.currentTime=t}catch(err){}}
    paintProgress(t);
  };
  $('#scrub').onchange=()=>{P.drag=false};
  $('#pRate').onclick=()=>{
    P.rate=RATES[(RATES.indexOf(P.rate)+1)%RATES.length];
    $('#pRate').textContent=P.rate===1?'1×':P.rate+'×';
    if(P.audio)P.audio.playbackRate=P.rate;
  };
  /* 场次切换器 */
  $('#sessBtn').onclick=e=>{e.stopPropagation();sessOpen(!$('#sess').classList.contains('open'))};
  $('#sessMenu').onclick=e=>e.stopPropagation();
  $('#sessQ').oninput=renderSessionMenu;
  $('#sessQ').onkeydown=e=>{if(e.key==='Escape'){e.preventDefault();sessOpen(false)}};
  document.addEventListener('click',()=>sessOpen(false));
  /* 改名弹层 */
  $('#dlgOk').onclick=()=>closeDlg($('#dlgInput').value.trim()||null);
  $('#dlgCancel').onclick=()=>closeDlg(null);
  $('#dlgMask').onclick=e=>{if(e.target.id==='dlgMask')closeDlg(null)};
  $('#dlgInput').onkeydown=e=>{
    if(e.key==='Enter'){e.preventDefault();closeDlg($('#dlgInput').value.trim()||null)}
    else if(e.key==='Escape'){e.preventDefault();closeDlg(null)}
  };
  document.onkeydown=e=>{
    if(e.key!=='Escape')return;
    if(dlgOpen())return closeDlg(null);
    if($('#setDrawer').classList.contains('show')){closeSettings();return}
    if($('#liveWs').classList.contains('cinema')){wsCinema(false);return}
    sessOpen(false);
  };
  $('#btnLive').onclick=liveOpen;
  $('#lpStart').onclick=liveStart;
  $('#lpStop').onclick=()=>liveStop();
  $('#wsPause').onclick=livePause;
  $('#wsMark').onclick=liveMark;
  $('#wsFont').onclick=wsFont;
  $('#wsFull').onclick=wsFull;
  $('#wsCinema').onclick=function(){wsCinema()};
  $('#wsCineExit').onclick=function(){wsCinema(false)};
  $('#wsCinePause').onclick=livePause;
  $('#wsCineSub').onclick=toggleCineSub;
  window.addEventListener('beforeunload',function(e){
    if(L.active||L.phase==='stopping'){
      e.preventDefault();
      e.returnValue='正在录制或保存，离开可能会中断，确定吗？';
      return e.returnValue;
    }
  });
  try{
    if(localStorage.getItem('ws.subs')==='0')$('#liveWs').classList.add('nosubs');
  }catch(e){}
  paintCineSub();
  $('#wsCineMark').onclick=liveMark;
  $('#wsCineFont').onclick=wsFont;
  $('#btnExpMd').onclick=function(){expDownload('md')};
  $('#btnExpSrt').onclick=function(){expDownload('srt')};
  $('#btnReader').onclick=function(){
    if(!S.session)return;
    window.open('/reader?session='+encodeURIComponent(S.session),'_blank');
  };
  $('#btnHi').onclick=function(){
    if(!S.session)return;
    window.open('/highlights?session='+encodeURIComponent(S.session),'_blank');
  };
  $('#btnLiveHub').onclick=function(){ window.open('/rooms','_blank') };
  $('#btnLib').onclick=function(){ window.open('/library','_blank') };
$('#btnPeople').onclick=function(){ window.open('/people','_blank') };
  $('#gq').addEventListener('input',function(){
    clearTimeout(_gqTimer);
    _gqTimer=setTimeout(doSearch,260);
  });
  $('#gq').addEventListener('keydown',function(e){
    if(e.key==='Escape'){ $('#gq').value=''; hideGres() }
  });
  document.addEventListener('click',function(e){
    if(!$('#gsearch').contains(e.target)) hideGres();
  });
  $('#btnSettings').onclick=openSettings;
  $('#setClose').onclick=closeSettings;
  $('#setMask').onclick=closeSettings;
  $('#setSave').onclick=saveSettings;
  $('#setTest').onclick=testLLM;
  $('#setFetch').onclick=fetchModels;
  $('#setProbe').onclick=probeModels;
  $('#setTemp').oninput=function(){$('#setTempVal').textContent=$('#setTemp').value};
  $('#setNew').onclick=newProfile;
  $('#savePrefs').onclick=async function(){
    const v=Number($('#prefTopics').value)||10;
    try{
      const p=await post('/api/settings/prefs',{topics:v});
      $('#prefTopics').value=p.topics;
      toast('AI 分类主题数已设为 '+p.topics);
    }catch(e){ toast('保存失败：'+e.message) }
  };
  const openModeSel=$('#prefOpenMode');
  if(openModeSel) openModeSel.onchange=async function(){
    const v=this.value;
    try{
      await post('/api/settings/prefs',{open_mode:v});
      toast('已切换：'+(v==='internal'?'应用内浏览器':'系统浏览器'));
    }catch(e){ toast('保存失败：'+e.message) }
  };
  const captureModeSel=$('#prefCaptureMode');
  if(captureModeSel) captureModeSel.onchange=async function(){
    const v=this.value;
    const label={browser:'应用内浏览器',direct:'抖音直取',external:'外部 Chrome'}[v]||v;
    try{
      await post('/api/settings/prefs',{capture_mode:v});
      toast('采集方式：'+label+'（对下一路生效）');
    }catch(e){ toast('保存失败：'+e.message) }
  };
  const closeActionSel=$('#prefCloseAction');
  if(closeActionSel) closeActionSel.onchange=async function(){
    const v=this.value;
    const label=v==='quit'?'退出程序':'最小化到托盘';
    try{
      await post('/api/settings/prefs',{close_action:v});
      toast('关闭窗口时：'+label);
    }catch(e){ toast('保存失败：'+e.message) }
  };
  $('#prefDirectFallback').onchange=async function(){
    const v=this.value;
    const label={ask:'直取失败后等你确认',auto:'直取失败后自动切浏览器',
      per_room:'直取失败后自动切独立浏览器',shared:'直取失败后自动切共享浏览器',
      none:'只报失败，不切换'}[v]||v;
    try{
      const st=await api('/api/settings');
      if(!st.direct_fallback){
        applyState(st);
        toast('当前服务还是旧版本，重启后才能保存这个设置');
        return;
      }
      await post('/api/settings/prefs',{direct_fallback:v});
      applyState(await api('/api/settings'));
      toast('已保存：'+label);
    }catch(e){ toast('保存失败：'+e.message) }
  };
  $('#setRename').onclick=function(){
    if(!SET.editing){ toast('先选一份配置'); return }
    profileAction('rename',SET.editing);
  };
  $('#lpList').addEventListener('scroll',function(){
    if($('#lpList').scrollTop<48)loadOlderSubs();
  });
  /* 分栏拖拽：比例存 localStorage，双击回到 46% */
  (function(){
    const main=document.querySelector('.ws-main'),sp=$('#wsSplit');
    let r=parseFloat(localStorage.getItem('ws.split')||'')||0.46;
    function apply(v){
      r=Math.min(0.86,Math.max(0.18,v));
      main.style.setProperty('--wsplit',(r*100).toFixed(2)+'%');
    }
    function end(){
      if(!sp.classList.contains('dragging'))return;
      sp.classList.remove('dragging');
      document.body.classList.remove('ws-resizing');
      localStorage.setItem('ws.split',r.toFixed(4));
    }
    apply(r);
    sp.addEventListener('pointerdown',function(e){
      sp.classList.add('dragging');
      document.body.classList.add('ws-resizing');
      try{sp.setPointerCapture(e.pointerId)}catch(err){}
      e.preventDefault();
    });
    sp.addEventListener('pointermove',function(e){
      if(!sp.classList.contains('dragging'))return;
      const b=main.getBoundingClientRect();
      if(b.width>0)apply((e.clientX-b.left)/b.width);
    });
    sp.addEventListener('pointerup',end);
    sp.addEventListener('pointercancel',end);
    sp.addEventListener('dblclick',function(){
      apply(0.46);localStorage.setItem('ws.split','0.46');
    });
  })();
  $('#wsExit').onclick=()=>{liveWs(false);liveMini(true)};
  $('#miniBack').onclick=()=>{liveMini(false);liveWs(true)};
  $('#miniPause').onclick=livePause;
  document.addEventListener('keydown',e=>{
    if(dlgOpen())return;                       // 弹层自己处理 Enter / Esc
    const t=e.target;const typing=t&&/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName);
    if(e.code==='Space'&&!typing){
      e.preventDefault();
      if(L.active)livePause();else playAudio();
      return;
    }
    if(!L.active||typing)return;
    const open=$('#liveWs').classList.contains('show');
    if((e.key==='m'||e.key==='M')&&open)liveMark();
    else if((e.key==='f'||e.key==='F')&&open)wsFull();
    else if((e.key==='v'||e.key==='V')&&open)wsCinema();
    else if((e.key==='s'||e.key==='S')&&open)toggleCineSub();
  });
  document.addEventListener('fullscreenchange',()=>{
    $('#wsFull').innerHTML=document.fullscreenElement?'⛶ 退出全屏':'⛶ 全屏<kbd>F</kbd>';
    if(!document.fullscreenElement&&L.cinema){
      L.cinema=false;$('#liveWs').classList.remove('cinema');
    }
  });
  $('#lpClose').onclick=()=>{
    if(L.active){if(confirm('正在监听，结束并保存本场吗？'))liveStop()}
    else liveShow(false);
  };
}
init().catch(e=>{toast('初始化失败：'+e.message);console.error(e)});

/* ================= 移动端外壳：底部 Tab / 顶栏收纳 / 更多面板 ================= */
(function mobileShell(){
  const MQ=window.matchMedia('(max-width:1024px)');
  const PICK=['#gsearch','#btnLib','#btnPeople','#btnExpMd','#btnExpSrt','#btnReader','#btnHi','#btnSettings'];
  const TABS=['filter','list','sum'];
  const sheet=$('#mSheet'), bin=$('#mSheetBtns'), more=$('#mMore');
  let packed=false,moved=[],anchor=null,cur='list';

  const panels=()=>$$('.grid > .panel');
  function setTab(name){
    if(TABS.indexOf(name)<0)name='list';
    cur=name;
    const idx=TABS.indexOf(name);
    panels().forEach((p,i)=>p.classList.toggle('m-on',i===idx));
    $$('#mTabbar button').forEach(b=>b.classList.toggle('on',b.dataset.t===name));
    const sc=idx===1?$('#listScroll'):(idx===2?$('#sumBody'):null);
    if(sc&&sc.scrollTop>0)sc.scrollTop=0;
  }
  function pack(on){
    if(on===packed)return;
    if(on){
      if(!anchor)anchor=document.createComment('topbar-anchor');
      const first=$('#btnLib');
      if(first&&!anchor.parentNode)first.before(anchor);
      moved=PICK.map(s=>$(s)).filter(Boolean);
      moved.forEach(el=>bin.appendChild(el));
      packed=true;
    }else{
      if(anchor&&anchor.parentNode)moved.forEach(el=>anchor.parentNode.insertBefore(el,anchor));
      moved=[];packed=false;
    }
  }
  function apply(){
    if(MQ.matches){pack(true);setTab(cur);document.body.classList.add('is-mobile')}
    else{pack(false);panels().forEach(p=>p.classList.remove('m-on'));document.body.classList.remove('is-mobile')}
  }
  more.onclick=()=>sheet.classList.add('show');
  sheet.addEventListener('click',function(e){
    if(e.target.closest('[data-close]')){sheet.classList.remove('show');return}
    const b=e.target.closest('button');
    if(b&&bin.contains(b))setTimeout(()=>sheet.classList.remove('show'),60);
  });
  $$('#mTabbar button').forEach(b=>b.onclick=()=>setTab(b.dataset.t));
  const notice=$('#notice');
  if(notice)notice.onclick=function(){if(MQ.matches)notice.classList.toggle('open')};
  if(MQ.addEventListener)MQ.addEventListener('change',apply);
  else if(MQ.addListener)MQ.addListener(apply);
  apply();
})();


export { _gqTimer, bindUI, init };
