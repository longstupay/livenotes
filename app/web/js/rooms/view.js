// 由 app/web/rooms.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $$, state } from './core.js';
import { startRealPoll } from './data.js';
import { openDiag } from './detail.js';
import { render } from './render.js';
  function setView(v){
    state.view = v;
    $$("#viewSeg button").forEach(b=>b.classList.toggle("on", b.dataset.view===v));
    render();
    syncUrl();
  }

  /* ================================================================ url */
  function syncUrl(){
    const q = new URLSearchParams();
    if(state.detailId) q.set("view","detail"), q.set("room",state.detailId);
    else if(state.diagOpen) q.set("view","diag");
    if(state.view!=="wall") q.set("list","1");
    const url = location.pathname + (q.toString()?"?"+q.toString():"");
    history.replaceState(null,"",url);
  }
  function loadUrl(){
    const q = new URLSearchParams(location.search);
    if(q.get("list")==="1") state.view = "list";
    $$("#viewSeg button").forEach(b=>b.classList.toggle("on", b.dataset.view===state.view));
    state.rooms = [];
    state.online = false;
    render();
    startRealPoll();
    const view = q.get("view");
    if(view==="detail" && q.get("room")) state.pendingDetail = q.get("room");
    if(view==="diag") openDiag();
  }

  /* ================================================================ keyboard */

export { loadUrl, setView, syncUrl };
