# -*- coding: utf-8 -*-
"""存储层回归：FTS/LIKE 混合检索、过滤、软删除、高光与推送台账。"""
from __future__ import annotations

import json
import sqlite3

import pytest
from helpers import add_rows, add_session, feats, utt_row

from app import store


SID = "S_STORE"


@pytest.fixture
def seeded(db):
    add_session(db, SID, title="抖音直播 11-05 20:00", duration_s=300, speakers=[
        {"speaker_id": "SPK_00", "display_name": "主播", "verified": 1},
        {"speaker_id": "SPK_01", "display_name": "嘉宾", "verified": 0},
    ])
    add_session(db, "S_OTHER", title="另一场", started_at="2026-11-04T20:00:00")
    add_rows(db, SID, [
        utt_row(0, "主播今天讲跳槽和薪资谈判", label="主播"),
        utt_row(1, "嘉宾说 OPC 政策要落地", speaker="SPK_01", label="嘉宾",
                category="question_consult"),
        utt_row(2, "好", speaker="SPK_01", label="嘉宾", category="chitchat",
                value_tag="low", t=20),
        utt_row(3, "多人重叠不可信", speaker="SPK_00", label="主播", overlap=1,
                value_tag="low", t=30, asr_conf=0.2),
        utt_row(4, "我觉得这个方案可行，关键是现金流", speaker="SPK_00",
                label="主播", t=40, importance=0.8, topic="职场"),
    ])
    add_rows(db, "S_OTHER", [utt_row(0, "另一场聊跳槽", t=0)])
    return db


def test_fts_trigram_available(db):
    # 标准 CPython 的 SQLite 自带 FTS5；没有时整个套件仍应按 LIKE 降级通过
    assert isinstance(store.FTS_TRIGRAM, bool)


def test_two_char_query_uses_like(seeded):
    """trigram 对两字词会静默返回 0 条，必须走 LIKE —— 这是踩过的坑。"""
    rows = store.query_utterances(seeded, {"session_id": SID, "q": "跳槽"})
    assert [r["text"] for r in rows] == ["主播今天讲跳槽和薪资谈判"]


def test_three_char_query_uses_fts(seeded, monkeypatch):
    # 先证明 FTS 路径返回正确结果；没有 FTS 时跳过（降级路径另有测试）
    if not store.FTS_TRIGRAM:
        pytest.skip("此 SQLite 未启用 FTS5 trigram")
    rows = store.query_utterances(seeded, {"session_id": SID, "q": "OPC"})
    assert [r["text"] for r in rows] == ["嘉宾说 OPC 政策要落地"]


def test_fts_false_falls_back_to_like(seeded, monkeypatch):
    monkeypatch.setattr(store, "FTS_TRIGRAM", False)
    rows = store.query_utterances(seeded, {"session_id": SID, "q": "OPC"})
    assert [r["text"] for r in rows] == ["嘉宾说 OPC 政策要落地"]


def test_fts_operational_error_falls_back(seeded, monkeypatch):
    """FTS 表损坏/查询语法异常时，query_utterances 必须自动重试 LIKE。"""
    if not store.FTS_TRIGRAM:
        pytest.skip("没有 FTS 可破坏")
    real_execute = sqlite3.Connection.execute

    class Broken:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if "utterances_fts" in sql and "MATCH" in sql:
                raise sqlite3.OperationalError("fts broken")
            return real_execute(self._conn, sql, params)

    rows = store.query_utterances(Broken(seeded), {"session_id": SID, "q": "OPC"})
    assert len(rows) == 1


def test_search_all_short_and_long(seeded):
    assert len(store.search_all(seeded, "跳槽")) == 2          # 两场各一条，LIKE
    assert len(store.search_all(seeded, "OPC")) == 1


def test_search_all_excludes_trash(seeded):
    store.trash_session(seeded, SID)
    assert store.search_all(seeded, "OPC") == []
    assert [s["id"] for s in store.list_sessions(seeded)] == ["S_OTHER"]
    assert [s["id"] for s in store.list_trash(seeded)] == [SID]
    store.restore_session(seeded, SID)
    assert len(store.search_all(seeded, "OPC")) == 1


def test_search_multi_dedupes(seeded):
    rows = store.search_multi(seeded, ["OPC", "OPC", "跳槽"])
    assert len(rows) == 3
    assert len({r["id"] for r in rows}) == 3


def test_query_filters(seeded):
    q = lambda **kw: store.query_utterances(seeded, {"session_id": SID, **kw})  # noqa: E731
    assert len(q()) == 5
    assert [r["speaker_id"] for r in q(speakers=["SPK_01"])] == ["SPK_01", "SPK_01"]
    assert [r["text"] for r in q(categories=["question_consult"])] == ["嘉宾说 OPC 政策要落地"]
    assert all(r["value_tag"] != "low" for r in q(exclude_low_value=True))
    assert all(r["overlap"] == 0 for r in q(exclude_overlap=True))
    assert all(r["asr_conf"] >= 0.45 for r in q(min_conf=0.45))
    assert [r["t_start"] for r in q(t_from=20, t_to=35)] == [20.0, 30.0]
    assert store.count_utterances(seeded, {"session_id": SID}) == 5


def test_update_utterance_clears_ai_fingerprint_and_marks_edited(seeded):
    uid = store.query_utterances(seeded, {"session_id": SID, "q": "OPC"})[0]["id"]
    seeded.execute("UPDATE utterances SET ai_fp='x', features_json='{}' WHERE id=?", (uid,))
    seeded.commit()
    assert store.update_utterance(seeded, SID, uid, text="嘉宾说 OPC 政策已落地") == 1
    row = dict(seeded.execute("SELECT * FROM utterances WHERE id=?", (uid,)).fetchone())
    assert row["edited_at"] and row["ai_fp"] is None and row["features_json"] is None
    # FTS 触发器跟随更新：旧词消失、新词可搜
    assert store.query_utterances(seeded, {"session_id": SID, "q": "政策要落地"}) == []
    assert len(store.query_utterances(seeded, {"session_id": SID, "q": "政策已落地"})) == 1


def test_set_utterance_speaker_marks_edited_and_keeps_other_rows(seeded):
    uid = store.query_utterances(seeded, {"session_id": SID, "q": "跳槽"})[0]["id"]
    assert store.set_utterance_speaker(seeded, SID, uid, "SPK_09", "老王") == 1
    row = dict(seeded.execute("SELECT * FROM utterances WHERE id=?", (uid,)).fetchone())
    assert row["speaker_id"] == "SPK_09" and row["speaker_label"] == "老王"
    assert row["edited_at"]
    # 同一场的另一条不受影响
    assert any(r["speaker_id"] == "SPK_00" for r in store.session_transcript(seeded, SID))


def test_rename_speaker_marks_verified(seeded):
    store.rename_speaker(seeded, SID, "SPK_01", "小鹿")
    sp = dict(seeded.execute(
        "SELECT * FROM speakers WHERE session_id=? AND speaker_id=?",
        (SID, "SPK_01")).fetchone())
    assert sp["display_name"] == "小鹿" and sp["verified"] == 1
    assert all(r["speaker_label"] == "小鹿" for r in
               store.query_utterances(seeded, {"session_id": SID, "speakers": ["SPK_01"]}))


def test_auto_relabel_never_overwrites_verified(seeded):
    store.rename_speaker(seeded, SID, "SPK_01", "小鹿")
    assert store.relabel_speaker(seeded, SID, "SPK_01", "嘉宾1") == 0
    assert store.query_utterances(
        seeded, {"session_id": SID, "speakers": ["SPK_01"]})[0]["speaker_label"] == "小鹿"


def test_snapshot_and_restore_rule_categories(seeded):
    store.snapshot_rule_categories(seeded, SID)
    assert store.has_ai_categories(seeded, SID) is False
    rows = store.query_utterances(seeded, {"session_id": SID})
    uid = rows[0]["id"]
    store.apply_ai_categories(seeded, SID, [(uid, "ad", "low", "广告", "fp1")])
    assert store.has_ai_categories(seeded, SID) is True
    store.restore_rule_categories(seeded, SID)
    restored = dict(seeded.execute("SELECT * FROM utterances WHERE id=?", (uid,)).fetchone())
    assert restored["category"] == "substantive"
    assert restored["category_rule"] == "substantive"
    assert restored["ai_fp"] is None


def test_speaker_display_rules():
    assert store.speaker_display("主播") == "主播"
    assert store.speaker_display("主播", "老王") == "主播 · 老王"
    assert store.speaker_display("主播", "老王", "cand") == "主播 · 老王（疑似）"
    assert store.speaker_display("老王", "老王") == "老王"
    assert store.speaker_display("", "") == "说话人"


def test_session_transcript_joins_person(seeded):
    store.upsert_person(seeded, "per_x", "老王")
    store.set_speaker_person(seeded, SID, "SPK_00", "per_x", 0.5, 0.1, "auto")
    row = store.session_transcript(seeded, SID)[0]
    assert row["person_name"] == "老王" and row["person_state"] == "auto"
    assert store.speaker_display(row["speaker_label"], row["person_name"],
                                 row["person_state"]).endswith("· 老王")


def test_timeline_buckets(seeded):
    bars = store.timeline(seeded, SID, bucket=20)
    assert [b["bucket"] for b in bars] == [0, 1, 2]
    assert sum(b["total"] for b in bars) == 5


def test_replace_highlights_preserves_feedback_by_peak(seeded):
    events = [feats(10, score=2.6, session_id=SID), feats(80, score=1.9, session_id=SID)]
    assert store.replace_highlights(seeded, SID, events) == 2
    rows = store.list_highlights(seeded, SID)
    hid = next(r["id"] for r in rows if r["peak_t"] == 10)
    store.set_highlight_feedback(seeded, SID, hid, 1)
    # 重算后同一峰值的人工标注不能丢
    events2 = [feats(10, score=2.7, session_id=SID), feats(80, score=2.0, session_id=SID),
               feats(150, score=2.2, session_id=SID)]
    store.replace_highlights(seeded, SID, events2)
    rows = store.list_highlights(seeded, SID)
    by_peak = {r["peak_t"]: r for r in rows}
    assert by_peak[10]["feedback"] == 1
    assert by_peak[80]["feedback"] == 0
    assert store.highlights_meta(seeded, SID)["rated"] == 1
    assert store.highlights_meta(seeded, SID)["good"] == 1


def test_highlight_features_json_decoded(seeded):
    e = feats(10, session_id=SID)
    e["features_json"] = json.dumps({"num": 2, "audio": False})
    store.replace_highlights(seeded, SID, [e])
    row = store.list_highlights(seeded, SID)[0]
    assert row["features"] == {"num": 2, "audio": False}


def test_claim_push_is_idempotent(seeded):
    assert store.claim_push(seeded, SID, "hl:10.00") is True
    assert store.claim_push(seeded, SID, "hl:10.00") is False       # 发送中
    store.close_push(seeded, SID, "hl:10.00", "sent")
    assert store.claim_push(seeded, SID, "hl:10.00") is False       # 已发出


def test_claim_push_retries_failed_and_skipped(seeded):
    store.claim_push(seeded, SID, "hl:1.00")
    store.close_push(seeded, SID, "hl:1.00", "failed", "send_fail")
    assert store.claim_push(seeded, SID, "hl:1.00", text="新文案") is True
    store.close_push(seeded, SID, "hl:1.00", "skipped", "daily_cap")
    assert store.claim_push(seeded, SID, "hl:1.00") is True


def test_claim_push_takes_over_stale_pending(seeded):
    store.claim_push(seeded, SID, "hl:2.00")
    seeded.execute("UPDATE push_log SET created_at='2000-01-01T00:00:00' "
                   "WHERE session_id=? AND item=?", (SID, "hl:2.00"))
    seeded.commit()
    assert store.claim_push(seeded, SID, "hl:2.00") is True


def test_pushes_today_counts_only_sent_highlights(seeded):
    store.claim_push(seeded, SID, "hl:1.00")
    store.close_push(seeded, SID, "hl:1.00", "sent")
    store.claim_push(seeded, SID, "hl:2.00")
    store.close_push(seeded, SID, "hl:2.00", "failed", "send_fail")
    store.claim_push(seeded, SID, "digest")
    store.close_push(seeded, SID, "digest", "sent")
    assert store.pushes_today(seeded) == 1
    assert store.pushed_peaks(seeded, SID) == {1.0}
    ov = store.push_overview(seeded, SID)
    assert ov["counts"]["sent"] == 2 and ov["counts"]["failed"] == 1


def test_log_push_skip_creates_ledger_row(seeded):
    store.log_push_skip(seeded, SID, "hl:3.00", "cooldown", score=2.7,
                        text="抢麦", features_json="{}")
    row = dict(seeded.execute(
        "SELECT * FROM push_log WHERE session_id=? AND item=?",
        (SID, "hl:3.00")).fetchone())
    assert row["status"] == "skipped" and row["reason"] == "cooldown"


def test_set_push_feedback_and_highlight_at_same_peak(seeded):
    store.claim_push(seeded, SID, "hl:5.00")
    store.close_push(seeded, SID, "hl:5.00", "sent")
    assert store.set_push_feedback(seeded, SID, "hl:5.00", -1) == 1
    store.replace_highlights(seeded, SID, [feats(5.0, session_id=SID)])
    assert store.set_highlight_feedback_at(seeded, SID, 5.0, -1) == 1
    row = dict(seeded.execute(
        "SELECT feedback FROM push_log WHERE session_id=? AND item=?",
        (SID, "hl:5.00")).fetchone())
    assert row["feedback"] == -1


def test_person_proto_trim_keeps_high_quality(db):
    store.upsert_person(db, "per_a", "甲")
    items = [("per_a", "S", "SPK_00", 2, b"\x00\x00" * 2, q)
             for q in range(10)]
    store.add_person_emb(db, items)
    assert store.count_person_emb(db, "per_a") == 10
    assert store.trim_person_emb(db, "per_a", 3) == 7
    qs = sorted(float(p[3]) for p in store.person_protos(db, "per_a"))
    assert qs == [7.0, 8.0, 9.0]


def test_merge_and_delete_person_moves_ownership(seeded):
    store.upsert_person(seeded, "per_a", "甲")
    store.upsert_person(seeded, "per_b", "乙")
    store.add_person_emb(seeded, [("per_a", SID, "SPK_00", 2, b"\x01\x00", 1.0)])
    store.set_speaker_person(seeded, SID, "SPK_00", "per_a", 0.4, 0.1, "auto")
    assert store.merge_persons(seeded, "per_a", "per_b") >= 1
    assert store.get_person(seeded, "per_a") is None
    assert store.count_person_emb(seeded, "per_b") == 1
    row = dict(seeded.execute(
        "SELECT person_id FROM speakers WHERE session_id=? AND speaker_id=?",
        (SID, "SPK_00")).fetchone())
    assert row["person_id"] == "per_b"
    store.delete_person(seeded, "per_b")
    row = dict(seeded.execute(
        "SELECT person_id, person_state FROM speakers WHERE session_id=? AND speaker_id=?",
        (SID, "SPK_00")).fetchone())
    assert row["person_id"] is None and row["person_state"] == ""


def test_clear_session_persons_keeps_manual_verdicts(seeded):
    add_rows(seeded, SID, [utt_row(9, "第三人", speaker="SPK_02", label="嘉宾2", t=90)])
    store.upsert_speaker(seeded, SID, "SPK_02", "嘉宾2")
    store.upsert_person(seeded, "per_a", "甲")
    store.set_speaker_person(seeded, SID, "SPK_00", "per_a", 0.5, 0.1, "auto")
    store.set_speaker_person(seeded, SID, "SPK_01", "per_a", 0.5, 0.1, "rejected")
    store.set_speaker_person(seeded, SID, "SPK_02", "per_a", 0.5, 0.1, "confirmed")
    store.clear_session_persons(seeded, SID)
    states = {r["speaker_id"]: r["person_state"] for r in
              seeded.execute("SELECT speaker_id, person_state FROM speakers "
                             "WHERE session_id=?", (SID,))}
    assert states.get("SPK_00", "") == ""
    assert states["SPK_01"] == "rejected"
    assert states["SPK_02"] == "confirmed"


def test_delete_session_removes_children(seeded):
    store.insert_mark(seeded, SID, 12.5, "重点")
    store.save_summary(seeded, SID, "content", {}, "extractive", {"x": 1})
    store.delete_session(seeded, SID)
    assert store.get_session(seeded, SID) is None
    for table in ("utterances", "speakers", "summaries", "marks", "utterance_emb"):
        n = seeded.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?",
                           (SID,)).fetchone()[0]
        assert n == 0, table
    # 跨场次搜索不会残留已删除场次
    assert store.search_all(seeded, "OPC") == []
