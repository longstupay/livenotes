# -*- coding: utf-8 -*-
"""场次体检：可播状态、演示标记、空采集与重复场次判定。

这些字段只做标记，前端据此决定「播放键能不能点」和「是否提示清理」；
任何自动删除都不在这里发生。
"""
from __future__ import annotations

from app import server


def _row(sid, title="场次", started="2026-11-11T10:00:00", duration=60,
         n=1, audio=None, source_mode="direct"):
    return {
        "id": sid, "title": title, "started_at": started,
        "duration_s": duration, "n_utterances": n,
        "audio_path": audio, "source_mode": source_mode,
    }


def _patch(monkeypatch, active=(), room_sids=()):
    monkeypatch.setattr(server.live, "list_active",
                        lambda: [{"id": sid} for sid in active])
    monkeypatch.setattr(server.rooms, "list_rooms",
                        lambda: [{"session_id": sid} for sid in room_sids])


def test_audio_state_is_truthful(tmp_path, monkeypatch):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"not empty")
    _patch(monkeypatch)
    out = server._mark_session_quality([
        _row("LIVE_READY", audio=str(audio)),
        _row("LIVE_MISSING", audio=str(tmp_path / "gone.m4a")),
        _row("LIVE_NONE"),
        _row("LIVE_ACTIVE"),
    ])
    got = {r["id"]: r["audio_state"] for r in out}
    assert got == {
        "LIVE_READY": "ready",
        "LIVE_MISSING": "missing",
        "LIVE_NONE": "none",
        "LIVE_ACTIVE": "none",
    }
    _patch(monkeypatch, active={"LIVE_ACTIVE"})
    out = server._mark_session_quality([_row("LIVE_ACTIVE")])
    assert out[0]["audio_state"] == "live"


def test_demo_and_empty_are_marked_not_deleted(tmp_path, monkeypatch):
    _patch(monkeypatch)
    out = server._mark_session_quality([
        # 老演示数据 source_mode 仍是 mixed，也要靠 DEMO_ 前缀认出来
        _row("DEMO_20261104_2000", duration=7200, source_mode="mixed"),
        _row("LIVE_ZERO", duration=0.0),
        _row("LIVE_EMPTY", duration=30, n=0),
        # 355 秒、0 句：可能是冷场/音乐，不自动判空
        _row("LIVE_QUIET", duration=355, n=0),
    ])
    by_id = {r["id"]: r for r in out}
    assert by_id["DEMO_20261104_2000"]["is_demo"] is True
    assert by_id["DEMO_20261104_2000"]["quality"]["empty"] is False
    assert by_id["LIVE_ZERO"]["quality"]["empty"] is True
    assert by_id["LIVE_EMPTY"]["quality"]["empty"] is True
    assert by_id["LIVE_QUIET"]["quality"]["empty"] is False
    assert len(out) == 4  # 只标记，不删行


def test_duplicate_keeps_room_linked_or_longer(tmp_path, monkeypatch):
    _patch(monkeypatch)
    rows = [
        _row("LIVE_A", title="同一路", started="2026-11-11T10:00:00", duration=100, n=5),
        _row("LIVE_B", title="同一路", started="2026-11-11T10:00:01", duration=200, n=9),
    ]
    out = {r["id"]: r for r in server._mark_session_quality(rows)}
    # 没被房间引用时，保留时长更长/句数更多的那条
    assert out["LIVE_A"]["quality"]["duplicate_of"] == "LIVE_B"
    assert out["LIVE_B"]["quality"]["duplicate_of"] == ""

    # 被房间引用的那条优先保留，即使更短
    _patch(monkeypatch, room_sids={"LIVE_A"})
    out = {r["id"]: r for r in server._mark_session_quality(rows)}
    assert out["LIVE_B"]["quality"]["duplicate_of"] == "LIVE_A"
    assert out["LIVE_A"]["quality"]["duplicate_of"] == ""


def test_same_title_far_apart_is_not_duplicate(tmp_path, monkeypatch):
    _patch(monkeypatch)
    rows = [
        _row("LIVE_A", title="同一路", started="2026-11-11T10:00:00"),
        _row("LIVE_B", title="同一路", started="2026-11-11T12:00:00"),
    ]
    out = server._mark_session_quality(rows)
    assert all(not r["quality"]["duplicate_of"] for r in out)
