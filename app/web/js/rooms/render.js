// 由 app/web/rooms.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { handleAction } from './actions.js';
import { $, $$, esc, fmt, num, state, timeOf } from './core.js';
import { fetchReal } from './data.js';
import { alertKey, diagRooms, engineLabel, ingestMeta, isListening, lastLine, lineSpeaker, stageLabel, statusMeta, switchEngineLabel } from './derived.js';
import { openDetail, openDiag, renderDetail, renderDiag } from './detail.js';
  function renderTop(){
    const live = state.rooms.filter(r=>isListening(r)).length;
    const bad = state.rooms.filter(r=>statusMeta(r).tone==="bad").length;
    const warn = state.rooms.filter(r=>statusMeta(r).tone==="warn" && r.state!=="pending" && r.state!=="starting").length;
    const starting = state.rooms.filter(r=>r.state==="pending"||r.state==="starting").length;
    const diag = diagRooms().length;
    const parts = [];
    if(state.rooms.length){
      parts.push("<b>"+live+"</b> 路在听");
      if(bad) parts.push("<b>"+bad+"</b> 路异常");
      if(starting) parts.push("<b>"+starting+"</b> 路启动中");
      if(warn) parts.push("<b>"+warn+"</b> 路需留意");
      parts.push("共 <b>"+state.rooms.length+"</b> 路");
    } else {
      parts.push("还没有房间");
    }
    $("#topSum").innerHTML = parts.join(" · ");

    const conn = $("#conn");
    if(!state.online){
      conn.className = "conn off";
      conn.innerHTML = "<i></i><span>本地服务未连接</span>";
    } else {
      conn.className = "conn";
      conn.innerHTML = "<i></i><span>本地服务已连接</span>";
    }

    const dc = $("#diagCount");
    if(diag){ dc.hidden=false; dc.textContent=diag; }
    else { dc.hidden=true; }
    $("#diagBtn").classList.toggle("primary", diag>0);
  }

  /* ================================================================ render: notices */
  function renderNotices(){
    const box = $("#notices");
    const items = [];
    if(!state.online){
      items.push('<div class="notice bad"><span class="mark">!</span><div class="txt">'
        + '<b>本地服务未连接。</b>页面显示的是最后一次读到的房间状态，可能已经过期。'
        + '</div><div class="acts"><button data-notice="reconnect">重试连接</button></div></div>');
    }
    const key = alertKey();
    const bad = state.rooms.filter(r=>statusMeta(r).tone==="bad");
    const needSwitch = state.rooms.filter(r=>r.state==="failed" && r.switchReady);
    const plainBad = bad.filter(r=>!(r.state==="failed" && r.switchReady));
    // 服务重启收口的房间单独提示：它们看起来像「已停止」，但没人主动停过。
    const interrupted = state.rooms.filter(r=>r.state==="stopped" && r.interrupted);
    const warn = state.rooms.filter(r=>statusMeta(r).tone==="warn" && r.state!=="pending" && r.state!=="starting"
      && !(r.state==="stopped" && r.interrupted));
    const starting = state.rooms.filter(r=>r.state==="pending" || r.state==="starting");
    if(state.online && (bad.length || interrupted.length || warn.length || starting.length) && state.ackKey!==key){
      const bits = [];
      if(needSwitch.length) bits.push("<b>"+needSwitch.length+" 路直取失败待切换</b>");
      if(plainBad.length) bits.push("<b>"+plainBad.length+" 路异常</b>");
      if(interrupted.length) bits.push("<b>"+interrupted.length+" 路因服务重启中断</b>");
      if(starting.length) bits.push("<b>"+starting.length+" 路启动中</b>");
      if(warn.length) bits.push("<b>"+warn.length+" 路需留意</b>");
      const tone = bad.length ? "bad" : "warn";
      const hasDiag = bad.length || starting.length;
      const view = hasDiag ? "diag" : (interrupted.length && !needSwitch.length && !warn.length ? "interrupted" : "filter");
      const advice = needSwitch.length
        ? "直取失败不会自动切浏览器；点「查看」确认切换后才能继续。"
        : bad.length ? "先看失败的几路，再处理没入库的。"
        : interrupted.length ? "不会自动重开；要接着采点「重试」，录音是否保存看卡片。"
        : starting.length ? "采集代理正在拉起这几路，通常会停在某一步，详情里能看到进度。"
        : "安静只是没人说话；断流看的是最近一块音频的时间差。";
      items.push('<div class="notice '+tone+'"><span class="mark">!</span><div class="txt">'
        + bits.join(" · ") + '。' + advice
        + '</div><div class="acts"><button data-notice="'+view+'">查看</button>'
        + (needSwitch.length?'<a href="/?settings=1">去设置</a>':'')
        + '<button data-notice="ack">忽略</button></div></div>');
    }
    box.innerHTML = items.join("");
    $$("[data-notice]", box).forEach(b=>{
      b.onclick = () => {
        const k = b.dataset.notice;
        if(k==="diag") openDiag();
        if(k==="filter"){ state.filter="alert"; render(); }
        if(k==="interrupted"){ state.filter="stopped"; render(); }
        if(k==="ack"){ state.ackKey = key; renderNotices(); }
        if(k==="reconnect"){ fetchReal(true); }
      };
    });
  }

  /* ================================================================ render: filters */
  function filterCounts(){
    const all = state.rooms.length;
    const live = state.rooms.filter(r=>isListening(r)).length;
    const alert = state.rooms.filter(r=>{
      const s = statusMeta(r);
      return s.tone==="bad" || (s.tone==="warn" && r.state!=="pending" && r.state!=="starting");
    }).length;
    const starting = state.rooms.filter(r=>r.state==="pending"||r.state==="starting").length;
    const stopped = state.rooms.filter(r=>r.state==="stopped").length;
    return {all,live,alert,starting,stopped};
  }
  function renderFilters(){
    const c = filterCounts();
    const defs = [
      {k:"all",label:"全部",n:c.all},
      {k:"live",label:"在听",n:c.live},
      {k:"alert",label:"异常",n:c.alert},
      {k:"starting",label:"启动中",n:c.starting},
      {k:"stopped",label:"已停止",n:c.stopped}
    ];
    $("#filters").innerHTML = defs.map(d=>
      '<button class="chip '+(state.filter===d.k?"on":"")+'" data-filter="'+d.k+'">'
      + '<span class="dot"></span>'+d.label+' <b>'+d.n+'</b></button>'
    ).join("");
    $$("#filters .chip").forEach(b=>b.onclick=()=>{ state.filter=b.dataset.filter; render(); });
  }
  function visibleRooms(){
    let rows = state.rooms.slice().sort((a,b)=>a.order-b.order);
    if(state.filter==="live") rows = rows.filter(r=>isListening(r));
    if(state.filter==="alert") rows = rows.filter(r=>{
      const s = statusMeta(r);
      return s.tone==="bad" || (s.tone==="warn" && r.state!=="pending" && r.state!=="starting");
    });
    if(state.filter==="starting") rows = rows.filter(r=>r.state==="pending"||r.state==="starting");
    if(state.filter==="stopped") rows = rows.filter(r=>r.state==="stopped");
    const q = state.query.trim().toLowerCase();
    if(q) rows = rows.filter(r=>(r.name||"").toLowerCase().includes(q) || (r.url||"").toLowerCase().includes(q));
    return rows;
  }

  /* ================================================================ render: wall */
  function cardHtml(r){
    const st = statusMeta(r);
    const ing = ingestMeta(r);
    const line = lastLine(r);
    const spk = lineSpeaker(r);
    const tone = st.tone==="bad" ? "bad" : st.tone==="warn" ? "warn" : "";
    const elapsed = r.session ? fmt(r.session.elapsed)
      : (r.state==="failed" ? "失败" : r.state==="pending" ? "待启动"
      : r.state==="starting" ? "启动中" : r.state==="stopped" ? (r.interrupted ? "已中断" : "已停止") : "—");
    const meta = [];
    if(r.session){
      meta.push("已录 <b>"+fmt(r.session.elapsed)+"</b>");
      meta.push("<b>"+num(r.session.n_utterances)+"</b> 句");
      if(num(r.session.dropped)) meta.push("丢 <b>"+num(r.session.dropped)+"</b> 块");
    } else if(r.state==="failed"){
      meta.push("受阻于<b>"+esc(stageLabel(r))+"</b>");
      meta.push("添加于 "+timeOf(r.created_at));
    } else if(r.state==="pending"){
      meta.push("添加于 "+timeOf(r.created_at));
    } else if(r.state==="starting"){
      meta.push("正在<b>"+esc(stageLabel(r))+"</b>");
    } else if(r.state==="stopped"){
      meta.push(r.hasRecording ? "已保存" : (r.sessionId ? "没有录到音频" : "已停止"));
    } else if(r.state==="live"){
      meta.push("在听 · 会话丢失");
    }
    if(r.updated_at) meta.push("更新 "+timeOf(r.updated_at));

    const acts = [];
    if(isListening(r)){
      acts.push('<button class="act primary" data-act="live" data-id="'+r.id+'">实时字幕</button>');
      acts.push('<button class="act" data-act="reader" data-id="'+r.id+'">回放</button>');
    } else if(r.state==="stopped"){
      if(r.sessionId) acts.push('<button class="act primary" data-act="reader" data-id="'+r.id+'">回放</button>');
      acts.push('<button class="act" data-act="retry" data-id="'+r.id+'">重试</button>');
    } else if(r.state==="failed" || r.state==="live"){
      if(r.state==="failed" && r.switchReady){
        acts.push('<button class="act primary" data-act="switch" data-id="'+r.id+'">切换到'+esc(switchEngineLabel(r.switchEngine))+'</button>');
      }
      acts.push('<button class="act'+(r.state==="failed"&&r.switchReady?"":" primary")+'" data-act="retry" data-id="'+r.id+'">'
        +(r.state==="failed"&&r.switchReady?"重试直取":"重试")+'</button>');
      if(r.url) acts.push('<a class="act" href="'+esc(r.url)+'" target="_blank" rel="noopener" data-act="open" data-id="'+r.id+'">打开直播间</a>');
    } else if(r.state==="pending"){
      acts.push('<button class="act" data-act="cancel" data-id="'+r.id+'">取消</button>');
    } else if(r.state==="starting"){
      acts.push('<button class="act" data-act="diag" data-id="'+r.id+'">看诊断</button>');
    }
    acts.push('<button class="act more" data-act="detail" data-id="'+r.id+'" aria-label="放大 '+esc(r.name)+'">⋯</button>');

    const lineCls = st.tone==="bad" ? "line err" : (r.state==="pending"||r.state==="starting") ? "line quiet" : "line";
    const eng = engineLabel(r);
    return '<article class="card tone-'+tone+'" data-id="'+r.id+'" tabindex="0" role="button" aria-label="'+esc("CH"+r.ch+" "+r.name+"，"+st.label+"，点击放大")+'">'
      + '<div class="signal">'
      +   '<div class="sig-top"><span class="ch">CH'+r.ch+'</span>'
      +   (eng?'<span class="eng '+(r.capture_engine==="direct"?"direct":"")+'">'+esc(eng)+'</span>':'')
      +   '<span class="sig-state '+st.tone+'"><i></i>'+st.label+'</span></div>'
      +   '<canvas class="wave" data-id="'+r.id+'" aria-hidden="true"></canvas>'
      +   '<div class="sig-foot"><span class="elapsed">'+esc(elapsed)+'</span>'
      +     '<span class="ingest '+ing.tone+'">'+ing.label+'</span></div>'
      + '</div>'
      + '<div class="card-body">'
      +   '<h3 class="name" title="'+esc(r.name)+'">'+esc(r.name)+'</h3>'
      +   '<div class="meta">'+meta.join(" · ")+'</div>'
      +   '<p class="'+lineCls+'">'+(spk?'<span class="spk">'+esc(spk)+'</span>':'')+esc(line)+'</p>'
      +   (r.switchReady
          ? '<div class="switch-hint"><b>直取失败</b><span>'
            +(r.switchRequested?"已确认，正在切换…":"需要你确认才能切到"+esc(switchEngineLabel(r.switchEngine))+"采集")
            +'</span></div>'
          : '')
      +   '<div class="card-actions">'+acts.join("")+'</div>'
      + '</div>'
      + '</article>';
  }
  function emptyHtml(){
    return '<div class="empty"><div class="big">◌</div>还没有在听的直播间<br>'
      + '把链接粘到上面就行——<b>抖音里点分享 → 复制链接</b>，整段粘过来也能认出链接</div>';
  }
  function renderWall(){
    const box = $("#wall");
    const rows = visibleRooms();
    if(!rows.length){ box.innerHTML = emptyHtml(); return; }
    box.innerHTML = rows.map(cardHtml).join("");
    $$(".card", box).forEach(card=>{
      const id = card.dataset.id;
      card.addEventListener("click", e=>{
        if(e.target.closest("[data-act]")) return;
        openDetail(id);
      });
      card.addEventListener("keydown", e=>{
        if(e.target.closest("[data-act]")) return;
        if(e.key==="Enter" || e.key===" "){ e.preventDefault(); openDetail(id); }
      });
    });
    $$("[data-act]", box).forEach(el=>{
      el.addEventListener("click", e=>{
        e.stopPropagation();
        handleAction(el.dataset.act, el.dataset.id, el);
      });
    });
  }
  /* ================================================================ render: list */
  function renderList(){
    const box = $("#listView");
    const rows = state.rooms.slice().sort((a,b)=>{
      const la = a.state==="live"?0:1, lb = b.state==="live"?0:1;
      if(la!==lb) return la-lb;
      return String(b.created_at||"").localeCompare(String(a.created_at||""));
    });
    if(!rows.length){ box.innerHTML = emptyHtml(); return; }
    box.innerHTML = rows.map(r=>{
      const st = statusMeta(r);
      const chip = st.tone==="bad" ? "failed" : st.tone==="warn" ? "starting" : st.tone==="ok" ? "live" : "pending";
      const nm = r.name || "（未命名）";
      const eng = engineLabel(r);
      let meta = (eng?eng+" · ":"") + (r.url || "");
      if(r.session) meta = "已录 "+fmt(r.session.elapsed)+" · "+num(r.session.n_utterances)+" 句 · "+meta;
      else if(r.state==="stopped") meta = (r.hasRecording ? "录音已保存" : "没有录到音频") + " · " + meta;
      const acts = [];
      if(isListening(r)) acts.push('<a href="/live?session='+encodeURIComponent(r.sessionId||"")+'">实时字幕</a>');
      if(r.sessionId) acts.push('<a href="/reader?session='+encodeURIComponent(r.sessionId)+'">回放</a>');
      if(r.state==="failed" && r.switchReady) acts.push('<button data-lact="switch" data-id="'+r.id+'">切换浏览器</button>');
      if(r.state==="failed" || r.state==="stopped" || (r.state==="live" && !isListening(r))) acts.push('<button data-lact="retry" data-id="'+r.id+'">'+(r.state==="failed"&&r.switchReady?"重试直取":"重试")+'</button>');
      if(r.state==="live" || r.state==="starting") acts.push('<button class="danger" data-lact="stop" data-id="'+r.id+'">停止</button>');
      acts.push('<button class="danger" data-lact="del" data-id="'+r.id+'">删除</button>');
      return '<div class="row">'
        + '<span class="dot '+st.dot+'"></span>'
        + '<div class="main"><div class="nm">'+esc(nm)+'</div>'
        + '<div class="rmeta">'+esc(meta)+'</div>'
        + (r.error?'<div class="rerr">'+esc(r.error)+'</div>':'')
        + '</div>'
        + '<span class="rchip '+chip+'">'+st.label+'</span>'
        + '<div class="racts">'+acts.join("")+'</div>'
        + '</div>';
    }).join("");
    $$("[data-lact]", box).forEach(b=>b.onclick=()=>{
      const k = b.dataset.lact;
      if(k==="retry") handleAction("retry", b.dataset.id, b);
      if(k==="switch") handleAction("switch", b.dataset.id, b);
      if(k==="stop") handleAction("stop", b.dataset.id, b);
      if(k==="del") handleAction("del", b.dataset.id, b);
    });
  }

  /* ================================================================ render */
  function render(){
    renderTop();
    renderNotices();
    renderFilters();
    const wall = $("#wall"), list = $("#listView");
    wall.hidden = state.view!=="wall";
    list.hidden = state.view!=="list";
    if(state.view==="wall") renderWall(); else renderList();
    if(state.detailId) renderDetail();
    if(state.diagOpen) renderDiag();
  }

  /* ================================================================ detail */
  function roomById(id){ return state.rooms.find(r=>r.id===id) || null; }

export { cardHtml, emptyHtml, filterCounts, render, renderFilters, renderList, renderNotices, renderTop, renderWall, roomById, visibleRooms };
