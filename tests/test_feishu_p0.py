# -*- coding: utf-8 -*-
"""飞书 P0：静默、去重、owner 固化、/状态、CH 房间解析。"""
from __future__ import annotations

import json

import pytest

import tools.feishu_bot as fb
from app import feishu
from app.config import LIVE_STALL_WARN_S


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(feishu, "CFG_PATH", tmp_path / "feishu.json")
    monkeypatch.delenv("FEISHU_OWNER_OPEN_ID", raising=False)
    monkeypatch.delenv("FEISHU_OPEN_ID", raising=False)
    fb._HINT_DAY.clear()
    yield
    fb._HINT_DAY.clear()


def _bot() -> feishu.Bot:
    """绕过 lark 客户端，只测 owner 判定。"""
    b = feishu.Bot.__new__(feishu.Bot)
    b.cfg = {}
    b.open_id = ""
    b.owner_open_id = ""
    b.seen_messages = feishu.MessageDeduper(500)
    return b


# ---------------------------------------------------------------- 去重

def test_message_deduper_ignores_repeat():
    d = feishu.MessageDeduper(500)
    assert d.seen("om_1") is False
    assert d.seen("om_1") is True
    assert d.seen("") is False          # 没有 message_id 的消息照常处理
    assert len(d) == 1


def test_message_deduper_keeps_last_n():
    d = feishu.MessageDeduper(3)
    for mid in ("a", "b", "c"):
        assert d.seen(mid) is False
    assert d.seen("a") is True          # 命中后移到最近
    assert d.seen("d") is False         # 挤掉最老的 b
    assert d.seen("b") is False         # b 已经被忘掉，当新消息
    assert len(d) == 3


# ---------------------------------------------------------------- owner

def test_first_sender_becomes_owner_and_cannot_be_overwritten():
    assert feishu.remember_open_id("ou_first") is True
    raw = json.loads(feishu.CFG_PATH.read_text(encoding="utf-8"))
    assert raw["owner_open_id"] == "ou_first"
    assert raw["open_id"] == "ou_first"

    assert feishu.remember_open_id("ou_second") is False
    raw = json.loads(feishu.CFG_PATH.read_text(encoding="utf-8"))
    assert raw["owner_open_id"] == "ou_first"
    assert raw["open_id"] == "ou_first"
    assert feishu.load_config()["owner_open_id"] == "ou_first"


def test_legacy_open_id_is_treated_as_owner(monkeypatch):
    feishu.CFG_PATH.write_text(json.dumps({"open_id": "ou_legacy"}), encoding="utf-8")
    assert feishu.load_config()["owner_open_id"] == "ou_legacy"
    assert feishu.remember_open_id("ou_stranger") is False
    assert json.loads(feishu.CFG_PATH.read_text(encoding="utf-8"))["open_id"] == "ou_legacy"


def test_env_owner_wins_without_writing_file(monkeypatch):
    monkeypatch.setenv("FEISHU_OWNER_OPEN_ID", "ou_boss")
    assert feishu.load_config()["owner_open_id"] == "ou_boss"
    assert feishu.remember_open_id("ou_stranger") is False
    assert not feishu.CFG_PATH.exists()


def test_bot_admit_only_owner():
    b = _bot()
    assert b.admit("ou_first") is True
    assert b.owner_open_id == "ou_first"
    assert b.open_id == "ou_first"
    assert b.admit("ou_second") is False
    assert b.open_id == "ou_first"      # 陌生人不能改写推送对象
    assert b.admit("") is False


def test_actor_open_id_accepts_three_event_shapes():
    class Id:
        def __init__(self, oid):
            self.open_id = oid

    class Sender:
        sender_id = Id("ou_msg")

    class MenuOp:
        operator_id = Id("ou_menu")

    class CardOp:
        open_id = "ou_card"

    assert feishu.actor_open_id(Sender()) == "ou_msg"
    assert feishu.actor_open_id(MenuOp()) == "ou_menu"
    assert feishu.actor_open_id(CardOp()) == "ou_card"
    assert feishu.actor_open_id(None) == ""

    # 卡片回调的 operator 是扁平的 open_id；取不到就会被误判成“没有权限”。
    b = _bot()
    b.owner_open_id = "ou_card"
    assert b.admit(feishu.actor_open_id(CardOp())) is True


# ---------------------------------------------------------------- 静默

def test_unknown_text_replies_help_at_most_once_a_day():
    first = fb._route("哈哈哈", "哈哈哈", "ou_1")
    second = fb._route("今天天气不错", "今天天气不错", "ou_1")
    assert "没看懂" in first
    assert "/状态" in first
    assert second == ""


def test_unknown_text_help_is_per_sender():
    assert fb._route("哈哈哈", "哈哈哈", "ou_1")
    assert fb._route("哈哈哈", "哈哈哈", "ou_2")


def test_route_link_wins_over_command_words(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(fb, "cmd_watch",
                        lambda text, *a, **k: calls.append(text) or "WATCH")
    out = fb._route("总结一下 https://live.douyin.com/123456", "", "ou_1")
    assert out == "WATCH"
    assert calls and "live.douyin.com" in calls[0]


# ---------------------------------------------------------------- 房间列表

def test_rooms_show_stable_ch(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", lambda: [
        {"id": "rm1", "seq": 1, "state": "live", "name": "一号",
         "url": "https://live.douyin.com/1"},
        {"id": "rm2", "seq": 2, "state": "stopped", "name": "二号",
         "url": "https://live.douyin.com/2"},
    ])
    out = fb.cmd_rooms()
    assert "CH01" in out and "CH02" in out
    assert "1. CH01" in out


# ---------------------------------------------------------------- /状态

def _rooms():
    return [
        {"id": "rm1", "seq": 1, "state": "live", "name": "一号",
         "session_id": "S1", "capture_engine": "direct"},
        {"id": "rm2", "seq": 2, "state": "live", "name": "二号",
         "session_id": "S2", "capture_engine": "per_room"},
    ]


def _sessions():
    return [
        {"id": "S1", "elapsed": 100, "n_utterances": 10, "last_line": "第一句",
         "last_final_ago_s": 3, "last_chunk_ago_s": 2, "paused": False,
         "utterances": [{"text": "第一句"}]},
        {"id": "S2", "elapsed": 50, "n_utterances": 1, "last_line": "",
         "last_final_ago_s": 1, "last_chunk_ago_s": 1, "paused": False,
         "utterances": [{"text": "第二句"}]},
    ]


def _api_factory(sessions=None, capture=None, closed=None, status=None):
    def fake(path, payload=None, timeout=6.0):
        if path == "/api/live/list":
            return {"sessions": _sessions() if sessions is None else sessions}
        if path == "/api/capture/status":
            return {"rooms": capture or {}}
        if path.startswith("/api/live/status"):
            if status is not None:
                return status
            sid = path.split("id=", 1)[-1]
            for s in (_sessions() if sessions is None else sessions):
                if s["id"] == sid:
                    return s
            return {}
        if path == "/api/rooms/close":
            if closed is not None:
                closed.append((payload or {}).get("id"))
            return {}
        return {}
    return fake


def test_status_lists_all_active_rooms(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", _rooms)
    monkeypatch.setattr(fb, "_api", _api_factory())
    out = fb.cmd_status("")
    assert "2 路状态" in out
    assert "CH01" in out and "CH02" in out
    assert "第一句" in out
    assert "第二句" in out          # /api/live/status 没给 last_line 时用 utterances 兜底
    assert "直取" in out


def test_status_single_ch_uses_detail(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", _rooms)
    monkeypatch.setattr(fb, "_api", _api_factory())
    out = fb.cmd_status("CH02")
    assert "CH02" in out
    assert "CH01" not in out
    assert "第二句" in out


def test_route_status_command(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", _rooms)
    monkeypatch.setattr(fb, "_api", _api_factory())
    out = fb._route("/状态 CH02", "/状态 ch02", "ou_1")
    assert "CH02" in out
    assert "CH01" not in out


def test_status_shows_capture_agent_state(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", lambda: [_rooms()[0]])
    cap = {"rm1": {"alive": False, "reason": "浏览器崩了", "age_s": 1}}
    monkeypatch.setattr(fb, "_api", _api_factory(capture=cap))
    out = fb.cmd_status("1")
    assert "采集代理：已退出" in out
    assert "浏览器崩了" in out


def test_status_unknown_ch_lists_available(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", _rooms)
    monkeypatch.setattr(fb, "_api", _api_factory())
    out = fb.cmd_status("9")
    assert "没有找到「9」" in out
    assert "CH01" in out and "CH02" in out


def test_status_stalled(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", lambda: [_rooms()[0]])
    stalled = dict(_sessions()[0],
                   last_chunk_ago_s=LIVE_STALL_WARN_S + 120,
                   last_final_ago_s=LIVE_STALL_WARN_S + 180)
    monkeypatch.setattr(fb, "_api", _api_factory(sessions=[stalled]))
    out = fb.cmd_status("1")
    assert "断流" in out
    assert "分钟前" in out


def test_status_session_lost_after_restart(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", lambda: [_rooms()[0]])
    monkeypatch.setattr(fb, "_api", _api_factory(sessions=[], status={}))
    out = fb.cmd_status("1")
    assert "会话丢失" in out
    assert "还没有字幕" not in out   # 不能把房间 live 当成还在出字


def test_status_no_active(monkeypatch):
    monkeypatch.setattr(fb, "_rooms", lambda: [
        {"id": "rm1", "seq": 1, "state": "stopped", "name": "一号"},
    ])
    monkeypatch.setattr(fb, "_api", _api_factory(sessions=[]))
    out = fb.cmd_status("")
    assert "现在没有在听" in out
    assert "CH01" in out


# ---------------------------------------------------------------- /stop 的 CH 语义

def test_stop_ch_resolves_by_stable_seq_not_display_order(monkeypatch):
    rooms = [
        {"id": "rm1", "seq": 1, "state": "stopped", "name": "一号"},
        {"id": "rm2", "seq": 2, "state": "live", "name": "二号"},
    ]
    closed: list[str] = []
    monkeypatch.setattr(fb, "_rooms", lambda: rooms)
    monkeypatch.setattr(fb, "_api", _api_factory(closed=closed))

    # 按排序，rm2 是第 1 路；CH01 仍然指向固定的 rm1。
    out = fb.cmd_stop("CH01")
    assert closed == ["rm1"]
    assert "CH01" in out


def test_stop_plain_number_keeps_old_semantics(monkeypatch):
    rooms = [
        {"id": "rm1", "seq": 1, "state": "stopped", "name": "一号"},
        {"id": "rm2", "seq": 2, "state": "live", "name": "二号"},
    ]
    closed: list[str] = []
    monkeypatch.setattr(fb, "_rooms", lambda: rooms)
    monkeypatch.setattr(fb, "_api", _api_factory(closed=closed))

    out = fb.cmd_stop("1")
    assert closed == ["rm2"]
    assert "CH02" in out
