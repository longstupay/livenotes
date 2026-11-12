# -*- coding: utf-8 -*-
"""周报/复盘：范围解析、多场汇总、兜底素材、飞书云文档降级。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from helpers import add_rows, add_session, utt_row

import tools.feishu_bot as fb
from app import feishu, report, store

WHOLE = {"categories": [], "speakers": [], "q": "", "min_conf": None,
         "exclude_low_value": True, "exclude_overlap": True}


@pytest.mark.parametrize("text,since,until", [
    ("", date(2026, 11, 3), date(2026, 11, 9)),
    ("本周", date(2026, 11, 9), date(2026, 11, 9)),
    ("上周", date(2026, 11, 2), date(2026, 11, 8)),
    ("今天", date(2026, 11, 9), date(2026, 11, 9)),
    ("7天", date(2026, 11, 3), date(2026, 11, 9)),
    ("最近3天", date(2026, 11, 7), date(2026, 11, 9)),
    ("11-03~11-09", date(2026, 11, 3), date(2026, 11, 9)),
    ("2026-11-01", date(2026, 11, 1), date(2026, 11, 1)),
])
def test_parse_range(text, since, until):
    r = report.parse_range(text, today=date(2026, 11, 9))
    assert r["since"] == since and r["until"] == until


@pytest.mark.parametrize("text", ["新房间", "11-03-09"])
def test_parse_range_rejects_junk(text):
    with pytest.raises(ValueError):
        report.parse_range(text, today=date(2026, 11, 9))


def _summary(overview: str, title: str, gist: str, quote: str,
             topics: list[str]) -> dict:
    return {
        "overview": overview,
        "topics": topics,
        "points": [{
            "title": title, "gist": gist, "t_from": 120.0, "t_to": 180.0,
            "evidence": [{"id": 1, "t_start": 130.0, "t_end": 132.0,
                          "speaker_label": "主播", "quote": quote}],
        }],
    }


def _seed(db):
    add_session(db, "S1", title="一号场", started_at="2026-11-05T20:00:00",
                duration_s=3600)
    add_rows(db, "S1", [utt_row(0, "场地一的第一句"), utt_row(1, "场地一的第二句")])
    store.set_minutes_url(db, "S1", "https://x.feishu.cn/minutes/abc")
    store.save_summary(db, "S1", "content", WHOLE, "llm(test)",
                       _summary("聊了账号风控。", "风控收紧", "平台查得严",
                                "平台最近查得很严", ["账号风控", "投放"]), "")
    # 后写一条筛选口径的总结：周报不能拿它当整场结论。
    store.save_summary(db, "S1", "content", {"q": "广告", "categories": [],
                                           "speakers": [], "t_from": 0,
                                           "t_to": 100}, "llm(test)",
                       _summary("这条不该出现。", "筛选要点", "不该出现",
                                "不该出现", ["广告"]), "")

    add_session(db, "S2", title="二号场", started_at="2026-11-06T21:00:00",
                duration_s=1800)
    add_rows(db, "S2", [
        utt_row(0, "今天主要讲投放模型的搭建和预算分配", topic="投放",
                importance=0.95),
        utt_row(1, "另一个低权重句子也顺便说一下", topic="闲聊", importance=0.2),
    ])

    add_session(db, "S3", title="范围外", started_at="2026-11-10T20:00:00",
                duration_s=600)
    add_rows(db, "S3", [utt_row(0, "这场在范围外")])
    add_session(db, "S4", title="没有字幕", started_at="2026-11-07T20:00:00",
                duration_s=600)
    add_session(db, "S5", title="已删除", started_at="2026-11-08T20:00:00",
                duration_s=600)
    add_rows(db, "S5", [utt_row(0, "这场已经进回收站")])
    store.trash_session(db, "S5")


def test_build_weekly_aggregates_and_avoids_lan_links(db, tmp_path, monkeypatch):
    monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / "reports")
    _seed(db)
    info = report.build(db, "weekly", date(2026, 11, 3), date(2026, 11, 9),
                        write=False)
    md = info["markdown"]
    assert info["stats"]["sessions"] == 2
    assert "直播周报" in md and "一号场" in md and "二号场" in md
    assert "账号风控" in md and "平台最近查得很严" in md
    assert "https://x.feishu.cn/minutes/abc" in md
    assert "reader?session=" not in md and "192.168." not in md
    assert "不该出现" not in md
    assert "本场没有已生成总结" in md          # S2 没有总结，走原句兜底
    assert "投放（2 场）" in md                  # 跨场重复话题
    assert "范围外" not in md and "已删除" not in md and "没有字幕" not in md
    assert "未回填妙记" in md
    assert info["path"] is None


def test_build_retro_keeps_human_sections(db, tmp_path, monkeypatch):
    monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / "reports")
    add_session(db, "S1", title="一号场", started_at="2026-11-05T20:00:00")
    add_rows(db, "S1", [utt_row(0, "一句可以进复盘的内容")])
    info = report.build(db, "retro", date(2026, 11, 3), date(2026, 11, 9),
                        write=False)
    md = info["markdown"]
    assert "直播复盘" in md
    assert "四、结论与行动" in md and "（待填）" in md
    assert "跨场观察" in md


def test_build_writes_markdown(db, tmp_path, monkeypatch):
    monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / "reports")
    add_session(db, "S1", title="一号场", started_at="2026-11-05T20:00:00")
    add_rows(db, "S1", [utt_row(0, "一句可以写进文件的内容")])
    info = report.build(db, "weekly", date(2026, 11, 3), date(2026, 11, 9))
    assert info["path"].is_file()
    assert info["path"].read_text(encoding="utf-8") == info["markdown"]
    assert info["path"].name.startswith("weekly_2026-11-03_2026-11-09")


def test_cleanup_removes_old_reports(tmp_path, monkeypatch):
    import os
    import time
    d = tmp_path / "reports"
    d.mkdir()
    old = d / "old.md"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    new = d / "new.md"
    new.write_text("x", encoding="utf-8")
    monkeypatch.setattr(report, "REPORTS_DIR", d)
    assert report.cleanup(30) == 1
    assert not old.exists() and new.exists()


# ---------------------------------------------------------------- 机器人

class _Bot:
    def __init__(self, doc=None):
        self.texts: list[tuple[str, str]] = []
        self.files: list[tuple[str, str]] = []
        self.docs: list[dict] = []
        self.doc = doc

    def send_text(self, text, open_id=""):
        self.texts.append((open_id, text)); return True

    def send_text_to_chat(self, chat_id, text):
        self.texts.append((chat_id, text)); return True

    def send_file(self, path, open_id=""):
        self.files.append((open_id, str(path))); return True

    def create_docx(self, title, markdown, share_with=""):
        self.docs.append({"title": title, "markdown": markdown,
                          "share_with": share_with})
        if isinstance(self.doc, Exception):
            raise self.doc
        return self.doc


def _run_worker(monkeypatch, bot, tmp_path, kind="weekly",
                since=date(2026, 11, 3), until=date(2026, 11, 9)):
    monkeypatch.setattr(fb, "_BOT", bot, raising=False)
    monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / "reports")
    fb._report_worker(kind, since, until, "ou_1")


def test_report_worker_prefers_doc(db, tmp_path, monkeypatch):
    _seed(db)
    bot = _Bot(doc={"ok": True, "url": "https://x.feishu.cn/docx/doc1",
                    "shared": True})
    _run_worker(monkeypatch, bot, tmp_path)
    text = "\n".join(t for _, t in bot.texts)
    assert "https://x.feishu.cn/docx/doc1" in text
    assert "已授权给你" in text
    assert not bot.files
    # 发起人 open_id 要传给 create_docx，否则应用建的文档用户打不开。
    assert bot.docs and bot.docs[0]["share_with"] == "ou_1"


def test_report_worker_reports_share_failure(db, tmp_path, monkeypatch):
    _seed(db)
    bot = _Bot(doc={"ok": True, "url": "https://x.feishu.cn/docx/doc1",
                    "shared": False, "share_requested": True,
                    "share_error": "code=1061004 forbidden"})
    _run_worker(monkeypatch, bot, tmp_path)
    text = "\n".join(t for _, t in bot.texts)
    assert "自动授权失败" in text and "1061004" in text
    assert "https://x.feishu.cn/docx/doc1" in text


def test_report_worker_falls_back_to_file(db, tmp_path, monkeypatch):
    _seed(db)
    bot = _Bot(doc={"ok": False, "error": "code=99991672 no permission"})
    _run_worker(monkeypatch, bot, tmp_path)
    text = "\n".join(t for _, t in bot.texts)
    assert "没能直接建飞书云文档" in text and "no permission" in text
    assert bot.files and bot.files[0][1].endswith(".md")


def test_report_worker_keeps_partial_doc_link(db, tmp_path, monkeypatch):
    """文档建好但正文写入失败时，别把已经建出来的文档链接丢掉。"""
    _seed(db)
    bot = _Bot(doc={"ok": False, "error": "写入正文失败",
                    "url": "https://x.feishu.cn/docx/doc1",
                    "shared": True, "share_requested": True})
    _run_worker(monkeypatch, bot, tmp_path)
    text = "\n".join(t for _, t in bot.texts)
    assert "https://x.feishu.cn/docx/doc1" in text
    assert "飞书云文档已建好" in text and "已授权给你" in text
    assert bot.files


def test_report_worker_no_sessions(db, tmp_path, monkeypatch):
    bot = _Bot(doc={"ok": True, "url": "x"})
    _run_worker(monkeypatch, bot, tmp_path, since=date(2026, 1, 1),
                until=date(2026, 1, 2))
    assert "没有可汇总的场次" in bot.texts[-1][1]
    assert not bot.files


def test_route_weekly_and_retro(monkeypatch):
    calls = []

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self.target, self.args, self.kwargs = target, args, kwargs or {}

        def start(self):
            calls.append((self.target, self.args))

    monkeypatch.setattr(fb, "threading", SimpleNamespace(Thread=FakeThread))
    monkeypatch.setattr(fb, "_send_text_target", lambda *a, **k: True)

    assert fb._route("/周报 上周", "/周报 上周") == ""
    assert calls and calls[-1][0] is fb._report_worker
    assert calls[-1][1][0] == "weekly"
    assert isinstance(calls[-1][1][1], date)
    assert fb._route("/复盘 7天", "/复盘 7天") == ""
    assert calls[-1][1][0] == "retro"
    assert "私聊" in fb._route("/周报", "/周报", chat_id="oc_1",
                              chat_type="group")


# ---------------------------------------------------------------- 云文档

class _Resp:
    def __init__(self, ok=True, code=0, msg="ok", data=None):
        self._ok, self.code, self.msg, self.data = ok, code, msg, data

    def success(self):
        return self._ok


def _fake_bot(monkeypatch, create_ok=True, share_ok=True, desc_ok=True):
    b = feishu.Bot.__new__(feishu.Bot)
    b.cfg = {"doc_url_base": "https://x.feishu.cn/docx/"}
    b.last_error = ""
    calls = {"desc": [], "share": []}

    class Doc:
        def convert(self, req):
            return _Resp(data=SimpleNamespace(
                blocks=[{"block_type": 2}], first_level_block_ids=["b1"]))

        def create(self, req):
            if not create_ok:
                return _Resp(False, 99991672, "no permission")
            return _Resp(data=SimpleNamespace(
                document=SimpleNamespace(document_id="doc1")))

    class Desc:
        def create(self, req):
            calls["desc"].append(req)
            if not desc_ok:
                return _Resp(False, 1770001, "invalid param")
            return _Resp(data=SimpleNamespace())

    class Perm:
        def create(self, req):
            calls["share"].append(req)
            if not share_ok:
                return _Resp(False, 1061004, "forbidden")
            return _Resp(data=SimpleNamespace())

    b.client = SimpleNamespace(
        docx=SimpleNamespace(v1=SimpleNamespace(
            document=Doc(), document_block_descendant=Desc())),
        drive=SimpleNamespace(v1=SimpleNamespace(permission_member=Perm())))
    return b, calls


def test_create_docx_builds_document(monkeypatch):
    b, calls = _fake_bot(monkeypatch)
    out = b.create_docx("周报", "# 标题\n\n正文")
    assert out["ok"] and out["url"] == "https://x.feishu.cn/docx/doc1"
    req = calls["desc"][0]
    assert req.document_id == "doc1" and req.block_id == "doc1"
    assert req.request_body.children_id == ["b1"]
    assert req.request_body.descendants == [{"block_type": 2}]
    assert not calls["share"]      # 没传 share_with 就不该调授权接口


def test_create_docx_grants_owner(monkeypatch):
    b, calls = _fake_bot(monkeypatch)
    out = b.create_docx("周报", "# 标题", share_with="ou_1")
    assert out["ok"] and out["shared"] and out["share_requested"]
    req = calls["share"][0]
    assert req.token == "doc1" and req.type == "docx"
    assert req.request_body.member_type == "openid"
    assert req.request_body.member_id == "ou_1"
    assert req.request_body.perm == "full_access"


def test_create_docx_share_failure_keeps_document(monkeypatch):
    b, _ = _fake_bot(monkeypatch, share_ok=False)
    out = b.create_docx("周报", "# 标题", share_with="ou_1")
    assert out["ok"] and not out["shared"]
    assert "1061004" in out["share_error"]
    assert out["url"] == "https://x.feishu.cn/docx/doc1"


def test_create_docx_partial_write_still_shares(monkeypatch):
    """正文写入失败也要授权，否则用户点开那个空文档还是无权限。"""
    b, calls = _fake_bot(monkeypatch, desc_ok=False)
    out = b.create_docx("周报", "# 标题", share_with="ou_1")
    assert not out["ok"] and out["url"] == "https://x.feishu.cn/docx/doc1"
    assert out["shared"] and out["share_requested"] and calls["share"]


def test_create_docx_permission_failure(monkeypatch):
    b, _ = _fake_bot(monkeypatch, create_ok=False)
    out = b.create_docx("周报", "# 标题")
    assert not out["ok"] and "99991672" in out["error"]


def test_create_docx_empty_markdown(monkeypatch):
    b, calls = _fake_bot(monkeypatch)
    out = b.create_docx("周报", "")
    assert not out["ok"] and not calls["desc"]
