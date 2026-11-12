// 由 app/web/rooms.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, num, state } from './core.js';
import { levelOf, statusMeta } from './derived.js';
import { roomById } from './render.js';
  const canvases = new Map();
  function sizeCanvas(c){
    const dpr = Math.min(2, window.devicePixelRatio||1);
    const rect = c.getBoundingClientRect();
    if(!rect.width || !rect.height) return null;
    const w = Math.round(rect.width*dpr), h = Math.round(rect.height*dpr);
    if(c.width!==w || c.height!==h){ c.width=w; c.height=h; }
    const ctx = c.getContext("2d");
    ctx.setTransform(dpr,0,0,dpr,0,0);
    return {ctx,w:rect.width,h:rect.height};
  }
  function signalColors(){
    const cs = getComputedStyle(document.documentElement);
    return {
      bg:cs.getPropertyValue("--signal-bg").trim() || "#081116",
      grid:cs.getPropertyValue("--signal-grid").trim() || "rgba(240,234,223,.055)",
      line:cs.getPropertyValue("--signal-line").trim() || "rgba(240,234,223,.14)",
      jade:cs.getPropertyValue("--signal-jade").trim() || "#8fd8b6",
      amber:cs.getPropertyValue("--signal-amber").trim() || "#f0bf69",
      red:cs.getPropertyValue("--signal-red").trim() || "#ef7b6d",
      dim:cs.getPropertyValue("--signal-dim").trim() || "#6e817d",
      ink:cs.getPropertyValue("--signal-ink").trim() || "#f0eadf"
    };
  }
  function drawGrid(ctx,w,h,col){
    ctx.strokeStyle = col.grid; ctx.lineWidth = 1;
    for(let x=0;x<w;x+=28){ ctx.beginPath(); ctx.moveTo(x+.5,0); ctx.lineTo(x+.5,h); ctx.stroke(); }
    for(let y=0;y<h;y+=22){ ctx.beginPath(); ctx.moveTo(0,y+.5); ctx.lineTo(w,y+.5); ctx.stroke(); }
    ctx.strokeStyle = col.line;
    ctx.beginPath(); ctx.moveTo(0,h/2+.5); ctx.lineTo(w,h/2+.5); ctx.stroke();
  }
  function drawWave(c,r,t){
    const s = sizeCanvas(c);
    if(!s) return;
    const {ctx,w,h} = s;
    const col = signalColors();
    ctx.clearRect(0,0,w,h);
    ctx.fillStyle = col.bg; ctx.fillRect(0,0,w,h);
    drawGrid(ctx,w,h,col);

    const st = statusMeta(r);
    const mid = h/2;
    const amp = h*0.30;

    if(r.state==="failed" || (r.state==="live" && (!r.session || r.session.running===false))){
      ctx.strokeStyle = col.red; ctx.globalAlpha=.72; ctx.lineWidth=1.3;
      ctx.beginPath(); ctx.moveTo(0,mid+.5); ctx.lineTo(w,mid+.5); ctx.stroke();
      ctx.globalAlpha=1;
      ctx.fillStyle = col.red;
      for(let i=0;i<3;i++){
        const x = w*(.28+i*.18);
        ctx.fillRect(x, mid-7, 1, 14);
      }
      return;
    }
    if(r.state==="pending" || r.state==="starting"){
      ctx.strokeStyle = col.dim; ctx.globalAlpha=.55; ctx.lineWidth=1;
      ctx.beginPath(); ctx.moveTo(0,mid+.5); ctx.lineTo(w,mid+.5); ctx.stroke();
      ctx.globalAlpha=1;
      if(!state.reduceMotion){
        const x = (t*0.055) % w;
        const g = ctx.createLinearGradient(x-18,0,x+18,0);
        g.addColorStop(0,"rgba(240,191,105,0)"); g.addColorStop(.5,"rgba(240,191,105,.20)");
        g.addColorStop(1,"rgba(240,191,105,0)");
        ctx.fillStyle = g; ctx.fillRect(x-18,0,36,h);
        ctx.strokeStyle = "rgba(240,191,105,.55)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x+.5,0); ctx.lineTo(x+.5,h); ctx.stroke();
      }
      ctx.strokeStyle = col.amber; ctx.globalAlpha=.7; ctx.lineWidth=1.2;
      ctx.beginPath(); ctx.moveTo(w*.5,mid-4); ctx.lineTo(w*.5,mid+4); ctx.stroke();
      ctx.globalAlpha=1;
      return;
    }
    if(r.state==="stopped" || !r.session){
      ctx.strokeStyle = col.dim; ctx.globalAlpha=.7; ctx.lineWidth=1.2;
      ctx.beginPath(); ctx.moveTo(0,mid+.5); ctx.lineTo(w,mid+.5); ctx.stroke();
      ctx.globalAlpha=1;
      return;
    }

    const lv = levelOf(r);
    const quiet = num(r.session.idle_s) >= 15 || r.session.paused;
    ctx.strokeStyle = quiet ? col.amber : col.jade;
    ctx.globalAlpha = .92;
    ctx.lineWidth = 1.25;
    ctx.beginPath();
    const step = 2;
    for(let x=0;x<=w;x+=step){
      const p = x/w;
      const env = Math.pow(Math.sin(Math.PI*p), .6);
      // 气口与音节：慢速包络，让波形像说话，而不是一条正弦
      const breath = .58 + .42*Math.sin(p*6.3 + t*.0011 + r.order*1.3);
      const syllable = .72 + .28*Math.sin(x*.045 - t*.004 + r.order);
      // 高频抖动按像素计频，卡片和放大层的手感一致
      const n = Math.sin(x*.16 + t*.0019 + r.order*1.7)*.32
              + Math.sin(x*.47 - t*.0031 + r.order*.9)*.22
              + Math.sin(x*.93 + t*.0047)*.12
              + Math.sin(p*9 + t*.0013)*.30;
      const y = mid + n*amp*Math.max(.08,lv)*env*breath*syllable;
      if(x===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    }
    ctx.stroke();
    ctx.globalAlpha=1;
  }
  function drawDetailWave(r){
    const c = $("[data-detail-wave]");
    if(!c) return;
    drawWave(c,r,performance.now());
  }
  function drawAll(t){
    if(state.view==="wall"){
      $$(".card .wave").forEach(c=>{
        const r = roomById(c.dataset.id);
        if(r) drawWave(c,r,t);
      });
    }
    if(state.detailId){
      const c = $("[data-detail-wave]");
      const r = roomById(state.detailId);
      if(c && r) drawWave(c,r,t);
    }
  }
  let raf = 0;
  function loop(t){
    drawAll(t);
    raf = requestAnimationFrame(loop);
  }
  function startLoop(){
    if(state.reduceMotion){ drawAll(performance.now()); return; }
    if(!raf) raf = requestAnimationFrame(loop);
  }
  function stopLoop(){ if(raf){ cancelAnimationFrame(raf); raf=0; } }

  /* ================================================================ real data */

export { canvases, drawAll, drawDetailWave, drawGrid, drawWave, loop, raf, signalColors, sizeCanvas, startLoop, stopLoop };
