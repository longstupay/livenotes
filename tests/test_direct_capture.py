# -*- coding: utf-8 -*-
"""抖音直取代理：不拉真浏览器/ffmpeg，用假进程验证会话、分块、重建、回退。"""
from __future__ import annotations

import io

import pytest

from app import export, live, models
from tools import capture_agent, direct_capture


class FakeProc:
    def __init__(self, data: bytes = b"", rc=None, pid: int = 1234) -> None:
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(b"")
        self.returncode = rc
        self.pid = pid
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _room(**kw) -> dict:
    r = {"id": "rm1", "url": "https://live.douyin.com/123456", "name": "测试房间",
         "state": "pending", "session_id": "", "capture_engine": ""}
    r.update(kw)
    return r


@pytest.fixture
def api_calls(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_api(base, path, payload=None, method="GET", timeout=8.0):
        calls.append((path, dict(payload or {})))
        if path == "/api/live/start":
            return {"session_id": "LIVE_test", "source_mode": "direct"}
        if path.startswith("/api/live/status"):
            return {"running": True}
        return {}

    monkeypatch.setattr(capture_agent, "api", fake_api)
    monkeypatch.setattr(direct_capture, "api", fake_api)
    return calls


@pytest.fixture
def fake_media(monkeypatch):
    chunks: list[tuple[str, bytes]] = []

    def fake_resolve(url, timeout=20.0):
        return "http://stream.example/live.flv", "fake"

    def fake_spawn(stream):
        # 正好一块 4096 样本；pcm_rms 非零，避免被当成静音。
        return FakeProc(b"\x00\x01" * direct_capture.CHUNK_SAMPLES)

    def fake_post(base, sid, data, timeout=5.0):
        chunks.append((sid, data))

    monkeypatch.setattr(direct_capture, "resolve_douyin", fake_resolve)
    monkeypatch.setattr(direct_capture, "spawn_ffmpeg", fake_spawn)
    monkeypatch.setattr(direct_capture, "post_chunk", fake_post)
    return chunks


def test_bring_up_creates_session_and_reports_live(api_calls, fake_media):
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    try:
        a.bring_up(_room())
        starts = [b for p, b in api_calls if p == "/api/live/start"]
        assert starts and starts[0].get("source_mode") == "direct"
        live_reports = [b for p, b in api_calls
                        if p == "/api/rooms/state" and b.get("state") == "live"]
        assert live_reports and live_reports[0].get("session_id") == "LIVE_test"
        assert fake_media and fake_media[0][0] == "LIVE_test"
        assert len(fake_media[0][1]) == direct_capture.CHUNK_BYTES
        info = a.running["rm1"]
        assert info["sid"] == "LIVE_test"
        assert info["first_audio_ms"] is not None
    finally:
        a.shutdown()


def test_bring_up_reports_real_web_rid_url(api_calls, fake_media, monkeypatch):
    """页面地址要用 owner.webRid，绝不能拿 reflow 的 room id 顶替。

    旧实现把 room id 拼成 live.douyin.com/<room id>，抖音只回
    「服务器开小差了」——直播间明明开着，页面却没有画面。
    """
    monkeypatch.setattr(direct_capture, "room_meta",
                        lambda url, timeout=8.0: {"web_rid": "123456789012",
                                                  "nickname": "主播", "title": "标题"})
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    try:
        a.bring_up(_room(
            url="https://webcast.amemv.com/douyin/webcast/reflow/7700000000000000003"))
        urls = [b.get("url") for p, b in api_calls
                if p == "/api/rooms/state" and b.get("url")]
        assert urls and urls[0] == "https://live.douyin.com/123456789012"
        assert "7700000000000000003" not in urls[0]
    finally:
        a.shutdown()


def test_bring_up_does_not_report_a_fake_url_without_web_rid(api_calls, fake_media,
                                                              monkeypatch):
    """拿不到 webRid 就什么都不报：前端会退回打开 reflow 页。"""
    monkeypatch.setattr(direct_capture, "room_meta",
                        lambda url, timeout=8.0: {"nickname": "主播"})
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    try:
        a.bring_up(_room(
            url="https://webcast.amemv.com/douyin/webcast/reflow/7700000000000000003"))
        assert not [b for p, b in api_calls
                    if p == "/api/rooms/state" and b.get("url")]
    finally:
        a.shutdown()


def test_bring_up_stops_orphan_session_when_room_stopped_midway(monkeypatch):
    """建会话期间用户点了停止：新会话要收掉，不能再报 starting/live。"""
    calls: list[tuple[str, dict]] = []

    def fake_api(base, path, payload=None, method="GET", timeout=8.0):
        calls.append((path, dict(payload or {})))
        if path == "/api/live/start":
            return {"session_id": "LIVE_orphan", "source_mode": "direct"}
        if path == "/api/rooms":
            return {"rooms": [{"id": "rm1", "state": "stopped"}]}
        return {}

    monkeypatch.setattr(capture_agent, "api", fake_api)
    monkeypatch.setattr(direct_capture, "api", fake_api)
    resolved: list = []
    monkeypatch.setattr(direct_capture, "resolve_douyin",
                        lambda *a, **k: resolved.append(1) or ("x", ""))
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    try:
        a.bring_up(_room())
        stops = [b for p, b in calls if p == "/api/live/stop"]
        assert stops and stops[-1].get("session_id") == "LIVE_orphan"
        reports = [b for p, b in calls if p == "/api/rooms/state"]
        assert not [b for b in reports
                    if b.get("session_id") == "LIVE_orphan"]
        assert not resolved            # 没有继续解析/拉 ffmpeg
        assert "rm1" not in a.running
    finally:
        a.shutdown()


def test_non_douyin_auto_mode_goes_straight_to_browser(api_calls, monkeypatch):
    class FakeFallback:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def bring_up(self, room, session=""):
            self.calls.append((room["id"], session))

    fb = FakeFallback()
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    a.fallback_mode = "auto"
    monkeypatch.setattr(a, "_get_fallback", lambda: fb)
    a.fallback_engine = "shared"
    try:
        a.bring_up(_room(url="https://live.bilibili.com/123"))
        assert fb.calls == [("rm1", "")]
        # 没有建 direct 会话，也没有尝试解析。
        assert not any(p == "/api/live/start" for p, _ in api_calls)
        assert direct_capture.is_douyin("https://live.douyin.com/1")
        assert not direct_capture.is_douyin("https://www.bilibili.com/1")
    finally:
        a.shutdown()


def test_non_douyin_ask_mode_waits_for_user(api_calls, monkeypatch):
    monkeypatch.delenv("LIVENOTES_DIRECT_FALLBACK", raising=False)
    monkeypatch.setattr(direct_capture, "find_chrome", lambda: "chrome.exe")
    monkeypatch.setattr(direct_capture, "find_shared_browser",
                        lambda: (_ for _ in ()).throw(SystemExit("no edge")))
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    try:
        a.bring_up(_room(url="https://live.bilibili.com/123"))
        reports = [b for p, b in api_calls if p == "/api/rooms/state"]
        failed = [b for b in reports if b.get("state") == "failed"]
        assert failed and failed[-1].get("switch_ready") is True
        assert failed[-1].get("switch_engine") == "per_room"
        assert "只支持抖音" in failed[-1].get("error", "")
        # 没有自动拉起浏览器：这里根本不会调 _get_fallback()。
        assert a.fallback is None
    finally:
        a.shutdown()


def test_resolve_failure_auto_mode_falls_back_with_same_session(api_calls, monkeypatch):
    def bad_resolve(url, timeout=20.0):
        raise direct_capture.ResolveError("解析失败")

    monkeypatch.setattr(direct_capture, "resolve_douyin", bad_resolve)

    class FakeFallback:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def bring_up(self, room, session=""):
            self.calls.append((room["id"], session))

        def tick(self, rooms):
            pass

        def fail(self, *args, **kwargs):
            pass

        def adopt(self, rooms):
            pass

        def shutdown(self):
            pass

    fb = FakeFallback()
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    a.fallback_mode = "auto"
    monkeypatch.setattr(a, "_get_fallback", lambda: fb)
    a.fallback_engine = "shared"
    try:
        a.bring_up(_room())
        assert fb.calls == [("rm1", "LIVE_test")]
        assert "rm1" in a.fallback_rooms
        events = [b.get("capture_event", {}).get("kind") for p, b in api_calls
                  if p == "/api/rooms/state"]
        assert "fallback" in events
    finally:
        a.shutdown()


def test_resolve_failure_ask_mode_waits_for_user(api_calls, monkeypatch):
    def bad_resolve(url, timeout=20.0):
        raise direct_capture.ResolveError("解析失败")

    monkeypatch.setattr(direct_capture, "resolve_douyin", bad_resolve)
    monkeypatch.setattr(direct_capture, "find_chrome", lambda: "chrome.exe")
    monkeypatch.delenv("LIVENOTES_DIRECT_FALLBACK", raising=False)

    class Boom:
        def bring_up(self, *a, **kw):
            raise AssertionError("ask 模式不应自动拉起浏览器")

    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    monkeypatch.setattr(a, "_get_fallback", lambda: Boom())
    try:
        a.bring_up(_room())
        reports = [b for p, b in api_calls if p == "/api/rooms/state"]
        failed = [b for b in reports if b.get("state") == "failed"]
        assert failed, reports
        last = failed[-1]
        assert last.get("switch_ready") is True
        assert last.get("switch_engine") == "per_room"
        assert last.get("switch_requested") is False
        assert "直取失败" in last.get("error", "")
        assert "rm1" not in a.fallback_rooms
        events = [b.get("capture_event", {}).get("kind") for p, b in api_calls
                  if p == "/api/rooms/state"]
        assert "switch_needed" in events
        assert "fallback" not in events
    finally:
        a.shutdown()


def test_recover_reuses_session_and_restarts_ffmpeg(api_calls, fake_media):
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    room = _room(state="live", session_id="LIVE_test")
    old = FakeProc(b"", rc=1)
    info = a._new_info(old, "LIVE_test", "http://old.example/live.flv")
    a.running["rm1"] = info
    try:
        a._check(room, info)
        assert info["restarts"] == 1
        assert info["sid"] == "LIVE_test"
        assert info["proc"] is not old
        # 已经退出的进程不再 terminate，但确实已经不在跑了。
        assert old.returncode == 1
        assert not any(p == "/api/live/start" for p, _ in api_calls)
    finally:
        a.shutdown()


def test_read_loop_rechunks_to_4096_samples(api_calls, monkeypatch):
    posted: list[bytes] = []
    monkeypatch.setattr(direct_capture, "post_chunk",
                        lambda base, sid, data, timeout=5.0: posted.append(data))
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    room = _room(state="live", session_id="S")
    proc = FakeProc(b"\x01\x00" * 5000)      # 10000 字节：一块 + 1808 字节尾巴
    info = a._new_info(proc, "S", "http://x.example/live.flv")
    try:
        a._read_loop("rm1", room, info, 0)
        assert len(posted) == 1
        assert len(posted[0]) == direct_capture.CHUNK_BYTES
        assert info["chunks"] == 1
    finally:
        a.shutdown()


def test_renew_session_skipped_when_room_stopped(monkeypatch):
    """点了停止后会话 404，不能再建新会话接着录。"""
    calls: list[tuple[str, dict]] = []

    def fake_api(base, path, payload=None, method="GET", timeout=8.0):
        calls.append((path, dict(payload or {})))
        if path == "/api/rooms":
            return {"rooms": [{"id": "rm1", "state": "stopped"}]}
        if path == "/api/live/start":
            return {"session_id": "NEW"}
        return {}

    monkeypatch.setattr(direct_capture, "api", fake_api)
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    proc = FakeProc()
    info = a._new_info(proc, "OLD", "http://stream.example/live.flv")
    a.running["rm1"] = info
    try:
        a._renew_session("rm1", _room(state="live"), info)
        assert not any(p == "/api/live/start" for p, _ in calls)
        assert "rm1" not in a.running
        assert proc.terminated and info["sid"] == "OLD"
    finally:
        a.shutdown()


def test_renew_session_rebuilds_when_room_still_live(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_api(base, path, payload=None, method="GET", timeout=8.0):
        calls.append((path, dict(payload or {})))
        if path == "/api/rooms":
            return {"rooms": [{"id": "rm1", "state": "live"}]}
        if path == "/api/live/start":
            return {"session_id": "NEW"}
        return {}

    monkeypatch.setattr(direct_capture, "api", fake_api)
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    info = a._new_info(FakeProc(), "OLD", "http://stream.example/live.flv")
    a.running["rm1"] = info
    try:
        a._renew_session("rm1", _room(state="live"), info)
        assert any(p == "/api/live/start" for p, _ in calls)
        assert info["sid"] == "NEW"
        assert "rm1" in a.running
    finally:
        a.shutdown()


def test_renew_session_waits_when_rooms_unreachable(monkeypatch):
    """服务暂时不可达时先不重建，避免网络抖动误建一场新会话。"""
    calls: list[tuple[str, dict]] = []

    def fake_api(base, path, payload=None, method="GET", timeout=8.0):
        calls.append((path, dict(payload or {})))
        if path == "/api/rooms":
            raise RuntimeError("暂时不可达")
        return {}

    monkeypatch.setattr(direct_capture, "api", fake_api)
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    info = a._new_info(FakeProc(), "OLD", "http://stream.example/live.flv")
    a.running["rm1"] = info
    try:
        a._renew_session("rm1", _room(state="live"), info)
        assert not any(p == "/api/live/start" for p, _ in calls)
        assert "rm1" in a.running and info["sid"] == "OLD"
    finally:
        a.shutdown()


def test_canonical_douyin_url_uses_web_rid_not_room_id(monkeypatch):
    """reflow 里的 room id 不是 web_rid：拿它拼地址就是「服务器开小差了」。"""
    reflow = ("https://webcast.amemv.com/douyin/webcast/reflow/"
              "7700000000000000003")
    html = ('<script>{"owner":{"id":1,"nickname":"主播",'
            '"webRid":"123456789012"}}</script>')

    def fake_landing(url, timeout=10.0):
        return reflow if direct_capture.is_douyin(url) else ""

    monkeypatch.setattr(direct_capture, "_landing_url", fake_landing)
    monkeypatch.setattr(direct_capture, "_fetch_reflow",
                        lambda landing, timeout: (html, 0.1))
    assert direct_capture.canonical_douyin_url(reflow) == \
        "https://live.douyin.com/123456789012"
    assert direct_capture.canonical_douyin_url(
        "https://live.douyin.com/123456?from=x") == "https://live.douyin.com/123456"
    assert direct_capture.canonical_douyin_url("https://live.bilibili.com/1") == ""
    assert direct_capture.is_douyin(reflow)


def test_canonical_douyin_url_refuses_to_invent_a_room(monkeypatch):
    """没有 webRid 时返回空串，绝不把 room id 伪装成房号。"""
    reflow = ("https://webcast.amemv.com/douyin/webcast/reflow/"
              "7700000000000000003")
    monkeypatch.setattr(direct_capture, "_landing_url",
                        lambda url, timeout=10.0: reflow)
    monkeypatch.setattr(direct_capture, "_fetch_reflow",
                        lambda landing, timeout: ("<html>no meta</html>", 0.1))
    assert direct_capture.canonical_douyin_url(reflow) == ""


def test_canonical_douyin_url_follows_short_link(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def geturl(self):
            return ("https://webcast.amemv.com/douyin/webcast/reflow/"
                    "7700000000000000003")

    monkeypatch.setattr(direct_capture.urllib.request, "urlopen",
                        lambda req, timeout=10: Resp())
    monkeypatch.setattr(direct_capture, "_fetch_reflow",
                        lambda landing, timeout: (
                            '<script>{"owner":{"webRid":"123456789012"}}</script>',
                            0.1))
    assert direct_capture.canonical_douyin_url("https://v.douyin.com/NSwokYsauAE") == \
        "https://live.douyin.com/123456789012"


def test_resolve_douyin_uses_reflow_player_url(monkeypatch):
    html = ('<html><webcast-reflow-player url="http://pull.example/'
            'stream-1_or4.flv?a=1&amp;b=2" backupUrl="http://hls/x.m3u8">'
            '</webcast-reflow-player></html>')
    monkeypatch.setattr(direct_capture, "_landing_url",
                        lambda url, timeout=10.0: direct_capture._REFLOW_BASE + "1")
    monkeypatch.setattr(direct_capture, "_fetch_reflow",
                        lambda landing, timeout: (html, 0.2))
    monkeypatch.setattr(direct_capture, "_resolve_streamlink",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("有播放地址时不该走 streamlink")))
    url, note = direct_capture.resolve_douyin(
        "https://webcast.amemv.com/douyin/webcast/reflow/1")
    assert url == "http://pull.example/stream-1_or4.flv?a=1&b=2"
    assert note.startswith("reflow ")


def test_resolve_douyin_offline_page_fails_fast(monkeypatch):
    monkeypatch.setattr(direct_capture, "_landing_url",
                        lambda url, timeout=10.0: direct_capture._REFLOW_BASE + "1")
    monkeypatch.setattr(direct_capture, "_fetch_reflow",
                        lambda landing, timeout: ("<div>直播已结束</div>", 0.1))
    monkeypatch.setattr(direct_capture, "_resolve_streamlink",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("页面明说下播时不该再跑 streamlink")))
    try:
        direct_capture.resolve_douyin(
            "https://webcast.amemv.com/douyin/webcast/reflow/1")
    except direct_capture.ResolveError as e:
        assert "没有开播" in str(e)
    else:
        raise AssertionError("应该报 ResolveError")


def test_resolve_douyin_server_error_is_not_offline(monkeypatch):
    monkeypatch.setattr(direct_capture, "_landing_url",
                        lambda url, timeout=10.0: direct_capture._REFLOW_BASE + "1")
    monkeypatch.setattr(direct_capture, "_fetch_reflow",
                        lambda landing, timeout: ("<div>服务器开小差了</div>", 0.1))
    monkeypatch.setattr(direct_capture, "_resolve_streamlink",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            direct_capture.ResolveError("streamlink 解析失败")))
    try:
        direct_capture.resolve_douyin(
            "https://webcast.amemv.com/douyin/webcast/reflow/1")
    except direct_capture.ResolveError as e:
        msg = str(e)
        assert "临时故障" in msg
        assert "没在播" not in msg
    else:
        raise AssertionError("应该报 ResolveError")


def test_resolve_douyin_falls_back_to_streamlink(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(direct_capture, "_landing_url",
                        lambda url, timeout=10.0: direct_capture._REFLOW_BASE + "1")
    monkeypatch.setattr(direct_capture, "_fetch_reflow",
                        lambda landing, timeout: (
                            '<html>no stream here<script>{"owner":'
                            '{"webRid":"123456789012"}}</script></html>', 0.1))

    def fake_streamlink(target, timeout):
        seen.append(target)
        return "http://stream.example/live.flv", "streamlink 0.1s"

    monkeypatch.setattr(direct_capture, "_resolve_streamlink", fake_streamlink)
    url, note = direct_capture.resolve_douyin(
        "https://webcast.amemv.com/douyin/webcast/reflow/1")
    assert url == "http://stream.example/live.flv"
    assert seen == ["https://live.douyin.com/123456789012"]


def test_direct_fallback_defaults_to_ask(monkeypatch):
    monkeypatch.delenv("LIVENOTES_DIRECT_FALLBACK", raising=False)
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    assert a.fallback_mode == "ask"


def test_apply_config_hot_updates_fallback_mode(monkeypatch):
    monkeypatch.delenv("LIVENOTES_DIRECT_FALLBACK", raising=False)
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")

    class OldFallback:
        def __init__(self):
            self.stopped = False

        def shutdown(self):
            self.stopped = True

    old = OldFallback()
    try:
        a.apply_config({"direct_fallback": "none"})
        assert a.fallback_mode == "none"
        # 未知值忽略，不会把已有设置改坏
        a.apply_config({"direct_fallback": "bogus"})
        assert a.fallback_mode == "none"
        # 切换策略时丢掉没在用的旧回退代理，下一次按新策略选引擎
        a.fallback = old
        a.fallback_engine = "shared"
        a.apply_config({"direct_fallback": "auto"})
        assert a.fallback_mode == "auto"
        assert a.fallback is None and old.stopped
        # 环境变量仍优先，方便脚本临时覆盖
        monkeypatch.setenv("LIVENOTES_DIRECT_FALLBACK", "per_room")
        a.apply_config({"direct_fallback": "none"})
        assert a.fallback_mode == "per_room"
    finally:
        a.shutdown()


def test_tick_switches_only_after_user_confirms(api_calls, monkeypatch):
    class FakeFallback:
        engine = "per_room"

        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def bring_up(self, room, session=""):
            self.calls.append((room["id"], session))

        def tick(self, rooms):
            pass

        def fail(self, *args, **kwargs):
            pass

        def adopt(self, rooms):
            pass

        def shutdown(self):
            pass

    fb = FakeFallback()
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    a.fallback_mode = "ask"
    a.fallback_engine = "per_room"
    monkeypatch.setattr(a, "_get_fallback", lambda: fb)
    try:
        # 只失败、用户没点：绝不切。
        a.tick([_room(state="failed", switch_ready=True, switch_requested=False)])
        assert fb.calls == []
        # 用户在收听页确认后才切，并且清掉等待位。
        a.tick([_room(state="failed", switch_ready=True, switch_requested=True,
                      switch_reason="streamlink 解析失败",
                      session_id="LIVE_test")])
        assert fb.calls == [("rm1", "LIVE_test")]
        assert "rm1" in a.fallback_rooms
        reports = [b for p, b in api_calls if p == "/api/rooms/state"]
        started = [b for b in reports if b.get("state") == "starting"]
        assert started, reports
        assert started[-1].get("switch_requested") is False
        assert started[-1].get("switch_ready") is False
    finally:
        a.shutdown()


def test_tick_applies_changed_policy_to_existing_failed_rooms(api_calls, monkeypatch):
    class FakeFallback:
        engine = "per_room"

        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def bring_up(self, room, session=""):
            self.calls.append((room["id"], session))

        def tick(self, rooms):
            pass

        def fail(self, *args, **kwargs):
            pass

        def adopt(self, rooms):
            pass

        def shutdown(self):
            pass

    fb = FakeFallback()
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    monkeypatch.setattr(a, "_get_fallback", lambda: fb)
    try:
        # 设置页从 ask 改成 auto 后，已经停在待切换的房间也补切一次。
        a.fallback_mode = "auto"
        a.tick([_room(state="failed", switch_ready=True, switch_requested=False,
                      switch_engine="per_room", session_id="LIVE_test")])
        assert fb.calls == [("rm1", "LIVE_test")]
    finally:
        a.shutdown()


def test_tick_none_policy_removes_switch_offer(api_calls):
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    try:
        a.fallback_mode = "none"
        a.tick([_room(state="failed", switch_ready=True, switch_requested=False,
                      switch_engine="per_room")])
        reports = [b for p, b in api_calls if p == "/api/rooms/state"]
        assert reports
        assert reports[-1].get("switch_ready") is False
        assert reports[-1].get("switch_engine") == ""
    finally:
        a.shutdown()


def test_server_hiccup_is_not_treated_as_offline():
    # 抖音的「服务器开小差了」是临时错误，直播间可能明明开着。
    assert "服务器开小差了" not in capture_agent.DEAD_MARKS
    assert "服务器开小差了" in capture_agent.PAGE_ERROR_MARKS


def test_agent_reports_its_engine(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_api(base, path, payload=None, method="GET", timeout=8.0):
        calls.append((path, dict(payload or {})))

    monkeypatch.setattr(capture_agent, "api", fake_api)
    a = capture_agent.Agent("http://127.0.0.1:8765", need_browser=False)
    a.report("rm1", "live", stage=6)
    assert calls[-1][1]["engine"] == "per_room"
    assert capture_agent.SharedAgent.engine == "shared"
    assert direct_capture.DirectAgent.engine == "direct"


def test_live_session_source_mode_direct():
    s = live.LiveSession("测试", source_mode="direct")
    assert s.source_mode == "direct"
    assert s.status()["source_mode"] == "direct"
    assert live.LiveSession("测试").source_mode == "tab"


def test_source_mode_meta_and_export():
    assert "direct" in models.SOURCE_MODE_META
    assert export._mode_name("direct") == "直取直播流（ffmpeg）"


# ---------------------------------------------------------------- 房间名
# 直取只从 reflow 页拿播放地址，页头的房间信息白丢了，于是收听页只能显示
# 域名（webcast.amemv.com）。下面这一段是 reflow 页首屏 JSON 的真实形状：
# RSC 把引号转义成 \"，名字就在里面。
_REFLOW_HTML = r"""<script>self.__rsc_f.push([1,"5:[\"$\",\"$L7\",null,{\"data\":{\"room\":{\"id\":7700000000000000003,\"idStr\":\"7700000000000000003\",\"status\":2,\"title\":\"示例直播标题\",\"userCount\":26,\"owner\":{\"id\":123456789,\"nickname\":\"示例主播\"}}}"])</script>"""


def test_parse_room_meta_reads_nickname_and_title():
    meta = direct_capture.parse_room_meta(_REFLOW_HTML)
    assert meta == {"nickname": "示例主播", "title": "示例直播标题"}
    assert direct_capture.display_name(meta) == \
        "示例主播 · 示例直播标题"


def test_parse_room_meta_reads_web_rid_from_owner():
    """owner.webRid 才是 live.douyin.com 的房号；观众对象里的号不能顶上来。"""
    html = ('<script>{"room":{"title":"今晚聊 FDE"},"owner":{"id":1,'
            '"nickname":"老张","webRid":"123456789012"},'
            '"viewer":{"webRid":"123456789012"}}</script>')
    meta = direct_capture.parse_room_meta(html)
    assert meta["web_rid"] == "123456789012"
    assert direct_capture.page_url(meta) == "https://live.douyin.com/123456789012"


def test_page_url_rejects_missing_or_bogus_web_rid():
    assert direct_capture.page_url({}) == ""
    assert direct_capture.page_url({"web_rid": "../x"}) == ""
    assert direct_capture.page_url({"web_rid": "123456789012"}) == \
        "https://live.douyin.com/123456789012"


def test_resolve_caches_meta_for_page_url(monkeypatch):
    """解析流地址和算页面地址共用同一张 reflow 页，不能多跑一次 HTTP。"""
    landing = direct_capture._REFLOW_BASE + "1"
    html = ('<webcast-reflow-player url="http://pull.example/live.flv">'
            '</webcast-reflow-player>'
            '<script>{"owner":{"webRid":"123456789012"}}</script>')
    calls: list[str] = []

    def fake_fetch(url, timeout):
        calls.append(url)
        return html, 0.1

    monkeypatch.setattr(direct_capture, "_landing_url",
                        lambda url, timeout=10.0: landing)
    monkeypatch.setattr(direct_capture, "_fetch_reflow", fake_fetch)
    direct_capture._META_CACHE.clear()
    stream, note = direct_capture.resolve_douyin(landing)
    assert stream == "http://pull.example/live.flv"
    meta = direct_capture.room_meta(landing)
    assert meta["web_rid"] == "123456789012"
    assert len(calls) == 1


def test_parse_room_meta_reads_plain_json_too():
    """同一份 HTML 换成未转义的 JSON（页面结构改版）也得认。"""
    plain = '{"room":{"title":"今晚聊 FDE","owner":{"nickname":"老张"}}}'
    assert direct_capture.parse_room_meta(plain) == {"nickname": "老张",
                                                    "title": "今晚聊 FDE"}


def test_parse_room_meta_ignores_useless_pages():
    # 下播页、空页、占位值都不该硬编出一个名字
    assert direct_capture.parse_room_meta("<div>直播已结束</div>") == {}
    assert direct_capture.parse_room_meta("") == {}
    assert direct_capture.parse_room_meta('{"title":"$undefined"}') == {}
    assert direct_capture.display_name({}) == ""


def test_display_name_prefers_anchor_when_truncating():
    meta = {"nickname": "某某主播", "title": "很长" * 40}
    name = direct_capture.display_name(meta, limit=20)
    assert name.startswith("某某主播 · ")
    assert len(name) <= 20 and name.endswith("…")


def test_display_name_falls_back_to_whichever_exists():
    assert direct_capture.display_name({"nickname": "老张"}) == "老张"
    assert direct_capture.display_name({"title": "今晚聊 FDE"}) == "今晚聊 FDE"


def test_room_meta_swallows_fetch_failure(monkeypatch):
    """名字是锦上添花：抓页面失败也只能返回空，绝不能把开播流程带崩。"""
    monkeypatch.setattr(direct_capture, "_landing_url",
                        lambda url, timeout=10.0: "https://webcast.amemv.com/"
                        "douyin/webcast/reflow/1")
    direct_capture._META_CACHE.clear()

    def boom(*a, **kw):
        raise OSError("no network")

    monkeypatch.setattr(direct_capture, "_fetch_reflow", boom)
    assert direct_capture.room_meta("https://live.douyin.com/1") == {}


def test_room_meta_caches_so_one_start_only_fetches_once(monkeypatch):
    monkeypatch.setattr(direct_capture, "_landing_url",
                        lambda url, timeout=10.0: "https://webcast.amemv.com/"
                        "douyin/webcast/reflow/2")
    direct_capture._META_CACHE.clear()
    calls = []

    def fake_fetch(landing, timeout):
        calls.append(landing)
        return _REFLOW_HTML, 0.1

    monkeypatch.setattr(direct_capture, "_fetch_reflow", fake_fetch)
    first = direct_capture.room_meta("https://live.douyin.com/2")
    second = direct_capture.room_meta("https://live.douyin.com/2")
    assert first == second and len(calls) == 1


def test_bring_up_names_room_before_creating_session(api_calls, fake_media, monkeypatch):
    """名字要在建会话之前拿到：会话标题（飞书推送/导出文件名）拼的就是它。"""
    monkeypatch.setattr(direct_capture, "room_meta",
                        lambda url, timeout=8.0: {"nickname": "示例主播",
                                                  "title": "今晚聊 FDE"})
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    try:
        a.bring_up(_room(name="", name_source="share"))
    finally:
        a.shutdown()
    named = [i for i, (p, b) in enumerate(api_calls)
             if p == "/api/rooms/state" and b.get("name")]
    start = [i for i, (p, b) in enumerate(api_calls) if p == "/api/live/start"]
    assert named and start and named[0] < start[0]
    assert api_calls[named[0]][1]["name"] == "示例主播 · 今晚聊 FDE"
    assert "示例主播 · 今晚聊 FDE" in api_calls[start[0]][1]["title"]


def test_bring_up_never_overwrites_a_manual_name(api_calls, fake_media, monkeypatch):
    """人工改过的名字是用户说了算：为了页面地址可以探页面，但绝不能回写 name。"""
    probed: list[str] = []

    def spy(url, timeout=8.0):
        probed.append(url)
        return {"nickname": "页面上的名字", "title": "页面上的标题"}

    monkeypatch.setattr(direct_capture, "room_meta", spy)
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    try:
        a.bring_up(_room(name="老张的第二场", name_source="user"))
    finally:
        a.shutdown()
    # 探测页面是为了拿 webRid；名字仍然必须是用户改的那个。
    assert probed
    assert not [b for p, b in api_calls
                if p == "/api/rooms/state" and b.get("name")]


def test_bring_up_still_starts_when_name_probe_breaks(api_calls, fake_media, monkeypatch):
    """取名字炸了也不能耽误录音：这一路照样要起来。"""
    def boom(*a, **kw):
        raise RuntimeError("解析炸了")

    monkeypatch.setattr(direct_capture, "room_meta", boom)
    a = direct_capture.DirectAgent("http://127.0.0.1:8765")
    try:
        a.bring_up(_room(name="", name_source="share"))
        assert a.running.get("rm1"), "名字取不到时这一路也必须正常启动"
    finally:
        a.shutdown()
    assert [b for p, b in api_calls
            if p == "/api/rooms/state" and b.get("state") == "live"]
