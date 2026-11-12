# -*- coding: utf-8 -*-
"""进程内指标注册表 + 采样落库。

设计约束和 app/asr_pool.py 的窗口统计保持一致：
  - 纯标准库，不引入 Prometheus / OpenTelemetry 依赖；
  - 采集线程、ASR worker、HTTP 线程都会写，计数必须线程安全；
  - 每场一份 counters / gauges / 延迟窗口，场次结束后落一条 session_metrics；
  - 全局快照每 ``LIVENOTES_METRICS_SAMPLE_S`` 秒写一批 metrics_samples，
    保留 ``LIVENOTES_METRICS_RETENTION_DAYS`` 天，供事后复盘「那几分钟」。

进程内指标是执行真相；SQLite 负责历史和重启后仍能看到。两者都失败时，
业务照常跑——指标永远不能拖垮录音与识别。
"""
from __future__ import annotations

import collections
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from . import logs
from .config import (LLM_PRICE_INPUT_PER_MTOK, LLM_PRICE_OUTPUT_PER_MTOK,
                     METRICS_RETENTION_DAYS, METRICS_SAMPLE_S, SESSION_METRICS_MAX)

WINDOW_MAX = 2048


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(vals[int(k)])
    return float(vals[lo] * (hi - k) + vals[hi] * (k - lo))


class Window:
    """固定长度的延迟窗口，送进来看最新值；P50/P95 按窗口内样本算。"""

    __slots__ = ("_vals", "_n")

    def __init__(self, maxlen: int = WINDOW_MAX) -> None:
        self._vals: collections.deque[float] = collections.deque(maxlen=maxlen)
        self._n = 0

    def add(self, value: float) -> None:
        try:
            self._vals.append(float(value))
            self._n += 1
        except (TypeError, ValueError):
            pass

    def snapshot(self) -> dict[str, Any]:
        vals = sorted(self._vals)
        if not vals:
            return {"n": 0, "min": 0.0, "max": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0}
        return {
            "n": self._n,
            "min": round(vals[0], 4),
            "max": round(vals[-1], 4),
            "avg": round(sum(vals) / len(vals), 4),
            "p50": round(_pct(vals, 0.50), 4),
            "p95": round(_pct(vals, 0.95), 4),
        }

    def reset(self) -> None:
        self._vals.clear()
        self._n = 0


class MetricSet:
    """一组计数器 / 瞬时值 / 延迟窗口。所有方法可跨线程调用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._windows: dict[str, Window] = {}

    def inc(self, name: str, value: float = 1.0) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + v

    def set(self, name: str, value: float | None) -> None:
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._gauges[name] = v

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            w = self._windows.get(name)
            if w is None:
                w = self._windows[name] = Window()
        w.add(value)

    def value(self, name: str) -> float:
        with self._lock:
            return float(self._counters.get(name, self._gauges.get(name, 0.0)))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = {k: round(v, 4) for k, v in self._counters.items()}
            gauges = {k: round(v, 4) for k, v in self._gauges.items()}
            windows = {k: w.snapshot() for k, w in self._windows.items()}
        return {"counters": counters, "gauges": gauges, "windows": windows}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._windows.clear()


class Registry:
    """全局指标 + 每场指标 + 已结束场次的最近视图。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._global = MetricSet()
        self._sessions: collections.OrderedDict[str, MetricSet] = collections.OrderedDict()
        self._views: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self.started_at = time.time()
        self.pid = os.getpid()
        self._cpu_last = time.process_time()
        self._cpu_wall = time.monotonic()
        self._cpu_percent = 0.0
        self._sampler: threading.Thread | None = None
        self._stop = threading.Event()
        self._interval = max(5.0, float(METRICS_SAMPLE_S))
        self._last_prune = 0.0

    # ---------------------------------------------------------- 写入
    def session(self, session_id: str) -> MetricSet:
        sid = str(session_id or "")
        with self._lock:
            ms = self._sessions.get(sid)
            if ms is None:
                ms = self._sessions[sid] = MetricSet()
                while len(self._sessions) > SESSION_METRICS_MAX:
                    self._sessions.popitem(last=False)
            else:
                self._sessions.move_to_end(sid)
            return ms

    def inc(self, name: str, value: float = 1.0, session_id: str = "") -> None:
        self._global.inc(name, value)
        if session_id:
            self.session(session_id).inc(name, value)

    def set(self, name: str, value: float | None, session_id: str = "") -> None:
        self._global.set(name, value)
        if session_id:
            self.session(session_id).set(name, value)

    def observe(self, name: str, value: float, session_id: str = "") -> None:
        self._global.observe(name, value)
        if session_id:
            self.session(session_id).observe(name, value)

    # ---------------------------------------------------------- 查询
    def uptime(self) -> float:
        return round(time.time() - self.started_at, 1)

    def global_value(self, name: str) -> float:
        return self._global.value(name)

    def session_snapshot(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            ms = self._sessions.get(str(session_id or ""))
        return ms.snapshot() if ms is not None else {}

    def ended_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._views.values()]

    def snapshot(self) -> dict[str, Any]:
        now_wall, cpu = time.monotonic(), time.process_time()
        elapsed = now_wall - self._cpu_wall
        if elapsed > 0:
            pct = (cpu - self._cpu_last) / elapsed * 100.0
            # 平滑一下，避免一次采样刚好撞上短命线程导致尖峰
            self._cpu_percent = (self._cpu_percent * 0.6 + pct * 0.4)
        self._cpu_last, self._cpu_wall = cpu, now_wall
        with self._lock:
            sessions = {sid: ms.snapshot() for sid, ms in self._sessions.items()}
        return {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "uptime_s": self.uptime(),
            "pid": self.pid,
            "threads": threading.active_count(),
            "cpu_percent": round(self._cpu_percent, 2),
            "global": self._global.snapshot(),
            "sessions": sessions,
        }

    # ---------------------------------------------------------- 生命周期
    def reset(self) -> None:
        """测试夹具用：清空进程内状态，不影响正在跑的线程。"""
        with self._lock:
            self._global.reset()
            self._sessions.clear()
            self._views.clear()
        self.started_at = time.time()
        self._cpu_last = time.process_time()
        self._cpu_wall = time.monotonic()
        self._cpu_percent = 0.0

    def record_session_end(self, session_id: str, view: dict[str, Any],
                           extra: dict[str, Any] | None = None) -> None:
        """场次结束：内存留一份最近视图，同时落 session_metrics 供跨重启复盘。"""
        sid = str(session_id or "")
        if not sid:
            return
        data = dict(view or {})
        # 调用方可能只传一个精简视图；指标快照由注册表补上，汇总里总能看到。
        data.setdefault("metrics", self.session_snapshot(sid))
        if extra:
            data.update(extra)
        data["finished_at"] = data.get("finished_at") or datetime.now().isoformat(
            timespec="seconds")
        with self._lock:
            self._views[sid] = data
            while len(self._views) > SESSION_METRICS_MAX:
                self._views.popitem(last=False)
        self.inc("live.sessions_finished")
        try:
            from . import store
            store.save_session_metrics(sid, data,
                                       started_at=str(data.get("started_at") or ""),
                                       finished_at=str(data.get("finished_at") or ""))
        except Exception as e:
            logs.log_event("metrics.session_end_failed", level="warn",
                           session_id=sid, error=str(e))

    # ---------------------------------------------------------- 采样
    def start_sampler(self, interval: float | None = None) -> None:
        with self._lock:
            if self._sampler is not None and self._sampler.is_alive():
                return
            self._stop.clear()
            if interval:
                self._interval = max(5.0, float(interval))
            self._sampler = threading.Thread(target=self._sample_loop,
                                             name="metrics-sampler", daemon=True)
            self._sampler.start()

    def stop_sampler(self) -> None:
        self._stop.set()
        th = self._sampler
        if th is not None and th.is_alive():
            th.join(timeout=3)
        self._sampler = None

    def _sample_loop(self) -> None:
        # 启动后先采一次，别让第一分钟是空白
        first = True
        while True:
            if not first:
                if self._stop.wait(self._interval):
                    return
            first = False
            try:
                sample_once()
            except Exception as e:
                logs.log_event("metrics.sample_failed", level="warn", error=str(e))


METRICS = Registry()


# ---------------------------------------------------------------- 组合快照

def build_snapshot() -> dict[str, Any]:
    """进程指标 + 实时链路 + LLM 调度 + 今日用量。接口与采样器共用一套口径。"""
    snap = METRICS.snapshot()
    active: list[dict] = []
    asr: dict = {}
    try:
        from . import live
        active = live.metrics_sessions()
        asr = live.POOL.stats()
    except Exception as e:
        snap["live_error"] = str(e)
    ended = [v for v in METRICS.ended_sessions()
             if v.get("id") not in {s.get("id") for s in active}]
    snap["live"] = {"sessions": active + ended, "asr": asr,
                    "active": len(active), "ended": len(ended)}
    try:
        from . import scheduler
        snap["llm"] = scheduler.SCHEDULER.stats_summary()
        snap["llm"]["budget"] = scheduler.SCHEDULER.budget_status()
    except Exception as e:
        snap["llm"] = {"error": str(e)}
    snap["db"] = db_daily_summary()
    return snap


def db_daily_summary() -> dict[str, Any]:
    """今日 token / 推送 / 场次。读库失败返回空壳，不影响接口其它部分。"""
    out: dict[str, Any] = {
        "day": datetime.now().strftime("%Y-%m-%d"),
        "tokens": {"calls": 0, "prompt": 0, "completion": 0, "total": 0, "cost_usd": 0.0},
        "pushes": {"sent": 0, "failed": 0, "skipped": 0, "pending": 0},
        "sessions": 0, "events_24h": 0,
    }
    try:
        from . import store
        day = out["day"]
        rows = store.llm_usage_breakdown(day=day, limit=2000)
        for r in rows:
            out["tokens"]["calls"] += int(r.get("calls") or 0)
            out["tokens"]["prompt"] += int(r.get("prompt_tokens") or 0)
            out["tokens"]["completion"] += int(r.get("completion_tokens") or 0)
            out["tokens"]["total"] += int(r.get("total_tokens") or 0)
        cost = (out["tokens"]["prompt"] / 1e6 * LLM_PRICE_INPUT_PER_MTOK
                + out["tokens"]["completion"] / 1e6 * LLM_PRICE_OUTPUT_PER_MTOK)
        out["tokens"]["cost_usd"] = round(cost, 6)
        since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
        conn = store.connect()
        try:
            for r in conn.execute(
                    "SELECT status,COUNT(*) AS n FROM push_log "
                    "WHERE substr(created_at,1,10)=? GROUP BY status", (day,)):
                st = str(r["status"])
                if st in out["pushes"]:
                    out["pushes"][st] = int(r["n"] or 0)
            out["sessions"] = int(conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE substr(started_at,1,10)=?",
                (day,)).fetchone()[0] or 0)
            out["events_24h"] = int(conn.execute(
                "SELECT COUNT(*) FROM metric_events WHERE ts>=?",
                (since,)).fetchone()[0] or 0)
        finally:
            conn.close()
    except Exception as e:
        out["error"] = str(e)
    return out


def sample_once() -> int:
    """采一次快照并落库。返回写入的行数。"""
    snap = build_snapshot()
    ts = str(snap.get("ts") or datetime.now().isoformat(timespec="seconds"))
    live = snap.get("live") or {}
    sessions = live.get("sessions") or []
    global_data = {k: v for k, v in snap.items() if k != "live"}
    global_data["live"] = {"active": live.get("active", 0),
                           "ended": live.get("ended", 0),
                           "asr": live.get("asr") or {}}
    rows = [{"ts": ts, "scope": "global", "session_id": "", "data": global_data}]
    for s in sessions:
        if not s.get("running"):
            continue          # 已结束的场次在 record_session_end 时已经落过一次
        rows.append({"ts": ts, "scope": "session",
                     "session_id": str(s.get("id") or ""), "data": s})
    try:
        from . import store
        n = store.record_metric_samples(rows)
        if time.time() - METRICS._last_prune > 3600:
            METRICS._last_prune = time.time()
            store.prune_metrics(days=METRICS_RETENTION_DAYS)
        return n
    except Exception as e:
        logs.log_event("metrics.persist_failed", level="warn", error=str(e))
        return 0


# ---------------------------------------------------------------- Prometheus

def _san(v: Any) -> str:
    out = []
    for ch in str(v):
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    name = "".join(out)
    if name and name[0].isdigit():
        name = "_" + name
    return name or "_"


def _flatten(prefix: str, obj: Any, out: list[tuple[str, float]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(prefix + "_" + _san(k), v, out)
    elif isinstance(obj, bool):
        out.append((prefix, 1.0 if obj else 0.0))
    elif isinstance(obj, (int, float)):
        out.append((prefix, float(obj)))


def prometheus_text(snap: dict[str, Any] | None = None) -> str:
    """把嵌套快照拍平成 Prometheus 文本格式，方便以后接外部监控。"""
    data = build_snapshot() if snap is None else snap
    flat: list[tuple[str, float]] = []
    _flatten("livenotes", data, flat)
    lines = []
    seen: set[str] = set()
    for name, val in flat:
        if name not in seen:
            seen.add(name)
            lines.append("# TYPE %s gauge" % name)
        lines.append("%s %s" % (name, ("%g" % val)))
    return "\n".join(lines) + "\n"
