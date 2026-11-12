# -*- coding: utf-8 -*-
"""桌面壳与排障用的紧凑诊断快照。

为什么单独一个接口，而不是让壳去拼 ``/api/health`` + ``/api/live/list`` +
``/api/rooms`` + ``/api/metrics``：

- 断流判断要看「最后一块音频距今多久」。``/api/live/list`` 对「从来没收到过
  第一块」的场次给 ``last_chunk_ago_s=null``，壳无法区分「刚建会话」和
  「建完就断了」；这里直接用 ``live.stalled_sessions(0)``，它按会话创建时间
  起算，口径和引擎自己的断流看门狗完全一致。
- 诊断包要在引擎已经崩了的时候也能有内容，所以同时返回最近的结构化事件与
  日志尾巴，壳只负责落盘和打包。
- 这是排障接口，不是业务接口：不带字幕正文、不带 LLM Key、房间 URL 只保留
  scheme/host/path，去掉 query（很多直播链接的 query 里带短时 token）。

接口本身不鉴权，和现有 ``/api/*`` 一样只监听回环地址；壳通过
``http://127.0.0.1:<port>/api/diagnostics`` 取。
"""
from __future__ import annotations

import os
import platform
import sys
from datetime import datetime
from urllib.parse import urlsplit

from . import live, logs, metrics, rooms, scheduler, store
from .config import (DATA_DIR, HOST, LIVE_STALL_STOP_S, LIVE_STALL_WARN_S,
                     MODELS_DIR, PORT, SAMPLE_RATE)

_ROOM_FIELDS = (
    "id", "seq", "name", "state", "stage", "failed_stage", "stage_note",
    "stage_at", "error", "session_id", "capture_engine", "switch_ready",
    "switch_engine", "switch_reason", "switch_requested", "saved",
    "has_recording", "interrupted", "auto_stopped", "auto_stop_reason",
    "created_at", "updated_at",
)


def _safe_url(url: str) -> str:
    """只留 scheme://host/path：直播链接的 query 常常带一次性签名。"""
    try:
        p = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if not p.netloc:
        return ""
    return "%s://%s%s" % (p.scheme, p.netloc, p.path or "/")


def _rooms() -> list[dict]:
    out: list[dict] = []
    try:
        rows = rooms.list_rooms()
    except Exception:
        return out
    for r in rows:
        item = {k: r.get(k) for k in _ROOM_FIELDS if k in r}
        item["url"] = _safe_url(r.get("url") or "")
        out.append(item)
    return out


def _live() -> list[dict]:
    """所有在跑的场次 + 距最后一块音频的秒数。"""
    views: dict[str, dict] = {}
    try:
        for v in live.metrics_sessions():
            views[str(v.get("id") or "")] = v
    except Exception:
        views = {}
    out: list[dict] = []
    try:
        stalled = live.stalled_sessions(0.0)
    except Exception:
        stalled = []
    for st in stalled:
        sid = str(st.get("id") or "")
        v = views.get(sid) or {}
        out.append({
            "id": sid,
            "title": st.get("title") or v.get("title") or "",
            "idle_s": st.get("idle_s"),
            "never": bool(st.get("never")),
            "running": bool(v.get("running", True)),
            "paused": bool(v.get("paused", False)),
            "elapsed": v.get("elapsed"),
            "n_utterances": v.get("n_utterances"),
            "queued": v.get("queued"),
            "dropped": v.get("dropped"),
            "error": v.get("error") or "",
            "started_at": v.get("started_at"),
        })
    return out


def _events(limit: int) -> list[dict]:
    try:
        return store.list_metric_events(limit=limit)
    except Exception:
        return []


def summary(event_limit: int = 80, log_limit: int = 30) -> dict:
    """一次调用就能回答「引擎活着吗、哪一路断了、最近出了什么错」。"""
    try:
        model_missing = live.Engine.missing()
    except Exception:
        model_missing = []
    try:
        asr = live.POOL.stats()
    except Exception:
        asr = {}
    try:
        llm = scheduler.SCHEDULER.stats_summary()
    except Exception:
        llm = {}
    try:
        capture = rooms.capture_status()
    except Exception:
        capture = {}
    return {
        "ok": True,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "engine": {
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "uptime_s": metrics.METRICS.uptime(),
            "data_dir": str(DATA_DIR),
            "models_dir": str(MODELS_DIR),
            "model_ready": not model_missing,
            "model_missing": model_missing,
            "asr": asr,
            "llm": llm,
        },
        "config": {
            "host": HOST,
            "port": PORT,
            "sample_rate": SAMPLE_RATE,
            "stall_warn_s": LIVE_STALL_WARN_S,
            "stall_stop_s": LIVE_STALL_STOP_S,
        },
        "live": _live(),
        "rooms": _rooms(),
        "capture": capture,
        "events": _events(max(1, min(500, int(event_limit)))),
        "recent_log": logs.read_recent(max(1, min(200, int(log_limit)))),
    }
