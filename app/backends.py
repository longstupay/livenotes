# -*- coding: utf-8 -*-
"""采集 / 说话人日志 / ASR 的可插拔适配器。

【诚实声明】本文件分清两类代码，请勿混淆：
  A. 已可直接运行：LoopbackCapture（系统回环录音）、FileCapture、EnergyVAD、
     重叠启发式、per_uid 模式下绕过日志的 NullDiarizer。仅依赖标准库 + 可选声卡库。
  B. 接口与调用姿势已给出、但需另装模型权重/环境才能跑：FunASRDiarizer、
     PyannoteDiarizer、FunASRASR、FasterWhisperASR。本次环境未实跑模型，
     因此默认演示走 MockASR/演示数据，绝不假装"已分离 8~10 人"。

架构上最关键的一点：
    观众端拿到的只有平台下混后的一路音频。source separation（把混音拆成 N 路
    干净人声）对 8~10 人同时发言没有可工程化的解；能做的是 diarization
    （"哪段时间谁在说"），且仅在近似轮流发言时有效。故 Pipeline 对重叠段
    采取「标记 + 不转写 + 不计入总结」策略，宁可留白也不编造。
"""
from __future__ import annotations

import abc
import array
import json
import math
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .config import SAMPLE_RATE, SEGMENT_SECONDS


@dataclass
class Segment:
    """一段待转写音频。speaker_id 非空 = 已确定身份（分轨模式）。"""
    wav_path: str
    t_start: float
    t_end: float
    speaker_id: str | None = None
    overlap: bool = False
    asr_conf_hint: float = 1.0
    meta: dict = field(default_factory=dict)


# ------------------------------------------------------------------ 采集层

class CaptureBackend(abc.ABC):
    @abc.abstractmethod
    def record(self, session_id: str, out_dir: Path,
               duration_s: float, on_segment: Callable[[Segment], None] | None = None) -> list[Segment]:
        """录制并切分为 60s 分片，返回相对整场起点的 Segment 列表。"""


def _to_mono_16k(pcm: bytes, channels: int, src_rate: int, dst_rate: int = SAMPLE_RATE) -> bytes:
    """int16 交错 PCM -> 单声道 16k。Python 3.13 已移除 audioop，这里手写线性插值。"""
    a = array.array("h")
    a.frombytes(pcm)
    if a.byteorder != ("little" if __import__("sys").byteorder == "little" else "big"):
        a.byteswap()
    mono = array.array("h")
    if channels > 1:
        for i in range(0, len(a) - channels + 1, channels):
            mono.append(int(sum(a[i:i + channels]) / channels))
    else:
        mono = a
    if src_rate == dst_rate:
        return mono.tobytes()
    step = src_rate / dst_rate
    out = array.array("h")
    n = len(mono)
    j = 0.0
    while int(j) + 1 < n:
        i = int(j)
        frac = j - i
        out.append(int(mono[i] * (1 - frac) + mono[i + 1] * frac))
        j += step
    return out.tobytes()


def _write_wav(path: Path, pcm: bytes, rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


class LoopbackCapture(CaptureBackend):
    """录制「本机正在播放的声音」，用于内部会议 / 公司自有直播间的本地记录。

    Windows : pip install pyaudiowpatch   （WASAPI loopback 设备）
    macOS   : pip install sounddevice；装 BlackHole 虚拟声卡并把系统输出设为它
    Linux   : pip install sounddevice；选 PulseAudio 的 xxx.monitor 输入
    """

    def __init__(self, device_hint: str = "", target_rate: int = SAMPLE_RATE):
        self.device_hint = device_hint
        self.target_rate = target_rate

    def record(self, session_id: str, out_dir: Path, duration_s: float,
               on_segment: Callable[[Segment], None] | None = None) -> list[Segment]:
        import sys
        out_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            return self._record_wasapi(session_id, out_dir, duration_s, on_segment)
        return self._record_sounddevice(session_id, out_dir, duration_s, on_segment)

    def _record_wasapi(self, sid: str, out: Path, duration_s: float,
                       on_segment: Callable[[Segment], None] | None) -> list[Segment]:
        try:
            import pyaudiowpatch as pa  # type: ignore
        except ImportError as e:
            raise RuntimeError("Windows 回环采集需要 pyaudiowpatch：pip install pyaudiowpatch") from e
        p = pa.PyAudio()
        segments: list[Segment] = []
        try:
            info = p.get_host_api_info_by_type(pa.paWASAPI)
            dev = p.get_device_info_by_index(info["defaultOutputDevice"])
            if not dev.get("isLoopbackDevice"):
                dev = next((d for d in p.get_loopback_device_info_generator()
                            if dev["name"].split("(")[0].strip() in d["name"]), None)
            if dev is None:
                raise RuntimeError("未找到 WASAPI loopback 设备")
            rate = int(dev["defaultSampleRate"])
            channels = max(1, int(dev["maxInputChannels"]))
            chunk = int(rate * 0.2)
            stream = p.open(format=pa.paInt16, channels=channels, rate=rate,
                            input=True, input_device_index=dev["index"],
                            frames_per_buffer=chunk)
            t0 = time.time()
            buf = bytearray()
            seg_start = 0.0
            idx = 0
            with stream:
                while time.time() - t0 < duration_s:
                    raw = stream.read(chunk, exception_on_overflow=False)
                    buf += _to_mono_16k(raw, channels, rate, self.target_rate)
                    elapsed = time.time() - t0
                    if elapsed - seg_start >= SEGMENT_SECONDS:
                        fp = out / f"{sid}_{idx:05d}.wav"
                        _write_wav(fp, bytes(buf), self.target_rate)
                        seg = Segment(str(fp), seg_start, elapsed, meta={"device": dev["name"]})
                        segments.append(seg)
                        if on_segment:
                            on_segment(seg)
                        buf.clear(); seg_start = elapsed; idx += 1
            if buf:
                fp = out / f"{sid}_{idx:05d}.wav"
                _write_wav(fp, bytes(buf), self.target_rate)
                segments.append(Segment(str(fp), seg_start, time.time() - t0))
            (out / f"{sid}_manifest.json").write_text(
                json.dumps({"session_id": sid, "rate": self.target_rate,
                            "segments": [s.__dict__ for s in segments]}, ensure_ascii=False),
                encoding="utf-8")
            return segments
        finally:
            p.terminate()

    def _record_sounddevice(self, sid: str, out: Path, duration_s: float,
                            on_segment: Callable[[Segment], None] | None) -> list[Segment]:
        try:
            import sounddevice as sd  # type: ignore
        except ImportError as e:
            raise RuntimeError("macOS/Linux 回环采集需要 sounddevice + 虚拟声卡/monitor 源") from e
        dev = self.device_hint or None
        if dev is None:
            for i, d in enumerate(sd.query_devices()):
                if any(k in d["name"].lower() for k in ("blackhole", "monitor", "loopback")):
                    dev, self.device_hint = i, i
                    break
        rate = self.target_rate
        segs: list[Segment] = []
        n_blocks = int(duration_s / SEGMENT_SECONDS) + 1
        for idx in range(n_blocks):
            left = min(SEGMENT_SECONDS, duration_s - idx * SEGMENT_SECONDS)
            if left <= 0:
                break
            data = sd.rec(int(left * rate), samplerate=rate, channels=1,
                          dtype="int16", device=dev)
            sd.wait()
            fp = out / f"{sid}_{idx:05d}.wav"
            _write_wav(fp, data.tobytes(), rate)
            s = Segment(str(fp), idx * SEGMENT_SECONDS, idx * SEGMENT_SECONDS + left)
            segs.append(s)
            if on_segment:
                on_segment(s)
        return segs


class FileCapture(CaptureBackend):
    """离线导入已有的整场 WAV（含主播端多轨导出）。用于回放与批量重跑。"""

    def __init__(self, path: Path, speaker_id: str | None = None):
        self.path, self.speaker_id = Path(path), speaker_id

    def record(self, session_id: str, out_dir: Path, duration_s: float,
               on_segment: Callable[[Segment], None] | None = None) -> list[Segment]:
        with wave.open(str(self.path), "rb") as w:
            rate, frames, ch = w.getframerate(), w.getnframes(), w.getnchannels()
            total = frames / rate
            raw = w.readframes(frames)
        pcm = _to_mono_16k(raw, ch, rate)
        out_dir.mkdir(parents=True, exist_ok=True)
        bps = SAMPLE_RATE * 2
        segs: list[Segment] = []
        for i, off in enumerate(range(0, len(pcm), bps * SEGMENT_SECONDS)):
            fp = out_dir / f"{session_id}_{i:05d}.wav"
            _write_wav(fp, pcm[off:off + bps * SEGMENT_SECONDS])
            s = Segment(str(fp), i * SEGMENT_SECONDS,
                        min((i + 1) * SEGMENT_SECONDS, total), speaker_id=self.speaker_id)
            segs.append(s)
        return segs


# ------------------------------------------------------- VAD / 重叠启发式

def energy_vad(wav_path: str | Path, frame_ms: int = 20,
               floor_ratio: float = 0.18) -> list[tuple[float, float, float]]:
    """标准库实现的能量 + 过零率 VAD。返回 [(t, rms, zcr), ...]（16k 单声道 WAV）。"""
    with wave.open(str(wav_path), "rb") as w:
        rate, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    pcm = _to_mono_16k(raw, ch, rate)
    a = array.array("h"); a.frombytes(pcm)
    n = max(1, int(rate * frame_ms / 1000))
    out: list[tuple[float, float, float]] = []
    for i in range(0, len(a) - n + 1, n):
        fr = a[i:i + n]
        rms = math.sqrt(sum(x * x for x in fr) / n)
        zc = sum(1 for k in range(1, n) if (fr[k - 1] < 0) != (fr[k] < 0)) / n
        out.append((i / rate, rms, zc))
    return out


def suspect_overlap(wav_path: str | Path, window_s: float = 2.0) -> list[tuple[float, float]]:
    """启发式：窗口内「高能量 + 过零率离散度大」疑似多人抢话。

    这不是真正的重叠检测，只是给人工复核/降权提供线索；真实系统应直接用
    diarizer 输出的多说话人同时激活区间，或用专门的 overlap detection 模型。
    """
    frames = energy_vad(wav_path)
    if not frames:
        return []
    peak = max(f[1] for f in frames) or 1.0
    n = max(1, int(window_s / 0.02))
    hits: list[tuple[float, float]] = []
    for i in range(0, len(frames), n):
        win = frames[i:i + n]
        if not win:
            break
        rms = [f[1] for f in win]; zc = [f[2] for f in win]
        mean_z = sum(zc) / len(zc)
        var_z = sum((x - mean_z) ** 2 for x in zc) / len(zc)
        loud = sum(1 for x in rms if x > peak * 0.25) / len(rms)
        if loud > 0.65 and var_z > 0.004 and mean_z > 0.06:
            hits.append((win[0][0], win[-1][0] + 0.02))
    return hits


# ------------------------------------------------------------ 说话人日志

class Diarizer(abc.ABC):
    @abc.abstractmethod
    def run(self, segments: Iterable[Segment]) -> list[Segment]:
        """为每片产出带 speaker_id / overlap 的细粒度片段。"""


class NullDiarizer(Diarizer):
    """分轨模式：uid 已确定身份，直接跳过日志，这是唯一能保证 8~10 人准确的路线。"""

    def run(self, segments: Iterable[Segment]) -> list[Segment]:
        return [s for s in segments if s.speaker_id]


class FunASRDiarizer(Diarizer):
    """3D-Speaker / FunASR CAM++ 说话人日志（中文场景首选，无需 HF token）。

    安装：pip install funasr modelscope
    参考调用：
        from funasr import AutoModel
        m = AutoModel(model="iic/speech_campplus_speaker-diarization_common",
                      model_revision="v1.0.0")
        res = m.generate(input=wav_path)   # -> [{"start":..,"end":..,"spk":0}]
    跨分片必须复用同一 speaker embedding 中心，否则同一人会被拆成多个标签。
    """

    def __init__(self, model: str = "iic/speech_campplus_speaker-diarization_common"):
        self.model_name = model
        self._m = None

    def _load(self):
        if self._m is None:
            from funasr import AutoModel  # type: ignore
            self._m = AutoModel(model=self.model_name)
        return self._m

    def run(self, segments: Iterable[Segment]) -> list[Segment]:
        raise NotImplementedError(
            "需安装 funasr 并接入跨分片声纹中心后启用；"
            "混音模式下请勿把聚类标签当成真实身份（详见 docs/01）")


class PyannoteDiarizer(Diarizer):
    """pyannote.audio 3.1（需 HuggingFace token 并接受模型条款）。"""

    def __init__(self, token: str = "", model: str = "pyannote/speaker-diarization-3.1"):
        self.token, self.model_name, self._p = token, model, None

    def run(self, segments: Iterable[Segment]) -> list[Segment]:
        raise NotImplementedError("需配置 HF token 与 GPU 环境后启用")


# ------------------------------------------------------------------- ASR

class ASRBackend(abc.ABC):
    @abc.abstractmethod
    def transcribe(self, seg: Segment) -> tuple[str, float]:
        """返回 (文本, 置信度)。重叠段应直接返回 ('', 0.0)，不要硬猜。"""


class MockASR(ASRBackend):
    def transcribe(self, seg: Segment) -> tuple[str, float]:
        return ("", 0.0) if seg.overlap else ("[待接入 ASR]", 0.0)


class FunASRASR(ASRBackend):
    """Paraformer-zh + VAD + 标点 + 字级时间戳，中文直播口语效果优于 Whisper。

    安装：pip install funasr modelscope
        m = AutoModel(model="paraformer-zh", vad_model="fsmn-vad",
                      punc_model="ct-punc", spk_model="cam++",
                      hotword="跳槽 Offer 连麦 小黄车")   # 热词很关键
    """

    def __init__(self, hotwords: str = ""):
        self.hotwords, self._m = hotwords, None

    def transcribe(self, seg: Segment) -> tuple[str, float]:
        if seg.overlap:
            return "", 0.0
        raise NotImplementedError("需安装 funasr 后启用；建议配领域热词表")


class FasterWhisperASR(ASRBackend):
    """备选：faster-whisper large-v3。注意 Whisper 在长静音段存在幻觉，
    必须先用 VAD 切分，且不能跳过重叠段的标记。"""

    def __init__(self, model_size: str = "large-v3", language: str = "zh"):
        self.model_size, self.language, self._m = model_size, language, None

    def transcribe(self, seg: Segment) -> tuple[str, float]:
        if seg.overlap:
            return "", 0.0
        raise NotImplementedError("需安装 faster-whisper 后启用")


# --------------------------------------------------------------- 组装

def build_pipeline(source_mode: str):
    """按输入条件选择路线。这一行决定了整个系统能达到的质量上限。"""
    if source_mode == "per_uid":
        return {"diarizer": NullDiarizer(), "asr": FunASRASR(),
                "note": "分轨：身份由 uid 确定，无需分离，无重叠问题"}
    if source_mode == "mixed":
        return {"diarizer": FunASRDiarizer(), "asr": FunASRASR(),
                "note": "混音兜底：聚类标签 + 重叠段剔除 + 人工校正，不可当会议记录级精度"}
    return {"diarizer": NullDiarizer(), "asr": MockASR(), "note": "演示数据"}
