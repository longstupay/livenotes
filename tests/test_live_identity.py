# -*- coding: utf-8 -*-
"""实时身份：在线聚类边界、疑似认人的保守规则、推送标签与收工回顾。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app import live


YEAR = datetime.now(timezone(timedelta(hours=8))).year
E0 = np.array([1.0, 0.0, 0.0], dtype="float32")
E1 = np.array([0.0, 1.0, 0.0], dtype="float32")
E01 = np.array([0.8, 0.6, 0.0], dtype="float32")     # 与 E0 相似度 0.8


# ---------------------------------------------------------------- 在线聚类

def test_first_segment_creates_cluster():
    c = live.Clusters(np)
    assert c.assign(E0, 5.0) == "SPK_00"
    assert c.ids == ["SPK_00"]


def test_same_voice_joins_and_centroid_moves_only_for_long_segments():
    c = live.Clusters(np)
    c.assign(E0, 5.0)
    assert c.assign(E01, 5.0) == "SPK_00"
    assert not np.allclose(c.cent[0], E0)               # 长句参与质心更新
    moved = c.cent[0].copy()
    assert c.assign(E01, 0.5) == "SPK_00"
    assert np.allclose(c.cent[0], moved)                # 短句不参与，避免噪声带偏


def test_different_voice_opens_cluster_only_when_long_enough():
    c = live.Clusters(np)
    c.assign(E0, 5.0)
    assert c.assign(E1, 0.5) == "SPK_00"                # 太短：宁可并到最近的人
    assert c.assign(E1, 1.2) == "SPK_01"                # 到门槛才允许新簇
    assert c.ids == ["SPK_00", "SPK_01"]


def test_max_speakers_caps_new_clusters():
    c = live.Clusters(np, max_n=1)
    c.assign(E0, 5.0)
    assert c.assign(E1, 10.0) == "SPK_00"


def test_missing_embedding_uses_fallback_or_first_cluster():
    c = live.Clusters(np)
    assert c.assign(None, 2.0) == "SPK_00"              # 没有簇时也能起一个
    assert c.cent[0] is None
    assert c.assign(E0, 5.0) == "SPK_00"                # 第一个真嵌入填进空质心
    assert np.allclose(c.cent[0], E0)
    assert c.assign(None, 2.0) == "SPK_00"
    assert c.assign(None, 2.0, fallback="SPK_09") == "SPK_09"


def test_move_and_add_track_turns():
    c = live.Clusters(np)
    c.assign(E0, 5.0)
    c.assign(E1, 5.0)
    c.add("SPK_00", 5.0)
    c.add("SPK_00", 3.0)
    c.add("SPK_01", 20.0)
    assert c.order() == ["SPK_01", "SPK_00"]
    c.move("SPK_00", "SPK_01", 3.0)
    assert c.seconds == {"SPK_00": 5.0, "SPK_01": 23.0}
    assert c.counts == {"SPK_00": 1, "SPK_01": 2}
    c.move("SPK_00", "SPK_00", 3.0)                    # 原地不动
    assert c.seconds["SPK_00"] == 5.0
    c.move("SPK_00", "SPK_07", 1.0)                    # 挪到尚不存在的簇
    assert "SPK_07" in c.ids and c.seconds["SPK_07"] == 1.0


def test_fold_retires_source_and_never_assigns_to_it_again():
    c = live.Clusters(np)
    c.assign(E0, 5.0)
    c.assign(E1, 5.0)
    c.add("SPK_00", 5.0)
    c.add("SPK_01", 5.0)
    c.fold("SPK_01", "SPK_00")                         # 人工合并：01 -> 00
    assert "SPK_01" in c.retired
    assert c.order() == ["SPK_00"]                     # 空壳不再出现在界面
    assert c.assign(E1, 5.0) != "SPK_01"               # 后续声音不会再落回退役簇
    assert c.ids[:2] == ["SPK_00", "SPK_01"]           # 簇号保留，避免编号撞车


# ---------------------------------------------------------------- 疑似认人

def _info(sid, *, score=0.5, raw=0.9, seconds=60.0, pid="p1",
          name="阿哲", ready=True, n=3):
    return (sid, {"ready": ready, "seconds": seconds, "n": n,
                  "candidates": [{"person_id": pid, "name": name,
                                  "score": score, "raw": raw}]})


def _hints(pairs, prev=None, manual=None, **kw):
    args = dict(auto_score=0.30, auto_raw=0.55, auto_seconds=30.0,
                confirm_n=2, cand_score=0.12)
    args.update(kw)
    return live._resolve_live_hints(dict(pairs), prev or {}, manual or {}, **args)


def test_candidate_and_auto_states():
    first = _hints([_info("SPK_00")])
    assert first["SPK_00"]["state"] == "cand"
    assert first["SPK_00"]["streak"] == 1
    second = _hints([_info("SPK_00")], prev=first)
    assert second["SPK_00"]["state"] == "auto"          # 连续 2 次同一个人
    assert second["SPK_00"]["streak"] == 2


def test_different_person_resets_streak():
    prev = _hints([_info("SPK_00")])
    other = _hints([_info("SPK_00", pid="p2", name="老王")], prev=prev)
    assert other["SPK_00"]["state"] == "cand"
    assert other["SPK_00"]["streak"] == 1


def test_auto_needs_score_raw_seconds_and_streak():
    prev = _hints([_info("SPK_00")])
    assert _hints([_info("SPK_00", score=0.29)], prev=prev)["SPK_00"]["state"] == "cand"
    assert _hints([_info("SPK_00", raw=0.54)], prev=prev)["SPK_00"]["state"] == "cand"
    assert _hints([_info("SPK_00", seconds=29.9)], prev=prev)["SPK_00"]["state"] == "cand"


def test_below_candidate_threshold_is_dropped():
    assert _hints([_info("SPK_00", score=0.11)]) == {}
    assert _hints([_info("SPK_00", ready=False)]) == {}
    assert _hints([("SPK_00", {"ready": True, "candidates": []})]) == {}


def test_manual_and_rejected_clusters_never_auto_name():
    assert _hints([_info("SPK_00")], manual={"SPK_00": "我自己改的"}) == {}
    assert _hints([_info("SPK_00")], rejected={"SPK_00"}) == {}


def test_same_person_occupies_only_one_cluster():
    a = _info("SPK_00", score=0.40, pid="p1")
    b = _info("SPK_01", score=0.55, pid="p1")
    out = _hints([a, b])
    assert set(out) == {"SPK_01"}                       # 分数高的先占座
    assert out["SPK_01"]["score"] == 0.55


def test_candidate_already_manually_named_elsewhere_is_skipped():
    out = _hints([_info("SPK_00", name="阿哲")],
                 manual={"SPK_01": "阿哲"})
    assert out == {}


def test_hint_rounds_and_copies_metadata():
    out = _hints([_info("SPK_00", score=0.45678, raw=0.98765, seconds=61.234, n=7)])
    h = out["SPK_00"]
    assert h["score"] == 0.457 and h["raw"] == 0.988
    assert h["seconds"] == 61.2 and h["n"] == 7


# ---------------------------------------------------------------- 推送标签

def test_push_label_states():
    assert live._push_label({"speaker_label": "张三"}) == "张三"
    assert live._push_label({"speaker_label": "张三", "speaker_state": "cand",
                             "speaker_score": 0.681}) == "张三（疑似 0.68）"
    assert live._push_label({"speaker_label": "张三", "speaker_state": "cand"}) == "张三（疑似）"
    assert live._push_label({"speaker_label": "嘉宾1", "speaker_state": "unknown"}) == "嘉宾1（识别中）"
    assert live._push_label({}) == "未识别说话人"


def test_apply_identity_prefers_manual_then_hint_then_position():
    s = live.LiveSession("测试")
    s.pos_labels = {"SPK_00": "主播", "SPK_01": "嘉宾1"}
    s.manual = {"SPK_00": "老周"}
    s.person_hint = {"SPK_01": {"name": "阿哲", "state": "cand", "score": 0.456}}

    ev = {"speaker_id": "SPK_00", "speaker_label": "主播", "speaker_score": 0.9}
    s._apply_identity(ev)
    assert (ev["speaker_label"], ev["speaker_state"]) == ("老周", "manual")
    assert "speaker_score" not in ev

    ev = {"speaker_id": "SPK_01", "speaker_label": "嘉宾1"}
    s._apply_identity(ev)
    assert (ev["speaker_label"], ev["speaker_state"], ev["speaker_score"]) == (
        "阿哲", "cand", 0.46)

    ev = {"speaker_id": "SPK_02", "speaker_label": "SPK_02"}
    s.pos_labels["SPK_02"] = "嘉宾2"
    s._apply_identity(ev)
    assert (ev["speaker_label"], ev["speaker_state"]) == ("嘉宾2", "unknown")


def test_apply_identity_rejects_and_old_events():
    s = live.LiveSession("测试")
    s.pos_labels = {"SPK_00": "主播"}
    s.rejected = {"SPK_00"}
    ev = {"speaker_id": "SPK_00", "speaker_label": "主播", "speaker_score": 0.5}
    s._apply_identity(ev)
    assert (ev["speaker_label"], ev["speaker_state"]) == ("主播", "rejected")
    assert "speaker_score" not in ev

    # 没有 speaker_id 的老事件：非位置标签只能当成人工改过的名字
    ev = {"speaker_label": "老周"}
    s._apply_identity(ev)
    assert (ev["speaker_label"], ev["speaker_state"]) == ("老周", "manual")

    ev = {"speaker_label": "嘉宾3"}
    s._apply_identity(ev)
    assert (ev["speaker_label"], ev["speaker_state"]) == ("嘉宾3", "unknown")

    ev = {}
    s._apply_identity(ev)
    assert (ev["speaker_label"], ev["speaker_state"]) == ("未识别说话人", "unknown")


def test_identity_map_manual_wins_over_hint():
    s = live.LiveSession("测试")
    s.manual = {"SPK_00": "老周"}
    s.person_hint = {"SPK_00": {"name": "别人", "state": "auto"},
                     "SPK_01": {"name": "阿哲", "state": "cand"},
                     "SPK_02": {"name": "", "state": "cand"}}
    assert s._identity_map() == {"SPK_00": ("老周", "confirmed"),
                                 "SPK_01": ("阿哲", "cand")}


# ---------------------------------------------------------------- 标题与回顾

def test_room_title_strips_center_prefix_and_platform_suffix():
    title = "监听中心 · 伍大姐的抖音直播间 - 抖音直播"
    assert live._room_title({"title": title}) == "伍大姐的抖音直播间"
    assert live._room_title({"title": "直播监听 · 老王聊跳槽-抖音直播"}) == "老王聊跳槽"
    assert live._room_title({"title": "我的场次"}) == "我的场次"
    assert live._room_title({}) == "本场"
    assert len(live._room_title({"title": "很" * 40})) == 22


def _session(started_utc=f"{YEAR}-11-05T09:04:38Z"):
    return {"id": "S_DIGEST", "title": "监听中心 · 老周直播间 - 抖音直播",
            "started_utc": started_utc, "started_at": f"{YEAR}-11-05T17:04:38",
            "tz_offset_min": 480}


def _events():
    return [{"peak_t": 60.0, "speaker_id": "SPK_00", "speaker_label": "主播",
             "text": "第一段高光", "kind": "info", "why": "结论句"},
            {"peak_t": 120.0, "speaker_id": "SPK_01", "speaker_label": "嘉宾1",
             "text": "第二段高光", "kind": "live", "why": "语速陡增"}]


def test_digest_text_lists_unpushed_with_both_timezones():
    text = live._digest_text(_session(), _events(), n_all=5, n_pushed=2,
                             identities={"SPK_00": ("老周", "confirmed"),
                                         "SPK_01": ("阿哲", "cand")})
    assert "本场高光补推 2 段" in text
    assert "《老周直播间》" in text
    assert "共 5 条入榜，实时推过 2 条" in text
    assert "1. 11-05 17:05:38 北京 / 09:05:38 UTC · 主播 · 老周 · 录音 1:00" in text
    assert "2. 11-05 17:06:38 北京 / 09:06:38 UTC · 嘉宾1 · 阿哲（疑似） · 录音 2:00" in text
    assert "回听：%s/reader?session=S_DIGEST&t=60" % live.BASE_URL in text


def test_digest_text_keeps_utc_date_when_day_differs():
    s = _session(started_utc=f"{YEAR}-11-05T15:30:00Z")   # 北京 23:30
    text = live._digest_text(s, [dict(_events()[0], peak_t=3600.0)], n_all=1, n_pushed=0)
    assert "11-06 00:30:00 北京 / 11-05 16:30:00 UTC" in text


def test_digest_card_has_fields_and_buttons():
    card = live._digest_card(_session(), _events(), n_all=5, n_pushed=2,
                             identities={"SPK_00": ("老周", "confirmed")})
    assert card["header"]["title"]["content"] == "🎬 本场高光 ·《老周直播间》"
    assert "共 5 条入榜，实时推过 2 条" in card["elements"][0]["text"]["content"]
    assert "老周" in card["elements"][1]["text"]["content"]
    actions = card["elements"][-1]["actions"]
    assert [a["text"]["content"] for a in actions] == ["回听第 1 段", "回听第 2 段"]
    assert actions[0]["url"].endswith("/reader?session=S_DIGEST&t=60")


def test_digest_card_without_events_has_no_action_row():
    card = live._digest_card(_session(), [], n_all=0, n_pushed=0)
    assert all(e["tag"] != "action" for e in card["elements"])
