// service worker：唯一有完整扩展 API 的地方。
// offscreen 文档只拿得到 chrome.runtime，所以「静音标签页」「关标签页」「存配置」
// 都必须由这里代做 —— 之前直接在 offscreen 里调 chrome.storage 就是这么崩的。

const DEFAULT_CFG = { send: true, base: "http://127.0.0.1:8765", closeOnStop: true };

// 共享模式的页面内采集不走 offscreen，需要在这里拿到当前配置。
// 面板改配置时会同步更新；模块加载时先读一次持久化值。
let pageCfg = Object.assign({}, DEFAULT_CFG);
loadCfg().then((v) => { pageCfg = v; }).catch(() => {});

async function loadCfg() {
  const v = await chrome.storage.local.get(["send", "base", "closeOnStop"]);
  return {
    send: v.send !== false,
    base: v.base || DEFAULT_CFG.base,
    closeOnStop: v.closeOnStop !== false
  };
}

// 把配置推给 offscreen（它自己不碰 storage）
async function pushCfg() {
  try {
    if (!(await chrome.offscreen.hasDocument())) return;
    const cfg = await loadCfg();
    await chrome.runtime.sendMessage({ type: "config", ...cfg });
  } catch (e) {
    // offscreen 还没加载完就发，会收不到；它加载完会发 offscreen-ready 再要一次
  }
}

async function ensureOffscreen() {
  if (!(await chrome.offscreen.hasDocument())) {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["USER_MEDIA"],
      justification: "捕获标签页音频"
    });
  }
  // 刚创建的 offscreen 文档脚本还没跑起来，紧接着发 start 会 "Receiving end does not exist"。
  // 所以握一次手再往下走。
  for (let i = 0; i < 20; i++) {
    try {
      await chrome.runtime.sendMessage({ type: "ping" });
      return;
    } catch (e) {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
}

// ---- 采集会话的记忆：offscreen 被 Chrome 回收后，至少知道哪些标签页该接回来 ----
const ACTIVE_KEY = "activeTabs";

async function activeTabs() {
  try {
    const v = await chrome.storage.session.get(ACTIVE_KEY);
    return v[ACTIVE_KEY] || [];
  } catch (e) {
    return [];
  }
}

async function setActiveTabs(list) {
  try {
    await chrome.storage.session.set({ [ACTIVE_KEY]: list.slice(-20) });
  } catch (e) {}
}

async function addActive(tab, sid) {
  if (!tab || tab.id == null) return;
  const list = await activeTabs();
  const i = list.findIndex((x) => x.tabId === tab.id);
  const item = { tabId: tab.id, title: tab.title || "", url: tab.url || "" };
  if (i >= 0) {
    // capture-session 可能比这次写入先到：别把已经记下的 sid 覆盖成空。
    item.sid = sid || list[i].sid || "";
    list[i] = Object.assign(list[i], item);
  } else {
    item.sid = sid || "";
    list.push(item);
  }
  await setActiveTabs(list);
}

async function removeActive(tabId) {
  const list = await activeTabs();
  await setActiveTabs(list.filter((x) => x.tabId !== tabId));
}

async function setActiveSession(tabId, sid) {
  const list = await activeTabs();
  const i = list.findIndex((x) => x.tabId === tabId);
  if (i >= 0) {
    list[i].sid = sid || "";
    await setActiveTabs(list);
  }
}

async function restoreActive() {
  const tabs = await activeTabs();
  for (const t of tabs) {
    try {
      const tab = await chrome.tabs.get(t.tabId);
      if (!tab || tab.id == null) continue;
      const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
      await chrome.runtime.sendMessage({
        type: "start", streamId, tabId: tab.id,
        title: tab.title || t.title || ("标签页 " + tab.id),
        url: tab.url || t.url || "", session: t.sid || ""
      });
    } catch (e) {
      // 标签页已经关了 / 取流失败：跳过这一路，别拖住其它房间
    }
  }
}

// ---- 共享浏览器模式：页面内采集 ----
// 代理自开的标签页没有 activeTab，chrome.tabCapture 会直接拒绝
// （"Extension has not been invoked for the current page"）。
// 这里改用页面自己的能力：对主 <video> 调 captureStream()，接进 AudioContext，
// 降采样到 16k，按 256ms 一块送回 service worker，再由这里 POST 给本地服务。
//
// 实测：标签静音不影响 captureStream（静音前后 RMS 都是 0.1 左右），
// 所以共享浏览器按标签静音即可，不需要系统级 pycaw。
// 注意扩展消息是 JSON 序列化，ArrayBuffer 会变成 {}，PCM 必须走 base64。
const pageCaps = new Map();        // tabId -> cap；worker 被回收后由第一块音频重建
const pageStopped = new Map();     // tabId -> 停止时刻；挡住停止后还在路上的音频块
const PAGE_MAX_INFLIGHT = 8;
const PAGE_STOP_GRACE_MS = 10000;

function pageSessionTitle(cap) {
  return "监听中心 · " + (cap.title || cap.tabId);
}

function b64ToBytes(s) {
  const bin = atob(s || "");
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function pageEnsureSession(cap) {
  if (cap.creating || !pageCfg.send) return "";
  const now = Date.now();
  if (now - (cap.lastTry || 0) < 3000) return "";   // 失败后别每块都重试
  cap.creating = true;
  cap.lastTry = now;
  try {
    const r = await fetch(pageCfg.base + "/api/live/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: pageSessionTitle(cap) })
    });
    if (!r.ok) {
      cap.sendErr = "建会话失败 HTTP " + r.status;
      return "";
    }
    const j = await r.json();
    if (j.session_id) {
      cap.sid = j.session_id;
      cap.sendErr = "";
      return cap.sid;
    }
    cap.sendErr = "建会话没返回 id";
  } catch (e) {
    cap.sendErr = "连不上本地服务（python run.py 起了吗？）";
  } finally {
    cap.creating = false;
  }
  return "";
}

// 页面里发回来的一块 PCM。sender.tab.id 就是房间标签页，不需要额外映射。
function pagePcm(msg, sender) {
  const tabId = sender && sender.tab && sender.tab.id;
  if (tabId == null || !msg || !msg.pcm) return { ok: false, error: "没有来源标签页" };
  // 停止/关标签页时页面里可能还有几块在途。别让它们把 cap 又建回来，
  // 否则会对着已经收尾的 session 继续推，甚至 404 后重建出一个没人管的会话。
  if (Date.now() - (pageStopped.get(tabId) || 0) < PAGE_STOP_GRACE_MS) {
    return { ok: false, error: "标签页已停止采集" };
  }
  pageStopped.delete(tabId);
  let cap = pageCaps.get(tabId);
  if (!cap) {
    // service worker 被回收过：凭第一块音频把这一路接回来，session 由页面带着，
    // 所以不会把一场拆成两场。
    cap = {
      tabId, sid: msg.sid || "", title: msg.title || "",
      url: (sender.tab && sender.tab.url) || "",
      chunks: 0, seconds: 0, rms: 0, lastChunkAt: Date.now(),
      inflight: 0, dropped: 0, sendErr: "", creating: false, lastTry: 0,
      startedAt: Date.now()
    };
    pageCaps.set(tabId, cap);
  }
  if (msg.sid) cap.sid = msg.sid;
  if (msg.title && !cap.title) cap.title = msg.title;
  cap.chunks += 1;
  cap.seconds += 4096 / 16000;
  cap.lastChunkAt = Date.now();
  cap.rms = Number(msg.rms) || 0;

  if (!pageCfg.send) return { ok: true, sid: cap.sid, send: false };
  if (!cap.sid) {
    pageEnsureSession(cap);              // 不阻塞音频：建好后下一块就会用上
    return { ok: true, sid: "", send: true };
  }
  if (cap.inflight >= PAGE_MAX_INFLIGHT) {
    cap.dropped += 1;
    return { ok: true, sid: cap.sid, send: true };
  }
  let body;
  try {
    body = b64ToBytes(msg.pcm);
  } catch (e) {
    cap.sendErr = "音频解码失败";
    return { ok: false, sid: cap.sid, send: true };
  }
  cap.inflight += 1;
  fetch(pageCfg.base + "/api/live/chunk", {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream", "X-Live-Id": cap.sid },
    body
  }).then((r) => {
    if (r.status === 404) {
      // 服务重启 / 会话被清掉：换一个新会话接着录，缺口留在一场里。
      cap.sid = "";
      cap.sendErr = "会话已失效，正在重建";
      pageEnsureSession(cap);
    } else if (!r.ok) {
      cap.sendErr = "发送失败 HTTP " + r.status;
    } else {
      cap.sendErr = "";
    }
  }).catch(() => {
    cap.sendErr = "连不上本地服务";
    cap.dropped += 1;
  }).finally(() => { cap.inflight -= 1; });
  return { ok: true, sid: cap.sid, send: true };
}

async function pageStart(msg) {
  const tabId = msg.tabId;
  if (tabId == null) return { error: "缺少 tabId" };
  pageStopped.delete(tabId);
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    return { error: "标签页不存在" };
  }
  pageCfg = await loadCfg();
  // 同一标签页重复启动（断流重建）：先让旧脚本收手，但保留同一个 session。
  await pageStop({ tabId, keepSession: true });
  try { await chrome.tabs.update(tabId, { muted: true }); } catch (e) {}

  let sid = msg.session || "";
  const cap = {
    tabId, sid, title: msg.title || tab.title || "", url: tab.url || "",
    chunks: 0, seconds: 0, rms: 0, lastChunkAt: Date.now(),
    inflight: 0, dropped: 0, sendErr: "", creating: false, lastTry: 0,
    startedAt: Date.now()
  };
  pageCaps.set(tabId, cap);
  if (!sid && pageCfg.send) sid = await pageEnsureSession(cap);

  let res;
  try {
    res = await chrome.scripting.executeScript({
      target: { tabId },
      func: pageCaptureMain,
      args: [sid, cap.title]
    });
  } catch (e) {
    pageCaps.delete(tabId);
    return { error: "注入采集脚本失败：" + ((e && e.message) || e) };
  }
  const out = res && res[0] && res[0].result;
  if (!out || !out.ok) {
    pageCaps.delete(tabId);
    return { error: (out && out.error) || "页面里没有可采集的音视频" };
  }
  return { ok: true, tabId, sid, title: cap.title, url: cap.url, video: out.video };
}

async function pageStop(msg) {
  const tabId = msg.tabId;
  const cap = pageCaps.get(tabId);
  pageCaps.delete(tabId);
  if (!msg.keepSession) pageStopped.set(tabId, Date.now());
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        try { globalThis.__lvnPageCap && globalThis.__lvnPageCap.stop(); } catch (e) {}
      }
    });
  } catch (e) {
    // 标签页已经没了 / 正在跳转：脚本跟着文档一起没了，不用管
  }
  if (!msg.keepSession && cap && cap.sid) {
    fetch(pageCfg.base + "/api/live/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: cap.sid })
    }).catch(() => {});
  }
  return { ok: true, sid: cap ? (cap.sid || "") : "" };
}

// 页面里跑的一次性探针：和代理那边的 RECHECK_JS 保持同一份判断口径。
function pageProbeMain() {
  const ok = (x) => !!x && !x.paused && !x.ended && x.readyState >= 2;
  const vs = Array.from(document.querySelectorAll("video"));
  const as = Array.from(document.querySelectorAll("audio"));
  let main = null, best = 0;
  for (const v of vs) {
    let r = { width: 0, height: 0 };
    try { r = v.getBoundingClientRect(); } catch (e) {}
    const a = (r.width | 0) * (r.height | 0);
    if (a > best) { best = a; main = v; }
  }
  return {
    playing: vs.filter(ok).length + as.filter(ok).length,
    main: main ? {
      area: best, playing: ok(main), paused: !!main.paused, ended: !!main.ended,
      dur: isFinite(main.duration) ? Math.round(main.duration) : -1
    } : null,
    title: document.title || "",
    url: location.href || "",
    text: (document.body ? document.body.innerText : "").slice(0, 2500)
  };
}

async function pageProbe(msg) {
  const tabId = msg.tabId;
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    return { gone: true };
  }
  let probe = null, error = "";
  try {
    const res = await chrome.scripting.executeScript({ target: { tabId }, func: pageProbeMain });
    probe = (res && res[0] && res[0].result) || null;
  } catch (e) {
    error = String((e && e.message) || e);
  }
  return {
    gone: false, tabId, title: tab.title || "", url: tab.url || "",
    status: tab.status, muted: !!(tab.mutedInfo && tab.mutedInfo.muted),
    probe, error
  };
}

async function pageTab(msg) {
  try {
    const t = await chrome.tabs.get(msg.tabId);
    return {
      ok: true, tabId: t.id, title: t.title || "", url: t.url || "",
      status: t.status, muted: !!(t.mutedInfo && t.mutedInfo.muted)
    };
  } catch (e) {
    return { ok: false, gone: true };
  }
}

async function pageTabs() {
  const tabs = await chrome.tabs.query({});
  return {
    tabs: tabs.map((t) => ({
      tabId: t.id, title: t.title || "", url: t.url || "",
      status: t.status, muted: !!(t.mutedInfo && t.mutedInfo.muted)
    }))
  };
}

function pageStats() {
  return {
    caps: [...pageCaps.values()].map((c) => ({
      tabId: c.tabId, sid: c.sid, title: c.title, url: c.url,
      chunks: c.chunks, seconds: Math.round(c.seconds), rms: c.rms,
      lastChunkAt: c.lastChunkAt, sendErr: c.sendErr, dropped: c.dropped,
      startedAt: c.startedAt
    }))
  };
}

async function pageOpen(msg) {
  // 必须让标签页先在前台把页面渲染出来：抖音在后台标签页里只放视频，
  // 不渲染标题/房间名，代理会误判成"页面没加载出来"。开好后由代理切回面板页。
  const tab = await chrome.tabs.create({ url: msg.url, active: msg.active !== false });
  // 页面一开始播就静音：取流在页面里做，标签静音不会影响它。
  try { await chrome.tabs.update(tab.id, { muted: true }); } catch (e) {}
  // create({active:true}) 在窗口没聚焦时不一定真的切过去，再显式切一次。
  try { await chrome.tabs.update(tab.id, { active: true }); } catch (e) {}
  return { ok: true, tabId: tab.id, url: tab.url || msg.url };
}

async function pageActivate(msg) {
  try {
    await chrome.tabs.update(msg.tabId, { active: true });
    return { ok: true };
  } catch (e) {
    return { ok: false, gone: true };
  }
}

async function pageFocusPanel() {
  try {
    const tabs = await chrome.tabs.query({ url: chrome.runtime.getURL("panel.html") });
    if (tabs.length) {
      await chrome.tabs.update(tabs[0].id, { active: true });
      return { ok: true, tabId: tabs[0].id };
    }
  } catch (e) {}
  return { ok: false };
}

async function pageClose(msg) {
  try {
    await chrome.tabs.remove(msg.tabId);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

// 注入到直播间页面里的采集器。必须是自包含函数（chrome.scripting 会把它
// 序列化后注入 ISOLATED world），不能引用外面任何变量。
function pageCaptureMain(sid, title) {
  const KEY = "__lvnPageCap";
  try { if (globalThis[KEY] && globalThis[KEY].stop) globalThis[KEY].stop(); } catch (e) {}

  const state = {
    stopped: false, sid: sid || "", title: title || "",
    send: true, ctx: null, src: null, node: null, gain: null,
    stream: null, video: null, inflight: 0, chunks: 0, ticks: 0, err: ""
  };

  function pickVideo() {
    const vs = Array.from(document.querySelectorAll("video"));
    let best = null, area = 0;
    for (const v of vs) {
      let r = { width: 0, height: 0 };
      try { r = v.getBoundingClientRect(); } catch (e) {}
      const a = (r.width | 0) * (r.height | 0);
      if (a > area) { area = a; best = v; }
    }
    return best;
  }

  function b64(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s);
  }

  function send(pcm, rms) {
    // sid 为空也照发：service worker 收到后会建会话并把新 sid 回给页面，
    // 这样服务短暂重启/建会话失败时不用等代理重新注入。
    if (state.stopped || !state.send) return;
    if (state.inflight >= 8) return;
    state.inflight += 1;
    try {
      chrome.runtime.sendMessage({
        type: "page-pcm", sid: state.sid, title: state.title, pcm: b64(pcm), rms
      }, (r) => {
        void chrome.runtime.lastError;     // 扩展重载/worker 不在时别刷控制台
        state.inflight -= 1;
        if (r && typeof r.sid === "string") state.sid = r.sid;
        if (r && typeof r.send === "boolean") state.send = r.send;
      });
    } catch (e) {
      state.inflight -= 1;
      state.err = String(e);
    }
  }

  function teardown() {
    try { if (state.node) state.node.disconnect(); } catch (e) {}
    try { if (state.src) state.src.disconnect(); } catch (e) {}
    try { if (state.gain) state.gain.disconnect(); } catch (e) {}
    try { if (state.stream) state.stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { if (state.ctx) state.ctx.close(); } catch (e) {}
    state.stream = null; state.node = null; state.src = null;
    state.gain = null; state.ctx = null; state.video = null;
  }

  function bind() {
    if (state.stopped) return;
    const v = pickVideo();
    if (!v) { state.err = "页面里没有 video"; return; }
    const live = state.stream &&
      state.stream.getAudioTracks().some((t) => t.readyState === "live");
    if (state.video === v && live) return;
    teardown();
    state.video = v;
    try {
      // 页面自己把 video 静音时 captureStream 也会是静音；标签已经在浏览器层
      // 静音，这里放开元素静音不会外放。
      if (v.muted) v.muted = false;
      state.stream = v.captureStream();
      if (!state.stream.getAudioTracks().length) {
        state.err = "video 没有音频轨";
        return;
      }
      state.ctx = new AudioContext({ sampleRate: 16000 });
      state.src = state.ctx.createMediaStreamSource(state.stream);
      state.node = state.ctx.createScriptProcessor(4096, 1, 1);
      state.gain = state.ctx.createGain();
      state.gain.gain.value = 0;          // 不接回扬声器
      state.node.onaudioprocess = (e) => {
        const d = e.inputBuffer.getChannelData(0);
        const out = new Int16Array(d.length);
        let sum = 0, peak = 0;
        for (let i = 0; i < d.length; i++) {
          let x = d[i];
          if (x > 1) x = 1; else if (x < -1) x = -1;
          out[i] = x < 0 ? x * 0x8000 : x * 0x7fff;
          sum += x * x;
          const ax = x < 0 ? -x : x;
          if (ax > peak) peak = ax;
        }
        state.chunks += 1;
        // 音频回调不受后台标签页定时器节流影响，用它做"暂停就拉回播放"的心跳。
        if (++state.ticks % 4 === 0) nudge();
        send(out.buffer, Math.sqrt(sum / Math.max(1, d.length)));
      };
      state.src.connect(state.node);
      state.node.connect(state.gain);
      state.gain.connect(state.ctx.destination);
      state.err = "";
    } catch (e) {
      state.err = "取流失败：" + ((e && e.message) || e);
    }
  }

  function stop() {
    state.stopped = true;
    clearInterval(state.timer);
    try {
      if (state.onMsg) chrome.runtime.onMessage.removeListener(state.onMsg);
    } catch (e) {}
    try {
      if (state.onVis) document.removeEventListener("visibilitychange", state.onVis, true);
    } catch (e) {}
    teardown();
    try { delete globalThis[KEY]; } catch (e) {}
  }

  function nudge() {
    // 抖音在标签页被切到后台/窗口被盖住时会暂停 <video>，而且切回来不一定
    // 自动续播。标签已经静音，这里直接 play() 不会外放。
    try {
      const v = pickVideo();
      if (v && v.paused && !v.ended) v.play().catch(() => {});
    } catch (e) {}
  }

  state.stop = stop;
  state.nudge = nudge;
  globalThis[KEY] = state;

  try {
    state.onMsg = (m) => {
      if (m && m.type === "page-config") state.send = m.send !== false;
    };
    chrome.runtime.onMessage.addListener(state.onMsg);
  } catch (e) {}

  try {
    state.onVis = () => setTimeout(nudge, 500);
    document.addEventListener("visibilitychange", state.onVis, true);
  } catch (e) {}

  bind();
  nudge();
  // 切清晰度/页面重渲染会换掉 <video>：定时确认，轨断了就重新绑。
  state.timer = setInterval(() => {
    if (state.stopped) return;
    try {
      const v = pickVideo();
      const live = state.stream &&
        state.stream.getAudioTracks().some((t) => t.readyState === "live");
      if (!v || v !== state.video || !live) bind();
      nudge();
    } catch (e) {
      state.err = String(e);
    }
  }, 2000);

  return { ok: !!state.video, video: !!state.video, sid: state.sid, err: state.err };
}


// 记住"常听的直播间"：下次开机一键全部打开，省得一个个找。
async function rememberRoom(tab) {
  if (!tab || !tab.url || !/^https?:/.test(tab.url)) return;
  const { rooms = [] } = await chrome.storage.local.get("rooms");
  if (rooms.some((r) => r.url === tab.url)) return;
  rooms.push({ url: tab.url, title: tab.title || tab.url });
  await chrome.storage.local.set({ rooms: rooms.slice(-20) });
}

async function openPanel() {
  const url = chrome.runtime.getURL("panel.html");
  const tabs = await chrome.tabs.query({ url });
  if (tabs.length) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    return;
  }
  await chrome.tabs.create({ url, index: 0 });
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab || tab.id == null) return;
  try {
    await ensureOffscreen();
    // 先记住这一路：offscreen 建好会话后会把 sid 补进来；失败再撤掉。
    // 反过来（成功后才记）会撞上 capture-session 先到的竞态，丢掉续接用的 sid。
    await addActive(tab);
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    const res = await chrome.runtime.sendMessage({
      type: "start", streamId, tabId: tab.id,
      title: tab.title || ("标签页 " + tab.id), url: tab.url || ""
    });
    await rememberRoom(tab);
    await openPanel();
    if (res && res.error) {
      console.warn("[livehub] 启动失败:", res.error);
      await removeActive(tab.id);
      chrome.action.setBadgeText({ text: "!" });
      chrome.action.setBadgeBackgroundColor({ color: "#b3261e" });
    } else {
      chrome.action.setBadgeText({ text: "" });
    }
  } catch (e) {
    console.error("[livehub] capture 失败", e);
    await removeActive(tab.id);
    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setBadgeBackgroundColor({ color: "#b3261e" });
  }
});

// 标签页关闭时释放它的流
chrome.tabs.onRemoved.addListener((tabId) => {
  removeActive(tabId);
  chrome.runtime.sendMessage({ type: "stop", tabId }).catch(() => {});
  // 共享模式：标签页是用户/代理关掉的，这一场也要正常收尾，别等 120 秒看门狗。
  const cap = pageCaps.get(tabId);
  if (cap) {
    pageCaps.delete(tabId);
    pageStopped.set(tabId, Date.now());
    if (cap.sid) {
      fetch(pageCfg.base + "/api/live/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: cap.sid })
      }).catch(() => {});
    }
  }
});

// 整页刷新/跳转会把注入的采集脚本一起冲掉。标签页重新加载完成后，
// 用原来的 session 重新注入，缺口留在一场里。
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (!info || info.status !== "complete") return;
  const cap = pageCaps.get(tabId);
  if (!cap) return;
  if (Date.now() - (cap.lastChunkAt || 0) < 5000) return;   // 注入脚本还活着
  pageStart({ tabId, session: cap.sid || "", title: cap.title || "" }).catch(() => {});
});

// ---- 断流告警 ----
const ICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAWklEQVR42u3NMQEAAAgDoC251a3gHwqg2XcCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPgaZQABAV9r9QAAAABJRU5ErkJggg==";

async function notify(title, message) {
  try {
    await chrome.notifications.create({
      type: "basic", iconUrl: ICON, title, message, priority: 1
    });
  } catch (e) {
    console.warn("[livehub] 通知失败", e);
  }
}

// ---- offscreen 守护：Chrome 内存紧时会回收扩展的 offscreen 文档 ----
// 被回收时没有任何回调，采集会静默停掉；用 alarm 每分钟 ping 一次，死了就重建，
// 再把之前记住的标签页按原 session 接回来。
const WATCH_ALARM = "offscreen-watchdog";
function ensureWatchAlarm() {
  // service worker 每次被唤醒都会跑到这里；先查再建，别把 1 分钟的周期反复重置。
  chrome.alarms.get(WATCH_ALARM).then((a) => {
    if (!a) chrome.alarms.create(WATCH_ALARM, { periodInMinutes: 1 });
  }).catch(() => {
    try { chrome.alarms.create(WATCH_ALARM, { periodInMinutes: 1 }); } catch (e) {}
  });
}
chrome.runtime.onInstalled.addListener(ensureWatchAlarm);
chrome.runtime.onStartup.addListener(ensureWatchAlarm);
ensureWatchAlarm();

chrome.alarms.onAlarm.addListener(async (a) => {
  if (!a || a.name !== WATCH_ALARM) return;
  try {
    // 共享模式用页面内采集，不需要 offscreen。没有 tabCapture 会话时连
    // offscreen 文档都不该留着——它自己就是几十 MB 的渲染进程。
    const active = await activeTabs();
    if (!active.length) {
      try {
        if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument();
      } catch (e) {}
      return;
    }
    if (!(await chrome.offscreen.hasDocument())) {
      await ensureOffscreen();
      return;                       // offscreen-ready 里会 restoreActive()
    }
    await chrome.runtime.sendMessage({ type: "ping" });
  } catch (e) {
    // 文档还在但已经不应答：关掉重建。被回收的采集会话由 restoreActive 续上。
    try { await chrome.offscreen.closeDocument(); } catch (x) {}
    await ensureOffscreen();
    notify("监听组件已重启", "采集组件被 Chrome 回收，正在尝试恢复之前的标签页。");
  }
});

// ---- 给 offscreen 和面板提供的服务 ----
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) return;

  if (msg.type === "alarm") {
    removeActive(msg.tabId);
    notify("监听中断：" + (msg.title || ("标签页 " + msg.tabId)),
           (msg.reason || "音频流已停止") + "\n请切到该标签页，点一次扩展图标重连。");
    chrome.action.setBadgeText({ text: "断" });
    chrome.action.setBadgeBackgroundColor({ color: "#b3261e" });
    return;
  }

  if (msg.type === "offscreen-ready") {
    pushCfg();                 // offscreen 加载完了，把持久化的配置给它
    restoreActive();           // 被 Chrome 回收过的话，把之前的标签页接回来
    return;
  }

  if (msg.type === "capture-session") {
    // offscreen 建好/重建了会话：记住 session_id，重建时才能续接同一场。
    setActiveSession(msg.tabId, msg.sid);
    return;
  }

  if (msg.type === "stop") {
    removeActive(msg.tabId);
    return;
  }

  if (msg.type === "stopAll") {
    setActiveTabs([]);
    return;
  }

  if (msg.type === "config") {
    // 面板改配置时：存下来，并转发给 offscreen（面板的消息它也直接收到，这里只是兜底）
    const patch = {};
    if (typeof msg.send === "boolean") patch.send = msg.send;
    if (typeof msg.closeOnStop === "boolean") patch.closeOnStop = msg.closeOnStop;
    if (msg.base) patch.base = msg.base;
    if (Object.keys(patch).length) {
      chrome.storage.local.set(patch).catch(() => {});
      loadCfg().then((v) => {
        pageCfg = v;
        // 页面内采集的开关要立刻生效：已经在采的标签页也通知一声。
        for (const tabId of pageCaps.keys()) {
          chrome.tabs.sendMessage(tabId, { type: "page-config", send: v.send })
            .catch(() => {});
        }
      }).catch(() => {});
    }
    return;
  }

  // ---- 共享浏览器模式（代理通过面板页调用） ----
  if (msg.type === "page-open") { pageOpen(msg).then(sendResponse); return true; }
  if (msg.type === "page-start") { pageStart(msg).then(sendResponse); return true; }
  if (msg.type === "page-stop") { pageStop(msg).then(sendResponse); return true; }
  if (msg.type === "page-probe") { pageProbe(msg).then(sendResponse); return true; }
  if (msg.type === "page-tab") { pageTab(msg).then(sendResponse); return true; }
  if (msg.type === "page-tabs") { pageTabs().then(sendResponse); return true; }
  if (msg.type === "page-stats") { sendResponse(pageStats()); return true; }
  if (msg.type === "page-close") { pageClose(msg).then(sendResponse); return true; }
  if (msg.type === "page-activate") { pageActivate(msg).then(sendResponse); return true; }
  if (msg.type === "page-focus-panel") { pageFocusPanel().then(sendResponse); return true; }
  if (msg.type === "page-pcm") { sendResponse(pagePcm(msg, sender)); return true; }

  if (msg.type === "mute") {
    chrome.tabs.update(msg.tabId, { muted: !!msg.muted })
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }

  if (msg.type === "close-tab") {
    chrome.tabs.remove(msg.tabId)
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
});
