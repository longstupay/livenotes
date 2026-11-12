// 由 app/web/rooms.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { handleAction } from './actions.js';
import { drawDetailWave } from './canvas.js';
import { $, $$, STAGES, esc, fmt, num, state, syncScrollLock, timeOf } from './core.js';
import { fetchDetailHistory, startDetailPoll, stopDetailPoll } from './data.js';
import { agoText, browserMeta, diagRooms, engineLabel, ingestMeta, isListening, lastChunkAgo, lastLine, lineSpeaker, stageIndex, stageLabel, stageNote, statusMeta, stepState, switchEngineLabel } from './derived.js';
import { roomById, visibleRooms } from './render.js';
import { syncUrl } from './view.js';
  function detailSignalHtml(r){
    const st = statusMeta(r);
    const ing = ingestMeta(r);
    const fail = r.state==="failed" || (r.state==="live" && !r.session);
    const line = lastLine(r);
    const spk = lineSpeaker(r);
    let center = "";
    if(fail){
      center = r.switchReady
        ? '<div class="center-note"><span class="mark">×</span><b>直取失败，等你确认</b>'
          + '<span>'+(r.switchRequested?"已确认，正在切换采集方式":"需要切换采集方式才能继续")+'</span></div>'
        : '<div class="center-note"><span class="mark">×</span><b>停在「'+esc(stageLabel(r))+'」</b>'
          + '<span>原因见下方 · 修好后可以重试</span></div>';
    } else if(r.state==="pending" || r.state==="starting"){
      const at = stageIndex(r);
      center = '<div class="center-note"><b>'+st.label+'</b><span>'+esc(stageLabel(r))+' · '+esc(stageNote(r,at))+'</span></div>';
    }
    return '<div class="detail-signal">'
      + '<div class="sig-top"><span class="ch">CH'+r.ch+'</span><span class="sig-state '+st.tone+'"><i></i>'+st.label+'</span></div>'
      + '<canvas class="wave" data-detail-wave="'+r.id+'" aria-hidden="true"></canvas>'
      + (center || ('<div class="last-line">'+(spk?'<span class="spk">'+esc(spk)+'</span>':'')+esc(line)+'</div>'))
      + '<div class="sig-foot"><span class="elapsed">'+(r.session?fmt(r.session.elapsed):"—")+'</span>'
      + '<span class="ingest '+ing.tone+'">'+ing.label+'</span></div>'
      + '</div>';
  }
  function stepsHtml(r){
    return '<div class="steps">'+STAGES.map((s,i)=>{
      const cls = stepState(r,i);
      const mark = cls==="done" ? "✓" : cls==="failed" ? "×" : cls==="current" ? "●" : "";
      return '<div class="step '+cls+'"><div class="rail"><span class="dot">'+mark+'</span><span class="bar"></span></div>'
        + '<div><div class="label">'+esc(s.label)+'</div>'
        + '<div class="note">'+esc(stageNote(r,i))+'</div></div></div>';
    }).join("")+'</div>';
  }
  const SUB_ROWS = 5;
  function subsHtml(r){
    const subs = (r.subtitles||[]).slice(-SUB_ROWS);
    const canSeek = isListening(r) && !!r.sessionId;
    if(subs.length){
      // 固定高度视窗 + 底部对齐：新句子从下方进入，旧句子从顶部溢出。
      // 每行单行省略，完整内容放 title；要看全文去「全部字幕」。
      // 在听的场次里点一行 = 从这一秒开始放原声（采集中的 spool）。
      return '<div class="subs-view">'
        + '<div class="subs">'+subs.map(s=>
          '<div class="sub-line '+(s.partial?"partial":"")+(canSeek&&s.t!=null?" seekable":"")+'"'
          + (canSeek&&s.t!=null?' data-lt="'+esc(s.t)+'"':'')
          + ' title="'+esc(s.text)+'">'
          + '<span class="who '+(s.speaker==="嘉宾"?"guest":"")+'">'+esc(s.speaker||"说话人")+'</span>'
          + '<span class="what">'+esc(s.text)+'</span>'
          + (s.t!=null?'<span class="at">'+esc(fmt(s.t))+'</span>':'')
          + '</div>'
        ).join("")+'</div></div>';
    }
    let msg = "正在等待第一句字幕…", live = true;
    if(r.state==="failed"){
      msg = "这一路没有起来，没有字幕。"; live = false;
    } else if(r.state==="pending" || r.state==="starting"){
      msg = "启动后才会开始出字。"; live = false;
    } else if(r.state==="stopped" || (r.session && r.session.history)){
      msg = "这一场没有留下字幕。"; live = false;
    } else if(r.state==="live" && !r.session){
      msg = "会话丢失，正在等采集端重新建立…"; live = false;
    } else if(r.session && num(r.session.n_utterances) > 0){
      // 已经有句子但详情还没拉回来：这是「读取中」，不是「还没出字」。
      msg = "正在读取字幕…";
    }
    // 空态和列表共用同一个固定高度，切换时下面的「启动过程」不会上下跳。
    return '<div class="subs-view is-empty"><div class="sub-wait">'
      + (live?'<i class="pulse" aria-hidden="true"></i>':'')
      + '<span>'+esc(msg)+'</span></div></div>';
  }
  function detailBodyHtml(r){
    const st = statusMeta(r);
    const ing = ingestMeta(r);
    const session = r.session;
    const left = [];
    const broken = r.state==="failed" || (r.state==="live" && !r.session);
    if(broken){
      const msg = r.error || (r.state==="live" ? "本地服务里没有这一路的会话，可能刚重启或采集页已关闭。" : "这一步没有通过");
      if(r.switchReady){
        const label = switchEngineLabel(r.switchEngine);
        left.push('<div class="sect"><div class="sect-h">需要你确认 <span class="spacer"></span><span class="hint">直取失败不会自动切换</span></div>'
          + '<div class="switch-ask"><b>继续听这一路，需要改用'+esc(label)+'采集。</b>'
          + '<p>'+esc(r.switchReason||msg)+'</p>'
          + (r.switchRequested
              ? '<div class="switch-actions"><span class="hintline">已确认，采集代理正在切换…</span></div>'
              : '<div class="switch-actions"><button class="btn primary" data-dact="switch" data-id="'+r.id+'">切换到'+esc(label)+'采集</button>'
                + '<button class="btn" data-dact="retry" data-id="'+r.id+'">重试直取</button></div>')
          + '</div></div>');
      }
      left.push('<div class="sect"><div class="sect-h">为什么没起来 <span class="spacer"></span><span class="hint">停在：'+esc(stageLabel(r))+'</span></div>'
        + '<div class="err-block"><b>'+esc(msg)+'</b>'
        + '<div class="hintline">修好后点「重试」，会重新拉起这一路（重新开浏览器、重新取流），不是从断点续传。</div></div></div>');
      left.push('<div class="sect"><div class="sect-h">启动过程</div>'+stepsHtml(r)+'</div>');
    } else {
      const hint = r.state==="stopped" ? (r.interrupted ? "已中断" : "已停止")
        : (session && session.history) ? "历史"
        : (session && session.running===false) ? "已结束" : "实时";
      const more = (isListening(r) && r.sessionId)
        ? '<a class="sect-link" href="/live?session='+encodeURIComponent(r.sessionId)+'">全部字幕 →</a>'
        : '';
      left.push('<div class="sect"><div class="sect-h">最新字幕 <span class="spacer"></span>'
        + '<span class="hint">'+hint+' · 最近 '+SUB_ROWS+' 句</span>'+more+'</div>'+subsHtml(r)+'</div>');
      left.push('<div class="sect"><div class="sect-h">启动过程</div>'+stepsHtml(r)+'</div>');
    }

    const kv = [];
    const bm = browserMeta(r);
    kv.push('<dt>房间状态</dt><dd class="'+st.tone+'">'+st.label+'</dd>');
    kv.push('<dt>采集浏览器</dt><dd class="'+bm.tone+'">'+esc(bm.label)+'</dd>');
    kv.push('<dt>入库</dt><dd class="'+ing.tone+'">'+ing.label+'</dd>');
    kv.push('<dt>会话</dt><dd class="'+(r.sessionId?"mono":"muted")+'">'+esc(r.sessionId||"—")+'</dd>');
    kv.push('<dt>已录</dt><dd>'+(session?fmt(session.elapsed):"—")+'</dd>');
    kv.push('<dt>句数</dt><dd>'+(session?num(session.n_utterances):"—")+'</dd>');
    const ca = session ? lastChunkAgo(session) : null;
    kv.push('<dt>最近一块</dt><dd class="'+(ca==null?"muted":ca<3?"ok":ca<15?"warn":"bad")+'">'
      +(ca==null?"—":esc(agoText(ca)))+'</dd>');
    const fa = (session && session.last_feed_ago_s != null) ? num(session.last_feed_ago_s) : null;
    kv.push('<dt>最后入库</dt><dd class="'+(fa==null?"muted":fa<15?"ok":"bad")+'">'
      +(fa==null?"—":esc(agoText(fa)))+'</dd>');
    kv.push('<dt>丢块</dt><dd class="'+(session&&num(session.dropped)?"warn":"muted")+'">'+(session?num(session.dropped):"—")+'</dd>');
    kv.push('<dt>队列</dt><dd class="muted">'+(session?num(session.queued):"—")+'</dd>');
    if(r.state==="stopped"){
      kv.push('<dt>录音</dt><dd class="'+(r.hasRecording?"saved":"muted")+'">'+(r.hasRecording?"已保存":"没有录到")+'</dd>');
    }
    kv.push('<dt>添加时间</dt><dd class="muted">'+esc(timeOf(r.created_at)||"—")+'</dd>');
    if(r.error && !broken) kv.push('<dt>错误</dt><dd class="err">'+esc(r.error)+'</dd>');

    const acts = [];
    if(isListening(r) && r.sessionId) acts.push('<button class="btn primary" data-dact="listen" data-id="'+r.id+'">听最近</button>');
    if(isListening(r)) acts.push('<button class="btn" data-dact="live" data-id="'+r.id+'">实时字幕</button>');
    if(r.sessionId) acts.push('<button class="btn" data-dact="reader" data-id="'+r.id+'">回放</button>');
    if(r.state==="failed" && r.switchReady){
      acts.push('<button class="btn primary" data-dact="switch" data-id="'+r.id+'">切换到'+esc(switchEngineLabel(r.switchEngine))+'采集</button>');
    }
    if(r.state==="failed" || r.state==="stopped" || (r.state==="live" && !isListening(r))){
      acts.push('<button class="btn" data-dact="retry" data-id="'+r.id+'">'
        +(r.state==="failed"&&r.switchReady?"重试直取":"重试")+'</button>');
    }
    if(r.state==="live" || r.state==="starting") acts.push('<button class="btn danger" data-dact="stop" data-id="'+r.id+'">停止</button>');
    if(r.state==="pending") acts.push('<button class="btn" data-dact="cancel" data-id="'+r.id+'">取消</button>');
    acts.push('<button class="btn danger" data-dact="del" data-id="'+r.id+'">删除</button>');

    return '<div class="detail-grid">'
      + '<div class="detail-main">'+left.join("")+'</div>'
      + '<div class="detail-side">'
      +   '<div class="sect"><div class="sect-h">运行状态</div><dl class="kv">'+kv.join("")+'</dl></div>'
      +   '<div class="sect"><div class="sect-h">操作</div><div class="dacts" style="display:flex;gap:6px;flex-wrap:wrap">'+acts.join("")+'</div></div>'
      + '</div></div>';
  }
  // 轮询每 1~2 秒就会调一次 renderDetail。以前每次都重建整个 .sheet，
  // 入场动画 sheetIn 跟着重播，页面看起来一直在闪。现在只在真正打开/切换
  // 房间时播动画；同一房间内容没变（比如已停止的场次）就完全不重建。
  let detailRenderedId = null;
  let detailHtml = "";
  function renderDetail(){
    const r = roomById(state.detailId);
    if(!r){ closeDetail(); return; }
    const st = statusMeta(r);
    const overlay = $("#detailOverlay");
    const wasActive = overlay.contains(document.activeElement);
    const scrollTop = overlay.scrollTop;
    const fresh = overlay.hidden || detailRenderedId !== r.id;
    const attrs = ' role="dialog" aria-modal="true" aria-label="'+esc(r.name)+' 详情"';
    const inner = '<div class="sheet-head"><div class="titles"><h2><span class="ch">CH'+r.ch+'</span>'+esc(r.name)+'</h2>'
      + '<div class="sub2">'+(engineLabel(r)?'<span class="eng '+(r.capture_engine==="direct"?"direct":"")+'">'+esc(engineLabel(r))+'</span> ':'')+esc(r.url||"")+'</div></div>'
      + '<div class="head-actions">'
      + (r.url?'<a class="btn small" href="'+esc(r.url)+'" target="_blank" rel="noopener">打开直播间</a>':'')
      + '<button class="btn ghost small" data-dact="rename" data-id="'+r.id+'" '
      + 'title="认不出这一路是什么的时候，自己给它起个名字">改名</button>'
      + '<button class="icon-btn" data-dclose aria-label="关闭">×</button></div></div>'
      + detailSignalHtml(r)
      + '<div class="sheet-body" style="padding:0">'+detailBodyHtml(r)+'</div>'
      + '<div class="sheet-foot">'
      + '<span class="spacer"></span>'
      + '<button class="btn ghost small" data-dnav="prev">← 上一个</button>'
      + '<button class="btn ghost small" data-dnav="next">下一个 →</button>'
      + '</div>';
    overlay.hidden = false;
    if(fresh || attrs + inner !== detailHtml){
      overlay.innerHTML = '<div class="sheet'+(fresh?"":" no-anim")+'"'+attrs+'>'+inner+'</div>';
      detailHtml = attrs + inner;
      detailRenderedId = r.id;
      $$("[data-dclose]", overlay).forEach(b=>b.onclick=closeDetail);
      overlay.onclick = e=>{ if(e.target===overlay) closeDetail(); };
      $$("[data-dact]", overlay).forEach(b=>b.onclick=()=>handleAction(b.dataset.dact,b.dataset.id,b));
      $$(".sub-line.seekable", overlay).forEach(el=>el.onclick=()=>{
        const cur = roomById(r.id);
        if(!cur || !cur.sessionId || !isListening(cur)) return;
        // 拖选字幕时不触发播放，避免复制一句话就跳走。
        if(window.getSelection && String(window.getSelection()).length) return;
        const t = Number(el.dataset.lt);
        if(!isFinite(t) || !window.LiveAudio) return;
        window.LiveAudio.openAt(cur.sessionId, t, {label:"CH"+cur.ch+" "+(cur.name||"")});
      });
      $$("[data-dnav]", overlay).forEach(b=>b.onclick=()=>switchDetail(b.dataset.dnav));
      const close = $("[data-dclose]", overlay);
      if(close && !wasActive) close.focus();
    }
    overlay.scrollTop = scrollTop;
    syncScrollLock();
    drawDetailWave(r);
  }
  function openDetail(id, opts){
    if(!roomById(id)) return;
    state.detailId = id;
    state.lastFocus = document.activeElement;
    renderDetail();
    syncUrl();
    const r = roomById(id);
    if(r.sessionId){
      // 停止/失败/重启后的房间在内存里没有 session 对象，直接走历史接口补齐；
      // 只有还活着的房间才值得每 1.2 秒问一次 /api/live/status。
      if(r.state==="stopped" || r.state==="failed") fetchDetailHistory(r);
      else startDetailPoll(id);
    }
  }
  function closeDetail(){
    stopDetailPoll();
    state.detailId = null;
    detailRenderedId = null;
    detailHtml = "";
    const o = $("#detailOverlay");
    o.hidden = true; o.innerHTML = "";
    syncScrollLock();
    if(state.lastFocus && state.lastFocus.focus) state.lastFocus.focus();
    syncUrl();
  }
  function switchDetail(dir){
    const rows = visibleRooms();
    if(!rows.length) return;
    let i = rows.findIndex(r=>r.id===state.detailId);
    if(i<0) i=0;
    i = (i + (dir==="next"?1:-1) + rows.length) % rows.length;
    openDetail(rows[i].id);
  }

  /* ================================================================ diagnostic */
  function diagRowHtml(r){
    const st = statusMeta(r);
    const at = stageIndex(r);
    const chips = STAGES.map((s,i)=>{
      const cls = stepState(r,i);
      const mark = cls==="done"?"✓":cls==="failed"?"×":cls==="current"?"●":"";
      return '<span class="dstep '+cls+'"><i>'+mark+'</i>'+esc(s.label)+'</span>';
    }).join("");
    const acts = [];
    if(r.state==="failed" && r.switchReady) acts.push('<button class="btn small primary" data-gact="switch" data-id="'+r.id+'">切换到'+esc(switchEngineLabel(r.switchEngine))+'</button>');
    if(r.state==="failed" || (r.state==="live" && !isListening(r))) acts.push('<button class="btn small" data-gact="retry" data-id="'+r.id+'">'+(r.state==="failed"&&r.switchReady?"重试直取":"重试")+'</button>');
    if(r.state==="pending") acts.push('<button class="btn small" data-gact="cancel" data-id="'+r.id+'">取消</button>');
    if(r.state==="starting") acts.push('<button class="btn small danger" data-gact="stop" data-id="'+r.id+'">停止</button>');
    if(r.url) acts.push('<a class="btn small" href="'+esc(r.url)+'" target="_blank" rel="noopener">打开直播间</a>');
    acts.push('<button class="btn small ghost" data-gact="detail" data-id="'+r.id+'">看详情</button>');
    return '<div class="diag-row '+(st.tone==="bad"?"bad":st.tone==="warn"?"warn":"")+'">'
      + '<div class="dr-head"><span class="ch">CH'+r.ch+'</span><span class="nm">'+esc(r.name)+'</span>'
      + '<span class="rchip '+st.key+'" style="margin-left:auto">'+st.label+'</span></div>'
      + (r.error?'<p class="dr-err">'+esc(r.error)+'</p>':'')
      + '<div class="dr-steps">'+chips+'</div>'
      + '<div class="dr-acts">'+acts.join("")+'</div></div>';
  }
  let diagRendered = false;
  let diagHtml = "";
  function renderDiag(){
    const rows = diagRooms();
    const bad = rows.filter(r=>statusMeta(r).tone==="bad").length;
    const warn = rows.filter(r=>statusMeta(r).tone==="warn" && r.state!=="pending" && r.state!=="starting").length;
    const starting = rows.filter(r=>r.state==="pending"||r.state==="starting").length;
    const overlay = $("#diagOverlay");
    const wasActive = overlay.contains(document.activeElement);
    const scrollTop = overlay.scrollTop;
    const fresh = overlay.hidden || !diagRendered;
    const inner = '<div class="sheet-head"><div class="titles"><h2>启动诊断</h2>'
      + '<div class="sub2">'+(rows.length?rows.length+" 路还没进入「在听」 · 在听的房间不在这里":"全部在听，没有需要诊断的房间")+'</div></div>'
      + '<div class="head-actions"><button class="icon-btn" data-gclose aria-label="关闭">×</button></div></div>'
      + '<div class="sheet-body">'
      + (rows.length
        ? '<div class="diag-sum">'
          + (bad?'<span class="chip"><span class="dot" style="background:var(--bad)"></span>失败 '+bad+'</span>':'')
          + (starting?'<span class="chip"><span class="dot" style="background:var(--warn)"></span>启动中 '+starting+'</span>':'')
          + (warn?'<span class="chip"><span class="dot" style="background:var(--warn)"></span>需留意 '+warn+'</span>':'')
          + '</div><div class="diag-list">'+rows.map(diagRowHtml).join("")+'</div>'
        : '<div class="diag-empty"><div class="big">✓</div><b>全部在听。</b><br>没有待启动、启动中或失败的房间。</div>')
      + '</div>'
      + '<div class="sheet-foot"><span class="spacer"></span>'
      + '<button class="btn small ghost" data-gclose>关闭</button></div>';
    overlay.hidden = false;
    if(fresh || inner !== diagHtml){
      overlay.innerHTML = '<div class="sheet diag'+(fresh?"":" no-anim")+'" role="dialog" aria-modal="true" aria-label="启动诊断">'+inner+'</div>';
      diagHtml = inner;
      diagRendered = true;
      $$("[data-gclose]", overlay).forEach(b=>b.onclick=closeDiag);
      overlay.onclick = e=>{ if(e.target===overlay) closeDiag(); };
      $$("[data-gact]", overlay).forEach(b=>b.onclick=()=>{
        const k = b.dataset.gact;
        if(k==="detail"){ closeDiag(); openDetail(b.dataset.id); }
        else handleAction(k,b.dataset.id,b);
      });
      const close = $("[data-gclose]", overlay);
      if(close && !wasActive) close.focus();
    }
    overlay.scrollTop = scrollTop;
    syncScrollLock();
  }
  function openDiag(){
    state.diagOpen = true;
    state.lastFocus = document.activeElement;
    renderDiag();
    syncUrl();
  }
  function closeDiag(){
    state.diagOpen = false;
    diagRendered = false;
    diagHtml = "";
    const o = $("#diagOverlay");
    o.hidden = true; o.innerHTML = "";
    syncScrollLock();
    if(state.lastFocus && state.lastFocus.focus) state.lastFocus.focus();
    syncUrl();
  }

  /* ================================================================ actions */

export { closeDetail, closeDiag, detailBodyHtml, detailSignalHtml, diagRowHtml, openDetail, openDiag, renderDetail, renderDiag, stepsHtml, subsHtml, switchDetail };
