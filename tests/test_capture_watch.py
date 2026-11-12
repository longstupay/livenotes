# -*- coding: utf-8 -*-
"""采集代理的运行中复查：怎么判断「这场直播已经结束了」。

抖音下播后页面常常自动播推荐流，标签页音频轨不会断——所以不能只看"有没有声音"，
要看主画面是不是直播流、页面文案、房间地址。CI 不装实时依赖，整个文件跳过。
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("websockets")

from tools import capture_agent as ca  # noqa: E402

ROOM = {"url": "https://live.douyin.com/123456",
        "resolved_url": "https://live.douyin.com/123456"}
LIVE_MAIN = {"area": 700000, "playing": True, "dur": -1}      # 直播流：duration=Infinity
FEED_MAIN = {"area": 700000, "playing": True, "dur": 240}     # 推荐流/点播：固定时长


def probe(playing=1, main=None, url=ROOM["url"], text=""):
    return {"playing": playing, "main": LIVE_MAIN if main is None else main,
            "url": url, "text": text}


class Harness:
    """只实现 recheck_end 用到的两个方法，不碰 Chrome。"""

    def __init__(self, probes):
        self.probes = list(probes)
        self.ended: list[tuple[str, str]] = []

    def probe_page(self, rid, info):
        return self.probes.pop(0)

    def end_room(self, rid, room, why):
        self.ended.append((rid, why))


def test_live_stream_is_not_ended():
    assert ca.judge_ended(probe(), ROOM) == ""
    # 侧栏缩略图在播不算证据：只看最大的那个主画面
    thumb_only = {"area": 14000, "playing": True, "dur": 30}
    assert ca.judge_ended(probe(main=thumb_only), ROOM) == ""


def test_feed_video_in_main_player_means_ended():
    why = ca.judge_ended(probe(main=FEED_MAIN), ROOM)
    assert "有头有尾" in why


def test_dead_copy_with_stopped_main_means_ended():
    why = ca.judge_ended(probe(playing=0, main={"area": 700000, "playing": False, "dur": -1},
                               text="直播已结束，去逛逛别的直播间吧"), ROOM)
    assert "直播已结束" in why


def test_dead_copy_is_ignored_while_main_still_plays():
    # 直播间聊天里有人打「直播已结束」，但主画面还在播：不能据此停掉
    assert ca.judge_ended(probe(text="观众甲：直播已结束了吗"), ROOM) == ""


def test_leaving_the_live_room_url_means_ended():
    why = ca.judge_ended(probe(playing=0, main=None,
                               url="https://www.douyin.com/?recommend=1"), ROOM)
    assert "离开直播间" in why


def test_url_rule_only_applies_to_douyin_rooms():
    # 快手/B 站的地址不参与这条判断，否则每一路都会被判"离开直播间"
    other = {"url": "https://live.kuaishou.com/u/abc", "resolved_url": ""}
    assert ca.judge_ended(probe(playing=0, main=None,
                                url="https://live.kuaishou.com/u/abc"), other) == ""


def test_single_blip_needs_no_action(monkeypatch):
    monkeypatch.setattr(ca, "RECHECK_S", 0)
    h = Harness([probe(playing=0, main={"area": 700000, "playing": False, "dur": -1}),
                 probe()])
    info: dict = {}
    room = dict(ROOM, state="live")
    ca.Agent.recheck_end(h, room, "r1", info)
    assert h.ended == [] and info["end_hits"] == 1
    ca.Agent.recheck_end(h, room, "r1", info)
    assert h.ended == [] and info["end_hits"] == 0     # 缓过来就清零


def test_two_confirms_stop_the_room(monkeypatch):
    monkeypatch.setattr(ca, "RECHECK_S", 0)
    h = Harness([probe(main=FEED_MAIN), probe(main=FEED_MAIN)])
    info: dict = {}
    room = dict(ROOM, state="live")
    ca.Agent.recheck_end(h, room, "r1", info)
    assert h.ended == []
    ca.Agent.recheck_end(h, room, "r1", info)
    assert len(h.ended) == 1 and h.ended[0][0] == "r1"


def test_only_live_rooms_are_rechecked(monkeypatch):
    monkeypatch.setattr(ca, "RECHECK_S", 0)
    h = Harness([])                                  # 没有探针可用：调用就会 IndexError
    ca.Agent.recheck_end(h, dict(ROOM, state="stopped"), "r1", {})
    ca.Agent.recheck_end(h, dict(ROOM, state="starting"), "r1", {})


# ---------------------------------------------------------------- 采集自愈
class CaptureHarness:
    base = "http://127.0.0.1:8765"

    def __init__(self, status, probes=()):
        self.status = status
        self.probes = list(probes)
        self.recovered = []

    def probe_page(self, rid, info):
        return self.probes.pop(0)

    def recover_capture(self, room, rid, info, idle):
        self.recovered.append((rid, idle))


def test_check_capture_recovers_when_page_still_live(monkeypatch):
    h = CaptureHarness({"running": True, "last_chunk_ago_s": 25}, [probe()])
    monkeypatch.setattr(ca, "api", lambda base, path: h.status)
    room = dict(ROOM, state="live", session_id="S1")
    ca.Agent.check_capture(h, room, "r1", {})
    assert h.recovered == [("r1", 25.0)]


def test_check_capture_does_not_recover_when_stream_ended(monkeypatch):
    h = CaptureHarness({"running": True, "last_chunk_ago_s": 25},
                       [probe(main=FEED_MAIN)])
    monkeypatch.setattr(ca, "api", lambda base, path: h.status)
    room = dict(ROOM, state="live", session_id="S1")
    ca.Agent.check_capture(h, room, "r1", {})
    assert h.recovered == []


def test_check_capture_ignores_fresh_chunks(monkeypatch):
    h = CaptureHarness({"running": True, "last_chunk_ago_s": 5}, [])
    monkeypatch.setattr(ca, "api", lambda base, path: h.status)
    room = dict(ROOM, state="live", session_id="S1")
    info = {"auto_restarts": 2}
    ca.Agent.check_capture(h, room, "r1", info)
    assert h.recovered == []
    # 采集恢复正常后重启预算清零：下一次真断了还有完整次数
    assert info["auto_restarts"] == 0


def test_check_capture_respects_cooldown(monkeypatch):
    h = CaptureHarness({"running": True, "last_chunk_ago_s": 25}, [])
    monkeypatch.setattr(ca, "api", lambda base, path: h.status)
    room = dict(ROOM, state="live", session_id="S1")
    ca.Agent.check_capture(h, room, "r1", {"recovered_at": time.time()})
    assert h.recovered == []


class RecoverHarness:
    base = "http://127.0.0.1:8765"

    def __init__(self, pages):
        self.pages = pages
        self.reports = []
        self.opened = []
        self.brought_up = []

    def report(self, *a, **kw):
        self.reports.append((a, kw))

    def open_capture(self, rid, port, url):
        self.opened.append((rid, port, url))
        return True

    def bring_up(self, room, session=""):
        self.brought_up.append((room, session))


def test_recover_capture_reopens_same_session(monkeypatch):
    h = RecoverHarness([{"type": "page", "url": ROOM["url"], "id": "live"}])
    monkeypatch.setattr(ca, "http_json", lambda url, timeout=2: h.pages)
    room = dict(ROOM, state="live", session_id="S1", name="测试间")
    info = {"port": 1234}
    ca.Agent.recover_capture(h, room, "r1", info, 25.0)
    assert h.opened and h.opened[0][0] == "r1" and h.opened[0][1] == 1234
    assert "session=S1" in h.opened[0][2]
    assert info["recovered_at"] > 0


class RestartHarness:
    def __init__(self, p):
        self.probe = p
        self.running = {"r1": {"proc": None}}
        self.reports = []
        self.brought_up = []

    def probe_page(self, rid, info):
        return self.probe

    def report(self, *a, **kw):
        self.reports.append((a, kw))

    def bring_up(self, room, session=""):
        self.brought_up.append((room, session))
        # 真实 bring_up 会换一份新的 info，重启预算必须能带过去。
        self.running[room["id"]] = {"proc": None}

    def fail(self, *a, **kw):
        raise AssertionError("不该失败")


def test_restart_after_loss_when_page_still_live():
    h = RestartHarness(probe())
    room = dict(ROOM, id="r1", state="stopped", auto_stopped=True,
                auto_stop_reason="已经 124 秒没收到音频块（不是没人说话）",
                session_id="S1", name="测试间")
    assert ca.Agent.restart_after_loss(h, room, "r1") is True
    assert room["state"] == "pending" and room["auto_stopped"] is False
    assert h.brought_up and h.brought_up[0][0]["state"] == "pending"
    assert h.running["r1"]["auto_restarts"] == 1


def test_restart_after_loss_stops_after_max_restarts():
    h = RestartHarness(probe())
    room = dict(ROOM, id="r1", state="stopped", auto_stopped=True,
                auto_stop_reason="已经 124 秒没收到音频块", session_id="S1")
    for _ in range(ca.RESTART_MAX):
        room.update(state="stopped", auto_stopped=True,
                    auto_stop_reason="已经 124 秒没收到音频块")
        assert ca.Agent.restart_after_loss(h, room, "r1") is True
    room.update(state="stopped", auto_stopped=True,
                auto_stop_reason="已经 124 秒没收到音频块")
    assert ca.Agent.restart_after_loss(h, room, "r1") is False
    assert len(h.brought_up) == ca.RESTART_MAX


def test_restart_after_loss_skips_ended_stream():
    h = RestartHarness(probe(main=FEED_MAIN))
    room = dict(ROOM, state="stopped", auto_stopped=True,
                auto_stop_reason="已经 124 秒没收到音频块", session_id="S1")
    assert ca.Agent.restart_after_loss(h, room, "r1") is False
    assert h.brought_up == []


def test_restart_after_loss_keeps_browser_on_transient_probe_failure():
    h = RestartHarness(None)          # CDP 一时没应答
    room = dict(ROOM, state="stopped", auto_stopped=True,
                auto_stop_reason="已经 124 秒没收到音频块", session_id="S1")
    for _ in range(3):
        assert ca.Agent.restart_after_loss(h, room, "r1") is True
    assert h.brought_up == [] and h.running["r1"]["restart_probe_misses"] == 3
    # 连续探不到就放弃，交给 tick 回收浏览器
    assert ca.Agent.restart_after_loss(h, room, "r1") is False


def test_restart_after_loss_skips_manual_stop():
    h = RestartHarness(probe())
    room = dict(ROOM, state="stopped", auto_stopped=False, session_id="S1")
    assert ca.Agent.restart_after_loss(h, room, "r1") is False
    assert h.brought_up == []
