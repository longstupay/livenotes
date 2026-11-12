// 由 app/web/rooms.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { STAGES, clamp, num, state } from './core.js';
import { render } from './render.js';
  function isLive(r){ return r.state==="live"; }
  function isListening(r){ return r.state==="live" && !!r.session && r.session.running!==false; }
  function needsDiag(r){
    return r.state==="pending" || r.state==="starting" || r.state==="failed"
      || (r.state==="live" && (!r.session || r.session.running===false));
  }
  function browserMeta(r){
    if(r.state==="pending") return {label:"未启动",tone:"muted"};
    if(r.state==="stopped") return {label:"已关闭",tone:"muted"};
    if(r.capture_engine==="direct"){
      // 直取不需要浏览器；失败时浏览器也确实没起来，别显示成「已退出」。
      return r.state==="failed"
        ? {label:"未启动（直取失败）",tone:"bad"}
        : {label:"不需要（直取）",tone:"muted"};
    }
    if(r.state==="failed") return {label:"已退出",tone:"bad"};
    const b = r.browser;
    if(!b || b.at==null) return {label:"未上报",tone:"muted"};
    if(b.alive===false) return {label:b.reason ? "已退出" : "已退出",tone:"bad"};
    const age = num(b.age_s);
    if(age > 20) return {label:"心跳超时 "+Math.round(age)+" 秒",tone:"warn"};
    return {label:"运行中",tone:"ok"};
  }
  // 采集引擎标签：direct=ffmpeg 直取，shared=一个 Edge 多标签，per_room=每房间浏览器。
  // 空值表示采集端还没上报（老数据/刚添加），不显示标签。
  const ENGINE_LABEL={direct:"直取",shared:"共享浏览器",per_room:"独立浏览器"};
  function engineLabel(r){ return ENGINE_LABEL[String(r.capture_engine||"")]||""; }
  // 直取失败后「可以切到哪个浏览器」：采集代理已经探好，前端只负责显示。
  const SWITCH_LABEL={shared:"共享浏览器",per_room:"独立浏览器"};
  function switchEngineLabel(engine){ return SWITCH_LABEL[String(engine||"")]||"浏览器"; }
  function statusMeta(r){
    if(r.state==="failed"){
      // 直取失败不是普通失败：不动的话这一路永远好不了，必须让人看到并确认切换。
      return r.switchReady
        ? {key:"failed",label:"直取失败",tone:"bad",dot:"failed"}
        : {key:"failed",label:"失败",tone:"bad",dot:"failed"};
    }
    if(r.state==="pending") return {key:"pending",label:"待启动",tone:"idle",dot:""};
    if(r.state==="starting") return {key:"starting",label:"启动中",tone:"warn",dot:"starting"};
    if(r.state==="stopped") return r.interrupted
      // 服务重启收口的：不是用户停的，也不是正常下播，值得看一眼。
      ? {key:"interrupted",label:"已中断",tone:"warn",dot:"starting"}
      : {key:"stopped",label:"已停止",tone:"idle",dot:""};
    const bm = browserMeta(r);
    if(r.state==="live" && bm.tone==="bad") return {key:"no-browser",label:"浏览器已退出",tone:"bad",dot:"failed"};
    if(!r.session) return {key:"missing",label:"会话丢失",tone:"bad",dot:"failed"};
    if(r.session.running===false){
      return r.hasRecording
        ? {key:"ended",label:"会话已结束",tone:"warn",dot:"starting"}
        : {key:"ended",label:"会话已结束",tone:"bad",dot:"failed"};
    }
    if(r.session.paused) return {key:"paused",label:"已暂停",tone:"warn",dot:"starting"};
    // 安静（idle_s）是「没人说话」；断流看的是音频块本身，两者不能混。
    // 会话建了 15 秒还没收到过任何一块，也算没在入库。
    const ca = r.session.last_chunk_ago_s;
    if((ca != null && num(ca) >= 15) || (ca == null && num(r.session.elapsed) >= 15))
      return {key:"stalled",label:"断流",tone:"bad",dot:"failed"};
    if(num(r.session.idle_s) >= 15) return {key:"quiet",label:"安静",tone:"warn",dot:"starting"};
    return {key:"live",label:"在听",tone:"ok",dot:"live"};
  }
  function ingestMeta(r){
    if(r.state==="stopped") return r.hasRecording ? {label:"已保存",tone:"saved"} : {label:"无录音",tone:"none"};
    if(r.state==="failed" || r.state==="pending" || r.state==="starting") return {label:"—",tone:"none"};
    if(!r.session) return {label:"未入库",tone:"bad"};
    if(r.session.running===false) return r.hasRecording ? {label:"已保存",tone:"saved"} : {label:"未入库",tone:"bad"};
    if(r.session.paused) return {label:"已暂停",tone:"warn"};
    const fa = r.session.last_feed_ago_s;
    if(fa != null && num(fa) <= 15) return {label:"已入库",tone:"ok"};
    if(fa != null) return {label:"未入库",tone:"bad"};
    if(r.session.last_chunk_ago_s == null && num(r.session.elapsed) >= 15)
      return {label:"未入库",tone:"bad"};
    return {label:"等待入库",tone:"warn"};
  }
  function lastChunkAgo(s){ return (s && s.last_chunk_ago_s != null) ? num(s.last_chunk_ago_s) : null; }
  function agoText(sec){ return sec < 1 ? "刚刚" : ago(sec); }
  function stageIndex(r){
    if(r.state==="pending") return 0;
    if(r.state==="starting") return clamp(num(r.stage) || 1, 1, 5);
    if(r.state==="failed"){
      // 没上报过 failed_stage 的旧记录不猜：返回 -1，由 UI 如实写「未上报」。
      return r.failStage == null ? -1 : clamp(num(r.failStage), 0, 5);
    }
    if(r.state==="live" && !r.session) return 4;
    if(r.state==="live" && r.session && r.session.running===false) return 5;
    if(r.state==="stopped" && !r.sessionId) return 0;
    return 6;
  }
  function stageLabel(r){
    const at = stageIndex(r);
    return (at >= 0 && STAGES[at]) ? STAGES[at].label : "未上报";
  }
  function stepState(r,i){
    const at = stageIndex(r);
    if(r.state==="stopped") return r.sessionId ? "done" : (i===0 ? "done" : "pending");
    const broken = r.state==="failed" || (r.state==="live" && !r.session);
    if(broken){
      if(at < 0) return "pending";
      if(i < at) return "done";
      if(i === at) return "failed";
      return "pending";
    }
    if(r.state==="pending" || r.state==="starting"){
      if(i < at) return "done";
      if(i === at) return "current";
      return "pending";
    }
    return "done";
  }
  function stageNote(r,i){
    if(r.state==="failed"){
      if(i===stageIndex(r)) return r.error || "这一步失败了";
      if(stageIndex(r)<0 && i===0) return "旧记录没有上报失败步骤，原因见下方";
    }
    if(r.state==="live" && !r.session && i===4) return "没有拿到 session_id";
    if(r.state==="live" && r.session && r.session.running===false && i===5)
      return r.hasRecording ? "会话已结束 · 录音已保存" : "会话已结束 · 音频没有入库";
    if(r.state==="stopped"){
      if(!r.sessionId){
        if(i===0) return "链接校验通过";
        return "没有会话记录";
      }
      if(i===5) return r.interrupted
        ? (r.hasRecording ? "服务重启中断 · 录音已保存" : "服务重启中断 · 没有录到音频")
        : (r.hasRecording ? "已停止 · 录音已保存" : "已停止 · 没有录到音频");
    }
    if(r.state==="pending" && i===0) return "等待采集代理接单";
    if(r.state==="starting" && i===stageIndex(r)) return r.stageNote || "正在处理…";
    if(r.state==="live" && i===5 && r.session){
      const a = lastChunkAgo(r.session);
      return a==null ? "等待第一块音频" : "最近一块 "+agoText(a);
    }
    return STAGES[i].note;
  }
  function ago(sec){
    sec = Math.max(0, Math.round(num(sec)));
    if(sec < 60) return sec + " 秒前";
    if(sec < 3600) return Math.floor(sec/60) + " 分钟前";
    return Math.floor(sec/3600) + " 小时前";
  }
  function abnormalRooms(){
    return state.rooms.filter(r=>{
      const s = statusMeta(r);
      return s.tone==="bad" || s.tone==="warn" || r.state==="failed" || r.state==="pending" || r.state==="starting";
    });
  }
  function diagRooms(){ return state.rooms.filter(needsDiag); }
  function alertKey(){
    return state.rooms.map(r=>{
      const st = statusMeta(r);
      return r.id+":"+r.state+":"+st.key+":"+(r.session?"1":"0")
        +":"+(r.session&&r.session.running===false?"ended":"")
        +":"+(r.failStage==null?"":r.failStage)+":"+(r.stage||"")+":"+r.hasRecording
        +":"+(r.interrupted?"int":"")
        +":"+(r.switchReady?"sw":"")+":"+(r.switchRequested?"swq":"")
        +":"+(r.browser?(r.browser.alive?1:0):"")+":"+(r.error||"");
    }).join("|");
  }
  function lastLine(r){
    if(r.session && r.session.last_line) return r.session.last_line;
    if(r.state==="live" && r.session && r.session.running===false)
      return r.hasRecording ? "会话已结束，录音已保存。" : "会话已结束，音频没有入库。";
    if(r.switchRequested) return "已确认切换，等待采集代理启动浏览器…";
    if(r.error) return r.switchReady ? r.error + " · 需要你确认切换" : r.error;
    if(r.state==="pending") return "等待采集代理启动浏览器…";
    if(r.state==="starting") return r.stageNote ? "启动中 · "+r.stageNote : "正在打开直播间并取音频…";
    if(r.state==="stopped"){
      if(r.sessionId) return r.hasRecording ? "已停止，录音已保存。" : "已停止，没有录到音频。";
      return "已停止，没有建立会话。";
    }
    if(r.state==="live" && !r.session) return "会话丢失，正在等采集端重新建立…";
    return "还没有字幕。";
  }
  function lineSpeaker(r){
    const subs = r.subtitles || [];
    const last = subs[subs.length-1];
    return last && last.speaker ? last.speaker : "";
  }
  function levelOf(r){
    if(r.state!=="live" || !r.session) return 0;
    if(r.session.paused) return 0;
    const v = num(r.session.level);
    return clamp(v*14, .02, 1);
  }

  /* ================================================================ render: top */

export { abnormalRooms, ago, agoText, alertKey, browserMeta, diagRooms, engineLabel, ingestMeta, isListening, isLive, lastChunkAgo, lastLine, levelOf, lineSpeaker, needsDiag, stageIndex, stageLabel, stageNote, statusMeta, stepState, switchEngineLabel };
