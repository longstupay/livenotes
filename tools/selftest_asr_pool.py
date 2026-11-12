# -*- coding: utf-8 -*-
"""ASR 工作池的确定性自测（不需要模型/音频，几秒跑完）。

    python tools/selftest_asr_pool.py

覆盖：
- 同一会话定稿严格按提交顺序执行；
- 跨会话轮转，一路连续说话不会饿死其他路；
- 定价优先于临时字幕；
- 临时字幕只保留最新一帧，旧帧计入 superseded；
- 临时字幕超过 TTL 直接过期；
- 单会话定稿队列有界，超了丢最旧；
- drain_session 会等排队的定稿跑完（收尾时不丢最后几句）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.asr_pool import AsrPool  # noqa: E402


class FakeEngine:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def transcribe(self, samples) -> str:
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return "测试文本"

    def new_embedder(self):
        return object()

    def embed(self, samples, extractor=None):
        return None


class FakeSession:
    def __init__(self, sid: str) -> None:
        self.id = sid
        self.log: list[tuple[str, int]] = []
        self.lock = threading.Lock()
        self.epoch = 0

    def commit_final(self, job, text, emb, conn) -> bool:
        with self.lock:
            self.log.append(("final", job.start_sample))
        return True

    def apply_partial(self, job, text) -> str:
        with self.lock:
            if job.epoch != self.epoch:
                return "stale"
            self.log.append(("partial", job.epoch))
        return "ok"

    def finals(self) -> list[int]:
        with self.lock:
            return [v for k, v in self.log if k == "final"]


def _complete(pool: AsrPool, sid: str) -> None:
    """模拟 worker 跑完一条定稿，让调度器可以继续给同一会话派活。"""
    with pool._cv:
        pool._states[sid].final_busy = False


def _pick(pool: AsrPool):
    with pool._cv:
        return pool._pick_locked()


def test_round_robin() -> None:
    pool = AsrPool(FakeEngine(), workers=1, conn_factory=None)
    a, b = FakeSession("A"), FakeSession("B")
    pool.register(a)
    pool.register(b)
    for i in range(3):
        pool.submit_final(a, b"x", start_sample=i, seconds=1, embed=False)
    pool.submit_final(b, b"x", start_sample=10, seconds=1, embed=False)
    got = []
    for _ in range(4):
        job = _pick(pool)
        assert job is not None
        got.append(job.session.id)
        _complete(pool, job.session.id)
    assert got == ["A", "B", "A", "A"], got


def test_final_before_partial() -> None:
    pool = AsrPool(FakeEngine(), workers=1, conn_factory=None)
    s = FakeSession("S")
    pool.register(s)
    pool.submit_partial(s, b"x", t0=0, seconds=1, epoch=0)
    pool.submit_final(s, b"x", start_sample=2, seconds=1, embed=False)
    job = _pick(pool)
    assert job.kind == "final", job.kind
    _complete(pool, "S")
    job = _pick(pool)
    assert job is None              # 定稿入队时已经把旧的临时字幕作废
    assert pool.session_stats("S")["superseded_partials"] == 1


def test_partial_coalesce() -> None:
    pool = AsrPool(FakeEngine(), workers=1, conn_factory=None)
    s = FakeSession("S")
    pool.register(s)
    for i in range(3):
        pool.submit_partial(s, b"x", t0=i, seconds=1, epoch=0)
    job = _pick(pool)
    assert job is not None and job.t0 == 2.0, job.t0
    assert pool.session_stats("S")["superseded_partials"] == 2


def test_partial_ttl() -> None:
    pool = AsrPool(FakeEngine(), workers=1, conn_factory=None, partial_ttl_s=0.1)
    s = FakeSession("S")
    pool.register(s)
    pool.submit_partial(s, b"x", t0=0, seconds=1, epoch=0)
    time.sleep(0.15)
    assert _pick(pool) is None
    assert pool.session_stats("S")["expired_partials"] == 1


def test_overflow_drops_oldest() -> None:
    pool = AsrPool(FakeEngine(), workers=1, conn_factory=None, max_pending_finals=2)
    s = FakeSession("S")
    pool.register(s)
    for i in range(3):
        pool.submit_final(s, b"x", start_sample=i, seconds=1, embed=False)
    stats = pool.session_stats("S")
    assert stats["pending_finals"] == 2, stats
    assert stats["dropped_finals"] == 1, stats
    with pool._cv:
        starts = [j.start_sample for j in pool._states["S"].pending_finals]
    assert starts == [1, 2], starts


def test_drain_waits_for_pending() -> None:
    engine = FakeEngine(delay=0.03)
    pool = AsrPool(engine, workers=2, conn_factory=None)
    s = FakeSession("S")
    pool.register(s)
    pool.start()
    for i in range(5):
        pool.submit_final(s, b"x", start_sample=i, seconds=1, embed=False)
    out = pool.drain_session("S", timeout=10)
    assert out["pending_left"] == 0 and not out["in_flight"], out
    assert s.finals() == [0, 1, 2, 3, 4], s.finals()
    pool.shutdown()


def test_ordering_under_load() -> None:
    engine = FakeEngine(delay=0.005)
    pool = AsrPool(engine, workers=4, conn_factory=None)
    sessions = [FakeSession("S%d" % i) for i in range(10)]
    for s in sessions:
        pool.register(s)
    pool.start()
    for i in range(8):
        for s in sessions:
            pool.submit_final(s, b"x", start_sample=i, seconds=1, embed=False)
    for s in sessions:
        out = pool.drain_session(s.id, timeout=20)
        assert out["pending_left"] == 0, out
        assert s.finals() == list(range(8)), (s.id, s.finals())
    pool.shutdown()


def main() -> int:
    tests = [
        test_round_robin,
        test_final_before_partial,
        test_partial_coalesce,
        test_partial_ttl,
        test_overflow_drops_oldest,
        test_drain_waits_for_pending,
        test_ordering_under_load,
    ]
    for t in tests:
        t()
        print("[ok] %s" % t.__name__)
    print("\nASR 工作池自测通过：%d 项" % len(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main())
