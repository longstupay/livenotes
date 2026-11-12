# -*- coding: utf-8 -*-
"""桌面诊断接口：壳的托盘看门狗和诊断包都吃这一份快照。

这里不依赖模型，也不启动真实采集：只验证口径（断流秒数、房间 URL 脱敏、
最近事件/日志）和 HTTP 出口稳定，保证壳在引擎崩掉之前能拿到可打包的数据。
"""
from __future__ import annotations

import json
import threading
import urllib.request

from app import diagnostics, live, rooms, server


def test_summary_shape_and_room_url_redaction(db, monkeypatch):
    monkeypatch.setattr(live, "stalled_sessions", lambda idle_s: [
        {"id": "S1", "title": "抖音直播", "idle_s": 42.5, "never": False},
    ])
    monkeypatch.setattr(live, "metrics_sessions", lambda: [
        {"id": "S1", "title": "抖音直播", "running": True, "paused": False,
         "elapsed": 120.0, "n_utterances": 8, "queued": 0, "dropped": 0,
         "error": "", "started_at": "2026-11-10T10:00:00"},
    ])
    monkeypatch.setattr(rooms, "list_rooms", lambda: [{
        "id": "rm1", "seq": 1, "name": "房间 A", "state": "live", "stage": 6,
        "session_id": "S1", "url": "https://live.douyin.com/123?token=secret&x=1",
        "error": "", "stage_note": "", "saved": False, "has_recording": False,
    }])
    out = diagnostics.summary()
    assert out["ok"] is True
    assert out["live"] == [{
        "id": "S1", "title": "抖音直播", "idle_s": 42.5, "never": False,
        "running": True, "paused": False, "elapsed": 120.0,
        "n_utterances": 8, "queued": 0, "dropped": 0, "error": "",
        "started_at": "2026-11-10T10:00:00",
    }]
    assert out["rooms"][0]["url"] == "https://live.douyin.com/123"
    assert "token" not in json.dumps(out["rooms"], ensure_ascii=False)
    assert out["config"]["stall_warn_s"] >= 0
    assert isinstance(out["events"], list)


def test_diagnostics_http_endpoint(db):
    srv = server.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/diagnostics" % port, timeout=5) as r:
            assert r.status == 200
            data = json.load(r)
        assert data["ok"] is True
        assert "engine" in data and "rooms" in data and "live" in data
        assert data["engine"]["pid"] > 0
        assert "uptime_s" in data["engine"]
    finally:
        srv.shutdown()
        srv.server_close()
