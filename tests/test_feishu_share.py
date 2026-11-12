# -*- coding: utf-8 -*-
"""飞书片段分享：私聊直发、群聊确认、语音失败降级为文件。"""
from __future__ import annotations

import pytest

import tools.feishu_bot as fb
from helpers import add_session
from app import clips, store

_REAL_ALLOW = clips.allow


class ShareBot:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.chat_texts: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict]] = []
        self.chat_cards: list[tuple[str, dict]] = []
        self.audios: list[tuple[str, str, int]] = []
        self.chat_audios: list[tuple[str, str, int]] = []
        self.files: list[tuple[str, str]] = []
        self.chat_files: list[tuple[str, str]] = []
        self.audio_ok = True
        self.file_ok = True

    def send_text(self, text, open_id="") -> bool:
        self.texts.append((open_id, text)); return True

    def send_text_to_chat(self, chat_id, text) -> bool:
        self.chat_texts.append((chat_id, text)); return True

    def send_card(self, card, open_id="") -> bool:
        self.cards.append((open_id, card)); return True

    def send_card_to_chat(self, chat_id, card) -> bool:
        self.chat_cards.append((chat_id, card)); return True

    def send_audio(self, path, duration_ms, open_id="") -> bool:
        self.audios.append((open_id, str(path), int(duration_ms)))
        return self.audio_ok

    def send_audio_to_chat(self, chat_id, path, duration_ms) -> bool:
        self.chat_audios.append((chat_id, str(path), int(duration_ms)))
        return self.audio_ok

    def send_file(self, path, open_id="") -> bool:
        self.files.append((open_id, str(path)))
        return self.file_ok

    def send_file_to_chat(self, chat_id, path) -> bool:
        self.chat_files.append((chat_id, str(path)))
        return self.file_ok


def _room(rid="rm1", ch="CH01", sid="S1"):
    return {"id": rid, "seq": int(ch[-2:]), "state": "live", "name": "一号",
            "session_id": sid, "capture_engine": "direct"}


@pytest.fixture(autouse=True)
def clean():
    fb._SHARE_PENDING.clear()
    clips.reset_rate_limit()
    yield
    fb._SHARE_PENDING.clear()
    clips.reset_rate_limit()


@pytest.fixture
def bot(monkeypatch):
    b = ShareBot()
    monkeypatch.setattr(fb, "_BOT", b, raising=False)
    monkeypatch.setattr(fb, "_rooms", lambda: [_room()])
    monkeypatch.setattr(fb.clips, "allow", lambda sid, cooldown_s=30: (True, 0.0))
    return b


@pytest.fixture
def fake_clip(monkeypatch, tmp_path):
    p = tmp_path / "clip.opus"
    p.write_bytes(b"OggS")
    clip = clips.Clip(p, 0.5, 90.0, 600.0, "pcm")

    def _make(sid, start, duration, conn=None):
        return clip, ""

    monkeypatch.setattr(fb.clips, "make_clip", _make)
    return clip


def test_private_share_sends_header_and_audio(bot, fake_clip):
    fb._share_worker(_room(), "S1", None, 90, "u1", "", "p2p")
    assert bot.texts and "🎧" in bot.texts[0][1]
    assert bot.audios and bot.audios[0][1].endswith(".opus")
    assert bot.audios[0][2] == 90000


def test_group_share_requires_confirm(bot, fake_clip):
    assert fb.cmd_share("CH01 90秒", "u1", "c1", "group") == ""
    assert bot.chat_cards and not bot.chat_audios
    nonce = next(iter(fb._SHARE_PENDING))
    fb._share_action("share_confirm", {"nonce": nonce}, "u1", "c1", "group")
    assert bot.chat_audios and bot.chat_audios[0][0] == "c1"


def test_group_cancel_drops_audio(bot, fake_clip):
    fb.cmd_share("CH01 90秒", "u1", "c1", "group")
    nonce = next(iter(fb._SHARE_PENDING))
    fb._share_action("share_cancel", {"nonce": nonce}, "u1", "c1", "group")
    assert not bot.chat_audios
    assert not fb._SHARE_PENDING


def test_audio_upload_falls_back_to_file(bot, fake_clip):
    bot.audio_ok = False
    fb._share_worker(_room(), "S1", None, 90, "u1", "", "p2p")
    assert bot.audios
    assert bot.files and bot.files[0][1].endswith(".opus")


def test_failed_send_offers_retry_and_refunds_limit(bot, fake_clip, monkeypatch):
    """语音和文件都发不出去时，要给出重试卡，且不占 30 秒冷却。"""
    monkeypatch.setattr(fb.clips, "allow", _REAL_ALLOW)
    clips.reset_rate_limit()
    bot.audio_ok = False
    bot.file_ok = False
    fb._share_worker(_room(), "S1", None, 90, "u1", "", "p2p")
    assert bot.cards and "重试" in str(bot.cards[-1][1])
    value = bot.cards[-1][1]["elements"][1]["actions"][0]["value"]
    assert value["start"] == 0.5   # 钉住实际截出的起点，重试不会换一段
    assert clips.allow("S1", cooldown_s=30)[0]


def test_multiple_rooms_asks_for_ch(monkeypatch):
    rooms = [_room("rm1", "CH01", "S1"), _room("rm2", "CH02", "S2")]
    monkeypatch.setattr(fb, "_rooms", lambda: rooms)
    out = fb.cmd_share("90秒", "u1", "", "p2p")
    assert "CH01" in out and "CH02" in out and "选一路" in out


def test_route_share_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(fb, "cmd_share",
                        lambda rest, o, c="", t="p2p": seen.update(rest=rest) or "")
    fb._route("/分享 CH01 90秒", "/分享 ch01 90秒", "u1", "", "p2p")
    assert seen.get("rest") == "CH01 90秒"


def test_route_share_confirm(monkeypatch):
    seen = {}
    monkeypatch.setattr(fb, "cmd_share_confirm",
                        lambda n, o, c="", t="p2p": seen.update(n=n) or "")
    fb._route("/确认分享 ABC123", "/确认分享 abc123", "u1", "", "p2p")
    assert seen.get("n") == "ABC123"


def test_minutes_backfill_and_replay(bot, db):
    add_session(db, "S1", title="一号", duration_s=60)
    out = fb.cmd_minutes("CH01 https://x.feishu.cn/minutes/abc", "u1")
    assert "已记下" in out
    assert store.get_session(db, "S1")["minutes_url"].endswith("/abc")
    out = fb.cmd_replay("CH01")
    assert "minutes/abc" in out


def test_replay_without_minutes_returns_lan_link(bot, db):
    add_session(db, "S1", title="一号", duration_s=60)
    out = fb.cmd_replay("CH01")
    assert "/live?session=S1" in out
    assert "局域网" in out


def test_summary_text_includes_minutes_url():
    data = {
        "room": {"ch": "CH01", "session_id": "S1"},
        "coverage": {"t_to": 60, "included": 3},
        "overview": "概述",
        "points": [],
        "minutes_url": "https://x.feishu.cn/minutes/abc",
    }
    assert "整场回听 https://x.feishu.cn/minutes/abc" in fb._summary_text(data)


def test_package_group_refused(bot):
    assert "私聊" in fb.cmd_package("CH01", "u1", "c1", "group")


def test_package_worker_sends_file(bot, db, tmp_path, monkeypatch):
    add_session(db, "S1")
    folder = tmp_path / "S1"
    folder.mkdir()
    for name in ("summary.md", "transcript.srt", "README.md", "meta.json"):
        (folder / name).write_text("x", encoding="utf-8")
    z = tmp_path / "S1.zip"
    z.write_bytes(b"PK")
    monkeypatch.setattr(fb.share, "build_package", lambda conn, sid: {
        "dir": folder, "files": ["summary.md", "transcript.srt",
                                 "README.md", "meta.json"],
        "audio": "audio.m4a", "title": "一号"})
    monkeypatch.setattr(fb.share, "zip_package", lambda d: z)
    fb._package_worker(_room(), "S1", "u1", "", "p2p")
    assert bot.files and bot.files[0][1].endswith(".zip")
