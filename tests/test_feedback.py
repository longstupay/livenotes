# -*- coding: utf-8 -*-
"""偏好回流：标签归因、样本门槛、偏置上限与"卡片优先于网页"的去重。"""
from __future__ import annotations

import json

import pytest
from helpers import add_session, feats

from app import feedback, store


SID = "S_FB"


def _seed_highlights(db, rows, sid=SID):
    add_session(db, sid)
    out = []
    for peak, feats_, why, verdict in rows:
        row = feats(peak, session_id=sid, why=why)
        row["features_json"] = json.dumps(feats_, ensure_ascii=False)
        out.append(row)
    store.replace_highlights(db, sid, out)
    # replace_highlights 只从库里带旧标注过来，新建行要再打一次标。
    for row, (_, _, _, verdict) in zip(out, rows):
        if verdict:
            store.set_highlight_feedback(db, sid, row["id"], verdict)
    return [r["id"] for r in out]


def _push(db, peak, feats_, verdict, sid=SID):
    item = "hl:%.2f" % peak
    assert store.claim_push(db, sid, item,
                            features_json=json.dumps(feats_, ensure_ascii=False))
    store.close_push(db, sid, item, "sent")
    store.set_push_feedback(db, sid, item, verdict)


# ---------------------------------------------------------------- 标签归因

def test_tags_from_structured_features():
    tags = feedback.tags_of({"num": 2, "concl": 1, "qa": 1, "interrupt": 1,
                             "burst": 3, "chars": 60, "rate": 6.0, "med_rate": 4.0})
    assert tags == {"num", "concl", "qa", "interrupt", "burst", "chars", "rate"}


def test_tags_thresholds_are_inclusive_and_guarded():
    assert feedback.tags_of({"num": 0.9, "burst": 2, "chars": 59,
                             "rate": 5.9, "med_rate": 4.0}) == set()
    assert feedback.tags_of({"num": "3"}) == {"num"}         # 字符串数字也认
    assert feedback.tags_of({"num": "很多", "rate": "快"}) == set()   # 坏值不抛
    assert feedback.tags_of(None) == set()


def test_tags_fall_back_to_why_for_old_rows():
    why = "提到 2 处数字/金额；结论句；接住提问；120 字长段；语速 2.0× 基线；抢麦；连着 4 句短句"
    assert feedback.tags_of({}, why) == {"num", "concl", "qa", "chars",
                                         "rate", "interrupt", "burst"}


def test_tags_union_of_features_and_why():
    assert feedback.tags_of({"concl": 1}, "提到 1 处数字/金额") == {"num", "concl"}


# ---------------------------------------------------------------- 偏置计算

def test_delta_requires_min_samples():
    assert feedback._delta(0, 2) == 0.0
    assert feedback._delta(1, 1) == 0.0


def test_delta_scales_with_sample_count_and_caps():
    assert feedback._delta(0, 3) == -0.2          # 3 条只给一半力度
    assert feedback._delta(0, 6) == -0.4
    assert feedback._delta(0, 60) == -0.4         # 不会越调越狠
    assert feedback._delta(3, 0) == 0.1
    assert feedback._delta(6, 0) == 0.2
    assert feedback._delta(2, 3) == 0.0           # 40% 好评：说不清，不动


def test_bias_sums_tags_and_caps():
    table = {"num": -0.4, "concl": -0.4, "qa": 0.2}
    assert feedback.bias({"num": 1, "concl": 1}, "", table) == -0.6   # 封顶
    assert feedback.bias({"num": 1, "qa": 1}, "", table) == -0.2
    assert feedback.bias({"num": 1}, "", None) == 0.0
    assert feedback.bias({}, "抢麦", {"interrupt": -0.2}) == -0.2


# ---------------------------------------------------------------- 聚合与去重

def test_verdicts_card_feedback_beats_web_highlight(db):
    """同一段两边都标过：卡片是推送当下的判断，以它为准。"""
    add_session(db, SID)
    _seed_highlights(db, [(10.0, {"num": 1}, "", -1)])
    _push(db, 10.0, {"concl": 1}, +1)
    assert feedback._verdicts(db) == [({"concl"}, 1)]


def test_verdicts_include_web_only_rows_using_why(db):
    _seed_highlights(db, [(50.0, {}, "提到 2 处数字/金额", -1),
                          (80.0, {"qa": 1}, "", +1)])
    got = feedback._verdicts(db)
    assert ({"num"}, -1) in got
    assert ({"qa"}, 1) in got


def test_verdicts_skip_unparseable_push_items_and_feedback_zero(db):
    add_session(db, SID)
    store.claim_push(db, SID, "hl:oops", features_json='{"num":1}')
    store.close_push(db, SID, "hl:oops", "sent")
    store.set_push_feedback(db, SID, "hl:oops", +1)
    store.claim_push(db, SID, "hl:9.00", features_json='{"num":1}')
    store.close_push(db, SID, "hl:9.00", "sent")
    store.set_push_feedback(db, SID, "hl:9.00", 0)
    assert feedback._verdicts(db) == []


def test_details_aggregates_and_sorts_by_most_suppressed(db):
    _seed_highlights(db, [
        (10.0, {"num": 1}, "", -1),
        (20.0, {"num": 1}, "", -1),
        (30.0, {"num": 1}, "", -1),
        (40.0, {"qa": 1}, "", +1),
        (50.0, {"qa": 1}, "", +1),
        (60.0, {"qa": 1}, "", +1),
    ])
    out = feedback.details(db)
    assert [x["tag"] for x in out][0] == "num"
    num = next(x for x in out if x["tag"] == "num")
    qa = next(x for x in out if x["tag"] == "qa")
    assert (num["good"], num["bad"], num["delta"]) == (0, 3, -0.2)
    assert (qa["good"], qa["bad"], qa["delta"]) == (3, 0, 0.1)
    assert num["label"] == feedback.TAGS["num"]


def test_details_omits_unrated_and_deltas_omits_zero(db):
    _seed_highlights(db, [(10.0, {"num": 1}, "", -1),
                          (20.0, {"num": 1}, "", -1),
                          (30.0, {"num": 1}, "", -1)])
    out = feedback.details(db)
    assert {x["tag"] for x in out} == {"num"}
    assert feedback.deltas(db) == {"num": -0.2}
    # 一条好评一条差评：显示但偏置为 0，不进 deltas
    _seed_highlights(db, [(40.0, {"concl": 1}, "", +1),
                          (50.0, {"concl": 1}, "", -1),
                          (60.0, {"concl": 1}, "", +1)], sid="S_FB2")
    assert "concl" in {x["tag"] for x in feedback.details(db)}
    assert "concl" not in feedback.deltas(db)


def test_feedback_can_be_revoked(db):
    ids = _seed_highlights(db, [(10.0, {"num": 1}, "", -1)])
    assert feedback.deltas(db) == {}
    store.set_highlight_feedback(db, SID, ids[0], -1)
    assert feedback.details(db)[0]["bad"] == 1
    store.set_highlight_feedback(db, SID, ids[0], 0)
    assert feedback.details(db) == []
