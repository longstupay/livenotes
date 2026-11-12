# -*- coding: utf-8 -*-
"""全局调度器的核心不变量：并发上限、指纹去重、退避重试、预算、失败可重入。"""
from __future__ import annotations

import threading
import time
import unittest

from app import scheduler


def make(**kw):
    opts = dict(concurrency=2, max_attempts=1, backoff_base=0.0, backoff_max=0.0,
                session_budget=0, daily_budget=0, dedupe_ttl=60.0,
                expected_output_tokens=10, job_history=50, persist=False)
    opts.update(kw)
    return scheduler.Scheduler(**opts)


class ConcurrencyTest(unittest.TestCase):
    def test_never_exceeds_limit(self):
        s = make(concurrency=2)
        lock = threading.Lock()
        active = [0]
        peak = [0]

        def handler(sid, payload):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.08)
            with lock:
                active[0] -= 1
            return payload["n"]

        s.register("t", handler)
        jobs = [s.submit("t", "S", {"n": i}, cache_ttl=0) for i in range(6)]
        self.assertEqual([j.wait(5) for j in jobs], list(range(6)))
        self.assertEqual(peak[0], 2)
        s.shutdown()

    def test_priority_promotes_queued_job(self):
        s = make(concurrency=1)
        started = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def handler(sid, payload):
            name = payload["n"]
            if name == "block":
                started.set()
                release.wait(3)
            else:
                order.append(name)
            return name

        s.register("t", handler)
        blocker = s.submit("t", "S", {"n": "block"}, priority=1, cache_ttl=0)
        self.assertTrue(started.wait(2))
        low = s.submit("t", "S", {"n": "A"}, priority=5, cache_ttl=0)
        promoted = s.submit("t", "S", {"n": "A"}, priority=1, cache_ttl=0)
        self.assertIs(low, promoted)          # 同指纹，复用同一个任务
        after = s.submit("t", "S", {"n": "B"}, priority=5, cache_ttl=0)
        release.set()
        self.assertEqual(blocker.wait(3), "block")
        self.assertEqual(low.wait(3), "A")
        self.assertEqual(after.wait(3), "B")
        self.assertEqual(order, ["A", "B"])   # 被提升后先于 B
        s.shutdown()


class DedupeTest(unittest.TestCase):
    def test_inflight_same_fingerprint_runs_once(self):
        s = make(concurrency=1)
        started = threading.Event()
        release = threading.Event()
        calls = [0]

        def handler(sid, payload):
            calls[0] += 1
            started.set()
            release.wait(3)
            return "ok"

        s.register("t", handler)
        j1 = s.submit("t", "S", {"x": 1})
        self.assertTrue(started.wait(2))
        j2 = s.submit("t", "S", {"x": 1})
        self.assertIs(j1, j2)
        release.set()
        self.assertEqual(j1.wait(3), "ok")
        self.assertEqual(j2.wait(3), "ok")
        self.assertEqual(calls[0], 1)
        s.shutdown()

    def test_completed_result_is_cached(self):
        s = make(concurrency=1, dedupe_ttl=60)
        calls = [0]

        def handler(sid, payload):
            calls[0] += 1
            return {"calls": calls[0]}

        s.register("t", handler)
        j1 = s.submit("t", "S", {"x": 1})
        self.assertEqual(j1.wait(3), {"calls": 1})
        j2 = s.submit("t", "S", {"x": 1})
        self.assertTrue(j2.cached)
        self.assertEqual(j2.id, j1.id)
        self.assertEqual(j2.wait(3), {"calls": 1})
        self.assertEqual(calls[0], 1)
        s.shutdown()

    def test_force_bypasses_completed_cache(self):
        s = make(concurrency=1, dedupe_ttl=60)
        calls = [0]

        def handler(sid, payload):
            calls[0] += 1
            return calls[0]

        s.register("t", handler)
        self.assertEqual(s.submit("t", "S", {"x": 1}).wait(3), 1)
        j2 = s.submit("t", "S", {"x": 1}, force=True)
        self.assertFalse(j2.cached)
        self.assertEqual(j2.wait(3), 2)
        self.assertEqual(calls[0], 2)
        s.shutdown()

    def test_fingerprint_is_stable_and_sensitive(self):
        s = make()
        a = s.fingerprint("summary", "S1", {"b": [2, 3], "a": 1})
        b = s.fingerprint("summary", "S1", {"a": 1, "b": [2, 3]})
        self.assertEqual(a, b)
        self.assertNotEqual(a, s.fingerprint("summary", "S2", {"a": 1, "b": [2, 3]}))
        self.assertNotEqual(a, s.fingerprint("polish", "S1", {"a": 1, "b": [2, 3]}))
        self.assertNotEqual(a, s.fingerprint("summary", "S1", {"a": 1, "b": [2, 4]}))


class FailureTest(unittest.TestCase):
    def test_failed_job_can_be_resubmitted(self):
        s = make(concurrency=1)
        state = {"n": 0}

        def handler(sid, payload):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("boom")
            return "ok"

        s.register("t", handler)
        j1 = s.submit("t", "S", {"x": 1})
        with self.assertRaises(scheduler.JobFailed) as ctx:
            j1.wait(3)
        self.assertEqual(str(ctx.exception), "boom")
        j2 = s.submit("t", "S", {"x": 1})   # 失败不进缓存，也不占指纹
        self.assertEqual(j2.wait(3), "ok")
        self.assertEqual(state["n"], 2)
        s.shutdown()

    def test_retry_api(self):
        s = make(concurrency=1)
        state = {"n": 0}

        def handler(sid, payload):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("first")
            return "ok"

        s.register("t", handler)
        j1 = s.submit("t", "S", {"x": 1})
        with self.assertRaises(scheduler.JobFailed):
            j1.wait(3)
        j2 = s.retry(j1.id)
        self.assertEqual(j2.wait(3), "ok")
        self.assertEqual(state["n"], 2)
        s.shutdown()

    def test_wait_timeout(self):
        s = make(concurrency=1)
        release = threading.Event()

        def handler(sid, payload):
            release.wait(3)
            return "ok"

        s.register("t", handler)
        job = s.submit("t", "S", {"x": 1})
        with self.assertRaises(scheduler.JobTimeout):
            job.wait(0.05)
        release.set()
        self.assertEqual(job.wait(3), "ok")
        s.shutdown()


class BudgetTest(unittest.TestCase):
    def test_session_budget_reserves_before_call(self):
        s = make(session_budget=100)
        r = s.reserve("S1", "summary", 60, 20)      # 预占 80
        self.assertEqual(s.budget_status("S1")["session"]["reserved"], 80)
        with self.assertRaises(scheduler.BudgetExceeded) as ctx:
            s.reserve("S1", "summary", 30, 10)      # 80 + 40 > 100
        self.assertEqual(ctx.exception.scope, "session")

        s.settle(r, {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60})
        b = s.budget_status("S1")
        self.assertEqual(b["session"]["used"], 60)
        self.assertEqual(b["session"]["reserved"], 0)
        self.assertEqual(b["session"]["remaining"], 40)

        r2 = s.reserve("S1", "polish", 30, 5)       # 60 + 35 <= 100
        s.release(r2)
        with self.assertRaises(scheduler.BudgetExceeded):
            s.reserve("S1", "polish", 40, 5)        # 60 + 45 > 100
        s.shutdown()

    def test_daily_budget_is_global_across_sessions(self):
        s = make(daily_budget=100)
        r = s.reserve("A", "summary", 50, 10)       # 60
        with self.assertRaises(scheduler.BudgetExceeded) as ctx:
            s.reserve("B", "summary", 50, 10)       # 60 + 60 > 100
        self.assertEqual(ctx.exception.scope, "daily")
        s.release(r)
        r2 = s.reserve("B", "summary", 50, 10)      # 释放后可以
        s.release(r2)
        s.shutdown()

    def test_zero_budget_means_unlimited(self):
        s = make(session_budget=0, daily_budget=0)
        r = s.reserve("S", "summary", 10 ** 9, 10 ** 9)
        s.release(r)
        b = s.budget_status("S")
        self.assertTrue(b["session"]["unlimited"])
        self.assertTrue(b["daily"]["unlimited"])
        s.shutdown()


if __name__ == "__main__":
    unittest.main()
