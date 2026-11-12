# -*- coding: utf-8 -*-
"""零依赖 HTTP 服务（stdlib http.server + SQLite）。"""
from __future__ import annotations

import io
import json
import mimetypes
from html import escape as _esc
import socket
import struct
import threading
import time
import traceback
import wave
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import (analysis, analysis_job, audio, classify, coach, diagnostics, export,
               feedback, glossary, highlight, live, livebrief, llm, logs, metrics,
               polish, profile, reader, rooms, runtime, scheduler, seed_demo, settings,
               share, store, tasks, timefmt, titles, voiceprint, webroot)
from .config import AUDIO_DIR, HOST, LIVE_STALL_STOP_S, LIVE_STALL_WARN_S, PORT, WEB_DIR

TRASH_DIR = AUDIO_DIR.parent / "trash"
from .models import SOURCE_MODE_META, parse_ts


def _num(v: Any, default: float | None = None) -> float | None:
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (ValueError, TypeError):
        return parse_ts(str(v)) or default


def _flag(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes", "on")


def _list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        v = ",".join(str(x) for x in v)
    return [x.strip() for x in str(v).split(",") if x.strip()]


def parse_filters(d: dict) -> dict:
    return {
        "session_id": str(d.get("session_id") or ""),
        "t_from": _num(d.get("t_from")),
        "t_to": _num(d.get("t_to")),
        "categories": _list(d.get("categories")),
        "speakers": _list(d.get("speakers")),
        "q": str(d.get("q") or "").strip()[:120],
        "exclude_low_value": _flag(d.get("exclude_low_value")),
        "exclude_overlap": _flag(d.get("exclude_overlap")),
        "min_conf": _num(d.get("min_conf")),
    }


def _flat(q: dict[str, list[str]]) -> dict:
    return {k: v[0] for k, v in q.items() if v}


def _since_iso(v: Any) -> str | None:
    """把 ``since`` 收敛成库里同格式的 ISO 字符串：epoch 秒或 ISO 都收。"""
    if v in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(v)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        pass
    try:
        return datetime.fromisoformat(str(v)).isoformat(timespec="seconds")
    except ValueError:
        return None


def _metric_group(p: str) -> str:
    """请求指标的路径分组：避免 /api/audio?sid=... 这类参数撑爆基数。"""
    if p == "/api/live/chunk":
        return "live_chunk"
    if p == "/api/live/status":
        return "live_status"
    if p in ("/api/live/audio", "/api/live/audio/meta"):
        return "live_audio"
    if p.startswith("/api/"):
        return p if len(p) <= 48 else "api_other"
    if p.startswith("/static/"):
        return "static"
    return "page"


def _record_capture_event(rid: str, ev: dict) -> None:
    """采集代理上报的直取事件 -> 进程指标。事件很小，失败不能影响房间状态。"""
    kind = str(ev.get("kind") or "")
    try:
        if kind == "resolve_ok":
            metrics.METRICS.inc("capture.resolve_ok")
            metrics.METRICS.observe("capture.resolve_ms", float(ev.get("ms") or 0))
        elif kind == "resolve_failed":
            metrics.METRICS.inc("capture.resolve_failed")
        elif kind == "first_audio":
            metrics.METRICS.observe("capture.first_audio_ms",
                                    float(ev.get("ms") or 0))
        elif kind == "reconnect":
            metrics.METRICS.inc("capture.reconnects")
        elif kind == "fallback":
            metrics.METRICS.inc("capture.fallbacks")
            logs.log_event("capture.fallback", level="warn", room_id=rid,
                           reason=str(ev.get("reason") or "")[:200])
        elif kind == "switch_needed":
            # 直取失败但没自动切：等用户确认。和旧 fallback 分开计，排障时一眼看出
            # 是「用户没看到提示」还是「浏览器也起不来」。
            metrics.METRICS.inc("capture.switch_needed")
            logs.log_event("capture.switch_needed", level="warn", room_id=rid,
                           reason=str(ev.get("reason") or "")[:200],
                           engine=str(ev.get("engine") or ""))
        elif kind == "switch_confirmed":
            metrics.METRICS.inc("capture.switch_confirmed")
            logs.log_event("capture.switch_confirmed", room_id=rid,
                           reason=str(ev.get("reason") or "")[:200],
                           engine=str(ev.get("engine") or ""))
    except Exception:
        traceback.print_exc()


def _move_audio(sid: str, to_trash: bool) -> None:
    """软删除时把录音挪到 data/trash，还原时挪回来。挪不动就算了，不影响文字。"""
    src_dir, dst_dir = (AUDIO_DIR, TRASH_DIR) if to_trash else (TRASH_DIR, AUDIO_DIR)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for suffix in (".wav", ".pcm", ".m4a", ".opus"):
        src, dst = src_dir / (sid + suffix), dst_dir / (sid + suffix)
        if src.exists():
            try:
                src.replace(dst)
            except OSError:
                pass


def _stage(v: Any, default: int = 0) -> int:
    """启动阶段 1..6；0 = 还没上报。夹在范围内，防止脏数据把进度画飞。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(0, min(6, n))


def _has_recording(conn, sid: str) -> bool:
    """这一场到底有没有落下的录音（不看内存里的 session 对象）。

    停止后 session 会被回收，但数据库里的 audio_path 还在；删了录音但保留
    逐字稿时 audio_path 为 NULL，这里就返回 False。duration_s > 0 也不能
    当作有录音——那只能说明当时采到过音频，不代表文件还在。
    """
    if not sid:
        return False
    sess = store.get_session(conn, sid)
    if not sess:
        return False
    ap = sess.get("audio_path")
    if not ap:
        return False
    try:
        return Path(ap).stat().st_size > 0
    except OSError:
        return False


def _recording_pending(sid: str) -> bool:
    """停止请求刚到时文件还在写（pcm），此时 DB 里的 audio_path 还没落。"""
    if not sid:
        return False
    for ext in (".pcm", ".wav", ".m4a"):
        p = live.AUDIO_DIR / (sid + ext)
        try:
            if p.is_file() and p.stat().st_size > 0:
                return True
        except OSError:
            pass
    return False


def _close_room(conn, rid: str) -> dict | None:
    """停止一路采集：先落 stopped，再等会话收尾。

    顺序很重要：live.stop 要等 ASR 收尾，可能几十秒；这期间采集端还在送块，
    会话被移除后直取代理收到 404 会重建会话——旧顺序就会「点了停止还在录」。
    先改房间状态，代理下一拍就停 ffmpeg；收尾完再按真实文件校正 has_recording。
    """
    r = rooms.get(rid)
    if not r:
        return None
    sid = r.get("session_id") or ""
    _mark_room_stopped(conn, sid, rid, has_recording=_recording_pending(sid))
    if sid:
        try:
            live.stop(sid)
        except live.LiveError:
            pass
        except Exception:
            traceback.print_exc()
    return _mark_room_stopped(conn, sid, rid)


def _mark_room_stopped(conn, sid: str, room_id: str = "",
                       has_recording: bool | None = None) -> dict | None:
    """把房间落到 stopped，并把「录音是否真的存在」写成持久字段。

    session_id 故意保留：停止/重启之后，详情页还要用它去 /api/reader 和
    /api/live/utterances 补历史字幕，不能再靠内存里的 session 对象。
    """
    room = rooms.get(room_id) if room_id else rooms.get_by_session(sid)
    if not room:
        return None
    sid = sid or room.get("session_id") or ""
    if has_recording is None:
        has_recording = _has_recording(conn, sid)
    return rooms.update(
        room["id"], state="stopped", stage=6, failed_stage=None,
        saved=bool(sid), has_recording=bool(has_recording),
        interrupted=False,
        switch_ready=False, switch_engine="", switch_reason="",
        switch_requested=False)


# 上次进程退出时没走到 stopped，服务重启后统一收口成这个原因。
INTERRUPT_ERROR = "服务重启，采集已中断"


def reconcile_rooms(conn) -> int:
    """启动时把上次残留的活动状态落成「已中断」，只认用户显式重试。

    rooms.json 里的 pending/starting/live 只说明「上次那个进程还活着」，不是
    现在的事实：服务一重启，内存里的会话就没了。原样留着会被采集代理当成
    待办，第一拍就重新拉起浏览器/ffmpeg——用户明明关过服务，直播间却自己
    开了。所以启动时必须赶在 make_server 之前收口，让代理第一眼就看到
    stopped。

    不自动恢复是产品选择：显式停止、重启服务都算用户意图，高于「自动续采」。
    确实需要无人值守续采的机器用 LIVENOTES_RESUME_ROOMS=1 显式打开。
    """
    n = 0
    for r in rooms.list_rooms():
        if r.get("state") not in ("pending", "starting", "live"):
            continue
        sid = r.get("session_id") or ""
        has = _has_recording(conn, sid)
        if not sid:
            tail = "这一路还没建立会话；要继续请点「重试」"
        elif has:
            tail = "中断前的录音已保存；要继续请点「重试」"
        else:
            tail = "没有可用的录音；要继续请点「重试」"
        upd = rooms.update(
            r["id"], state="stopped", stage=6, failed_stage=None,
            stage_note="服务重启，采集已中断",
            error=f"{INTERRUPT_ERROR}。{tail}。",
            saved=bool(sid), has_recording=has, interrupted=True,
            switch_ready=False, switch_engine="", switch_reason="",
            switch_requested=False, auto_stopped=False, auto_stop_reason="")
        if upd:
            n += 1
            logs.log_event("rooms.interrupted", room=r["id"], session=sid,
                           has_recording=has)
    if n:
        print(f"[rooms] 上次退出时有 {n} 路没正常结束，已置为「已中断」", flush=True)
    return n


# ---------------------------------------------------------------- 断流看门狗
# 断流时采集页已经不在了，没人会调 /api/live/stop。不收尾的话这场会永远停在
# running：WAV 不落盘（只在 worker 退出时写）、没有总结、没有回顾，要等进程
# 重启被 recover_spools 才捞得回来。「无人值守过夜」就差这一步。
_STALL_LOCK = threading.Lock()
# 同一路房间同时点两次「开始听」/采集端与桌面壳同时建会话时，只允许一个会话。
_LIVE_START_LOCK = threading.Lock()
_STALL_CLOSING: set[str] = set()      # 正在收尾的会话，防止每轮重复触发
_WATCHDOG: threading.Thread | None = None


def _stall_reason(item: dict) -> str:
    if item.get("never"):
        return "建立会话后一直没有收到音频块（采集页没在推流）"
    return ("已经 %d 秒没收到音频块（不是没人说话；采集链路断了："
            "浏览器崩溃 / 页面被关 / 页面被冻结）" % int(item.get("idle_s") or 0))


def _warn_capture_lost(sid: str, item: dict) -> None:
    """块停超过预警阈值：先告警，不结束场次。"""
    idle = float(item.get("idle_s") or 0)
    room = rooms.get_by_session(sid)
    rid = (room or {}).get("id", "")
    if room and room.get("state") in ("live", "starting"):
        # 房间卡片上的「断流」由 last_chunk_ago_s 自动算；这里写一句更具体的原因，
        # 让详情页和事后排查能一眼看到是采集链路，而不是「没人说话」。
        rooms.update(rid, stage_note="采集断流：已经 %d 秒没收到音频块（不是没人说话）" % int(idle))
    try:
        live.push_capture_lost(sid, idle)
    except Exception:
        traceback.print_exc()
    logs.log_event("live.capture_lost_alert", level="warn", session_id=sid,
                   idle_s=round(idle, 1), room_id=rid)


def _watch_stalled_warn(warn_s: float | None = None) -> list[str]:
    """块停超过 warn_s 的场次先发一次飞书预警，不结束。返回本轮新告警的会话 id。"""
    limit = LIVE_STALL_WARN_S if warn_s is None else warn_s
    if limit <= 0:
        return []
    warned: list[str] = []
    for item in live.stalled_sessions(limit):
        sid = item["id"]
        # 同一场只告一次；feed() 收到下一块会把这个标志复位。
        if not live.mark_chunk_stall_warned(sid):
            continue
        warned.append(sid)
        threading.Thread(target=_warn_capture_lost, args=(sid, item),
                         name="watchdog-warn-" + sid, daemon=True).start()
    return warned


def _clear_recovered_notes() -> None:
    """块恢复后把房间上的「采集断流」提示换掉，别让一句话挂一整场。"""
    for room in rooms.list_rooms():
        if not str(room.get("stage_note") or "").startswith("采集断流"):
            continue
        sid = str(room.get("session_id") or "")
        if not sid:
            continue
        age = live.chunk_age(sid)
        if age is not None and age < 10:
            rooms.update(room["id"], stage_note="采集已恢复")


def _auto_close_session(sid: str, reason: str) -> None:
    """断流场次收尾：先把房间落到 stopped，再停会话（落盘 + 排队总结 + 发回顾）。

    顺序有讲究：采集页每 5 秒看一次房间状态，先落 stopped 它就会自己收手，
    不会在 stop 那几十秒里收到 404 又建出一个新会话。

    房间如果已经被采集页标成 failed，原因以它为准，只收尾场次、不改房间状态——
    那条失败原因比「断流」更有用，飞书也已经按 failed 通知过了。
    """
    room = rooms.get_by_session(sid)
    if not room:
        with _STALL_LOCK:
            _STALL_CLOSING.discard(sid)
        return
    print("[watchdog] %s 断流收尾 %s：%s" % (room["id"], sid, reason), flush=True)
    rid = room["id"]
    if room.get("state") in ("live", "starting"):
        rooms.update(rid, state="stopped", stage=6, failed_stage=None,
                     auto_stopped=True, auto_stop_reason=reason,
                     stage_note="断流自动收尾：" + reason, error=reason,
                     saved=False, has_recording=False)
    try:
        live.stop(sid)             # finalize=True：WAV、总结、本场回顾都走正常收尾
    except live.LiveError:
        pass                        # 会话对象已经没了（服务重启过），只剩房间要收
    except Exception:
        traceback.print_exc()
    # 停完才知道录音到底落没落盘，回填给收听页（"已保存 / 未入库"那一格）
    try:
        conn = store.connect()
        try:
            saved = _has_recording(conn, sid)
        finally:
            conn.close()
        rooms.update(rid, saved=saved, has_recording=saved)
    except Exception:
        traceback.print_exc()
    finally:
        with _STALL_LOCK:
            _STALL_CLOSING.discard(sid)


def _watch_stalled(idle_s: float | None = None) -> list[str]:
    """检查一轮，给断流的场次各起一个收尾线程。返回本轮触发的会话 id。"""
    limit = LIVE_STALL_STOP_S if idle_s is None else idle_s
    if limit <= 0:
        return []
    hit = live.stalled_sessions(limit)
    started: list[str] = []
    for item in hit:
        sid = item["id"]
        with _STALL_LOCK:
            if sid in _STALL_CLOSING:
                continue
            _STALL_CLOSING.add(sid)
        started.append(sid)
        threading.Thread(target=_auto_close_session, args=(sid, _stall_reason(item)),
                         name="watchdog-close-" + sid, daemon=True).start()
    return started


def start_watchdog(interval_s: float = 10.0) -> threading.Thread | None:
    """起一个常驻线程定期查断流；重复调用不会起第二个。"""
    global _WATCHDOG
    with _STALL_LOCK:
        if _WATCHDOG is not None and _WATCHDOG.is_alive():
            return _WATCHDOG

        def loop() -> None:
            while True:
                time.sleep(interval_s)
                try:
                    # 先预警，再收尾：30 秒发飞书，120 秒才落 stopped。
                    # 顺序反过来的话，已经到收尾阈值的场次会先被停掉，预警就发不出去了。
                    _watch_stalled_warn()
                    _watch_stalled()
                    _clear_recovered_notes()
                except Exception:
                    traceback.print_exc()

        _WATCHDOG = threading.Thread(target=loop, name="live-watchdog", daemon=True)
        _WATCHDOG.start()
        return _WATCHDOG


def _library_rows(conn, trash: bool) -> list[dict]:
    rows = store.list_trash(conn) if trash else store.list_sessions(conn)
    out = []
    for r in rows:
        ap = r.get("audio_path")
        size = 0
        if ap:
            try:
                size = Path(ap).stat().st_size
            except OSError:
                size = 0
        r = dict(r)
        r["audio_bytes"] = size
        r["has_audio"] = bool(ap and size)
        out.append(r)
    return out


def _library_stats(conn) -> dict:
    live_rows = store.list_sessions(conn)
    trash_rows = store.list_trash(conn)
    audio = 0
    for r in live_rows + trash_rows:
        ap = r.get("audio_path")
        if ap:
            try:
                audio += Path(ap).stat().st_size
            except OSError:
                pass
    return {
        "sessions": len(live_rows), "trash": len(trash_rows),
        "audio_bytes": audio,
        "audio_sessions": sum(1 for r in live_rows if r.get("audio_path")),
        "total_seconds": round(sum(float(r.get("duration_s") or 0) for r in live_rows), 1),
        "utterances": sum(int(r.get("n_utterances") or 0) for r in live_rows),
    }


def _audio_state(sid: str, audio_path: str | None, active: set[str]) -> str:
    """这一场现在到底能不能回放。前端只展示，不再自己猜 audio_path。"""
    if sid in active:
        return "live"
    if not audio_path:
        return "none"
    try:
        p = Path(str(audio_path))
        if p.is_file() and p.stat().st_size > 0:
            return "ready"
    except OSError:
        pass
    return "missing"


def _mark_session_quality(rows: list[dict]) -> list[dict]:
    """给 /api/sessions 补可播状态、演示标记和「空/重复」体检信息。

    只做标记，不删除；清理入口必须让用户先预览。重复的判定保持保守：
    同标题、开始时间相差 <= 120 秒，保留「被房间引用 / 时长更长 / 句数更多」
    的那一条，避免把两场真实直播误判成重复。
    """
    active = {str(s.get("id") or "") for s in live.list_active()}
    room_sids = {str(r.get("session_id") or "") for r in rooms.list_rooms()
                 if r.get("session_id")}
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r.get("title") or ""), []).append(r)

    duplicate_of: dict[str, str] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda x: str(x.get("started_at") or ""))
        clusters: list[list[dict]] = []
        for r in ordered:
            try:
                ts = datetime.fromisoformat(
                    str(r.get("started_at") or "")).replace(tzinfo=None)
            except (ValueError, TypeError):
                ts = datetime.min
            if clusters:
                try:
                    prev = datetime.fromisoformat(
                        str(clusters[-1][-1].get("started_at") or "")).replace(tzinfo=None)
                except (ValueError, TypeError):
                    prev = datetime.min
                if abs((ts - prev).total_seconds()) <= 120:
                    clusters[-1].append(r)
                    continue
            clusters.append([r])
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            keep = max(cluster, key=lambda r: (
                1 if str(r.get("id") or "") in room_sids else 0,
                float(r.get("duration_s") or 0),
                int(r.get("n_utterances") or 0),
            ))
            keep_id = str(keep.get("id") or "")
            for r in cluster:
                sid = str(r.get("id") or "")
                if sid and sid != keep_id:
                    duplicate_of[sid] = keep_id

    out: list[dict] = []
    for r in rows:
        s = dict(r)
        sid = str(s.get("id") or "")
        s["audio_state"] = _audio_state(sid, s.get("audio_path"), active)
        s["is_demo"] = sid.startswith("DEMO_") or s.get("source_mode") == "demo"
        duration = float(s.get("duration_s") or 0)
        n = int(s.get("n_utterances") or 0)
        empty = duration <= 5 or (n == 0 and duration < 60)
        dup = duplicate_of.get(sid, "")
        reason = []
        if empty:
            reason.append("0 秒场次" if duration <= 5 else "空采集")
        if dup:
            reason.append("与 %s 重复" % dup)
        s["quality"] = {"empty": empty, "duplicate_of": dup,
                        "reason": "；".join(reason)}
        out.append(s)
    return out


def _cleanup_candidates(rows: list[dict]) -> list[dict]:
    """体检候选：演示、空采集、重复（被保留的那条不算）。只读，不删。

    前端先把这些候选一次性预览出来，用户勾选后再走批量回收站；
    正在录制的场次永远不进候选。
    """
    out: list[dict] = []
    for r in rows:
        if r.get("audio_state") == "live":
            continue
        q = r.get("quality") or {}
        kinds: list[str] = []
        reasons: list[str] = []
        if r.get("is_demo"):
            kinds.append("demo")
            reasons.append("演示数据")
        if q.get("empty"):
            kinds.append("empty")
            reasons.append(q.get("reason") or "空采集")
        if q.get("duplicate_of"):
            kinds.append("dup")
            reasons.append("与 %s 重复" % q["duplicate_of"])
        if not kinds:
            continue
        n = dict(r)
        n["cleanup_kinds"] = kinds
        n["cleanup_reasons"] = reasons
        try:
            ap = r.get("audio_path")
            n["audio_bytes"] = Path(str(ap)).stat().st_size if ap else 0
        except OSError:
            n["audio_bytes"] = 0
        out.append(n)
    return out


def _job_failure(e: Exception, sid: str = "") -> tuple[dict, int]:
    """把调度器包装后的异常还原成 HTTP 语义：预算 429、模型/网络 502、业务 400。

    错误文案用外层 e（处理器可能加了「这一片失败，可以继续」这类人话），
    状态码沿异常链找原始类型（外层的 RuntimeError 看不出 429/401）。
    """
    cause = e
    node = getattr(e, "cause", None) or e.__cause__ or e
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        cause = node
        if isinstance(node, (scheduler.BudgetExceeded, llm.LLMError)):
            break
        node = getattr(node, "__cause__", None)
    if isinstance(cause, scheduler.BudgetExceeded):
        return {"error": str(e), "budget": scheduler.budget_status(sid)}, 429
    if isinstance(cause, llm.LLMError):
        return {"error": str(e)}, 502
    return {"error": str(e)}, 400


# 桌面壳是 tauri.localhost 的本地页，调引擎 HTTP API 需要 CORS。
# 只放行本机 origin，不让普通网页跨域读本地服务。
#   - tauri.localhost：正式壳页（Windows 上是 http，其他平台可能是 https）
#   - 127.0.0.1 / localhost 任意端口：`tauri dev` 的 devUrl（壳页从 dev server 加载）
# 引擎只监听回环地址，所以「本机 origin」就是全部可信来源；远程站点仍然读不到响应。
CORS_ORIGINS = {"http://tauri.localhost", "https://tauri.localhost"}
CORS_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "tauri.localhost"}


def _trusted_origin(origin: str) -> bool:
    if not origin:
        return False
    if origin in CORS_ORIGINS:
        return True
    try:
        u = urlparse(origin)
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    return u.scheme in ("http", "https") and host in CORS_LOOPBACK_HOSTS


class Handler(BaseHTTPRequestHandler):
    server_version = "LiveNotes/0.1"

    # ------------------------------------------------------------ 基础
    def _cors_origin(self) -> str:
        try:
            return (self.headers.get("Origin") or "").strip()
        except Exception:
            return ""

    def end_headers(self) -> None:
        origin = self._cors_origin()
        if _trusted_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        origin = self._cors_origin()
        if not _trusted_origin(origin):
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        # Chromium 的 Private Network Access：从非回环页（如 tauri.localhost）
        # 请求 127.0.0.1 时会额外要求这个头，缺了预检就失败。
        if (self.headers.get("Access-Control-Request-Private-Network") or "").lower() == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # BaseHTTPRequestHandler 在 send_response 时就调用这里，早于 _record_request；
        # 请求日志统一由 _record_request 出，避免一次请求两行、正常请求还被标成 warn。
        return

    def send_response(self, code: int, message: str | None = None) -> None:
        # 记下状态码，_record_request 在请求结束时统一出指标。
        self._metric_status = int(code)
        super().send_response(code, message)

    def _record_request(self, method: str, path: str, t0: float) -> None:
        try:
            ms = (time.perf_counter() - t0) * 1000.0
            status = int(getattr(self, "_metric_status", 0) or 0)
            group = _metric_group(path)
            metrics.METRICS.inc("http.requests")
            metrics.METRICS.inc("http.status.%d" % status)
            metrics.METRICS.observe("http.duration_ms", ms)
            metrics.METRICS.observe("http.duration.%s_ms" % group, ms)
            if status >= 500:
                metrics.METRICS.inc("http.errors")
            self._metric_recorded = True
            # 采集块 4 次/秒、状态轮询 1 次/秒：正常时不进日志，免得刷屏。
            if group in ("live_chunk", "live_status") and status < 400 and ms < 1000:
                return
            if group.startswith("/api/") or status >= 400 or ms >= 1000:
                level = "error" if status >= 500 else "warn" if status >= 400 else "info"
                logs.log_event("http.request", level=level, method=method,
                               path=group, status=status, ms=round(ms, 1))
        except Exception:
            pass

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, ctype: str, status: int = 200,
               cache: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _attachment(self, text: str, ctype: str, filename: str) -> None:
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % filename.replace('"', ""))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _attachment_bytes(self, data: bytes, ctype: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % filename.replace('"', ""))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file_attachment(self, path: Path, ctype: str, filename: str) -> None:
        """流式下发文件，避免把 100+ MB 的分享包整包读进内存。"""
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % filename.replace('"', ""))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                buf = f.read(262144)
                if not buf:
                    break
                self.wfile.write(buf)

    def _file(self, path: Path) -> None:
        # 防止 /static/../../ 目录穿越；本地服务也不该把工作区其他文件暴露出去。
        # 合法根目录有两个：基座 app/web 和（桌面版的）overlay，见 webroot.py。
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if not resolved.is_file() or not webroot.is_inside(resolved):
            self._json({"error": "not found"}, 404)
            return
        path = resolved
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or "javascript" in ctype:
            ctype += "; charset=utf-8"
        # 前端拆成模块后，浏览器会按启发式缓存 .js/.css：改了文件刷新看不到效果，
        # 排查半天其实是在跑旧代码。本地服务重发一遍的代价可以忽略，一律 revalidate。
        self._bytes(path.read_bytes(), ctype, cache="no-cache")

    def _serve_static(self, rel: str) -> None:
        """静态资源按层找：桌面 overlay 优先，基座兜底。

        网页版没有 overlay，行为和以前完全一样；桌面版的同一路径文件会被
        ``desktop/web/**`` 换掉，基座里不需要任何桌面分支。
        """
        path = webroot.resolve_static(rel)
        if path is None:
            self._json({"error": "not found"}, 404)
            return
        self._file(path)

    def _card_feedback(self, q: dict) -> None:
        """飞书卡片上的 👍/👎 落地点。

        为什么是打开一个页面而不是回调：飞书卡片回调要求公网可达的回调地址，
        与"纯内网、不出网"冲突；URL 按钮在飞书里点开即访问本机服务，
        点一下就记下，不需要任何外网配置。
        """
        sid = str(q.get("sid") or q.get("session_id") or "").strip()
        try:
            t = float(q.get("t") or 0)
            v = int(float(q.get("v") or 0))
        except (TypeError, ValueError):
            t, v = 0.0, 0
        if not sid or v not in (1, -1):
            body = ('<p class="h">这个链接不完整</p><p>回飞书里重新点一次 👍 / 👎。</p>')
            return self._bytes(self._page("参数不对", body).encode("utf-8"),
                               "text/html; charset=utf-8", 400)
        item = "hl:%.2f" % t
        conn = store.connect()
        try:
            sess = store.get_session(conn, sid) or {}
            n = store.set_push_feedback(conn, sid, item, v)
            store.set_highlight_feedback_at(conn, sid, t, v)
            row = conn.execute("SELECT text FROM push_log WHERE session_id=? AND item=?",
                               (sid, item)).fetchone()
            learned = [x for x in feedback.details(conn) if x["delta"] < 0]
        finally:
            conn.close()
        mark = "👍 有用" if v > 0 else "👎 没用"
        when = timefmt.clock_line(sess, t) if sess else ""
        snippet = (row["text"] if row else "") or ""
        snippet = _esc(snippet.splitlines()[1] if len(snippet.splitlines()) > 1 else snippet[:60])
        # sid 来自查询串，直接拼 href 等于给自己开一个反射 XSS 的口子
        link = _esc("/reader?session=%s&t=%d" % (quote(sid, safe=""), int(t)))
        extra = ("<p>下一场开始，这类会少推一些%s。</p>"
                 % ("（" + "、".join(x["label"] for x in learned[:3]) + "）" if learned else "")
                 ) if v < 0 else "<p>下一场开始，这类会更容易被推。</p>"
        body = (
            '<p class="mark">%s</p>'
            '<p class="h">已记下：%s</p>'
            '%s'
            '<p class="muted">%s · 录音 %s</p>'
            '<p><a class="btn" href="%s">回听这一段</a></p>'
            '<p class="muted">这个页面可以关掉。%s</p>'
            % ("✅", mark, ("<p>「%s」</p>" % snippet) if snippet else "",
               _esc(when), timefmt.offset(t), link, extra)
        )
        self._bytes(self._page("已记下", body).encode("utf-8"),
                    "text/html; charset=utf-8")

    @staticmethod
    def _page(title: str, body: str) -> str:
        return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s · 直播语记</title><style>
body{margin:0;padding:44px 22px;background:#f4f6f9;color:#1d2330;
 font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.card{max-width:460px;margin:0 auto;background:#fff;border:1px solid #e6e9ef;border-radius:14px;
 padding:26px 24px;box-shadow:0 6px 24px rgba(23,32,50,.06)}
.mark{font-size:40px;margin:0 0 6px}.h{font-size:20px;font-weight:700;margin:0 0 10px}
p{font-size:14px;line-height:1.75;margin:8px 0}
.muted{color:#6b7480;font-size:12px}
.btn{display:inline-block;margin-top:6px;padding:9px 16px;border-radius:9px;background:#2b5cff;
 color:#fff;text-decoration:none;font-size:13px}
</style></head><body><div class="card">%s</div></body></html>""" % (_esc(title), body)

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:
        t0 = time.perf_counter()
        u = urlparse(self.path)
        p, q = unquote(u.path), _flat(parse_qs(u.query, keep_blank_values=True))
        try:
            if p == "/favicon.ico":       # 别让浏览器的默认请求进事件流
                self.send_response(204)
                self.end_headers()
                return
            if p in ("/", "/index.html"):
                return self._file(WEB_DIR / "index.html")
            if p in ("/reader", "/reader.html"):
                return self._file(WEB_DIR / "reader.html")
            if p in ("/library", "/library.html"):
                return self._file(WEB_DIR / "library.html")
            if p in ("/people", "/people.html"):
                return self._file(WEB_DIR / "people.html")
            if p in ("/highlights", "/highlights.html"):
                return self._file(WEB_DIR / "highlights.html")
            if p in ("/live", "/live.html"):
                return self._file(WEB_DIR / "live.html")
            if p in ("/capture", "/capture.html"):
                return self._file(WEB_DIR / "capture.html")
            if p in ("/rooms", "/rooms.html"):
                return self._file(WEB_DIR / "rooms.html")
            if p in ("/pushes", "/pushes.html"):
                return self._file(WEB_DIR / "pushes.html")
            if p in ("/glossary", "/glossary.html"):
                return self._file(WEB_DIR / "glossary.html")
            if p in ("/metrics", "/metrics.html"):
                return self._file(WEB_DIR / "metrics.html")
            if p == "/card/feedback":
                return self._card_feedback(q)
            if p.startswith("/static/"):
                return self._serve_static(p[len("/static/"):])
            conn = store.connect()
            try:
                return self._get_api(p, q, conn)
            finally:
                conn.close()
        except Exception as e:  # 统一报错，便于前端展示
            traceback.print_exc()
            self._json({"error": str(e)}, 500)
        finally:
            self._record_request("GET", p, t0)

    def _get_metrics(self, q: dict) -> None:
        """统一的指标出口。默认 JSON；``?format=prometheus`` 给外部监控用。"""
        snap = metrics.build_snapshot()
        try:
            snap["events"] = store.list_metric_events(limit=20)
        except Exception:
            snap["events"] = []
        since = _since_iso(q.get("since"))
        if since:
            snap["series"] = store.list_metric_samples(
                since=since, scope=q.get("scope") or "global",
                session_id=(q.get("session_id") or None),
                limit=min(20000, int(_num(q.get("limit"), 1200) or 1200)))
        fmt = str(q.get("format") or "json").lower()
        if fmt in ("prometheus", "prom", "text"):
            body = metrics.prometheus_text(snap).encode("utf-8")
            return self._bytes(body, "text/plain; version=0.0.4; charset=utf-8")
        return self._json(snap)

    def _get_api(self, p: str, q: dict, conn) -> None:
        if p == "/api/host/manifest":
            # 宿主清单：网页版返回 overlay=false 的空清单（不能 404，否则
            # 基座适配器在网页控制台里每次刷新都留一条红字）。
            return self._json(webroot.manifest())
        if p == "/api/health":
            return self._json({
                "ok": True, "fts_trigram": store.FTS_TRIGRAM,
                # 壳用 pid + port 确认 engine.json 里的引擎确实是同一个进程，
                # 避免端口被别的程序复用后误复用。
                **runtime.health_fields(),
                "llm": scheduler.SCHEDULER.stats_summary(),
                # 健康检查也顺手给出「有没有在跑、有没有卡住」，不用再打三个接口。
                "asr": live.POOL.stats(),
                "uptime_s": metrics.METRICS.uptime(),
                "sessions": len(live.metrics_sessions()),
                "stalls": int(metrics.METRICS.global_value("live.stalls")),
            })
        if p == "/api/diagnostics":
            # 壳的托盘看门狗与诊断包共用：一次拿到引擎、在跑场次的断流秒数、
            # 房间状态、最近事件和日志尾巴。参数只用于诊断包控制体积。
            return self._json(diagnostics.summary(
                event_limit=int(_num(q.get("events"), 80) or 80),
                log_limit=int(_num(q.get("log"), 30) or 30)))
        if p == "/api/metrics":
            return self._get_metrics(q)
        if p == "/api/metrics/events":
            since = _since_iso(q.get("since"))
            limit = min(500, int(_num(q.get("limit"), 100) or 100))
            return self._json({
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "events": store.list_metric_events(
                    since=since, limit=limit, level=q.get("level") or None),
            })
        if p == "/api/metrics/series":
            since = _since_iso(q.get("since")) or _since_iso(
                time.time() - 3600)
            return self._json({"rows": store.list_metric_samples(
                since=since, scope=q.get("scope") or "global",
                session_id=(q.get("session_id") if q.get("session_id") else None),
                limit=min(20000, int(_num(q.get("limit"), 1200) or 1200)))})
        if p == "/api/metrics/sessions":
            # 重启后仍能看到上一场丢了多少句、花了多少 token（读 session_metrics）。
            return self._json({"sessions": store.list_session_metrics(
                limit=min(200, int(_num(q.get("limit"), 40) or 40)))})
        if p == "/api/jobs":
            # 任务面板/排障：内存里正在跑的 + 台账里最近的历史（含重启前失败的）。
            limit = min(200, int(_num(q.get("limit"), 50) or 50))
            sid = str(q.get("session_id") or "")
            return self._json(scheduler.SCHEDULER.snapshot(
                limit=limit, session_id=sid or None))
        if p == "/api/profile":
            return self._json({"keywords": profile.keywords()})
        if p == "/api/glossary":
            return self._json({"terms": glossary.entries(), "keywords": profile.keywords()})
        if p == "/api/pushes":
            sid = str(q.get("session_id") or "")
            if not sid:
                rows = store.list_sessions(conn)
                sid = rows[0]["id"] if rows else ""
            if not sid:
                return self._json({"session": {}, "rows": [], "budget": {}})
            ov = store.push_overview(conn, sid)
            sent = conn.execute(
                "SELECT COUNT(*) FROM push_log WHERE session_id=? AND status='sent' "
                "AND item LIKE 'hl:%'", (sid,)).fetchone()[0]
            from .config import PUSH_COOLDOWN_S, PUSH_DAILY_CAP, PUSH_SESSION_CAP
            return self._json({
                "session": store.get_session(conn, sid) or {},
                "sessions": store.list_sessions(conn)[:50],
                "overview": ov,
                "learned": feedback.details(conn),
                "budget": {"daily_used": ov["daily_used"], "daily_cap": PUSH_DAILY_CAP,
                           "session_sent": int(sent or 0), "session_cap": PUSH_SESSION_CAP,
                           "cooldown_s": PUSH_COOLDOWN_S},
            })
        if p == "/api/highlights":
            sid = q.get("session_id", "")
            limit = min(100, int(_num(q.get("limit"), 50) or 50))
            return self._json({
                "session": store.get_session(conn, sid),
                "highlights": store.list_highlights(conn, sid, limit),
                "meta": store.highlights_meta(conn, sid),
                "th": highlight.TH,
                # 「你标过没用的那类，我们已经少推了」——透明才敢让人继续标
                "learned": feedback.details(conn),
            })
        if p == "/api/sessions":
            return self._json({"sessions": _mark_session_quality(
                store.list_sessions(conn))})
        sid = q.get("session_id", "")
        if p == "/api/meta":
            return self._json(store.get_meta(conn, sid))
        if p == "/api/utterances":
            f = parse_filters(q)
            limit = min(5000, int(_num(q.get("limit"), 2000) or 2000))
            offset = int(_num(q.get("offset"), 0) or 0)
            rows = store.query_utterances(conn, f, limit=limit, offset=offset)
            total = store.count_utterances(conn, f)
            return self._json({"total": total, "rows": rows, "filter": f})
        if p == "/api/speakers":
            sid = str(q.get("session_id") or q.get("id") or "")
            if not sid:
                return self._json({"error": "缺少 session_id"}, 400)
            return self._json({"speakers": store.speaker_totals(conn, sid)})
        if p == "/api/timeline":
            bucket = int(_num(q.get("bucket"), 300) or 300)
            return self._json({"bucket": bucket,
                               "bars": store.timeline(conn, sid, bucket, parse_filters(q))})
        if p == "/api/evidence":
            ids = [int(x) for x in _list(q.get("ids")) if x.isdigit()]
            return self._json({"rows": store.get_evidence(conn, sid, ids)})
        if p == "/api/summaries":
            return self._json({"rows": store.list_summaries(conn, sid)})
        if p == "/api/analysis/status":
            # 页面刷新 / 切场次回来都会打这里：有在跑的任务就把进度接上
            return self._json(analysis_job.status(conn, sid, q.get("run_id") or None))
        if p == "/api/analysis/runs":
            return self._json({"runs": store.list_runs(conn, sid, 10)})
        if p == "/api/summary":
            scope = str(q.get("scope") or "content")
            row = store.latest_summary(conn, sid, scope)
            if not row:
                return self._json({"ok": False, "reason": "还没有生成过"})
            return self._json({"ok": True, "created_at": row.get("created_at"),
                               "payload": row.get("payload") or {}})
        if p == "/api/audio":
            return self._audio_slice(conn, q)
        if p == "/api/live/audio/meta":
            return self._live_audio_meta(conn, q)
        if p == "/api/live/audio":
            return self._live_audio(conn, q)
        if p == "/api/live/list":
            return self._json({"sessions": live.list_active()})
        if p == "/api/live/brief":
            code, data = livebrief.api(
                conn, rooms.list_rooms(), live.list_active(),
                rooms.capture_status(), q)
            return self._json(data, code)
        if p == "/api/rooms":
            rows = rooms.list_rooms()
            # 老记录没有稳定编号：按创建时间补一次 seq，之后 CH 编号不再随加删房间跳动。
            missing = [r for r in rows if not r.get("seq")]
            if missing:
                nxt = max([int(r.get("seq") or 0) for r in rows] + [0]) + 1
                for r in sorted(missing, key=lambda x: (x.get("created_at") or "",
                                                        x.get("id") or "")):
                    upd = rooms.update(r["id"], seq=nxt)
                    if upd:
                        rows[rows.index(r)] = upd
                    nxt += 1
            # 老记录（这次改动之前停掉的）没有 saved/has_recording：第一次读到时
            # 用数据库里的录音信息补一次并写回，之后就是持久字段，不用再猜。
            for i, r in enumerate(rows):
                if r.get("state") == "stopped" and "has_recording" not in r:
                    sid = r.get("session_id") or ""
                    upd = rooms.update(
                        r["id"], saved=bool(sid),
                        has_recording=_has_recording(conn, sid))
                    if upd:
                        rows[i] = upd
            return self._json({"rooms": rows,
                               "capture": {
                                   "direct_fallback": settings.direct_fallback()["mode"],
                               }})
        if p == "/api/capture/status":
            # 采集代理心跳：浏览器到底还在不在，不靠房间状态推
            return self._json({"rooms": rooms.capture_status()})
        if p == "/api/live/info":
            return self._json(live.info())
        if p == "/api/live/status":
            try:
                return self._json(live.status(str(q.get("id") or q.get("session_id") or "")))
            except live.LiveError as e:
                return self._json({"error": str(e)}, 404)
        if p == "/api/live/utterances":
            # 工作台往上翻：只回 before_t 之前的 N 条，避免整场重传
            sid = str(q.get("id") or q.get("session_id") or "")
            bt = _num(q.get("before_t"))
            limit = min(400, int(_num(q.get("limit"), 120) or 120))
            if not sid:
                return self._json({"rows": []})
            return self._json({"rows": store.live_history(conn, sid, bt, limit),
                               "limit": limit})
        if p == "/api/reader":
            sid = str(q.get("session_id") or "")
            try:
                return self._json(reader.read(
                    conn, sid,
                    speakers=_list(q.get("speakers")),
                    exclude_low=_flag(q.get("exclude_low"))))
            except ValueError as e:
                return self._json({"error": str(e)}, 404)
        if p == "/api/library":
            return self._json({"sessions": _library_rows(conn, False),
                               "stats": _library_stats(conn)})
        if p == "/api/library/trash":
            return self._json({"sessions": _library_rows(conn, True),
                               "stats": _library_stats(conn)})
        if p == "/api/cleanup":
            cand = _cleanup_candidates(_mark_session_quality(store.list_sessions(conn)))
            counts = {"total": len(cand), "demo": 0, "empty": 0, "duplicate": 0}
            for r in cand:
                for k in r.get("cleanup_kinds") or []:
                    counts[{"demo": "demo", "empty": "empty", "dup": "duplicate"}[k]] += 1
            return self._json({"sessions": cand, "counts": counts,
                               "stats": _library_stats(conn)})
        if p == "/api/search":
            qtext = str(q.get("q") or "").strip()
            limit = min(200, int(_num(q.get("limit"), 60) or 60))
            # 术语表参与检索：OPC政策 拆成 OPC + 政策，别名（欧匹西）也能搜到
            expanded = glossary.expand_query(qtext) or [qtext]
            rows = (store.search_multi(conn, expanded, limit) if len(expanded) > 1
                    else store.search_all(conn, qtext, limit))
            return self._json({"q": qtext, "count": len(rows), "rows": rows,
                               "expanded": expanded if len(expanded) > 1 else []})
        if p == "/api/polish":
            sid = str(q.get("session_id") or "")
            try:
                return self._json(polish.cached(conn, sid) or {"ok": False, "reason": "还没润色过"})
            except ValueError as e:
                return self._json({"ok": False, "reason": str(e)}, 404)
        if p == "/api/settings":
            return self._json(settings.public())
        if p == "/api/llm/models":
            return self._json(llm.list_models())
        if p == "/api/sessions/suggest_title":
            sid = str(q.get("session_id") or "")
            if not store.get_session(conn, sid):
                return self._json({"error": "场次不存在"}, 404)
            # fresh=1 只在「换一个」时传：那一次才真的再调模型
            fresh = str(q.get("fresh") or "0") not in ("0", "", "false", "False")
            return self._json(reader.suggest_title(conn, sid, fresh=fresh))
        if p == "/api/export":
            sid = str(q.get("session_id") or "")
            if not store.get_session(conn, sid):
                return self._json({"error": "场次不存在"}, 404)
            body, ctype, name = export.build(conn, sid, str(q.get("format") or "md"))
            return self._attachment(body, ctype, name)
        if p == "/api/share/info":
            sid = str(q.get("session_id") or "")
            s = store.get_session(conn, sid)
            if not s:
                return self._json({"error": "场次不存在"}, 404)
            ap = Path(str(s.get("audio_path") or ""))
            try:
                size = ap.stat().st_size if ap.is_file() else 0
            except OSError:
                size = 0
            fmt = str(s.get("audio_format") or "")
            if not fmt and ap.suffix:
                fmt = ap.suffix.lower().lstrip(".")
            return self._json({"ok": True, "session_id": sid,
                               "minutes_url": s.get("minutes_url") or "",
                               "audio_format": fmt, "audio_bytes": size,
                               "has_audio": bool(size),
                               "has_recording": bool(size)})
        if p == "/api/share/package":
            sid = str(q.get("session_id") or "")
            try:
                info = share.build_package(conn, sid)
                zpath = share.zip_package(info["dir"])
            except ValueError as e:
                return self._json({"error": str(e)}, 404)
            except Exception as e:
                traceback.print_exc()
                return self._json({"error": "打包失败：%s" % e}, 500)
            return self._file_attachment(zpath, "application/zip", zpath.name)
        if p == "/api/coach":
            return self._json(coach.analyze(conn, sid))
        if p == "/api/coach/cross":
            return self._json(coach.cross(conn, str(q.get("label") or "") or None))
        if p == "/api/persons":
            return self._json({
                "persons": store.list_persons(conn),
                "thresholds": {"auto": voiceprint.AUTO_THRESHOLD,
                               "candidate": voiceprint.CAND_THRESHOLD},
                "hint": "改名即注册：任意一场把说话人改成真实昵称，声纹就进了底库。",
            })
        if p == "/api/person":
            pid = str(q.get("person_id") or "")
            if not pid:
                name = str(q.get("name") or "").strip()
                if not name:
                    return self._json({"error": "缺少 person_id"}, 400)
                hits = store.find_persons(conn, name)
                if not hits:
                    return self._json({"ok": False, "reason": "底库里没有「%s」" % name,
                                       "rows": [], "persons": []})
                pid = hits[0]["person_id"]
            person = store.get_person(conn, pid)
            if not person:
                return self._json({"error": "人物不存在"}, 404)
            rows = store.person_appearances(conn, pid)
            person["n_protos"] = store.count_person_emb(conn, pid)
            person["appearances"] = rows
            person["sessions"] = len({r["session_id"] for r in rows})
            person["seconds"] = round(sum(float(r["seconds"] or 0) for r in rows), 1)
            return self._json({"ok": True, "person": person,
                               "thresholds": {"auto": voiceprint.AUTO_THRESHOLD,
                                              "candidate": voiceprint.CAND_THRESHOLD}})
        if p == "/api/persons/explain":
            sid = str(q.get("session_id") or "")
            spk = str(q.get("speaker_id") or "")
            if not sid or not spk:
                return self._json({"error": "缺少 session_id / speaker_id"}, 400)
            return self._json({"rows": voiceprint.explain(conn, sid, spk)})
        return self._json({"error": "unknown endpoint " + p}, 404)

    def _audio_slice(self, conn, q: dict) -> None:
        """回放。默认整流输出（支持 Range，播放器才能随意拖动进度）；
        显式传 t_start/t_end 时仍切出那一段，兼容旧调用。无音频时明确报错，不伪造。"""
        s = store.get_session(conn, q.get("session_id", ""))
        ap = (s or {}).get("audio_path")
        t0, t1 = _num(q.get("t_start"), 0) or 0, _num(q.get("t_end"), 10) or 10
        if not ap or not Path(ap).is_file():
            return self._json({
                "error": "该场次没有原始音频，无法回放",
                "hint": "演示数据只含结构化文本；真实采集请用 LoopbackCapture 录制后写入 sessions.audio_path",
                "t_start": t0, "t_end": t1,
            }, 404)
        if "t_start" not in q and "t_end" not in q:
            return self._audio_stream(Path(ap), download=bool(q.get("download")))
        data = audio.slice_wav_bytes(Path(ap), t0, t1)
        if data is None:
            return self._json({"error": "这段音频切不出来",
                               "hint": "文件可能已被清理，或 ffmpeg 不可用"}, 415)
        return self._bytes(data, "audio/wav")

    # -------------------------------------------------- 采集中回听
    @staticmethod
    def _wav_header(nbytes: int) -> bytes:
        """16k / 单声道 / 16bit 的 44 字节 WAV 头。data 长度按窗口快照写死。"""
        rate, ch, width = live.SAMPLE_RATE, 1, 2
        block = ch * width
        return struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + nbytes, b"WAVE",
            b"fmt ", 16, 1, ch, rate, rate * block, block, width * 8,
            b"data", nbytes)

    @staticmethod
    def _wav_ready(path: Path) -> bool:
        """收尾中的 WAV 头可能是半截的；只有头和帧数都对得上才当它可播。"""
        try:
            with wave.open(str(path), "rb") as w:
                n = w.getnframes()
                if n <= 0:
                    return False
                need = 44 + n * w.getsampwidth() * w.getnchannels()
                return path.stat().st_size >= need
        except (OSError, EOFError, wave.Error):
            return False

    @staticmethod
    def _wav_duration(path: Path) -> float:
        try:
            with wave.open(str(path), "rb") as w:
                rate = w.getframerate()
                return w.getnframes() / rate if rate else 0.0
        except (OSError, EOFError, wave.Error):
            return 0.0

    def _final_audio(self, conn, sid: str) -> Path | None:
        """结束后的 WAV：优先数据库路径；收尾刚完成、数据库还没更新时按约定路径兜底。"""
        s = store.get_session(conn, sid)
        cands: list[Path] = []
        if s and s.get("audio_path"):
            cands.append(Path(s["audio_path"]))
        cands.append(live.AUDIO_DIR / (sid + ".wav"))
        cands.append(live.AUDIO_DIR / (sid + ".m4a"))
        for p in cands:
            try:
                # m4a/opus 没有 WAV 头，别用 _wav_ready 去校验；存在且非空即可播。
                ok = p.is_file() and p.stat().st_size > 0 and (
                    p.suffix.lower() != ".wav" or self._wav_ready(p))
                if ok:
                    return p
            except OSError:
                continue
        return None

    @staticmethod
    def _live_audio_message(reason: str) -> str:
        return {
            "not_active": "这一路不在采集，可能已经停止；录音还在的话会在收尾后变成回放",
            "no_audio": "采集刚开始，还没有收到音频",
            "too_short": "这一段音频还太短，等几秒再试",
        }.get(reason or "", "现在没有可听的音频")

    def _live_audio_meta(self, conn, q: dict) -> None:
        """播放器先问窗口：拿到字节快照的起止秒，再用同一组参数请求音频。"""
        sid = str(q.get("session_id") or q.get("id") or "")
        if not sid:
            return self._json({"error": "缺少 session_id"}, 400)
        seconds = _num(q.get("seconds"), 300.0)
        start, end = _num(q.get("start")), _num(q.get("end"))
        w = live.audio_window(sid, seconds=seconds, start_s=start, end_s=end,
                              default_s=300.0, min_s=0.0)
        if w.get("ok"):
            return self._json({
                "available": True, "live": True, "session_id": sid,
                "start_s": round(w["start_s"], 3), "end_s": round(w["end_s"], 3),
                "elapsed_s": round(w["total_s"], 3),
                "paused": w["paused"], "running": w["running"],
                "last_chunk_ago_s": w.get("last_chunk_ago_s"),
                "window_s": round(w["end_s"] - w["start_s"], 3),
            })
        path = self._final_audio(conn, sid)
        if path:
            dur = (float((store.get_session(conn, sid) or {}).get("duration_s") or 0)
                   or self._wav_duration(path))
            return self._json({
                "available": True, "live": False, "session_id": sid,
                "start_s": 0.0, "end_s": round(dur, 3), "elapsed_s": round(dur, 3),
                "paused": False, "running": False,
                "playback_url": "/api/audio?session_id=" + quote(sid),
            })
        reason = str(w.get("reason") or "")
        return self._json({
            "available": False, "live": reason != "not_active", "session_id": sid,
            "reason": reason, "message": self._live_audio_message(reason),
            "elapsed_s": round(float(w.get("total_s") or 0.0), 3),
            "paused": bool(w.get("paused")), "running": bool(w.get("running")),
            "last_chunk_ago_s": w.get("last_chunk_ago_s"),
        })

    def _live_audio(self, conn, q: dict) -> None:
        """采集中按 spool 切片；结束后同一组 start/end 从 WAV 切同样的窗口。"""
        sid = str(q.get("session_id") or q.get("id") or "")
        if not sid:
            return self._json({"error": "缺少 session_id"}, 400)
        seconds = _num(q.get("seconds"))
        start, end = _num(q.get("start")), _num(q.get("end"))
        w = live.audio_window(sid, seconds=seconds, start_s=start, end_s=end,
                              default_s=60.0)
        if w.get("ok"):
            return self._live_wav_stream(w)

        path = self._final_audio(conn, sid)
        if path:
            if start is not None or end is not None:
                t0 = start if start is not None else 0.0
                t1 = end if end is not None else (
                    float((store.get_session(conn, sid) or {}).get("duration_s") or 0)
                    or self._wav_duration(path))
                data = audio.slice_wav_bytes(path, t0, t1)
                if data is None:
                    return self._json({"error": "这段音频切不出来",
                                       "reason": "slice_failed"}, 415)
                return self._bytes(data, "audio/wav")
            return self._audio_stream(path)

        reason = str(w.get("reason") or "")
        return self._json({"error": self._live_audio_message(reason),
                           "reason": reason}, 404)

    def _live_wav_stream(self, w: dict) -> None:
        """把 spool 的一个窗口包成 WAV 输出。虚拟文件 = 44 字节头 + PCM 窗口。

        文件在请求期间还在增长：这里只按快照 nbytes 输出，Range 也以快照
        总长为界，避免 Content-Length 和实际写入不一致。
        """
        start_byte = int(w["start_byte"])
        nbytes = int(w["nbytes"])
        try:
            actual = Path(w["spool"]).stat().st_size - start_byte
        except OSError:
            return self._json({"error": "音频正在收尾，稍后再试",
                               "reason": "finalizing"}, 409)
        nbytes = max(0, min(nbytes, actual))
        nbytes -= nbytes % 2
        if nbytes <= 0:
            return self._json({"error": self._live_audio_message("too_short"),
                               "reason": "too_short"}, 409)
        hdr = self._wav_header(nbytes)
        total = len(hdr) + nbytes
        start, end = 0, total - 1
        partial = False
        rng = (self.headers.get("Range") or "").strip()
        if rng.startswith("bytes="):
            spec = rng[len("bytes="):].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if not a:                       # bytes=-500 末尾 500 字节
                    start, end = max(0, total - int(b)), total - 1
                else:
                    start = int(a)
                    end = int(b) if b else total - 1
                partial = True
            except ValueError:
                start, end = 0, total - 1
                partial = False
            start = max(0, min(start, total - 1))
            end = max(start, min(end, total - 1))
        length = max(0, end - start + 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        pos, left = start, length
        if pos < len(hdr):
            take = min(len(hdr) - pos, left)
            self.wfile.write(hdr[pos:pos + take])
            pos += take
            left -= take
        if left <= 0:
            return
        pcm_pos = start_byte + (pos - len(hdr))
        with open(w["spool"], "rb") as f:
            f.seek(pcm_pos)
            while left > 0:
                buf = f.read(min(262144, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)

    def _audio_stream(self, path: Path, download: bool = False) -> None:
        if download:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", audio.content_type(path))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % path.name.replace('"', ""))
            self.end_headers()
            self.wfile.write(data)
            return
        """整场 WAV，带 Accept-Ranges。浏览器据此按需取字节，拖动进度条不用整段下载。"""
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = (self.headers.get("Range") or "").strip()
        partial = False
        if rng.startswith("bytes="):
            spec = rng[len("bytes="):].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if not a:                       # bytes=-500 末尾 500 字节
                    start, end = max(0, size - int(b)), size - 1
                else:
                    start = int(a)
                    end = int(b) if b else size - 1
                partial = True
            except ValueError:
                start, end = 0, size - 1
                partial = False
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
        length = max(0, end - start + 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", audio.content_type(path))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                buf = f.read(min(262144, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:
        t0 = time.perf_counter()
        u = urlparse(self.path)
        p = unquote(u.path)
        try:
            if p == "/api/live/chunk":            # 裸 PCM，不走 JSON
                return self._live_chunk()
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "invalid JSON body"}, 400)
            try:
                conn = store.connect()
                try:
                    return self._post_api(p, body, conn)
                finally:
                    conn.close()
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)
        finally:
            self._record_request("POST", p, t0)

    def _live_chunk(self) -> None:
        sid = self.headers.get("X-Live-Id") or ""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not sid or n <= 0:
            return self._json({"error": "missing X-Live-Id or body"}, 400)
        if n > 2_000_000:
            self.close_connection = True
            return self._json({"error": "chunk too large"}, 413)
        pcm = self.rfile.read(n)
        try:
            return self._json(live.feed(sid, pcm))
        except live.LiveError as e:
            return self._json({"error": str(e)}, 404)

    def _post_api(self, p: str, b: dict, conn) -> None:
        if p == "/api/rooms":
            url = str(b.get("url") or "")
            try:
                room, created = rooms.add(url, str(b.get("resolved_from") or ""),
                                          str(b.get("name") or ""))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"room": room, "created": created},
                              201 if created else 200)
        if p == "/api/rooms/rename":
            # 人工改名：用户可能比页面更清楚这一路是什么（"老张的第二场"这种）。
            # 一旦改成 user，采集端后来读到的名字不许再覆盖，否则改了又变回去。
            rid = str(b.get("id") or "")
            nm = str(b.get("name") or "").strip()[:60]
            if not rooms.get(rid):
                return self._json({"error": "房间不存在"}, 404)
            if not nm:
                return self._json({"error": "名字不能是空的"}, 400)
            r = rooms.update(rid, name=nm, name_source="user")
            # 会话标题（飞书推送、导出文件名都在用）跟着改，否则收听页叫新
            # 名字、飞书卡片还是旧名字，等于没改。
            if r and r.get("session_id"):
                live.retitle(str(r["session_id"]), "监听中心 · " + nm)
            return self._json({"room": r})
        if p == "/api/rooms/remove":
            rid = str(b.get("id") or "")
            r = rooms.get(rid)
            sid = (r or {}).get("session_id") or ""
            # 先移除房间再停会话：和 close 同理，采集端看到房间没了就回收
            # ffmpeg/浏览器，不会在收尾的几十秒里继续录或重建会话。
            ok = rooms.remove(rid)
            if r and sid:
                try:
                    live.stop(sid)
                except live.LiveError:
                    pass
                except Exception:
                    traceback.print_exc()
            return self._json({"ok": ok})
        if p == "/api/rooms/state":
            # 采集端/采集页回报：名字、状态、阶段、会话 id、失败原因。
            # 阶段以这里写入的为准，前端不再按错误文案猜。
            rid = str(b.get("id") or "")
            r = rooms.get(rid)
            if not r:
                return self._json({"error": "房间不存在"}, 404)
            f: dict[str, Any] = {}
            if "url" in b:
                # 采集代理在浏览器里落地后回报的真实房号（短链/reflow -> live.douyin.com/xxx）。
                u = str(b.get("url") or "").strip()[:300]
                if u.startswith(("http://", "https://")):
                    f["resolved_url"] = u
            if "name" in b:
                nm = str(b.get("name") or "").strip()[:80]
                # 用户手动改过的名字不许被采集端的回报盖掉：改了又变回去，
                # 是比"没有名字"更糟的体验。空串也不当成"清空"。
                if nm and str(r.get("name_source") or "") != "user":
                    f["name"] = nm
                    f["name_source"] = "page"
            if "session_id" in b:
                f["session_id"] = str(b.get("session_id") or "")
            if "engine" in b:
                # 采集引擎：direct / shared / per_room。房间卡片和排障用。
                f["capture_engine"] = str(b.get("engine") or "")[:20]
            # 直取失败后的「切换浏览器采集」入口：switch_ready 由采集代理亮起，
            # switch_requested 只能由用户在收听页点确认后写入（见 /api/rooms/switch）。
            if "switch_ready" in b:
                f["switch_ready"] = bool(b.get("switch_ready"))
            if "switch_engine" in b:
                f["switch_engine"] = str(b.get("switch_engine") or "")[:20]
            if "switch_reason" in b:
                f["switch_reason"] = str(b.get("switch_reason") or "")[:300]
            if "switch_requested" in b:
                f["switch_requested"] = bool(b.get("switch_requested"))
            if isinstance(b.get("capture_event"), dict):
                _record_capture_event(rid, b["capture_event"])
            if "stage_note" in b:
                f["stage_note"] = str(b.get("stage_note") or "")[:200]
            if "error" in b:
                f["error"] = str(b.get("error") or "")[:500]
            if "saved" in b:
                f["saved"] = bool(b.get("saved"))
            if "has_recording" in b:
                f["has_recording"] = bool(b.get("has_recording"))
            # 自动收尾的标记（断流看门狗 / 采集代理判定下播）：飞书据此发通知，
            # 也让人第二天看到「已停止」时知道不是自己按的。
            if "auto_stopped" in b:
                f["auto_stopped"] = bool(b.get("auto_stopped"))
            if "auto_stop_reason" in b:
                f["auto_stop_reason"] = str(b.get("auto_stop_reason") or "")[:200]
            st = str(b.get("state") or "")
            # 用户已经停止这一路之后，任何迟到的 starting/live/failed 都不能把
            # 房间重新点亮——包括采集代理自己的上报。停止时会话被删，代理收到
            # 404 会重建会话并回报 live；旧版本就是这样"点了停止还在录"，最后
            # 只能删房间。要重新开始必须先置回 pending（用户重试 / 断流自动恢复），
            # 所以这里不再给 source=agent 开后门。
            if r.get("state") == "stopped" and st in ("starting", "live", "failed"):
                return self._json({"room": r, "ignored": "stopped"})
            if st:
                f["state"] = st
                if st == "pending":
                    # 重试 = 重新拉起这一路，不是断点续传：旧会话链接先清掉。
                    # 断流自动收尾的标记也要清，否则下一场结束时飞书还会说"断流"。
                    f.update(stage=0, failed_stage=None, stage_note="", error="",
                             session_id="", saved=False, has_recording=False,
                             interrupted=False,
                             auto_stopped=False, auto_stop_reason="",
                             switch_ready=False, switch_engine="",
                             switch_reason="", switch_requested=False)
                elif st == "starting":
                    stage = _stage(b.get("stage"), 1)
                    f.update(stage=stage, failed_stage=None, error="", saved=False)
                    # 重新拉起时先别挂着上一场的 session_id；阶段 5 会带新的回来。
                    if "session_id" not in b and stage <= 4:
                        f["session_id"] = ""
                    # 开始跑了，旧的「待切换」入口就收掉（除非采集端明确说还要留着）。
                    f.setdefault("switch_ready", False)
                    f.setdefault("switch_requested", False)
                    if not f.get("switch_ready"):
                        f.setdefault("switch_engine", "")
                        f.setdefault("switch_reason", "")
                elif st == "live":
                    f.update(stage=_stage(b.get("stage"), 6), failed_stage=None,
                             error="", saved=False)
                    f.setdefault("switch_ready", False)
                    f.setdefault("switch_requested", False)
                    if not f.get("switch_ready"):
                        f.setdefault("switch_engine", "")
                        f.setdefault("switch_reason", "")
                elif st == "failed":
                    if b.get("failed_stage") is not None:
                        fs = _stage(b.get("failed_stage"), 0)
                    elif b.get("stage") is not None:
                        fs = _stage(b.get("stage"), 0)
                    else:
                        fs = _stage(r.get("stage"), 0)
                    f["failed_stage"] = fs
                    if "stage" in b:
                        f["stage"] = _stage(b.get("stage"), fs)
                    f["saved"] = False
                elif st == "stopped":
                    f.update(stage=6, failed_stage=None,
                             switch_ready=False, switch_engine="",
                             switch_reason="", switch_requested=False)
                    f.setdefault("saved", bool(f.get("session_id") or r.get("session_id")))
            else:
                if "stage" in b:
                    f["stage"] = _stage(b.get("stage"), 0)
                if "failed_stage" in b:
                    f["failed_stage"] = (None if b.get("failed_stage") is None
                                         else _stage(b.get("failed_stage"), 0))
            r = rooms.update(rid, **f)
            if not r:
                return self._json({"error": "房间不存在"}, 404)
            return self._json({"room": r})
        if p == "/api/rooms/switch":
            # 「直取失败，切到浏览器采集」：只有用户在收听页确认后才调用。
            # 这里只置一个请求位，采集代理下一轮 tick 看到才真正拉起浏览器。
            rid = str(b.get("id") or "")
            r = rooms.get(rid)
            if not r:
                return self._json({"error": "房间不存在"}, 404)
            if r.get("state") != "failed" or not r.get("switch_ready"):
                return self._json(
                    {"error": "这一路现在没有可切换的浏览器采集"}, 400)
            if r.get("switch_requested"):
                return self._json({"ok": True, "room": r, "already": True})
            room = rooms.update(rid, switch_requested=True)
            return self._json({"ok": True, "room": room})
        if p == "/api/capture/status":
            # 采集代理每轮 tick 上报浏览器存活；只进内存，不写 rooms.json
            rid = str(b.get("id") or "")
            if not rid:
                return self._json({"error": "缺少房间 id"}, 400)
            item = rooms.capture_update(
                rid, bool(b.get("alive", True)), pid=b.get("pid"),
                adopted=bool(b.get("adopted")), reason=str(b.get("reason") or ""))
            return self._json({"ok": True, "capture": item})
        if p == "/api/rooms/close":
            # 关房间 = 停会话 + 交给采集端回收浏览器/ffmpeg
            rid = str(b.get("id") or "")
            room = _close_room(conn, rid)
            if room is None:
                return self._json({"error": "房间不存在"}, 404)
            return self._json({"ok": True, "room": room})
        if p == "/api/profile":
            kw = b.get("keywords")
            if not isinstance(kw, list):
                return self._json({"error": "keywords 必须是数组"}, 400)
            return self._json({"keywords": profile.save([str(x) for x in kw])})
        if p == "/api/glossary":
            terms = b.get("terms")
            if not isinstance(terms, list):
                return self._json({"error": "terms 必须是数组"}, 400)
            return self._json({"terms": glossary.save(terms)})
        if p == "/api/glossary/reapply":
            sid = str(b.get("session_id") or "") or None
            return self._json(glossary.reapply(conn, sid))
        if p == "/api/utterances/update":
            sid = str(b.get("session_id") or "")
            try:
                uid = int(b.get("id"))
            except (TypeError, ValueError):
                return self._json({"error": "缺少 id"}, 400)
            text = b.get("text")
            if text is not None:
                text = str(text).strip()
                if not text:
                    return self._json({"error": "文字不能清空（要删这句请用别的操作）"}, 400)
                if len(text) > 2000:
                    return self._json({"error": "单句超过 2000 字"}, 400)
            spk = b.get("speaker_id")
            label = b.get("speaker_label") or b.get("label")
            n = store.update_utterance(conn, sid, uid,
                                       text=text,
                                       speaker_id=str(spk) if spk else None,
                                       speaker_label=str(label) if label else None)
            if not n:
                return self._json({"error": "没有这句（或不属于这场）"}, 404)
            if spk:
                live.note_reassign(sid, uid, None, str(spk), str(label or spk))
            row = conn.execute(
                "SELECT id,t_start,t_end,speaker_id,speaker_label,text FROM utterances "
                "WHERE id=?", (uid,)).fetchone()
            return self._json({"ok": True, "utterance": dict(row) if row else {}})
        if p == "/api/highlights/build":
            sid = str(b.get("session_id") or "")
            if not sid:
                return self._json({"error": "session_id required"}, 400)
            if not store.get_session(conn, sid):
                return self._json({"error": "场次不存在"}, 404)
            limit = min(50, int(b.get("limit") or highlight.LIMIT))
            out = highlight.build(conn, sid, limit=limit)
            return self._json(out)
        if p == "/api/highlights/feedback":
            sid = str(b.get("session_id") or "")
            hid = int(b.get("id") or 0)
            verdict = int(b.get("verdict") or 0)
            if not sid or not hid or verdict not in (1, -1, 0):
                return self._json({"error": "session_id / id / verdict 参数不对"}, 400)
            r = store.set_highlight_feedback(conn, sid, hid, verdict)
            if not r.get("updated"):
                return self._json({"error": "这条高光不存在"}, 404)
            r["meta"] = store.highlights_meta(conn, sid)
            # 打完标立刻看到"偏好变了"，不用刷新页面——反馈闭环要当场有回声
            r["learned"] = feedback.details(conn)
            return self._json(r)
        if p == "/api/highlights/manual":
            sid = str(b.get("session_id") or "")
            if not sid or not store.get_session(conn, sid):
                return self._json({"error": "场次不存在"}, 404)
            try:
                uid = int(b.get("utterance_id") or 0)
            except (TypeError, ValueError):
                uid = 0
            t0 = b.get("t_start")
            try:
                t0 = float(t0) if t0 is not None else None
            except (TypeError, ValueError):
                t0 = None
            if not uid and t0 is None:
                return self._json({"error": "需要 utterance_id 或 t_start"}, 400)
            out = store.toggle_manual_highlight(
                conn, sid, utterance_id=uid, t_start=t0,
                text=str(b.get("text") or ""),
                speaker_label=str(b.get("speaker_label") or ""))
            return self._json(out)
        if p == "/api/live/start":
            try:
                # 采集端可以指定来源；direct 由 tools/direct_capture.py 传入。
                # 老采集页不传时仍按标签页采集，行为不变。
                mode = str(b.get("source_mode") or "tab").strip().lower()
                if mode not in SOURCE_MODE_META:
                    mode = "tab"
                room_id = str(b.get("room_id") or "").strip()
                # 同一路重复 start 只认已有会话：桌面壳恢复采集、采集页刷新、
                # 用户连点两次「开始听」，都不该再产生一个同标题双胞胎。
                with _LIVE_START_LOCK:
                    if room_id:
                        room = rooms.get(room_id)
                        sid = str((room or {}).get("session_id") or "")
                        if sid:
                            try:
                                st = live.status(sid)
                                st.update({"session_id": sid, "reused": True,
                                           "room_id": room_id})
                                return self._json(st, 200)
                            except live.LiveError:
                                pass
                    out = live.start(str(b.get("title") or ""), source_mode=mode)
                    if room_id and rooms.get(room_id):
                        rooms.update(room_id, session_id=out["session_id"])
                        out["room_id"] = room_id
                    out["reused"] = False
                    return self._json(out, 201)
            except live.LiveError as e:
                return self._json({"error": str(e),
                                   "hint": "pip install -r requirements-live.txt 并运行 python tools/fetch_models.py"}, 503)
        if p == "/api/live/stop":
            sid = str(b.get("session_id") or "")
            try:
                out = live.stop(sid)
            except live.LiveError as e:
                # 服务重启后会话对象已经没了，但房间还挂着 session_id：
                # 采集页关页时发来的 stop 仍应把房间落到 stopped。
                room = _mark_room_stopped(conn, sid)
                if room:
                    return self._json({"ok": True, "stopped": True, "room": room})
                return self._json({"error": str(e)}, 404)
            # 从 /live、采集页 beforeunload 停的场次也要把房间落到 stopped；
            # 否则采集页已经关了，收听页还显示「在听」。
            room = _mark_room_stopped(conn, sid)
            if room:
                out["room"] = room
            return self._json(out)
        if p == "/api/live/pause":
            try:
                return self._json(live.set_paused(str(b.get("session_id") or ""),
                                                 bool(b.get("paused", True))))
            except live.LiveError as e:
                return self._json({"error": str(e)}, 404)
        if p == "/api/live/mark":
            try:
                return self._json(live.mark(str(b.get("session_id") or ""),
                                            str(b.get("note") or "")))
            except live.LiveError as e:
                return self._json({"error": str(e)}, 404)
        if p == "/api/live/reject_hint":
            sid = str(b.get("session_id") or "")
            spk = str(b.get("speaker_id") or "")
            if not sid or not spk:
                return self._json({"error": "缺少 session_id / speaker_id"}, 400)
            return self._json(live.reject_hint(sid, spk))
        if p == "/api/live/cancel":
            return self._json(live.cancel(str(b.get("session_id") or "")))
        if p == "/api/analysis/start":
            sid = str(b.get("session_id") or "")
            try:
                return self._json(analysis_job.start(conn, sid, str(b.get("kind") or "full")))
            except RuntimeError as e:
                return self._json({"error": str(e)}, 400)
        if p == "/api/analysis/cancel":
            return self._json(analysis_job.cancel(conn, str(b.get("session_id") or "")))
        if p == "/api/summarize/estimate":
            sid = str(b.get("session_id") or "")
            f = parse_filters(b.get("filters") or {})
            f["session_id"] = sid
            try:
                return self._json(analysis.estimate(conn, sid, f,
                                                    str(b.get("mode") or "content")))
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if p == "/api/summarize":
            sid = str(b.get("session_id") or "")
            f = parse_filters(b.get("filters") or {})
            f["session_id"] = sid
            # 只有客户端完全没表态时才用默认值；不能覆盖显式传来的 false，
            # 否则「按分类总结」永远看不到广告/闲聊分组（需求 6）。
            raw_f = b.get("filters") or {}
            if "exclude_low_value" not in raw_f:
                f["exclude_low_value"] = bool(b.get("exclude_low_value", True))
            # 重叠段本来就不该进总结（和结束收尾那条链路保持一致）；
            # 客户端没表态时按 true 兜底，别因为少传一个字段就把不可信片段算进去。
            if "exclude_overlap" not in raw_f:
                f["exclude_overlap"] = True
            try:
                # 入全局队列：重复点击/下播自动总结会命中同一指纹，不产生第二次调用。
                job = tasks.submit_summary(sid, f, str(b.get("mode") or "content"),
                                           str(b.get("engine") or "auto"),
                                           force=bool(b.get("force")))
                out = job.wait(timeout=scheduler.SCHEDULER.task_timeout)
            except scheduler.JobTimeout as e:
                return self._json({"error": str(e), "job_id": e.job.id,
                                   "status": e.job.status}, 504)
            except scheduler.JobFailed as e:
                payload, status = _job_failure(e, sid)
                return self._json(payload, status)
            if isinstance(out, dict):
                out = dict(out)
                if job.cached:
                    # 调度器命中 10 分钟内的成功结果：对前端来说就是缓存命中。
                    out["cached"] = True
                out["job"] = job.public()
            return self._json(out)
        if p == "/api/settings/prefs":
            return self._json(settings.set_prefs(b if isinstance(b, dict) else {}))
        if p == "/api/settings":
            if not isinstance(b, dict):
                return self._json({"error": "格式不对"}, 400)
            base = str(b.get("base") or "").strip()
            if base and not base.startswith(("http://", "https://")):
                return self._json({"error": "接口地址要以 http:// 或 https:// 开头"}, 400)
            return self._json(settings.upsert(b))
        if p in ("/api/settings/activate", "/api/settings/delete", "/api/settings/duplicate"):
            pid = str((b or {}).get("id") or "")
            fn = {"/api/settings/activate": settings.activate,
                  "/api/settings/delete": settings.delete,
                  "/api/settings/duplicate": settings.duplicate}[p]
            try:
                return self._json(fn(pid))
            except KeyError:
                return self._json({"error": "这份配置不存在"}, 404)
        if p == "/api/share/minutes":
            sid = str((b or {}).get("session_id") or "")
            url = str((b or {}).get("url") or "").strip()
            if not store.get_session(conn, sid):
                return self._json({"error": "场次不存在"}, 404)
            if url and not url.startswith(("http://", "https://")):
                return self._json({"error": "妙记链接要以 http:// 或 https:// 开头"}, 400)
            store.set_minutes_url(conn, sid, url or None)
            return self._json({"ok": True, "session_id": sid, "minutes_url": url})
        if p == "/api/library/batch":
            action = str(b.get("action") or "")
            raw_ids = b.get("session_ids")
            ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else []
            ids = [x for x in dict.fromkeys(ids) if x][:500]
            if action not in ("trash", "restore", "purge", "drop_audio"):
                return self._json(
                    {"error": "action 必须是 trash/restore/purge/drop_audio"}, 400)
            if not ids:
                return self._json({"error": "没有选中的场次"}, 400)
            active = {str(s.get("id") or "") for s in live.list_active()}
            done: list[str] = []
            skipped: list[dict] = []
            for sid in ids:
                s = store.get_session(conn, sid)
                if not s:
                    skipped.append({"id": sid, "reason": "场次不存在"})
                    continue
                if sid in active:
                    skipped.append({"id": sid, "reason": "正在录制，先停止采集"})
                    continue
                if action == "trash":
                    store.trash_session(conn, sid)
                    _move_audio(sid, to_trash=True)
                elif action == "restore":
                    if not s.get("deleted_at"):
                        skipped.append({"id": sid, "reason": "不在回收站"})
                        continue
                    store.restore_session(conn, sid)
                    _move_audio(sid, to_trash=False)
                elif action == "purge":
                    store.purge_session(conn, sid)
                    for suffix in (".wav", ".pcm", ".m4a", ".opus"):
                        for d in (AUDIO_DIR, TRASH_DIR):
                            try:
                                (d / (sid + suffix)).unlink(missing_ok=True)
                            except OSError:
                                pass
                else:
                    for key in ("audio_path", "audio_original_path"):
                        ap = s.get(key)
                        if ap:
                            try:
                                Path(ap).unlink(missing_ok=True)
                            except OSError:
                                pass
                    store.drop_audio(conn, sid)
                done.append(sid)
            return self._json({"ok": True, "done": done, "skipped": skipped})
        if p == "/api/export/batch":
            raw_ids = b.get("session_ids")
            ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else []
            fmt = str(b.get("format") or "md").lower()
            if fmt not in ("md", "srt", "txt"):
                fmt = "md"
            buf = io.BytesIO()
            n = 0
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for sid in dict.fromkeys(ids):
                    if not store.get_session(conn, sid):
                        continue
                    try:
                        body, _ctype, name = export.build(conn, sid, fmt)
                    except Exception:
                        traceback.print_exc()
                        continue
                    z.writestr(name, body)
                    n += 1
            if not n:
                return self._json({"error": "没有可导出的场次"}, 404)
            fname = "livenotes-export-%s.zip" % datetime.now().strftime("%Y%m%d-%H%M")
            return self._attachment_bytes(buf.getvalue(), "application/zip", fname)
        if p in ("/api/library/trash", "/api/library/restore",
                 "/api/library/purge", "/api/library/drop_audio"):
            sid = str(b.get("session_id") or "")
            s = store.get_session(conn, sid)
            if not s:
                return self._json({"error": "场次不存在"}, 404)
            if p == "/api/library/trash":
                store.trash_session(conn, sid)
                _move_audio(sid, to_trash=True)
            elif p == "/api/library/restore":
                store.restore_session(conn, sid)
                _move_audio(sid, to_trash=False)
            elif p == "/api/library/purge":
                store.purge_session(conn, sid)
                for suffix in (".wav", ".pcm", ".m4a", ".opus"):
                    for d in (AUDIO_DIR, TRASH_DIR):
                        try:
                            (d / (sid + suffix)).unlink(missing_ok=True)
                        except OSError:
                            pass
            else:
                for key in ("audio_path", "audio_original_path"):
                    ap = s.get(key)
                    if ap:
                        try:
                            Path(ap).unlink(missing_ok=True)
                        except OSError:
                            pass
                store.drop_audio(conn, sid)
            return self._json({"ok": True})
        if p == "/api/polish":
            sid = str(b.get("session_id") or "")
            try:
                job = tasks.submit_polish(sid, force=bool(b.get("force")))
                out = job.wait(timeout=scheduler.SCHEDULER.task_timeout)
            except scheduler.JobTimeout as e:
                return self._json({"ok": False, "error": str(e), "job_id": e.job.id}, 504)
            except scheduler.JobFailed as e:
                payload, status = _job_failure(e, sid)
                payload["ok"] = False
                return self._json(payload, status)
            if isinstance(out, dict) and job.cached:
                out = dict(out, cached=True)
            return self._json(out)
        if p == "/api/jobs/retry":
            jid = str(b.get("job_id") or "")
            if not jid:
                return self._json({"error": "缺少 job_id"}, 400)
            try:
                job = scheduler.SCHEDULER.retry(jid)
            except KeyError:
                return self._json({"error": "任务不存在"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "job": job.public()})
        if p == "/api/llm/test":
            # 表单值优先：没保存也能测，否则用户会以为接口坏了
            return self._json(llm.test_connection(b if isinstance(b, dict) else None))
        if p == "/api/llm/models":
            ov = {k: b.get(k) for k in ("base", "key") if isinstance(b, dict) and b.get(k)}
            return self._json(llm.list_models(ov))
        if p == "/api/llm/probe":
            ov = {k: b.get(k) for k in ("base", "key") if isinstance(b, dict) and b.get(k)}
            return self._json(llm.probe_many(ov, list(b.get("models") or [])))
        if p == "/api/demo/seed":
            # 演示数据改成显式加载：空库不再自动生成，已有演示场次可还原。
            existing = store.get_session(conn, seed_demo.SESSION_ID)
            if existing:
                restored = bool(existing.get("deleted_at"))
                if restored:
                    store.restore_session(conn, seed_demo.SESSION_ID)
                return self._json({"ok": True, "session_id": seed_demo.SESSION_ID,
                                   "restored": restored})
            n = seed_demo.seed(conn)
            return self._json({"ok": True, "session_id": seed_demo.SESSION_ID,
                               "rows": n})
        if p == "/api/sessions/rename":
            sid = str(b.get("session_id") or "")
            title = str(b.get("title") or "").strip()[:60]
            if not title:
                return self._json({"error": "标题不能为空"}, 400)
            # 人工改名 = 用户命名，此后自动总结不再覆盖它
            if not store.rename_session(conn, sid, title, source="user"):
                return self._json({"error": "场次不存在"}, 404)
            return self._json({"ok": True, "session": store.get_session(conn, sid)})
        if p == "/api/sessions/title/adopt":
            sid = str(b.get("session_id") or "")
            if not store.get_session(conn, sid):
                return self._json({"error": "场次不存在"}, 404)
            try:
                return self._json(titles.adopt(conn, sid, b.get("title")))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if p in ("/api/classify/ai", "/api/classify/restore"):
            sid = str(b.get("session_id") or "")
            if not store.get_session(conn, sid):
                return self._json({"error": "场次不存在"}, 404)
            if p == "/api/classify/restore":
                return self._json(classify.restore(conn, sid))
            try:
                topics = b.get("topics")
                job = tasks.submit_classify(
                    sid, offset=int(b.get("offset") or 0),
                    limit=int(b.get("limit") or classify.BATCH),
                    topics=topics if isinstance(topics, list) else None,
                    force=bool(b.get("force")))
                out = job.wait(timeout=scheduler.SCHEDULER.task_timeout)
            except scheduler.JobTimeout as e:
                return self._json({"ok": False, "error": str(e), "job_id": e.job.id}, 504)
            except scheduler.JobFailed as e:
                payload, status = _job_failure(e, sid)
                payload["ok"] = False
                return self._json(payload, status)
            return self._json(out)
        if p == "/api/utterances/reassign":
            sid = str(b.get("session_id") or "")
            uid = b.get("utterance_id")
            t0 = b.get("t_start")
            spk = str(b.get("speaker_id") or "")
            if not sid:
                return self._json({"error": "缺少 session_id"}, 400)
            if spk in ("", "__new__"):
                # 新建一个说话人：挑一个没被占用的 SPK_xx
                used = {r["speaker_id"] for r in conn.execute(
                    "SELECT speaker_id FROM speakers WHERE session_id=?", (sid,))}
                n = 0
                while "SPK_%02d" % n in used:
                    n += 1
                spk = "SPK_%02d" % n
            if not str(b.get("label") or "").strip() and spk.startswith("SPK_"):
                pass
            # 目标说话人的显示名：优先用传入的，其次查 speakers 表，最后退回 id
            label = str(b.get("label") or "").strip()
            if not label:
                row = conn.execute("SELECT display_name FROM speakers WHERE session_id=? "
                                   "AND speaker_id=?", (sid, spk)).fetchone()
                label = (row["display_name"] if row and row["display_name"] else spk)
            if uid is not None:
                n = store.set_utterance_speaker(conn, sid, int(uid), spk, label)
            elif t0 is not None:
                conn.execute("UPDATE utterances SET speaker_id=?, speaker_label=? "
                             "WHERE session_id=? AND ABS(t_start-?)<0.05",
                             (spk, label, sid, float(t0)))
                conn.commit()
                n = 1
            else:
                return self._json({"error": "缺少 utterance_id / t_start"}, 400)
            store.upsert_speaker(conn, sid, spk, label)
            live.note_reassign(sid, int(uid) if uid is not None else None,
                               float(t0) if t0 is not None else None, spk, label)
            return self._json({"ok": True, "changed": n, "speaker_id": spk, "label": label})
        if p == "/api/speakers/recluster":
            sid = str(b.get("session_id") or "")
            try:
                k = int(b.get("k") or 0) or None
            except (TypeError, ValueError):
                k = None
            try:
                return self._json(live.recluster(sid, k))
            except live.LiveError as e:
                return self._json({"error": str(e)}, 400)
        if p == "/api/speakers/rename":
            store.rename_speaker(conn, b["session_id"], b["speaker_id"], str(b["name"])[:40])
            live.note_rename(b["session_id"], b["speaker_id"], str(b["name"])[:40])
            return self._json({"ok": True, "meta": store.get_meta(conn, b["session_id"])})
        if p == "/api/persons/rematch":
            sid = str(b.get("session_id") or "")
            if sid:
                return self._json(voiceprint.match_session(conn, sid))
            return self._json(voiceprint.match_all(conn))
        if p == "/api/persons/confirm":
            sid = str(b.get("session_id") or "")
            spk = str(b.get("speaker_id") or "")
            pid = str(b.get("person_id") or "")
            state = str(b.get("state") or "confirmed")
            if not sid or not spk:
                return self._json({"error": "缺少 session_id / speaker_id"}, 400)
            if state == "rejected" or not pid:
                store.set_speaker_person(conn, sid, spk, None, None, None,
                                         "rejected" if state == "rejected" else "")
                return self._json({"ok": True, "state": state})
            person = store.get_person(conn, pid)
            if not person:
                return self._json({"error": "人物不存在"}, 404)
            store.set_speaker_person(conn, sid, spk, pid, None, None, "confirmed")
            # 先占座：哪怕这一簇声纹太少、enroll 失败，也不能让同场另一个人继续挂着同一个 pid
            cleared = store.clear_person_claims(conn, sid, pid, spk)
            store.rename_speaker(conn, sid, spk, person["name"])   # 名字跟着落，界面才对得上
            # 人工确认 = 一次高质量注册：把这一簇的原型并进那个人，下次更容易认出来
            out = voiceprint.enroll_speaker(conn, sid, spk, person["name"], person_id=pid)
            live.note_rename(sid, spk, person["name"], enroll=False)
            return self._json({"ok": True, "state": "confirmed", "person_id": pid,
                               "name": person["name"], "added": out.get("added", 0),
                               "cleared": out.get("cleared") or cleared})
        if p == "/api/persons/enroll":
            sid = str(b.get("session_id") or "")
            spk = str(b.get("speaker_id") or "")
            name = str(b.get("name") or "").strip()
            if not sid or not spk or not name:
                return self._json({"error": "缺少 session_id / speaker_id / name"}, 400)
            out = voiceprint.enroll_speaker(conn, sid, spk, name,
                                            force_new=bool(b.get("force_new")))
            if out.get("ok"):
                store.rename_speaker(conn, sid, spk, name)
                live.note_rename(sid, spk, name, enroll=False)
            return self._json(out)
        if p == "/api/persons/merge":
            n = store.merge_persons(conn, str(b.get("src_id") or ""), str(b.get("dst_id") or ""))
            return self._json({"ok": True, "moved": n})
        if p == "/api/persons/rename":
            pid = str(b.get("person_id") or "")
            name = str(b.get("name") or "").strip()[:40]
            if not store.get_person(conn, pid) or not name:
                return self._json({"error": "人物不存在或名字为空"}, 400)
            store.upsert_person(conn, pid, name)
            return self._json({"ok": True, "person": store.get_person(conn, pid)})
        if p == "/api/persons/forget":
            store.delete_person(conn, str(b.get("person_id") or ""))
            return self._json({"ok": True})
        if p == "/api/speakers/merge":
            sid = str(b["session_id"])
            src_ids = _list(b.get("src_ids"))
            target_id = str(b["target_id"])
            target_label = str(b.get("target_label") or target_id)
            n = store.merge_speakers(conn, sid, src_ids, target_id, target_label)
            live.note_merge(sid, src_ids, target_id, target_label)
            return self._json({"ok": True, "merged": n})
        return self._json({"error": "unknown endpoint " + p}, 404)


class _Server(ThreadingHTTPServer):
    """N 路采集的连接闸门。

    stdlib 默认 request_queue_size=5，而 HTTP/1.0 下每个请求都是一条新连接：
    5 路采集页 + 面板轮询时，每秒几十条短连接。Windows 在 accept 队列满时
    直接回 RST，采集代理看到的就是 WinError 10061「目标计算机积极拒绝」。
    队列放大到 128，给突发留余量。
    """
    request_queue_size = 128


def make_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    # Windows 上 SO_REUSEADDR 允许两个进程绑同一个端口，结果是请求随机落到其中
    # 一个（表现为"页面时好时坏"，很难查）。这里先用一个不带 REUSEADDR 的裸 socket
    # 探一下，被占用就直接报清楚，而不是静默双绑。
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as e:
        raise SystemExit(
            "端口 %d 已被占用（%s）。多半是上一次的服务没退干净，先关掉它再启动。"
            % (port, e.strerror or e)) from e
    finally:
        probe.close()
    # 断流看门狗跟着服务一起起：无人值守过夜时，它是唯一会替人按下"停止"的东西。
    start_watchdog()
    return _Server((host, port), Handler)
