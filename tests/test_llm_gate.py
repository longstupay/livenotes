# -*- coding: utf-8 -*-
"""llm.chat 的闸门：并发槽、指数退避、可重试判定、预算预占/结算。"""
from __future__ import annotations

import random
import unittest
from unittest import mock

from app import llm, scheduler


def _cfg() -> dict:
    return {"base": "http://example.invalid/v1", "key": "k", "model": "test-model",
            "temperature": 0.2, "timeout": 5, "configured": True, "source": "test"}


def _sched(**kw):
    opts = dict(concurrency=1, max_attempts=3, backoff_base=0.0, backoff_max=0.0,
                session_budget=0, daily_budget=0, expected_output_tokens=10,
                persist=False)
    opts.update(kw)
    return scheduler.Scheduler(**opts)


def _chat(s, fake, *, usage=None, session_id="S", max_attempts=None):
    with mock.patch.object(llm._scheduler, "SCHEDULER", s), \
         mock.patch.object(llm.settings, "resolve", return_value=_cfg()), \
         mock.patch.object(llm, "_post_chat", side_effect=fake):
        return llm.chat([{"role": "user", "content": "ping"}],
                        session_id=session_id, task="test", usage=usage,
                        max_attempts=max_attempts)


class RetryTest(unittest.TestCase):
    def test_retryable_error_retries_then_succeeds(self):
        s = _sched()
        calls = []

        def fake(cfg, payload, timeout, total_timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise llm.LLMError("HTTP 429", status=429, retryable=True)
            return {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                              "total_tokens": 12}}

        usage = {}
        self.assertEqual(_chat(s, fake, usage=usage), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(usage["total_tokens"], 12)
        self.assertEqual(s.budget_status("S")["session"]["used"], 12)
        s.shutdown()

    def test_non_retryable_stops_immediately(self):
        s = _sched()
        calls = []

        def fake(cfg, payload, timeout, total_timeout=None):
            calls.append(1)
            raise llm.LLMError("HTTP 401 bad key", status=401, retryable=False)

        with self.assertRaises(llm.LLMError):
            _chat(s, fake)
        self.assertEqual(len(calls), 1)
        s.shutdown()

    def test_retry_after_is_respected(self):
        s = _sched()
        calls = []

        def fake(cfg, payload, timeout, total_timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise llm.LLMError("HTTP 429", status=429, retryable=True,
                                   retry_after=0.01)
            return {"choices": [{"message": {"content": "ok"}}]}

        self.assertEqual(_chat(s, fake), "ok")
        self.assertEqual(len(calls), 2)
        s.shutdown()

    def test_usage_falls_back_when_provider_omits_it(self):
        s = _sched()
        usage = {}

        def fake(cfg, payload, timeout, total_timeout=None):
            return {"choices": [{"message": {"content": "你好世界"}}]}

        self.assertEqual(_chat(s, fake, usage=usage), "你好世界")
        self.assertGreater(usage["total_tokens"], 0)
        self.assertEqual(s.budget_status("S")["session"]["used"],
                         usage["total_tokens"])
        s.shutdown()


class BudgetGateTest(unittest.TestCase):
    def test_budget_exceeded_never_sends_request(self):
        s = _sched(session_budget=5)
        calls = []

        def fake(cfg, payload, timeout, total_timeout=None):
            calls.append(1)
            return {"choices": [{"message": {"content": "ok"}}]}

        with self.assertRaises(scheduler.BudgetExceeded):
            _chat(s, fake)
        self.assertEqual(calls, [])
        s.shutdown()

    def test_failed_call_releases_reservation(self):
        s = _sched()
        calls = []

        def fake(cfg, payload, timeout, total_timeout=None):
            calls.append(1)
            raise llm.LLMError("HTTP 401", status=401, retryable=False)

        with self.assertRaises(llm.LLMError):
            _chat(s, fake)
        self.assertEqual(s.budget_status("S")["session"]["reserved"], 0)
        self.assertEqual(s.budget_status("S")["session"]["used"], 0)
        s.shutdown()


class BackoffTest(unittest.TestCase):
    def test_exponential_with_jitter(self):
        random.seed(7)
        d1 = llm._retry_delay(1, base=1.0, cap=30.0)
        d3 = llm._retry_delay(3, base=1.0, cap=30.0)
        self.assertGreaterEqual(d1, 0.5)
        self.assertLessEqual(d1, 1.0)
        self.assertGreaterEqual(d3, 2.0)
        self.assertLessEqual(d3, 4.0)
        self.assertEqual(llm._retry_delay(1, retry_after=7, base=1, cap=30), 7)
        self.assertEqual(llm._retry_delay(1, retry_after=99, base=1, cap=30), 30)
        self.assertEqual(llm._retry_delay(1, base=0, cap=0), 0.0)

    def test_token_estimate_handles_cjk(self):
        self.assertGreater(llm.estimate_tokens("这是一段中文"), 3)
        self.assertGreater(llm.estimate_messages(
            [{"role": "user", "content": "你好"}]), 0)


if __name__ == "__main__":
    unittest.main()
