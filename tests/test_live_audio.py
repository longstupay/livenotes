# -*- coding: utf-8 -*-
"""采集中回听：spool 窗口切片、WAV 头、Range、结束后 WAV 兜底。

这里不启动 VAD/ASR，只造一个「正在写入的 PCM 文件」和内存会话，
把播放器真正会走的 HTTP 路径测一遍。
"""
from __future__ import annotations

import json
import struct
import threading
import urllib.error
import urllib.request
import wave
from urllib.parse import urlencode

import pytest

from app import live, server
from tests import helpers


def _fake_session(sid: str = "LIVE_T", seconds: float = 2.0, *,
                  paused: bool = False, running: bool = True) -> live.LiveSession:
    s = live.LiveSession("测试", source_mode="tab")
    s.id = sid
    s.spool = live.AUDIO_DIR / (sid + ".pcm")
    s.spool.parent.mkdir(parents=True, exist_ok=True)
    s.spool.write_bytes(b"\x01\x00" * int(16000 * seconds))
    s.paused = paused
    s.running = running
    live._SESSIONS[sid] = s
    return s


@pytest.fixture
def http_base(db):
    srv = server.make_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        srv.shutdown()
        srv.server_close()
        live.stop_watchdog()


def _get(url: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_audio_window_clamps_and_aligns():
    _fake_session(seconds=5.0)
    w = live.audio_window("LIVE_T", seconds=10)
    assert w["ok"]
    assert w["start_s"] == 0.0
    assert w["end_s"] == 5.0
    assert w["nbytes"] == 16000 * 2 * 5


def test_audio_window_tail_and_absolute():
    _fake_session(seconds=10.0)
    tail = live.audio_window("LIVE_T", seconds=3)
    assert tail["start_s"] == pytest.approx(7.0)
    assert tail["end_s"] == pytest.approx(10.0)
    assert tail["nbytes"] == 3 * 32000

    exact = live.audio_window("LIVE_T", start_s=2, end_s=4)
    assert exact["start_s"] == 2.0
    assert exact["end_s"] == 4.0
    assert exact["nbytes"] == 2 * 32000

    clamped = live.audio_window("LIVE_T", start_s=9, end_s=100)
    assert clamped["start_s"] == 9.0
    assert clamped["end_s"] == 10.0


def test_audio_window_reasons():
    assert live.audio_window("NOPE")["reason"] == "not_active"
    _fake_session(seconds=0)
    assert live.audio_window("LIVE_T")["reason"] == "no_audio"
    live._SESSIONS["LIVE_T"].spool.write_bytes(b"\x00\x00" * 16000)
    assert live.audio_window("LIVE_T", start_s=0, end_s=0)["reason"] == "too_short"


def test_evidence_link_switches_between_live_and_reader():
    from app import livebrief

    s = _fake_session(seconds=1, running=True)
    row = {"id": 1, "t_start": 12.0, "speaker_label": "主播", "text": "一句原话"}
    assert live._reader_link("LIVE_T", 12).startswith(live.BASE_URL + "/live?")
    assert livebrief._evidence(row, "LIVE_T")["reader_url"].startswith(live.BASE_URL + "/live?")

    s.running = False
    assert live._reader_link("LIVE_T", 12).startswith(live.BASE_URL + "/reader?")
    # 收尾回顾在这个窗口里发送：会话还在内存，但必须回 reader。
    assert livebrief._evidence(row, "LIVE_T")["reader_url"].startswith(live.BASE_URL + "/reader?")
    assert not live.is_active("NOPE")


def test_live_audio_http_slice_and_range(http_base):
    _fake_session(seconds=2.0)
    base = http_base

    q = urlencode({"session_id": "LIVE_T", "seconds": 1})
    code, _, body = _get(base + "/api/live/audio/meta?" + q)
    assert code == 200
    meta = json.loads(body)
    assert meta["available"] and meta["live"]
    assert meta["start_s"] == pytest.approx(1.0, abs=0.01)
    assert meta["end_s"] == pytest.approx(2.0, abs=0.01)

    q = urlencode({"session_id": "LIVE_T", "start": meta["start_s"],
                   "end": meta["end_s"]})
    url = base + "/api/live/audio?" + q
    code, hdr, body = _get(url)
    assert code == 200
    assert hdr["Content-Type"].startswith("audio/wav")
    assert len(body) == 44 + 32000
    assert body[:4] == b"RIFF"
    assert struct.unpack("<I", body[40:44])[0] == 32000

    code, hdr, body = _get(url, {"Range": "bytes=0-43"})
    assert code == 206
    assert hdr["Content-Range"] == "bytes 0-43/%d" % (44 + 32000)
    assert len(body) == 44
    assert body[:4] == b"RIFF"

    code, hdr, body = _get(url, {"Range": "bytes=44-"})
    assert code == 206
    assert hdr["Content-Range"] == "bytes 44-%d/%d" % (44 + 32000 - 1, 44 + 32000)
    assert len(body) == 32000


def test_live_audio_no_audio_reports_clear_error(http_base):
    _fake_session(seconds=0)
    q = urlencode({"session_id": "LIVE_T", "seconds": 5})
    code, _, body = _get(http_base + "/api/live/audio/meta?" + q)
    assert code == 200
    meta = json.loads(body)
    assert not meta["available"] and meta["live"]
    assert meta["reason"] == "no_audio"

    code, _, body = _get(http_base + "/api/live/audio?" + q)
    assert code == 404
    assert "还没有收到音频" in json.loads(body)["error"]


def test_live_audio_falls_back_to_final_wav(http_base, db):
    p = live.AUDIO_DIR / "LIVE_END.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 32000)      # 2 秒
    helpers.add_session(db, "LIVE_END", audio_path=str(p), duration_s=2.0)

    q = urlencode({"session_id": "LIVE_END", "seconds": 5})
    code, _, body = _get(http_base + "/api/live/audio/meta?" + q)
    assert code == 200
    meta = json.loads(body)
    assert meta["available"] and not meta["live"]
    assert meta["elapsed_s"] == pytest.approx(2.0)

    q = urlencode({"session_id": "LIVE_END", "start": 1.0, "end": 2.0})
    code, hdr, body = _get(http_base + "/api/live/audio?" + q)
    assert code == 200
    assert len(body) == 44 + 32000
    assert body[:4] == b"RIFF"


def test_live_audio_falls_back_to_convention_path(http_base, db):
    """刚收尾完、数据库还没更新时，按 AUDIO_DIR/<sid>.wav 兜底。"""
    p = live.AUDIO_DIR / "LIVE_RACE.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)      # 1 秒
    helpers.add_session(db, "LIVE_RACE", audio_path=None, duration_s=1.0)

    q = urlencode({"session_id": "LIVE_RACE", "seconds": 1})
    code, _, body = _get(http_base + "/api/live/audio/meta?" + q)
    assert code == 200
    assert json.loads(body)["available"]
