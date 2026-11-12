# -*- coding: utf-8 -*-
"""标签页实时监听：浏览器回传 16k PCM -> VAD -> ASR -> 声纹聚类 -> SQLite。

采集与识别是解耦的：本模块的会话线程只做 VAD/断句并把定稿、临时字幕丢进
``app/asr_pool.py`` 的共享工作池；解码、声纹提取和落库都在工作池里完成，
多路会话之间按「定稿优先 + 跨会话轮转」调度，不再抢一把全局解码锁。

为什么不用 WebSocket：服务端是 stdlib http.server，不想为此引入框架。
浏览器每 256ms 用 fetch 发一块裸 PCM（application/octet-stream），
HTTP/1.1 的短连接在 localhost 上开销可以忽略，线程模型也简单。

为什么用标签页捕获：getDisplayMedia 只拿被选中标签页的音频，
微信提示音、其他页面都不会混进来，也不需要装虚拟声卡。

模型只在 /api/live/start 时才加载；没装 sherpa-onnx/numpy 时
其他功能（演示数据、回放、总结）完全不受影响。
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import traceback
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import analysis
from . import audio as _audio
from . import glossary
from . import logs
from . import metrics
from . import scheduler
from . import store
from . import tasks
from . import timefmt
from . import recluster as _recluster
from .asr_pool import AsrPool
from .config import (
    AUDIO_DIR,
    BASE_URL,
    DATA_DIR,
    LIVE_ASR_LANGUAGE,
    LIVE_FORCE_SEGMENT_S,
    LIVE_HINT_AUTO_S,
    LIVE_HINT_CONFIRM,
    LIVE_HINT_EVERY_S,
    LIVE_HINT_MIN_S,
    LIVE_MAX_SEGMENT_S,
    LIVE_MAX_SPEAKERS,
    LIVE_MIN_SPEECH_S,
    LIVE_PARTIAL_INTERVAL_S,
    LIVE_PARTIAL_MAX_S,
    LIVE_PARTIAL_MIN_S,
    LIVE_STALL_IDLE_S,
    LIVE_STALL_MIN_S,
    LIVE_STALL_S,
    PUSH_CARD,
    PUSH_DIGEST,
    PUSH_SESSION_CAP,
    SENSEVOICE_MODEL,
    SENSEVOICE_TOKENS,
    SAMPLE_RATE,
    SPEAKER_MODEL,
    VAD_MODEL,
    asr_threads,
)

WINDOW = 512                  # silero VAD 在 16k 下的窗长
CHUNK_SAMPLES = 4096          # 浏览器每块 256ms，与服务端只做长度校验无关的约定
QUEUE_MAX = 400               # 约 100s 缓冲，超了丢最旧的，宁可丢也不阻塞浏览器
SPK_THRESHOLD = 0.45          # CAM++ 余弦相似度；实测同人 ~0.69，异人 ~0.02
SPK_MIN_SECONDS = 0.5         # 太短的片段声纹不可靠
SPK_NEW_MIN_SECONDS = 1.2     # 只有足够长的片段才允许开新簇；2.0 太严，4 秒一句的直播里
                              # 大量 1~2 秒的发言永远开不出新簇，只能被并到别人身上

_TAG_RE = re.compile(r"<\|[^|]*\|>")
# 位置标签不是人名：飞书兜底时不能把它当"已经认出来了"。
_POSITIONAL_RE = re.compile(
    r"^(主播|嘉宾|说话人|用户|观众|有人|未识别|连麦|路人|网友|SPK[_-]?\d+)")
FINISH_LOG = DATA_DIR / "finalize.log"


def _log(msg: str, event: str = "live.finalize", level: str = "info",
         session_id: str = "", **fields: Any) -> None:
    """收尾链路的日志：结构化 JSON + data/finalize.log（保留人读的旧入口）。

    收尾跑在后台线程里，没日志的话「后台没出东西」就只能靠猜——真发生过。
    """
    logs.log_event(event, level=level, msg=msg, session_id=session_id,
                   component="live", **fields)
    try:
        with open(FINISH_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


class LiveError(RuntimeError):
    """模型缺失、会话不存在等可展示给用户的错误。"""


# ------------------------------------------------------------------ 模型

class Engine:
    """进程内只加载一次的三件套：SenseVoice、silero VAD、CAM++。

    关键约定（sherpa-onnx 官方）：``OfflineRecognizer`` 本身线程安全，但
    ``OfflineStream`` 不能跨线程共享；CPU provider 下多个线程可以各自
    ``create_stream()`` 后并发 ``decode_stream()``。因此这里不再用一把全局
    解码锁把所有会话串起来——调度交给 ``asr_pool``，识别器单例共享。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 所有 ASR worker 共用 _spk（CAM++）。embed 只有几十毫秒，一把锁足够；
        # 每个 worker 各开一份 ONNX 会话实测约 39MB，8 个 worker 就是 300MB+。
        self._spk_lock = threading.Lock()
        self._rec = None
        self._spk = None
        self._np = None
        self._sherpa = None
        self.error: str | None = None

    @staticmethod
    def missing() -> list[str]:
        return [str(p) for p in (SENSEVOICE_MODEL, SENSEVOICE_TOKENS, VAD_MODEL, SPEAKER_MODEL)
                if not Path(p).is_file()]

    @property
    def ready(self) -> bool:
        return self._rec is not None and not self.missing()

    def load(self) -> None:
        if self._rec is not None:
            return
        miss = self.missing()
        if miss:
            raise LiveError(
                "缺少本地模型：" + "、".join(Path(p).name for p in miss)
                + "。运行 python tools/fetch_models.py 下载（约 270MB）。")
        try:
            import numpy as np
            import sherpa_onnx
        except ImportError as e:  # 不装依赖时应用其余功能照常
            raise LiveError(
                "实时监听需要 sherpa-onnx 与 numpy：pip install -r requirements-live.txt") from e
        with self._lock:
            if self._rec is not None:
                return
            self._np = np
            self._sherpa = sherpa_onnx
            try:
                self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(SENSEVOICE_MODEL), tokens=str(SENSEVOICE_TOKENS),
                    # 共享会话的 intra-op 线程数；并发请求数由 asr_pool 的 worker 决定
                    num_threads=asr_threads(), use_itn=True, language=LIVE_ASR_LANGUAGE,
                    decoding_method="greedy_search", debug=False, provider="cpu")
                self._spk = self._new_embedder()
                self.error = None
            except Exception as e:
                self._rec = None
                self.error = str(e)
                raise LiveError("模型加载失败：%s" % e) from e

    def _new_embedder(self):
        return self._sherpa.SpeakerEmbeddingExtractor(
            self._sherpa.SpeakerEmbeddingExtractorConfig(
                model=str(SPEAKER_MODEL), num_threads=1, provider="cpu"))

    def new_embedder(self):
        """给每个 ASR worker 一个独立的声纹提取器，避免 embed 成为新的串行点。

        CAM++ 只有 28MB，N 份的内存代价远小于让所有 worker 抢一把锁。
        """
        if self._rec is None:
            raise LiveError("ASR 尚未加载")
        return self._new_embedder()

    def transcribe(self, samples) -> str:
        # 每次调用各自 create_stream()；识别器在线程间共享（CPU provider 下安全）
        st = self._rec.create_stream()
        st.accept_waveform(SAMPLE_RATE, samples)
        self._rec.decode_stream(st)
        return _clean(st.result.text or "")

    def embed(self, samples, extractor=None):
        # 默认走共享的 _spk：用 _spk_lock 串行化。embed 只做一次前向，
        # 几十毫秒量级；相比每个 worker 复制一份 CAM++（实测 ~39MB），
        # 这点串行代价可以忽略。显式传入 extractor 的调用方自己负责串行化。
        if extractor is None:
            with self._spk_lock:
                return self._embed_with(samples, self._spk)
        return self._embed_with(samples, extractor)

    def _embed_with(self, samples, ex):
        st = ex.create_stream()
        st.accept_waveform(SAMPLE_RATE, samples)
        st.input_finished()
        v = self._np.asarray(ex.compute(st), dtype="float32")
        n = float(self._np.linalg.norm(v)) or 1.0
        return v / n


ENGINE = Engine()
# 采集线程只负责 VAD/断句并把任务丢进来；解码、声纹、落库都在工作池里完成。
POOL = AsrPool(ENGINE)


def _clean(text: str) -> str:
    text = _TAG_RE.sub("", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    # 术语纠错放在最后一步：ASR 原始输出 -> 去标签/空白 -> 统一行话写法。
    # 纠错失败绝不能影响转写，所以整段兜底。
    try:
        return glossary.correct(text)
    except Exception:
        return text


def _meaningful(text: str) -> bool:
    return any(ch.isalnum() or "一" <= ch <= "鿿" for ch in text)


# ------------------------------------------------------------------ 聚类

class Clusters:
    """在线声纹聚类。最多 max_n 个簇，质心为归一化向量的滑动平均。"""

    def __init__(self, np, max_n: int = LIVE_MAX_SPEAKERS) -> None:
        self.np = np
        self.max_n = max_n
        self.ids: list[str] = []
        self.cent: list[Any] = []
        self.seconds: dict[str, float] = {}
        self.counts: dict[str, int] = {}
        # 人工合并掉的源簇：句数和秒数已清零，质心也不再参与后续分配，
        # 但簇号保留在 ids 里，避免 "SPK_%02d" % len(ids) 生成重复编号。
        self.retired: set[str] = set()

    def assign(self, emb, duration: float = 0.0, fallback: str | None = None) -> str:
        if emb is None:
            if not self.ids:
                return self._new(None)
            return fallback or self.ids[0]
        if not self.ids:
            return self._new(emb)
        valid = [i for i, c in enumerate(self.cent)
                 if c is not None and self.ids[i] not in self.retired]
        if not valid:                                   # 只出现过无嵌入的短句
            live_ids = [i for i in range(len(self.ids))
                        if self.ids[i] not in self.retired]
            if not live_ids:
                return self._new(emb)
            self.cent[live_ids[0]] = emb
            return self.ids[live_ids[0]]
        sims = [(i, float(emb @ self.cent[i])) for i in valid]
        best_i, best = max(sims, key=lambda t: t[1])
        if best >= SPK_THRESHOLD:
            if duration >= 1.0:                    # 短句不参与质心更新，避免被噪声带偏
                c = 0.7 * self.cent[best_i] + 0.3 * emb
                self.cent[best_i] = c / (float(self.np.linalg.norm(c)) or 1.0)
            return self.ids[best_i]
        # 短片段不新开簇：宁可暂时并到最近的人，也不要凭空多一个"嘉宾"
        if len(self.ids) < self.max_n and duration >= SPK_NEW_MIN_SECONDS:
            return self._new(emb)
        return self.ids[best_i]

    def _new(self, emb=None) -> str:
        sid = "SPK_%02d" % len(self.ids)
        self.ids.append(sid)
        self.cent.append(emb)
        self.seconds[sid] = 0.0
        self.counts[sid] = 0
        return sid

    def move(self, src: str, dst: str, seconds: float) -> None:
        """把一句话从 src 挪到 dst：两侧的时长和句数都要跟着调，话轮比例才准。"""
        if src == dst:
            return
        self.seconds[src] = max(0.0, self.seconds.get(src, 0.0) - seconds)
        self.counts[src] = max(0, self.counts.get(src, 0) - 1)
        self.seconds[dst] = self.seconds.get(dst, 0.0) + seconds
        self.counts[dst] = self.counts.get(dst, 0) + 1
        if dst not in self.ids:
            self.ids.append(dst)
            self.cent.append(None)

    def add(self, sid: str, seconds: float) -> None:
        self.seconds[sid] = self.seconds.get(sid, 0.0) + max(0.0, seconds)
        self.counts[sid] = self.counts.get(sid, 0) + 1

    def fold(self, src: str, dst: str) -> None:
        """把人工合并掉的源簇退役：质心不再参与分配，目标接住它的质心。"""
        if not src or src == dst:
            return
        if dst not in self.ids:
            self.ids.append(dst)
            self.cent.append(None)
            self.seconds.setdefault(dst, 0.0)
            self.counts.setdefault(dst, 0)
        try:
            si = self.ids.index(src)
            di = self.ids.index(dst)
        except ValueError:
            self.retired.add(src)
            return
        if self.cent[si] is not None and self.cent[di] is None:
            self.cent[di] = self.cent[si]
        self.cent[si] = None
        self.retired.add(src)

    def order(self) -> list[str]:
        return sorted([s for s in self.ids if s not in self.retired],
                      key=lambda s: self.seconds.get(s, 0.0), reverse=True)


# ------------------------------------------------------------------ 会话

def _resolve_live_hints(info: dict, prev: dict, manual: dict, *,
                        auto_score: float, auto_raw: float,
                        auto_seconds: float, confirm_n: int,
                        cand_score: float,
                        rejected: set[str] | None = None) -> dict[str, dict]:
    """把一次只读打分变成"直播里可以显示的名字"。

    规则刻意比结束后保守：
      - 只过候选线 → cand（chip/飞书带"疑似"）
      - 过自动线 + 满 auto_seconds + 连续 confirm_n 次同一个人 → auto（去掉"疑似"）
      - 一个人在本场只能占一个簇，分数高的先占座
      - 人工改过名或点过"不是他"的簇不参与，绝不覆盖人
    """
    rejected = rejected or set()
    manual_names = {str(v).strip() for v in manual.values() if v}
    rows = []
    for sid, item in info.items():
        if sid in manual or sid in rejected:
            continue
        cands = item.get("candidates") or []
        if not item.get("ready") or not cands:
            continue
        c = cands[0]
        if float(c.get("score") or 0.0) < cand_score:
            continue
        if str(c.get("name") or "") in manual_names:
            continue
        rows.append((sid, c, item))

    rows.sort(key=lambda x: -float(x[1].get("score") or 0.0))
    used: set[str] = set()
    out: dict[str, dict] = {}
    for sid, c, item in rows:
        pid = str(c.get("person_id") or "")
        if not pid or pid in used:
            continue
        used.add(pid)
        old = prev.get(sid) or {}
        same = old.get("person_id") == pid
        streak = int(old.get("streak") or 0) + 1 if same else 1
        seconds = float(item.get("seconds") or 0.0)
        auto = (float(c.get("score") or 0.0) >= auto_score
                and float(c.get("raw") or 0.0) >= auto_raw
                and seconds >= auto_seconds
                and streak >= confirm_n)
        out[sid] = {
            "person_id": pid, "name": str(c.get("name") or ""),
            "score": round(float(c.get("score") or 0.0), 3),
            "raw": round(float(c.get("raw") or 0.0), 3),
            "seconds": round(seconds, 1), "n": int(item.get("n") or 0),
            "state": "auto" if auto else "cand", "streak": streak,
        }
    return out


def _push_label(ev: dict) -> str:
    """飞书卡片上的说话人：人名 / 人名（疑似 0.68） / 嘉宾1（识别中）。"""
    label = str(ev.get("speaker_label") or "未识别说话人")
    state = str(ev.get("speaker_state") or "")
    if state == "cand":
        score = ev.get("speaker_score")
        label += "（疑似%s）" % ((" %.2f" % float(score)) if score is not None else "")
    elif state == "unknown":
        label += "（识别中）"
    return label


class LiveSession:
    def __init__(self, title: str, source_mode: str = "tab") -> None:
        # 时区在"开始这一刻"定死：started_at 保留 naive 本地时间（前端和历史代码都按它
        # 显示），另外存 UTC 与偏移。飞书文案要同时给北京和 UTC，而机器搬到别的时区
        # 之后，只有录制当时的偏移能还原真相。
        now = datetime.now().astimezone()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        self.id = "LIVE_" + stamp
        self.title = title or ("直播监听 " + now.strftime("%m-%d %H:%M"))
        # 采集来源：tab=浏览器页面，direct=解析流地址后 ffmpeg 直取。
        # 只影响元数据、提示文案和导出展示，不改变 VAD/ASR 管线。
        self.source_mode = (source_mode or "tab").strip().lower()
        self.started_at = now.replace(tzinfo=None).isoformat(timespec="seconds")
        self.started_utc = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        self.tz_offset_min = int((now.utcoffset() or timedelta(0)).total_seconds() // 60)
        self.q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.running = False
        self.error: str | None = None
        self.dropped = 0
        self.total_samples = 0
        self.recognized_s = 0.0
        self.speech_s = 0.0            # VAD 判定为人声的时长（用于区分"没识别"和"本来就没人说话"）
        self.speech = False
        self.partial: dict | None = None   # 当前这句的临时字幕（未定稿）
        self.paused = False                # 客户端暂停推流时置位，计时随 total_samples 一起冻结
        self.flush_req = threading.Event()  # 暂停时请求工作线程把半句立刻切出来
        self.marks: list[dict] = []        # 本场打点（同时落库，回看时仍可见）
        self.n_utts = 0                    # 本场已落库的发言条数（status 只回最后 60 句，数不了总数）
        self.last_speech_s = 0.0           # 最后一次判定"有人说话"的秒数，用来算静默了多久
        self.level = 0.0                   # 最近一块的 RMS，用来判断标签页是不是静音
        self.chars = 0                     # 已转写字数，用于实时语速
        self.questions = 0                 # 疑似提问句数（问号/吗呢吧结尾，启发式）
        self.utts: list[dict] = []
        self.clusters: Clusters | None = None
        self.labels: dict[str, str] = {}
        self.pos_labels: dict[str, str] = {}   # 位置标签（主播/嘉宾N），飞书兜底用
        self.manual: dict[str, str] = {}
        self.person_hint: dict[str, dict] = {}  # 直播中的疑似认人（只展示，不写底库）
        self.rejected: set[str] = set()         # 直播中点了「不是他」的簇，别再自动认
        self._hint_at = 0.0                    # 上次疑似比对的时间（monotonic）
        self._hint_lock = threading.Lock()      # 定时器和语音线程都可能刷新
        self._hint_thread: threading.Thread | None = None
        self.host_id: str | None = None
        self.recluster: dict | None = None   # 结束后全场重聚类的结果摘要
        self.spool = AUDIO_DIR / (self.id + ".pcm")
        self.audio_path: Path | None = None
        self._thread: threading.Thread | None = None
        self._spool_fh = None
        self.finished_at: str | None = None
        # 临时字幕的「代」：切句/暂停/停顿都会 +1，迟到的旧结果直接丢弃
        self._partial_epoch = 0
        self._closed = False
        self._discard = False          # 取消的场次：不再往库里写
        self._stop_lock = threading.Lock()
        self._stop_done = False
        self.scorer = None                 # 实时显著性打分（W2），首次有句子时才创建
        self._scorer_lock = threading.Lock()   # feed 在 ASR worker，poll 在采集线程
        self.pushed = 0                    # 本场已实时推送条数（不含结束回顾）
        self.cancelled = False             # 取消的场次不推送（连回顾也不发）
        self.push_bias: dict[str, float] = {}   # 从 👍/👎 学来的门槛偏置，start() 时加载
        self._last_audio_at = time.monotonic()   # 最后一次收到音频的墙钟，冷场/关页靠它兜底
        # 「最近一块」要看音频块本身，而不是最后一句人声：安静但流还在时，
        # idle_s 会一直涨，看起来像断流。last_chunk 是采集页发来的时刻，
        # last_feed 是工作线程真正写进 spool（入库）的时刻。
        self._last_chunk_mono = 0.0
        self.last_chunk_at = ""
        # 断流预警：服务端在块停 30 秒时发一次飞书；块回来后 feed() 复位。
        # 和上面的管线停滞（有语音但没定稿）是两件事，不能共用一个标志。
        self._chunk_stall_warned = False
        self._chunk_stall_warned_at = 0.0
        self._last_feed_mono = 0.0
        self.last_feed_at = ""
        # 「这一场从什么时候开始等音频」：断流看门狗要区分「刚建会话、第一块还没到」
        # 和「本来在推、后来断了」。以 running 置位那一刻为准，别算进模型加载时间。
        self._started_mono = time.monotonic()
        # 可观测性：最后一次定稿落库的墙钟 + 停滞状态。看门狗靠它发现
        # 「有语音但几分钟没有新句子」，这正是历史事故里查不到线索的那一段。
        self.last_final_at = time.monotonic()
        self._stall_state = False
        self._stall_since = 0.0
        self._stall_last_log = 0.0

    # -------------------------------------------------- 生命周期
    def start(self) -> None:
        ENGINE.load()
        import numpy as np
        from sherpa_onnx import VadModelConfig, VoiceActivityDetector

        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        self.clusters = Clusters(np, LIVE_MAX_SPEAKERS)
        cfg = VadModelConfig()
        cfg.silero_vad.model = str(VAD_MODEL)
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.55
        cfg.silero_vad.min_speech_duration = LIVE_MIN_SPEECH_S
        cfg.silero_vad.max_speech_duration = LIVE_MAX_SEGMENT_S
        cfg.silero_vad.window_size = WINDOW
        cfg.sample_rate = SAMPLE_RATE
        cfg.num_threads = 1
        cfg.debug = False
        self._vad_cfg = cfg
        self._vad_cls = VoiceActivityDetector

        conn = store.connect()
        try:
            store.insert_session(conn, {
                "id": self.id, "title": self.title, "started_at": self.started_at,
                "duration_s": 0.0, "source_mode": self.source_mode,
                "sample_rate": SAMPLE_RATE,
                "started_utc": self.started_utc, "tz_offset_min": self.tz_offset_min,
                "audio_path": None, "warnings": [
                    ("直取直播流：解析流地址后由 ffmpeg 直接拉音频，不加载播放器页面。"
                     if self.source_mode == "direct" else
                     "标签页实时采集：只包含被选中标签页的音频，不含其他系统声音。"),
                    "说话人由 CAM++ 声纹在线聚类得到（实时上限 %d 人）；结束后会用全场声纹重聚类，"
                    "抢话/重叠段仍可能归错，可改名或点「重新分人」校正。" % LIVE_MAX_SPEAKERS,
                    "直播间背景音乐会影响断句和转写，摘要以逐字稿为准。",
                ], "speakers": [],
            })
            # 反馈闭环：本场用上一场为止学到的偏好，定下就不再改口。
            try:
                from . import feedback
                self.push_bias = feedback.deltas(conn)
                if self.push_bias:
                    _log("推送偏好 %s" % ", ".join(
                        "%s%+.2f" % (k, v) for k, v in sorted(self.push_bias.items())))
            except Exception:
                traceback.print_exc()
        finally:
            conn.close()
        # 不缓冲：采集中回听直接读这个文件，写进去的字节要马上能被读到。
        self._spool_fh = open(self.spool, "wb", buffering=0)
        self.running = True
        self._started_mono = time.monotonic()
        POOL.register(self)          # 登记 + 按需启动共享 ASR 工作池
        POOL.start()
        self._thread = threading.Thread(target=self._worker, name="live-" + self.id, daemon=True)
        self._thread.start()
        # 没有新句子时也要能刷新疑似：常客说 25 秒就停，不能永远停在 cand。
        self._hint_thread = threading.Thread(target=self._hint_loop,
                                             name="hint-" + self.id, daemon=True)
        self._hint_thread.start()
        self.last_final_at = time.monotonic()
        metrics.METRICS.inc("live.sessions_started")
        logs.log_event("live.session_start", session_id=self.id,
                       title=self.title, asr_workers=asr_threads(),
                       workers=POOL.workers, source=self.source_mode)

    def feed(self, pcm: bytes) -> None:
        if not self.running:
            raise LiveError("会话已结束")
        now = time.monotonic()
        prev = self._last_chunk_mono
        if self._chunk_stall_warned:
            # 块回来了：这次断流的预警作废，下一次再断还能再报一次。
            # 记下缺口秒数，事后能从事件流里还原「哪一段没录到」。
            # prev=0 表示预警发生在「第一块之前」，没有缺口可算，但仍要复位。
            self._chunk_stall_warned = False
            metrics.METRICS.inc("live.capture_resumed", 1, session_id=self.id)
            logs.log_event("live.capture_resumed", session_id=self.id,
                           gap_s=round(now - prev, 1) if prev else 0.0)
        self._last_audio_at = now
        self._last_chunk_mono = now
        self.last_chunk_at = datetime.now().isoformat(timespec="seconds")
        metrics.METRICS.inc("live.chunks_in", 1, session_id=self.id)
        metrics.METRICS.inc("live.bytes_in", len(pcm), session_id=self.id)
        if self.paused:
            metrics.METRICS.inc("live.chunks_paused", 1, session_id=self.id)
            return
        if len(pcm) % 2:
            pcm = pcm[:-1]
        try:
            self.q.put_nowait(pcm)
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            metrics.METRICS.inc("live.chunks_dropped", 1, session_id=self.id)
            self.q.put_nowait(pcm)

    def mark_chunk_stall_warned(self) -> bool:
        """断流预警只发一次；feed() 收到下一块时复位。"""
        if self._chunk_stall_warned:
            return False
        self._chunk_stall_warned = True
        self._chunk_stall_warned_at = time.monotonic()
        return True

    def stop(self, finalize: bool = True) -> dict:
        with self._stop_lock:
            if self._stop_done:
                return self.status()
        self.stop_evt.set()
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=30)
        if self._hint_thread:
            self._hint_thread.join(timeout=2)
        if self._thread and self._thread.is_alive():
            self.error = (self.error or "") + " 结束处理超时（仍在收尾）。"
        # 采集线程退出前已把 VAD 里剩余的句子全部提交；等它们解码、落库完再收尾，
        # 否则重聚类会漏掉最后几句。超时说明有异常长的解码，记下来但不无限挂住。
        drained = POOL.drain_session(self.id, timeout=60)
        self._closed = True
        # _discard 只由 cancel() 显式置位；finalize=False 仍要把已切出的定稿落库，
        # 只是跳过总结/重聚类（进程退出时不该丢最后几句）。
        if drained.get("in_flight"):
            self.error = (self.error or "") + " ASR 收尾超时（最后一句可能丢失）。"

        duration = round(self.total_samples / SAMPLE_RATE, 2)
        ap = str(self.audio_path) if self.audio_path else None
        try:
            abytes = self.audio_path.stat().st_size if self.audio_path else None
        except OSError:
            abytes = None
        conn = store.connect()
        try:
            store.finish_session(conn, self.id, duration, ap,
                                 max(0.0, duration - self.recognized_s),
                                 audio_format=("wav" if ap else None),
                                 audio_bytes=abytes, audio_original_path=ap)
        finally:
            conn.close()
        # 收尾只负责把 WAV 落盘并写库；转码放到后台，失败也不影响这场可播。
        # 放在 finish_session 之后：否则转码线程先写完 audio_path=m4a，会被
        # 这里的 finish_session 又覆盖回 WAV。
        if ap:
            _audio.schedule_transcode(self.id, ap)

        out = self.status()
        asr_snapshot = out.get("asr") or {}
        # 先取指标视图再 unregister，否则工作池里这份 asr 统计就没了。
        if not self.cancelled:
            try:
                view = self.metrics_view()
                view["asr"] = asr_snapshot or view.get("asr") or {}
                metrics.METRICS.record_session_end(
                    self.id, view,
                    extra={"duration_s": duration, "pushed": self.pushed,
                           "error": self.error or "",
                           "finalize": bool(finalize)})
                logs.log_event(
                    "live.session_stop", session_id=self.id, duration_s=duration,
                    utterances=self.n_utts, dropped=self.dropped,
                    pushed=self.pushed, error=self.error or "")
            except Exception:
                traceback.print_exc()
        POOL.unregister(self.id)
        if finalize:
            # 重聚类 / 起标题 / 总结全部丢到后台：stop 只负责把音频和逐字稿落盘。
            # 以前重聚类是同步跑的（就在这个请求线程里），场次一长「录音落盘」那一格
            # 就得干等；而它本来也不影响数据完整性。
            self._auto_finalize()
            out = self.status()
            out["asr"] = asr_snapshot
        with self._stop_lock:
            self._stop_done = True
        return out

    def _auto_finalize(self) -> None:
        """下播收尾：先把总结排进全局队列，再在后台发本场回顾。

        总结不再每场起一个线程直接打模型；任务排队后立刻返回，「结束并保存」
        不会被模型拖住。收尾前的重聚类也放进这个任务里（见 tasks._run_summary），
        保证总结用的是整场声纹重分后的说话人。
        """
        if self.cancelled:      # 取消 = 用户不要这场，别在聊天里留下一条回顾
            return
        sid = self.id
        try:
            job = tasks.submit_finalize(sid)
            _log("已排队自动总结：%s（%s）" % (job.id, sid))
        except Exception:
            traceback.print_exc()
            _log("自动总结排队失败 %s" % sid)

        def run() -> None:
            # 回顾不依赖 LLM，先发；总结在调度器里慢慢排队，不互相等。
            try:
                _log("收尾回顾开始 %s" % sid)
                push_digest(self._sess_meta(), identities=self._identity_map() or None)
            except Exception:
                traceback.print_exc()

        threading.Thread(target=run, name="finalize-push-" + sid, daemon=True).start()

    # -------------------------------------------------- 工作线程
    def _worker(self) -> None:
        conn = store.connect()
        np = self.clusters.np
        # 30 秒缓冲足够：max_speech_duration 是 20 秒，留 10 秒余量。
        # 每个会话的 VAD 缓冲是 16k float32，60 秒约 3.8MB，30 秒省一半。
        vad = self._vad_cls(self._vad_cfg, buffer_size_in_seconds=30)
        carry = np.empty(0, dtype="float32")
        pbuf: list[Any] = []          # 当前还没切句的音频窗，用来出临时字幕
        pstart = 0.0
        last_partial = 0.0
        last_touch = 0.0
        last_cut = 0.0
        try:
            while True:
                try:
                    item = self.q.get(timeout=0.25)
                except queue.Empty:
                    if self.flush_req.is_set():
                        # 暂停：把当前半句切出来，恢复后不会和后面的音频拼成一句
                        vad.flush()
                        self._drain(vad)
                        pbuf.clear()
                        carry = np.empty(0, dtype="float32")
                        self._drop_partial()
                        self.flush_req.clear()
                    self._poll_scorer()
                    if self.stop_evt.is_set():
                        break
                    continue
                if item is None:
                    break
                self._spool_fh.write(item)
                self._last_feed_mono = time.monotonic()
                self.last_feed_at = datetime.now().isoformat(timespec="seconds")
                x = np.frombuffer(item, dtype="<i2").astype("float32") / 32768.0
                self.total_samples += len(x)
                if carry.size:
                    x = np.concatenate([carry, x])
                base = self.total_samples - len(x)      # x[0] 在整条流里的采样下标
                n = (len(x) // WINDOW) * WINDOW
                for i in range(0, n, WINDOW):
                    w = x[i:i + WINDOW]
                    vad.accept_waveform(w)
                    if vad.is_speech_detected():
                        if not pbuf:
                            pstart = (base + i) / SAMPLE_RATE
                        pbuf.append(w)
                carry = x[n:]
                produced = self._drain(vad)
                now_s = self.total_samples / SAMPLE_RATE
                try:
                    self.level = float(np.sqrt((x * x).mean()))
                except Exception:
                    pass
                if vad.is_speech_detected():
                    self.speech_s += (len(item) // 2) / SAMPLE_RATE
                    self.last_speech_s = now_s
                if produced:
                    pbuf.clear()
                    self._drop_partial()
                    last_cut = now_s
                    last_partial = now_s
                elif vad.is_speech_detected() and now_s - last_cut >= LIVE_FORCE_SEGMENT_S:
                    # BGM/连续说话时 VAD 可能几十秒不主动断句；到点强制切，延迟才有上界
                    vad.flush()
                    self._drain(vad)
                    pbuf.clear()
                    self._drop_partial()
                    last_cut = now_s
                    last_partial = now_s
                elif pbuf and not vad.is_speech_detected():
                    pbuf.clear()
                    self._drop_partial()
                elif (pbuf and LIVE_PARTIAL_INTERVAL_S > 0
                      and now_s - last_partial >= LIVE_PARTIAL_INTERVAL_S
                      and vad.is_speech_detected()):
                    # 当前这句还没到切点：先把已收到的部分识别出来，当灰色临时字幕
                    buf = np.concatenate(pbuf)
                    last_partial = now_s
                    if len(buf) >= LIVE_PARTIAL_MIN_S * SAMPLE_RATE:
                        self._update_partial(buf, pstart)
                self.speech = bool(vad.is_speech_detected())
                if self.flush_req.is_set():
                    vad.flush()
                    self._drain(vad)
                    pbuf.clear()
                    carry = np.empty(0, dtype="float32")
                    self._drop_partial()
                    self.flush_req.clear()
                if time.time() - last_touch >= 5:
                    store.touch_session(conn, self.id, round(self.total_samples / SAMPLE_RATE, 2))
                    last_touch = time.time()
            vad.flush()
            self._drain(vad)
            self._drop_partial()
        except Exception as e:  # 单个会话出错不能拖垮服务
            self.error = "%s: %s" % (type(e).__name__, e)
            metrics.METRICS.inc("live.errors", 1, session_id=self.id)
            logs.log_event("live.worker_error", level="error", session_id=self.id,
                           error=self.error)
            traceback.print_exc()
        finally:
            # 回顾关掉时，最后那条压着的高光必须在这里补发，否则"收工"就等于丢一条。
            if not PUSH_DIGEST:
                self._poll_scorer(force=True)
            # 采集线程只负责把音频落盘；定稿的落库在 ASR worker 里，
            # 由 LiveSession.stop() 等 drain_session 完成后再更新场次摘要。
            self._spool_fh.close()
            self._finalize_wav()
            conn.close()
            self.running = False
            self.finished_at = store.now()
            self.speech = False

    def _update_partial(self, buf, t0: float) -> None:
        """把当前半句先送去识别，作为灰色临时字幕；定稿走 commit_final。

        这里只做一次拷贝和入队，解码在共享工作池里做。同一会话只保留最新一帧，
        排队过久会被 TTL 丢掉，不再像以前那样让临时字幕占着全局解码锁。
        """
        n = int(LIVE_PARTIAL_MAX_S * SAMPLE_RATE)
        if len(buf) > n:
            t0 += (len(buf) - n) / SAMPLE_RATE
            buf = buf[-n:]
        import numpy as np
        samples = np.array(buf, dtype="float32", copy=True)
        with self.lock:
            epoch = self._partial_epoch
        POOL.submit_partial(self, samples, t0=t0,
                            seconds=len(samples) / SAMPLE_RATE, epoch=epoch,
                            audio_end_wall=time.monotonic())

    def _drop_partial(self) -> None:
        with self.lock:
            self.partial = None
            self._partial_epoch += 1      # 让还在解码的旧半句结果作废

    def _drain(self, vad) -> int:
        """把 VAD 切出来的定稿全部入队；解码与落库由 ASR worker 完成。"""
        import numpy as np
        n = 0
        while not vad.empty():
            seg = vad.front
            samples = np.array(seg.samples, dtype="float32", copy=True)
            start = int(seg.start)
            vad.pop()
            n += 1
            dur = len(samples) / SAMPLE_RATE
            if dur < LIVE_MIN_SPEECH_S:
                continue
            if dur > LIVE_MAX_SEGMENT_S + 1:
                samples = samples[: int((LIVE_MAX_SEGMENT_S + 1) * SAMPLE_RATE)]
                dur = len(samples) / SAMPLE_RATE
            # 这句已经切出来定稿，灰色临时字幕立刻作废
            self._drop_partial()
            end_sample = start + len(samples)
            # 这段话最后一个采样「实际到达」的墙钟时间：用来算真正的端到端延迟
            # （包含 VAD 等静音/等强切的时间，而不只是解码耗时）
            audio_end_wall = time.monotonic() - max(
                0, self.total_samples - end_sample) / SAMPLE_RATE
            POOL.submit_final(self, samples, start_sample=start, seconds=dur,
                              embed=dur >= SPK_MIN_SECONDS,
                              audio_end_wall=audio_end_wall)
        return n

    # -------------------------------------------------- ASR worker 回调
    def commit_final(self, job, text: str, emb, conn) -> bool:
        """定稿结果落库。由 ASR worker 调用；同一会话严格串行，聚类顺序稳定。"""
        dur = job.seconds
        t0 = job.start_sample / SAMPLE_RATE
        if not _meaningful(text):
            return False
        # 分类在录制当下就跑，否则实时场次的「剔除低价值 / 按分类总结」全是摆设
        try:
            cls = analysis.classify(text)
        except Exception:          # 分类出错不能影响落库
            traceback.print_exc()
            cls = {}
        with self.lock:
            if self._closed or self._discard:
                return False
            spk = self.clusters.assign(emb, dur)
            self.clusters.add(spk, dur)
        # 先刷新"疑似认人"（只读、最多 20 秒一次），再算标签，保证这句就能用上新名字。
        # 这里用的是上一句为止的库数据；差一句 4 秒，比在语音线程里多查一次划算。
        self._refresh_hints(conn)
        label = self._refresh_labels(conn, spk)
        with self.lock:
            if self._discard:
                return False
        ids = store.insert_utterances(conn, [{
            "session_id": self.id, "t_start": round(t0, 2), "t_end": round(t0 + dur, 2),
            "speaker_id": spk, "speaker_label": label, "text": text,
            "asr_conf": 0.9, "overlap": 0,
            "category": cls.get("category", "substantive"),
            "value_tag": cls.get("value_tag", "normal"),
            "importance": cls.get("importance", 0.5),
            "topic": cls.get("topic", ""),
            "low_value_reason": cls.get("low_value_reason", ""),
        }])
        # 定稿真正落库了才算"有产出"：停滞看门狗和端到端延迟都以这里为准。
        now_mono = time.monotonic()
        self.last_final_at = now_mono
        metrics.METRICS.inc("live.finals", 1, session_id=self.id)
        end_wall = getattr(job, "audio_end_wall", None)
        if end_wall:
            metrics.METRICS.observe("live.final_commit_ms",
                                    max(0.0, now_mono - end_wall) * 1000.0,
                                    session_id=self.id)
        if self._stall_state:
            self._stall_state = False
            metrics.METRICS.inc("live.stalls_recovered", 1, session_id=self.id)
            logs.log_event("live.stall_recovered", session_id=self.id,
                           stalled_s=round(now_mono - self._stall_since, 1))
        if emb is not None and ids:
            # 声纹存成 float16（192 维 = 384 字节/句），一整场 500 句也才 190KB，
            # 换来的是结束后可以整场重聚类、反复调 K，而不用重跑 ASR。
            try:
                store.save_embeddings(
                    conn, self.id,
                    [(ids[0], self.clusters.np.float16(emb).tobytes(), int(emb.shape[0]))])
            except Exception:
                traceback.print_exc()
        row = {
            "id": ids[0] if ids else None,
            "t_start": round(t0, 2), "t_end": round(t0 + dur, 2),
            "speaker_id": spk, "speaker_label": label, "text": text,
            "topic": cls.get("topic", ""),
            "category": cls.get("category", "substantive"),
            "value_tag": cls.get("value_tag", "normal"),
        }
        with self.lock:
            self.n_utts += 1
            self.recognized_s += dur
            self.utts.append(row)
            if len(self.utts) > 200:
                del self.utts[:100]
            self.chars += len(text)
            if ("?" in text) or ("？" in text) or text.rstrip().endswith(("吗", "呢", "吧")):
                self.questions += 1
        self._maybe_push(row)
        return True

    def apply_partial(self, job, text: str) -> str:
        """把临时字幕结果放上屏；返回 ok / empty / stale（供池子计数）。"""
        if not _meaningful(text):
            return "empty"
        with self.lock:
            if self._closed or self.paused or job.epoch != self._partial_epoch:
                return "stale"
            self.partial = {"t_start": round(job.t0, 1), "text": text,
                            "seconds": round(job.seconds, 1)}
            return "ok"

    def _maybe_push(self, row: dict) -> None:
        """实时打一分；命中就异步推飞书。任何异常都吞掉——不能影响录音和字幕。"""
        with self._scorer_lock:
            try:
                if self.scorer is None:
                    from . import highlight
                    self.scorer = highlight.OnlineScorer(bias=self.push_bias)
                ev = self.scorer.feed(row)
                self._flush_skip_log()
                if ev:
                    self._emit_event(ev)
            except Exception:
                traceback.print_exc()

    def _poll_scorer(self, force: bool = False) -> None:
        """没有新句子时也要能把压着的高光放出去。

        三种场景：这段话说完了冷场、用户按了暂停、直接关掉标签页（音频断了但
        没人点结束）。force=True 用在收尾，把 pending 无条件取出来。
        """
        if self.scorer is None:
            return
        with self._scorer_lock:
            if self.scorer is None:
                return
            try:
                silence = 1e9 if force else (time.monotonic() - self._last_audio_at)
                ev = self.scorer.poll(self.total_samples / SAMPLE_RATE,
                                      silence_s=silence, force=force)
            except Exception:
                traceback.print_exc()
                return
            self._flush_skip_log()
            if ev:
                self._emit_event(ev)

    def _flush_skip_log(self) -> None:
        """把 scorer 记下的"没推"原因落台账，然后清掉，避免重复记。"""
        sk = getattr(self.scorer, "last_skip", None) if self.scorer else None
        if not sk:
            return
        self.scorer.last_skip = None
        self._log_skip(sk["peak_t"], sk["reason"], sk.get("score", 0.0),
                       sk.get("why") or "", sk.get("features") or {})

    def _log_skip(self, peak_t: float, reason: str, score: float = 0.0,
                  why: str = "", features: dict | None = None) -> None:
        try:
            conn = store.connect()
            try:
                store.log_push_skip(
                    conn, self.id, "hl:%.2f" % float(peak_t), reason,
                    score=float(score or 0), text=why,
                    features_json=json.dumps(features or {}, ensure_ascii=False))
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()

    def _emit_event(self, ev: dict) -> None:
        """一条事件 -> 一次推送。上限在发送前挡住，别让一场直播响个没完。"""
        if self.cancelled:
            return
        if self.pushed >= PUSH_SESSION_CAP:
            self._log_skip(ev.get("peak_t") or 0, "session_cap",
                           ev.get("score") or 0, ev.get("why") or "",
                           ev.get("features") or {})
            return
        self._apply_identity(ev)
        self.pushed += 1
        threading.Thread(target=push_highlight,
                         args=(ev, self._sess_meta()), daemon=True).start()

    def _sess_meta(self) -> dict:
        """推送要用的场次信息：带时区，才能换算出北京/UTC 两个时间。"""
        return {"id": self.id, "title": self.title, "started_at": self.started_at,
                "started_utc": self.started_utc, "tz_offset_min": self.tz_offset_min}

    def _identity_map(self) -> dict[str, tuple[str, str]]:
        """收工回顾用：speaker_id → (名字, 状态)。人工优先，其次直播疑似。"""
        out = {sid: (name, "confirmed") for sid, name in list(self.manual.items())}
        for sid, h in list(self.person_hint.items()):
            if h.get("name"):
                out.setdefault(sid, (str(h["name"]), str(h.get("state") or "")))
        return out

    def _apply_identity(self, ev: dict) -> None:
        """推送前把"当时的旧标签"换成"此刻认得的人"。

        OnlineScorer 会压 45 秒再推峰值句，这 45 秒正好够声纹攒出结论；
        如果只在句子上游定标签，飞书永远慢半拍。没有名字就退回位置标签，
        并标成"识别中"，绝不把上一句的人名带出去。
        """
        sid = str(ev.get("speaker_id") or "")
        if sid and sid in self.rejected:
            ev["speaker_label"] = self.pos_labels.get(sid) or "说话人"
            ev["speaker_state"] = "rejected"
            ev.pop("speaker_score", None)
            return
        if sid and sid in self.manual:
            ev["speaker_label"] = self.manual[sid]
            ev["speaker_state"] = "manual"
            ev.pop("speaker_score", None)
            return
        h = self.person_hint.get(sid) if sid else None
        if h:
            ev["speaker_label"] = h["name"]
            ev["speaker_state"] = h["state"]
            ev["speaker_score"] = round(float(h["score"]), 2)
            return
        old = str(ev.get("speaker_label") or "")
        fallback = self.pos_labels.get(sid) or ""
        if fallback:
            ev["speaker_label"] = fallback
            ev["speaker_state"] = "unknown"
        elif old and not _POSITIONAL_RE.match(old):
            ev["speaker_label"] = old          # 旧事件没有 speaker_id 时只能信标签
            ev["speaker_state"] = "manual"
        else:
            ev["speaker_label"] = old or "未识别说话人"
            ev["speaker_state"] = "unknown"

    def _hint_loop(self) -> None:
        """定时刷新疑似：只读、不改标签。

        只靠逐句定稿（commit_final）有个洞：常客说 25 秒就停，第二句没来，永远停在 cand。
        这里每 LIVE_HINT_EVERY_S 秒补一次，只更新 person_hint；chip 由 status()
        动态取名，不需要写库。
        """
        while not self.stop_evt.wait(max(1.0, LIVE_HINT_EVERY_S)):
            if not self.running:
                return
            try:
                conn = store.connect()
                try:
                    self._refresh_hints(conn)
                finally:
                    conn.close()
            except Exception:
                traceback.print_exc()

    def _display_label(self, sid: str) -> str:
        """chip 上此刻该显示的名字：人工 > 疑似 > 位置标签。"""
        if sid in self.manual:
            return self.manual[sid]
        h = self.person_hint.get(sid) or {}
        if h.get("name"):
            return str(h["name"])
        return self.pos_labels.get(sid) or self.labels.get(sid) or "说话人"

    def reject_hint(self, speaker_id: str) -> str:
        """直播中「不是他」：只清这次判断，不注册、不写原型。

        同时把这一簇记住，本场后续的自动比对不再给它贴同一个人；
        数据库侧由调用方写 rejected，结束后的 match_session 也不会再命中。
        """
        self.rejected.add(speaker_id)
        self.person_hint.pop(speaker_id, None)
        self.manual.pop(speaker_id, None)
        label = self.pos_labels.get(speaker_id) or "说话人"
        self.labels[speaker_id] = label
        with self.lock:
            for u in self.utts:
                if u.get("speaker_id") == speaker_id:
                    u["speaker_label"] = label
        return label

    def _refresh_hints(self, conn) -> None:
        """直播中的「疑似认人」：只读比对，最多每 LIVE_HINT_EVERY_S 秒一次。

        这里刻意不调用 match_session：那会清空/写回 person_state，把人工确认、
        结束后的权威复算和实时展示搅在一起。展示错一次可以改，底库写错很难洗。
        """
        if not self.running or not self.clusters:
            return
        if not self._hint_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            if now - self._hint_at < LIVE_HINT_EVERY_S:
                return
            self._hint_at = now
            from . import voiceprint
            info = voiceprint.identify_session(conn, self.id, top=1,
                                               min_seconds=LIVE_HINT_MIN_S)
            manual = dict(self.manual)   # 改名是 HTTP 线程做的，先拍快照再跑
            hints = _resolve_live_hints(
                info, self.person_hint, manual,
                auto_score=voiceprint.AUTO_THRESHOLD,
                auto_raw=voiceprint.AUTO_MIN_RAW,
                auto_seconds=LIVE_HINT_AUTO_S,
                confirm_n=LIVE_HINT_CONFIRM,
                cand_score=voiceprint.CAND_THRESHOLD,
                rejected=self.rejected)
        except Exception:
            traceback.print_exc()
            return
        finally:
            self._hint_lock.release()
        for sid, h in hints.items():
            old = self.person_hint.get(sid) or {}
            if old.get("person_id") != h["person_id"] or old.get("state") != h["state"]:
                logs.log_event("live.identity_hint", session_id=self.id,
                               speaker_id=sid, name=h["name"], state=h["state"],
                               score=round(float(h["score"]), 2))
        # 点「不是他」和定时刷新可能擦肩：赋回前再挡一次，别把刚否掉的又贴回来
        self.person_hint = {k: v for k, v in hints.items() if k not in self.rejected}

    def _refresh_labels(self, conn, new_spk: str | None = None) -> str:
        """按累计发言时长给簇命名；有滞回，避免嘉宾话多时主播/嘉宾来回跳。

        标签计算在会话锁里完成，数据库改名放到锁外：status() 每几百毫秒就读一次，
        不能让一次 relabel 卡住它。
        """
        with self.lock:
            changed, label = self._labels_locked(new_spk)
        for sid, lab in changed:
            if sid in self.manual:
                continue
            try:
                store.relabel_speaker(conn, self.id, sid, lab)
            except Exception:
                traceback.print_exc()
            with self.lock:                       # 已显示的记录同步改标签
                for u in self.utts:
                    if u["speaker_id"] == sid:
                        u["speaker_label"] = lab
        if new_spk:
            try:
                store.upsert_speaker(conn, self.id, new_spk, label)
            except Exception:
                traceback.print_exc()
        return label

    def _labels_locked(self, new_spk: str | None = None) -> tuple[list[tuple[str, str]], str]:
        """调用者必须持有 self.lock。返回 (需要改库的标签, 当前说话人的显示名)。"""
        order = self.clusters.order()
        if not order:
            return [], "说话人"
        if self.host_id is None:
            self.host_id = order[0]
        elif len(order) > 1:
            cur = self.clusters.seconds.get(self.host_id, 0.0)
            top = self.clusters.seconds.get(order[0], 0.0)
            if order[0] != self.host_id and top > max(cur * 1.6, cur + 20):
                self.host_id = order[0]
        auto: dict[str, str] = {}
        if len(order) >= 2:
            auto[self.host_id] = "主播"
            n = 0
            for sid in order:
                if sid == self.host_id:
                    continue
                n += 1
                auto[sid] = "嘉宾" if n == 1 else "嘉宾%d" % n
        else:
            # 只有一个人说话时，大概率就是主播本人；太短则先不急着下结论
            only = order[0]
            auto[only] = "主播" if self.clusters.seconds.get(only, 0) >= 10 else "说话人1"
        auto.update(self.manual)
        self.pos_labels = dict(auto)             # 位置标签：飞书兜底用，不含疑似名字
        # 复制一份再遍历：person_hint 会被 HTTP 线程的"改名/不是他"改，边遍历边改会抛
        # RuntimeError（这里跑在采集线程上，抛出只会让这一句悄悄丢掉）。
        for sid, h in list(self.person_hint.items()):
            if sid in self.manual:               # 人改过的名字永远优先
                continue
            auto[sid] = h["name"]
        changed = [(sid, lab) for sid, lab in auto.items() if self.labels.get(sid) != lab]
        self.labels = auto
        return changed, self.labels.get(new_spk or "", "说话人")

    def _finalize_wav(self) -> None:
        if not self.spool.exists():
            return
        out = AUDIO_DIR / (self.id + ".wav")
        try:
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                with open(self.spool, "rb") as f:
                    while True:
                        block = f.read(1 << 20)
                        if not block:
                            break
                        w.writeframes(block)
            self.audio_path = out
            # 采集中可能有人在读 spool。Windows 上删一个正被读的文件会失败，
            # 但 WAV 已经写好了，留个 spool 不影响回放，不能因此把收尾算失败。
            try:
                self.spool.unlink(missing_ok=True)
            except OSError:
                traceback.print_exc()
        except Exception:
            traceback.print_exc()

    # -------------------------------------------------- 查询
    def status(self) -> dict:
        asr = POOL.session_stats(self.id)
        with self.lock:
            rows = list(self.utts)
            c = self.clusters
            speakers = []
            if self.recluster and self.recluster.get("speakers"):
                # 已经做过全场重聚类：内存里的在线簇号已经作废，以重聚类结果为准
                speakers = [dict(s) for s in self.recluster["speakers"]]
            elif c:
                for sid in c.order():
                    # 人工合并后源簇的时长/句数都归零，但簇号还留着（不能删，
                    # 删了之后 "SPK_%02d" % len(ids) 会和现存簇号撞车）。
                    # 查询时跳过这些空壳，界面上就不会多出一个 0 秒的说话人。
                    if c.counts.get(sid, 0) <= 0 and c.seconds.get(sid, 0.0) <= 0:
                        continue
                    h = self.person_hint.get(sid) or {}
                    speakers.append({
                        "speaker_id": sid,
                        "label": self._display_label(sid),
                        "seconds": round(c.seconds.get(sid, 0.0), 1),
                        "count": c.counts.get(sid, 0),
                        "manual": sid in self.manual,
                        "person_state": h.get("state")
                        or ("rejected" if sid in self.rejected else
                            ("confirmed" if sid in self.manual else "")),
                        "person_name": h.get("name") or self.manual.get(sid, ""),
                        "person_score": round(float(h["score"]), 2)
                        if h.get("score") is not None else None,
                    })
            return {
                "id": self.id, "title": self.title, "started_at": self.started_at,
                "source_mode": self.source_mode,
                "running": self.running, "finished_at": self.finished_at,
                "elapsed": round(self.total_samples / SAMPLE_RATE, 1),
                "speech": self.speech, "queued": self.q.qsize(), "dropped": self.dropped,
                "paused": self.paused,
                "recognized_s": round(self.recognized_s, 1),
                "speech_s": round(min(self.speech_s, self.total_samples / SAMPLE_RATE), 1),
                "n_utterances": self.n_utts,
                "level": round(self.level, 5),
                "idle_s": round(max(0.0, self.total_samples / SAMPLE_RATE - self.last_speech_s), 1),
                "last_chunk_at": self.last_chunk_at or None,
                "last_chunk_ago_s": round(max(0.0, time.monotonic() - self._last_chunk_mono), 1)
                if self._last_chunk_mono else None,
                "last_feed_at": self.last_feed_at or None,
                "last_feed_ago_s": round(max(0.0, time.monotonic() - self._last_feed_mono), 1)
                if self._last_feed_mono else None,
                # 最后一句定稿距今。还没出字时返回 None，避免把“建会话到现在”
                # 误报成“最后一句很久以前”。
                "last_final_ago_s": round(max(0.0, time.monotonic() - self.last_final_at), 1)
                if self.n_utts else None,
                "chars": self.chars, "questions": self.questions,
                "marks": list(self.marks),
                "error": self.error, "engine": "SenseVoiceSmall-int8 + CAM++",
                "pushed": self.pushed, "started_utc": self.started_utc,
                "tz_offset_min": self.tz_offset_min,
                "partial": self.partial,
                "recluster": self.recluster,
                "asr": asr,
                "utterances": rows[-60:], "speakers": speakers,
            }

    # -------------------------------------------------- 指标
    def metrics_view(self) -> dict:
        """给 /api/metrics 和采样器用的精简视图：不带 60 句字幕，开销很小。"""
        now = time.monotonic()
        elapsed = self.total_samples / SAMPLE_RATE
        with self.lock:
            last_line = self.utts[-1]["text"][:60] if self.utts else ""
            n_recent = len(self.utts)
        return {
            "id": self.id, "title": self.title,
            "state": "running" if self.running else "stopped",
            "running": bool(self.running), "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round(elapsed, 1),
            "queued": self.q.qsize(), "dropped": self.dropped,
            "paused": bool(self.paused), "speech": bool(self.speech),
            "n_utterances": self.n_utts, "n_recent_utts": n_recent,
            "recognized_s": round(self.recognized_s, 1),
            "speech_s": round(min(self.speech_s, elapsed), 1),
            "last_speech_age_s": round(max(0.0, elapsed - self.last_speech_s), 1),
            "last_final_age_s": round(max(0.0, now - self.last_final_at), 1),
            "stall": bool(self._stall_state),
            "error": self.error or "", "pushed": self.pushed,
            "last_line": last_line,
            "asr": POOL.session_stats(self.id),
            "metrics": metrics.METRICS.session_snapshot(self.id),
        }

    def check_stall(self, now: float | None = None) -> bool:
        """有语音但太久没有新定稿 = 停滞。返回当前是否处于停滞。

        不把「本来没人说话」当停滞：录内静默超过 LIVE_STALL_IDLE_S 就跳过。
        持续停滞每 5 分钟补一条事件，避免事件表被一分钟一条刷爆。
        """
        now = time.monotonic() if now is None else now
        if not self.running or self.paused:
            return self._stall_state
        elapsed = self.total_samples / SAMPLE_RATE
        idle = elapsed - self.last_speech_s
        age = now - self.last_final_at
        stalled = (elapsed >= LIVE_STALL_MIN_S and idle <= LIVE_STALL_IDLE_S
                   and age >= LIVE_STALL_S)
        if stalled:
            if not self._stall_state:
                self._stall_state = True
                self._stall_since = now
                self._stall_last_log = now
                metrics.METRICS.inc("live.stalls", 1, session_id=self.id)
                logs.log_event(
                    "live.stall", level="warn", session_id=self.id,
                    idle_s=round(idle, 1), last_final_s=round(age, 1),
                    queued=self.q.qsize(), dropped=self.dropped,
                    n_utterances=self.n_utts, elapsed_s=round(elapsed, 1))
            elif now - self._stall_last_log >= 300:
                self._stall_last_log = now
                logs.log_event(
                    "live.stall", level="warn", session_id=self.id,
                    idle_s=round(idle, 1),
                    last_final_s=round(now - self.last_final_at, 1),
                    queued=self.q.qsize(), dropped=self.dropped,
                    n_utterances=self.n_utts, elapsed_s=round(elapsed, 1),
                    persistent=True)
        elif self._stall_state:
            self._stall_state = False
            metrics.METRICS.inc("live.stalls_recovered", 1, session_id=self.id)
            logs.log_event("live.stall_recovered", session_id=self.id,
                           stalled_s=round(now - self._stall_since, 1))
        return self._stall_state


# ------------------------------------------------------------------ 推送

# 为什么不再用进程内计数器做"每天最多几条"：进程一重启额度就清零，
# 而且它在发送前自增——一次网络抖动就白瞎一条额度，用户却什么也没收到。
# 现在预算、幂等、失败原因全部落在 push_log 表里（app/store.py），
# 重启/重试/并发都不会重复发，也能回答"为什么没收到"。
_BOT = None
_BOT_LOCK = threading.Lock()


def _get_bot():
    """飞书机器人客户端。第一次用时才建，没配凭证的机器上永远走不到这里。"""
    global _BOT
    with _BOT_LOCK:
        if _BOT is None:
            from . import feishu
            _BOT = feishu.Bot()
    return _BOT


def _reader_link(sid: str, t: float) -> str:
    """采集中去 /live 能直接听 spool；结束后 /live 没有音频，仍走 reader。"""
    page = "live" if is_active(sid) else "reader"
    return "%s/%s?session=%s&t=%d" % (BASE_URL, page, sid, int(t or 0))


def _feedback_link(sid: str, t: float, verdict: int) -> str:
    """卡片上的 👍/👎 落到本地：飞书只有 URL 按钮能在纯内网用，
    点开一个确认页即写回，不需要回调地址/公网。"""
    return "%s/card/feedback?sid=%s&t=%.2f&v=%d" % (BASE_URL, sid, float(t or 0), verdict)


def _room_title(session: dict) -> str:
    """把会话标题洗成"能一眼认出是哪个间"的短名。

    监听中心建会话时标题是「监听中心 · 伍大姐的抖音直播间 - 抖音直播」，
    推 5 路时这串前缀每次都在，手机上第一行就看不全重点。
    """
    t = str(session.get("title") or "").strip()
    for pre in ("监听中心 · ", "监听中心·", "直播监听 · ", "直播监听·"):
        if t.startswith(pre):
            t = t[len(pre):]
    for suf in (" - 抖音直播", "-抖音直播", " - 抖音", " · 抖音直播", " - 快手直播"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    return (t.strip() or "本场")[:22]


def _push_text(ev: dict, session: dict) -> str:
    """实时高光文案。时间给两份：北京用于对表，UTC 用于跨系统对日志。

    录音内偏移保留在第二行——手机上点回听之前，先得知道"是第几分钟"。
    """
    peak = float(ev.get("peak_t") or 0)
    icon = {"both": "🔥", "live": "🎙", "info": "💡"}.get(ev.get("kind") or "", "✨")
    sid = str(session.get("id") or ev.get("session_id") or "")
    return "\n".join([
        "%s %s · %s" % (icon, _push_label(ev), timefmt.clock_line(session, peak)),
        "📻 %s · 录音 %s · %s" % (_room_title(session), timefmt.offset(peak),
                                  ev.get("why") or "综合得分靠前"),
        "「%s」" % (ev.get("text") or "")[:120],
        "回听：%s" % _reader_link(sid, peak),
    ])


def _push_card(ev: dict, session: dict) -> dict:
    """实时高光的飞书卡片：时间做成两个字段，回听做成按钮。

    卡片比纯文本多出来的不是好看，是少一次操作：手机上不用长按复制链接，
    点按钮直接进阅读页那一刻。schema 写错会发送失败，所以 live._send 必须
    能回退纯文本——这条底线不能丢。
    """
    peak = float(ev.get("peak_t") or 0)
    kind = str(ev.get("kind") or "info")
    icon = {"both": "🔥", "live": "🎙", "info": "💡"}.get(kind, "✨")
    cn, utc = timefmt.clock_pair(session, peak)
    sid = str(session.get("id") or ev.get("session_id") or "")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": {"both": "carmine", "live": "orange"}.get(kind, "blue"),
            "title": {"tag": "plain_text",
                      "content": "%s %s ·《%s》" % (icon, _push_label(ev), _room_title(session))},
        },
        "elements": [
            {"tag": "div", "fields": [
                {"is_short": True,
                 "text": {"tag": "lark_md", "content": "**北京时间**\n%s" % cn}},
                {"is_short": True,
                 "text": {"tag": "lark_md", "content": "**UTC**\n%s" % utc}},
            ]},
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "**录音 %s** · %s" % (timefmt.offset(peak),
                                              ev.get("why") or "综合得分靠前")}},
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "「%s」" % (ev.get("text") or "")[:120]}},
            {"tag": "action", "actions": [
                {"tag": "button", "type": "primary",
                 "text": {"tag": "plain_text", "content": "回听这一段"},
                 "url": _reader_link(sid, peak)},
                {"tag": "button", "type": "default",
                 "text": {"tag": "plain_text", "content": "👍 有用"},
                 "url": _feedback_link(sid, peak, 1)},
                {"tag": "button", "type": "default",
                 "text": {"tag": "plain_text", "content": "👎 没用"},
                 "url": _feedback_link(sid, peak, -1)},
            ]},
        ],
    }


def _digest_speaker(ev: dict, identities: dict | None) -> str:
    """收工回顾的说话人：位置+人名+疑似，和逐字稿一致。"""
    sid = str(ev.get("speaker_id") or "")
    name, state = (identities or {}).get(sid, ("", ""))
    return store.speaker_display(ev.get("speaker_label"), name, state)


def _digest_text(session: dict, events: list[dict], n_all: int,
                 n_pushed: int, identities: dict | None = None) -> str:
    """收工回顾：只列"实时没推到的"，每条都给两个时区。

    不把已推过的再列一遍——那些消息还在聊天记录里，重复只会让人关通知。
    """
    sid = str(session.get("id") or "")
    title = _room_title(session)
    lines = ["🎬 本场高光补推 %d 段 ·《%s》" % (len(events), title),
             "%s 开始 · 共 %d 条入榜，实时推过 %d 条"
             % (timefmt.clock_line(session, 0), n_all, n_pushed)]
    for i, ev in enumerate(events, 1):
        peak = float(ev.get("peak_t") or 0)
        cn, utc = timefmt.clock_pair(session, peak)
        when = ("%s 北京 / %s UTC" % (cn, utc[6:])
                if cn[:5] == utc[:5] else "%s 北京 / %s UTC" % (cn, utc))
        lines.append("%d. %s · %s · 录音 %s" % (
            i, when, _digest_speaker(ev, identities),
            timefmt.offset(peak)))
        lines.append("「%s」" % (ev.get("text") or "")[:60])
        lines.append("回听：%s" % _reader_link(sid, peak))
    return "\n".join(lines)


def _digest_card(session: dict, events: list[dict], n_all: int,
                 n_pushed: int, identities: dict | None = None) -> dict:
    """收工回顾的卡片：一条消息里最多 3 段，每段一个「回听」按钮。"""
    sid = str(session.get("id") or "")
    title = _room_title(session)
    elements: list[dict] = [{
        "tag": "div", "text": {"tag": "lark_md",
        "content": "%s 开始 · 共 %d 条入榜，实时推过 %d 条"
                   % (timefmt.clock_line(session, 0), n_all, n_pushed)}}]
    actions: list[dict] = []
    for i, ev in enumerate(events, 1):
        peak = float(ev.get("peak_t") or 0)
        cn, utc = timefmt.clock_pair(session, peak)
        when = ("%s 北京 / %s UTC" % (cn, utc[6:])
                if cn[:5] == utc[:5] else "%s 北京 / %s UTC" % (cn, utc))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            "**%d · %s**\n%s · 录音 %s\n「%s」" % (
                i, when, _digest_speaker(ev, identities),
                timefmt.offset(peak), (ev.get("text") or "")[:60])}})
        actions.append({"tag": "button", "type": "default",
                        "text": {"tag": "plain_text", "content": "回听第 %d 段" % i},
                        "url": _reader_link(sid, peak)})
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text",
                             "content": "🎬 本场高光 ·《%s》" % title}},
        "elements": elements,
    }


def _send(conn, sid: str, item: str, text: str, level: str = "",
          score: float = 0.0, card: dict | None = None,
          features_json: str = "") -> bool:
    """占位 -> 预算 -> 发送 -> 收尾。所有分支都写台账，失败不抛。

    卡片优先，失败回退纯文本：文案可以讲究，消息不能丢。
    每个终态同时进指标与结构化日志，「为什么没收到」不再只能查库。
    """
    from .config import PUSH_DAILY_CAP, PUSH_ENABLED
    t0 = time.monotonic()

    def done(status: str, reason: str = "") -> None:
        ms = (time.monotonic() - t0) * 1000.0
        # Registry.inc / observe 带 session_id 时会同时写全局和该场，别调两次。
        metrics.METRICS.inc("push.%s" % status, 1, session_id=sid)
        if status in ("sent", "failed"):
            metrics.METRICS.observe("push.latency_ms", ms, session_id=sid)
        # 没配机器人、没打开会话、预算跳过都不是故障，别把它们刷进事件流。
        expected = reason in ("", "daily_cap", "all_pushed", "no_highlight",
                              "no_bot", "no_open_id", "already_claimed")
        logs.log_event("push.%s" % status,
                       level="info" if expected else "warn",
                       session_id=sid, item=item, reason=reason or "",
                       latency_ms=round(ms, 1) if status in ("sent", "failed") else None)

    if not PUSH_ENABLED:
        done("skipped", "disabled")
        return False
    card_json = json.dumps(card, ensure_ascii=False) if card else ""
    try:
        if not store.claim_push(conn, sid, item, level=level, score=score,
                                text=text, card_json=card_json,
                                features_json=features_json):
            done("duplicate", "already_claimed")   # 已经推过（重试/并发/重启）
            return False
        if item.startswith("hl:") and store.pushes_today(conn) >= PUSH_DAILY_CAP:
            store.close_push(conn, sid, item, "skipped", "daily_cap")
            done("skipped", "daily_cap")
            return False
        try:
            bot = _get_bot()
        except Exception as e:                  # 没配凭证不是 bug，是没开这个功能
            store.close_push(conn, sid, item, "failed", "no_bot: %s" % e)
            done("failed", "no_bot")
            return False
        if not bot.open_id:
            store.close_push(conn, sid, item, "failed", "no_open_id")
            done("failed", "no_open_id")
            return False
        ok, reason = False, ""
        if card and PUSH_CARD:
            try:
                ok = bool(bot.send_card(card))
            except Exception:
                traceback.print_exc()
                ok = False
            if not ok:
                # 卡片被拒（schema/权限/版本）不算失败，退回文本继续发
                ok = bool(bot.send_text(text))
                reason = "card_fallback" if ok else "send_fail"
        else:
            ok = bool(bot.send_text(text))
            reason = "" if ok else "send_fail"
        store.close_push(conn, sid, item, "sent" if ok else "failed", reason)
        done("sent" if ok else "failed", reason)
        return ok
    except Exception:
        traceback.print_exc()
        store.close_push(conn, sid, item, "failed", "exception")
        done("failed", "exception")
        return False


def push_capture_lost(sid: str, idle_s: float) -> bool:
    """断流预警：明确说「不是没人说话」，并给出回听页。

    每段断流发一条：item 带上本次预警的时间戳，push_log 的 (session_id, item)
    幂等只用来挡同一段的重复发送；块回来后 feed() 复位，下一次再断是新的一条。
    """
    s = _SESSIONS.get(sid)
    title = _room_title({"title": s.title if s else "本场"})
    text = "\n".join([
        "⚠️ 采集链路断了 ·《%s》" % title,
        "已经 %d 秒没收到音频块。这不是没人说话：安静时采集页也会每 256ms 推一块。" % int(idle_s),
        "可能是浏览器崩溃 / 采集页被关 / 页面被冻结；直播间可能还在播。",
        "采集端会自动尝试恢复；如果一直没恢复，请打开收听页看一眼。",
        "收听页：%s/live?session=%s" % (BASE_URL, sid),
    ])
    # 同一声道内 monotonic 不会重复；重启后会话本身也没了，不存在跨进程撞号。
    episode = int((s._chunk_stall_warned_at if s else 0) * 1000) or int(time.time())
    try:
        conn = store.connect()
    except Exception:
        traceback.print_exc()
        return False
    try:
        return _send(conn, sid, "alert:stall:%d" % episode, text)
    finally:
        conn.close()


def _push_skip(sid: str, item: str, reason: str) -> None:
    """没走到 _send 的跳过分支（没高光 / 全推过）也要计数，口径才完整。"""
    metrics.METRICS.inc("push.skipped", 1, session_id=sid)
    logs.log_event("push.skipped", session_id=sid, item=item, reason=reason)


def push_highlight(ev: dict, session: dict) -> bool:
    """把一条高光推进飞书。失败只打日志——推送永远不能影响录音。"""
    sid = str(session.get("id") or ev.get("session_id") or "")
    peak = round(float(ev.get("peak_t") or 0), 2)
    text = _push_text(ev, session)
    card = _push_card(ev, session)
    features_json = json.dumps(ev.get("features") or {}, ensure_ascii=False)
    try:
        conn = store.connect()
    except Exception:
        traceback.print_exc()
        return False
    try:
        ok = _send(conn, sid, "hl:%.2f" % peak, text, card=card,
                   level=str(ev.get("level") or ""), score=float(ev.get("score") or 0),
                   features_json=features_json)
        logs.log_event("push.highlight_done", session_id=sid,
                       result="ok" if ok else "skip",
                       offset=timefmt.offset(peak), why=ev.get("why") or "")
        return ok
    finally:
        conn.close()


def push_digest(session: dict, limit: int = 3,
                identities: dict | None = None) -> bool:
    """结束后补一条本场回顾，只补"还没推过"的。

    为什么要有：实时推送有冷却和上限，一场直播里最值得听的三段很可能只推出去
    一段。收工这一条是"补齐"，不是"再骚扰一次"——全都推过就静默。
    identities 由 LiveSession 传入（人工 + 直播疑似）；不传则退回库里的结果。
    """
    sid = str(session.get("id") or "")
    if not (PUSH_DIGEST and sid):
        return False
    from . import highlight
    try:
        conn = store.connect()
    except Exception:
        traceback.print_exc()
        return False
    try:
        try:
            events = highlight.score_session(conn, sid)
        except Exception:
            traceback.print_exc()
            events = []
        if not events:
            # 必须走 log_push_skip（先占位再收尾）：close_push 只是一条 UPDATE，
            # 行不存在时它什么也不写，于是"为什么没收到回顾"在台账里永远是空白。
            store.log_push_skip(conn, sid, "digest", "no_highlight")
            _push_skip(sid, "digest", "no_highlight")
            return False
        if identities is None:
            identities = {}
            for r in conn.execute(
                    "SELECT s.speaker_id, s.person_state, p.name FROM speakers s "
                    "LEFT JOIN persons p ON p.person_id=s.person_id "
                    "WHERE s.session_id=?", (sid,)):
                identities[r["speaker_id"]] = (r["name"] or "", r["person_state"] or "")
        pushed = store.pushed_peaks(conn, sid)
        rest = [e for e in events if round(float(e["peak_t"]), 2) not in pushed]
        if not rest:
            store.log_push_skip(conn, sid, "digest", "all_pushed")
            _push_skip(sid, "digest", "all_pushed")
            return False
        n_pushed = sum(1 for e in events
                       if round(float(e["peak_t"]), 2) in pushed)
        text = _digest_text(session, rest[:limit], len(events), n_pushed, identities)
        card = _digest_card(session, rest[:limit], len(events), n_pushed, identities)
        ok = _send(conn, sid, "digest", text, card=card)
        logs.log_event("push.digest_done", session_id=sid,
                       result="ok" if ok else "skip", n_events=len(rest))
        return ok
    finally:
        conn.close()


# ------------------------------------------------------------------ 注册表

_SESSIONS: dict[str, LiveSession] = {}
_LOCK = threading.Lock()


def _new_id() -> str:
    sid = "LIVE_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    base, n = sid, 1
    while sid in _SESSIONS:
        n += 1
        sid = "%s_%d" % (base, n)
    return sid


def is_active(sid: str) -> bool:
    """会话是否还在采集。回听链接据此选 /live 还是 /reader。

    不看「还在内存里」：收尾回顾是在 stop() 里另一个线程发的，
    那时会话可能还没从 _SESSIONS 弹出，但已经 running=False，
    链接必须走 reader，不能指到一个「不在监听」的 /live。
    """
    s = _SESSIONS.get(str(sid or ""))
    return bool(s and s.running and not s.stop_evt.is_set())


def list_active() -> list[dict]:
    """当前所有在跑的监听会话。

    5 路并行以后，"谁在听哪个间、还在不在出字"必须能一眼看到，
    否则某一路上午断了，到晚上才发现少了 8 小时。
    """
    with _LOCK:
        items = list(_SESSIONS.values())
    out: list[dict] = []
    for s in items:
        try:
            st = s.status()
        except Exception:
            continue
        with s.lock:
            last = (s.utts[-1]["text"][:60] if s.utts else "")
            n = len(s.utts)
        st["n_recent_utts"] = n
        st["pushed"] = getattr(s, "pushed", 0)
        st["last_line"] = last
        out.append(st)
    out.sort(key=lambda x: str(x.get("started_at") or ""))
    return out


def mark_chunk_stall_warned(sid: str) -> bool:
    """给服务端断流预警用：同一场只发一次，块回来后由 feed() 复位。"""
    s = _SESSIONS.get(sid)
    return bool(s and s.mark_chunk_stall_warned())


def chunk_age(sid: str) -> float | None:
    """距上一块音频多少秒；从没收到过块返回 None。轻量版，不组装 status。"""
    s = _SESSIONS.get(sid)
    if not s or not s._last_chunk_mono:
        return None
    return max(0.0, time.monotonic() - s._last_chunk_mono)


AUDIO_BYTES_PER_SEC = SAMPLE_RATE * 2      # 16k / 单声道 / s16le
LIVE_AUDIO_MAX_S = 7200.0                  # 单次回听窗口上限，够 2 小时


def audio_window(sid: str, *, seconds: float | None = None,
                 start_s: float | None = None, end_s: float | None = None,
                 default_s: float = 60.0,
                 min_s: float = 0.2) -> dict:
    """采集中回听：按字节偏移切正在写入的 spool，不转码。

    spool 是 16k/单声道/s16le，``offset = 秒 * 32000``。以文件实际大小为准，
    不用墙钟：暂停、断流、队列丢块时，音频与逐字稿都按实际写入的样本数走，
    两边天然对齐。返回的 start_byte/nbytes 是请求那一刻的快照，调用方只读
    这一段，文件继续增长也不会把 Content-Length 撑破。
    """
    s = _SESSIONS.get(sid)
    if not s:
        return {"ok": False, "reason": "not_active", "session_id": sid}
    try:
        size = s.spool.stat().st_size
    except OSError:
        size = 0
    size -= size % 2                       # 末尾半个采样不播
    total_s = size / AUDIO_BYTES_PER_SEC
    paused, running = bool(s.paused), bool(s.running)
    chunk_ago = (round(max(0.0, time.monotonic() - s._last_chunk_mono), 1)
                 if s._last_chunk_mono else None)
    if size <= 0:
        return {"ok": False, "reason": "no_audio", "session_id": sid,
                "total_s": 0.0, "paused": paused, "running": running,
                "last_chunk_ago_s": chunk_ago, "spool": str(s.spool)}

    if end_s is None:
        end_v = total_s
    else:
        end_v = max(0.0, min(float(end_s), total_s))
    if start_s is None:
        span = float(default_s if seconds is None else seconds)
        span = max(0.0, min(span, LIVE_AUDIO_MAX_S))
        start_v = max(0.0, end_v - span)
    else:
        start_v = max(0.0, min(float(start_s), end_v))

    start_i = max(0, min(int(start_v * SAMPLE_RATE), int(total_s * SAMPLE_RATE)))
    end_i = max(start_i, min(int(end_v * SAMPLE_RATE), int(total_s * SAMPLE_RATE)))
    nbytes = (end_i - start_i) * 2
    if nbytes < max(2, int(min_s * AUDIO_BYTES_PER_SEC)):
        return {"ok": False, "reason": "too_short", "session_id": sid,
                "total_s": total_s, "paused": paused, "running": running,
                "last_chunk_ago_s": chunk_ago, "spool": str(s.spool)}

    return {
        "ok": True, "session_id": sid, "spool": str(s.spool),
        "start_s": start_i / SAMPLE_RATE, "end_s": end_i / SAMPLE_RATE,
        "start_byte": start_i * 2, "nbytes": nbytes,
        "total_s": total_s, "paused": paused, "running": running,
        "last_chunk_ago_s": chunk_ago,
    }


def stalled_sessions(idle_s: float, now: float | None = None) -> list[dict]:
    """还在 running、但已经 idle_s 秒没收到音频块的场次。

    看的是 last_chunk（采集页真发来一块的时刻），不是 last_speech：主播安静地坐着、
    流还在推的时候 idle_s 会一直涨，那不是断流。采集页每 256ms 推一块（静音也推），
    所以「一块都没到」只可能是链路断了。

    从没收到过块的场次按 _started_mono 起算——刚建会话的头几秒不能误判。
    """
    t = time.monotonic() if now is None else now
    with _LOCK:
        items = list(_SESSIONS.values())
    out: list[dict] = []
    for s in items:
        if not s.running:
            continue
        never = not s._last_chunk_mono
        idle = t - (s._started_mono if never else s._last_chunk_mono)
        if idle >= idle_s:
            out.append({"id": s.id, "title": s.title, "idle_s": round(idle, 1),
                        "never": never})
    out.sort(key=lambda x: -x["idle_s"])
    return out


def metrics_sessions() -> list[dict]:
    """在跑的场次的精简指标视图。看板与采样器共用，不返回字幕正文。"""
    with _LOCK:
        items = list(_SESSIONS.values())
    out: list[dict] = []
    for s in items:
        try:
            out.append(s.metrics_view())
        except Exception:
            traceback.print_exc()
    out.sort(key=lambda x: str(x.get("started_at") or ""))
    return out


# ---------------------------------------------------------------- 停滞看门狗

_WATCHDOG: threading.Thread | None = None
_WATCHDOG_LOCK = threading.Lock()
_WATCHDOG_STOP = threading.Event()


def _watchdog_loop(interval: float = 10.0) -> None:
    while not _WATCHDOG_STOP.wait(interval):
        with _LOCK:
            sessions = list(_SESSIONS.values())
        now = time.monotonic()
        for s in sessions:
            try:
                s.check_stall(now)
            except Exception:
                traceback.print_exc()


def start_watchdog(interval: float = 10.0) -> None:
    """启动全局停滞巡检。run.py 在服务启动时调用；测试不会自动起线程。"""
    global _WATCHDOG
    with _WATCHDOG_LOCK:
        if _WATCHDOG is not None and _WATCHDOG.is_alive():
            return
        _WATCHDOG_STOP.clear()
        _WATCHDOG = threading.Thread(target=_watchdog_loop, args=(max(2.0, interval),),
                                     name="live-watchdog", daemon=True)
        _WATCHDOG.start()


def stop_watchdog() -> None:
    _WATCHDOG_STOP.set()
    th = _WATCHDOG
    if th is not None and th.is_alive():
        th.join(timeout=2)


def start(title: str = "", source_mode: str = "tab") -> dict:
    s = LiveSession(title, source_mode=source_mode)
    with _LOCK:
        s.id = _new_id()
        s.spool = AUDIO_DIR / (s.id + ".pcm")
        _SESSIONS[s.id] = s
    try:
        s.start()
    except Exception:
        POOL.unregister(s.id)
        with _LOCK:
            _SESSIONS.pop(s.id, None)
        raise
    return {"session_id": s.id, "title": s.title, "sample_rate": SAMPLE_RATE,
            "chunk_samples": CHUNK_SAMPLES, "source_mode": s.source_mode,
            "engine": "SenseVoiceSmall-int8 + CAM++ (CPU)"}


def feed(sid: str, pcm: bytes) -> dict:
    s = _SESSIONS.get(sid)
    if not s:
        raise LiveError("会话不存在或已结束")
    s.feed(pcm)
    return {"ok": True, "queued": s.q.qsize(), "dropped": s.dropped}


def status(sid: str) -> dict:
    s = _SESSIONS.get(sid)
    if not s:
        raise LiveError("会话不存在或已结束")
    return s.status()


def set_paused(sid: str, paused: bool) -> dict:
    """暂停只冻结计时、不再收音频；工作线程会把当前半句切出来，避免跨暂停拼句。"""
    s = _SESSIONS.get(sid)
    if not s:
        raise LiveError("会话不存在或已结束")
    s.paused = bool(paused)
    if s.paused:
        s.flush_req.set()
    return s.status()


def mark(sid: str, note: str = "") -> dict:
    s = _SESSIONS.get(sid)
    if not s:
        raise LiveError("会话不存在或已结束")
    t = round(s.total_samples / SAMPLE_RATE, 1)
    conn = store.connect()
    try:
        m = store.insert_mark(conn, sid, t, note[:80])
    finally:
        conn.close()
    with s.lock:
        s.marks.append(m)
        marks = list(s.marks)
    return {"mark": m, "marks": marks}


def stop(sid: str, finalize: bool = True) -> dict:
    s = _SESSIONS.get(sid)
    if not s:
        raise LiveError("会话不存在或已结束")
    out = s.stop(finalize=finalize)
    with _LOCK:
        _SESSIONS.pop(sid, None)
    conn = store.connect()
    try:
        out["meta"] = store.get_meta(conn, sid)
    finally:
        conn.close()
    return out


def cancel(sid: str) -> dict:
    """采集在拿到音频前就失败了（用户取消共享/没勾音频），删掉空场次。"""
    s = _SESSIONS.get(sid)
    if s:
        s.cancelled = True
        s._discard = True              # 工作池里迟到的结果不再写库
        try:
            s.stop(finalize=False)
        except Exception:
            traceback.print_exc()
        with _LOCK:
            _SESSIONS.pop(sid, None)
    conn = store.connect()
    try:
        store.delete_session(conn, sid)
    finally:
        conn.close()
    for p in (AUDIO_DIR / (sid + ".pcm"), AUDIO_DIR / (sid + ".wav"),
              AUDIO_DIR / (sid + ".m4a"), AUDIO_DIR / (sid + ".opus")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


def recluster(sid: str, k: int | None = None) -> dict:
    """对已保存的场次重跑说话人聚类（可指定人数，None 为自动）。"""
    conn = store.connect()
    try:
        if not store.get_session(conn, sid):
            raise LiveError("场次不存在")
        try:
            out = _recluster.recluster_session(conn, sid, k)
        except ValueError as e:      # 没有声纹 / 声纹太少，属于可展示的业务错误
            raise LiveError(str(e)) from e
    finally:
        conn.close()
    s = _SESSIONS.get(sid)
    if s:
        s.recluster = out
        s.labels = {x["speaker_id"]: x["label"] for x in out["speakers"]}
        s.person_hint = {}               # 在线簇号已作废，疑似结果一并清掉
        s.pos_labels = {}
        s.rejected.clear()
    return out


def note_reassign(sid: str, utt_id: int | None, t_start: float | None,
                  speaker_id: str, label: str) -> bool:
    """把一句话改判给别人。实时中要同步内存里的行和话轮比例。"""
    s = _SESSIONS.get(sid)
    if not s:
        return False
    with s.lock:
        for u in s.utts:
            hit = (utt_id is not None and u.get("id") == utt_id) or (
                utt_id is None and t_start is not None
                and abs(float(u.get("t_start") or 0) - float(t_start)) < 0.05)
            if not hit:
                continue
            old = u.get("speaker_id")
            u["speaker_id"] = speaker_id
            u["speaker_label"] = s._display_label(speaker_id)
            if s.clusters and old:
                dur = float(u["t_end"]) - float(u["t_start"])
                s.clusters.move(old, speaker_id, max(0.0, dur))
            return True
    return False


def note_merge(sid: str, src_ids: list[str], target_id: str,
               target_label: str = "") -> None:
    """人工把一簇并到另一簇：内存字幕和话轮比例要同步，不能等场后重聚类。

    DB 侧的 merge_speakers 只改库；实时页读的是内存里的 utts/clusters，
    不补这一步，用户点了「这就是老王」之后画面要等到本场结束才变。
    """
    s = _SESSIONS.get(sid)
    if not s or not target_id:
        return
    src = {str(x) for x in (src_ids or []) if x and str(x) != target_id}
    if not src:
        return
    with s.lock:
        for u in s.utts:
            old = u.get("speaker_id")
            if old not in src:
                continue
            u["speaker_id"] = target_id
            u["speaker_label"] = s._display_label(target_id) or target_label
            if s.clusters and old:
                dur = float(u.get("t_end") or 0) - float(u.get("t_start") or 0)
                s.clusters.move(old, target_id, max(0.0, dur))
        for k in src:
            if s.clusters:
                s.clusters.fold(k, target_id)
            s.manual.pop(k, None)
            s.person_hint.pop(k, None)
            s.rejected.discard(k)
            s.labels.pop(k, None)
            s.pos_labels.pop(k, None)
        if target_label and target_id not in s.manual:
            s.labels.setdefault(target_id, target_label)


def reject_hint(sid: str, speaker_id: str) -> dict:
    """直播中「不是他」：清掉这次自动判断，不注册、不写原型。

    DB 里记 rejected 是必要的：结束后的 match_session 看到 rejected 会跳过，
    同一个人不会在复算时又被贴回来。rejected 不算"入库"，不会往底库加样本。
    """
    s = _SESSIONS.get(sid)
    conn = store.connect()
    try:
        store.set_speaker_person(conn, sid, speaker_id, None, None, None, "rejected")
        label = (s.pos_labels.get(speaker_id) if s else "") or "说话人"
        store.relabel_speaker(conn, sid, speaker_id, label)
    finally:
        conn.close()
    if s:
        label = s.reject_hint(speaker_id)
    return {"ok": True, "speaker_id": speaker_id, "label": label}


def retitle(sid: str, title: str) -> bool:
    """改这一场会话的标题。

    人工改房间名时一起改：飞书推送、高光卡片、导出文件名用的都是**会话**
    标题，只改房间的话，收听页已经叫新名字、飞书里还是旧名字，等于没改。
    会话已经结束（不在内存里）就返回 False——历史场次的标题以落库的为准，
    这里不去改存档。
    """
    s = _SESSIONS.get(str(sid or ""))
    if not s or not str(title or "").strip():
        return False
    s.title = str(title).strip()[:120]
    return True


def note_rename(sid: str, speaker_id: str, name: str, enroll: bool = True) -> None:
    """人工改名。注意要连已经推给前端的那些行一起改，
    否则实时字幕里会同时出现旧名（改名之前说的）和新名（改名之后说的）。

    改名同时就是「注册」：把这一簇的声纹写进跨场次的人物底库（app/voiceprint.py），
    下回这个人再开口就能被认出来。放在内存会话判断之前，是因为已经结束的场次
    事后补改名同样要能入底库。
    """
    if enroll:
        _enroll_voiceprint(sid, speaker_id, name)
    s = _SESSIONS.get(sid)
    if not s:
        return
    s.manual[speaker_id] = name
    s.labels[speaker_id] = name
    s.person_hint.pop(speaker_id, None)   # 人工改名后不再让自动结果覆盖
    s.rejected.discard(speaker_id)         # 人工点名优先于"不是他"
    # 同场同名只留这一簇：DB 侧 enroll_speaker 已经清掉别的 person_id，
    # 内存里的 chips/字幕也要同步，否则页面上还会看到两个"阿哲"。
    # 复制后再遍历：这两个 dict 同时被采集线程（刷新疑似标签）读写
    dups = {k for k, h in list(s.person_hint.items())
            if k != speaker_id and h.get("name") == name}
    dups |= {k for k, v in list(s.manual.items()) if k != speaker_id and v == name}
    for k in dups:
        s.person_hint.pop(k, None)
        s.manual.pop(k, None)
        s.labels[k] = s.pos_labels.get(k) or "说话人"
    with s.lock:
        for u in s.utts:
            if u.get("speaker_id") == speaker_id:
                u["speaker_label"] = name
            elif u.get("speaker_id") in dups:
                u["speaker_label"] = s.pos_labels.get(u.get("speaker_id"), "说话人")


def _enroll_voiceprint(sid: str, speaker_id: str, name: str) -> None:
    """改名时顺手把声纹存进人物底库。失败只打日志：注册不该挡住一次改名。"""
    import sqlite3

    from . import voiceprint
    try:
        conn = store.connect()
        try:
            out = voiceprint.enroll_speaker(conn, sid, speaker_id, name)
        finally:
            conn.close()
        if out.get("ok"):
            logs.log_event("voiceprint.enrolled", session_id=sid, name=name,
                           person_id=out["person_id"], utterances=out["utterances"])
        elif out.get("reason"):
            logs.log_event("voiceprint.enroll_skipped", level="warn", session_id=sid,
                           name=name, reason=out["reason"])
    except sqlite3.Error:
        traceback.print_exc()
    except Exception:
        traceback.print_exc()


def info() -> dict:
    """模型状态 + 正在采集的会话。

    active 是给 tools/dev.py 用的：改代码要重启进程，但正在采集的场次一重启就断，
    守护进程靠这个列表决定"立刻重启"还是"等这场录完"。
    """
    missing = Engine.missing()
    with _LOCK:
        active = [sid for sid, s in _SESSIONS.items() if s.running]
    return {
        "ready": not missing,
        "missing": missing,
        "active": active,
        "models_dir": str(SENSEVOICE_MODEL.parent.parent),
        "sample_rate": SAMPLE_RATE,
        "chunk_samples": CHUNK_SAMPLES,
        "max_speakers": LIVE_MAX_SPEAKERS,
        "engine": "SenseVoiceSmall-int8 + CAM++ (CPU)",
        "asr": POOL.stats(),
        "asr_threads": asr_threads(),
        # tools/dev.py 靠这个判断「下播收尾还在跑，先别重启换代码」
        "llm_jobs": scheduler.SCHEDULER.stats_summary(),
        "error": ENGINE.error,
    }


def shutdown() -> None:
    stop_watchdog()
    with _LOCK:
        sessions = list(_SESSIONS.values())
    for s in sessions:
        try:
            s.stop(finalize=False)
        except Exception:
            traceback.print_exc()
    POOL.shutdown()
    with _LOCK:
        _SESSIONS.clear()


def recover_spools() -> int:
    """进程被强杀时，把遗留的 .pcm 收尾成 WAV，避免整场音频丢失。"""
    if not AUDIO_DIR.is_dir():
        return 0
    n = 0
    ready: list[tuple[str, Path]] = []
    conn = store.connect()
    try:
        for pcm in sorted(AUDIO_DIR.glob("*.pcm")):
            sid = pcm.stem
            wav = AUDIO_DIR / (sid + ".wav")
            try:
                if not wav.exists():
                    with wave.open(str(wav), "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(SAMPLE_RATE)
                        with open(pcm, "rb") as f:
                            while True:
                                block = f.read(1 << 20)
                                if not block:
                                    break
                                w.writeframes(block)
                    n += 1
                dur = wav.stat().st_size / (SAMPLE_RATE * 2.0)
                pcm.unlink(missing_ok=True)
                conn.execute("UPDATE sessions SET audio_path=?, duration_s=MAX(duration_s,?) "
                             "WHERE id=?", (str(wav), round(dur, 2), sid))
                ready.append((sid, wav))
            except OSError:
                traceback.print_exc()
        conn.commit()
    finally:
        conn.close()
    # 数据库提交后再转码：转码线程要读 sessions.audio_path。
    for sid, wav in ready:
        _audio.schedule_transcode(sid, wav)
    return n
