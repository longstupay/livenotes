// 由 app/web/rooms.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。
  "use strict";

  /* ================================================================ data */
  const STAGES = [
    {key:"link", label:"链接校验", note:"四道闸：形式 / 落地 / 形状 / 域名"},
    {key:"browser", label:"启动采集浏览器", note:"独立 profile + 调试端口"},
    {key:"page", label:"打开直播间", note:"页面加载、钉标题、确认开播"},
    {key:"audio", label:"取到音频", note:"零点击自动选源，拿到音轨"},
    {key:"session", label:"建立会话", note:"本地服务返回 session_id"},
    {key:"ingest", label:"开始入库", note:"音频块持续送达，字幕开始出字"}
  ];

  /* ================================================================ state */
  const state = {
    online:false,
    rooms:[],
    view:"wall",
    filter:"all",
    query:"",
    detailId:null,
    pendingDetail:null,
    diagOpen:false,
    history:{},          // session_id -> 历史字幕/时长，停止后仍能在详情里看到
    ackKey:"",
    lastFocus:null,
    toastTimer:0,
    realTimer:0,
    detailTimer:0,
    reduceMotion:window.matchMedia("(prefers-reduced-motion: reduce)").matches
  };
  const $ = (s,r)=> (r||document).querySelector(s);
  const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
  const esc = s => String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const pad = n => String(n).padStart(2,"0");
  const fmt = sec => {
    sec = Math.max(0, Math.round(sec||0));
    const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
    return h ? h+":"+pad(m)+":"+pad(s) : pad(m)+":"+pad(s);
  };
  const clamp = (n,a,b)=>Math.max(a,Math.min(b,n));
  const num = n => (n==null||isNaN(n)) ? 0 : Number(n);
  const timeOf = iso => {
    if(!iso) return "";
    const m = String(iso).match(/T(\d{2}):(\d{2})/);
    return m ? m[1]+":"+m[2] : "";
  };
  function syncScrollLock(){
    const lock = !!state.detailId || state.diagOpen || !$("#modalOverlay").hidden;
    document.body.classList.toggle("no-scroll", lock);
  }

  /* ================================================================ derived */

export { $, $$, STAGES, clamp, esc, fmt, num, pad, state, syncScrollLock, timeOf };
