'use strict';

/* ================================================================ base */
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const API = (window.__LN_ENGINE__ || '').replace(/\/+$/, '');
const ENTRY = window.__LN_ENTRY__ || '';
let ENGINE_ORIGIN = '';
try { ENGINE_ORIGIN = API ? new URL(API).origin : ''; } catch (e) { ENGINE_ORIGIN = ''; }
let ENTRY_SESSION = '';
try { ENTRY_SESSION = new URL(ENTRY || 'http://x/').searchParams.get('session') || ''; } catch (e) {}
const TAURI = window.__TAURI__;

const S = {
  view: 'listen',
  stage: 'signal',
  online: false,
  health: null,
  rooms: [],
  live: [],
  sessions: [],
  selectedId: null,
  selectedSid: '',
  lines: [],
  speakers: [],
  speakersSid: '',
  partial: null,
  sessInfo: null,
  annotTab: 'all',
  annotAuto: true,
  annotSkipped: new Set(),
  /* 标注工作区的面板布局：房间列表 / 直播画面 / 字幕 / 本场声音 */
  panes: { side: true, insp: true, live: true, tx: true },
  annotLiveH: 0.52,
  annotMax: '',
  spkPop: null,
  filter: 'all',
  addOpen: false,
  busy: false,
  pageOpen: false,
  pageRoom: null,
  stageLabel: null,
  pageHidden: false,
  overlayDepth: 0,
  frameUrl: '',
  frameEmbedUrl: '',
  frameRawUrl: '',
  frameTitle: '',
  frameMeta: '',
  frameHead: false,
  frameRaw: false,
  frameBackView: 'listen',
  selectedSession: ENTRY_SESSION,
  paletteOpen: false,
  paletteIndex: 0,
  paletteActions: [],
  modalOpen: false,
  closeAction: 'tray',
  txBusy: false,
  lastToastAt: 0,
  trashCount: 0,
  batchMode: false,
  batchSel: new Set(),
  workbenchReady: false,
  workbenchSid: '',
  frameLoads: 0,
  frameKind: '',
};

/* ================================================================ utils */
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
const pad = (n) => String(n).padStart(2, '0');
const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
const fmtDur = (sec) => {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? h + ':' + pad(m) + ':' + pad(s) : pad(m) + ':' + pad(s);
};
const fmtClock = (iso) => {
  if (!iso) return '';
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) return String(iso).slice(0, 16);
  return m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
};
const hostOf = (u) => { try { return new URL(u).host.replace(/^www\./, ''); } catch (e) { return ''; } };
const shortSid = (sid) => String(sid || '').replace(/^LIVE_/, '').replace(/_/g, ' ');

/* 位置标签不是人名：与 app/voiceprint.py 的 GENERIC_NAMES 对齐。
   改名前如果只是「嘉宾 / 运营」，就只算角色标注，不算认出这个人。 */
const GENERIC_RE = /^(主播|嘉宾|运营|说话人|未知|路人|观众|网友|连麦|有人|未识别|用户|SPK[_-]?\d+)$/i;
const isGenericName = (name) => {
  const n = String(name == null ? '' : name).trim();
  return !n || GENERIC_RE.test(n) || /^(嘉宾|说话人|用户|观众)\s*\d+$/.test(n);
};
const SPK_COLORS = ['#E8C47D', '#9CC8B0', '#B9A0E8', '#8FB4D9', '#E89088', '#D7C08A', '#A9C7E8'];
function spkColor(id) {
  let h = 0;
  const s = String(id || '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return SPK_COLORS[Math.abs(h) % SPK_COLORS.length];
}

/* 直播 status 和场后 /api/speakers 的字段名不同，统一成一种形状再画。 */
function normSpeaker(sp) {
  const id = String(sp.speaker_id || sp.id || '');
  const label = String(sp.label || sp.speaker_label || id || '说话人');
  const manual = sp.manual != null ? !!sp.manual : !!sp.verified;
  return {
    id, label,
    seconds: Math.max(0, Number(sp.seconds) || 0),
    count: Number(sp.count != null ? sp.count : (sp.n || 0)) || 0,
    manual,
    personState: String(sp.person_state || ''),
    personName: String(sp.person_name || ''),
    score: sp.person_score != null && sp.person_score !== '' ? Number(sp.person_score) : null,
    verified: !!sp.verified,
  };
}
function spkKind(sp) {
  if (!sp) return 'unknown';
  if (sp.personState === 'rejected') return 'rejected';
  if (sp.manual) return isGenericName(sp.label) ? 'role' : 'confirmed';
  if (sp.personState === 'confirmed') return 'confirmed';
  if (sp.personState === 'auto') return 'auto';
  if (sp.personState === 'cand' || sp.personState === 'candidate') return 'candidate';
  if (sp.personName) return 'candidate';
  if (sp.verified && !isGenericName(sp.label)) return 'confirmed';
  return 'unknown';
}
const isClaimed = (kind) => kind !== 'unknown' && kind !== 'rejected';
function annotSpeakers() {
  return (S.speakers || []).map(normSpeaker).filter((s) => s.id)
    .sort((a, b) => b.seconds - a.seconds || String(a.id).localeCompare(String(b.id)));
}
function spkMap() {
  const m = new Map();
  annotSpeakers().forEach((s) => m.set(s.id, s));
  return m;
}
function unclaimedCount(sess) {
  if (!sess || !Array.isArray(sess.speakers)) return 0;
  return sess.speakers.map(normSpeaker).filter((s) => {
    const k = spkKind(s);
    return (k === 'unknown' || k === 'rejected') && s.seconds >= 5;
  }).length;
}

function invoke(cmd, args) {
  if (!TAURI || !TAURI.core || !TAURI.core.invoke) {
    return Promise.reject(new Error('桌面 IPC 不可用'));
  }
  return TAURI.core.invoke(cmd, args);
}

async function api(path, opts) {
  const res = await fetch(API + path, Object.assign({ cache: 'no-store' }, opts || {}));
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}
async function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2800);
}

function copyText(text) {
  const value = String(text || '');
  if (!value) return;
  const fallback = () => {
    try {
      const ta = document.createElement('textarea');
      ta.value = value;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      toast('已复制');
    } catch (e) { toast('复制失败'); }
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).then(() => toast('已复制')).catch(fallback);
  } else {
    fallback();
  }
}

/* ================================================================ 应用内右键菜单
   WebView2 的原生菜单在 Rust 侧已经关掉；这里接管场次/房间/输入框。
   菜单只在对象上出现，空白画布不弹。 */
const CTX = { items: [], index: -1 };

// 菜单图标用内联 SVG，不依赖系统字体里有没有 ⧉/⤓ 这类符号。
const CTX_ICONS = {
  open: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  play: '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  rename: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13 5l4 4"/></svg>',
  copy: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="8" y="8" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>',
  export: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
  trash: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
  page: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 9h16"/></svg>',
  link: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M10 14a4 4 0 0 0 5.7 0l2.3-2.3a4 4 0 0 0-5.7-5.7L11 7"/><path d="M14 10a4 4 0 0 0-5.7 0L6 12.3a4 4 0 0 0 5.7 5.7L13 17"/></svg>',
  retry: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M20 11a8 8 0 1 0-2 5"/><path d="M20 4v7h-7"/></svg>',
  stop: '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>',
  remove: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M6 6l12 12M18 6L6 18"/></svg>',
};
function ctxIcon(name) { return CTX_ICONS[name] || ''; }

function closeCtxMenu() {
  const el = $('#ctxMenu');
  if (!el || el.hidden) return;
  el.hidden = true;
  el.innerHTML = '';
  CTX.items = [];
  CTX.index = -1;
}

function paintCtxIndex() {
  $$('#ctxMenu .ctx-item').forEach((b) => {
    b.classList.toggle('on', Number(b.dataset.i) === CTX.index);
  });
  const cur = $('#ctxMenu .ctx-item.on');
  if (cur) cur.focus({ preventScroll: true });
}

function showCtxMenu(items, x, y) {
  const el = $('#ctxMenu');
  if (!el) return;
  CTX.items = (items || []).filter(Boolean);
  if (!CTX.items.length) return;
  el.innerHTML = CTX.items.map((it, i) => {
    if (it.sep) return '<div class="ctx-sep"></div>';
    const cls = 'ctx-item' + (it.danger ? ' danger' : '') +
      (it.disabled ? ' disabled' : '');
    return '<button class="' + cls + '" data-i="' + i + '" role="menuitem"' +
      (it.disabled ? ' disabled' : '') + '>' +
      '<span class="ctx-ico">' + ctxIcon(it.icon) + '</span>' +
      '<span class="ctx-label">' + esc(it.label) + '</span>' +
      (it.hint ? '<span class="ctx-hint">' + esc(it.hint) + '</span>' : '') +
      '</button>';
  }).join('');
  el.hidden = false;
  el.style.left = '0px';
  el.style.top = '0px';
  const r = el.getBoundingClientRect();
  const pad = 8;
  el.style.left = Math.max(pad, Math.min(x, window.innerWidth - r.width - pad)) + 'px';
  el.style.top = Math.max(pad, Math.min(y, window.innerHeight - r.height - pad)) + 'px';
  const first = CTX.items.findIndex((it) => !it.sep && !it.disabled);
  CTX.index = first;
  paintCtxIndex();
}

function runCtxItem(i) {
  const it = CTX.items[i];
  if (!it || it.sep || it.disabled) return;
  closeCtxMenu();
  try { if (it.run) it.run(); } catch (e) { toast(String(e && e.message || e)); }
}

function ctxKey(e) {
  const el = $('#ctxMenu');
  if (!el || el.hidden) return;
  const usable = CTX.items
    .map((it, i) => ({ it, i }))
    .filter((x) => !x.it.sep && !x.it.disabled)
    .map((x) => x.i);
  const at = usable.indexOf(CTX.index);
  if (e.key === 'Escape') {
    e.preventDefault(); e.stopPropagation(); closeCtxMenu(); return;
  }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault(); e.stopPropagation();
    if (!usable.length) return;
    const next = e.key === 'ArrowDown'
      ? usable[(at + 1) % usable.length]
      : usable[(at - 1 + usable.length) % usable.length];
    CTX.index = next;
    paintCtxIndex();
    return;
  }
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault(); e.stopPropagation();
    runCtxItem(CTX.index);
  }
}

function initContextMenu() {
  const el = $('#ctxMenu');
  if (!el) return;
  el.addEventListener('click', (e) => {
    const b = e.target.closest('[data-i]');
    if (b) runCtxItem(Number(b.dataset.i));
  });
  document.addEventListener('keydown', ctxKey, true);
  document.addEventListener('mousedown', (e) => {
    if (!el.hidden && !el.contains(e.target)) closeCtxMenu();
  }, true);
  document.addEventListener('scroll', closeCtxMenu, true);
  window.addEventListener('blur', closeCtxMenu);
  window.addEventListener('resize', closeCtxMenu);
  // 兜底：即使 WebView2 设置没生效，也不让原生网页菜单露出来。
  // 输入框/可编辑区给一个最小编辑菜单，其余空白区域不弹。
  document.addEventListener('contextmenu', (e) => {
    const editable = e.target.closest('input, textarea, [contenteditable="true"]');
    if (editable) {
      e.preventDefault();
      showCtxMenu([
        { icon: '', label: '剪切', hint: 'Ctrl+X', run: () => { try { document.execCommand('cut'); } catch (err) {} } },
        { icon: 'copy', label: '复制', hint: 'Ctrl+C', run: () => { try { document.execCommand('copy'); } catch (err) {} } },
        { icon: '', label: '粘贴', hint: 'Ctrl+V', run: () => {
          if (navigator.clipboard && navigator.clipboard.readText) {
            navigator.clipboard.readText().then((t) => {
              try { document.execCommand('insertText', false, t); } catch (err) { toast('请用 Ctrl+V 粘贴'); }
            }).catch(() => toast('请用 Ctrl+V 粘贴'));
          } else { toast('请用 Ctrl+V 粘贴'); }
        } },
        { sep: true },
        { icon: '', label: '全选', hint: 'Ctrl+A', run: () => { try { document.execCommand('selectAll'); } catch (err) {} } },
      ], e.clientX, e.clientY);
      return;
    }
    const sel = window.getSelection();
    if (sel && String(sel).trim()) {
      e.preventDefault();
      showCtxMenu([{ icon: 'copy', label: '复制选中文字', hint: 'Ctrl+C', run: () => copyText(String(sel)) }],
        e.clientX, e.clientY);
      return;
    }
    e.preventDefault();
    closeCtxMenu();
  });
}

/* ================================================================ model */
function sessionFor(room) {
  if (!room || !room.session_id) return null;
  return S.live.find((s) => s.id === room.session_id) || null;
}
function stateInfo(room) {
  if (!room) return { k: 'stopped', label: '未选择' };
  const live = !!sessionFor(room) || room.state === 'live';
  if (room.state === 'failed' || (room.error && !live)) return { k: 'failed', label: '异常' };
  if (room.state === 'stopped') return { k: 'stopped', label: '已停止' };
  if (live) return { k: 'live', label: '在听' };
  return { k: 'pending', label: '启动中' };
}
function roomCh(room) {
  if (!room) return 'CH--';
  const seq = Number(room.seq) || (S.rooms.indexOf(room) + 1);
  return 'CH' + pad(seq);
}
function roomName(room) {
  if (!room) return '选择一个直播间';
  return room.name || hostOf(room.url) || '未命名直播间';
}
function captureLabel(room) {
  const v = room && room.capture_engine;
  return { direct: '直取', shared: '共享浏览器', per_room: '独立浏览器' }[v] || '—';
}
function liveRooms() {
  return S.rooms.filter((r) => stateInfo(r).k === 'live');
}
function selectedRoom() {
  return S.rooms.find((r) => r.id === S.selectedId) || null;
}
function ensureSelection() {
  if (S.selectedId && S.rooms.some((r) => r.id === S.selectedId)) return;
  const first = S.rooms.find((r) => stateInfo(r).k === 'live') || S.rooms[0] || null;
  S.selectedId = first ? first.id : null;
  S.lines = [];
  S.selectedSid = first ? (first.session_id || '') : '';
  if (S.pageOpen && S.pageRoom !== S.selectedId) closeStageView();
}

/* ================================================================ data */
async function refreshRooms() {
  if (!API) { S.online = false; renderAll(); return; }
  try {
    const [roomsRes, liveRes, health] = await Promise.all([
      api('/api/rooms'),
      api('/api/live/list').catch(() => ({ sessions: [] })),
      api('/api/health').catch(() => null),
    ]);
    S.rooms = (roomsRes.rooms || []).slice().sort((a, b) => {
      const rank = (r) => ({ live: 0, pending: 1, failed: 2, stopped: 3 }[stateInfo(r).k] ?? 3);
      return rank(a) - rank(b) || (Number(a.seq) || 0) - (Number(b.seq) || 0);
    });
    S.live = liveRes.sessions || [];
    S.health = health;
    S.online = true;
  } catch (e) {
    S.online = false;
    S.health = null;
  }
  ensureSelection();
  const room = selectedRoom();
  const sid = room ? (room.session_id || '') : '';
  if (sid !== S.selectedSid) { S.selectedSid = sid; S.lines = []; }
  renderAll();
}

async function loadSessions() {
  const had = S.selectedSession;
  try {
    const d = await api('/api/sessions');
    S.sessions = d.sessions || [];
  } catch (e) {
    S.sessions = [];
  }
  refreshTrashCount();
  // 默认打开一场“有内容且能回放”的场次，而不是列表最上面的空会话。
  // 提前在启动时选中，切到工作台就只加载一次 iframe，不会先白一下再重载。
  const alive = !!S.selectedSession && S.sessions.some((s) => s.id === S.selectedSession);
  if (!alive) {
    const best = S.sessions.find((s) => s.audio_state === 'ready' && Number(s.n_utterances) > 0)
      || S.sessions.find((s) => Number(s.n_utterances) > 0)
      || S.sessions[0];
    S.selectedSession = best ? best.id : '';
  }
  if (S.view !== 'workbench') return;
  if (S.selectedSession && S.selectedSession !== S.workbenchSid) {
    setWorkbenchFrame(S.selectedSession);
  }
  renderSidebar();
}

function setWorkbenchFrame(sid, autoplay, seekT) {
  sid = sid || '';
  // 同一个工作台 iframe：只发消息原地切场次，不再整页重载。
  // 这样正在播放的音频、筛选条件和滚动位置都能保住。
  if (S.view === 'workbench' && S.frameKind === 'workbench' && sid) {
    S.selectedSession = sid;
    if (S.workbenchReady) {
      if (S.workbenchSid !== sid || autoplay || seekT != null) {
        postToFrame({ type: 'ln-open-session', session: sid,
                     autoplay: !!autoplay, t: seekT == null ? null : Number(seekT) });
      }
      renderSidebar();
      return;
    }
    // iframe 还在加载：目标就是它时等加载完；换了场次才改 URL。
    if (S.workbenchSid === sid) { renderSidebar(); return; }
  }
  const q = 'embed=1' + (sid ? '&session=' + encodeURIComponent(sid) : '') +
    (autoplay ? '&autoplay=1' : '') +
    (seekT != null ? '&t=' + encodeURIComponent(seekT) : '');
  setFrame(API + '/?' + q, API + '/' + (sid ? '?session=' + encodeURIComponent(sid) : ''),
    '工作台', sid ? shortSid(sid) : '默认场次', true);
  S.frameKind = 'workbench';
  S.workbenchSid = sid;
}

function postToFrame(msg) {
  const f = $('#frame');
  if (!f || !f.contentWindow) return;
  try { f.contentWindow.postMessage(msg, ENGINE_ORIGIN || '*'); } catch (e) {}
}

async function refreshTrashCount() {
  if (!API) return;
  try {
    const d = await api('/api/library');
    S.trashCount = Number((d.stats || {}).trash) || 0;
    if (S.view === 'workbench') renderSidebar();
  } catch (e) {}
}

let txBusy = false;
async function pollTranscript() {
  const room = selectedRoom();
  if (S.view !== 'listen' || !room || !room.session_id) return;
  // 标注模式里页面是打开的，但字幕必须继续轮询；只有普通页面模式才停。
  if (S.pageOpen && S.stage !== 'annotate') return;
  if (txBusy) return;
  txBusy = true;
  try {
    let rows = [];
    if (stateInfo(room).k === 'live') {
      const d = await api('/api/live/status?id=' + encodeURIComponent(room.session_id));
      rows = Array.isArray(d.utterances) ? d.utterances
        : (d.utterances && Array.isArray(d.utterances.rows) ? d.utterances.rows : []);
      S.speakers = Array.isArray(d.speakers) ? d.speakers : [];
      S.speakersSid = room.session_id;
      S.partial = d.partial || null;
      S.sessInfo = d;
    } else {
      // 已停止 / 异常的房间没有实时会话，回放最后一次会话的字幕，
      // 总比停在一句「还在等第一句」有用。
      const d = await api('/api/utterances?session_id=' + encodeURIComponent(room.session_id) + '&limit=120');
      rows = (Array.isArray(d.rows) ? d.rows : []).slice().sort((a, b) => {
        const ta = a.t_start != null ? a.t_start : (a.t || 0);
        const tb = b.t_start != null ? b.t_start : (b.t || 0);
        return ta - tb;
      });
      S.partial = null;
      S.sessInfo = null;
      if (S.speakersSid !== room.session_id) {
        try {
          const sp = await api('/api/speakers?session_id=' + encodeURIComponent(room.session_id));
          S.speakers = Array.isArray(sp.speakers) ? sp.speakers : [];
          S.speakersSid = room.session_id;
        } catch (e) { S.speakers = []; }
      }
    }
    S.lines = rows.slice(-80);
    S.health = S.health || {};
    if (S.stage === 'annotate') renderAnnot();
    renderInspector();
  } catch (e) {
    /* 会话可能刚结束；下一次 rooms 轮询会修正 */
  } finally {
    txBusy = false;
  }
}

async function pollRoomsLoop() {
  await refreshRooms();
  setTimeout(pollRoomsLoop, S.online ? 2000 : 5000);
}
async function pollTranscriptLoop() {
  await pollTranscript();
  setTimeout(pollTranscriptLoop, 1500);
}
async function pollSessionsLoop() {
  if (S.view === 'workbench') await loadSessions();
  setTimeout(pollSessionsLoop, 15000);
}

/* ================================================================ views */
// 方案 B：整窗换肤。监听/实时字幕是暗色工作室，工作台/资料是暖纸阅读面。
const VIEW_THEME = {
  listen: 'studio', live: 'studio',
  workbench: 'paper', files: 'paper', people: 'paper', settings: 'paper', frame: 'paper',
};
function applyTheme() {
  document.documentElement.dataset.theme = VIEW_THEME[S.view] || 'paper';
}
function withTheme(url) {
  const theme = VIEW_THEME[S.view] || 'paper';
  try {
    const u = new URL(url, location.href);
    u.searchParams.set('theme', theme);
    return u.toString();
  } catch (e) { return url; }
}

const VIEW_META = {
  listen: { title: '收听', sub: '全部直播间', side: true, insp: true },
  workbench: { title: '工作台', sub: '场次与字幕', side: true, insp: false },
  files: { title: '文件中心', sub: '录音与回放', side: false, insp: false },
  people: { title: '人物档案', sub: '声纹与确认', side: false, insp: false },
  settings: { title: '设置', sub: '采集与模型', side: false, insp: false },
  live: { title: '实时字幕', sub: '正在监听', side: false, insp: false },
  frame: { title: '页面', sub: '', side: false, insp: false },
};

function switchView(view, opts) {
  opts = opts || {};
  if (S.view === 'listen' && view !== 'listen') closeStageView();
  if (S.paletteOpen) closePalette();
  S.view = view;
  S.stage = view === 'listen' ? S.stage : 'signal';
  applyTheme();

  const meta = VIEW_META[view] || VIEW_META.listen;
  const body = $('#body');
  // 收听页尊重用户自己开合的面板；其它页面按视图默认。
  body.classList.toggle('no-side', !(view === 'listen' ? S.panes.side : meta.side));
  body.classList.toggle('no-insp', !(view === 'listen' ? S.panes.insp : meta.insp));
  applyPaneButtons();
  $('#listenView').hidden = view !== 'listen';
  $('#frameView').hidden = view === 'listen';

  if (view === 'workbench') {
    const sid = opts.session || S.selectedSession || '';
    S.selectedSession = sid;
    setWorkbenchFrame(sid, opts.autoplay, opts.t);
    loadSessions();
  } else if (view === 'files') {
    const tab = ['trash', 'cleanup'].includes(opts.tab) ? opts.tab : 'live';
    setFrame(API + '/library?embed=1&tab=' + tab,
      API + '/library?tab=' + tab, '文件中心', '录音与回放', true);
    S.frameBackView = 'listen';
  } else if (view === 'people') {
    setFrame(API + '/people?embed=1', API + '/people', '人物档案', '声纹与确认', true);
    S.frameBackView = 'listen';
  } else if (view === 'settings') {
    setFrame(API + '/?embed=1&settings=1', API + '/?settings=1', '设置', '采集与模型', true);
    S.frameBackView = 'listen';
  } else if (view === 'live') {
    const sid = opts.session || S.selectedSession || '';
    const q = 'embed=1' + (sid ? '&session=' + encodeURIComponent(sid) : '');
    setFrame(API + '/live?' + q, API + '/live' + (sid ? '?session=' + encodeURIComponent(sid) : ''),
      '实时字幕', sid ? shortSid(sid) : '全部在听', true);
    S.frameBackView = opts.back || 'listen';
  } else if (view === 'frame') {
    setFrame(opts.url, opts.rawUrl || opts.url, opts.title || '页面', opts.meta || '', true);
    S.frameBackView = opts.back || 'listen';
  }
  // 工作台/资料页是嵌进来的旧页面：用深色边框包一层，像工作区里的画布；
  // 实时字幕本身就是深色全屏页，不需要这一圈。
  $('#frameView').classList.toggle('framed', view !== 'listen' && view !== 'live');

  $('#crumb').innerHTML = '<b>' + esc(meta.title) + '</b><i>/</i><span>' + esc(meta.sub) + '</span>';
  document.title = 'LiveNotes · ' + meta.title;
  renderRail();
  renderSidebar();
  renderStage();
  renderInspector();
  renderStatus();
}

function setFrame(embedUrl, rawUrl, title, meta, head) {
  S.frameEmbedUrl = embedUrl;
  S.frameRawUrl = rawUrl;
  S.frameTitle = title;
  S.frameMeta = meta || '';
  S.frameHead = !!head;
  S.frameRaw = false;
  S.workbenchReady = false;
  S.frameKind = '';
  $('#frameTitle').textContent = title;
  $('#frameMeta').textContent = meta || '';
  $('#frameView').classList.toggle('no-head', !head);
  $('#frameRaw').textContent = '原工具栏';
  $('#frame').src = withTheme(embedUrl);
}

/* ================================================================ render */
function renderAll() {
  renderRail();
  renderSidebar();
  renderStage();
  renderInspector();
  renderStatus();
}

function renderRail() {
  const active = (S.view === 'live' || S.view === 'frame') ? 'listen' : S.view;
  $$('.rail-item').forEach((b) => b.classList.toggle('on', b.dataset.view === active));
  const n = liveRooms().length;
  const badge = $('#railBadge');
  badge.hidden = !n;
  badge.textContent = String(n);
}

function renderSidebar() {
  const meta = VIEW_META[S.view] || VIEW_META.listen;
  $('#sideTitle').textContent = S.view === 'listen' ? '在听' : (S.view === 'workbench' ? '场次' : meta.title);
  $('#btnAdd').hidden = S.view !== 'listen';
  $('#sideFoot').hidden = false;
  $('#sideFoot').textContent = S.view === 'listen'
    ? '关掉窗口不影响录音 · 最多 5 路'
    : '点场次打开工作台 · 数据来自本地引擎';
  const tools = $('#sideTools');
  const list = $('#sideList');
  const count = S.view === 'listen' ? S.rooms.length : S.sessions.length;
  $('#sideCount').textContent = String(count);

  if (S.view === 'listen') {
    const liveN = liveRooms().length;
    const badN = S.rooms.filter((r) => stateInfo(r).k === 'failed').length;
    const stoppedN = S.rooms.filter((r) => stateInfo(r).k === 'stopped').length;
    tools.innerHTML =
      '<button class="chip ' + (S.filter === 'live' ? 'on' : '') + '" data-filter="live">在听 <span class="n">' + liveN + '</span></button>' +
      '<button class="chip ' + (S.filter === 'all' ? 'on' : '') + '" data-filter="all">全部 <span class="n">' + S.rooms.length + '</span></button>' +
      (badN ? '<button class="chip ' + (S.filter === 'issue' ? 'on' : '') + '" data-filter="issue">异常 <span class="n">' + badN + '</span></button>' : '');
    $('#sideFoot').innerHTML = '关掉窗口不影响录音 · 最多 5 路' +
      (stoppedN ? ' · <button class="link danger" data-clear-stopped>清除已停止 ' + stoppedN + '</button>' : '');
    const rows = S.rooms.filter((r) => {
      const k = stateInfo(r).k;
      if (S.filter === 'live') return k === 'live';
      if (S.filter === 'issue') return k === 'failed';
      return true;
    });
    list.innerHTML = rows.length ? rows.map(roomRowHtml).join('') : (
      '<div class="side-empty">' + (S.rooms.length
        ? '这个筛选下没有直播间。'
        : '还没有在听的直播间。<br>点右上角 ＋ 粘贴链接开始。') + '</div>'
    );
    return;
  }

  if (S.view === 'workbench') {
    const ids = new Set(S.sessions.map((s) => s.id));
    for (const id of Array.from(S.batchSel)) if (!ids.has(id)) S.batchSel.delete(id);
    tools.innerHTML =
      '<button class="chip on">全部场次 <span class="n">' + S.sessions.length + '</span></button>' +
      '<button class="chip ' + (S.batchMode ? 'on' : '') + '" data-batch-toggle>批量</button>' +
      (S.batchMode ? '<button class="chip" data-batch-all>全选</button>' : '');
    $('#sideFoot').innerHTML = S.batchMode
      ? ('已选 <b>' + S.batchSel.size + '</b> 场 · ' +
         '<button class="link" data-batch-trash' + (S.batchSel.size ? '' : ' disabled') + '>移到回收站</button> · ' +
         '<button class="link" data-batch-export' + (S.batchSel.size ? '' : ' disabled') + '>导出</button> · ' +
         '<button class="link" data-batch-cancel>取消</button>')
      : ('点场次打开工作台 · 数据来自本地引擎 · <button class="link" data-open-trash>回收站' +
         (S.trashCount ? ' ' + S.trashCount : '') + '</button>');
    if (!S.sessions.length) {
      list.innerHTML = '<div class="side-empty">还没有场次。<br>去「收听」加一个直播间，出字后这里会出现。' +
        '<br><button class="btn sm" data-demo="1">加载演示数据</button></div>';
    } else {
      list.innerHTML = S.sessions.slice(0, 100).map((s) => {
        const picked = S.batchSel.has(s.id);
        const on = !S.batchMode && s.id === S.selectedSession ? ' on' : '';
        const date = fmtClock(s.started_at);
        const dur = fmtDur((s.duration_s != null ? s.duration_s : s.elapsed) || 0);
        const n = s.n_utterances != null ? s.n_utterances : 0;
        let tag = '';
        if (s.is_demo) tag = '<span class="room-tag demo">演示</span>';
        else if (s.audio_state === 'missing') tag = '<span class="room-tag missing">录音丢失</span>';
        else if (s.audio_state === 'none') tag = '<span class="room-tag none">仅文字</span>';
        else if (s.audio_state === 'live') tag = '<span class="room-tag live">录制中</span>';
        else if (s.quality && s.quality.duplicate_of) tag = '<span class="room-tag dup">重复</span>';
        const actions = S.batchMode ? '' : ('<span class="row-actions">' +
          (s.audio_state === 'ready'
            ? '<span class="row-act" data-play="' + esc(s.id) + '" title="从开头播放">▶</span>'
            : '') +
          '<span class="row-act" data-more="session" data-id="' + esc(s.id) + '" title="更多操作">⋯</span>' +
          '</span>');
        return '<button class="room-row' + on + (picked ? ' picked' : '') +
          '" data-session="' + esc(s.id) + '">' +
          (S.batchMode
            ? '<span class="batch-check' + (picked ? ' on' : '') + '"></span>'
            : '<span class="room-ch">' + (s.audio_state === 'live' ? 'LIVE' : '已收') + '</span>') +
          '<span class="room-main"><span class="room-name">' + esc(s.title || '未命名场次') + '</span>' +
          '<span class="room-sub">' + esc(date) + ' · ' + n + ' 条' + tag + '</span></span>' +
          '<span class="room-time">' + esc(dur) + '</span>' + actions + '</button>';
      }).join('');
    }
    return;
  }

  tools.innerHTML = '';
  list.innerHTML = '';
}

function roomRowHtml(r) {
  const st = stateInfo(r);
  const s = sessionFor(r);
  const on = r.id === S.selectedId ? ' on' : '';
  const dur = s ? fmtDur(s.elapsed) : '';
  const tail = st.k === 'live'
    ? (s ? ' · ' + fmtDur(s.elapsed) : '')
    : (r.error ? ' · ' + esc(String(r.error).slice(0, 26)) : '');
  const nNew = st.k === 'live' ? unclaimedCount(s) : 0;
  const sub = '<span class="state ' + st.k + '"><i></i>' + st.label + '</span>' +
    (tail ? '<span class="sub-text">' + tail + '</span>' : '') +
    (nNew ? '<span class="room-tag new">新声音' + (nNew > 1 ? ' ' + nNew : '') + '</span>' : '');
  return '<button class="room-row' + on + '" data-room="' + esc(r.id) + '">' +
    '<span class="room-ch">' + roomCh(r) + '</span>' +
    '<span class="room-main"><span class="room-name">' + esc(roomName(r)) + '</span>' +
    '<span class="room-sub">' + sub + '</span></span>' +
    '<span class="room-time">' + esc(dur) + '</span>' +
    '<span class="row-actions"><span class="row-act" data-more="room" data-id="' +
    esc(r.id) + '" title="更多操作">⋯</span></span></button>';
}

function renderStage() {
  const room = selectedRoom();
  const st = room ? stateInfo(room) : null;
  const isWall = S.stage === 'wall';
  const isAnnot = S.stage === 'annotate';
  const isPage = S.stage === 'page';

  $('#stageCh').textContent = room ? roomCh(room) : 'CH--';
  $('#stageName').textContent = room ? roomName(room) : '选择一个直播间';
  $('#stageMark').textContent = room ? roomCh(room) : 'CH--';
  $('#btnStop').disabled = !room || st.k === 'stopped';
  $('#btnWall').textContent = isWall ? '信号' : '监控墙';
  $$('#stageSeg button').forEach((b) => {
    b.classList.toggle('on', !isWall && b.dataset.stage === S.stage);
    b.setAttribute('aria-selected', String(!isWall && b.dataset.stage === S.stage));
  });

  let meta = '—';
  if (room) {
    const s = sessionFor(room);
    meta = st.label + ' · ' + captureLabel(room) + (s ? ' · ' + fmtDur(s.elapsed) : '');
    if (isAnnot) {
      const a = annotStats();
      meta += ' · ' + a.claimed + ' 个已认领 / ' + a.todo + ' 个待认领';
    }
  }
  $('#stageMeta').textContent = meta;

  const wall = $('#wall');
  const annot = $('#annot');
  const scope = $('#scope');
  const empty = $('#stageEmpty');
  const err = $('#stageError');
  const badge = $('#stageBadge');
  const stats = $('#stageStats');

  wall.hidden = !isWall || !room;
  annot.hidden = !isAnnot || !room;
  scope.style.display = (isWall || isAnnot || isPage || !room) ? 'none' : '';
  empty.hidden = !!room;
  $('#stageMark').hidden = !room || isWall || isAnnot || isPage;
  err.hidden = !room || isWall || isAnnot || isPage || st.k !== 'failed';
  badge.hidden = !room || isWall || isAnnot || isPage || !S.pageOpen;
  stats.hidden = !room || isWall || isAnnot || isPage;
  if (S.pageOpen && !isAnnot && !isPage) {
    badge.className = 'stage-badge live';
    badge.innerHTML = '<span class="dot"></span>页面模式 · 采集仍在引擎里跑';
  }
  if (room && !isWall && !isAnnot && !isPage) {
    const s = sessionFor(room);
    const lag = s && s.last_chunk_ago_s != null ? '最后一块 ' + Number(s.last_chunk_ago_s).toFixed(1) + 's 前' : '等待音频块';
    const n = s && s.n_utterances != null ? s.n_utterances : 0;
    stats.textContent = fmtDur(s ? s.elapsed : 0) + ' · ' + n + ' 条 · ' + lag;
  }
  if (err.hidden === false && room) {
    $('#stageErrTitle').textContent = st.k === 'failed' ? '这一路出了点问题' : '这一路没有在采集';
    $('#stageErrText').textContent = room.error || '采集端没有回报状态。可以先重试；仍然失败就去「设置」看诊断。';
  }
  if (isWall) renderWall();
  if (!isAnnot && S.spkPop) closeSpeakerPop();
  if (isAnnot && room) { renderAnnot(); applyAnnotLayout(); }
}

let wallWaveSeed = 0;
function wavePath(seed, w, h) {
  const mid = h / 2;
  let d = 'M0 ' + mid.toFixed(1);
  for (let x = 0; x <= w; x += 4) {
    const p = x / w;
    const a = Math.sin(p * 22 + seed) * 0.34 + Math.sin(p * 61 + seed * 1.7) * 0.16;
    const env = Math.exp(-Math.pow((p - 0.5) * 2.4, 2));
    const y = mid - a * env * (h * 0.42);
    d += ' L' + x + ' ' + y.toFixed(1);
  }
  return d;
}
function renderWall() {
  const wall = $('#wall');
  if (!S.rooms.length) {
    wall.innerHTML = '<div class="side-empty" style="grid-column:1/-1;padding:40px">还没有直播间。<br>先添加一路，再回来看监控墙。</div>';
    return;
  }
  wall.innerHTML = S.rooms.map((r, i) => {
    const st = stateInfo(r);
    const s = sessionFor(r);
    const on = r.id === S.selectedId ? ' on' : '';
    const name = esc(roomName(r));
    const line = s && s.last_line ? s.last_line : (r.error || '—');
    return '<button class="wall-card' + on + '" data-room="' + esc(r.id) + '">' +
      '<span class="wc-top"><span class="wc-ch">' + roomCh(r) + '</span>' +
      '<span class="wc-name">' + name + '</span>' +
      '<span class="wc-state state ' + st.k + '"><i></i>' + st.label + '</span></span>' +
      '<span class="wc-wave"><svg viewBox="0 0 220 54" preserveAspectRatio="none">' +
      '<path d="' + wavePath(i + 1.3, 220, 54) + '" fill="none" stroke="' +
      (st.k === 'live' ? 'rgba(132,214,178,.72)' : 'rgba(143,180,217,.28)') +
      '" stroke-width="1.3"/></svg></span>' +
      '<span class="wc-foot"><span>' + fmtDur(s ? s.elapsed : 0) + '</span>' +
      '<span class="grow">' + esc(String(line).slice(0, 34)) + '</span></span></button>';
  }).join('') +
    '<button class="wall-card wall-add" data-add="1" title="添加直播间">' +
    '<span class="wa-plus">＋</span><span class="wa-title">添加一路直播间</span>' +
    '<span class="wa-hint">粘贴链接，开始听</span></button>';
}

/* ================================================================ 标注模式 */
function annotStats() {
  const list = annotSpeakers();
  let total = 0, green = 0, amber = 0, claimed = 0, todo = 0;
  list.forEach((sp) => {
    const k = spkKind(sp);
    total += sp.seconds;
    if (k === 'confirmed' || k === 'auto' || k === 'role') { green += sp.seconds; claimed += 1; }
    else if (k === 'candidate') { amber += sp.seconds; claimed += 1; }
    else if (k === 'unknown' || k === 'rejected') todo += 1;
  });
  const pct = total > 0 ? Math.min(100, Math.round(100 * (green + amber) / total)) : 0;
  const greenPct = total > 0 ? Math.min(100, Math.round(100 * green / total)) : 0;
  const amberPct = total > 0 ? Math.min(100 - greenPct, Math.round(100 * amber / total)) : 0;
  return { list, total, green, amber, claimed, todo, pct, greenPct, amberPct, n: list.length };
}
function spkCls(kind, sp) {
  if (sp && /^主播/.test(sp.label) &&
      (kind === 'confirmed' || kind === 'auto' || kind === 'role')) return 'host';
  return ({ confirmed: 'known', auto: 'auto', role: 'role', candidate: 'sus',
            unknown: 'unknown', rejected: 'rejected' })[kind] || 'unknown';
}
function spkById(id) {
  return annotSpeakers().find((s) => s.id === id) || null;
}

function renderAnnot() {
  if (S.stage !== 'annotate') return;
  const room = selectedRoom();
  if (!room) return;
  const a = annotStats();
  const total = S.sessInfo && S.sessInfo.n_utterances != null
    ? S.sessInfo.n_utterances : S.lines.length;
  $('#txCount').textContent = String(total);
  $('#txTodoN').textContent = String(a.todo);
  $$('#txTabs .tx-tab').forEach((b) => b.classList.toggle('on', b.dataset.tab === S.annotTab));
  $('#txAuto').classList.toggle('on', S.annotAuto);
  renderAnnotFoot(room);

  // 正在认人时先别重画行：一重画弹层就丢了，等用户选完再继续。
  if (S.spkPop) return;

  const map = spkMap();
  const todoIds = new Set(a.list.filter((sp) => {
    const k = spkKind(sp);
    return k === 'unknown' || k === 'rejected';
  }).map((sp) => sp.id));
  const first = a.list.find((sp) => todoIds.has(sp.id) &&
    !S.annotSkipped.has(sp.id) && sp.seconds >= 3);
  renderAnnotBanner(first);

  let rows = S.lines;
  if (S.annotTab === 'todo') rows = rows.filter((r) => todoIds.has(String(r.speaker_id || '')));

  const html = rows.map((r) => {
    const id = String(r.speaker_id || '');
    const sp = map.get(id);
    const kind = sp ? spkKind(sp) : 'unknown';
    const label = sp ? sp.label : (r.speaker_label || id || '说话人');
    const high = r.value_tag === 'high' || r.value_tag === 'important';
    const t = r.t_start != null ? fmtDur(r.t_start) : (r.t != null ? fmtDur(r.t) : '');
    const score = kind === 'candidate' && sp && sp.score != null
      ? '<span class="score">疑似 ' + sp.score.toFixed(2) + '</span>' : '';
    return '<div class="trow' + (high ? ' high' : '') + '">' +
      '<span class="tt">' + esc(t) + '</span>' +
      '<button class="spk ' + spkCls(kind, sp) + '" data-spk-id="' + esc(id || label) + '" title="点一下认人">' +
        '<i style="background:' + (sp ? spkColor(id) : 'currentColor') + '"></i>' +
        esc(label) + score + '</button>' +
      '<span class="tx">' + esc(r.text || '') + '</span></div>';
  }).join('');

  const partial = S.partial && S.partial.text && S.annotTab === 'all'
    ? '<div class="trow partial"><span class="tt">' + esc(fmtDur(S.partial.t_start || 0)) +
      '</span><span class="spk">识别中</span><span class="tx">' + esc(S.partial.text) + '</span></div>'
    : '';

  const emptyMsg = !room.session_id
    ? '这一路还没有建立会话。等开始出字后再看。'
    : (S.annotTab === 'todo' ? '没有待认领的声音了。' : '还在等第一句。页面开始出声后，这里会一条条出现。');

  const scroll = $('#txScroll');
  if (!scroll) return;
  const keep = scroll.scrollTop;
  const nearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 48;
  scroll.innerHTML = html + partial +
    ((!html && !partial) ? '<div class="tx-empty">' + emptyMsg + '</div>' : '');
  if (S.annotAuto && (nearBottom || !html)) scroll.scrollTop = scroll.scrollHeight;
  else scroll.scrollTop = keep;
}

function renderAnnotBanner(first) {
  const el = $('#txBanner');
  if (!el) return;
  if (!first) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  el.innerHTML = '<b>' + esc(first.label || first.id) + '</b> 已说 ' +
    esc(fmtDur(first.seconds)) + '，还没认领<span class="sp"></span>' +
    '<button class="btn" data-spk-act="name" data-spk="' + esc(first.id) + '">起名</button>' +
    '<button class="btn" data-spk-act="skip" data-spk="' + esc(first.id) + '">跳过</button>';
}

function renderAnnotFoot(room) {
  const note = $('#txFootNote');
  if (!note) return;
  let text = '还没有会话';
  if (S.sessInfo) {
    if (S.sessInfo.paused) text = '已暂停转写 · 采集继续';
    else if (S.sessInfo.last_final_ago_s != null) {
      text = '最后定稿 ' + Number(S.sessInfo.last_final_ago_s).toFixed(1) + 's 前';
    } else text = '等待下一句';
  } else if (room && room.session_id) text = '已结束 · 可继续标注';
  note.textContent = text;
}

function speakerCardHtml(sp) {
  const kind = spkKind(sp);
  const skipped = S.annotSkipped.has(sp.id);
  const color = spkColor(sp.id);
  let tag = '<span class="snd-tag todo">待认领</span>';
  if (kind === 'confirmed') tag = '<span class="snd-tag ok">已确认</span>';
  else if (kind === 'auto') tag = '<span class="snd-tag ok">自动认出</span>';
  else if (kind === 'role') tag = '<span class="snd-tag role">仅角色</span>';
  else if (kind === 'candidate') {
    tag = '<span class="snd-tag sus">疑似' +
      (sp.score != null ? ' ' + sp.score.toFixed(2) : '') + '</span>';
  } else if (kind === 'rejected') tag = '<span class="snd-tag rej">不是他</span>';
  else if (skipped) tag = '<span class="snd-tag rej">已跳过</span>';

  const stateNote = kind === 'candidate' ? '声纹候选'
    : kind === 'confirmed' ? '已入人物库'
    : kind === 'auto' ? '自动比对通过'
    : kind === 'rejected' ? '本场不再自动认'
    : kind === 'role' ? '名字以后再补'
    : '未入人物库';

  let acts = '';
  const id = esc(sp.id);
  if (kind === 'candidate') {
    acts = '<button class="btn tiny amber" data-spk-act="confirm" data-spk="' + id + '">确认是 ' +
      esc(sp.label) + '</button>' +
      '<button class="btn tiny" data-spk-act="reject" data-spk="' + id + '">不是他</button>';
  } else if (kind === 'unknown') {
    acts = '<button class="btn tiny amber" data-spk-act="name" data-spk="' + id + '">起名</button>' +
      '<button class="btn tiny" data-spk-act="role" data-spk="' + id + '">只标角色</button>' +
      (skipped
        ? '<button class="btn tiny" data-spk-act="unskip" data-spk="' + id + '">取消跳过</button>'
        : '<button class="btn tiny" data-spk-act="skip" data-spk="' + id + '">跳过</button>');
  } else if (kind === 'rejected') {
    acts = '<button class="btn tiny" data-spk-act="name" data-spk="' + id + '">重新认领</button>';
  } else if (kind === 'role') {
    acts = '<button class="btn tiny amber" data-spk-act="name" data-spk="' + id + '">补名字</button>' +
      '<button class="btn tiny" data-spk-act="role" data-spk="' + id + '">改角色</button>';
  } else {
    acts = '<button class="btn tiny" data-spk-act="name" data-spk="' + id + '">改名</button>' +
      '<button class="btn tiny" data-spk-act="reject" data-spk="' + id + '">不是他</button>';
  }
  return '<div class="snd' + (kind === 'unknown' ? ' todo' : '') +
    (kind === 'rejected' ? ' rej' : '') + '">' +
    '<div class="snd-top"><i class="snd-dot" style="background:' + color + '"></i>' +
      '<span class="snd-name">' + esc(sp.label) + '</span>' + tag + '</div>' +
    '<div class="snd-meta"><span>' + fmtDur(sp.seconds) + '</span><span>' +
      sp.count + ' 句</span><span>' + stateNote + '</span></div>' +
    (acts ? '<div class="snd-acts">' + acts + '</div>' : '') + '</div>';
}

function renderAnnotInspector(room) {
  const st = stateInfo(room);
  const s = sessionFor(room);
  const a = annotStats();
  const cards = a.list.length ? a.list.map(speakerCardHtml).join('')
    : '<div class="insp-empty">还没有分出说话人。第一句定稿后会在这里出现。</div>';
  const hint = a.todo
    ? '下游：总结 / 高光已带名字；<b>导出前建议补完</b>。未确认的“疑似”不会写成确定事实。'
    : (a.n ? '本场声音都认领了。总结、高光、导出可以直接用名字。' : '等第一句定稿后开始统计。');
  const kv = [];
  kv.push(['状态', '<b class="state ' + st.k + '" style="display:inline-flex"><i></i>' + st.label + '</b>']);
  kv.push(['采集', esc(captureLabel(room))]);
  kv.push(['会话', room.session_id ? esc(shortSid(room.session_id)) : '—']);
  kv.push(['时长', s ? fmtDur(s.elapsed) : '—']);
  kv.push(['字幕', (s && s.n_utterances != null ? s.n_utterances : S.lines.length) + ' 条']);
  if (s) {
    kv.push(['最后一块', s.last_chunk_ago_s != null
      ? Number(s.last_chunk_ago_s).toFixed(1) + 's 前' : '—']);
    kv.push(['质量', (s.dropped || 0) + ' 丢块 · ' + (s.queued || 0) + ' 排队']);
  } else {
    kv.push(['回放', room.session_id ? '最后一次会话' : '—']);
  }

  const scroll = $('#inspScroll');
  const top = scroll.scrollTop;
  scroll.innerHTML =
    '<div class="insp-section"><h3>本场声音 <span class="count">' + a.n + '</span>' +
      '<span class="grow"></span><span style="font-weight:400;letter-spacing:0;font-size:8.5px">按发言时长</span></h3>' +
      cards + '</div>' +
    '<div class="insp-section"><h3>标注进度</h3>' +
      '<div class="progress"><i style="width:' + a.greenPct + '%"></i>' +
        '<b style="width:' + a.amberPct + '%"></b></div>' +
      '<div class="pstat"><b>' + a.pct + '%</b><span>按发言时长 · 已认领 ' +
        a.claimed + '/' + a.n + ' 个声音</span></div>' +
      '<div class="phint">' + hint + '</div></div>' +
    '<div class="insp-section"><h3>这一路</h3><dl class="kv">' +
      kv.map((r) => '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>').join('') +
      '</dl>' + (s && s.paused ? '<div class="phint"><b>已暂停</b>：采集还在，只是不再转写。</div>' : '') +
    '</div>';
  scroll.scrollTop = top;

  const acts = [];
  acts.push('<button class="btn sm" data-act="page">显示页面</button>');
  acts.push('<button class="btn sm" data-act="live"' + (room.session_id ? '' : ' disabled') + '>实时字幕</button>');
  if (st.k === 'failed' || st.k === 'stopped') {
    acts.push('<button class="btn sm" data-act="retry">重试</button>');
  }
  if (st.k !== 'stopped') acts.push('<button class="btn sm danger" data-act="stop">停止</button>');
  $('#inspActions').innerHTML = acts.join('');
}

function openSpeakerPop(id, anchor) {
  const sp = spkById(id);
  if (!sp) return;
  S.spkPop = { id };
  const pop = $('#spkPop');
  const kind = spkKind(sp);
  const others = annotSpeakers().filter((x) => x.id !== id).slice(0, 5);
  let html = '<div class="ph"><b>这一簇是谁说的？</b>改判后整簇生效，不是只改这一句</div>';
  if (kind === 'candidate') {
    html += '<button class="pi on" data-target-confirm="1">' +
      '<span class="dot" style="background:' + spkColor(sp.id) + '"></span>' +
      esc(sp.label) + '<span class="badge">声纹 ' +
      (sp.score != null ? sp.score.toFixed(2) : '疑似') + ' 疑似</span></button>';
  }
  html += others.map((x) => '<button class="pi" data-target="' + esc(x.id) + '">' +
    '<span class="dot" style="background:' + spkColor(x.id) + '"></span>' + esc(x.label) +
    '<span class="meta">' + fmtDur(x.seconds) + '</span></button>').join('');
  html += '<button class="pi pnew" data-new="1">＋ 新建人物…</button>' +
    '<div class="roles"><div class="lb">只标角色（不进声纹库，名字以后再补）</div>' +
    '<div class="row"><button class="r" data-role="主播">主播</button>' +
    '<button class="r" data-role="嘉宾">嘉宾</button>' +
    '<button class="r" data-role="运营">运营</button></div></div>';
  if (kind === 'candidate') {
    html += '<button class="pi danger" data-reject="1">不是这个人</button>';
  }
  html += '<div class="pf">改名 = <b>注册声纹</b> · 可在「人物」页删除</div>';
  pop.innerHTML = html;
  pop.hidden = false;
  placeSpeakerPop(anchor);
  renderAnnot();
}

function placeSpeakerPop(anchor) {
  const pop = $('#spkPop');
  if (!pop || !anchor) return;
  const r = anchor.getBoundingClientRect();
  const pr = pop.getBoundingClientRect();
  // 不往左翻：左边是原生直播页子视图，HTML 弹层会被它盖住。
  // 右边不够就压到检查器上，宁可盖住 HTML，也不要被原生视图吃掉。
  let x = Math.min(r.right + 8, window.innerWidth - pr.width - 10);
  x = Math.max(8, x);
  let y = r.top - 4;
  if (y + pr.height > window.innerHeight - 10) y = window.innerHeight - pr.height - 10;
  y = Math.max(48, y);
  pop.style.left = x + 'px';
  pop.style.top = y + 'px';
}

function closeSpeakerPop() {
  if (!S.spkPop) return;
  S.spkPop = null;
  const pop = $('#spkPop');
  if (pop) { pop.hidden = true; pop.innerHTML = ''; }
  renderAnnot();
}

async function fetchAnnotSnapshot() {
  const room = selectedRoom();
  if (!room || !room.session_id) { S.speakers = []; S.lines = []; return; }
  if (stateInfo(room).k === 'live') {
    const d = await api('/api/live/status?id=' + encodeURIComponent(room.session_id));
    S.speakers = Array.isArray(d.speakers) ? d.speakers : [];
    S.speakersSid = room.session_id;
    S.partial = d.partial || null;
    S.sessInfo = d;
    S.lines = (Array.isArray(d.utterances) ? d.utterances : []).slice(-80);
    return;
  }
  const [sp, ut] = await Promise.all([
    api('/api/speakers?session_id=' + encodeURIComponent(room.session_id)),
    api('/api/utterances?session_id=' + encodeURIComponent(room.session_id) + '&limit=120'),
  ]);
  S.speakers = Array.isArray(sp.speakers) ? sp.speakers : [];
  S.speakersSid = room.session_id;
  S.partial = null;
  S.sessInfo = null;
  S.lines = (Array.isArray(ut.rows) ? ut.rows : []).slice().sort((a, b) => {
    const ta = a.t_start != null ? a.t_start : (a.t || 0);
    const tb = b.t_start != null ? b.t_start : (b.t || 0);
    return ta - tb;
  });
}

async function refreshAnnot() {
  try { await fetchAnnotSnapshot(); } catch (e) {}
  renderAnnot();
  renderInspector();
  renderStatus();
  renderSidebar();
}

async function renameSpeaker(id, name) {
  const room = selectedRoom();
  const n = String(name || '').trim().slice(0, 40);
  if (!room || !room.session_id || !id || !n) return false;
  try {
    if (isGenericName(n)) {
      // 主播 / 嘉宾 / 运营只是角色，不进人物底库。
      await post('/api/speakers/rename', {
        session_id: room.session_id, speaker_id: id, name: n,
      });
      toast('已标为「' + n + '」角色，不注册声纹。');
    } else {
      // 走 enroll 而不是 rename：能拿到“声纹够不够注册”的结果，
      // 不会在样本不足时骗用户说已经入底库。
      const out = await post('/api/persons/enroll', {
        session_id: room.session_id, speaker_id: id, name: n,
      });
      if (out && out.ok) {
        toast('已改名为 ' + n + ' · 声纹样本 +' + (out.added || 0) + '。');
      } else {
        await post('/api/speakers/rename', {
          session_id: room.session_id, speaker_id: id, name: n,
        });
        toast('已改名为 ' + n + '；' + ((out && out.reason) || '这一簇声纹样本不够') +
          '，本次没入人物库。');
      }
    }
    await refreshAnnot();
    refreshRooms();
    return true;
  } catch (e) {
    toast('改名失败：' + e.message);
    return false;
  }
}

function promptSpeakerName(id) {
  const sp = spkById(id);
  if (!sp) return;
  const current = isGenericName(sp.label) ? '' : sp.label;
  openModal(
    '<h3>给这一簇起个名字</h3>' +
    '<p>改名会把这一簇的声纹注册进人物底库，下次这个人再开口就能被认出来。' +
    '只想标角色就取消，在弹层里选「主播 / 嘉宾 / 运营」。</p>' +
    '<input class="modal-input" id="spkName" maxlength="40" placeholder="真实姓名或常用昵称" value="' +
    esc(current) + '" autocomplete="off" spellcheck="false">' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn primary" id="spkNameOk">保存并注册</button></div>',
    (sheet) => {
      const input = $('#spkName', sheet);
      const ok = $('#spkNameOk', sheet);
      setTimeout(() => { input.focus(); input.select(); }, 30);
      const submit = async () => {
        const name = (input.value || '').trim();
        if (!name) { toast('名字不能为空。'); return; }
        if (isGenericName(name)) {
          toast('「' + name + '」是角色标签，请用「只标角色」。');
          return;
        }
        ok.disabled = true;
        const done = await renameSpeaker(id, name);
        ok.disabled = false;
        if (done) closeModal();
      };
      ok.onclick = submit;
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); submit(); }
      });
    });
}

async function confirmSpeaker(id) {
  const sp = spkById(id);
  if (!sp) return;
  const name = isGenericName(sp.label) ? (sp.personName || '') : sp.label;
  closeSpeakerPop();
  if (!name) { promptSpeakerName(id); return; }
  await renameSpeaker(id, name);
}

async function rejectSpeaker(id) {
  const room = selectedRoom();
  if (!room || !room.session_id || !id) return;
  closeSpeakerPop();
  try {
    await post('/api/live/reject_hint', { session_id: room.session_id, speaker_id: id });
    toast('已排除，本场不再自动认这个人。');
    await refreshAnnot();
    refreshRooms();
  } catch (e) {
    toast('操作失败：' + e.message);
  }
}

function skipSpeaker(id) {
  S.annotSkipped.add(id);
  closeSpeakerPop();
  renderAnnot();
  renderInspector();
}

function unskipSpeaker(id) {
  S.annotSkipped.delete(id);
  renderAnnot();
  renderInspector();
}

async function mergeSpeakerInto(srcId, target) {
  const src = spkById(srcId);
  if (!src || !target || src.id === target.id) return;
  const ks = spkKind(src);
  const kt = spkKind(target);
  // 两个都已经有身份时，合并是“改判一个人”，不是修正一个未知簇：
  // 多问一句，避免手一滑把已经确认的人并没了。
  if (isClaimed(ks) && isClaimed(kt) && src.label !== target.label) {
    closeSpeakerPop();
    openModal(
      '<h3>把「' + esc(src.label) + '」并进「' + esc(target.label) + '」？</h3>' +
      '<p>这一簇的 ' + src.count + ' 句会改判给「' + esc(target.label) +
      '」，本场不再保留「' + esc(src.label) + '」。' +
      '如果两边其实是两个人，请取消，改用「不是他」或重新起名。</p>' +
      '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
      '<button class="btn danger" data-mconfirm="merge">确认合并</button></div>',
      (sheet) => {
        sheet.querySelector('[data-mconfirm="merge"]').onclick = () => {
          closeModal();
          doMergeSpeaker(src.id, target);
        };
      });
    return;
  }
  doMergeSpeaker(src.id, target);
}

async function doMergeSpeaker(srcId, target) {
  const room = selectedRoom();
  if (!room || !room.session_id || !target || !srcId || srcId === target.id) return;
  closeSpeakerPop();
  try {
    await post('/api/speakers/merge', {
      session_id: room.session_id,
      src_ids: [srcId],
      target_id: target.id,
      target_label: target.label,
    });
    toast('已把这一簇并入「' + target.label + '」。');
    await refreshAnnot();
    refreshRooms();
  } catch (e) {
    toast('合并失败：' + e.message);
  }
}

async function handleSpkAction(act, id, el) {
  if (!id) return;
  if (act === 'name' || act === 'rename') return promptSpeakerName(id);
  if (act === 'role') return openSpeakerPop(id, el || $('#txScroll'));
  if (act === 'confirm') return confirmSpeaker(id);
  if (act === 'reject') return rejectSpeaker(id);
  if (act === 'skip') return skipSpeaker(id);
  if (act === 'unskip') return unskipSpeaker(id);
}

async function markMoment() {
  const room = selectedRoom();
  if (!room || !room.session_id) { toast('这一路还没有会话。'); return; }
  try {
    await post('/api/live/mark', { session_id: room.session_id, note: '标注模式打点' });
    toast('已打点，可在工作台的时间轴上找到。');
  } catch (e) {
    toast('打点失败：' + e.message);
  }
}

async function togglePause() {
  const room = selectedRoom();
  if (!room || !room.session_id) return;
  const s = S.sessInfo || sessionFor(room) || {};
  const paused = !s.paused;
  try {
    await post('/api/live/pause', { session_id: room.session_id, paused });
    toast(paused ? '已暂停转写，采集继续。' : '已恢复转写。');
    await refreshAnnot();
  } catch (e) {
    toast('操作失败：' + e.message);
  }
}

function renderInspector() {
  const room = selectedRoom();
  const show = S.view === 'listen' && !!room;
  $('#inspector').classList.toggle('placeholder', !show);
  if (!show) {
    $('#inspKicker').textContent = '尚未选择';
    $('#inspTitle').textContent = '选择一个直播间';
    $('#inspSub').textContent = '右边会显示这一路的实时状态和字幕。';
    $('#inspScroll').innerHTML = '<div class="insp-empty">从左边选一路，或先添加一个直播间。<br><br>收听页只负责“看”和“控制”；采集、转写、保存都在本地引擎里，切到别的页面不会中断。</div>';
    $('#inspActions').innerHTML = '';
    return;
  }
  const st = stateInfo(room);
  const s = sessionFor(room);
  $('#inspKicker').textContent = roomCh(room) + ' · ' + st.label;
  $('#inspTitle').textContent = roomName(room);
  $('#inspSub').textContent = (hostOf(room.resolved_url || room.url) || '本地直播间') +
    (room.session_id ? ' · ' + shortSid(room.session_id) : '');

  if (S.stage === 'annotate') {
    renderAnnotInspector(room);
    return;
  }

  const rows = [];
  rows.push(['状态', '<b class="state ' + st.k + '" style="display:inline-flex"><i></i>' + st.label + '</b>']);
  rows.push(['采集', esc(captureLabel(room))]);
  rows.push(['会话', room.session_id ? esc(shortSid(room.session_id)) : '—']);
  rows.push(['时长', s ? fmtDur(s.elapsed) : '—']);
  if (s) {
    rows.push(['字幕', String(s.n_utterances || 0) + ' 条']);
    rows.push(['最后一块', s.last_chunk_ago_s != null ? Number(s.last_chunk_ago_s).toFixed(1) + 's 前' : '—']);
    if (s.last_line) rows.push(['最后一句', esc(String(s.last_line).slice(0, 60))]);
  }
  if (room.error) rows.push(['错误', '<span style="color:var(--red)">' + esc(String(room.error).slice(0, 80)) + '</span>']);

  const lines = S.lines.slice(-12).map((r) => {
    const high = r.value_tag === 'high' || r.value_tag === 'important';
    const t = r.t_start != null ? fmtDur(r.t_start) : (r.t != null ? fmtDur(r.t) : '');
    return '<div class="tline' + (high ? ' high' : '') + '">' +
      '<span class="tt">' + esc(t) + '</span>' +
      '<span class="tx">' + esc(r.text || '') + '</span></div>';
  }).join('');

  const scroll = $('#inspScroll');
  const top = scroll.scrollTop;
  scroll.innerHTML =
    '<div class="insp-section"><h3>这一路</h3><dl class="kv">' +
    rows.map((r) => '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>').join('') +
    '</dl></div>' +
    '<div class="insp-section"><h3>实时字幕 <span class="count">' + (s ? (s.n_utterances || 0) : 0) + '</span></h3>' +
    (lines || '<div class="insp-empty">' + (room.session_id ? '还在等第一句。页面开始出声后，这里会一条条出现。' : '这一路还没有建立会话。启动完成后再看。') + '</div>') +
    '</div>';
  scroll.scrollTop = top;

  const acts = [];
  acts.push('<button class="btn sm" data-act="page">' + (S.stage === 'page' ? '返回信号' : '显示页面') + '</button>');
  acts.push('<button class="btn sm" data-act="live"' + (room.session_id ? '' : ' disabled') + '>实时字幕</button>');
  if (st.k === 'failed' || st.k === 'stopped') acts.push('<button class="btn sm" data-act="retry">重试</button>');
  if (st.k === 'failed' || st.k === 'stopped') acts.push('<button class="btn sm danger" data-act="remove">移除</button>');
  if (st.k !== 'stopped') acts.push('<button class="btn sm danger" data-act="stop">停止</button>');
  $('#inspActions').innerHTML = acts.join('');
}

function renderStatus() {
  const el = $('#engineStatus');
  el.className = 'engine ' + (S.online ? 'ok' : 'bad');
  const port = API ? (API.match(/:(\d+)/) || [])[1] : '';
  el.innerHTML = '<i></i><span>' + (S.online
    ? '本地引擎 · 127.0.0.1' + (port ? ':' + port : '')
    : '引擎未连接 · 重试中') + '</span>';
  // 连不上时把地址和排查路径放进 tooltip：最常见的原因是引擎没起来、
  // 端口换了，或页面 origin 被引擎的 CORS 白名单挡掉（tauri dev 的 devUrl）。
  el.title = S.online
    ? '引擎 ' + (API || '—')
    : (API
      ? '请求 ' + API + '/api/rooms 失败。检查：引擎是否启动、端口是否变化、托盘 → 诊断里的日志。'
      : '没有拿到引擎地址（window.__LN_ENGINE__ 为空）。');
  $('#statRooms').textContent = S.rooms.length + ' 个直播间';
  $('#statLive').textContent = liveRooms().length + ' 路在听';
  $('#liveCount').textContent = String(liveRooms().length);
  const pill = $('#recPill');
  pill.classList.toggle('idle', liveRooms().length === 0);
  const dot = $('#recDot');
  dot.classList.toggle('pulse', liveRooms().length > 0);
  $('#statHint').textContent = S.online
    ? (S.view === 'listen' && S.stage === 'annotate'
      ? '标注模式 · 已认领 ' + annotStats().claimed + '/' + annotStats().n + ' 人 · 关窗后继续录音'
      : '关窗后继续录音 · Ctrl K 命令')
    : '检查引擎日志或托盘诊断';
}

/* ================================================================ room ops */
async function selectRoom(id) {
  if (!id || id === S.selectedId) {
    if (id && S.stage === 'wall') setStage('signal');
    return;
  }
  const keepAnnot = S.stage === 'annotate';
  closeSpeakerPop();
  if (S.pageOpen) await closeStageView();
  S.selectedId = id;
  S.selectedSid = (selectedRoom() || {}).session_id || '';
  S.lines = [];
  S.speakers = [];
  S.speakersSid = '';
  S.partial = null;
  S.sessInfo = null;
  S.annotSkipped = new Set();
  S.stage = keepAnnot ? 'annotate' : 'signal';
  renderSidebar();
  renderStage();
  renderInspector();
  pollTranscript();
  if (keepAnnot) ensureAnnotPage();
}

async function addRoom() {
  const input = $('#addInput');
  const url = (input.value || '').trim();
  if (!url || S.busy) return;
  S.busy = true;
  $('#addGo').disabled = true;
  $('#addGo').textContent = '添加中…';
  try {
    const r = await post('/api/rooms', { url });
    input.value = '';
    if (r.created === false && r.room) {
      toast('这个链接已经在列表里了，已帮你选中。');
      S.selectedId = r.room.id;
    } else {
      toast('已加入收听，正在启动采集。');
    }
    S.addOpen = false;
    $('#addBox').classList.remove('on');
    await refreshRooms();
    if (r.room && r.room.id) {
      S.selectedId = r.room.id;
      renderSidebar(); renderStage(); renderInspector();
    }
  } catch (e) {
    toast('加不进去：' + e.message);
  } finally {
    S.busy = false;
    $('#addGo').disabled = false;
    $('#addGo').textContent = '开始听';
  }
}

async function stopRoom(room) {
  if (!room) return;
  try {
    await post('/api/rooms/close', { id: room.id });
    toast('已停止，录音会正常收尾保存。');
    if (S.pageRoom === room.id) await closeStageView();
    await refreshRooms();
  } catch (e) {
    toast('停止失败：' + e.message);
  }
}

async function retryRoom(room) {
  if (!room) return;
  try {
    await post('/api/rooms/state', { id: room.id, state: 'pending', error: '' });
    toast('已重新排队，采集端会重新拉起这一路。');
    await refreshRooms();
  } catch (e) {
    toast('重试失败：' + e.message);
  }
}

/* ================================================================ 对象菜单与数据闭环 */
function findSession(id) {
  return S.sessions.find((s) => s.id === id) || null;
}
function findRoom(id) {
  return S.rooms.find((r) => r.id === id) || null;
}

function openWorkbenchPlay(id) {
  S.selectedSession = id;
  switchView('workbench', { session: id, autoplay: true });
}

function sessionMenu(id, x, y) {
  const s = findSession(id);
  if (!s) return;
  const canPlay = s.audio_state === 'ready';
  showCtxMenu([
    { icon: 'open', label: '打开工作台', hint: '↵', run: () => {
      S.selectedSession = id;
      switchView('workbench', { session: id });
    } },
    { icon: 'play', label: '从开头播放', disabled: !canPlay, run: () => openWorkbenchPlay(id) },
    { sep: true },
    { icon: 'rename', label: '重命名', run: () => renameSessionDialog(id) },
    { icon: 'copy', label: '复制场次 ID', run: () => copyText(id) },
    { icon: 'export', label: '导出 Markdown', run: () => downloadSession(id) },
    { sep: true },
    { icon: 'trash', label: '移到回收站…', danger: true, run: () => confirmTrashSession(id) },
  ], x, y);
}

function roomMenu(id, x, y) {
  const r = findRoom(id);
  if (!r) return;
  const st = stateInfo(r);
  showCtxMenu([
    { icon: 'play', label: '打开实时字幕', disabled: !r.session_id, run: () => {
      switchView('live', { session: r.session_id, back: 'listen' });
    } },
    { icon: 'page', label: '显示采集页面', disabled: !(r.url || r.resolved_url), run: () => {
      selectRoom(r.id).then(() => openStagePage());
    } },
    { icon: 'link', label: '复制直播链接', disabled: !(r.url || r.resolved_url),
      run: () => copyText(r.resolved_url || r.url) },
    { sep: true },
    { icon: 'retry', label: '重试采集', disabled: st.k === 'live' || st.k === 'pending',
      run: () => retryRoom(r) },
    { icon: 'stop', label: '停止采集', disabled: st.k === 'stopped', run: () => confirmStopRoom(r) },
    { sep: true },
    { icon: 'remove', label: '移除这一路…', danger: true, run: () => confirmRemoveRoom(r) },
  ], x, y);
}

function renameSessionDialog(id) {
  const s = findSession(id) || {};
  openModal(
    '<h3>重命名场次</h3>' +
    '<p>改名后，自动总结不会再覆盖你起的标题。</p>' +
    '<input class="modal-input" id="renameInput" value="' + esc(s.title || '') + '" maxlength="60">' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn primary" data-mconfirm="rename">保存</button></div>',
    (sheet) => {
      const input = sheet.querySelector('#renameInput');
      const ok = async () => {
        const title = input.value.trim();
        if (!title) { toast('标题不能为空'); return; }
        try {
          await post('/api/sessions/rename', { session_id: id, title });
          closeModal();
          toast('已重命名');
          await loadSessions();
          if (S.view === 'workbench' && S.selectedSession === id) setWorkbenchFrame(id);
        } catch (e) { toast('重命名失败：' + e.message); }
      };
      input.focus();
      input.select();
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); ok(); }
      });
      sheet.querySelector('[data-mconfirm="rename"]').onclick = ok;
    }
  );
}

async function downloadSession(id) {
  try {
    const res = await fetch(API + '/api/export?session_id=' + encodeURIComponent(id) + '&format=md');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = id + '.md';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast('已导出 Markdown');
  } catch (e) {
    toast('导出失败：' + e.message);
  }
}

function confirmTrashSession(id) {
  const s = findSession(id) || {};
  openModal(
    '<h3>移到回收站？</h3>' +
    '<p>「' + esc(s.title || '未命名场次') + '」的音频会一起移入回收站，逐字稿和总结保留。' +
    '可以在文件中心还原。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="trash">移到回收站</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="trash"]').onclick = async () => {
        closeModal();
        try {
          await post('/api/library/trash', { session_id: id });
          S.sessions = S.sessions.filter((x) => x.id !== id);
          if (S.selectedSession === id) {
            const next = S.sessions.find((x) => x.audio_state === 'ready' &&
              Number(x.n_utterances) > 0) || S.sessions.find((x) => Number(x.n_utterances) > 0)
              || S.sessions[0];
            S.selectedSession = next ? next.id : '';
            if (S.view === 'workbench') setWorkbenchFrame(S.selectedSession);
          }
          renderSidebar();
          renderStage();
          renderInspector();
          toast('已移到回收站，可在文件中心还原');
        } catch (e) {
          toast('删除失败：' + e.message);
        }
      };
    }
  );
}

function confirmStopRoom(room) {
  if (!room) return;
  openModal(
    '<h3>停止「' + esc(roomName(room)) + '」？</h3>' +
    '<p>会停止采集并关闭这一路的采集浏览器。已经录下的内容会正常收尾保存，回放不受影响。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="stop">停止这一路</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="stop"]').onclick = () => { closeModal(); stopRoom(room); };
    }
  );
}

function confirmRemoveRoom(room) {
  if (!room) return;
  const active = stateInfo(room).k !== 'stopped';
  openModal(
    '<h3>移除「' + esc(roomName(room)) + '」？</h3>' +
    '<p>' + (active ? '会先停止这一路采集，再' : '') +
    '从在听列表移除。已经录下的内容会保留，之后还能在工作台找到。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="remove">移除这一路</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="remove"]').onclick = async () => {
        closeModal();
        try {
          await post('/api/rooms/remove', { id: room.id });
          if (S.pageRoom === room.id) await closeStageView();
          toast('已移除，已录内容保留在工作台');
          await refreshRooms();
          await loadSessions();
        } catch (e) {
          toast('移除失败：' + e.message);
        }
      };
    }
  );
}

async function clearStoppedRooms() {
  const rooms = S.rooms.filter((r) => stateInfo(r).k === 'stopped');
  if (!rooms.length) { toast('现在没有已停止的直播间。'); return; }
  openModal(
    '<h3>清除 ' + rooms.length + ' 路已停止的直播间？</h3>' +
    '<p>只从在听列表移除，不删除已录内容。每路对应的场次仍能在工作台找到。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="clear">清除已停止</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="clear"]').onclick = async () => {
        closeModal();
        let done = 0;
        for (const r of rooms) {
          try { await post('/api/rooms/remove', { id: r.id }); done += 1; } catch (e) {}
        }
        toast(done ? ('已移除 ' + done + ' 路，已录内容保留') : '移除失败，请稍后再试');
        await refreshRooms();
        await loadSessions();
      };
    }
  );
}

/* ---------------- 工作台批量操作 ---------------- */
function batchIds() {
  return Array.from(S.batchSel).filter((id) => findSession(id));
}
function confirmBatchTrash() {
  const ids = batchIds();
  if (!ids.length) return;
  openModal(
    '<h3>把选中的 ' + ids.length + ' 场移到回收站？</h3>' +
    '<p>音频会一起移走，逐字稿和总结保留；可以在文件中心的回收站还原。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="batch-trash">移到回收站</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="batch-trash"]').onclick = async () => {
        closeModal();
        try {
          const r = await post('/api/library/batch',
            { action: 'trash', session_ids: ids });
          const skipped = (r.skipped || []).length;
          S.batchMode = false;
          S.batchSel.clear();
          await loadSessions();
          renderSidebar();
          toast('已移入回收站 ' + (r.done || []).length + ' 场' +
            (skipped ? ('，跳过 ' + skipped + ' 场（正在录制）') : '') +
            '，可在文件中心还原');
        } catch (e) { toast('删除失败：' + e.message); }
      };
    }
  );
}
async function batchExport() {
  const ids = batchIds();
  if (!ids.length) return;
  toast('正在打包 ' + ids.length + ' 场…');
  try {
    const res = await fetch(API + '/api/export/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_ids: ids, format: 'md' }),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { const d = await res.json(); msg = d.error || msg; } catch (e) {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'livenotes-export-' + new Date().toISOString().slice(0, 10) + '.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast('已导出 ' + ids.length + ' 场的 Markdown 压缩包');
  } catch (e) { toast('批量导出失败：' + e.message); }
}

async function loadDemo() {
  try {
    const d = await post('/api/demo/seed', {});
    S.selectedSession = d.session_id || '';
    await loadSessions();
    switchView('workbench', { session: S.selectedSession });
    toast('已加载演示数据，可右键删除');
  } catch (e) {
    toast('加载演示数据失败：' + e.message);
  }
}

function stopAll() {
  const rooms = S.rooms.filter((r) => stateInfo(r).k !== 'stopped');
  if (!rooms.length) { toast('现在没有需要停止的直播间。'); return; }
  openModal(
    '<h3>停止全部 ' + rooms.length + ' 路？</h3>' +
    '<p>会停止采集并关闭每一路的采集浏览器。已经录下的内容会正常收尾保存，之后还能在文件中心回放。</p>' +
    '<ul class="modal-list">' + rooms.slice(0, 8).map((r) =>
      '<li><span class="ch">' + roomCh(r) + '</span><span>' + esc(roomName(r)) + '</span></li>').join('') +
    (rooms.length > 8 ? '<li>… 还有 ' + (rooms.length - 8) + ' 路</li>' : '') + '</ul>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn danger" data-mconfirm="stopall">停止全部</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mconfirm="stopall"]').onclick = async () => {
        closeModal();
        for (const r of rooms) {
          try { await post('/api/rooms/close', { id: r.id }); } catch (e) {}
        }
        if (S.pageOpen) await closeStageView();
        toast('已停止全部采集。');
        await refreshRooms();
      };
    }
  );
}

/* ================================================================ close / quit */
// 关闭窗口和退出应用是两件事：
//   - 没有录音时，按设置「最小化到托盘 / 退出程序」；
//   - 有录音时一律先问：托盘继续 / 停止并退出 / 取消。
// 真正退出走 shell_quit，Tauri Exit 事件会回收引擎；Job Object 再兜底。
function activeRooms() {
  return S.rooms.filter((r) => ['live', 'pending'].includes(stateInfo(r).k));
}

async function fetchCloseAction() {
  try {
    const r = await fetch(API + '/api/settings', { cache: 'no-store' });
    if (r.ok) {
      const s = await r.json();
      const v = s && s.prefs && s.prefs.close_action;
      if (v === 'quit' || v === 'tray') S.closeAction = v;
    }
  } catch (e) {}
  return S.closeAction || 'tray';
}

function hideToTray() {
  const first = !localStorage.getItem('ln.trayHint');
  if (first) {
    localStorage.setItem('ln.trayHint', '1');
    toast('已最小化到托盘，录音继续。退出请用托盘菜单「退出 LiveNotes」。');
    setTimeout(() => { invoke('shell_hide').catch(() => {}); }, 1800);
    return;
  }
  invoke('shell_hide').catch(() => {});
}

async function quitNow() {
  try {
    await invoke('shell_quit');
  } catch (e) {
    toast('退出失败：' + e.message);
  }
}

async function stopRoomsAndQuit(rooms) {
  toast('正在停止 ' + rooms.length + ' 路…');
  for (const r of rooms) {
    try { await post('/api/rooms/close', { id: r.id }); } catch (e) {}
  }
  await quitNow();
}

function openCloseModal(rooms) {
  openModal(
    '<h3>还有 ' + rooms.length + ' 路在录音</h3>' +
    '<p>关闭窗口不会停止录音。要让它在托盘继续跑，还是停止采集并退出？</p>' +
    '<ul class="modal-list">' + rooms.slice(0, 8).map((r) =>
      '<li><span class="ch">' + roomCh(r) + '</span><span>' + esc(roomName(r)) + '</span></li>').join('') +
    (rooms.length > 8 ? '<li>… 还有 ' + (rooms.length - 8) + ' 路</li>' : '') + '</ul>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn" data-mact="tray">最小化到托盘</button>' +
    '<button class="btn danger" data-mact="quit">停止并退出</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mact="tray"]').onclick = () => { closeModal(); hideToTray(); };
      sheet.querySelector('[data-mact="quit"]').onclick = () => { closeModal(); stopRoomsAndQuit(rooms); };
    }
  );
}

function openQuitModal(n) {
  openModal(
    '<h3>退出 LiveNotes？</h3>' +
    '<p>还有 ' + n + ' 路在录音。退出会停止采集并结束引擎；已经录下的内容会正常收尾保存。</p>' +
    '<div class="sheet-actions"><button class="btn" data-mclose>取消</button>' +
    '<button class="btn" data-mact="tray">最小化到托盘</button>' +
    '<button class="btn danger" data-mact="quit">停止并退出</button></div>',
    (sheet) => {
      sheet.querySelector('[data-mact="tray"]').onclick = () => { closeModal(); hideToTray(); };
      sheet.querySelector('[data-mact="quit"]').onclick = () => {
        closeModal();
        const rooms = activeRooms();
        if (rooms.length) stopRoomsAndQuit(rooms);
        else quitNow();
      };
    }
  );
}

async function requestClose() {
  const rooms = activeRooms();
  if (rooms.length) {
    openCloseModal(rooms);
    return;
  }
  const action = await fetchCloseAction();
  if (action === 'quit') return quitNow();
  hideToTray();
}

function requestQuit(n) {
  const rooms = activeRooms();
  const count = n || rooms.length;
  if (count > 0) {
    openQuitModal(count);
    return;
  }
  quitNow();
}

/* ================================================================ stage page */
function stageRect() {
  // 标注模式里原生页面只占左栏；整块舞台留给字幕，子视图不能盖 HTML。
  const el = (S.stage === 'annotate' && $('#annotPage')) ? $('#annotPage') : $('#stageWrap');
  const r = el.getBoundingClientRect();
  return { x: Math.round(r.left), y: Math.round(r.top), w: Math.max(1, Math.round(r.width)), h: Math.max(1, Math.round(r.height)) };
}
/* 旧直取把 reflow 的 room id 当 web_rid，拼过 live.douyin.com/<id>；抖音对
   那个地址只回「服务器开小差了」。同一个 id 同时出现在 reflow 路径里，就当
   这个 resolved_url 不可信，退回 reflow 页；采集端回报真实 web_rid 之后两者
   不同，才用真实地址。 */
function roomPageUrl(room) {
  const resolved = String(room.resolved_url || '');
  const reflow = /\/reflow\/(\d+)/.exec(room.url || '');
  const live = /^https?:\/\/live\.douyin\.com\/(\d+)/.exec(resolved);
  if (live && reflow && live[1] === reflow[1]) return room.url || resolved;
  return resolved || room.url || '';
}

async function openStagePage() {
  const room = selectedRoom();
  if (!room) return;
  const url = roomPageUrl(room);
  if (!url) { toast('这一路没有可打开的页面。'); return; }
  await closeStageView();
  const label = 'stage-' + String(room.id).replace(/[^a-zA-Z0-9_-]/g, '-').slice(0, 48);
  const r = stageRect();
  try {
    await invoke('room_open', {
      label, url, sid: room.session_id || '', capture: false,
      x: r.x, y: r.y, w: r.w, h: r.h,
    });
    S.stageLabel = label;
    S.pageOpen = true;
    S.pageRoom = room.id;
    S.pageHidden = false;
    renderStage();
  } catch (e) {
    toast('页面打开失败：' + String(e));
  }
}
async function closeStageView() {
  const label = S.stageLabel;
  S.stageLabel = null;
  S.pageOpen = false;
  S.pageRoom = null;
  S.pageHidden = false;
  if (label) { try { await invoke('room_close', { label }); } catch (e) {} }
  renderStage();
}

/* 三种舞台模式：信号 / 页面 / 标注。页面与标注都要开原生子视图，
   区别只在矩形：页面占满舞台，标注只占左栏，右栏留给字幕。 */
async function setStage(mode) {
  if (!['signal', 'page', 'annotate', 'wall'].includes(mode) || S.stage === mode) return;
  const room = selectedRoom();
  S.stage = mode;
  closeSpeakerPop();
  if (mode === 'signal' || mode === 'wall') {
    if (S.pageOpen) await closeStageView();
  }
  renderStage();
  renderInspector();
  renderStatus();
  if (mode === 'page') {
    if (!room) return;
    if (!(room.url || room.resolved_url)) {
      toast('这一路没有可打开的页面。');
      S.stage = 'signal';
      renderStage();
      renderInspector();
      return;
    }
    await openStagePage();
  } else if (mode === 'annotate') {
    await ensureAnnotPage();
    pollTranscript();
  }
}

async function ensureAnnotPage() {
  if (S.stage !== 'annotate') return;
  const room = selectedRoom();
  if (!room) return;
  const url = roomPageUrl(room);
  // 登录页 / 已结束的房间不自动开页：开了也只是空白，反而挡住“没有页面”的说明。
  if (!url || stateInfo(room).k !== 'live') {
    if (S.pageOpen) await closeStageView();
    renderStage();
    return;
  }
  // 画面面板关着、或字幕正放大时不开原生视图：开了也看不见，还白占一个 WebView。
  if (!S.panes.live || S.annotMax === 'tx') {
    renderStage();
    return;
  }
  if (S.pageOpen && S.pageRoom === room.id) {
    await layoutStageView();
    renderStage();
    return;
  }
  await openStagePage();
}
async function layoutStageView() {
  if (!S.stageLabel || S.pageHidden) return;
  const r = stageRect();
  try { await invoke('room_layout', { label: S.stageLabel, x: r.x, y: r.y, w: r.w, h: r.h }); } catch (e) {}
}
let layoutTimer = 0;
function scheduleStageLayout() {
  if (!S.stageLabel) return;
  clearTimeout(layoutTimer);
  layoutTimer = setTimeout(layoutStageView, 120);
}
async function hideStageForOverlay() {
  await syncStageVisible();
}
async function restoreStageFromOverlay() {
  await syncStageVisible();
}

/* ================================================================ overlay */
function openModal(html, mount) {
  if (S.modalOpen) return;
  S.overlayDepth++;
  if (S.overlayDepth === 1) hideStageForOverlay();
  S.modalOpen = true;
  const modal = $('#modal');
  const sheet = $('#modalSheet');
  sheet.innerHTML = html;
  modal.hidden = false;
  if (mount) mount(sheet);
  modal.onclick = (e) => { if (e.target === modal) closeModal(); };
  $$('[data-mclose]', sheet).forEach((b) => { b.onclick = closeModal; });
}
function closeModal() {
  if (!S.modalOpen) return;
  S.modalOpen = false;
  $('#modal').hidden = true;
  $('#modalSheet').innerHTML = '';
  S.overlayDepth = Math.max(0, S.overlayDepth - 1);
  if (S.overlayDepth === 0) restoreStageFromOverlay();
}

function paletteActions() {
  const room = selectedRoom();
  const list = [
    { id: 'listen', label: '收听工作区', hint: '看所有在听、切换直播间', key: '1', icon: '◉', run: () => switchView('listen') },
    { id: 'workbench', label: '工作台', hint: '场次、字幕、AI 总结', key: '2', icon: '▤', run: () => switchView('workbench') },
    { id: 'files', label: '文件中心', hint: '录音、回放、导出', key: '3', icon: '▧', run: () => switchView('files') },
    { id: 'cleanup', label: '体检清理', hint: '演示 / 空采集 / 重复场次', icon: '⌫', run: () => switchView('files', { tab: 'cleanup' }) },
    { id: 'trash', label: '回收站', hint: '还原或彻底删除', icon: '▤', run: () => switchView('files', { tab: 'trash' }) },
    { id: 'people', label: '人物档案', hint: '声纹与人名确认', key: '4', icon: '◎', run: () => switchView('people') },
    { id: 'settings', label: '设置', hint: '采集、模型、桌面选项', key: '5', icon: '⚙', run: () => switchView('settings') },
    { id: 'add', label: '添加直播间', hint: '粘贴抖音 / 快手 / B 站链接', icon: '＋', run: () => { switchView('listen'); openAdd(); } },
    { id: 'wall', label: '打开监控墙', hint: '所有直播间一屏看状态', icon: '▦', run: () => { switchView('listen'); setStage('wall'); } },
  ];
  if (room) {
    list.push({ id: 'annotate', label: '打开标注模式', hint: '直播页 + 全高字幕，边听边认人', icon: '◎', run: () => { switchView('listen'); setStage('annotate'); } });
    list.push({ id: 'page', label: S.stage === 'page' ? '返回信号画布' : '显示直播页面', hint: roomName(room), icon: '▭', run: () => { switchView('listen'); setTimeout(() => setStage(S.stage === 'page' ? 'signal' : 'page'), 0); } });
    if (room.session_id) list.push({ id: 'live', label: '打开实时字幕', hint: roomName(room), icon: '≡', run: () => switchView('live', { session: room.session_id }) });
  }
  if (S.rooms.some((r) => stateInfo(r).k !== 'stopped')) {
    list.push({ id: 'stopall', label: '停止全部采集', hint: liveRooms().length + ' 路在听', icon: '■', run: () => { closePalette(); setTimeout(stopAll, 0); } });
  }
  list.push({ id: 'refresh', label: '刷新数据', hint: '重新读取房间与会话', icon: '↻', run: () => { refreshRooms(); loadSessions(); toast('已刷新。'); } });
  return list;
}
function openPalette() {
  if (S.paletteOpen || S.modalOpen) return;
  S.overlayDepth++;
  if (S.overlayDepth === 1) hideStageForOverlay();
  S.paletteOpen = true;
  S.paletteIndex = 0;
  const p = $('#palette');
  p.hidden = false;
  const input = $('#pInput');
  input.value = '';
  renderPalette();
  setTimeout(() => input.focus(), 20);
}
function closePalette() {
  if (!S.paletteOpen) return;
  S.paletteOpen = false;
  $('#palette').hidden = true;
  S.overlayDepth = Math.max(0, S.overlayDepth - 1);
  if (S.overlayDepth === 0) restoreStageFromOverlay();
}
function renderPalette() {
  const q = ($('#pInput').value || '').trim().toLowerCase();
  S.paletteActions = paletteActions().filter((a) =>
    !q || (a.label + ' ' + a.hint + ' ' + a.id).toLowerCase().includes(q));
  const list = $('#pList');
  if (!S.paletteActions.length) {
    list.innerHTML = '<div class="p-empty">没有匹配的命令。试试“收听”“工作台”“停止”。</div>';
    return;
  }
  S.paletteIndex = clamp(S.paletteIndex, 0, S.paletteActions.length - 1);
  list.innerHTML = S.paletteActions.map((a, i) =>
    '<button class="p-item' + (i === S.paletteIndex ? ' on' : '') + '" data-pi="' + i + '">' +
    '<span class="pi-icon">' + a.icon + '</span>' +
    '<span class="pi-main"><b>' + esc(a.label) + '</b><span>' + esc(a.hint || '') + '</span></span>' +
    (a.key ? '<span class="pi-key">Ctrl ' + a.key + '</span>' : '') + '</button>').join('');
  const on = list.querySelector('.p-item.on');
  if (on) on.scrollIntoView({ block: 'nearest' });
}
function runPalette(i) {
  const a = S.paletteActions[i];
  if (!a) return;
  closePalette();
  setTimeout(() => a.run(), 0);
}

/* ================================================================ add box */
function openAdd() {
  S.addOpen = true;
  $('#addBox').classList.add('on');
  const input = $('#addInput');
  input.focus();
  input.select();
}
function closeAdd() {
  S.addOpen = false;
  $('#addBox').classList.remove('on');
  $('#addInput').value = '';
}

/* ================================================================ scope */
const scope = { ctx: null, w: 0, h: 0, raf: 0 };
function resizeScope() {
  const c = $('#scope');
  if (!c) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const r = c.getBoundingClientRect();
  if (!r.width || !r.height) return;
  scope.w = r.width; scope.h = r.height;
  c.width = Math.round(r.width * dpr);
  c.height = Math.round(r.height * dpr);
  scope.ctx = c.getContext('2d');
  scope.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
function drawScope(now) {
  scope.raf = requestAnimationFrame(drawScope);
  const room = selectedRoom();
  if (!room || S.view !== 'listen' || S.stage !== 'signal' || S.pageOpen || document.hidden) return;
  if (!scope.ctx) resizeScope();
  const ctx = scope.ctx;
  if (!ctx) return;
  const w = scope.w, h = scope.h;
  if (!w || !h) return;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#070B0F';
  ctx.fillRect(0, 0, w, h);

  ctx.strokeStyle = 'rgba(143,180,217,.055)';
  ctx.lineWidth = 1;
  for (let x = 0; x < w; x += 44) {
    ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, h); ctx.stroke();
  }
  for (let y = 0; y < h; y += 36) {
    ctx.beginPath(); ctx.moveTo(0, y + 0.5); ctx.lineTo(w, y + 0.5); ctx.stroke();
  }
  ctx.strokeStyle = 'rgba(143,180,217,.13)';
  ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();

  const s = sessionFor(room);
  const live = stateInfo(room).k === 'live';
  const t = now / 1000;
  const amp = (live ? 0.55 + Math.min(0.35, Number(s && s.level) || 0.1) : 0.12) * h * 0.32;
  const phase = t * (live ? 1.6 : 0.25);
  const seed = String(room.id).split('').reduce((a, c) => a + c.charCodeAt(0), 0) % 100;

  function line(color, alpha, mult, speed) {
    ctx.beginPath();
    for (let x = 0; x <= w; x += 2) {
      const p = x / w;
      const env = 0.25 + 0.75 * Math.exp(-Math.pow((p - 0.5) * 1.7, 2));
      const v = (Math.sin(p * 34 + phase * speed + seed) * 0.55 +
        Math.sin(p * 91 - phase * 0.7 + seed * 0.3) * 0.24 +
        Math.sin(p * 13 + phase * 0.4) * 0.2) * amp * env * mult;
      const y = h / 2 + v;
      if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.globalAlpha = alpha;
    ctx.lineWidth = 1.4;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }
  line('rgba(230,181,102,.85)', 0.85, 1, 1);
  line('rgba(143,180,217,.55)', 0.5, 1.35, 1.13);

  const px = ((now / 26) % (w + 120)) - 60;
  const grad = ctx.createLinearGradient(px - 18, 0, px + 18, 0);
  grad.addColorStop(0, 'rgba(230,181,102,0)');
  grad.addColorStop(0.5, 'rgba(230,181,102,.18)');
  grad.addColorStop(1, 'rgba(230,181,102,0)');
  ctx.fillStyle = grad;
  ctx.fillRect(px - 18, 0, 36, h);
  ctx.strokeStyle = 'rgba(230,181,102,.5)';
  ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
}
function startScope() {
  cancelAnimationFrame(scope.raf);
  scope.raf = requestAnimationFrame(drawScope);
}

/* ================================================================ navigate */
function navigateToUrl(href) {
  let u;
  try { u = new URL(href, API + '/'); } catch (e) { return; }
  if (u.origin !== ENGINE_ORIGIN) { toast('外部页面请在应用内浏览器打开。'); return; }
  const path = u.pathname.replace(/\/+$/, '') || '/';
  const sid = u.searchParams.get('session') || u.searchParams.get('session_id') || '';
  if (path === '/rooms') return switchView('listen');
  if (path === '/live') return switchView('live', { session: sid });
  if (path === '/library') {
    const tab = u.searchParams.get('tab') || 'live';
    return switchView('files', { tab });
  }
  if (path === '/people') return switchView('people');
  if (path === '/' || path === '/index.html') {
    if (u.searchParams.get('settings') != null) return switchView('settings');
    if (sid) S.selectedSession = sid;
    const t = u.searchParams.get('t');
    return switchView('workbench', { session: sid, t });
  }
  const embed = new URL(u.href);
  embed.searchParams.set('embed', '1');
  return switchView('frame', {
    url: embed.href, rawUrl: u.href,
    title: ({ '/reader': '阅读', '/highlights': '高光榜', '/metrics': '指标' })[path] || '页面',
    meta: sid ? shortSid(sid) : '', back: 'workbench',
  });
}

/* ================================================================ bind */
function bind() {
  document.addEventListener('click', (e) => {
    const rail = e.target.closest('.rail-item');
    if (rail) {
      const v = rail.dataset.view;
      if (v === 'settings') switchView('settings');
      else switchView(v);
      return;
    }
  });

  document.addEventListener('mousedown', (e) => {
    if (!S.spkPop) return;
    if (e.target.closest('#spkPop') || e.target.closest('[data-spk-id]') ||
        e.target.closest('[data-spk-act]')) return;
    closeSpeakerPop();
  });

  $('#btnAdd').onclick = () => { S.addOpen ? closeAdd() : openAdd(); };
  $('#emptyAdd').onclick = () => { switchView('listen'); openAdd(); };
  $('#addGo').onclick = addRoom;
  $('#addCancel').onclick = closeAdd;
  $('#addInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addRoom(); }
    if (e.key === 'Escape') { e.preventDefault(); closeAdd(); }
  });

  $('#sideTools').addEventListener('click', (e) => {
    const toggle = e.target.closest('[data-batch-toggle]');
    if (toggle) {
      S.batchMode = !S.batchMode;
      S.batchSel.clear();
      renderSidebar();
      return;
    }
    if (e.target.closest('[data-batch-all]')) {
      if (S.batchSel.size === S.sessions.length) S.batchSel.clear();
      else S.sessions.slice(0, 100).forEach((s) => S.batchSel.add(s.id));
      renderSidebar();
      return;
    }
    const b = e.target.closest('[data-filter]');
    if (!b) return;
    S.filter = b.dataset.filter;
    renderSidebar();
  });
  $('#sideList').addEventListener('click', (e) => {
    const play = e.target.closest('[data-play]');
    if (play) { e.preventDefault(); e.stopPropagation(); openWorkbenchPlay(play.dataset.play); return; }
    const more = e.target.closest('[data-more]');
    if (more) {
      e.preventDefault(); e.stopPropagation();
      const rect = more.getBoundingClientRect();
      if (more.dataset.more === 'session') sessionMenu(more.dataset.id, rect.left, rect.bottom + 4);
      else roomMenu(more.dataset.id, rect.left, rect.bottom + 4);
      return;
    }
    const demo = e.target.closest('[data-demo]');
    if (demo) { e.preventDefault(); e.stopPropagation(); loadDemo(); return; }
    const r = e.target.closest('[data-room]');
    if (r) return selectRoom(r.dataset.room);
    const s = e.target.closest('[data-session]');
    if (s) {
      if (S.batchMode && S.view === 'workbench') {
        if (S.batchSel.has(s.dataset.session)) S.batchSel.delete(s.dataset.session);
        else S.batchSel.add(s.dataset.session);
        renderSidebar();
        return;
      }
      S.selectedSession = s.dataset.session;
      switchView('workbench', { session: S.selectedSession });
    }
  });
  $('#sideList').addEventListener('contextmenu', (e) => {
    const s = e.target.closest('[data-session]');
    const r = e.target.closest('[data-room]');
    if (!s && !r) return;
    e.preventDefault();
    e.stopPropagation();
    if (s) sessionMenu(s.dataset.session, e.clientX, e.clientY);
    else roomMenu(r.dataset.room, e.clientX, e.clientY);
  });
  $('#sideFoot').addEventListener('click', (e) => {
    if (e.target.closest('[data-clear-stopped]')) clearStoppedRooms();
    if (e.target.closest('[data-open-trash]')) switchView('files', { tab: 'trash' });
    if (e.target.closest('[data-batch-trash]')) confirmBatchTrash();
    if (e.target.closest('[data-batch-export]')) batchExport();
    if (e.target.closest('[data-batch-cancel]')) {
      S.batchMode = false; S.batchSel.clear(); renderSidebar();
    }
  });
  $('#wall').addEventListener('click', (e) => {
    if (e.target.closest('[data-add]')) { switchView('listen'); openAdd(); return; }
    const r = e.target.closest('[data-room]');
    if (!r) return;
    S.stage = 'signal';
    selectRoom(r.dataset.room);
    renderStage();
  });

  $('#stageSeg').addEventListener('click', (e) => {
    const b = e.target.closest('[data-stage]');
    if (!b) return;
    setStage(b.dataset.stage);
  });
  $('#btnWall').onclick = () => {
    if (S.stage === 'wall') setStage('signal');
    else setStage('wall');
  };
  $('#btnStop').onclick = () => confirmStopRoom(selectedRoom());
  $('#stageRetry').onclick = () => retryRoom(selectedRoom());

  // 实时字幕 / 标注：点说话人认人、切筛选、自动滚动
  $('#txTabs').addEventListener('click', (e) => {
    const b = e.target.closest('[data-tab]');
    if (!b) return;
    S.annotTab = b.dataset.tab;
    renderAnnot();
  });
  $('#txAuto').addEventListener('click', () => {
    S.annotAuto = !S.annotAuto;
    $('#txAuto').classList.toggle('on', S.annotAuto);
    if (S.annotAuto) {
      const sc = $('#txScroll');
      sc.scrollTop = sc.scrollHeight;
    }
  });
  $('#txScroll').addEventListener('click', (e) => {
    const chip = e.target.closest('.spk[data-spk-id]');
    if (!chip) return;
    e.preventDefault();
    openSpeakerPop(chip.dataset.spkId, chip);
  });
  $('#txScroll').addEventListener('scroll', () => {
    const sc = $('#txScroll');
    const near = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 48;
    if (near !== S.annotAuto) {
      S.annotAuto = near;
      $('#txAuto').classList.toggle('on', near);
    }
  });
  $('#txBanner').addEventListener('click', (e) => {
    const b = e.target.closest('[data-spk-act]');
    if (!b) return;
    handleSpkAction(b.dataset.spkAct, b.dataset.spk, b);
  });
  $('#spkPop').addEventListener('click', (e) => {
    const b = e.target.closest('[data-target],[data-target-confirm],[data-new],[data-role],[data-reject]');
    if (!b || !S.spkPop) return;
    const cur = S.spkPop.id;
    if (b.dataset.target) {
      const t = spkById(b.dataset.target);
      if (t) mergeSpeakerInto(cur, t);
      return;
    }
    if (b.dataset.targetConfirm) { confirmSpeaker(cur); return; }
    if (b.dataset.new) { promptSpeakerName(cur); return; }
    if (b.dataset.role) { renameSpeaker(cur, b.dataset.role); return; }
    if (b.dataset.reject) { rejectSpeaker(cur); return; }
  });
  $('#inspScroll').addEventListener('click', (e) => {
    const b = e.target.closest('[data-spk-act]');
    if (!b) return;
    handleSpkAction(b.dataset.spkAct, b.dataset.spk, b);
  });
  $('#inspActions').addEventListener('click', (e) => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const room = selectedRoom();
    const act = b.dataset.act;
    if (act === 'page') return setStage(S.stage === 'page' ? 'signal' : 'page');
    if (act === 'live' && room && room.session_id) return switchView('live', { session: room.session_id, back: 'listen' });
    if (act === 'retry') return retryRoom(room);
    if (act === 'remove') return confirmRemoveRoom(room);
    if (act === 'stop') return $('#btnStop').click();
  });
  $('#frameBack').onclick = () => switchView(S.frameBackView || 'listen');
  $('#frame').addEventListener('load', () => {
    S.frameLoads += 1;
    if (S.view === 'workbench' && S.frameKind === 'workbench') S.workbenchReady = true;
  });
  $('#frameRaw').onclick = () => {
    S.frameRaw = !S.frameRaw;
    $('#frameRaw').textContent = S.frameRaw ? '收起工具栏' : '原工具栏';
    $('#frame').src = S.frameRaw ? S.frameRawUrl : S.frameEmbedUrl;
  };

  $('#cmdkBtn').onclick = openPalette;
  $('#btnSettings').onclick = () => switchView('settings');
  $('#recPill').onclick = () => {
    switchView('listen');
    setStage(S.stage === 'wall' ? 'signal' : 'wall');
  };
  $('#palette').addEventListener('click', (e) => { if (e.target === $('#palette')) closePalette(); });
  $('#pInput').addEventListener('input', () => { S.paletteIndex = 0; renderPalette(); });
  $('#pInput').addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); S.paletteIndex = Math.min(S.paletteActions.length - 1, S.paletteIndex + 1); renderPalette(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); S.paletteIndex = Math.max(0, S.paletteIndex - 1); renderPalette(); }
    else if (e.key === 'Enter') { e.preventDefault(); runPalette(S.paletteIndex); }
    else if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
  });
  $('#pList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-pi]');
    if (b) runPalette(Number(b.dataset.pi));
  });

  document.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      S.paletteOpen ? closePalette() : openPalette();
      return;
    }
    if (mod && ['1', '2', '3', '4', '5'].includes(e.key)) {
      e.preventDefault();
      const v = ['listen', 'workbench', 'files', 'people', 'settings'][Number(e.key) - 1];
      switchView(v);
      return;
    }
    const tag = (e.target && e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || tag === 'select' ||
      (e.target && e.target.isContentEditable);
    if (!typing && !mod && S.view === 'listen' && S.stage === 'annotate') {
      if (e.key === '[') { e.preventDefault(); setPane('side', !S.panes.side); return; }
      if (e.key === ']') { e.preventDefault(); setPane('insp', !S.panes.insp); return; }
      if (e.key === '\\') { e.preventDefault(); setPane('tx', !S.panes.tx); return; }
      if (e.key === 'm' || e.key === 'M') { e.preventDefault(); markMoment(); return; }
      if (e.code === 'Space' || e.key === ' ') { e.preventDefault(); togglePause(); return; }
    }
    if (e.key === 'Escape') {
      if (S.spkPop) { closeSpeakerPop(); return; }
      if (S.paletteOpen) { closePalette(); return; }
      if (S.modalOpen) { closeModal(); return; }
      if (S.addOpen) { closeAdd(); return; }
    }
  });

  window.addEventListener('message', (e) => {
    if (ENGINE_ORIGIN && e.origin !== ENGINE_ORIGIN) return;
    const d = e.data || {};
    if (d.type === 'ln-key' && d.key === 'k') { e.preventDefault?.(); S.paletteOpen ? closePalette() : openPalette(); }
    else if (d.type === 'ln-key' && ['1', '2', '3', '4', '5'].includes(String(d.key))) {
      switchView(['listen', 'workbench', 'files', 'people', 'settings'][Number(d.key) - 1]);
    } else if (d.type === 'ln-navigate' && d.href) {
      navigateToUrl(d.href);
    } else if (d.type === 'ln-session-changed' && d.session) {
      // 工作台内部换了场次：侧栏跟着选中，但不再重载 iframe。
      S.selectedSession = String(d.session);
      S.workbenchSid = String(d.session);
      if (S.view === 'workbench') renderSidebar();
    } else if (d.type === 'ln-ready') {
      if (S.view === 'workbench') {
        S.workbenchReady = true;
        if (d.session) {
          S.selectedSession = String(d.session);
          S.workbenchSid = String(d.session);
          renderSidebar();
        }
      }
    }
  });

  // 无边框窗口：自绘标题栏按钮。没有 Tauri（网页预览）时整组隐藏。
  const win = (TAURI && TAURI.window && TAURI.window.getCurrentWindow)
    ? TAURI.window.getCurrentWindow() : null;
  if (!win) {
    const box = $('.win-controls');
    if (box) box.hidden = true;
  } else {
    const on = (sel, fn) => { const b = $(sel); if (b) b.addEventListener('click', fn); };
    on('#winMin', () => win.minimize());
    on('#winMax', () => win.toggleMaximize());
    // 关闭按钮走壳内确认：有录音时先问，没录音按设置走托盘或退出。
    on('#winClose', () => requestClose());
    const bar = $('.topbar');
    if (bar) bar.addEventListener('dblclick', (e) => {
      if (e.target.closest('button,input,.cmdk')) return;
      if (e.target.closest('[data-tauri-drag-region]')) win.toggleMaximize();
    });
  }

  window.addEventListener('resize', () => { resizeScope(); applyAnnotLayout(); scheduleStageLayout(); });
  if (window.ResizeObserver) {
    new ResizeObserver(() => { resizeScope(); applyAnnotLayout(); scheduleStageLayout(); }).observe($('#stageWrap'));
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { resizeScope(); refreshRooms(); }
  });

  if (TAURI && TAURI.event && TAURI.event.listen) {
    TAURI.event.listen('shell_navigate', (e) => {
      if (e && e.payload) navigateToUrl(String(e.payload));
    });
    TAURI.event.listen('shell_show_listen', () => switchView('listen'));
    TAURI.event.listen('shell_close_requested', () => {
      invoke('shell_close_ack').catch(() => {});
      requestClose();
    });
    TAURI.event.listen('shell_quit_requested', (e) => {
      requestQuit(Number(e && e.payload) || 0);
    });
    TAURI.event.listen('shell_stop_all', () => stopAll());
  }
}

/* ================================================================ init */
// 自动化验收用的最小控制面。只在桌面壳里存在，网页端不会加载这份脚本。
window.__LN_SHELL__ = {
  state() {
    return {
      view: S.view, stage: S.stage, online: S.online,
      rooms: S.rooms.length, live: liveRooms().length,
      selected: S.selectedId, pageOpen: S.pageOpen,
      lines: S.lines.length, sessions: S.sessions.length,
      speakers: annotSpeakers().length, claimed: annotStats().claimed,
      todo: annotStats().todo, annotTab: S.annotTab, annotAuto: S.annotAuto,
      selectedSession: S.selectedSession, workbenchSid: S.workbenchSid,
      workbenchReady: S.workbenchReady, frameLoads: S.frameLoads,
      trashCount: S.trashCount, batchMode: S.batchMode,
      batchSelected: S.batchSel.size,
    };
  },
  report(tag) {
    const detail = JSON.stringify(this.state());
    return invoke('spike_report', { tag: tag || 'shell', detail })
      .catch(() => null);
  },
  view(name) { switchView(name); return name; },
  listen() { switchView('listen'); },
  select(i) {
    const r = S.rooms[i];
    if (r) selectRoom(r.id);
    return r ? r.id : null;
  },
  wall() { switchView('listen'); setStage('wall'); },
  signal() { switchView('listen'); setStage('signal'); },
  annotate() { switchView('listen'); return setStage('annotate'); },
  stage(mode) { return setStage(mode); },
  speakers() { return annotSpeakers(); },
  palette() { openPalette(); },
  paletteClose() { closePalette(); },
  page() { return openStagePage(); },
  pageClose() { return closeStageView(); },
  pageUrl(url) {
    const r = selectedRoom();
    if (r) { r.url = url; r.resolved_url = ''; renderStage(); }
    return r ? r.id : null;
  },
  add() { switchView('listen'); openAdd(); },
  filter(f) { S.filter = f; renderSidebar(); },
  stopModal() { $('#btnStop').click(); },
  stopModalClose() { closeModal(); },
  requestClose() { requestClose(); },
  closeAction() { return S.closeAction; },
  workbench(sid) { switchView('workbench', { session: sid || S.selectedSession }); },
  live(sid) { switchView('live', { session: sid || '', back: 'workbench' }); },
  frame(url, title) { switchView('frame', { url, title: title || '页面', back: 'workbench' }); },
};
/* ================================================================ 面板布局
   标注工作区按工作台式面板组织：房间列表、直播画面、实时字幕、本场声音
   各自可开可关；画面与字幕之间可拖高，任一可放大。原生子视图只跟着
   「直播画面」这块矩形走，关掉或放大字幕时直接隐藏。 */
const PANE_KEY = 'ln.shell.panes.v1';

function loadPanes() {
  try {
    const v = JSON.parse(localStorage.getItem(PANE_KEY) || 'null');
    if (!v) return;
    if (v.panes) Object.assign(S.panes, v.panes);
    if (typeof v.liveH === 'number') S.annotLiveH = clamp(v.liveH, 0.24, 0.82);
    if (typeof v.max === 'string') S.annotMax = v.max;
  } catch (e) {}
}
function savePanes() {
  try {
    localStorage.setItem(PANE_KEY, JSON.stringify({
      panes: S.panes, liveH: S.annotLiveH, max: S.annotMax,
    }));
  } catch (e) {}
}
function applyPaneButtons() {
  const body = $('#body');
  // 房间列表 / 本场声音的开关状态以实际布局为准：别的视图可能本来就收起。
  const actual = {
    side: !body.classList.contains('no-side'),
    insp: !body.classList.contains('no-insp'),
    live: S.panes.live,
    tx: S.panes.tx,
  };
  $$('#paneTools [data-pane]').forEach((b) => {
    const on = !!actual[b.dataset.pane];
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
}
function applyBodyPanes() {
  if (S.view !== 'listen') return;
  const body = $('#body');
  body.classList.toggle('no-side', !S.panes.side);
  body.classList.toggle('no-insp', !S.panes.insp);
}
/* 画面这块当前该不该露出来：位置模式、面板开关、放大状态、弹层，四者取与。 */
function stageWantsVisible() {
  const layoutOk = S.stage === 'page'
    ? true
    : (S.stage === 'annotate' && S.panes.live && S.annotMax !== 'tx');
  return layoutOk && S.overlayDepth === 0;
}
async function syncStageVisible() {
  if (!S.stageLabel) return;
  const want = stageWantsVisible();
  if (want === !S.pageHidden) return;
  S.pageHidden = !want;
  try { await invoke('room_show', { label: S.stageLabel, visible: want }); } catch (e) {}
  if (want) layoutStageView();
}
function applyAnnotLayout() {
  const annot = $('#annot');
  if (!annot || annot.hidden) return;
  const h = annot.clientHeight || 0;
  const liveOn = S.panes.live && S.annotMax !== 'tx';
  const txOn = S.panes.tx && S.annotMax !== 'live';
  const split = liveOn && txOn && !S.annotMax;
  const liveRow = !liveOn ? '0px'
    : S.annotMax === 'live' ? 'minmax(0,1fr)'
    : Math.round(h * S.annotLiveH) + 'px';
  annot.style.gridTemplateRows = liveRow + ' ' + (split ? '5px' : '0px') +
    ' ' + (txOn ? 'minmax(0,1fr)' : '0px');
  annot.dataset.live = liveOn ? 'on' : 'off';
  annot.dataset.tx = txOn ? 'on' : 'off';
  const empty = $('#annotEmpty');
  if (empty) empty.hidden = liveOn || txOn;
  $$('.apane-btn[data-pane-act="max"]').forEach((b) => {
    const on = S.annotMax === b.dataset.pane;
    b.classList.toggle('on', on);
    b.title = (on ? '还原' : '放大') + (b.dataset.pane === 'live' ? '画面' : '字幕');
  });
  syncStageVisible();
}
function setPane(k, on) {
  if (!(k in S.panes)) return;
  // 在别的页面点面板开关，先回到收听工作区再改。
  if (S.view !== 'listen' && (k === 'side' || k === 'insp')) switchView('listen');
  S.panes[k] = !!on;
  if (!on && S.annotMax === k) S.annotMax = '';
  applyBodyPanes();
  applyPaneButtons();
  applyAnnotLayout();
  savePanes();
  if (S.stage === 'annotate' && S.panes.live && S.annotMax !== 'tx' && !S.stageLabel) ensureAnnotPage();
}
function toggleAnnotMax(k) {
  if (k !== 'live' && k !== 'tx') return;
  S.annotMax = S.annotMax === k ? '' : k;
  if (S.annotMax) S.panes[k] = true;
  applyPaneButtons();
  applyAnnotLayout();
  savePanes();
  if (S.stage === 'annotate' && S.panes.live && S.annotMax !== 'tx' && !S.stageLabel) ensureAnnotPage();
}
function initPanes() {
  loadPanes();
  applyBodyPanes();
  applyPaneButtons();
  $$('#paneTools [data-pane]').forEach((b) => {
    b.addEventListener('click', () => setPane(b.dataset.pane, !S.panes[b.dataset.pane]));
  });
  const sideClose = $('#btnSideClose');
  if (sideClose) sideClose.addEventListener('click', () => setPane('side', false));
  const inspClose = $('#btnInspClose');
  if (inspClose) inspClose.addEventListener('click', () => setPane('insp', false));
  $$('.apane-btn[data-pane-act="max"]').forEach((b) => {
    b.addEventListener('click', () => toggleAnnotMax(b.dataset.pane));
  });
  $$('.apane-btn[data-pane-act="close"]').forEach((b) => {
    b.addEventListener('click', () => setPane(b.dataset.pane, false));
  });

  const split = $('#asplit');
  if (split) {
    split.addEventListener('pointerdown', (e) => {
      const annot = $('#annot');
      if (!annot || S.annotMax) return;
      const total = annot.clientHeight || 1;
      const startY = e.clientY;
      const startH = total * S.annotLiveH;
      let raf = 0;
      e.preventDefault();
      document.body.classList.add('splitting');
      const move = (ev) => {
        const h = clamp(startH + ev.clientY - startY, 150, Math.max(180, total - 170));
        S.annotLiveH = h / total;
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          annot.style.gridTemplateRows = Math.round(h) + 'px 5px minmax(0,1fr)';
          layoutStageView();
        });
      };
      const up = () => {
        document.body.classList.remove('splitting');
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        savePanes();
        layoutStageView();
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    });
    split.addEventListener('dblclick', () => {
      S.annotLiveH = 0.52;
      applyAnnotLayout();
      layoutStageView();
      savePanes();
    });
  }
}

function init() {
  initContextMenu();
  initPanes();
  bind();
  resizeScope();
  startScope();
  renderAll();
  switchView('listen');
  refreshRooms();
  fetchCloseAction();
  loadSessions();
  setTimeout(pollRoomsLoop, 300);
  setTimeout(pollTranscriptLoop, 900);
  setTimeout(pollSessionsLoop, 15000);
}
init();
