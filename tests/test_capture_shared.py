# -*- coding: utf-8 -*-
"""共享浏览器模式的代理逻辑：房间 = 标签页，采集 = 页面内 captureStream。

不拉真浏览器：FakeShared 按消息类型回放结果，验证代理怎么开标签页、等开播、
起采集、回收、续接 session。真实浏览器链路由 tools/capture_agent.py 手测。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import capture_agent


class FakeShared:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[dict] = []
        self.proc = None
        self.port = 1
        self.profile = Path("fake-profile")
        self.exe = "msedge"
        self.alive_flag = True

    def alive(self) -> bool:
        return self.alive_flag

    def ensure(self) -> None:
        pass

    def call(self, msg: dict, timeout: float = 15.0) -> dict:
        self.calls.append(dict(msg))
        r = self.responses.get(msg["type"], {})
        if callable(r):
            return r(msg)
        if isinstance(r, list):
            return r.pop(0) if r else {}
        return dict(r)

    def stop(self) -> None:
        pass

    def types(self) -> list[str]:
        return [c["type"] for c in self.calls]

    def first(self, typ: str) -> dict:
        return next(c for c in self.calls if c["type"] == typ)


def _room(**kw) -> dict:
    r = {"id": "rm1", "url": "https://live.douyin.com/123456", "name": "",
         "state": "pending", "session_id": ""}
    r.update(kw)
    return r


@pytest.fixture
def api_calls(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    def fake_api(base, path, payload=None, method="GET", timeout=8.0):
        calls.append((path, payload))
        return {}

    monkeypatch.setattr(capture_agent, "api", fake_api)
    return calls


def _agent(fake: FakeShared) -> capture_agent.SharedAgent:
    return capture_agent.SharedAgent("http://127.0.0.1:8765", fake)


def test_bring_up_opens_tab_and_reports_live(api_calls):
    fake = FakeShared({
        "page-open": {"ok": True, "tabId": 7},
        "page-probe": {"gone": False, "title": "测试房间 - 抖音直播",
                       "probe": {"playing": 1, "main": {"playing": True, "area": 90000,
                                                        "dur": -1},
                                 "url": "https://live.douyin.com/123456",
                                 "text": "测试房间 正在直播"}},
        "page-start": {"ok": True, "tabId": 7, "sid": "S1"},
        "page-focus-panel": {"ok": True},
    })
    agent = _agent(fake)
    room = _room()
    try:
        agent.bring_up(room)
        assert agent.running["rm1"]["tab_id"] == 7
    finally:
        agent.shutdown()

    assert fake.types()[0] == "page-open"
    assert room["name"] == "测试房间"
    assert room["session_id"] == "S1"
    reports = [p for path, p in api_calls if path == "/api/rooms/state"]
    assert any(p.get("state") == "live" and p.get("session_id") == "S1"
               for p in reports)


def test_bring_up_marks_ended_room_failed_and_closes_tab(api_calls):
    fake = FakeShared({
        "page-open": {"ok": True, "tabId": 9},
        "page-probe": {"gone": False, "title": "某直播间 - 抖音直播",
                       "probe": {"playing": 0, "main": None,
                                 "url": "https://live.douyin.com/123456",
                                 "text": "直播已结束"}},
    })
    agent = _agent(fake)
    room = _room()
    try:
        agent.bring_up(room)
    finally:
        agent.shutdown()

    reports = [p for path, p in api_calls if path == "/api/rooms/state"]
    assert any(p.get("state") == "failed" and "直播已结束" in str(p.get("error"))
               for p in reports)
    stop = fake.first("page-stop")
    assert stop["tabId"] == 9 and stop["keepSession"] is False
    assert fake.first("page-close")["tabId"] == 9
    assert "rm1" not in agent.running


def test_recover_reinjects_same_session_without_reopening_tab(api_calls):
    fake = FakeShared({
        "page-tab": {"ok": True, "tabId": 7},
        "page-start": {"ok": True, "tabId": 7, "sid": "S1"},
    })
    agent = _agent(fake)
    info = {"tab_id": 7, "sid": "S1", "started": 0.0}
    room = _room(state="live", session_id="S1", name="测试房间")
    agent.running["rm1"] = info
    try:
        agent.recover_capture(room, "rm1", info, 25.0)
    finally:
        agent.shutdown()

    assert "page-open" not in fake.types()
    start = fake.first("page-start")
    assert start["tabId"] == 7 and start["session"] == "S1"


def test_tick_syncs_session_recreated_by_extension(api_calls):
    fake = FakeShared({
        "page-tabs": {"tabs": [{"tabId": 7, "url": "https://live.douyin.com/123456"}]},
        "page-stats": {"caps": [{"tabId": 7, "sid": "NEW", "chunks": 10,
                                 "seconds": 2, "rms": 0.1}]},
        "page-probe": {"gone": False, "title": "测试房间",
                       "probe": {"playing": 1, "main": {"playing": True, "area": 90000,
                                                        "dur": -1},
                                 "url": "https://live.douyin.com/123456",
                                 "text": ""}},
    })
    agent = _agent(fake)
    room = _room(state="live", session_id="OLD", name="测试房间")
    agent.running["rm1"] = {"tab_id": 7, "sid": "OLD", "started": 0.0}
    try:
        agent.tick([room])
    finally:
        agent.shutdown()

    assert room["session_id"] == "NEW"
    reports = [p for path, p in api_calls if path == "/api/rooms/state"]
    assert any(p.get("session_id") == "NEW" and p.get("state") == "live"
               for p in reports)


def test_adopt_takes_over_tab_of_live_session(api_calls, monkeypatch):
    fake = FakeShared({
        "page-stats": {"caps": [{"tabId": 7, "sid": "S1", "chunks": 10,
                                 "seconds": 2, "rms": 0.1}]},
    })
    agent = _agent(fake)
    monkeypatch.setattr(agent, "session_alive", lambda room: True)
    room = _room(state="live", session_id="S1", name="测试房间")
    try:
        agent.adopt([room])
        assert agent.running["rm1"]["tab_id"] == 7
        assert agent.running["rm1"]["adopted"] is True
    finally:
        agent.shutdown()
