# -*- coding: utf-8 -*-
"""可观测性回归：指标窗口、采样落库、事件流、结构化日志与 HTTP 出口。

这些用例不依赖模型，和 CI 的降级路径一致；子进程测试把 DATA_DIR 指到
临时目录，避免往仓库的 data/ 里写指标和日志。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import live, logs, metrics, store


# ---------------------------------------------------------------- 内存指标

def test_window_snapshot_percentiles():
    w = metrics.Window(maxlen=100)
    for v in range(1, 101):
        w.add(v)
    snap = w.snapshot()
    assert snap["n"] == 100
    assert snap["min"] == 1 and snap["max"] == 100
    assert 49 <= snap["p50"] <= 52
    assert 94 <= snap["p95"] <= 96
    assert snap["avg"] == pytest.approx(50.5)


def test_registry_global_and_session_counters():
    metrics.METRICS.inc("chunks", 2)
    metrics.METRICS.inc("chunks", 3, session_id="S1")
    metrics.METRICS.observe("latency", 12.0, session_id="S1")
    snap = metrics.METRICS.snapshot()
    # 带 session_id 的写入会同时进全局和该场，这是有意的口径
    assert snap["global"]["counters"]["chunks"] == 5
    assert snap["sessions"]["S1"]["counters"]["chunks"] == 3
    assert snap["sessions"]["S1"]["windows"]["latency"]["n"] == 1


def test_session_end_keeps_recent_view_and_persists(db):
    metrics.METRICS.inc("live.finals", 4, session_id="S_END")
    view = {"id": "S_END", "title": "测试场", "running": False,
            "started_at": "2026-11-06T10:00:00", "finished_at": "2026-11-06T10:10:00"}
    metrics.METRICS.record_session_end("S_END", view, extra={"duration_s": 600})
    ended = metrics.METRICS.ended_sessions()
    assert any(v["id"] == "S_END" and v["duration_s"] == 600 for v in ended)
    row = store.get_session_metrics("S_END")
    assert row and row["data"]["duration_s"] == 600
    assert row["data"]["metrics"]["counters"]["live.finals"] == 4


# ---------------------------------------------------------------- 落库

def test_metric_samples_filters_and_prune(db):
    store.record_metric_samples([
        {"ts": "2020-01-01T00:00:00", "scope": "global", "data": {"v": 1}},
        {"ts": "2099-01-01T00:00:00", "scope": "global", "data": {"v": 2}},
        {"ts": "2099-01-01T00:00:00", "scope": "session",
         "session_id": "S1", "data": {"v": 3}},
    ])
    all_rows = store.list_metric_samples()
    assert len(all_rows) == 3
    recent = store.list_metric_samples(since="2099-01-01T00:00:00", scope="global")
    assert len(recent) == 1 and recent[0]["data"]["v"] == 2
    one = store.list_metric_samples(scope="session", session_id="S1")
    assert len(one) == 1 and one[0]["session_id"] == "S1"

    store.record_metric_event("warn", "live.stall", session_id="S1",
                              fields={"idle_s": 61}, ts="2020-01-01T00:00:00")
    # 老于保留期的快照和事件会被清掉；session_metrics 不跟着删
    out = store.prune_metrics(days=7)
    assert out["samples"] >= 1 and out["events"] >= 1
    assert all(r["ts"] != "2020-01-01T00:00:00" for r in store.list_metric_samples())


def test_metric_events_roundtrip(db):
    store.record_metric_event("error", "live.worker_error", session_id="S2",
                              fields={"error": "boom"})
    rows = store.list_metric_events(limit=5)
    assert rows[0]["event"] == "live.worker_error"
    assert rows[0]["fields"]["error"] == "boom"
    assert store.list_metric_events(level="info") == []


def test_sample_once_writes_global_row(db):
    n = metrics.sample_once()
    assert n >= 1
    rows = store.list_metric_samples(scope="global", limit=10)
    assert rows and "global" in rows[-1]["data"]
    assert "db" in rows[-1]["data"]


# ---------------------------------------------------------------- 日志

def test_log_event_jsonl_and_event_table(db):
    logs.log_event("unit.test", level="warn", session_id="S3", value=7)
    files = sorted((logs.LOG_DIR).glob("app-*.jsonl"))
    assert files, "结构化日志必须落盘"
    rec = json.loads(files[-1].read_text(encoding="utf-8").splitlines()[-1])
    assert rec["event"] == "unit.test"
    assert rec["level"] == "warn"
    assert rec["session_id"] == "S3"
    assert rec["value"] == 7
    events = store.list_metric_events(level="warn")
    assert any(e["event"] == "unit.test" for e in events)


# ---------------------------------------------------------------- 接口

SCRIPT = r'''
import json, sys, threading, urllib.request
from pathlib import Path
from app import metrics, server, store

data = Path(sys.argv[1])
data.mkdir(parents=True, exist_ok=True)
conn = store.connect()
store.init_db(conn)
conn.close()
store.record_metric_samples([
    {"ts": "2099-01-01T00:00:00", "scope": "global", "data": {"v": 1}},
])
srv = server.make_server("127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port
out = {}
try:
    out["metrics"] = json.load(urllib.request.urlopen(base + "/api/metrics"))
    out["series"] = json.load(urllib.request.urlopen(
        base + "/api/metrics?since=2099-01-01T00:00:00&limit=10"))
    out["prom"] = urllib.request.urlopen(
        base + "/api/metrics?format=prometheus").read().decode("utf-8")
    out["events"] = json.load(urllib.request.urlopen(base + "/api/metrics/events"))
    out["sessions"] = json.load(urllib.request.urlopen(base + "/api/metrics/sessions"))
    out["health"] = json.load(urllib.request.urlopen(base + "/api/health"))
    out["page"] = urllib.request.urlopen(base + "/metrics").read().decode("utf-8")
finally:
    srv.shutdown()
    srv.server_close()
print(json.dumps(out, ensure_ascii=True))
'''


def test_metrics_http_endpoints(tmp_path):
    env = dict(os.environ)
    env["LIVENOTES_DATA_DIR"] = str(tmp_path / "data")
    res = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path / "data")],
                         cwd=str(Path(__file__).resolve().parents[1]),
                         capture_output=True, text=True, encoding="utf-8",
                         timeout=60, env=env)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])

    m = out["metrics"]
    for key in ("ts", "uptime_s", "global", "live", "llm", "db", "events"):
        assert key in m, key
    assert m["live"]["sessions"] == []
    assert out["series"]["series"], "since 查询应返回已落库的采样"

    assert "livenotes_uptime_s" in out["prom"]
    assert "livenotes_global_counters_http_requests" in out["prom"]
    assert "events" in out["events"]
    assert out["sessions"]["sessions"] == []
    assert out["health"]["ok"] is True and out["health"]["sessions"] == 0
    assert "运行指标" in out["page"] and "/api/metrics" in out["page"]


# ---------------------------------------------------------------- 停滞

def test_metrics_view_has_dashboard_fields():
    s = live.LiveSession("视图测试")
    s.running = True
    s.id = "S_VIEW"
    s.total_samples = 40 * live.SAMPLE_RATE
    s.n_utts = 7
    s.recognized_s = 31.0
    s.speech_s = 28.0
    s.last_speech_s = 35.0
    metrics.METRICS.inc("live.chunks_in", 12, session_id="S_VIEW")
    view = s.metrics_view()
    assert view["id"] == "S_VIEW" and view["running"] is True
    assert view["elapsed"] == 40.0 and view["n_utterances"] == 7
    assert view["metrics"]["counters"]["live.chunks_in"] == 12
    assert "asr" in view and "last_final_age_s" in view


def test_stall_watchdog_marks_and_recovers(db):
    s = live.LiveSession("停滞测试")
    s.running = True
    s.id = "S_STALL"
    s.total_samples = 60 * live.SAMPLE_RATE
    s.last_speech_s = 55.0          # 录内 5 秒前还有人说话
    now = 1_000_000.0
    s.last_final_at = now - live.LIVE_STALL_S - 5
    assert s.check_stall(now) is True
    assert metrics.METRICS.global_value("live.stalls") == 1
    assert metrics.METRICS.session_snapshot("S_STALL")["counters"]["live.stalls"] == 1
    # 持续停滞不重复计数
    assert s.check_stall(now + 30) is True
    assert metrics.METRICS.global_value("live.stalls") == 1
    # 新定稿回来 -> 恢复
    s.last_final_at = now + 40
    assert s.check_stall(now + 41) is False
    assert metrics.METRICS.global_value("live.stalls_recovered") == 1


def test_stall_ignores_silence_and_pause():
    s = live.LiveSession("静默测试")
    s.running = True
    s.total_samples = 60 * live.SAMPLE_RATE
    s.last_speech_s = 0.0           # 录内 60 秒没人说话
    s.last_final_at = 0.0
    assert s.check_stall(1_000_000.0) is False
    s.last_speech_s = 55.0
    s.paused = True
    assert s.check_stall(1_000_000.0) is False
    assert metrics.METRICS.global_value("live.stalls") == 0
