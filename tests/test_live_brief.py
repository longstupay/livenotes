# -*- coding: utf-8 -*-
"""简报：证据闸门、缓存、单飞超时、预算降级、长直播抽样。"""
from __future__ import annotations

import json
import threading
import time

import pytest

from app import livebrief, scheduler
from tests.helpers import add_rows, add_session, utt_row


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    livebrief.reset()
    monkeypatch.setattr(livebrief, "BRIEF_LLM", True)
    monkeypatch.setattr(livebrief, "BRIEF_DAILY_CAP", 100)
    monkeypatch.setattr(livebrief, "BRIEF_SESSION_TOKEN_CAP", 0)
    monkeypatch.setattr(livebrief, "BRIEF_TOPICS_TTL_S", 90.0)
    monkeypatch.setattr(livebrief, "BRIEF_SUMMARY_TTL_S", 300.0)
    monkeypatch.setattr(livebrief, "BRIEF_SUMMARY_MAX_ROWS", 350)
    monkeypatch.setattr(livebrief.settings, "resolve",
                        lambda *a, **k: {"configured": True, "model": "test-model"})
    yield
    livebrief.reset()


def _seed(db, n=20, sid="S1"):
    add_session(db, sid, started_at="2026-11-09T18:00:00", duration_s=3600)
    topics = ["账号风控", "设备预算", "投放复盘"]
    rows = [
        utt_row(i, "第%d句：这是一段有信息量的讨论，涉及账号风控和设备预算。" % i,
                t=i * 20, topic=topics[i % 3], importance=0.4 + (i % 5) / 10)
        for i in range(n)
    ]
    return add_rows(db, sid, rows)


def _fake_chat(kind: str, valid: bool = True, calls: list | None = None):
    def fake(messages, **kw):
        user = json.loads(messages[1]["content"])
        ids = [int(u[0]) for u in user["u"]]
        if calls is not None:
            calls.append((kind, len(ids), ids))
        use = ids[:2] if valid else [999999, 999998]
        if kind == "topics":
            return json.dumps({"topics": [
                {"title": "账号风控", "gist": "平台近期查得严。", "evidence_ids": use},
                {"title": "设备预算", "gist": "先算清设备成本。", "evidence_ids": use},
            ]}, ensure_ascii=False)
        return json.dumps({
            "overview": "这段主要在讨论账号风控与设备预算。",
            "points": [
                {"title": "账号风控", "gist": "平台近期查得严。", "evidence_ids": use},
                {"title": "设备预算", "gist": "先算清设备成本。", "evidence_ids": use},
            ],
        }, ensure_ascii=False)
    return fake


def test_topics_llm_valid_and_cache(db, monkeypatch):
    _seed(db)
    calls: list = []
    monkeypatch.setattr(livebrief, "_chat", _fake_chat("topics", True, calls))
    first = livebrief.topics(db, "S1", 10, t_to=400)
    assert first["ok"] and first["engine"] == "llm"
    assert len(first["topics"]) == 2
    assert first["topics"][0]["evidence"][0]["reader_url"].startswith("http")
    assert first["cached"] is False
    assert calls and calls[0][1] == 20

    second = livebrief.topics(db, "S1", 10, t_to=400)
    assert second["cached"] is True
    assert len(calls) == 1


def test_topics_invalid_evidence_falls_back(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(livebrief, "_chat", _fake_chat("topics", False))
    # active=True：这个用例只关心证据闸门，别让「断流」提示按真实时钟盖掉它。
    data = livebrief.topics(db, "S1", 10, t_to=400, active=True)
    assert data["engine"] == "extractive"
    assert data["notice"]["code"] == "llm_failed"
    assert data["topics"]
    assert all(len(t["evidence"]) >= 2 for t in data["topics"])


def test_topics_no_utterances_does_not_call_model(db, monkeypatch):
    add_session(db, "EMPTY", started_at="2026-11-09T18:00:00", duration_s=10)
    called: list = []
    monkeypatch.setattr(livebrief, "_chat", lambda *a, **k: called.append(1))
    data = livebrief.topics(db, "EMPTY", 10, t_to=10)
    assert data["notice"]["code"] == "no_utterances"
    assert data["topics"] == []
    assert not called


def test_summary_points_carry_beijing_wall_clock(db, monkeypatch):
    add_session(db, "CLK", started_at="2026-11-09T14:09:48", duration_s=3600,
                started_utc="2026-11-09T06:09:48+00:00", tz_offset_min=480)
    rows = [utt_row(i, "第%d句：讨论内容足够长，包含信息量与结论。" % i,
                    t=i * 20, topic="账号风控", importance=0.6) for i in range(8)]
    add_rows(db, "CLK", rows)
    monkeypatch.setattr(livebrief, "_chat", _fake_chat("summary", True))
    data = livebrief.summary(db, "CLK", t_to=140)
    cov = data["coverage"]
    assert cov["start_clock"] == "11-09 14:09"
    assert cov["clock_tz"] == "北京时间"
    assert cov["from_clock"] == "14:09" and cov["to_clock"] == "14:12"
    p = data["points"][0]
    # 证据取前两句（t=0 / t=20，各 2 秒），14:09:48 起算
    assert p["from_clock"] == "14:09" and p["to_clock"] == "14:10"
    # 缓存命中也要带着墙上时间
    again = livebrief.summary(db, "CLK", t_to=140)
    assert again["cached"] is True
    assert again["points"][0]["from_clock"] == "14:09"


def test_summary_budget_exceeded_falls_back(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(livebrief.scheduler, "budget_status", lambda sid="": {
        "session": {"limit": 100, "remaining": 0, "used": 100},
        "daily": {"limit": 0, "remaining": None, "used": 0},
    })
    called: list = []
    monkeypatch.setattr(livebrief, "_chat", lambda *a, **k: called.append(1))
    data = livebrief.summary(db, "S1", t_to=400)
    assert data["engine"] == "extractive"
    assert data["notice"]["code"] == "budget_exceeded"
    assert data["points"]
    assert not called


def test_summary_llm_budget_exception_falls_back(db, monkeypatch):
    _seed(db)

    def boom(*a, **k):
        raise scheduler.BudgetExceeded("session", 100, 100)

    monkeypatch.setattr(livebrief, "_chat", boom)
    data = livebrief.summary(db, "S1", t_to=400)
    assert data["engine"] == "extractive"
    assert data["notice"]["code"] == "budget_exceeded"


def test_summary_long_session_samples_latest_and_spread(db, monkeypatch):
    n = 420
    add_session(db, "LONG", started_at="2026-11-09T18:00:00", duration_s=5000)
    rows = [
        utt_row(i, "第%d句：长直播里的讨论内容，包含信息量与结论。" % i,
                t=i * 10, topic="话题%d" % (i % 5), importance=0.5)
        for i in range(n)
    ]
    ids = add_rows(db, "LONG", rows)
    monkeypatch.setattr(livebrief, "BRIEF_SUMMARY_MAX_ROWS", 60)
    calls: list = []
    monkeypatch.setattr(livebrief, "_chat", _fake_chat("summary", True, calls))
    data = livebrief.summary(db, "LONG", t_to=4200)
    assert data["engine"] == "llm"
    assert data["coverage"]["sampled_rows"] <= 60
    assert calls and calls[0][1] <= 60
    sent_ids = set(calls[0][2])
    assert ids[-1] in sent_ids          # 最新一句必须在
    assert ids[n // 2] not in sent_ids or len(sent_ids) < n  # 不是简单截前 60 条
    assert any("分层抽样" in c for c in data["caveats"])


def test_single_flight_waiter_times_out_busy():
    key = ("flight",)
    started = threading.Event()

    def slow():
        started.set()
        time.sleep(0.3)
        return {"ok": True, "v": 1}

    out: list = []
    th = threading.Thread(
        target=lambda: out.append(livebrief._single(key, "S", "topics", slow, 5)))
    th.start()
    assert started.wait(1)
    with pytest.raises(livebrief.BriefError) as ei:
        livebrief._single(key, "S", "topics", slow, 0.05)
    assert ei.value.code == "busy"
    th.join(2)
    assert out and out[0]["ok"] is True


def test_topics_cooldown_returns_busy_when_cache_expired(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(livebrief, "BRIEF_TOPICS_TTL_S", 0.0)
    monkeypatch.setattr(livebrief, "_chat", _fake_chat("topics", True))
    livebrief.topics(db, "S1", 10, t_to=400)
    with pytest.raises(livebrief.BriefError) as ei:
        livebrief.topics(db, "S1", 10, t_to=400)
    assert ei.value.code == "busy"


def test_resolve_room_and_status():
    rooms = [
        {"id": "rm1", "seq": 1, "state": "live", "name": "一号", "session_id": "S1"},
        {"id": "rm2", "seq": 2, "state": "live", "name": "二号", "session_id": "S2"},
    ]
    assert livebrief.resolve_room(rooms, [], "CH02")["id"] == "rm2"
    with pytest.raises(livebrief.BriefError) as ei:
        livebrief.resolve_room(rooms, [], None)
    assert ei.value.code == "ambiguous_room"

    sessions = [{"id": "S1", "elapsed": 100, "n_utterances": 10,
                 "last_line": "最后一句", "last_final_ago_s": 3,
                 "last_chunk_ago_s": 200, "paused": False, "running": True}]
    data = livebrief.status([rooms[0]], sessions,
                            {"rm1": {"alive": True, "age_s": 2}})
    row = data["rooms"][0]
    assert row["ch"] == "CH01"
    assert row["stalled"] is True
    assert row["capture_alive"] is True
    assert data["summary"]["stalled"] == 1


def test_api_topic_response(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(livebrief, "_chat", _fake_chat("topics", True))
    rooms = [{"id": "rm1", "seq": 1, "state": "live", "name": "一号",
              "session_id": "S1"}]
    sessions = [{"id": "S1", "elapsed": 400, "running": True}]
    code, data = livebrief.api(db, rooms, sessions, {}, {
        "kind": "topics", "room_id": "rm1", "minutes": 5, "timeout": 5})
    assert code == 200
    assert data["ok"] and data["room"]["ch"] == "CH01"
    assert data["window"]["minutes"] == 5
    assert data["window"]["t_to"] == 400


def test_api_ambiguous_room(db):
    rooms = [
        {"id": "rm1", "seq": 1, "state": "live", "name": "一号", "session_id": "S1"},
        {"id": "rm2", "seq": 2, "state": "live", "name": "二号", "session_id": "S2"},
    ]
    code, data = livebrief.api(db, rooms, [], {}, {"kind": "summary"})
    assert code == 409
    assert data["error"]["code"] == "ambiguous_room"


def test_peek_does_not_call_model(db, monkeypatch):
    _seed(db)
    called: list = []
    monkeypatch.setattr(livebrief, "_chat", lambda *a, **k: called.append(1))
    data = livebrief.peek(db, "S1", "summary", t_to=400)
    assert data["dry"] is True
    assert data["cached"] is False
    assert data["rows"] == 20
    assert data["would_use_llm"] is True
    assert not called


def test_topics_stale_window_says_no_new_content(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(livebrief, "_chat", _fake_chat("topics", True))
    data = livebrief.topics(db, "S1", 10, t_to=600, active=True)
    assert data["coverage"]["stale"] is True
    assert data["notice"]["code"] == "stale_content"
    assert "没有新内容" in data["notice"]["message"]


def test_refresh_does_not_bypass_cooldown(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(livebrief, "_chat", _fake_chat("topics", True))
    livebrief.topics(db, "S1", 10, t_to=400)
    with pytest.raises(livebrief.BriefError) as ei:
        livebrief.topics(db, "S1", 10, refresh=True, t_to=400)
    assert ei.value.code == "busy"


def test_summary_stale_cache_reports_new_content(db, monkeypatch):
    _seed(db)
    calls: list = []
    monkeypatch.setattr(livebrief, "_chat", _fake_chat("summary", True, calls))
    first = livebrief.summary(db, "S1", t_to=200)
    assert first["ok"]
    made = len(calls)

    peek = livebrief.peek(db, "S1", "summary", t_to=400)
    assert peek["dry"] is True and peek["cached"] is True
    assert peek["notice"]["code"] == "cached_new"
    assert peek["new_utterances"] > 0

    second = livebrief.summary(db, "S1", t_to=400)
    assert second["cached"] is True
    assert second["notice"]["code"] == "cached_new"
    assert second["new_utterances"] == peek["new_utterances"]
    assert len(calls) == made          # 没有为了新内容再调一次模型


def test_topics_long_window_samples_for_llm(db, monkeypatch):
    n = 420
    add_session(db, "LONG2", started_at="2026-11-09T18:00:00", duration_s=5000)
    rows = [
        utt_row(i, "第%d句：长窗口里的讨论内容，包含信息量与结论。" % i,
                t=i * 10, topic="话题%d" % (i % 5), importance=0.5)
        for i in range(n)
    ]
    add_rows(db, "LONG2", rows)
    monkeypatch.setattr(livebrief, "BRIEF_SUMMARY_MAX_ROWS", 60)
    calls: list = []
    monkeypatch.setattr(livebrief, "_chat", _fake_chat("topics", True, calls))
    data = livebrief.topics(db, "LONG2", 120, t_to=4200)
    assert data["engine"] == "llm"
    assert calls and calls[0][1] <= 60
    assert any("分层抽样" in c for c in data["caveats"])
