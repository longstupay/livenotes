# -*- coding: utf-8 -*-
"""任务层的指纹契约：同一份稿子/同一组筛选只产生一次调用。"""
from __future__ import annotations

import sqlite3
import unittest

from app import analysis, analysis_job, scheduler, tasks  # noqa: F401


class FilterTest(unittest.TestCase):
    def test_normalize_is_order_and_whitespace_insensitive(self):
        a = tasks.normalize_filters({"categories": ["b", "a"], "speakers": ["y", "x"],
                                     "q": "  hi "})
        b = tasks.normalize_filters({"categories": ["a", "b"], "speakers": ["x", "y"],
                                     "q": "hi"})
        self.assertEqual(a, b)
        self.assertTrue(a["exclude_low_value"])
        self.assertTrue(a["exclude_overlap"])

    def test_explicit_false_is_kept(self):
        f = tasks.normalize_filters({"exclude_low_value": False, "exclude_overlap": False})
        self.assertFalse(f["exclude_low_value"])
        self.assertFalse(f["exclude_overlap"])


class SummaryFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.s = scheduler.Scheduler(persist=False)

    def test_auto_and_user_summary_share_fingerprint(self):
        # UI 默认 exOvl=false，下播收尾用 true；build_scope 反正会强制剔除重叠，
        # 两者送给模型的内容相同，必须命中同一个指纹。
        auto_filters = {"t_from": None, "t_to": None, "categories": [], "speakers": [],
                        "q": "", "exclude_low_value": True, "exclude_overlap": True,
                        "min_conf": None}
        user_filters = dict(auto_filters, exclude_overlap=False, min_conf=0.9)
        auto = tasks.summary_payload(auto_filters, "content", "auto", "digest-1", auto=True)
        user = tasks.summary_payload(user_filters, "content", "auto", "digest-1", auto=False)
        fp_auto = self.s.fingerprint("summary", "LIVE_1",
                                     tasks.fingerprint_payload(auto))
        fp_user = self.s.fingerprint("summary", "LIVE_1",
                                     tasks.fingerprint_payload(user))
        self.assertEqual(fp_auto, fp_user)

    def test_low_value_choice_changes_fingerprint(self):
        filters = {"categories": [], "speakers": [], "q": "",
                   "exclude_low_value": True}
        keep = tasks.summary_payload(filters, "content", "auto", "d1")
        drop = tasks.summary_payload(dict(filters, exclude_low_value=False),
                                     "content", "auto", "d1")
        fp = lambda p: self.s.fingerprint("summary", "LIVE_1",  # noqa: E731
                                          tasks.fingerprint_payload(p))
        self.assertNotEqual(fp(keep), fp(drop))

    def test_different_content_or_filter_changes_fingerprint(self):
        filters = {"categories": [], "speakers": [], "q": ""}
        base = tasks.summary_payload(filters, "content", "auto", "d1")
        other_digest = tasks.summary_payload(filters, "content", "auto", "d2")
        other_filter = tasks.summary_payload({**filters, "q": "工资"},
                                             "content", "auto", "d1")
        fp = lambda p: self.s.fingerprint("summary", "LIVE_1", p)  # noqa: E731
        self.assertNotEqual(fp(base), fp(other_digest))
        self.assertNotEqual(fp(base), fp(other_filter))


class SessionDigestTest(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE utterances (id INTEGER, session_id TEXT, speaker_id TEXT, "
            "speaker_label TEXT, category TEXT, value_tag TEXT, overlap INTEGER, "
            "asr_conf REAL, topic TEXT, text TEXT)")
        conn.execute("INSERT INTO utterances VALUES "
                     "(1,'S','SPK_00','小王','substantive','normal',0,0.9,'','你好')")
        return conn

    def test_digest_changes_when_content_changes(self):
        conn = self._conn()
        d1 = analysis.session_digest(conn, "S")
        self.assertEqual(d1, analysis.session_digest(conn, "S"))
        conn.execute("INSERT INTO utterances VALUES "
                     "(2,'S','SPK_00','小王','substantive','normal',0,0.9,'','再见')")
        self.assertNotEqual(d1, analysis.session_digest(conn, "S"))

    def test_digest_changes_when_category_changes(self):
        conn = self._conn()
        d1 = analysis.session_digest(conn, "S")
        conn.execute("UPDATE utterances SET category='chitchat' WHERE id=1")
        self.assertNotEqual(d1, analysis.session_digest(conn, "S"))

    def test_digest_changes_when_label_or_topic_changes(self):
        # 显示名和话题都直接进模型输入，必须让指纹失效
        conn = self._conn()
        d1 = analysis.session_digest(conn, "S")
        conn.execute("UPDATE utterances SET speaker_label='小李' WHERE id=1")
        d2 = analysis.session_digest(conn, "S")
        self.assertNotEqual(d1, d2)
        conn.execute("UPDATE utterances SET topic='价格' WHERE id=1")
        self.assertNotEqual(d2, analysis.session_digest(conn, "S"))


class ClassifyFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.s = scheduler.Scheduler(persist=False)

    def _fp(self, payload):
        return self.s.fingerprint("classify", "LIVE_1",
                                  tasks.classify_fingerprint_payload(payload))

    def test_digest_is_ignored_so_inflight_clicks_join(self):
        # 分类会改 category，category 又进内容摘要；摘要若进指纹，
        # 任务一跑起来第二次点击就匹配不上正在跑的那一片了。
        base = {"offset": 0, "limit": 40, "topics": ["a"], "digest": "d1",
                "prompt": "v1"}
        self.assertEqual(self._fp(base), self._fp(dict(base, digest="d2")))

    def test_offset_or_topics_still_change_fingerprint(self):
        base = {"offset": 0, "limit": 40, "topics": ["a"], "digest": "d1",
                "prompt": "v1"}
        self.assertNotEqual(self._fp(base), self._fp(dict(base, offset=40)))
        self.assertNotEqual(self._fp(base), self._fp(dict(base, topics=["b"])))


class RegistrationTest(unittest.TestCase):
    def test_business_handlers_are_registered(self):
        kinds = set(scheduler.SCHEDULER._handlers)
        self.assertTrue({"summary", "polish", "classify", "analysis_run"} <= kinds)


if __name__ == "__main__":
    unittest.main()
