/* 采集中回听：一个固定在底部的播放器，/live 与 /rooms 共用。
 *
 * 服务端只给「窗口」：先问 /api/live/audio/meta 拿到字节快照的起止秒，
 * 再用同一组 start/end 请求 /api/live/audio。播放器不关心 PCM 怎么写的，
 * 只负责把绝对时间轴（录音内偏移）显示出来。
 */
(function(){
  "use strict";

  if (window.LiveAudio) return;

  var S = {
    sid:"", label:"", open:false, mode:"tail", windowSec:300, t:0,
    start:0, end:0, total:0, paused:false, running:false, live:true,
    chunkAgo:null, loading:false, message:"", seekEnd:false, retry:0, poll:0, gen:0
  };
  var bar = null, audio = null, els = {};

  var CSS = [
    "body.la-open{padding-bottom:96px}",
    // 贴住左右边而不是 left:50% + translateX(-50%)：飞书等内嵌 WebView 对
    // transform / 100vw / min() 的支持参差，旧内核上会整条跑到屏幕外。
    // left/right + margin:auto 在任何内核上都居中，最差也只是铺满整行。
    ".la-bar{position:fixed;left:0;right:0;bottom:14px;margin:0 auto;z-index:130;",
    "  width:calc(100% - 24px);max-width:1080px;box-sizing:border-box;display:flex;flex-wrap:wrap;",
    "  align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line,#d8dee9);border-radius:12px;",
    "  background:var(--surface,rgba(255,255,255,.98));box-shadow:0 10px 30px rgba(20,30,50,.16);",
    "  font:12.5px/1.4 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',system-ui,sans-serif;",
    "  color:var(--ink,#1d2330)}",
    ".la-bar[hidden]{display:none}",
    ".la-main{flex:1 1 190px;min-width:150px}",
    ".la-title{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".la-sub{margin-top:2px;color:var(--muted,#6b7480);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".la-btn{font:inherit;padding:5px 10px;border-radius:8px;border:1px solid var(--line,#d8dee9);background:var(--surface,#fff);",
    "  cursor:pointer;color:var(--ink,#3c4656);white-space:nowrap}",
    ".la-btn:hover{border-color:var(--accent,#2b5cff);color:var(--accent,#2b5cff)}",
    ".la-play{width:38px;height:38px;border-radius:50%;padding:0;font-size:15px;",
    "  background:var(--accent,#2b5cff);border-color:var(--accent,#2b5cff);color:var(--surface,#fff);flex:0 0 auto}",
    ".la-play:hover{filter:brightness(.94);background:var(--accent,#2b5cff);color:var(--surface,#fff)}",
    ".la-track{flex:2 1 240px;min-width:170px;display:flex;align-items:center;gap:8px}",
    ".la-track input[type=range]{flex:1;min-width:80px;accent-color:var(--accent,#2b5cff);margin:0}",
    ".la-times{display:flex;gap:6px;font-variant-numeric:tabular-nums;color:var(--muted,#6b7480);",
    "  font-size:11px;white-space:nowrap}",
    ".la-wins{display:flex;gap:4px;flex:0 0 auto}",
    ".la-wins button{font:inherit;font-size:11px;padding:3px 7px;border-radius:6px;",
    "  border:1px solid var(--line,#e2e6ee);background:var(--surface,#fff);color:var(--muted,#6b7480);cursor:pointer}",
    ".la-wins button.on{border-color:var(--accent,#2b5cff);color:var(--accent,#2b5cff);background:var(--accent-soft,#eef3ff)}",
    ".la-close{flex:0 0 auto;width:30px;height:30px;border-radius:8px;border:1px solid transparent;",
    "  background:transparent;color:var(--muted,#9aa3b0);font-size:17px;line-height:1;cursor:pointer}",
    ".la-close:hover{background:var(--surface-2,#f0f2f6);color:var(--ink,#3c4656)}",
    "@media(max-width:720px){",
    // 手机上两行就够：标题 / 播放+进度+跳到最新+关闭。
    // 时长窗口按钮先收起（回听链接本来就是定位播放），别把条撑到屏幕外。
    "  body.la-open{padding-bottom:120px}",
    "  .la-bar{left:0;right:0;bottom:calc(8px + env(safe-area-inset-bottom,0px));",
    "    width:calc(100% - 16px);gap:8px;padding:9px 10px;max-height:55vh;overflow:auto}",
    "  .la-main{flex:1 1 100%;min-width:0}",
    "  .la-sub{white-space:normal}",
    "  .la-play{width:34px;height:34px;font-size:14px}",
    "  .la-track{flex:1 1 140px;min-width:120px}",
    "  .la-track input[type=range]{min-width:0;width:100%}",
    "  .la-times{flex:0 0 auto}",
    "  #laLatest{padding:5px 8px}",
    "  .la-wins{display:none}",
    "}"
  ].join("\n");

  var HTML =
    '<div class="la-main"><div class="la-title" id="laTitle">最近音频</div>'
    + '<div class="la-sub" id="laSub">—</div></div>'
    + '<button class="la-btn la-play" id="laPlay" type="button" aria-label="播放">▶</button>'
    + '<div class="la-track"><input id="laSeek" type="range" min="0" max="1" step="0.1" value="0" aria-label="进度">'
    + '<div class="la-times"><span id="laCur">0:00</span><span id="laDur">0:00</span></div></div>'
    + '<div class="la-wins" id="laWins">'
    + '<button type="button" data-la-win="30">30秒</button>'
    + '<button type="button" data-la-win="300" class="on">5分钟</button>'
    + '<button type="button" data-la-win="600">10分钟</button></div>'
    + '<button class="la-btn" id="laLatest" type="button">跳到最新</button>'
    + '<button class="la-close" id="laClose" type="button" aria-label="关闭">×</button>'
    + '<audio id="laAudio" preload="metadata"></audio>';

  function fmt(sec){
    sec = Math.max(0, Math.round(Number(sec) || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    function p(n){ return String(n).padStart(2, "0"); }
    return h ? (h + ":" + p(m) + ":" + p(s)) : (m + ":" + p(s));
  }
  function fmtLen(sec){
    sec = Math.max(0, Number(sec) || 0);
    if (sec < 60) return Math.round(sec) + " 秒";
    return Math.round(sec / 60) + " 分钟";
  }
  function reasonText(r){
    if (r === "not_active") return "这一路不在采集，可能已经停止";
    if (r === "no_audio") return "采集刚开始，还没有收到音频";
    if (r === "too_short") return "这一段音频还太短，等几秒再试";
    return "现在没有可听的音频";
  }

  function ensure(){
    if (bar) return;
    var st = document.createElement("style");
    st.id = "laStyle";
    st.textContent = CSS;
    document.head.appendChild(st);
    bar = document.createElement("div");
    bar.className = "la-bar";
    bar.id = "laBar";
    bar.hidden = true;
    bar.innerHTML = HTML;
    document.body.appendChild(bar);

    els.title = document.getElementById("laTitle");
    els.sub = document.getElementById("laSub");
    els.play = document.getElementById("laPlay");
    els.seek = document.getElementById("laSeek");
    els.cur = document.getElementById("laCur");
    els.dur = document.getElementById("laDur");
    els.wins = document.getElementById("laWins");
    els.latest = document.getElementById("laLatest");
    els.close = document.getElementById("laClose");
    audio = document.getElementById("laAudio");

    els.play.onclick = function(){ if (audio.paused) play(); else audio.pause(); };
    els.seek.oninput = function(){
      if (!audio.duration) return;
      try { audio.currentTime = Number(els.seek.value) || 0; } catch (e) {}
      updateLabels();
    };
    els.wins.onclick = function(e){
      var b = e.target.closest ? e.target.closest("[data-la-win]") : null;
      if (!b) return;
      S.mode = "tail";
      S.windowSec = Number(b.getAttribute("data-la-win")) || 300;
      syncWins();
      loadWindow({ seconds:S.windowSec, autoplay:true });
    };
    els.latest.onclick = function(){
      S.mode = "tail";
      S.windowSec = 20;
      syncWins();
      loadWindow({ seconds:20, autoplay:true, seekEnd:true });
    };
    els.close.onclick = close;

    audio.addEventListener("play", function(){ els.play.textContent = "⏸"; S.message = ""; updateLabels(); });
    audio.addEventListener("pause", function(){ els.play.textContent = "▶"; updateLabels(); });
    audio.addEventListener("timeupdate", updateLabels);
    audio.addEventListener("loadedmetadata", function(){
      if (S.seekEnd && audio.duration){
        try { audio.currentTime = Math.max(0, audio.duration - 0.3); } catch (e) {}
        S.seekEnd = false;
      }
      updateLabels();
    });
    audio.addEventListener("ended", function(){
      S.message = S.live && S.running ? "已到最新，点「跳到最新」继续" : "这一段已经播完";
      updateLabels();
    });
    audio.addEventListener("error", function(){
      if (!S.open || !audio.getAttribute("src")) return;
      S.message = "这段音频读不出来，可能正在收尾或录音已清理";
      updateLabels();
    });
  }

  function syncWins(){
    if (!els.wins) return;
    var bs = els.wins.querySelectorAll("[data-la-win]");
    for (var i = 0; i < bs.length; i++){
      var on = S.mode === "tail" && Number(bs[i].getAttribute("data-la-win")) === S.windowSec;
      bs[i].classList.toggle("on", on);
    }
  }

  function title(){
    if (S.mode === "at") return S.label + " · 从 " + fmt(S.t) + " 开始";
    return S.label + " · 最近 " + fmtLen(S.end - S.start || S.windowSec);
  }

  function status(){
    if (S.loading) return "正在准备音频…";
    if (S.message) return S.message;
    var bits = ["窗口 " + fmt(S.start) + "–" + fmt(S.end)];
    if (S.total) bits.push("已录 " + fmt(S.total));
    if (S.paused) bits.push("已暂停");
    else if (!S.live) bits.push("采集已结束");
    else if (S.chunkAgo != null && S.chunkAgo >= 15) bits.push("断流 " + Math.round(S.chunkAgo) + "s");
    else if (S.running) bits.push("监听中");
    else bits.push("等待音频");
    return bits.join(" · ");
  }

  function updateLabels(){
    if (!bar) return;
    els.title.textContent = title();
    els.sub.textContent = status();
    var dur = (audio && audio.duration && isFinite(audio.duration)) ? audio.duration : (S.end - S.start);
    els.seek.max = Math.max(1, dur || 1);
    if (audio && document.activeElement !== els.seek){
      els.seek.value = Math.min(Math.max(0, audio.currentTime || 0), Number(els.seek.max) || 1);
    }
    els.cur.textContent = fmt(S.start + ((audio && audio.currentTime) || 0));
    els.dur.textContent = fmt(S.end);
    els.play.disabled = !audio || !audio.getAttribute("src");
    els.latest.disabled = !S.live && !S.loading;
  }

  function setDisabled(on){
    if (!bar) return;
    els.play.disabled = on;
    els.seek.disabled = on;
    els.latest.disabled = on;
  }

  function fetchMeta(opts){
    var qs = "session_id=" + encodeURIComponent(S.sid);
    if (opts.seconds != null) qs += "&seconds=" + encodeURIComponent(opts.seconds);
    if (opts.start != null) qs += "&start=" + encodeURIComponent(opts.start);
    if (opts.end != null) qs += "&end=" + encodeURIComponent(opts.end);
    return fetch("/api/live/audio/meta?" + qs + "&_=" + Date.now())
      .then(function(r){ return r.json().then(function(j){
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      }); });
  }

  function play(){
    if (!audio || !audio.getAttribute("src")) return;
    var p = audio.play();
    if (p && p.catch) p.catch(function(){
      S.message = "浏览器拦住了自动播放，点 ▶ 开始";
      updateLabels();
    });
  }

  function loadWindow(opts){
    if (!S.open) return;
    opts = opts || {};
    var gen = ++S.gen;            // 连点窗口按钮时，只有最后一次请求能落地
    clearTimeout(S.retry);
    S.loading = true;
    S.message = "";
    updateLabels();
    setDisabled(true);

    fetchMeta(opts).then(function(m){
      if (!S.open || gen !== S.gen || m.session_id !== S.sid) return;
      S.live = !!m.live;
      S.paused = !!m.paused;
      S.running = !!m.running;
      S.chunkAgo = (m.last_chunk_ago_s == null) ? null : Number(m.last_chunk_ago_s);
      S.total = Number(m.elapsed_s) || 0;
      if (!m.available){
        S.loading = false;
        S.message = m.message || reasonText(m.reason);
        audio.pause();
        audio.removeAttribute("src");
        audio.load();
        setDisabled(true);
        updateLabels();
        // 刚开播：等第一块音频，不让人反复点。
        if (m.live && (m.reason === "no_audio" || m.reason === "too_short")){
          S.retry = setTimeout(function(){
            if (S.open && S.sid === m.session_id) loadWindow(opts);
          }, 3000);
        }
        return;
      }

      var start, end;
      if (m.live){
        start = Number(m.start_s) || 0;
        end = Number(m.end_s) || 0;
      } else {
        // 会话刚结束：同一组 start/end 继续从 WAV 里切，位置不跳。
        var total = Number(m.elapsed_s) || 0;
        start = opts.start != null ? Math.max(0, Math.min(Number(opts.start), total)) : 0;
        end = opts.end != null ? Math.max(start, Math.min(Number(opts.end), total)) : total;
        if (end - start < 0.2){ end = total; start = Math.max(0, end - 60); }
      }
      if (end - start < 0.2){
        S.loading = false;
        S.message = "这一段音频还太短，等几秒再试";
        setDisabled(true);
        updateLabels();
        return;
      }
      S.start = start;
      S.end = end;
      if (opts.seconds != null) S.windowSec = Number(opts.seconds) || S.windowSec;
      S.seekEnd = !!opts.seekEnd;
      var qs = "session_id=" + encodeURIComponent(S.sid)
        + "&start=" + encodeURIComponent(start.toFixed(3))
        + "&end=" + encodeURIComponent(end.toFixed(3))
        + "&_=" + Date.now();
      audio.pause();
      audio.src = "/api/live/audio?" + qs;
      audio.load();
      S.loading = false;
      setDisabled(false);
      syncWins();
      updateLabels();
      if (opts.autoplay !== false) play();
    }).catch(function(e){
      if (!S.open || gen !== S.gen) return;
      S.loading = false;
      S.message = "读取音频信息失败：" + (e && e.message ? e.message : e);
      setDisabled(false);
      updateLabels();
    });
  }

  function startPoll(){
    clearInterval(S.poll);
    S.poll = setInterval(function(){
      if (!S.open || !S.sid) return;
      fetchMeta({ seconds:1 }).then(function(m){
        if (!S.open || m.session_id !== S.sid) return;
        S.live = !!m.live;
        S.paused = !!m.paused;
        S.running = !!m.running;
        S.chunkAgo = (m.last_chunk_ago_s == null) ? null : Number(m.last_chunk_ago_s);
        S.total = Number(m.elapsed_s) || S.total;
        updateLabels();
      }).catch(function(){});
    }, 4000);
  }

  function open(sid, opts){
    opts = opts || {};
    ensure();
    clearTimeout(S.retry);
    clearInterval(S.poll);
    S.sid = sid;
    S.label = opts.label || sid;
    S.mode = "tail";
    S.open = true;
    S.windowSec = Number(opts.windowSec) || 300;
    S.t = 0;
    S.message = "";
    S.start = 0; S.end = 0; S.total = 0;
    S.paused = false; S.running = false; S.live = true; S.chunkAgo = null;
    bar.hidden = false;
    document.body.classList.add("la-open");
    syncWins();
    loadWindow({ seconds:S.windowSec, autoplay:true });
    startPoll();
  }

  function openAt(sid, t, opts){
    opts = opts || {};
    ensure();
    clearTimeout(S.retry);
    clearInterval(S.poll);
    S.sid = sid;
    S.label = opts.label || sid;
    S.mode = "at";
    S.open = true;
    S.t = Math.max(0, Number(t) || 0);
    S.message = "";
    S.start = 0; S.end = 0; S.total = 0;
    S.paused = false; S.running = false; S.live = true; S.chunkAgo = null;
    bar.hidden = false;
    document.body.classList.add("la-open");
    syncWins();
    loadWindow({ start:Math.max(0, S.t - 2), end:S.t + 45, autoplay:true });
    startPoll();
  }

  function close(){
    S.open = false;
    clearTimeout(S.retry);
    clearInterval(S.poll);
    if (audio){
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    if (bar) bar.hidden = true;
    document.body.classList.remove("la-open");
  }

  window.LiveAudio = { open:open, openAt:openAt, close:close };
})();
