# -*- coding: utf-8 -*-
"""合成 PCM 端到端：推流 -> VAD 切句 -> ASR/声纹 -> 术语纠错 -> 聚类 -> SQLite/FTS -> WAV。

模型边界（VAD / ASR / CAM++）用确定性的假实现替换，其余全部走生产代码：
采集队列、工作线程、逐句落库、说话人标签、结束落盘。CI 不装 sherpa-onnx 也能跑。
"""
from __future__ import annotations

import sys
import types
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from app import glossary, live, store


SR = live.SAMPLE_RATE
SEG_S = 2.0
GAP_S = 0.8
F_A, F_B = 440.0, 880.0                      # 两个"人"的基频，仅用来造不同声纹
E_A = np.array([1.0, 0.0, 0.0], dtype="float32")
E_B = np.array([0.0, 1.0, 0.0], dtype="float32")
TEXT_A = "欧匹西 的 F D E 报价"
TEXT_B = "这个方案我同意"


def _pcm() -> np.ndarray:
    def tone(freq):
        n = int(SR * SEG_S)
        t = np.arange(n, dtype="float32") / SR
        return (0.5 * 32767 * np.sin(2 * np.pi * freq * t)).astype("<i2")

    gap = np.zeros(int(SR * GAP_S), dtype="<i2")
    return np.concatenate([tone(F_A), gap, tone(F_B), gap, tone(F_A), gap])


def _freq(samples) -> float:
    s = np.asarray(samples, dtype="float32")
    if s.size < 2:
        return 0.0
    crossings = int(np.count_nonzero(np.diff(np.signbit(s))))
    return crossings / 2.0 / (len(s) / SR)


class FakeVad:
    """按能量切句：连续响 = 一句，静音收尾。行为足够贴近 silero 的对外接口。"""

    def __init__(self, cfg, buffer_size_in_seconds=60):
        self.cfg = cfg
        self.pos = 0
        self.cur: list | None = None
        self.queue: list = []
        self._speech = False

    def accept_waveform(self, window):
        w = np.asarray(window, dtype="float32")
        start = self.pos
        self.pos += len(w)
        speech = float(np.sqrt((w * w).mean())) > 0.02
        if speech:
            if self.cur is None:
                self.cur = [start, []]
            self.cur[1].append(w.copy())
        elif self.cur is not None:
            self._close()
        self._speech = speech

    def _close(self):
        if self.cur is None:
            return
        start, parts = self.cur
        self.queue.append(SimpleNamespace(start=start,
                                          samples=np.concatenate(parts)))
        self.cur = None

    def flush(self):
        self._close()

    def is_speech_detected(self) -> bool:
        return self._speech

    def empty(self) -> bool:
        return not self.queue

    @property
    def front(self):
        return self.queue[0]

    def pop(self):
        self.queue.pop(0)


class FakeVadConfig:
    def __init__(self):
        self.silero_vad = SimpleNamespace()


def _fake_transcribe(samples) -> str:
    # 真实 Engine.transcribe 的出口会做术语纠错；假实现保持同一契约。
    return live._clean(TEXT_A if _freq(samples) < 650 else TEXT_B)


def _fake_embed(samples, extractor=None):
    return E_A if _freq(samples) < 650 else E_B


class _FakeEmbedder:
    """占位声纹提取器：真 CAM++ 由 ENGINE 懒加载并跨 worker 共享。"""


@pytest.fixture
def fake_engine(monkeypatch):
    fake_mod = types.ModuleType("sherpa_onnx")
    fake_mod.VadModelConfig = FakeVadConfig
    fake_mod.VoiceActivityDetector = FakeVad
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_mod)
    monkeypatch.setattr(live.ENGINE, "load", lambda: None)
    monkeypatch.setattr(live.ENGINE, "transcribe", _fake_transcribe)
    monkeypatch.setattr(live.ENGINE, "embed", _fake_embed)
    # 工作池现在共享 ENGINE._spk，不再按 worker 创建提取器；保留 new_embedder
    # 的假实现，兼容其他可能调用它的路径。embed 已被替换成 _fake_embed。
    monkeypatch.setattr(live.ENGINE, "new_embedder", lambda: _FakeEmbedder())
    # 工作池是进程级单例：上一个用例可能已经用别的引擎起过 worker，
    # 先收掉，让本用例的 worker 在替换后的 new_embedder 下重建。
    live.POOL.shutdown()
    # 收尾的重聚类/总结/飞书回顾另测；这里只验证采集到落盘的链路本身。
    monkeypatch.setattr(live.LiveSession, "_auto_finalize", lambda self: None)
    monkeypatch.setattr(live.LiveSession, "_maybe_push", lambda self, row: None)


def _feed(session, pcm: np.ndarray, chunk: int = live.CHUNK_SAMPLES):
    for i in range(0, len(pcm), chunk):
        session.feed(pcm[i:i + chunk].tobytes())


def test_synthetic_pcm_runs_through_the_whole_pipeline(db, fake_engine):
    glossary.save([{"term": "OPC", "aliases": ["欧匹西"]},
                   {"term": "FDE", "aliases": []}])
    pcm = _pcm()

    s = live.LiveSession("合成音频测试")
    s._hint_at = float("inf")       # 跳过定时认人，专测采集链路
    s.start()
    try:
        _feed(s, pcm)
    finally:
        st = s.stop()

    # ---- 会话与 WAV
    assert st["error"] is None and st["running"] is False
    assert st["n_utterances"] == 3
    assert st["elapsed"] == pytest.approx(len(pcm) / SR, abs=0.01)
    assert st["recognized_s"] == pytest.approx(3 * SEG_S, abs=0.1)
    assert s.audio_path is not None and s.audio_path.exists()
    with wave.open(str(s.audio_path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SR)
        assert w.getnframes() == len(pcm)

    row = db.execute("SELECT * FROM sessions WHERE id=?", (s.id,)).fetchone()
    assert row["audio_path"] == str(s.audio_path)
    assert row["duration_s"] == pytest.approx(len(pcm) / SR, abs=0.01)
    assert row["untranscribed_seconds"] == pytest.approx(
        len(pcm) / SR - s.recognized_s, abs=0.01)
    assert row["untranscribed_seconds"] == pytest.approx(3 * GAP_S, abs=0.5)
    assert row["started_utc"] and row["tz_offset_min"] is not None

    # ---- 逐句落库：两个说话人，同一人回到同一个簇
    rows = store.query_utterances(db, {"session_id": s.id})
    assert [r["text"] for r in rows] == [TEXT_A.replace("欧匹西", "OPC").replace("F D E", "FDE"),
                                         TEXT_B,
                                         TEXT_A.replace("欧匹西", "OPC").replace("F D E", "FDE")]
    assert [r["speaker_id"] for r in rows] == ["SPK_00", "SPK_01", "SPK_00"]
    assert [round(r["t_start"], 1) for r in rows] == [0.0, 2.8, 5.6]
    assert {r["speaker_label"] for r in rows if r["speaker_id"] == "SPK_00"} == {"主播"}
    assert {r["speaker_label"] for r in rows if r["speaker_id"] == "SPK_01"} == {"嘉宾"}
    assert store.count_embeddings(db, s.id) == 3
    assert store.count_utterances(db, {"session_id": s.id}) == 3

    # ---- 实时状态与 FTS 检索
    speakers = {x["speaker_id"]: x for x in st["speakers"]}
    assert set(speakers) == {"SPK_00", "SPK_01"}
    assert speakers["SPK_00"]["count"] == 2
    assert speakers["SPK_00"]["seconds"] == pytest.approx(4.0, abs=0.1)
    assert speakers["SPK_01"]["label"] == "嘉宾"
    if store.FTS_TRIGRAM:
        assert [r["speaker_id"] for r in
                store.query_utterances(db, {"session_id": s.id, "q": "FDE"})] == [
                    "SPK_00", "SPK_00"]
    assert [r["text"] for r in
            store.query_utterances(db, {"session_id": s.id, "q": "方案"})] == [TEXT_B]

    transcript = store.session_transcript(db, s.id)
    assert [(r["speaker_label"], r["text"]) for r in transcript] == [
        ("主播", rows[0]["text"]), ("嘉宾", TEXT_B), ("主播", rows[2]["text"])]


def test_feed_rejects_audio_after_stop(db, fake_engine):
    s = live.LiveSession("结束之后")
    s._hint_at = float("inf")
    s.start()
    s.stop()
    with pytest.raises(live.LiveError):
        s.feed(b"\x00\x00")


def test_empty_session_finalizes_without_audio(db, fake_engine):
    """一句有效话都没切出来：也要正常落盘、结束，不能卡住或报错。"""
    s = live.LiveSession("没人说话")
    s._hint_at = float("inf")
    s.start()
    try:
        s.feed(np.zeros(SR * 2, dtype="<i2").tobytes())
    finally:
        st = s.stop()
    assert st["error"] is None and st["n_utterances"] == 0
    assert s.audio_path is not None and s.audio_path.exists()
    assert store.count_utterances(db, {"session_id": s.id}) == 0
