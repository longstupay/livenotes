# -*- coding: utf-8 -*-
"""录音归档：WAV -> m4a、保留期清理、PCM 切片与内容类型。"""
from __future__ import annotations

import math
import struct
import time
import wave
from pathlib import Path

import pytest
from helpers import add_session

from app import audio, store


def _write_wav(path: Path, seconds: float = 1.0, freq: float = 440.0) -> None:
    n = int(seconds * 16000)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / 16000)))
            for i in range(n)))


def test_content_type_by_suffix():
    assert audio.content_type("x.wav") == "audio/wav"
    assert audio.content_type("x.m4a") == "audio/mp4"
    assert audio.content_type("x.opus") == "audio/ogg"
    assert audio.content_type("x.bin") == "application/octet-stream"


def test_slice_pcm_is_sample_accurate(isolated_paths):
    pcm = isolated_paths / "audio" / "S_TEST.pcm"
    # 2 秒：前半 1000、后半 -1000，便于检查切点
    samples = [1000] * 16000 + [-1000] * 16000
    pcm.write_bytes(b"".join(struct.pack("<h", x) for x in samples))
    out = isolated_paths / "slice.wav"
    assert audio._slice_pcm(pcm, out, 0.5, 1.0)
    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getnframes() == 16000
        first = struct.unpack("<h", w.readframes(1))[0]
    assert first == 1000


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_transcode_updates_session_and_keeps_original(db, isolated_paths):
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 1.0)
    add_session(db, "S_TEST", audio_path=str(wav))

    out = audio.transcode_session("S_TEST", wav, conn=db)
    assert out and out.suffix == ".m4a" and out.stat().st_size > 0

    s = store.get_session(db, "S_TEST")
    assert s["audio_path"] == str(out)
    assert s["audio_format"] == "m4a"
    assert s["audio_original_path"] == str(wav)
    assert int(s["audio_bytes"]) == out.stat().st_size
    assert wav.is_file()          # 保留期内不删原文件


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_cleanup_originals_only_after_archive(db, isolated_paths):
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 1.0)
    add_session(db, "S_TEST", audio_path=str(wav))
    out = audio.transcode_session("S_TEST", wav, conn=db)
    assert out

    # days=-1 表示永久保留
    assert audio.cleanup_originals(db, days=-1) == 0
    assert wav.is_file()

    # days=0 且归档可播：删原 WAV，但不清 audio_path
    assert audio.cleanup_originals(db, days=0) == 1
    assert not wav.exists()
    s = store.get_session(db, "S_TEST")
    assert s["audio_path"] == str(out)
    assert s["audio_original_path"] is None


def test_cleanup_originals_keeps_when_archive_missing(db, isolated_paths):
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 0.5)
    add_session(db, "S_TEST", audio_path=str(wav))
    store.set_audio_meta(db, "S_TEST", audio_format="wav",
                         audio_original_path=str(wav))
    # audio_path 仍指向 WAV，说明还没转码，不能删
    assert audio.cleanup_originals(db, days=0) == 0
    assert wav.is_file()


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_slice_m4a_returns_wav_for_reader(db, isolated_paths):
    """归档成 m4a 后，reader 的 t_start/t_end 仍要拿到能直接播的 WAV。"""
    import io

    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 2.0)
    add_session(db, "S_TEST", audio_path=str(wav))
    m4a = audio.transcode_session("S_TEST", wav, conn=db)
    assert m4a

    data = audio.slice_wav_bytes(m4a, 0.5, 1.0)
    assert data and data[:4] == b"RIFF"
    with wave.open(io.BytesIO(data), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        # AAC 帧边界有几十毫秒误差，不要求精确到采样点
        assert 0.5 * 16000 * 0.8 <= w.getnframes() <= 0.5 * 16000 * 1.2


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_extract_opus_from_pcm(isolated_paths):
    pcm = isolated_paths / "audio" / "S_TEST.pcm"
    pcm.write_bytes(b"\x00\x00" * 16000 * 3)
    out = isolated_paths / "clip.opus"
    ok, err = audio.extract_opus(pcm, out, 0.5, 1.0)
    assert ok, err
    assert out.stat().st_size > 0
    assert audio.content_type(out) == "audio/ogg"
