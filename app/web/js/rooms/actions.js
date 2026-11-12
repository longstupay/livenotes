// 由 app/web/rooms.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, esc, state, syncScrollLock } from './core.js';
import { fetchReal, postJson } from './data.js';
import { isListening, switchEngineLabel } from './derived.js';
import { closeDetail, openDetail, openDiag } from './detail.js';
import { roomById } from './render.js';
  function toast(msg){
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(()=>{ t.hidden=true; }, 2600);
  }
  function handleAction(k,id,el){
    const r = roomById(id);
    if(!r) return;
    if(k==="detail"){ openDetail(id); return; }
    if(k==="diag"){ openDiag(); return; }
    if(k==="listen"){
      if(!r.sessionId){ toast("这一路还没有可听的音频。"); return; }
      if(!window.LiveAudio){ toast("播放器还没加载好，刷新页面再试。"); return; }
      window.LiveAudio.open(r.sessionId,{label:"CH"+r.ch+" "+(r.name||"直播间")});
      return;
    }
    if(k==="live"){
      if(r.sessionId){ location.href="/live?session="+encodeURIComponent(r.sessionId); return; }
      toast("这一路还没有会话，等开始出字后再打开实时字幕。");
      return;
    }
    if(k==="reader"){
      if(r.sessionId){ location.href="/reader?session="+encodeURIComponent(r.sessionId); return; }
      toast("这一场没有可回放的录音。");
      return;
    }
    if(k==="open"){ return; }
    if(k==="retry"){ retryRoom(r); return; }
    if(k==="switch"){ openSwitchConfirm(r); return; }
    if(k==="stop"){ openStopConfirm(r); return; }
    if(k==="cancel"){ cancelRoom(r); return; }
    if(k==="del"){ openDeleteConfirm(r); return; }
    if(k==="rename"){ openRename(r); return; }
  }
  async function retryRoom(r){
    if(isListening(r)) return;
    try{
      // 重试 = 重新拉起这一路：先回到 pending，让采集代理重新开浏览器、重新取流。
      await postJson("/api/rooms/state",{id:r.id,state:"pending",error:""});
      state.ackKey = "";
      toast("已重新排队，采集代理会重新拉起这一路（不是断点续传）。");
      await fetchReal(false);
    }catch(e){ toast("重试失败："+e.message); }
  }
  async function switchRoom(r){
    try{
      // 直取失败后不自动切浏览器：这里就是用户点「确认」之后唯一发起的请求。
      await postJson("/api/rooms/switch",{id:r.id});
      state.ackKey = "";
      toast("已确认切换，采集代理会在几秒内改用"+switchEngineLabel(r.switchEngine)+"采集。");
      await fetchReal(false);
    }catch(e){ toast("切换失败："+e.message); }
  }
  async function cancelRoom(r){
    try{
      await postJson("/api/rooms/close",{id:r.id});
      state.ackKey = "";
      toast("已取消这一路。");
      await fetchReal(false);
    }catch(e){ toast("取消失败："+e.message); }
  }
  async function stopRoom(r){
    try{
      await postJson("/api/rooms/close",{id:r.id});
      state.ackKey = "";
      toast("已停止，录音会正常收尾保存。");
      await fetchReal(false);
    }catch(e){ toast("停止失败："+e.message); }
  }
  async function deleteRoom(r){
    try{
      await postJson("/api/rooms/remove",{id:r.id});
      state.ackKey = "";
      if(state.detailId===r.id) closeDetail();
      toast("已从墙上移除，已录内容会保留。");
      await fetchReal(false);
    }catch(e){ toast("删除失败："+e.message); }
  }
  async function renameRoom(r, name){
    try{
      // 改名是"用户说了算"：服务端会把它标成 name_source=user，采集端
      // 后来从页面读到的名字不会再盖回来。
      const j = await postJson("/api/rooms/rename",{id:r.id,name:name});
      state.ackKey = "";
      toast("已改名为「"+((j.room&&j.room.name)||name)+"」，采集端不会再覆盖。");
      await fetchReal(false);
    }catch(e){ toast("改名失败："+e.message); }
  }

  /* ================================================================ modals */
  function openModal(html){
    const o = $("#modalOverlay");
    o.innerHTML = '<div class="sheet small" role="dialog" aria-modal="true">'+html+'</div>';
    o.hidden = false;
    document.body.classList.add("no-scroll");
    o.onclick = e=>{ if(e.target===o) closeModal(); };
    $$("[data-mclose]", o).forEach(b=>b.onclick=closeModal);
    const f = $("[data-mfocus]", o);
    if(f) f.focus();
  }
  function closeModal(){
    const o = $("#modalOverlay");
    o.hidden = true; o.innerHTML = "";
    syncScrollLock();
  }
  function openSwitchConfirm(r){
    if(!r.switchReady) return;
    const label = switchEngineLabel(r.switchEngine);
    openModal('<div class="sheet-body"><h3 class="modal-title">切换到'+esc(label)+'采集？</h3>'
      + '<p class="modal-text"><b>直取已经失败：</b>'+esc(r.switchReason||r.error||"原因未知")+'</p>'
      + '<p class="modal-text">直取失败后不会自动切换。确认后采集代理会打开'
      + (r.switchEngine==="shared" ? "共享浏览器标签页" : "一个独立的采集浏览器")
      + '继续录这一路。'+(r.sessionId?'已经录下的部分会保留。':'')+'</p>'
      + '<label class="modal-check"><input type="checkbox" id="switchRemember">'
      + '<span>以后直取失败也自动切到浏览器（可在设置里改回来）</span></label>'
      + '<div class="modal-actions"><button class="btn" data-mclose>先不切</button>'
      + '<button class="btn primary" data-mfocus data-confirm="switch">切换到'+esc(label)+'采集</button></div></div>');
    $("[data-confirm='switch']").onclick = async ()=>{
      const remember = $("#switchRemember") && $("#switchRemember").checked;
      if(remember){
        try{ await postJson("/api/settings/prefs",{direct_fallback:"auto"}); }
        catch(e){ toast("自动切换设置没保存上："+e.message); }
      }
      closeModal(); switchRoom(r);
    };
  }
  function openStopConfirm(r){
    openModal('<div class="sheet-body"><h3 class="modal-title">停止「'+esc(r.name)+'」？</h3>'
      + '<p class="modal-text">会停止采集并关闭这一路的<b>专属采集浏览器</b>。已经录下的音频会正常收尾保存，回放不受影响。</p>'
      + '<div class="modal-actions"><button class="btn" data-mclose>取消</button>'
      + '<button class="btn danger" data-mfocus data-confirm="stop">停止这一路</button></div></div>');
    $("[data-confirm='stop']").onclick = ()=>{ closeModal(); stopRoom(r); };
  }
  function openRename(r){
    // 名字只用来认人，不带任何状态：改坏了再改一次就行，所以不做二次确认。
    const cur = String(r.name||"");
    openModal('<div class="sheet-body"><h3 class="modal-title">给这一路起个名字</h3>'
      + '<p class="modal-text">默认是采集端读到的「主播名 · 房间标题」。改成你自己认得出的'
      + '说法（比如「老张的第二场」）也有效，采集端不会再把名字盖回去。</p>'
      + '<input type="text" id="renameInput" maxlength="60" value="'+esc(cur)+'" '
      + 'placeholder="例如：示例主播 · 今晚聊 FDE">'
      + '<div class="modal-actions"><button class="btn" data-mclose>取消</button>'
      + '<button class="btn primary" data-mfocus data-confirm="rename">保存</button></div></div>');
    const input = $("#renameInput");
    const submit = ()=>{
      const v = String(input && input.value || "").trim();
      if(!v){ toast("名字不能是空的。"); if(input) input.focus(); return; }
      closeModal(); renameRoom(r, v);
    };
    $("[data-confirm='rename']").onclick = submit;
    if(input){
      input.onkeydown = e=>{ if(e.key==="Enter"){ e.preventDefault(); submit(); } };
      input.select();
    }
  }
  function openDeleteConfirm(r){
    openModal('<div class="sheet-body"><h3 class="modal-title">删除「'+esc(r.name)+'」？</h3>'
      + '<p class="modal-text">会先停止这一路采集，再从收听列表移除；<b>已经录下的内容会保留</b>，之后还能在回放里找到。</p>'
      + '<div class="modal-actions"><button class="btn" data-mclose>取消</button>'
      + '<button class="btn danger" data-mfocus data-confirm="del">删除房间</button></div></div>');
    $("[data-confirm='del']").onclick = ()=>{ closeModal(); deleteRoom(r); };
  }

  /* ================================================================ canvas */

export { cancelRoom, closeModal, deleteRoom, handleAction, openDeleteConfirm, openModal, openRename, openStopConfirm, openSwitchConfirm, renameRoom, retryRoom, stopRoom, switchRoom, toast };
