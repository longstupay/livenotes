# -*- coding: utf-8 -*-
"""共享 ASR 工作池：把「采集/VAD」与「识别」拆成生产者/消费者。

改造前每个 LiveSession 的工作线程直接调用 ``ENGINE.transcribe()``，而
transcribe 用一把全局 ``_decode_lock`` 包住整个解码过程，于是：

- 10 路同时说话 = 所有解码严格串行，一路的长句会阻塞所有路的字幕；
- 滚动字幕每秒一次解码，和定稿抢同一把锁，临时字幕会拖慢更重要的定稿；
- 会话数一多，线程都在等锁，CPU 用不满，延迟却线性增长；
- 单会话的输入队列只丢最旧的 PCM，没有跨会话的公平调度。

现在：

    采集线程（VAD/断句） --submit--> 本模块调度器 --dispatch--> N 个 ASR worker

调度规则：

- 定稿（final）优先于临时字幕（partial）；
- 同一会话同时只跑一条定稿，且按提交顺序执行（声纹聚类/落库依赖顺序）；
- 临时字幕每会话最多一条在跑 + 一条待跑，新的覆盖旧的；切句/停顿时整代作废；
- 临时字幕有 TTL，排队超过 TTL 直接丢（下一帧会覆盖更长的音频）；
- 跨会话轮转 + 每会话定稿队列有界，避免某一路持续说话饿死其他路，也避免
  积压无限增长；被丢的定稿计入 dropped，最终体现在「未转写」时长里；
- 识别器单例共享（sherpa-onnx 明确 Recognizer 线程安全、Stream 不共享，
  CPU provider 下可并发 decode），不复制 240MB 模型；所有 worker 共用
  Engine 里的一个 CAM++ 提取器，用一把锁串行化几十毫秒的 embed，
  避免每个 worker 各开一份 ~39MB 的 ONNX 会话。

所有时间统计都在这里，``/api/live/status``、``/api/live/info`` 和压测脚本读同一份。
"""
from __future__ import annotations

import math
import threading
import time
import traceback
from collections import deque
from typing import Any

from . import store

FINAL = "final"
PARTIAL = "partial"


class _Window:
    """固定长度的延迟样本窗口，给 P50/P95 用；不保存原始音频。"""

    __slots__ = ("_values",)

    def __init__(self, maxlen: int = 512) -> None:
        self._values: deque[float] = deque(maxlen=maxlen)

    def add(self, value: float) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if math.isfinite(v):
            self._values.append(v)

    def snapshot(self) -> dict[str, Any]:
        if not self._values:
            return {"n": 0, "p50": None, "p95": None, "avg": None, "max": None}
        values = sorted(self._values)
        n = len(values)
        return {
            "n": n,
            "p50": round(values[max(0, math.ceil(0.50 * n) - 1)], 3),
            "p95": round(values[max(0, math.ceil(0.95 * n) - 1)], 3),
            "avg": round(sum(values) / n, 3),
            "max": round(values[-1], 3),
        }


class AsrJob:
    __slots__ = (
        "kind", "session", "samples", "start_sample", "seconds", "t0", "epoch",
        "embed", "submitted_at", "audio_end_wall", "expires_at",
        "started_at", "done_at",
    )

    def __init__(self, kind: str, session: Any, samples: Any, *,
                 start_sample: int = 0, seconds: float = 0.0, t0: float = 0.0,
                 epoch: int = 0, embed: bool = False,
                 audio_end_wall: float | None = None,
                 expires_at: float | None = None) -> None:
        self.kind = kind
        self.session = session
        self.samples = samples
        self.start_sample = int(start_sample)
        self.seconds = float(seconds)
        self.t0 = float(t0)
        self.epoch = int(epoch)
        self.embed = bool(embed)
        self.submitted_at = time.monotonic()
        self.audio_end_wall = audio_end_wall
        self.expires_at = expires_at
        self.started_at = 0.0
        self.done_at = 0.0


class _SessionState:
    __slots__ = (
        "sid", "pending_finals", "partial", "final_busy", "partial_busy",
        "closing", "submitted_finals", "submitted_partials",
        "decoded_finals", "decoded_partials", "dropped_finals",
        "superseded_partials", "expired_partials", "stale_partials", "errors",
        "audio_s", "decode_s",
        "final_latency", "partial_latency", "final_wait", "partial_wait",
    )

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.pending_finals: deque[AsrJob] = deque()
        self.partial: AsrJob | None = None
        self.final_busy = False
        self.partial_busy = False
        self.closing = False
        self.submitted_finals = 0
        self.submitted_partials = 0
        self.decoded_finals = 0
        self.decoded_partials = 0
        self.dropped_finals = 0
        self.superseded_partials = 0
        self.expired_partials = 0
        self.stale_partials = 0
        self.errors = 0
        self.audio_s = 0.0
        self.decode_s = 0.0
        self.final_latency = _Window()
        self.partial_latency = _Window()
        self.final_wait = _Window()
        self.partial_wait = _Window()


class AsrPool:
    """跨会话公平调度的 ASR 工作池。线程安全，可由任意采集线程提交任务。"""

    def __init__(self, engine: Any, *, workers: int | None = None,
                 max_pending_finals: int | None = None,
                 partial_ttl_s: float | None = None,
                 conn_factory=store.connect) -> None:
        from .config import LIVE_ASR_MAX_PENDING_FINALS, LIVE_PARTIAL_TTL_S, asr_workers

        self.engine = engine
        self.workers = max(1, int(workers)) if workers else asr_workers()
        self.max_pending_finals = max(
            1, int(max_pending_finals if max_pending_finals is not None
                   else LIVE_ASR_MAX_PENDING_FINALS))
        self.partial_ttl_s = max(
            0.1, float(partial_ttl_s if partial_ttl_s is not None else LIVE_PARTIAL_TTL_S))
        self._conn_factory = conn_factory
        self._cv = threading.Condition()
        self._states: dict[str, _SessionState] = {}
        self._order: list[str] = []
        self._rr = 0
        self._threads: list[threading.Thread] = []
        self._started = False
        self._stopping = False
        self._totals: dict[str, Any] = {
            "submitted_finals": 0, "submitted_partials": 0,
            "decoded_finals": 0, "decoded_partials": 0,
            "dropped_finals": 0, "superseded_partials": 0,
            "expired_partials": 0, "stale_partials": 0, "errors": 0,
            "audio_s": 0.0, "decode_s": 0.0,
            "final_latency": _Window(), "partial_latency": _Window(),
            "final_wait": _Window(), "partial_wait": _Window(),
        }

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._cv:
            if self._started:
                return
            self._started = True
            for i in range(self.workers):
                t = threading.Thread(target=self._worker, name="asr-%d" % i, daemon=True)
                self._threads.append(t)
                t.start()

    def shutdown(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        for t in list(self._threads):
            if t is not threading.current_thread():
                t.join(timeout=5)
        with self._cv:
            self._threads.clear()
            self._states.clear()
            self._order.clear()
            self._rr = 0
            self._started = False
            self._stopping = False

    def register(self, session: Any) -> None:
        """会话开始时登记；同一 id 重新登记会清掉上一次的 closing 标记。

        登记本身不启动工作线程（由调用方调用 ``start()``），这样调度器可以
        在测试里脱离真实模型单独验证。
        """
        with self._cv:
            sid = session.id
            st = self._states.get(sid)
            if st is None:
                st = _SessionState(sid)
                self._states[sid] = st
                self._order.append(sid)
            st.closing = False

    def unregister(self, session_id: str) -> None:
        with self._cv:
            self._states.pop(session_id, None)
            try:
                self._order.remove(session_id)
            except ValueError:
                pass
            self._rr = 0

    # ------------------------------------------------------------ 提交
    def submit_final(self, session: Any, samples: Any, *, start_sample: int,
                     seconds: float, embed: bool,
                     audio_end_wall: float | None = None) -> bool:
        """提交一条定稿。采样已经复制过，调用方可以立刻复用自己的缓冲区。"""
        job = AsrJob(FINAL, session, samples, start_sample=start_sample,
                     seconds=seconds, embed=embed, audio_end_wall=audio_end_wall)
        with self._cv:
            st = self._states.get(session.id)
            if st is None or st.closing:
                return False
            # 定稿已经切出来了，之前攒着的临时字幕同一段音频，全部作废
            if st.partial is not None:
                st.partial = None
                st.superseded_partials += 1
                self._totals["superseded_partials"] += 1
            if len(st.pending_finals) >= self.max_pending_finals:
                st.pending_finals.popleft()
                st.dropped_finals += 1
                self._totals["dropped_finals"] += 1
            st.pending_finals.append(job)
            st.submitted_finals += 1
            self._totals["submitted_finals"] += 1
            self._cv.notify()
        return True

    def submit_partial(self, session: Any, samples: Any, *, t0: float,
                       seconds: float, epoch: int,
                       audio_end_wall: float | None = None) -> bool:
        """提交一帧临时字幕；同会话只保留最新一帧，排队过久会自动过期。"""
        now = time.monotonic()
        job = AsrJob(PARTIAL, session, samples, t0=t0, seconds=seconds,
                     epoch=epoch, audio_end_wall=audio_end_wall,
                     expires_at=now + self.partial_ttl_s)
        with self._cv:
            st = self._states.get(session.id)
            if st is None or st.closing:
                return False
            # 定稿马上要来了/正在跑：临时字幕必然作废，不再占用解码器
            if st.final_busy or st.pending_finals:
                return False
            if st.partial is not None:
                st.superseded_partials += 1
                self._totals["superseded_partials"] += 1
            st.partial = job
            st.submitted_partials += 1
            self._totals["submitted_partials"] += 1
            self._cv.notify()
        return True

    # ------------------------------------------------------------ 收尾
    def drain_session(self, session_id: str, timeout: float = 60.0) -> dict[str, Any]:
        """等待某会话的定稿全部跑完；超时则丢掉剩余排队项并计数。

        调用前采集线程必须已经停止提交新任务。返回时若 ``in_flight`` 仍为真，
        说明有一条解码超过了 timeout，调用方应把会话标记为丢弃，避免迟到的
        结果写进已经收尾/删除的场次。
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cv:
            st = self._states.get(session_id)
            if st is None:
                return {"pending_left": 0, "in_flight": False, "dropped": 0}
            st.closing = True
            if st.partial is not None:
                st.partial = None
                self._totals["superseded_partials"] += 1
            while (st.pending_finals or st.final_busy) and time.monotonic() < deadline:
                self._cv.wait(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
            left = len(st.pending_finals)
            in_flight = st.final_busy
            if left:
                st.pending_finals.clear()
                st.dropped_finals += left
                self._totals["dropped_finals"] += left
            return {"pending_left": left, "in_flight": in_flight,
                    "dropped": st.dropped_finals}

    # ------------------------------------------------------------ 调度
    def _pick_locked(self) -> AsrJob | None:
        """跨会话轮转：每个会话每轮最多领一个任务；会话内定稿优先。"""
        n = len(self._order)
        now = time.monotonic()
        for _ in range(n):
            sid = self._order[self._rr % n]
            self._rr += 1
            st = self._states.get(sid)
            if st is None:
                continue
            if st.final_busy:
                continue                      # 同会话定稿严格串行，保证落库顺序
            if st.pending_finals:
                job = st.pending_finals.popleft()
                st.final_busy = True
                return job
            if st.closing:                    # 收尾中的会话不再出新临时字幕
                continue
            if st.partial is not None and not st.partial_busy:
                job = st.partial
                st.partial = None
                if job.expires_at is not None and now > job.expires_at:
                    st.expired_partials += 1
                    self._totals["expired_partials"] += 1
                    continue
                st.partial_busy = True
                return job
        return None

    def _take(self) -> AsrJob | None:
        with self._cv:
            while not self._stopping:
                job = self._pick_locked()
                if job is not None:
                    job.started_at = time.monotonic()
                    return job
                self._cv.wait(timeout=0.25)
        return None

    def _worker(self) -> None:
        conn = None
        try:
            while True:
                job = self._take()
                if job is None:
                    break
                # sqlite 连接跟随 worker 线程，只有定稿才需要落库
                if job.kind == FINAL and conn is None and self._conn_factory is not None:
                    try:
                        conn = self._conn_factory()
                    except Exception:
                        traceback.print_exc()
                self._run_one(job, conn)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _run_one(self, job: AsrJob, conn) -> None:
        outcome: Any = False
        decode_s = 0.0
        error: str | None = None
        try:
            t0 = time.monotonic()
            text = self.engine.transcribe(job.samples)
            decode_s = time.monotonic() - t0
            if job.kind == FINAL:
                emb = None
                if job.embed:
                    try:
                        emb = self.engine.embed(job.samples)
                    except Exception:
                        traceback.print_exc()
                outcome = bool(job.session.commit_final(job, text, emb, conn))
            else:
                outcome = job.session.apply_partial(job, text)
        except Exception as e:
            error = "%s: %s" % (type(e).__name__, e)
            traceback.print_exc()
        finally:
            self._finish(job, outcome, decode_s, time.monotonic(), error)

    def _finish(self, job: AsrJob, outcome: Any, decode_s: float,
                done: float, error: str | None) -> None:
        job.done_at = done
        wait = max(0.0, job.started_at - job.submitted_at)
        committed = outcome == "ok" if job.kind == PARTIAL else bool(outcome)
        with self._cv:
            st = self._states.get(job.session.id)
            if st is not None:
                if job.kind == FINAL:
                    st.final_busy = False
                else:
                    st.partial_busy = False
            if error is not None:
                self._totals["errors"] += 1
                if st is not None:
                    st.errors += 1
            else:
                self._totals["audio_s"] += job.seconds
                self._totals["decode_s"] += decode_s
                if st is not None:
                    st.audio_s += job.seconds
                    st.decode_s += decode_s
                if job.kind == FINAL:
                    self._totals["decoded_finals"] += 1
                    self._totals["final_wait"].add(wait)
                    if committed and job.audio_end_wall is not None:
                        self._totals["final_latency"].add(done - job.audio_end_wall)
                    if st is not None:
                        st.decoded_finals += 1
                        st.final_wait.add(wait)
                        if committed and job.audio_end_wall is not None:
                            st.final_latency.add(done - job.audio_end_wall)
                else:
                    self._totals["decoded_partials"] += 1
                    self._totals["partial_wait"].add(wait)
                    if committed and job.audio_end_wall is not None:
                        self._totals["partial_latency"].add(done - job.audio_end_wall)
                    if st is not None:
                        st.decoded_partials += 1
                        st.partial_wait.add(wait)
                        if committed and job.audio_end_wall is not None:
                            st.partial_latency.add(done - job.audio_end_wall)
                    if outcome == "stale":
                        self._totals["stale_partials"] += 1
                        if st is not None:
                            st.stale_partials += 1
            self._cv.notify_all()

    # ------------------------------------------------------------ 查询
    def reset_stats(self) -> None:
        """清空计数与延迟窗口（压测在预热结束后调用），不影响正在跑的任务。"""
        with self._cv:
            for st in self._states.values():
                st.submitted_finals = 0
                st.submitted_partials = 0
                st.decoded_finals = 0
                st.decoded_partials = 0
                st.dropped_finals = 0
                st.superseded_partials = 0
                st.expired_partials = 0
                st.stale_partials = 0
                st.errors = 0
                st.audio_s = 0.0
                st.decode_s = 0.0
                st.final_latency = _Window()
                st.partial_latency = _Window()
                st.final_wait = _Window()
                st.partial_wait = _Window()
            self._totals = {
                "submitted_finals": 0, "submitted_partials": 0,
                "decoded_finals": 0, "decoded_partials": 0,
                "dropped_finals": 0, "superseded_partials": 0,
                "expired_partials": 0, "stale_partials": 0, "errors": 0,
                "audio_s": 0.0, "decode_s": 0.0,
                "final_latency": _Window(), "partial_latency": _Window(),
                "final_wait": _Window(), "partial_wait": _Window(),
            }

    def stats(self) -> dict[str, Any]:
        with self._cv:
            t = self._totals
            states = list(self._states.values())
            pending_finals = sum(len(st.pending_finals) for st in states)
            pending_partials = sum(1 for st in states if st.partial is not None)
            busy = sum(1 for st in states if st.final_busy or st.partial_busy)
            audio_s = float(t["audio_s"])
            return {
                "workers": self.workers,
                "sessions": len(states),
                "busy": busy,
                "pending_finals": pending_finals,
                "pending_partials": pending_partials,
                "submitted_finals": t["submitted_finals"],
                "submitted_partials": t["submitted_partials"],
                "decoded_finals": t["decoded_finals"],
                "decoded_partials": t["decoded_partials"],
                "dropped_finals": t["dropped_finals"],
                "superseded_partials": t["superseded_partials"],
                "expired_partials": t["expired_partials"],
                "stale_partials": t["stale_partials"],
                "errors": t["errors"],
                "audio_s": round(audio_s, 2),
                "decode_s": round(float(t["decode_s"]), 2),
                "rtf": round(float(t["decode_s"]) / audio_s, 4) if audio_s else 0.0,
                "final_latency": t["final_latency"].snapshot(),
                "partial_latency": t["partial_latency"].snapshot(),
                "final_wait": t["final_wait"].snapshot(),
                "partial_wait": t["partial_wait"].snapshot(),
            }

    def session_stats(self, session_id: str) -> dict[str, Any]:
        with self._cv:
            st = self._states.get(session_id)
            if st is None:
                return {}
            audio_s = float(st.audio_s)
            return {
                "pending_finals": len(st.pending_finals),
                "partial_pending": st.partial is not None,
                "final_busy": st.final_busy,
                "partial_busy": st.partial_busy,
                "submitted_finals": st.submitted_finals,
                "submitted_partials": st.submitted_partials,
                "decoded_finals": st.decoded_finals,
                "decoded_partials": st.decoded_partials,
                "dropped_finals": st.dropped_finals,
                "superseded_partials": st.superseded_partials,
                "expired_partials": st.expired_partials,
                "stale_partials": st.stale_partials,
                "errors": st.errors,
                "audio_s": round(audio_s, 2),
                "decode_s": round(float(st.decode_s), 2),
                "rtf": round(float(st.decode_s) / audio_s, 4) if audio_s else 0.0,
                "final_latency": st.final_latency.snapshot(),
                "partial_latency": st.partial_latency.snapshot(),
                "final_wait": st.final_wait.snapshot(),
                "partial_wait": st.partial_wait.snapshot(),
            }
