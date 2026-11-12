# -*- coding: utf-8 -*-
"""飞书 P1/P2：命令解析、卡片、确认码、群聊、菜单、订阅。"""
from __future__ import annotations

import json

import pytest

import tools.feishu_bot as fb
from app import feishu


class FakeBot:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict]] = []
        self.chat_texts: list[tuple[str, str]] = []
        self.chat_cards: list[tuple[str, dict]] = []
        self.text_ok = True
        self.card_ok = True

    def send_text(self, text: str, open_id: str = "") -> bool:
        self.texts.append((open_id, text))
        return self.text_ok

    def send_card(self, card: dict, open_id: str = "") -> bool:
        self.cards.append((open_id, card))
        return self.card_ok

    def send_text_to_chat(self, chat_id: str, text: str) -> bool:
        self.chat_texts.append((chat_id, text))
        return self.text_ok

    def send_card_to_chat(self, chat_id: str, card: dict) -> bool:
        self.chat_cards.append((chat_id, card))
        return self.card_ok


@pytest.fixture(autouse=True)
def clean():
    fb._BRIEF_PENDING.clear()
    fb._LAST_BRIEF.clear()
    fb._PENDING_CHOICE.clear()
    fb._LAST_CHOICE_ROOM.clear()
    fb._SUBS.clear()
    fb._HINT_DAY.clear()
    yield
    fb._BRIEF_PENDING.clear()
    fb._LAST_BRIEF.clear()
    fb._PENDING_CHOICE.clear()
    fb._LAST_CHOICE_ROOM.clear()
    fb._SUBS.clear()
    fb._HINT_DAY.clear()


@pytest.fixture
def bot(monkeypatch):
    b = FakeBot()
    monkeypatch.setattr(fb, "_BOT", b, raising=False)
    return b


def _room(rid="rm1", ch="CH01", sid="S1"):
    return {"id": rid, "seq": int(ch[-2:]), "state": "live", "name": "一号",
            "session_id": sid, "capture_engine": "direct"}


def _summary_payload(rid="rm1"):
    return {
        "ok": True, "kind": "summary", "cached": False,
        "as_of": "2026-11-09T18:10:00+08:00",
        "room": {"room_id": rid, "ch": "CH01", "name": "一号", "session_id": "S1"},
        "coverage": {"t_to": 600, "included": 42, "sampled_rows": 0,
                     "excluded_low_value_total": 3,
                     "t_from": 0, "to_clock": "18:10",
                     "start_clock": "11-09 17:10", "clock_tz": "北京时间"},
        "overview": "这段主要在讨论账号风控。",
        "points": [{
            "title": "账号风控", "gist": "平台近期查得严。",
            "t_from": 120, "t_to": 180,
            "from_clock": "18:00", "to_clock": "18:03",
            "evidence": [{"id": 1, "t_start": 130, "speaker": "主播",
                          "quote": "平台最近查得很严",
                          "reader_url": "http://x/reader?session=S1&t=130"}]}],
        "caveats": ["ASR 可能有误"],
    }


def _topics_payload(rid="rm1"):
    return {
        "ok": True, "kind": "topics", "cached": False,
        "as_of": "2026-11-09T18:10:00+08:00",
        "room": {"room_id": rid, "ch": "CH01", "name": "一号", "session_id": "S1"},
        "window": {"minutes": 10, "t_from": 0, "t_to": 600,
                   "from_clock": "18:00", "to_clock": "18:10",
                   "clock_tz": "北京时间"},
        "coverage": {"matched": 30, "included": 28, "last_utterance_ago_s": 20},
        "topics": [{
            "title": "账号风控", "gist": "平台近期查得严。",
            "t_from": 120, "t_to": 180,
            "from_clock": "18:00", "to_clock": "18:03",
            "evidence": [{"id": 1, "t_start": 130, "speaker": "主播",
                          "quote": "平台最近查得很严",
                          "reader_url": "http://x/reader?session=S1&t=130"}]}],
        "caveats": [],
    }


# ---------------------------------------------------------------- 命令解析

@pytest.mark.parametrize("rest,target,minutes", [
    ("", None, 10),
    ("2", "2", 10),
    ("CH02 30分钟", "CH02", 30),
    ("30分钟", None, 30),
    ("30", "30", 10),
    ("0分钟", None, 1),
    ("200分钟", None, 120),
    ("CH02 5m", "CH02", 5),
])
def test_parse_topics_args(rest, target, minutes):
    assert fb.parse_topics_args(rest) == (target, minutes)


def test_parse_topics_args_rejects_junk():
    with pytest.raises(ValueError):
        fb.parse_topics_args("新房间 10分钟")


def test_pick_room_choice_target_and_ambiguous(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", lambda: [
        _room("rm1", "CH01"), _room("rm2", "CH02", "S2")])
    room, cands, err = fb._pick_room_choice("CH02")
    assert err == "" and room["id"] == "rm2" and not cands
    room, cands, err = fb._pick_room_choice("")
    assert room is None and not err and [r["id"] for r in cands] == ["rm1", "rm2"]
    room, cands, err = fb._pick_room_choice("CH99")
    assert room is None and "没有找到" in err


def test_pick_room_choice_single_active_or_history(monkeypatch):
    active = _room("rm1", "CH01")
    stopped = {"id": "rm2", "seq": 2, "state": "stopped", "name": "二号",
               "url": "https://x", "session_id": "S2"}
    monkeypatch.setattr(fb, "_rooms", lambda: [stopped, active])
    room, cands, err = fb._pick_room_choice("")
    assert err == "" and room["id"] == "rm1" and not cands

    monkeypatch.setattr(fb, "_rooms", lambda: [stopped])
    room, cands, err = fb._pick_room_choice("")
    assert err == "" and room["id"] == "rm2" and not cands


def test_pick_room_choice_none(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", lambda: [])
    room, cands, err = fb._pick_room_choice("")
    assert room is None and not cands and "链接" in err


def test_pick_room_choice_active_without_session(monkeypatch):
    starting = {"id": "rm1", "seq": 1, "state": "starting", "name": "一号"}
    monkeypatch.setattr(fb, "_rooms", lambda: [starting])
    room, cands, err = fb._pick_room_choice("")
    assert room is None and not cands and "启动" in err


# ---------------------------------------------------------------- 多路选择

@pytest.mark.parametrize("raw,want", [
    ("1", "1"), ("第1路", "1"), ("第一路", "1"), (" 1 路 ", "1"),
    ("CH2", "CH02"), ("ch02", "CH02"), ("２", "2"),
])
def test_normalize_choice(raw, want):
    assert fb._normalize_choice(raw) == want


def test_ask_room_stores_pending_and_sends_picker(monkeypatch):
    rooms = [_room("rm1", "CH01"), _room("rm2", "CH02", "S2")]
    monkeypatch.setattr(fb, "_sessions_map", lambda: {})
    sent: list = []
    monkeypatch.setattr(fb, "_send_card_target",
                        lambda card, text, oid, cid="", ctype="p2p":
                        sent.append((card, text, oid, cid, ctype)) or True)
    fb._ask_room("summary", rooms, 0, "ou")
    p = fb._PENDING_CHOICE["ou"]
    assert p["tokens"]["1"] == "rm1"
    assert p["tokens"]["CH02"] == "rm2"
    assert p["tokens"]["第一路"] == "rm1"
    assert p["skip_confirm"] is True
    assert 240 < p["expires"] - p["at"] <= 300
    body = json.dumps(sent[0][0], ensure_ascii=False)
    assert "生成这路总结" in body and "CH02" in body
    assert "回复编号" in sent[0][1] and "5 分钟" in sent[0][1]


def test_ask_room_group_notes_mention(monkeypatch):
    rooms = [_room("rm1", "CH01"), _room("rm2", "CH02", "S2")]
    monkeypatch.setattr(fb, "_sessions_map", lambda: {})
    sent: list = []
    monkeypatch.setattr(fb, "_send_card_target",
                        lambda card, text, oid, cid="", ctype="p2p":
                        sent.append((card, text, oid, cid, ctype)) or True)
    fb._ask_room("summary", rooms, 0, "ou", "oc_1", "group")
    assert "要先 @我" in sent[0][1]
    assert "要先 @我" in json.dumps(sent[0][0], ensure_ascii=False)


def test_take_room_choice_summary_skips_confirm(monkeypatch):
    rooms = [_room("rm1", "CH01"), _room("rm2", "CH02", "S2")]
    monkeypatch.setattr(fb, "_rooms", lambda: rooms)
    monkeypatch.setattr(fb, "_sessions_map", lambda: {})
    monkeypatch.setattr(fb, "_send_card_target",
                        lambda card, text, oid, cid="", ctype="p2p": True)
    calls: list = []
    monkeypatch.setattr(fb, "cmd_summary",
                        lambda *a, **k: calls.append((a, k)) or "")
    fb._ask_room("summary", rooms, 0, "ou")
    assert fb._take_room_choice("CH02", "ou") == ""
    assert calls and calls[0][0][0] == "CH02"
    assert calls[0][1]["skip_confirm"] is True
    assert "ou" not in fb._PENDING_CHOICE
    assert fb._LAST_CHOICE_ROOM["ou"] == "rm2"


def test_take_room_choice_topics_keeps_minutes(monkeypatch):
    rooms = [_room("rm1", "CH01"), _room("rm2", "CH02", "S2")]
    monkeypatch.setattr(fb, "_rooms", lambda: rooms)
    monkeypatch.setattr(fb, "_sessions_map", lambda: {})
    monkeypatch.setattr(fb, "_send_card_target",
                        lambda card, text, oid, cid="", ctype="p2p": True)
    calls: list = []
    monkeypatch.setattr(fb, "cmd_topics",
                        lambda *a, **k: calls.append((a, k)) or "")
    fb._ask_room("topics", rooms, 30, "ou")
    assert fb._take_room_choice("第1路", "ou") == ""
    assert calls and calls[0][0][0] == "CH01 30分钟"


def test_take_room_choice_invalid_keeps_pending(monkeypatch):
    rooms = [_room("rm1", "CH01"), _room("rm2", "CH02", "S2")]
    monkeypatch.setattr(fb, "_rooms", lambda: rooms)
    monkeypatch.setattr(fb, "_sessions_map", lambda: {})
    monkeypatch.setattr(fb, "_send_card_target", lambda *a, **k: True)
    fb._ask_room("summary", rooms, 0, "ou")
    msg = fb._take_room_choice("9", "ou")
    assert "没有" in msg and "1~2" in msg
    assert "ou" in fb._PENDING_CHOICE
    assert fb._take_room_choice("随便说一句", "ou") is None
    assert "ou" in fb._PENDING_CHOICE


def test_take_room_choice_expired(monkeypatch):
    rooms = [_room("rm1", "CH01"), _room("rm2", "CH02", "S2")]
    monkeypatch.setattr(fb, "_sessions_map", lambda: {})
    monkeypatch.setattr(fb, "_send_card_target", lambda *a, **k: True)
    fb._ask_room("summary", rooms, 0, "ou")
    fb._PENDING_CHOICE["ou"]["expires"] = 0
    assert "过期" in fb._take_room_choice("1", "ou")
    assert "ou" not in fb._PENDING_CHOICE
    assert fb._take_room_choice("1", "ou") is None


def test_cmd_summary_multi_room_sends_picker(monkeypatch):
    rooms = [_room("rm1", "CH01"), _room("rm2", "CH02", "S2")]
    monkeypatch.setattr(fb, "_rooms", lambda: rooms)
    calls: list = []
    monkeypatch.setattr(fb, "_ask_room",
                        lambda *a, **k: calls.append((a, k)))
    assert fb.cmd_summary("", "ou", skip_confirm=True) == ""
    assert calls and calls[0][0][0] == "summary"
    assert [r["id"] for r in calls[0][0][1]] == ["rm1", "rm2"]


def test_cmd_topics_multi_room_sends_picker(monkeypatch):
    rooms = [_room("rm1", "CH01"), _room("rm2", "CH02", "S2")]
    monkeypatch.setattr(fb, "_rooms", lambda: rooms)
    calls: list = []
    monkeypatch.setattr(fb, "_ask_room",
                        lambda *a, **k: calls.append((a, k)))
    assert fb.cmd_topics("", "ou") == ""
    assert calls and calls[0][0][0] == "topics"
    assert calls[0][0][2] == 10


# ---------------------------------------------------------------- 格式与卡片

def test_topics_text_and_card():
    data = _topics_payload()
    text = fb._topics_text(data)
    assert "最近 10 分钟" in text
    assert "18:00–18:10 北京时间" in text
    assert "账号风控" in text and "平台最近查得很严" in text
    assert "18:00–18:03，录音 2:00–3:00" in text
    assert "reader?session=S1" in text
    card = fb._topics_card(data)
    body = json.dumps(card, ensure_ascii=False)
    assert "刷新" in body and "整场总结" in body
    assert "时间为北京时间" in body
    assert "/近聊 CH01" in body          # 卡片按钮失效时的文字兜底
    assert any(e.get("tag") == "action" for e in card["elements"])


def test_summary_text_and_card():
    data = _summary_payload()
    text = fb._summary_text(data)
    assert "18:10:00" in text and "概述" in text
    assert "开播 11-09 17:10（北京时间）· 已录 10:00" in text
    assert "18:00–18:03，录音 2:00–3:00" in text
    assert "剔除 3 句低价值" in text
    assert "ASR 可能有误" in text
    card = fb._summary_card(data)
    body = json.dumps(card, ensure_ascii=False)
    assert "/刷新" in body and "阶段性总结" in body
    assert "时间为北京时间" in body


def test_brief_points_without_clocks_keep_offsets_only():
    """老简报/老场次没有北京时间：只显示录音偏移，不编一个假时间。"""
    p = {"t_from": 120, "t_to": 180}
    assert fb._span(p) == "2:00–3:00"
    text = fb._summary_text({
        "kind": "summary", "as_of": "2026-11-09T18:30:00+08:00",
        "room": {"ch": "CH01"},
        "coverage": {"t_to": 600, "included": 10},
        "points": [dict(p, title="账号风控", gist="……", evidence=[])],
    })
    assert "开播 10:00" in text and "2:00–3:00" in text
    assert "北京时间" not in text
    note = fb._note_with_tz({"coverage": {}}, "按钮没反应就发 /刷新")
    assert note == "按钮没反应就发 /刷新"


def test_confirm_card_has_text_fallback():
    room = _room()
    card = fb._brief_confirm_card(room, "ABC123", 42)
    body = json.dumps(card, ensure_ascii=False)
    assert "42" in body and "/确认简报 ABC123" in body
    assert "5 分钟" in body


def test_brief_error_accepts_plain_string():
    # 旧服务/500 兜底返回 {"error": "unknown endpoint ..."}，不能在这里二次崩溃。
    msg = fb._brief_error({"error": "unknown endpoint /api/live/brief"})
    assert "unknown endpoint" in msg


def test_brief_error_room_errors_include_room_list(monkeypatch):
    monkeypatch.setattr(fb, "cmd_rooms", lambda: "CH01 一号")
    msg = fb._brief_error({"error": {"code": "ambiguous_room", "message": "多路"}})
    assert "多路" in msg and "CH01" in msg
    msg = fb._brief_error({"error": {"code": "busy", "message": "忙",
                                     "retry_after_s": 3}})
    assert "3 秒" in msg


def test_console_card_has_actions_and_reader(monkeypatch, bot):
    monkeypatch.setattr(fb, "_brief_api", lambda *a, **k: {
        "ok": True, "kind": "status", "rooms": [{
            "room_id": "rm1", "ch": "CH01", "name": "一号", "room_state": "live",
            "session_id": "S1", "elapsed": 100, "n_utterances": 10,
            "last_line": "最后一句", "stalled": False}]})
    assert fb.cmd_console("ou") == ""
    assert bot.cards
    body = json.dumps(bot.cards[0][1], ensure_ascii=False)
    assert "状态" in body and "近10分钟" in body and "总结" in body
    assert "reader?session=S1" in body


def test_console_empty_returns_text(monkeypatch, bot):
    monkeypatch.setattr(fb, "_brief_api", lambda *a, **k: {"ok": True, "rooms": []})
    assert "没在听" in fb.cmd_console("ou")
    assert not bot.cards


# ---------------------------------------------------------------- 生成与确认

def test_summary_first_asks_for_confirmation(monkeypatch, bot):
    room = _room()
    calls: list = []

    def api(kind, rid="", **params):
        calls.append(params)
        assert params.get("dry") == 1
        return {"ok": True, "cached": False, "would_use_llm": True, "rows": 42,
                "room": {"room_id": rid, "ch": "CH01"}}

    monkeypatch.setattr(fb, "_brief_api", api)
    sent: list = []
    monkeypatch.setattr(fb, "_send_card_target",
                        lambda card, text, oid, cid="", ctype="p2p":
                        sent.append((card, text)) or True)
    fb._summary_worker(room, "ou")
    assert calls and sent
    assert "确认简报" in sent[0][1]
    assert len(fb._BRIEF_PENDING) == 1


def test_summary_cached_dry_sends_without_confirm(monkeypatch, bot):
    room = _room()
    payload = _summary_payload()
    payload["cached"] = True
    monkeypatch.setattr(fb, "_brief_api", lambda *a, **k: payload)
    sent: list = []
    monkeypatch.setattr(fb, "_send_card_target",
                        lambda card, text, oid, cid="", ctype="p2p":
                        sent.append(text) or True)
    fb._summary_worker(room, "ou")
    assert sent and "账号风控" in sent[0]
    assert not fb._BRIEF_PENDING
    assert fb._LAST_BRIEF["ou"]["kind"] == "summary"


def test_summary_without_llm_generates_extractive(monkeypatch, bot):
    room = _room()
    calls: list = []

    def api(kind, rid="", **params):
        calls.append(dict(params))
        if params.get("dry"):
            return {"ok": True, "cached": False, "would_use_llm": False, "rows": 12}
        return _summary_payload()

    monkeypatch.setattr(fb, "_brief_api", api)
    sent: list = []
    monkeypatch.setattr(fb, "_send_card_target",
                        lambda card, text, oid, cid="", ctype="p2p":
                        sent.append(text) or True)
    fb._summary_worker(room, "ou")
    assert len(calls) == 2
    assert calls[1].get("refresh") == 0
    assert sent and not fb._BRIEF_PENDING


def test_summary_skip_confirm_generates_without_card(monkeypatch, bot):
    room = _room()
    calls: list = []

    def api(kind, rid="", **params):
        calls.append(dict(params))
        if params.get("dry"):
            return {"ok": True, "cached": False, "would_use_llm": True,
                    "rows": 42}
        return _summary_payload()

    monkeypatch.setattr(fb, "_brief_api", api)
    sent: list = []
    monkeypatch.setattr(fb, "_send_card_target",
                        lambda card, text, oid, cid="", ctype="p2p":
                        sent.append(text) or True)
    fb._summary_worker(room, "ou", skip_confirm=True)
    assert len(calls) == 2 and calls[0].get("dry") == 1
    assert not fb._BRIEF_PENDING
    assert all("确认简报" not in x for x in sent)
    assert sent and "账号风控" in sent[-1]


def test_summary_without_skip_confirm_still_asks(monkeypatch, bot):
    room = _room()
    monkeypatch.setattr(fb, "_brief_api", lambda *a, **k: {
        "ok": True, "cached": False, "would_use_llm": True, "rows": 42})
    sent: list = []
    monkeypatch.setattr(fb, "_send_card_target",
                        lambda card, text, oid, cid="", ctype="p2p":
                        sent.append(text) or True)
    fb._summary_worker(room, "ou")
    assert sent and "确认简报" in sent[0]
    assert len(fb._BRIEF_PENDING) == 1


def test_nonce_one_time_and_expiry(monkeypatch):
    n = fb._new_nonce("rm1", "ou")
    assert fb._take_nonce(n, "other") is None       # 别人的确认码不能用
    assert fb._take_nonce(n, "ou") is not None
    assert fb._take_nonce(n, "ou") is None          # 一次性

    n2 = fb._new_nonce("rm1", "ou")
    fb._BRIEF_PENDING[n2]["at"] = 0
    assert fb._take_nonce(n2, "ou") is None         # 过期


def test_confirm_action_runs_summary_with_refresh(monkeypatch):
    room = _room()
    monkeypatch.setattr(fb, "_room_by_id", lambda rid: room)
    monkeypatch.setattr(fb, "_brief_api", lambda *a, **k: {
        "ok": True, "cached": False, "would_use_llm": True, "rows": 10})
    calls: list = []
    monkeypatch.setattr(fb, "_summary_worker", lambda *a, **k: calls.append((a, k)))
    n = fb._new_nonce("rm1", "ou")
    fb._brief_action("brief_confirm", {"nonce": n, "room": "rm1"}, "ou")
    assert calls and calls[0][1].get("refresh") is True
    assert calls[0][1].get("confirmed") is True


def test_topics_worker_sends_card_and_remembers(monkeypatch, bot):
    room = _room()
    monkeypatch.setattr(fb, "_brief_api", lambda *a, **k: _topics_payload())
    sent: list = []
    monkeypatch.setattr(fb, "_send_card_target",
                        lambda card, text, oid, cid="", ctype="p2p":
                        sent.append((card, text, oid, cid, ctype)) or True)
    fb._topics_worker(room, 10, "ou")
    assert sent and "账号风控" in sent[0][1]
    assert fb._LAST_BRIEF["ou"]["kind"] == "topics"
    assert fb._LAST_BRIEF["ou"]["minutes"] == 10


def test_refresh_status_does_not_touch_rooms(monkeypatch):
    monkeypatch.setattr(fb, "cmd_status", lambda target="": "现场 %s" % target)
    fb._LAST_BRIEF["ou"] = {"kind": "status", "target": "CH02",
                            "room_id": "", "minutes": 0, "at": 1e12}
    assert fb.cmd_refresh("ou") == "现场 CH02"


# ---------------------------------------------------------------- 命令路由

def test_route_resumes_pending_room_choice(monkeypatch):
    rooms = [_room("rm1", "CH01"), _room("rm2", "CH02", "S2")]
    monkeypatch.setattr(fb, "_rooms", lambda: rooms)
    monkeypatch.setattr(fb, "_sessions_map", lambda: {})
    monkeypatch.setattr(fb, "_send_card_target", lambda *a, **k: True)
    calls: list = []
    monkeypatch.setattr(fb, "cmd_topics",
                        lambda *a, **k: calls.append((a, k)) or "")
    fb._ask_room("topics", rooms, 10, "ou")
    assert fb._route("2", "2", "ou") == ""
    assert calls and calls[0][0][0] == "CH02 10分钟"


def test_brief_action_summary_confirm_is_one_tap(monkeypatch):
    room = _room()
    monkeypatch.setattr(fb, "_room_by_id", lambda rid: room)
    calls: list = []
    monkeypatch.setattr(fb, "_summary_worker",
                        lambda *a, **k: calls.append((a, k)))
    fb._brief_action("brief_summary", {"room": "rm1", "confirm": 1}, "ou")
    assert calls and calls[0][1]["skip_confirm"] is True
    assert fb._LAST_CHOICE_ROOM["ou"] == "rm1"

    fb._brief_action("brief_summary", {"room": "rm1"}, "ou")
    assert calls[-1][1]["skip_confirm"] is False


def test_menu_summary_is_one_tap(monkeypatch, bot):
    calls: list = []
    monkeypatch.setattr(fb, "cmd_summary",
                        lambda *a, **k: calls.append((a, k)) or "")
    fb.handle_menu("summary", "ou")
    assert calls and calls[0][1]["skip_confirm"] is True


def test_route_brief_commands(monkeypatch):
    calls: list = []
    monkeypatch.setattr(fb, "cmd_topics",
                        lambda rest, oid="", cid="", ctype="p2p":
                        calls.append(("topics", rest, oid, cid, ctype)) or "T")
    monkeypatch.setattr(fb, "cmd_summary",
                        lambda rest, oid="", cid="", ctype="p2p":
                        calls.append(("summary", rest, oid, cid, ctype)) or "S")
    monkeypatch.setattr(fb, "cmd_refresh",
                        lambda oid="", cid="", ctype="p2p":
                        calls.append(("refresh", oid, cid, ctype)) or "R")
    monkeypatch.setattr(fb, "cmd_confirm",
                        lambda nonce, oid="", cid="", ctype="p2p":
                        calls.append(("confirm", nonce, oid, cid, ctype)) or "C")
    monkeypatch.setattr(fb, "cmd_console",
                        lambda oid="", cid="", ctype="p2p":
                        calls.append(("console", oid, cid, ctype)) or "K")
    monkeypatch.setattr(fb, "cmd_subscribe",
                        lambda rest, oid="", cid="", ctype="p2p":
                        calls.append(("sub", rest, oid, cid, ctype)) or "U")
    monkeypatch.setattr(fb, "cmd_unsubscribe",
                        lambda oid="", cid="", ctype="p2p":
                        calls.append(("unsub", oid, cid, ctype)) or "X")

    assert fb._route("/近聊CH02 30分钟", "/近聊ch02 30分钟", "ou") == "T"
    assert calls[-1] == ("topics", "CH02 30分钟", "ou", "", "p2p")
    assert fb._route("/最新 2", "/最新 2", "ou") == "T"
    assert calls[-1][1] == "2"
    assert fb._route("/总结 CH02", "/总结 ch02", "ou") == "S"
    assert calls[-1][1] == "CH02"
    assert fb._route("/刷新", "/刷新", "ou") == "R"
    assert calls[-1][0] == "refresh"
    assert fb._route("/确认简报 ABC", "/确认简报 abc", "ou") == "C"
    assert calls[-1][1] == "ABC"
    assert fb._route("/控制台", "/控制台", "ou") == "K"
    assert fb._route("/订阅 10", "/订阅 10", "ou", "oc_1", "group") == "U"
    assert calls[-1] == ("sub", "10", "ou", "oc_1", "group")
    assert fb._route("/退订", "/退订", "ou", "oc_1", "group") == "X"


def test_route_status_remembers_for_refresh(monkeypatch):
    monkeypatch.setattr(fb, "cmd_status", lambda target="": "S %s" % target)
    assert fb._route("/状态 CH02", "/状态 ch02", "ou") == "S CH02"
    assert fb._LAST_BRIEF["ou"]["kind"] == "status"
    assert fb._LAST_BRIEF["ou"]["target"] == "CH02"


def test_route_group_unknown_is_silent(monkeypatch):
    monkeypatch.setattr(fb, "NL_ENABLED", False)
    assert fb._route("哈哈哈", "哈哈哈", "ou", "oc_1", "group") == ""
    monkeypatch.setattr(fb, "NL_ENABLED", True)
    assert fb._route("哈哈哈", "哈哈哈", "ou", "oc_1", "group") == ""


def test_nl_route_only_group_and_known_intents(monkeypatch):
    monkeypatch.setattr(fb, "NL_ENABLED", True)
    calls: list = []
    monkeypatch.setattr(fb, "cmd_summary",
                        lambda rest, oid="", cid="", ctype="p2p":
                        calls.append(("summary", rest)) or "S")
    monkeypatch.setattr(fb, "cmd_topics",
                        lambda rest, oid="", cid="", ctype="p2p":
                        calls.append(("topics", rest)) or "T")
    monkeypatch.setattr(fb, "cmd_status",
                        lambda target="": calls.append(("status", target)) or "Q")

    assert fb._nl_route("CH02 总结一下", "ou", "oc_1", "group") == "S"
    assert calls[-1] == ("summary", "CH02")
    assert fb._nl_route("最近10分钟在聊什么 CH2", "ou", "oc_1", "group") == "T"
    assert calls[-1] == ("topics", "CH02 10分钟")
    assert fb._nl_route("现在状态怎么样 CH2", "ou", "oc_1", "group") == "Q"
    assert calls[-1] == ("status", "CH02")
    assert fb._nl_route("今天天气不错", "ou", "oc_1", "group") == ""
    assert fb._nl_route("总结一下", "ou", "", "p2p") == ""
    monkeypatch.setattr(fb, "NL_ENABLED", False)
    assert fb._nl_route("总结一下", "ou", "oc_1", "group") == ""


# ---------------------------------------------------------------- 投递 / 菜单 / 订阅

def test_send_target_uses_chat_in_group(bot):
    assert fb._send_text_target("x", "ou", "oc_1", "group")
    assert bot.chat_texts == [("oc_1", "x")]
    assert fb._send_text_target("y", "ou", "oc_1", "p2p")
    assert bot.texts == [("ou", "y")]


def test_card_failure_falls_back_to_text(bot):
    bot.card_ok = False
    assert fb._send_card_target({"elements": []}, "兜底文字", "ou")
    assert bot.texts == [("ou", "兜底文字")]


def test_handle_text_group_replies_to_chat(monkeypatch, bot):
    monkeypatch.setattr(fb, "_route", lambda *a, **k: "回复")
    fb.handle_text("你好", "ou", "oc_1", "group")
    assert bot.chat_texts == [("oc_1", "回复")]
    assert not bot.texts


def test_handle_text_silent_when_no_reply(monkeypatch, bot):
    monkeypatch.setattr(fb, "_route", lambda *a, **k: "")
    fb.handle_text("哈哈哈", "ou")
    assert not bot.texts and not bot.chat_texts


def test_menu_maps_keys(monkeypatch, bot):
    monkeypatch.setattr(fb, "cmd_status", lambda arg="": "STATUS")
    monkeypatch.setattr(fb, "cmd_rooms", lambda: "ROOMS")
    sent: list = []
    monkeypatch.setattr(fb, "_send_text_target",
                        lambda text, oid, cid="", ctype="p2p":
                        sent.append(text) or True)
    fb.handle_menu("status", "ou")
    fb.handle_menu("rooms", "ou")
    fb.handle_menu("unknown", "ou")
    assert sent[:2] == ["STATUS", "ROOMS"]
    assert "/近聊" in sent[2]


def test_subscribe_group_target_and_unsubscribe():
    out = fb.cmd_subscribe("10", "ou", "oc_1", "group")
    assert "这个群" in out
    key = fb._sub_key("ou", "oc_1", "group")
    assert fb._SUBS[key]["chat_id"] == "oc_1"
    assert fb._SUBS[key]["minutes"] == 10
    assert "已退订" in fb.cmd_unsubscribe("ou")
    assert not fb._SUBS


def test_subscribe_clamps_minutes():
    fb.cmd_subscribe("1", "ou")
    assert fb._SUBS[fb._sub_key("ou")]["minutes"] == 5
    fb.cmd_subscribe("999", "ou")
    assert fb._SUBS[fb._sub_key("ou")]["minutes"] == 60


def test_brief_cancel_consumes_nonce():
    n = fb._new_nonce("rm1", "ou")
    toast, card = fb.handle_card({"act": "brief_cancel", "nonce": n}, "ou")
    assert "取消" in toast and card is None
    assert fb._take_nonce(n, "ou") is None


# ---------------------------------------------------------------- 群聊传输

def test_chat_whitelist_from_env(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu, "CFG_PATH", tmp_path / "feishu.json")
    monkeypatch.setenv("FEISHU_CHAT_IDS", "oc_1，oc_2 oc_3")
    assert feishu.load_config()["chat_ids"] == ["oc_1", "oc_2", "oc_3"]


def test_chat_whitelist_from_file(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu, "CFG_PATH", tmp_path / "feishu.json")
    monkeypatch.delenv("FEISHU_CHAT_IDS", raising=False)
    feishu.CFG_PATH.write_text(json.dumps({"chat_ids": ["oc_a"]}), encoding="utf-8")
    assert feishu.load_config()["chat_ids"] == ["oc_a"]


def test_mentions_and_strip_placeholders():
    class M:
        message_type = "text"
        content = json.dumps({"text": "@_user_1 /近聊 CH02 30分钟"})

    assert feishu.mentioned(M())
    assert feishu.strip_mentions(feishu.message_text(M())) == "/近聊 CH02 30分钟"


def test_mention_false_without_placeholder():
    class M:
        message_type = "text"
        content = json.dumps({"text": "/状态"})

    assert not feishu.mentioned(M())


def test_route_link_passes_group_target(monkeypatch):
    calls: list = []
    monkeypatch.setattr(fb, "cmd_watch",
                        lambda text, oid="", cid="", ctype="p2p":
                        calls.append((text, oid, cid, ctype)) or "W")
    monkeypatch.setattr(fb.watchlink, "extract",
                        lambda text: "https://x.example/live/1")
    assert fb._route("看看 https://x.example/live/1", "",
                     "ou", "oc_1", "group") == "W"
    assert calls and calls[0][2] == "oc_1" and calls[0][3] == "group"
