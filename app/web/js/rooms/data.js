// 由 app/web/rooms.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { toast } from './actions.js';
import { num, pad, state } from './core.js';
import { lastLine, stageNote } from './derived.js';
import { openDetail, renderDetail } from './detail.js';
import { render, roomById } from './render.js';
  async function fetchJson(url){
    const r = await fetch(url,{cache:"no-store"});
    if(!r.ok) throw new Error("HTTP "+r.status);
    return r.json();
  }
  async function postJson(url, body){
    const r = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body)});
    const j = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(j.error || ("HTTP "+r.status));
    return j;
  }
  function normalizeRealRoom(room, session, capture){
    const host = (()=>{ try{ return new URL(room.url).host; }catch(e){ return ""; } })();
    const error = room.error || "";
    const stage = Number(room.stage);
    const fs = (room.failed_stage === null || room.failed_stage === undefined)
      ? null : Number(room.failed_stage);
    return {
      id: room.id,
      ch: "",
      order: 0,
      // 后端 seq 是稳定编号；老记录没有 seq 时前端才退回按顺序编号。
      seq: Number(room.seq) || 0,
      name: room.name || host || "（未命名）",
      url: room.url || "",
      host,
      state: room.state || "pending",
      sessionId: room.session_id || "",
      session: session ? {
        elapsed: session.elapsed, n_utterances: session.n_utterances,
        last_line: session.last_line || "", level: session.level,
        idle_s: session.idle_s, dropped: session.dropped, queued: session.queued,
        paused: session.paused, running: session.running,
        last_chunk_at: session.last_chunk_at, last_chunk_ago_s: session.last_chunk_ago_s,
        last_feed_at: session.last_feed_at, last_feed_ago_s: session.last_feed_ago_s,
        history: false
      } : null,
      error,
      stage: Number.isFinite(stage) ? stage : 0,
      failStage: Number.isFinite(fs) ? fs : null,
      stageNote: room.stage_note || "",
      saved: !!room.saved,
      hasRecording: !!room.has_recording,
      // 服务重启时被收口的房间：落成 stopped，但和用户主动停止要分开展示。
      interrupted: !!room.interrupted,
      // 采集代理通过 /api/rooms/state 上报的引擎（direct / shared / per_room），
      // 卡片、列表和详情靠它显示「直取 / 共享浏览器 / 独立浏览器」。
      capture_engine: room.capture_engine || "",
      // 直取失败后采集端亮起的「切换浏览器采集」入口；只有用户点了确认
      // （POST /api/rooms/switch）采集代理才会真的切过去。
      switchReady: !!room.switch_ready,
      switchEngine: room.switch_engine || "",
      switchReason: room.switch_reason || "",
      switchRequested: !!room.switch_requested,
      browser: capture || null,
      created_at: room.created_at || "",
      updated_at: room.updated_at || "",
      subtitles: []
    };
  }
  function applyHistory(r, h){
    if(!r || !h) return;
    r.session = Object.assign({}, h.session);
    r.subtitles = h.subtitles.slice();
    r.historyLoaded = true;
  }
  async function fetchReal(showToast){
    try{
      // 列表轮询每 2 秒整批重建房间对象，字幕却只由详情轮询
      // （/api/live/status，1.2 秒一次）写进 room.subtitles。这里先把上一份
      // 对象留住，重建后把同一个会话的字幕接回去；否则每次列表轮询都会把
      // 字幕清空，详情就会在「有字幕 / 还没有字幕」之间反复闪。
      const prevById = new Map(state.rooms.map(r=>[r.id,r]));
      let liveOk = true;
      const [roomsRes, liveRes, capRes] = await Promise.all([
        fetchJson("/api/rooms"),
        fetchJson("/api/live/list").catch(()=>{ liveOk = false; return {sessions:[]}; }),
        fetchJson("/api/capture/status").catch(()=>({rooms:{}}))
      ]);
      const sessions = liveRes.sessions || [];
      const bySid = new Map(sessions.map(s=>[s.id,s]));
      const caps = capRes.rooms || {};
      const rows = (roomsRes.rooms || []).map(r=>normalizeRealRoom(
          r, bySid.get(r.session_id), caps[r.id] || null))
        .sort((a,b)=>(a.state==="live"?0:1)-(b.state==="live"?0:1)
          || String(b.created_at||"").localeCompare(String(a.created_at||"")));
      rows.forEach((r,i)=>{ r.order=i; r.ch = r.seq ? pad(r.seq) : pad(i+1); });
      for(const r of rows){
        const prev = prevById.get(r.id);
        if(!prev) continue;
        // 只有同一个会话才继承：换了一场（session_id 变化）必须从空白开始。
        if(prev.sessionId && prev.sessionId === r.sessionId){
          r.subtitles = prev.subtitles || [];
          r.historyLoaded = !!prev.historyLoaded;
        }
        // /api/live/list 偶发失败时别立刻判「会话丢失」：沿用上一份 session，
        // 下一轮成功再以服务端为准。否则整页会闪一下失败态。
        if(!liveOk && prev.session && r.state==="live"){
          r.session = prev.session;
          r.sessionId = r.sessionId || prev.sessionId;
        }
      }
      // 历史字幕缓存在 state.history 里：每 2 秒重建 rooms 时把它贴回去，
      // 停止后的详情才不会闪一下又变回空白。
      for(const r of rows){
        const h = state.history[r.sessionId];
        if(h && (!r.session || r.state==="stopped" || r.state==="failed")) applyHistory(r,h);
      }
      state.rooms = rows;
      state.online = true;
      // 详情开着时房间才从 pending/failed 变成 live：补上字幕轮询，
      // 不用关掉重开详情。
      if(state.detailId){
        const d = roomById(state.detailId);
        if(d && d.state==="live" && d.sessionId && !state.detailTimer) startDetailPoll(d.id);
      }
      if(showToast) toast("已重新读取真实房间。");
      render();
      if(state.pendingDetail && roomById(state.pendingDetail)){
        const id = state.pendingDetail;
        state.pendingDetail = null;
        openDetail(id);
      }
    }catch(e){
      state.online = false;
      render();
      if(showToast) toast("连不上本地服务："+e.message);
    }
  }
  function startRealPoll(){
    stopRealPoll();
    fetchReal(false);
    state.realTimer = setInterval(()=>{
      if(document.hidden) return;
      fetchReal(false);
    }, 2000);
  }
  function stopRealPoll(){
    if(state.realTimer){ clearInterval(state.realTimer); state.realTimer=0; }
  }
  async function fetchDetailHistory(r){
    const sid = r && r.sessionId;
    if(!sid) return false;
    const cached = state.history[sid];
    if(cached){
      const cur = roomById(r.id) || r;
      applyHistory(cur, cached);
      if(state.detailId===cur.id) renderDetail();
      return true;
    }
    let rows = [], rd = null;
    try{
      const u = await fetchJson("/api/live/utterances?id="+encodeURIComponent(sid)+"&limit=20");
      rows = u.rows || [];
    }catch(e){ /* 逐字稿可能被删了，下面还有 reader 兜底 */ }
    try{
      rd = await fetchJson("/api/reader?session_id="+encodeURIComponent(sid));
    }catch(e){ /* 场次记录不在了就只能用逐字稿 */ }
    if(!rd && !rows.length) return false;
    const sess = (rd && rd.session) || {};
    const stats = (rd && rd.stats) || {};
    const lastRow = rows.length ? rows[rows.length-1] : null;
    const lastPara = (rd && rd.paragraphs && rd.paragraphs.length)
      ? rd.paragraphs[rd.paragraphs.length-1] : null;
    const lastLine = (lastRow && lastRow.text) || (lastPara && lastPara.text) || "";
    const h = {
      session: {
        elapsed: num(sess.duration_s) || (lastRow ? num(lastRow.t_end) : 0),
        n_utterances: num(stats.utterances),
        last_line: lastLine, level: 0, idle_s: 0, dropped: 0, queued: 0,
        paused: false, running: false, history: true,
        last_chunk_at: null, last_chunk_ago_s: null,
        last_feed_at: null, last_feed_ago_s: null
      },
      subtitles: rows.map(u=>({speaker:u.speaker_label||u.speaker||u.label||"说话人",
        text:u.text,t:u.t_start}))
    };
    state.history[sid] = h;
    // 这两秒里 fetchReal 可能已经重建了 state.rooms；历史要贴到当前对象上，
    // 否则 renderDetail 读到的还是没字幕的那一份。
    const cur = roomById(r.id) || r;
    applyHistory(cur, h);
    if(state.detailId===cur.id) renderDetail();
    return true;
  }
  async function fetchDetailReal(id){
    const r = roomById(id);
    if(!r || !r.sessionId) return;
    try{
      const st = await fetchJson("/api/live/status?id="+encodeURIComponent(r.sessionId));
      if(state.detailId!==id) return;
      const cur = roomById(id);
      if(!cur) return;
      cur.session = Object.assign({}, cur.session||{}, {
        elapsed: st.elapsed, n_utterances: st.n_utterances,
        last_line: (st.utterances&&st.utterances.length) ? st.utterances[st.utterances.length-1].text : (cur.session&&cur.session.last_line)||"",
        level: st.level, idle_s: st.idle_s, dropped: st.dropped, queued: st.queued,
        paused: st.paused, running: st.running,
        last_chunk_at: st.last_chunk_at, last_chunk_ago_s: st.last_chunk_ago_s,
        last_feed_at: st.last_feed_at, last_feed_ago_s: st.last_feed_ago_s,
        history: false
      });
      const finals = (st.utterances||[]).slice(-6).map(u=>({speaker:u.speaker_label||u.speaker||u.label||"说话人",text:u.text,t:u.t_start}));
      // 服务端偶尔会先返回空数组（刚建会话 / 采集端重连）。不要拿一次空响应
      // 把已经显示的字幕清掉：有句子才整体替换，空响应时保留上一份定稿。
      // 临时字幕不保留，这一轮没有就消失。
      const keep = finals.length ? finals : (cur.subtitles||[]).filter(s=>!s.partial);
      if(st.partial) keep.push({speaker:st.partial.speaker||"说话人",text:st.partial.text,partial:true});
      cur.subtitles = keep.slice(-6);
      if(state.detailId===id) renderDetail();
    }catch(e){
      // 内存里没有这场（停止 / 服务重启）：补历史字幕，而不是显示「没有建立会话」。
      const got = await fetchDetailHistory(r);
      const cur = roomById(id);
      if(got && cur && cur.state!=="live") stopDetailPoll();
    }
  }
  function startDetailPoll(id){
    stopDetailPoll();
    fetchDetailReal(id);
    state.detailTimer = setInterval(()=>{
      if(document.hidden || state.detailId!==id) return;
      fetchDetailReal(id);
    }, 1200);
  }
  function stopDetailPoll(){
    if(state.detailTimer){ clearInterval(state.detailTimer); state.detailTimer=0; }
  }

  /* ================================================================ view / search */

export { applyHistory, fetchDetailHistory, fetchDetailReal, fetchJson, fetchReal, normalizeRealRoom, postJson, startDetailPoll, startRealPoll, stopDetailPoll, stopRealPoll };
