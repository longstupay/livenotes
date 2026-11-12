// 采集与上行。这一层的职责只有三件：拿到音频、切成块、送到本地服务。
// 所有"送不到"的情况都必须可见 —— 用户最怕的不是报错，是以为在录其实没录。

const caps = new Map();   // tabId -> 在抓的
const dead = [];          // 已断的（留在面板上，让人看得见）
const STALE_MS = 6000;    // 超过 6 秒没有音频块就判定断流
const MAX_INFLIGHT = 8;   // 背压：同时最多 8 个在途请求，超了丢块，别把内存吃爆

const cfg = {
  send: true,                              // 默认就送：勾选式"要不要录"迟早会忘
  base: "http://127.0.0.1:8765",
  online: false,
  checkedAt: 0,
  closeOnStop: true                        // 停止时连标签页一起关掉（可在面板关掉）
};

// 注意：offscreen 文档只有 chrome.runtime，没有 chrome.storage、也没有 chrome.tabs。
// 持久化和标签页操作一律交给 service worker 代做，这里只保留内存状态。
chrome.runtime.sendMessage({ type: "offscreen-ready" }).catch(() => {});

// ---------------------------------------------------------------- 连接状态

async function probe() {
  try {
    const r = await fetch(cfg.base + "/api/health", { cache: "no-store" });
    cfg.online = r.ok;
  } catch (e) {
    cfg.online = false;
  }
  cfg.checkedAt = Date.now();
}
setInterval(probe, 5000);
probe();

// ---------------------------------------------------------------- 会话

function sessionTitle(cap) {
  const base = "监听中心 · " + (cap.title || cap.tabId);
  return cap.reconnects ? base + "（续" + cap.reconnects + "）" : base;
}

// 服务重启、会话被清理、用户勾选推送 —— 这三件事都会让"当前没有会话"，
// 所以建会话必须是可以随时补的，而不是只在开始捕获时建一次。
async function ensureSession(cap) {
  if (cap.creating || !cfg.send) return;
  const now = Date.now();
  if (now - (cap.lastTry || 0) < 3000) return;   // 失败后别每块都重试
  cap.creating = true;
  cap.lastTry = now;
  try {
    const r = await fetch(cfg.base + "/api/live/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: sessionTitle(cap) })
    });
    if (!r.ok) {
      cap.sendErr = "建会话失败 HTTP " + r.status;
      cfg.online = false;
      return;
    }
    const j = await r.json();
    if (j.session_id) {
      cap.sid = j.session_id;
      cap.sendErr = "";
      cap.reconnects = cap.reconnects || 0;
      // 告诉 service worker 这一路用的是哪个会话：offscreen 被 Chrome 回收后，
      // 重建时才能续接同一场，而不是把缺口拆成两场。
      chrome.runtime.sendMessage({
        type: "capture-session", tabId: cap.tabId, sid: cap.sid
      }).catch(() => {});
    } else {
      cap.sendErr = "建会话没返回 id";
    }
  } catch (e) {
    cap.sendErr = "连不上本地服务（python run.py 起了吗？）";
    cfg.online = false;
  } finally {
    cap.creating = false;
  }
}

// ---------------------------------------------------------------- 上行

function sendChunk(cap, pcm) {
  if (!cfg.send) return;
  if (!cap.sid) { ensureSession(cap); return; }   // 还没有会话：补建，这块丢掉
  if (cap.inflight >= MAX_INFLIGHT) { cap.dropped += 1; return; }
  cap.inflight += 1;
  fetch(cfg.base + "/api/live/chunk", {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream", "X-Live-Id": cap.sid },
    body: pcm
  }).then((r) => {
    if (r.status === 404) {
      // 服务重启过 / 会话被清掉了：这个 id 已失效，换一个新会话接着录
      cap.sid = "";
      cap.reconnects = (cap.reconnects || 0) + 1;
      ensureSession(cap);
    } else if (!r.ok) {
      cap.sendErr = "发送失败 HTTP " + r.status;
    } else {
      cap.sendErr = "";
      cfg.online = true;
    }
  }).catch(() => {
    cap.sendErr = "连不上本地服务";
    cfg.online = false;
    cap.dropped += 1;
  }).finally(() => { cap.inflight -= 1; });
}

// ---------------------------------------------------------------- 标签页静音

// tabCapture 一旦拿到流，声音就从扬声器被摘走；流一关，声音会"还回去"。
// 所以停止采集前必须先把标签页静音，否则 5 路一起停就是 5 个直播间同时外放。
async function setMuted(tabId, muted) {
  try {
    const r = await chrome.runtime.sendMessage({ type: "mute", tabId, muted: !!muted });
    return !!(r && r.ok);
  } catch (e) {
    return false;   // 标签页已经关了，静音无从谈起
  }
}

async function closeTab(tabId) {
  try {
    const r = await chrome.runtime.sendMessage({ type: "close-tab", tabId });
    return !!(r && r.ok);
  } catch (e) {
    return false;
  }
}

// ---------------------------------------------------------------- 采集

async function start({ streamId, tabId, title, url, session }) {
  if (caps.has(tabId)) return { error: "这个标签页已经在监听中" };
  const idx = dead.findIndex((d) => d.tabId === tabId);
  if (idx >= 0) dead.splice(idx, 1);

  // 先取消静音再取流：上一次停止会自动静音该标签页，带着静音去收音频不稳妥。
  // 取流成功后 Chrome 自己会把声音从扬声器摘走，所以不会有一瞬间的外放。
  await setMuted(tabId, false);

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
      video: false
    });
  } catch (e) {
    return { error: "取流失败：" + (e && e.message) };
  }

  const ctx = new AudioContext({ sampleRate: 16000 });
  await ctx.audioWorklet.addModule(chrome.runtime.getURL("pcm-worklet.js"));
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "pcm16", {
    numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]
  });
  const gain = ctx.createGain();
  gain.gain.value = 0;                 // 不接回扬声器：本地保持安静
  src.connect(node);
  node.connect(gain);
  gain.connect(ctx.destination);

  const cap = {
    tabId, title, url, stream, ctx, node,
    rms: 0, peak: 0, chunks: 0, seconds: 0,
    dropped: 0, inflight: 0, reconnects: 0,
    startedAt: Date.now(), lastChunkAt: Date.now(),
    sid: session || "", sendErr: "", creating: false, lastTry: 0,
    ended: false, endedAt: 0, reason: ""
  };
  caps.set(tabId, cap);

  // 被 service worker 重建时带着原 session 回来：服务端还在跑就续接；
  // 404 会走 sendChunk 里的重建分支，不会卡死。
  if (cfg.send && !cap.sid) ensureSession(cap);      // 不等它，先把音频抓起来

  node.port.onmessage = (e) => {
    const { pcm, rms } = e.data;
    cap.rms = rms;
    cap.peak = Math.max(cap.peak, rms);
    cap.chunks += 1;
    cap.seconds += 4096 / 16000;
    cap.lastChunkAt = Date.now();
    sendChunk(cap, pcm);
  };

  stream.getAudioTracks().forEach((t) => {
    t.onended = () => markEnded(cap, "标签页音频流结束（下播、刷新或页面跳转）");
  });
  return { ok: true, tabId };
}

function markEnded(cap, reason, silent) {
  if (cap.ended) return;
  cap.ended = true;
  cap.endedAt = Date.now();
  cap.reason = reason || "";
  cap.rms = 0;
  // 顺序很重要：先静音，再断流。反过来的话，从断流到静音之间会漏出一段声音。
  setMuted(cap.tabId, true).then(() => {
    try { cap.node && cap.node.disconnect(); } catch (e) {}
    try { cap.stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { cap.ctx.close(); } catch (e) {}
    // 关闭标签页放在最后：先静音、先断流、先让服务端收尾，再关。
    if (cfg.closeOnStop) {
      closeTab(cap.tabId).then((ok) => { if (ok) cap.closed = true; });
    }
  });
  caps.delete(cap.tabId);
  dead.push(cap);
  if (!silent) {
    chrome.runtime.sendMessage({
      type: "alarm", tabId: cap.tabId, title: cap.title, reason
    }).catch(() => {});
  }
  if (cfg.send && cap.sid) {
    fetch(cfg.base + "/api/live/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: cap.sid })
    }).catch(() => {});
  }
}

function stop(tabId) {
  const c = caps.get(tabId);
  if (!c) return false;
  markEnded(c, "手动停止", true);
  return true;
}

// 看门狗：媒体流有时不触发 ended（页面还在但没声音了），只能靠"多久没来块"判断
setInterval(() => {
  const now = Date.now();
  for (const c of caps.values()) {
    if (now - c.lastChunkAt > STALE_MS) {
      markEnded(c, "超过 " + Math.round(STALE_MS / 1000) + " 秒没有音频（静音、下播或断网）");
    }
  }
}, 2000);

function brief(c) {
  return {
    tabId: c.tabId, title: c.title, url: c.url,
    rms: c.rms, peak: c.peak, chunks: c.chunks,
    seconds: Math.round(c.seconds), startedAt: c.startedAt,
    lastChunkAt: c.lastChunkAt, dropped: c.dropped, reconnects: c.reconnects,
    sid: c.sid, sendErr: c.sendErr,
    sampleRate: c.ctx ? c.ctx.sampleRate : 0,
    state: c.ended ? "ended" : (c.ctx ? c.ctx.state : "?"),
    ended: !!c.ended, endedAt: c.endedAt, reason: c.reason,
    closed: !!c.closed
  };
}

function snapshot() {
  return {
    caps: [...caps.values()].map(brief),
    dead: dead.map(brief),
    send: cfg.send, base: cfg.base, online: cfg.online,
    closeOnStop: cfg.closeOnStop, now: Date.now()
  };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  try {
    if (msg.type === "ping") { sendResponse({ ok: true }); return true; }
    if (msg.type === "start") { start(msg).then(sendResponse); return true; }
    if (msg.type === "stats") { sendResponse(snapshot()); return true; }
    if (msg.type === "stop") { sendResponse({ ok: stop(msg.tabId) }); return true; }
    if (msg.type === "stopAll") {
      [...caps.keys()].forEach((id) => stop(id));
      sendResponse({ ok: true });
      return true;
    }
    if (msg.type === "clearDead") { dead.length = 0; sendResponse({ ok: true }); return true; }
    if (msg.type === "unmute") {
      setMuted(msg.tabId, false).then((ok) => sendResponse({ ok }));
      return true;
    }
    if (msg.type === "config") {
      if (typeof msg.send === "boolean") cfg.send = msg.send;
      if (msg.base) cfg.base = msg.base;
      if (typeof msg.closeOnStop === "boolean") cfg.closeOnStop = msg.closeOnStop;
      // 关键：勾上之后，已经在抓的那几路要立刻补建会话，否则它们永远不入库
      if (cfg.send) {
        probe();
        for (const c of caps.values()) if (!c.sid) ensureSession(c);
      }
      sendResponse({ ok: true, send: cfg.send, base: cfg.base, online: cfg.online });
      return true;
    }
    if (msg.type === "retry") {
      probe();
      for (const c of caps.values()) {
        c.lastTry = 0;
        if (!c.sid) ensureSession(c);
      }
      sendResponse({ ok: true });
      return true;
    }
  } catch (e) {
    sendResponse({ error: String(e) });
    return true;
  }
});
