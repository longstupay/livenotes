# -*- coding: utf-8 -*-
"""清理中心与人工高光的存储/判定回归。"""
from __future__ import annotations

from helpers import add_rows, add_session, utt_row

from app import server, store

SID = "S_CLEAN"


def test_cleanup_candidates_mark_only_and_skip_active():
    rows = [
        {"id": "DEMO_1", "title": "演示", "is_demo": True, "audio_state": "ready",
         "quality": {"empty": False, "duplicate_of": "", "reason": ""}},
        {"id": "S_EMPTY", "title": "空采集", "is_demo": False, "audio_state": "none",
         "quality": {"empty": True, "duplicate_of": "", "reason": "空采集"}},
        {"id": "S_DUP", "title": "重复", "is_demo": False, "audio_state": "ready",
         "quality": {"empty": False, "duplicate_of": "S_KEEP", "reason": "与 S_KEEP 重复"}},
        # 正在录制：哪怕时长再短也不能进清理候选
        {"id": "S_LIVE", "title": "录制中", "is_demo": False, "audio_state": "live",
         "quality": {"empty": True, "duplicate_of": "", "reason": "空采集"}},
        {"id": "S_OK", "title": "正常", "is_demo": False, "audio_state": "ready",
         "quality": {"empty": False, "duplicate_of": "", "reason": ""}},
    ]
    out = {r["id"]: r for r in server._cleanup_candidates(rows)}
    assert set(out) == {"DEMO_1", "S_EMPTY", "S_DUP"}
    assert out["DEMO_1"]["cleanup_kinds"] == ["demo"]
    assert out["S_EMPTY"]["cleanup_kinds"] == ["empty"]
    assert out["S_DUP"]["cleanup_kinds"] == ["dup"]
    assert "演示数据" in out["DEMO_1"]["cleanup_reasons"]


def test_manual_highlight_toggle_and_survives_rebuild(db):
    add_session(db, SID, duration_s=300)
    ids = add_rows(db, SID, [
        utt_row(0, "这句值得回听"),
        utt_row(1, "这句普通"),
    ])
    r = store.toggle_manual_highlight(db, SID, utterance_id=ids[0])
    assert r["highlighted"] is True
    rows = store.list_highlights(db, SID)
    assert len(rows) == 1
    assert rows[0]["kind"] == "manual"
    assert int(rows[0]["utterance_id"]) == ids[0]
    assert rows[0]["text"] == "这句值得回听"

    # 重新分析会清掉算法高光，但人工标记必须原样保留
    store.replace_highlights(db, SID, [{
        "session_id": SID, "t_start": 10.0, "t_end": 20.0, "peak_t": 12.0,
        "kind": "info", "level": "P1", "score": 2.0, "text": "算法挑的",
    }])
    rows = store.list_highlights(db, SID)
    manual = [x for x in rows if x["kind"] == "manual"]
    assert len(manual) == 1
    assert int(manual[0]["utterance_id"]) == ids[0]
    assert len(rows) == 2

    # 再点一次取消标记
    r = store.toggle_manual_highlight(db, SID, utterance_id=ids[0])
    assert r["highlighted"] is False
    assert all(x["kind"] != "manual" for x in store.list_highlights(db, SID))


def test_manual_highlight_falls_back_to_time(db):
    add_session(db, SID, duration_s=300)
    ids = add_rows(db, SID, [utt_row(0, "没有 id 也能标", t=42.0)])
    r = store.toggle_manual_highlight(db, SID, t_start=42.0)
    assert r["highlighted"] is True
    rows = store.list_highlights(db, SID)
    assert rows[0]["text"] == "没有 id 也能标"
    assert int(rows[0]["utterance_id"]) == ids[0]
