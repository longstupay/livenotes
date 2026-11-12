# -*- coding: utf-8 -*-
"""AI 分析 run 必须走全局队列，而不是每条 run 起一个裸线程。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import analysis_job, scheduler, settings, store

SID = "LIVE_QUEUE"


def _cfg() -> dict:
    return {"base": "http://example.invalid/v1", "key": "k", "model": "test-model",
            "temperature": 0.2, "timeout": 5, "configured": True, "source": "test"}


class _FakeJob:
    def __init__(self, jid: str = "job_test") -> None:
        self.id = jid
        self.status = "queued"

    def public(self) -> dict:
        return {"id": self.id, "status": self.status}


class _FakeScheduler:
    def __init__(self) -> None:
        self.submitted: list[tuple] = []
        self.job = _FakeJob()

    def submit(self, kind, session_id, payload, **kwargs):
        self.submitted.append((kind, session_id, payload, kwargs))
        return self.job

    def get(self, job_id):
        return self.job if job_id == self.job.id else None


class AnalysisJobQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "test.db")
        for p in (
            mock.patch.object(store, "DB_PATH", self.db),
            mock.patch.object(settings, "resolve", return_value=_cfg()),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        store.init_db(self.conn)
        store.insert_session(self.conn, {
            "id": SID, "title": "测试场次", "started_at": "2026-11-05T10:00:00",
            "duration_s": 600, "source_mode": "mixed",
        })
        self.fake = _FakeScheduler()
        p = mock.patch.object(scheduler, "SCHEDULER", self.fake)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: analysis_job._RUN_JOBS.clear())
        self.addCleanup(lambda: analysis_job._EVENTS.clear())

    def test_start_queues_an_analysis_run_job(self):
        out = analysis_job.start(self.conn, SID, "full")
        self.assertFalse(out["already"])
        self.assertEqual(out["run"]["status"], "queued")
        self.assertEqual(out["job"]["id"], "job_test")
        self.assertEqual(len(self.fake.submitted), 1)
        kind, sid, payload, kwargs = self.fake.submitted[0]
        self.assertEqual(kind, "analysis_run")
        self.assertEqual(sid, SID)
        self.assertEqual(payload["run_id"], out["run"]["id"])
        self.assertEqual(payload["kind"], "full")
        self.assertEqual(kwargs["cache_ttl"], 0)
        self.assertEqual(analysis_job._RUN_JOBS[out["run"]["id"]], "job_test")

    def test_queued_run_is_not_marked_stale_while_job_alive(self):
        out = analysis_job.start(self.conn, SID, "full")
        run_id = out["run"]["id"]
        with mock.patch.object(analysis_job, "_is_stale", return_value=True):
            # job 还在排队：不能因为 10 分钟没进度就把 run 标成中断
            got = analysis_job.status(self.conn, SID, run_id)
            self.assertEqual(got["run"]["status"], "queued")
            # job 已结束且长时间没进度：这才是真的断了
            self.fake.job.status = "succeeded"
            got = analysis_job.status(self.conn, SID, run_id)
            self.assertEqual(got["run"]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
