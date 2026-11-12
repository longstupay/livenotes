# -*- coding: utf-8 -*-
"""分享包：摘要/字幕/README/meta、zip、音频与妙记链接回填。"""
from __future__ import annotations

import zipfile

import pytest
from helpers import add_rows, add_session, utt_row

from app import audio, share, store


def test_package_text_only(db, isolated_paths, monkeypatch):
    monkeypatch.setattr(share, "SHARE_DIR", isolated_paths / "share")
    add_session(db, "S_TEST", title="测试场", duration_s=120)
    add_rows(db, "S_TEST", [utt_row(0, "第一句话"), utt_row(1, "第二句话")])

    info = share.build_package(db, "S_TEST", include_audio=False)
    names = set(info["files"])
    assert {"summary.md", "transcript.srt", "README.md", "meta.json"} <= names
    summary = (info["dir"] / "summary.md").read_text(encoding="utf-8")
    assert "第一句话" in summary
    srt = (info["dir"] / "transcript.srt").read_text(encoding="utf-8")
    assert "00:00:00,000" in srt
    readme = (info["dir"] / "README.md").read_text(encoding="utf-8")
    assert "妙记" in readme


def test_package_rejects_missing_session(db):
    with pytest.raises(ValueError):
        share.build_package(db, "NOPE", include_audio=False)


def test_zip_contains_files(db, isolated_paths, monkeypatch):
    monkeypatch.setattr(share, "SHARE_DIR", isolated_paths / "share")
    add_session(db, "S_TEST")
    add_rows(db, "S_TEST", [utt_row(0, "一句话")])
    info = share.build_package(db, "S_TEST", include_audio=False)
    z = share.zip_package(info["dir"])
    assert z.is_file()
    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
    assert {"summary.md", "transcript.srt", "README.md", "meta.json"} <= names


def test_minutes_url_roundtrip(db):
    add_session(db, "S_TEST")
    store.set_minutes_url(db, "S_TEST", "https://x.feishu.cn/minutes/abc")
    assert store.get_session(db, "S_TEST")["minutes_url"].endswith("/abc")
    store.set_minutes_url(db, "S_TEST", None)
    assert not store.get_session(db, "S_TEST")["minutes_url"]


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_package_includes_archived_audio(db, isolated_paths, monkeypatch):
    import math
    import struct
    import wave

    monkeypatch.setattr(share, "SHARE_DIR", isolated_paths / "share")
    wav = isolated_paths / "audio" / "S_TEST.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 16000)))
                               for i in range(16000)))
    add_session(db, "S_TEST", audio_path=str(wav), duration_s=1.0)
    info = share.build_package(db, "S_TEST")
    assert info["audio"] == "audio.m4a"
    assert (info["dir"] / "audio.m4a").is_file()
    # 转码后 session.audio_path 也应指向 m4a
    assert str(store.get_session(db, "S_TEST")["audio_path"]).endswith(".m4a")
