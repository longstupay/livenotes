// 由 app/web/rooms.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { closeModal, toast } from './actions.js';
import { drawAll, startLoop, stopLoop } from './canvas.js';
import { $, state } from './core.js';
import { fetchReal, postJson } from './data.js';
import { closeDetail, closeDiag, openDetail, openDiag, switchDetail } from './detail.js';
import { render } from './render.js';
import { loadUrl, setView } from './view.js';
  function topOverlay(){
    if(!$("#modalOverlay").hidden) return "modal";
    if(!$("#diagOverlay").hidden) return "diag";
    if(!$("#detailOverlay").hidden) return "detail";
    return "";
  }
  document.addEventListener("keydown", e=>{
    if(e.key==="Escape"){
      const top = topOverlay();
      if(top==="modal"){ closeModal(); e.preventDefault(); return; }
      if(top==="diag"){ closeDiag(); e.preventDefault(); return; }
      if(top==="detail"){ closeDetail(); e.preventDefault(); return; }
    }
    if((e.key==="ArrowLeft"||e.key==="ArrowRight") && state.detailId && topOverlay()==="detail"){
      e.preventDefault(); switchDetail(e.key==="ArrowRight"?"next":"prev"); return;
    }
    if(e.key==="/" && !topOverlay() && document.activeElement!==$("#search")){
      e.preventDefault(); $("#search").focus(); return;
    }
  });

  /* ================================================================ init */
  $("#viewSeg").addEventListener("click", e=>{
    const b = e.target.closest("[data-view]");
    if(b) setView(b.dataset.view);
  });
  $("#search").addEventListener("input", e=>{
    state.query = e.target.value;
    render();
  });
  $("#diagBtn").onclick = openDiag;
  $("#addBtn").onclick = async ()=>{
    const url = ($("#urlInput").value||"").trim();
    const btn = $("#addBtn");
    if(!url || btn.disabled) return;
    btn.disabled = true; btn.textContent = "添加中…";
    try{
      const r = await postJson("/api/rooms",{url});
      $("#urlInput").value = "";
      // 重复粘贴一个已停止/已中断的房间不会自动重开，这里把下一步说清楚。
      const dup = r.room || {};
      toast(r.created===false
        ? (dup.state==="stopped"
            ? (dup.interrupted ? "这个链接已经在列表里，上次因服务重启中断；点「重试」继续。"
                               : "这个链接已经在列表里，已停止；点「重试」继续。")
            : "这个链接已经在列表里了。")
        : "已加入收听，正在启动采集。");
      await fetchReal(false);
    }catch(e){
      toast("加不进去："+e.message);
    }finally{
      btn.disabled = false; btn.textContent = "开始听";
    }
  };
  $("#urlInput").addEventListener("keydown", e=>{
    if(e.key==="Enter"){ e.preventDefault(); $("#addBtn").click(); }
  });
  window.addEventListener("resize", ()=>{ if(state.reduceMotion) drawAll(performance.now()); });
  document.addEventListener("visibilitychange", ()=>{
    if(document.hidden) stopLoop(); else startLoop();
  });

  loadUrl();
  startLoop();

  window.__wall = {state, openDetail, openDiag, render};


export { topOverlay };
