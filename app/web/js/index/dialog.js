// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, S, api, esc, fmt, post, toast } from './core.js';
import { L, liveWs } from './live.js';
import { paintSubs } from './subs.js';
let dlgResolve=null;
function dlgOpen(){return $('#dlgMask').classList.contains('show')}
function askText({title,desc,value='',placeholder='',suggest=[],groups=[]}){
  return new Promise(res=>{
    dlgResolve=res;
    $('#dlgTitle').textContent=title||'';
    $('#dlgDesc').textContent=desc||'';
    $('#dlgInput').value=value;
    $('#dlgInput').placeholder=placeholder;
    let html='';
    if(groups&&groups.length){
      // 候选按来源分组：模型起的 / 原话摘录 / 结构信息，用户一眼能看出哪个是"名字"
      html=groups.map(g=>'<div class="dlg-g"><span class="dlg-gk">'+esc(g.label||'')+'</span>'
        +g.items.map(s=>`<button data-v="${esc(s)}">${esc(s)}</button>`).join('')+'</div>').join('');
    }else{
      html=(suggest||[]).map(s=>`<button data-v="${esc(s)}">${esc(s)}</button>`).join('');
    }
    $('#dlgSug').innerHTML=html;
    $$('#dlgSug button').forEach(b=>b.onclick=()=>{
      $('#dlgInput').value=b.dataset.v;$('#dlgInput').focus();
    });
    $('#dlgMask').classList.add('show');
    setTimeout(()=>{$('#dlgInput').focus();$('#dlgInput').select()},30);
  });
}
function closeDlg(v){
  $('#dlgMask').classList.remove('show');
  const r=dlgResolve;dlgResolve=null;
  if(r)r(v);
}
async function wsRename(spk,label){
  const name=await askText({
    title:`把「${label}」改成真实身份`,
    desc:'只影响本场次的显示名，之后所有字幕、筛选和总结都用这个名字。',
    value:'',
    placeholder:'例如：主播小A / 嘉宾林总',
    suggest:['主播','嘉宾1','嘉宾2','运营']
  });
  if(!name)return;
  const nm=name.slice(0,40);
  try{
    await post('/api/speakers/rename',{session_id:L.sid,speaker_id:spk,name:nm});
    L.hist.forEach(function(u){if(u.speaker_id===spk)u.speaker_label=nm});
    paintSubs(L.partial);
    toast('已改名为「'+nm+'」');
  }catch(e){toast('改名失败：'+e.message)}
}
async function wsRejectHint(spk){
  if(!L.sid)return;
  try{
    const r=await post('/api/live/reject_hint',{session_id:L.sid,speaker_id:spk});
    const label=(r&&r.label)||'说话人';
    L.hist.forEach(function(u){if(u.speaker_id===spk)u.speaker_label=label});
    paintSubs(L.partial);
    toast('已清掉这次判断，没有写入底库');
  }catch(e){toast('操作失败：'+e.message)}
}
function wsFont(){
  L.font=L.font>=17?13:L.font+2;
  document.documentElement.style.setProperty('--lpfs',L.font+'px');
  toast('字幕字号 '+L.font+'px');
}
function wsFull(){
  const el=$('#liveWs');
  if(document.fullscreenElement){document.exitFullscreen().catch(()=>{})}
  else if(el.requestFullscreen){el.requestFullscreen().catch(e=>toast('无法全屏：'+e.message))}
  else toast('当前浏览器不支持全屏');
}
/* 只看画面：视频铺满整个工作台，字幕压成底部浮层（直播复盘最常用的姿势）。
   进入时顺手把整个工作台全屏，退出时一起还原。 */
function paintCineSub(){
  const el=$('#liveWs');
  const off=el.classList.contains('nosubs');
  const b=$('#wsCineSub');
  b.classList.toggle('on',!off);
  b.textContent=off?'字幕：关':'字幕：开';
  b.title=off?'当前隐藏字幕，点一下显示（S）':'当前显示字幕，点一下隐藏（S）';
}
function toggleCineSub(){
  const el=$('#liveWs');
  el.classList.toggle('nosubs');
  try{localStorage.setItem('ws.subs', el.classList.contains('nosubs')?'0':'1')}catch(e){}
  paintCineSub();
}
function wsCinema(v){
  const el=$('#liveWs');
  const on=(typeof v==='boolean')?v:!el.classList.contains('cinema');
  el.classList.toggle('cinema',on);
  L.cinema=on;
  if(on){
    if(el.requestFullscreen&&!document.fullscreenElement){
      el.requestFullscreen().catch(function(){});
    }
    toast('画面模式：V 或 Esc 退出');
  }else{
    if(document.fullscreenElement){document.exitFullscreen().catch(function(){})}
    toast('已退出画面模式');
  }
}
function expDownload(fmt){
  if(!S.session)return;
  const a=document.createElement('a');
  a.href='/api/export?session_id='+encodeURIComponent(S.session)+'&format='+fmt;
  a.download=S.session+(fmt==='srt'?'.srt':'.md');
  document.body.appendChild(a);a.click();a.remove();
  toast(fmt==='srt'?'正在导出字幕 SRT':'正在导出纪要 Markdown');
}
/* ---------------- 设置：大模型（多档案） ---------------- */

export { askText, closeDlg, dlgOpen, dlgResolve, expDownload, paintCineSub, toggleCineSub, wsCinema, wsFont, wsFull, wsRejectHint, wsRename };
