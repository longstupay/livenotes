# -*- coding: utf-8 -*-
"""P1/P2 接口回归：防重复采集、批量回收、体检候选、批量导出。"""
from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
import zipfile

import pytest
from helpers import add_rows, add_session, utt_row

from app import live, server


@pytest.fixture(autouse=True)
def _isolate_rooms(tmp_path, monkeypatch):
    # /api/cleanup 会读 rooms.json 判断哪条重复该保留；测试不能碰仓库里的 data/。
    monkeypatch.setattr(server.rooms, "PATH", tmp_path / "rooms.json")


@pytest.fixture
def http_base(db):
    srv = server.make_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        srv.shutdown()
        srv.server_close()
        live.stop_watchdog()


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _post_raw(base: str, path: str, body: dict):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, dict(r.headers), r.read()


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def test_live_start_reuses_active_room_session(http_base, monkeypatch):
    monkeypatch.setattr(server.rooms, "get",
                        lambda rid: {"id": rid, "session_id": "S_ALIVE"})
    monkeypatch.setattr(server.live, "status",
                        lambda sid: {"running": True, "elapsed": 1.5})
    monkeypatch.setattr(server.live, "start", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("已有活跃会话时不应再建一个")))
    code, body = _post(http_base, "/api/live/start",
                       {"room_id": "rm1", "title": "直播"})
    assert code == 200
    assert body["session_id"] == "S_ALIVE"
    assert body["reused"] is True
    assert body["room_id"] == "rm1"


def test_live_start_binds_new_session_to_room(http_base, monkeypatch):
    monkeypatch.setattr(server.rooms, "get",
                        lambda rid: {"id": rid, "session_id": ""})
    monkeypatch.setattr(server.live, "status",
                        lambda sid: (_ for _ in ()).throw(live.LiveError("gone")))
    calls = []

    def fake_start(title="", source_mode="tab"):
        calls.append((title, source_mode))
        return {"session_id": "S_NEW", "title": title, "source_mode": source_mode}

    monkeypatch.setattr(server.live, "start", fake_start)
    updates = []
    monkeypatch.setattr(server.rooms, "update",
                        lambda rid, **kw: updates.append((rid, kw)) or {"id": rid, **kw})
    code, body = _post(http_base, "/api/live/start",
                       {"room_id": "rm2", "title": "直播", "source_mode": "direct"})
    assert code == 201
    assert body["session_id"] == "S_NEW"
    assert body["reused"] is False
    assert calls == [("直播", "direct")]
    assert updates == [("rm2", {"session_id": "S_NEW"})]


def test_cleanup_lists_candidates_and_batch_trash_restore(http_base, db):
    add_session(db, "S_EMPTY", title="空采集", duration_s=0,
                started_at="2026-11-11T10:00:00")
    add_session(db, "S_OK", title="正常场", duration_s=300,
                started_at="2026-11-11T12:00:00")
    add_rows(db, "S_OK", [utt_row(0, "正常内容")])

    cleanup = _get(http_base, "/api/cleanup")
    ids = {r["id"] for r in cleanup["sessions"]}
    assert "S_EMPTY" in ids
    assert "S_OK" not in ids
    assert cleanup["counts"]["total"] >= 1

    code, out = _post(http_base, "/api/library/batch",
                      {"action": "trash", "session_ids": ["S_EMPTY", "S_OK"]})
    assert code == 200
    assert set(out["done"]) == {"S_EMPTY", "S_OK"}
    trash = _get(http_base, "/api/library/trash")
    assert {r["id"] for r in trash["sessions"]} == {"S_EMPTY", "S_OK"}

    code, out = _post(http_base, "/api/library/batch",
                      {"action": "restore", "session_ids": ["S_EMPTY"]})
    assert code == 200 and out["done"] == ["S_EMPTY"]
    live_rows = _get(http_base, "/api/library")
    assert "S_EMPTY" in {r["id"] for r in live_rows["sessions"]}


def test_batch_export_returns_zip(http_base, db):
    add_session(db, "S_EXP", title="导出场", duration_s=60)
    add_rows(db, "S_EXP", [utt_row(0, "要导出的内容")])
    status, headers, data = _post_raw(
        http_base, "/api/export/batch", {"session_ids": ["S_EXP"], "format": "md"})
    assert status == 200
    assert headers.get("Content-Type") == "application/zip"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        assert len(names) == 1
        assert names[0].endswith(".md")
        assert "要导出的内容" in z.read(names[0]).decode("utf-8")
