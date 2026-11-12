# -*- coding: utf-8 -*-
"""片段分享：参数解析、PCM 切片、缓存与限流。"""
from __future__ import annotations

import struct

import pytest
from helpers import add_session

from app import audio, clips


@pytest.mark.parametrize("text,start,duration", [
    ("CH01 90秒", None, 90),
    ("CH01 2分钟", None, 120),
    ("CH01 1:30", 90, 90),
    ("CH01 1:30 2分钟", 90, 120),
    ("CH01 1分30秒", None, 90),
    ("CH01", None, 90),
    ("90秒", None, 90),
])
def test_parse_share_args(text, start, duration):
    out = clips.parse_share_args("/分享 " + text)
    assert out["start"] == start
    assert out["duration"] == duration
    assert out["error"] == ""


def test_parse_share_args_clamps():
    out = clips.parse_share_args("/分享 CH01 99分钟")
    assert out["duration"] == clips.MAX_SECONDS
    out = clips.parse_share_args("/分享 CH01 1秒")
    assert out["duration"] == clips.MIN_SECONDS


def test_parse_seconds_units():
    assert clips.parse_seconds("90秒") == 90
    assert clips.parse_seconds("1分30秒") == 90
    assert clips.parse_seconds("3分钟") == 180
    assert clips.parse_seconds("1:30") == 90
    assert clips.parse_seconds("00:01:30") == 90
    assert clips.parse_seconds("1.5分钟") == 90
    assert clips.parse_seconds("乱写") is None


def test_source_prefers_spool(db, isolated_paths):
    pcm = isolated_paths / "audio" / "S_TEST.pcm"
    pcm.write_bytes(b"\x00\x00" * 16000 * 2)
    add_session(db, "S_TEST", duration_s=2.0)
    src, total, kind = clips.source_for("S_TEST", conn=db)
    assert src == pcm and kind == "pcm"
    assert abs(total - 2.0) < 0.01


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_make_clip_from_spool_and_cache(db, isolated_paths):
    pcm = isolated_paths / "audio" / "S_TEST.pcm"
    pcm.write_bytes(struct.pack("<h", 1000) * 16000 * 3)
    add_session(db, "S_TEST", duration_s=3.0)
    clip, err = clips.make_clip("S_TEST", 0.5, 1.0, conn=db)
    assert clip and not err
    assert clip.path.is_file() and clip.path.suffix == ".opus"
    assert 0.9 <= clip.duration <= 1.1
    same, _ = clips.make_clip("S_TEST", 0.5, 1.0, conn=db)
    assert same.path == clip.path


def test_rate_limit():
    clips.reset_rate_limit()
    ok, wait = clips.allow("S_TEST", cooldown_s=30)
    assert ok and wait == 0
    ok, wait = clips.allow("S_TEST", cooldown_s=30)
    assert not ok and wait > 0
    clips.reset_rate_limit()
    assert clips.allow("S_TEST", cooldown_s=30)[0]


def test_release_refunds_rate_limit():
    clips.reset_rate_limit()
    assert clips.allow("S_TEST", cooldown_s=30)[0]
    assert not clips.allow("S_TEST", cooldown_s=30)[0]
    clips.release("S_TEST")
    assert clips.allow("S_TEST", cooldown_s=30)[0]


def test_make_clip_without_start_uses_latest(db, isolated_paths, monkeypatch):
    """「最近90秒」必须从末尾往前推，不能从 0:00 开始。"""
    pcm = isolated_paths / "audio" / "S_TEST.pcm"
    pcm.write_bytes(b"\x00\x00" * 16000 * 3)
    add_session(db, "S_TEST", duration_s=3.0)
    seen = {}

    def fake_extract(src, dst, start, duration):
        seen.update(start=start, duration=duration)
        return True, ""

    monkeypatch.setattr(clips.audio, "extract_opus", fake_extract)
    clip, err = clips.make_clip("S_TEST", None, 1.0, conn=db)
    assert clip and not err
    assert abs(clip.start - 2.0) < 0.01
    assert seen["start"] == 2.0
