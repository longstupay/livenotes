// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { coach, showEvidence } from './coach.js';
import { $, $$, CAT_META, S, api, esc, fmt, post, toast } from './core.js';
import { activeFilters, refresh, renderFilters } from './filters.js';
import { setPlayhead } from './player.js';
import { paintSessionBtn, renderSessionMenu } from './session.js';
const SUM_TAB_NAME={content:'按内容',time:'按时间段',category:'按分类',filtered:'当前筛选'};
/* 切 tab 只读已有结果：先看内存缓存，再看库里存过的，都没有才让用户点生成 */
async function showSummary(mode){
  if(mode==='coach'){ coach(); return }
  if(mode==='content')tbarHide();       // 换场次时先清掉上一场标题，有结果再画回来
  if(S.sumCache[mode]){ renderSummary(S.sumCache[mode]); return }
  try{
    const r=await api('/api/summary?session_id='+encodeURIComponent(S.session)
      +'&scope='+encodeURIComponent(mode));
    if(r.ok&&r.payload&&((r.payload.points||[]).length||r.payload.overview)){
      S.sumCache[mode]=r.payload;
      renderSummary(r.payload);
      return;
    }
  }catch(e){}
  summaryEmpty(mode);
}
function summaryEmpty(mode){
  $('#sumBody').innerHTML='<div class="empty">还没有生成过「'+(SUM_TAB_NAME[mode]||'')+'」的总结<br>'
    +'<span style="font-size:11.5px">不会自动调用模型，点下面的按钮才会生成</span><br>'
    +'<button class="primary" id="genNow" style="margin-top:14px">生成总结</button></div>';
  const b=$('#genNow');
  if(b)b.onclick=function(){summarize(mode)};
}
async function waitForSummary(mode,tries){
  for(let i=0;i<tries;i++){
    try{
      const r=await api('/api/summary?session_id='+encodeURIComponent(S.session)
        +'&scope='+encodeURIComponent(mode));
      if(r.ok&&r.payload){ S.sumCache[mode]=r.payload; renderSummary(r.payload); return true }
    }catch(e){}
    await new Promise(function(res){setTimeout(res,700)});
  }
  summaryEmpty(mode);
  return false;
}
let _sumTimer=null,_sumT0=0;
function sumLoading(text){
  $('#sumBody').innerHTML='<div class="sloading"><i class="spin"></i><span id="slTxt"></span><b id="slSec">0.0s</b></div>';
  $('#slTxt').textContent=text;
  _sumT0=Date.now();
  clearInterval(_sumTimer);
  _sumTimer=setInterval(function(){
    const el=$('#slSec');
    if(!el){clearInterval(_sumTimer);return}
    el.textContent=((Date.now()-_sumT0)/1000).toFixed(1)+'s';
  },100);
}
function sumStop(){clearInterval(_sumTimer);_sumTimer=null}
async function summarize(mode,silent,engineOverride){
  if(S.busy)return;
  S.busy=true;$('#btnSum').disabled=true;
  let f;
  if(mode==='content')      f={categories:[],speakers:[],q:'',min_conf:null};
  else if(mode==='time')    f={t_from:S.tFrom,t_to:S.tTo,categories:[],speakers:[],q:'',min_conf:null};
  else if(mode==='category')f={...activeFilters(),categories:[]};
  else                       f=activeFilters();
  f.session_id=S.session;f.exclude_low_value=S.exLow;f.exclude_overlap=S.exOvl;
  const eng=engineOverride||$('#engine').value;
  let tip='正在归纳…';
  try{
    if(eng==='auto'){
      try{
        const est=await post('/api/summarize/estimate',{session_id:S.session,filters:f,mode:mode});
        tip=est.cached
          ? ('输入没变，复用上次结果（0 token）')
          : est.would_use_llm
          ? ('正在调用 '+est.model+' 生成…（'+est.rows+' 条发言）')
          : ('本地抽取中 · '+est.rows+' 条发言，不调用模型，0 token');
      }catch(e){}
    }else if(eng==='llm'){ tip='正在调用大模型生成…' }
    else { tip='本地抽取中…' }
    sumLoading(tip);
    const d=await post('/api/summarize',{session_id:S.session,mode,filters:f,engine:eng});
    S.sumCache[mode]=d;
    renderSummary(d);
    if(d.cached)toast('输入没变，复用上次总结（0 token）');
    if(d.title_applied){                 // 顺带改的名要立刻反映到顶部和场次列表
      try{
        const r=await api('/api/sessions');S.sessions=r.sessions||[];
        renderSessionMenu();paintSessionBtn();
        toast('已按内容命名为「'+(d.session_title||d.title)+'」');
      }catch(e){}
    }
  }catch(e){ $('#sumBody').innerHTML='<div class="empty">生成失败：'+esc(e.message)+'</div>' }
  finally{ sumStop();S.busy=false;$('#btnSum').disabled=false }
}
/* ---------------- AI 分析：一次点击跑完「总结 + 逐句标注」（服务端任务） ---------------- */
let _runTimer=null,_runBusy=false;
function paintRun(run){
  const el=$('#runBar');if(!el)return;
  S.runActive=!!(run&&run.active);
  if(!run){el.hidden=true;return}
  el.hidden=false;
  const pct=Math.max(0,Math.min(100,Number(run.progress)||0));
  const stages=(run.stages||[]).map(function(s){
    const cls=s.status==='done'?'d':s.status==='failed'?'f':(s.status==='canceled'?'':'x');
    const detail=s.detail?(' · '+esc(s.detail)):'';
    return '<i class="'+cls+'">'+esc(s.label||s.key)+detail+'</i>';
  }).join('');
  el.innerHTML='<div class="rb-h"><b>'+(run.active?'AI 分析中…':(run.status==='done'?'AI 分析完成':(run.status==='partial'?'部分完成':(run.status==='canceled'?'已取消':'分析结束'))))+'</b>'
    +'<span>'+esc(run.message||'')+'</span>'
    +(run.active?'<button class="rb-x" id="rbCancel">取消</button>':'')
    +'</div><div class="rb-track"><i class="rb-fill" style="width:'+pct+'%"></i></div>'
    +(stages?'<div class="rb-stage">'+stages+'</div>':'');
  const c=$('#rbCancel');
  if(c)c.onclick=async function(){
    c.textContent='取消中…';c.style.pointerEvents='none';
    try{await post('/api/analysis/cancel',{session_id:S.session})}catch(e){}
    pollRun();
  };
}
function runStop(){clearTimeout(_runTimer);_runTimer=null}
function pollRun(){
  runStop();
  _runTimer=setTimeout(async function(){
    if(!S.session)return;
    let r;
    try{
      r=await api('/api/analysis/status?session_id='+encodeURIComponent(S.session));
    }catch(e){ pollRun(); return }
    const run=r.run;
    if(!run){paintRun(null);return}
    paintRun(run);
    if(r.active){ pollRun(); return }
    if(!_runBusy&&(run.status==='done'||run.status==='partial')){await onRunDone(run);return}
    setTimeout(function(){ if(!S.runActive)paintRun(null) },8000);
  },900);
}
async function onRunDone(run){
  _runBusy=true;
  try{
    try{
      const m=await api('/api/meta?session_id='+encodeURIComponent(S.session));
      S.meta=m;renderFilters();            // 分类计数 / AI 徽标跟着变
    }catch(e){}
    await refresh();                        // 列表与时间轴的颜色也按新分类重画
    try{
      const r=await api('/api/summary?session_id='+encodeURIComponent(S.session)+'&scope=content');
      S.sumCache={};                       // 标签变了，其余几个 tab 的旧结果一律作废
      if(r.ok&&r.payload){
        S.sumCache['content']=r.payload;
        if(S.mode==='content')renderSummary(r.payload);
      }
      const ss=await api('/api/sessions');S.sessions=ss.sessions||[];
      renderSessionMenu();paintSessionBtn();
    }catch(e){}
    toast(run.status==='partial'?('AI 分析部分完成：'+(run.error||run.message||'')):'AI 分析完成');
  }finally{
    _runBusy=false;
    setTimeout(function(){ if(!S.runActive)paintRun(null) },6000);
  }
}
async function aiAnalyze(){
  if(!S.session)return;
  const b=$('#btnAIAnalyze');
  if(b&&b.disabled)return;
  if(b){b.disabled=true;b.textContent='启动中…'}
  try{
    const r=await post('/api/analysis/start',{session_id:S.session,kind:'full'});
    if(!r||!r.run)throw new Error((r&&r.error)||'没有拿到任务');
    if(r.already)toast('这一场已经在跑了，接着看进度');
    paintRun(r.run);pollRun();
  }catch(e){
    toast('启动失败：'+(e.message||'未知错误'));
  }finally{
    if(b){b.disabled=false;b.textContent='AI 分析'}
  }
}
async function resumeRun(){
  if(!S.session)return;
  try{
    const r=await api('/api/analysis/status?session_id='+encodeURIComponent(S.session));
    const run=r.run;
    S.runActive=!!(run&&run.active);
    if(run&&run.active){paintRun(run);pollRun()}
    else if(run&&(run.status==='partial'||run.status==='failed')){paintRun(run)}
    else paintRun(null);
  }catch(e){}
}
/* ---------------- AI 标题条：总结顺手起的名字，不额外调模型 ---------------- */
function tbarHide(){const el=$('#tbar');if(!el)return;el.hidden=true;el.innerHTML=''}
function paintTitleBar(d){
  const el=$('#tbar');if(!el)return;
  const cur=(S.sessions.find(x=>x.id===S.session)||{}).title||'';
  const main=(d&&d.title)||'';
  if(!main){tbarHide();return}
  // 说"这个名字还不够格"的时候，不摆一条空标题在总结上面；候选在改名框里
  const why0=d.title_reason||'';
  if(!d.title_applied&&/内容太少|内容不足|只能给出人数|局部筛选/.test(why0)){tbarHide();return}
  const alts=((d.title_alts)||[]).filter(t=>t&&t!==main&&t!==cur).slice(0,2);
  const applied=!!d.title_applied||main===cur;
  // 没有可做的事就别占地方：名字已经是当前名、又没有备选，标题条直接收起来
  if(applied&&!alts.length){tbarHide();return}
  const why=why0;
  el.hidden=false;
  // 窄面板：按钮跟标题各占一行，标题最多两行，别让按钮把名字挤成省略号
  el.innerHTML='<div class="tb-h"><span class="tb-k">🏷 AI 标题</span>'
    +'<span class="tb-r">'
      +(applied?'<span class="tb-badge">当前名</span>':'<button class="tb-btn" id="tbAdopt">采用</button>')
      +'<button class="tb-btn ghost" id="tbMore" title="再调一次模型换个名字（会消耗 token）">换一个</button>'
    +'</span></div>'
    +'<b class="tb-t" title="'+esc(main)+'">'+esc(main)+'</b>'
    +(alts.length?'<div class="tb-alts">'
      +alts.map(a=>'<button class="tb-alt" data-t="'+esc(a)+'">'+esc(a)+'</button>').join('')+'</div>':'')
    +(why?'<div class="tb-note">'+esc(why)+'</div>':'');
  const ad=$('#tbAdopt');if(ad)ad.onclick=()=>adoptTitle(main);
  const more=$('#tbMore');if(more)more.onclick=titleFresh;
  $$('#tbar .tb-alt').forEach(b=>b.onclick=()=>adoptTitle(b.dataset.t));
}
async function adoptTitle(t){
  t=(t||'').trim();if(!t||!S.session)return;
  try{
    await post('/api/sessions/title/adopt',{session_id:S.session,title:t});
    const r=await api('/api/sessions');S.sessions=r.sessions||[];
    renderSessionMenu();paintSessionBtn();
    paintTitleBar({title:t,title_alts:[],title_applied:true,
      title_reason:'你采用了这个名字，之后自动总结不会再覆盖'});
    toast('已命名为「'+t+'」');
  }catch(e){toast('采用失败：'+e.message)}
}
async function titleFresh(){
  if(!S.session)return;
  const el=$('#tbar');if(el)el.style.opacity=.55;
  try{
    const sg=await api('/api/sessions/suggest_title?session_id='+encodeURIComponent(S.session)+'&fresh=1');
    const ai=(sg.groups||[]).filter(g=>g.kind==='ai')[0];
    if(!ai||!ai.items||!ai.items.length){toast('模型没给出新名字（没配置或内容太少）');return}
    paintTitleBar({title:ai.items[0],title_alts:ai.items.slice(1,3),title_applied:false,
      title_reason:'新起的名字，点「采用」才生效'});
  }catch(e){toast('换一个失败：'+e.message)}
  finally{if(el)el.style.opacity=1}
}
function renderSummary(d){
  const cov=d.coverage||{},st=d.stats||{};
  const all=[];                       // 扁平化后的要点（分组时也要能按序号回查）
  const mkPoint=(p)=>{
    const i=all.length; all.push(p);
    return '<div class="point">'
      +'<div class="pt-h"><span class="pt-t">'+esc(p.title)+'</span>'
      +'<span class="pt-time">'+fmt(p.t_from)+'–'+fmt(p.t_to)+'</span></div>'
      +'<div class="pt-g">'+esc(p.gist)+'</div>'
      +'<div class="pt-m"><span>'+p.utterance_count+' 条相关发言</span>'
      +'<span>'+esc((p.speakers||[]).slice(0,4).join('、'))+((p.speakers||[]).length>4?' 等':'')+'</span></div>'
      +'<div class="quote">“'+esc((p.evidence[0]||{}).quote||'')+'” —— '
      +esc((p.evidence[0]||{}).speaker_label||'')+' '+esc((p.evidence[0]||{}).timecode||'')+'</div>'
      +'<div class="pt-b"><button data-i="'+i+'" class="evbtn">查看原文 ('+p.evidence.length+')</button>'
      +'<button data-play="'+p.t_from+'" data-end="'+p.t_to+'" class="playbtn">▶ 定位时段</button></div></div>';
  };
  let pointsHtml;
  if(d.groups&&d.groups.length){
    pointsHtml=d.groups.map(function(g){
      const c=CAT_META[g.category]||['未分类','#999'];
      return '<div class="cgrp"><div class="cgrp-h">'
        +'<span class="tag cat" style="background:'+c[1]+'">'+esc(g.label||c[0])+'</span>'
        +'<span class="cgrp-n">'+g.count+' 条发言</span></div>'
        +(g.points.length?g.points.map(mkPoint).join('')
          :'<div class="cgrp-empty">这个类别没有能绑定证据的要点</div>')
        +'</div>';
    }).join('');
  }else{
    pointsHtml=d.points.length?d.points.map(mkPoint).join('')
      :'<div class="empty">没有能绑定证据的要点。<br>请放宽筛选条件，或关闭「剔除低价值」。</div>';
  }
  const stats='<span class="stat">纳入 '+(d.points||[]).reduce(function(a,p){return a+p.utterance_count},0)+' 条</span>'
    +'<span class="stat">说话人 '+(st.speakers||0)+'</span>'
    +'<span class="stat">要点 '+(d.points||[]).length+'</span>'
    +(d.groups&&d.groups.length?('<span class="stat">类别 '+d.groups.length+'</span>'):'');
  const isLLM=d.engine_kind==='llm';
  const u=d.usage||{};
  const eMeta=d.cached
    ? ' · 缓存命中 · 0 token'
    : (isLLM?(' · '+esc(d.engine_model||'')+' · '+((u.ms||0)/1000).toFixed(1)+'s'
            +(u.total_tokens?(' · '+u.total_tokens+' tokens'):' · token 用量未知'))
           :' · 0 token');
  const ebar='<div class="ebar '+(isLLM?'llm':'local')+'">'
    +'<span class="edot"></span><b>'+(isLLM?'大模型总结':'本地抽取式')+'</b>'
    +eMeta
    +(d.engine_reason?'<span class="ereason">· '+esc(d.engine_reason)+'</span>':'')
    +((!isLLM&&d.engine_reason&&d.engine_reason.indexOf('内容太少')>=0)
        ?'<button class="linkbtn" id="forceLLM">仍用大模型</button>':'')
    +'</div>';
  const covLine='<span class="stat">匹配 '+(cov.matched||0)+' / 采用 '+(cov.included||0)+'</span>'
    +'<span class="stat">剔除低价值 '+(cov.excluded_low_value_total||0)+'</span>'
    +'<span class="stat">重叠未转写 '+Math.round(cov.excluded_overlap_s||0)+'s</span>'
    +'<span class="stat">覆盖率 '+Math.round((cov.coverage_ratio||0)*100)+'%</span>';
  $('#sumBody').innerHTML=ebar+'<div class="ov">'+esc(d.overview)+'</div>'
    +'<div class="stats">'+stats+'</div><div class="stats" style="padding-bottom:6px">'+covLine+'</div>'
    +((d.caveats||[]).length?('<div class="caveats">'+d.caveats.map(function(c){return '<div>· '+esc(c)+'</div>'}).join('')+'</div>'):'')
    +pointsHtml;
  $$('#sumBody .evbtn').forEach(b=>b.onclick=()=>showEvidence(all[Number(b.dataset.i)].evidence));
  $$('#sumBody .playbtn').forEach(b=>b.onclick=()=>setPlayhead(Number(b.dataset.play),null,true,Number(b.dataset.end)));
  const fb=$('#forceLLM');
  if(fb)fb.onclick=()=>summarize(S.mode,false,'llm');
  // 标题只跟「按内容（整场）」有关：别的维度说了算的是筛选，不是这一场叫什么
  if((d.scope||'content')==='content')paintTitleBar(d);
}

export { SUM_TAB_NAME, _runTimer, _sumTimer, adoptTitle, aiAnalyze, onRunDone, paintRun, paintTitleBar, pollRun, renderSummary, resumeRun, runStop, showSummary, sumLoading, sumStop, summarize, summaryEmpty, tbarHide, titleFresh, waitForSummary };
