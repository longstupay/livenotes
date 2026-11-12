# -*- coding: utf-8 -*-
"""老库迁移：补列、标题归属推断、推送台账补列、FTS 索引重建、崩溃任务收尾。"""
from __future__ import annotations

import sqlite3

import pytest
from helpers import add_rows, add_session, utt_row

from app import store


OLD_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, started_at TEXT NOT NULL,
    duration_s REAL NOT NULL, source_mode TEXT NOT NULL,
    sample_rate INTEGER DEFAULT 16000, audio_path TEXT,
    overlap_seconds REAL DEFAULT 0, untranscribed_seconds REAL DEFAULT 0,
    warnings_json TEXT DEFAULT '[]', created_at TEXT
);
CREATE TABLE utterances (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    t_start REAL NOT NULL, t_end REAL NOT NULL,
    speaker_id TEXT NOT NULL, speaker_label TEXT NOT NULL, text TEXT NOT NULL,
    asr_conf REAL DEFAULT 1.0, overlap INTEGER DEFAULT 0,
    category TEXT DEFAULT 'substantive', value_tag TEXT DEFAULT 'normal',
    importance REAL DEFAULT 0.5, topic TEXT DEFAULT '', low_value_reason TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE speakers (
    session_id TEXT NOT NULL, speaker_id TEXT NOT NULL, display_name TEXT,
    verified INTEGER DEFAULT 0, note TEXT DEFAULT '',
    PRIMARY KEY (session_id, speaker_id)
);
CREATE TABLE summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    scope TEXT NOT NULL, filter_json TEXT NOT NULL, engine TEXT NOT NULL,
    payload_json TEXT NOT NULL, created_at TEXT
);
CREATE TABLE push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    item TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', reason TEXT DEFAULT '',
    level TEXT DEFAULT '', score REAL DEFAULT 0, text TEXT DEFAULT '',
    created_at TEXT, sent_at TEXT
);
CREATE TABLE analysis_runs (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'full',
    stages_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
    progress INTEGER DEFAULT 0, message TEXT DEFAULT '', usage_json TEXT DEFAULT '{}',
    error TEXT DEFAULT '', started_at TEXT, updated_at TEXT, finished_at TEXT,
    created_at TEXT
);
"""


def _old_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(OLD_SCHEMA)
    conn.executemany(
        "INSERT INTO sessions(id,title,started_at,duration_s,source_mode) VALUES (?,?,?,?,?)",
        [
            ("OLD_DEFAULT", "抖音直播 11-05 20:00", "2026-11-01T20:00:00", 60, "tab"),
            ("OLD_AUTO", "老王聊跳槽 · 03:41", "2026-11-02T20:00:00", 60, "mixed"),
            ("OLD_USER", "我自己的场次名字", "2026-11-03T20:00:00", 60, "demo"),
        ])
    conn.executemany(
        "INSERT INTO utterances(session_id,t_start,t_end,speaker_id,speaker_label,text) "
        "VALUES (?,?,?,?,?,?)",
        [("OLD_DEFAULT", 0, 2, "SPK_00", "主播", "历史库里的跳槽记录"),
         ("OLD_USER", 0, 2, "SPK_00", "主播", "另一条历史记录")])
    conn.execute(
        "INSERT INTO push_log(session_id,item,status,text) VALUES ('OLD_DEFAULT','hl:1.00','sent','旧台账')")
    conn.execute(
        "INSERT INTO analysis_runs(id,session_id,status,created_at) "
        "VALUES ('run_running','OLD_DEFAULT','running','2026-11-01T20:00:00')")
    conn.execute(
        "INSERT INTO analysis_runs(id,session_id,status,created_at) "
        "VALUES ('run_done','OLD_DEFAULT','done','2026-11-01T20:00:00')")
    conn.commit()
    return conn


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_old_db_gets_all_new_columns(tmp_path):
    conn = _old_db(tmp_path)
    store.init_db(conn)
    assert {"deleted_at", "ai_topics_json", "started_utc", "tz_offset_min",
            "title_source", "title_alts_json", "title_updated_at"} <= _cols(conn, "sessions")
    assert {"category_rule", "value_tag_rule", "features_json", "ai_fp",
            "edited_at"} <= _cols(conn, "utterances")
    assert {"person_id", "person_score", "person_bg", "person_state"} <= _cols(conn, "speakers")
    assert {"card_json", "features_json", "feedback", "feedback_at"} <= _cols(conn, "push_log")
    assert "fingerprint" in _cols(conn, "summaries")


def test_title_source_backfill_does_not_steal_user_titles(tmp_path):
    conn = _old_db(tmp_path)
    store.init_db(conn)
    src = {r["id"]: r["title_source"] for r in
           conn.execute("SELECT id,title_source FROM sessions")}
    assert src == {"OLD_DEFAULT": "default", "OLD_AUTO": "auto", "OLD_USER": "legacy"}
    # 再跑一次不能把用户名字改回 auto/default
    store.init_db(conn)
    src2 = {r["id"]: r["title_source"] for r in
            conn.execute("SELECT id,title_source FROM sessions")}
    assert src2 == src


def test_old_utterances_become_fts_searchable(tmp_path):
    """外部内容表不会自动收录建表前的行；迁移后必须 rebuild，否则升级即"搜不到"。"""
    conn = _old_db(tmp_path)
    store.init_db(conn)
    if not store.FTS_TRIGRAM:
        pytest.skip("此 SQLite 未启用 FTS5 trigram")
    rows = store.query_utterances(conn, {"session_id": "OLD_DEFAULT", "q": "历史库"})
    assert [r["text"] for r in rows] == ["历史库里的跳槽记录"]
    # 重建是幂等的：再初始化一次不会出现重复命中
    store.init_db(conn)
    assert len(store.query_utterances(conn, {"session_id": "OLD_DEFAULT", "q": "历史库"})) == 1


def test_crashed_runs_are_marked_interrupted(tmp_path):
    conn = _old_db(tmp_path)
    store.init_db(conn)
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM analysis_runs")}
    assert rows["run_running"]["status"] == "interrupted"
    assert rows["run_running"]["error"]
    assert rows["run_running"]["finished_at"]
    assert rows["run_done"]["status"] == "done"
    assert store.get_active_run(conn, "OLD_DEFAULT") is None


def test_old_push_log_rows_keep_working(tmp_path):
    conn = _old_db(tmp_path)
    store.init_db(conn)
    row = dict(conn.execute("SELECT * FROM push_log").fetchone())
    assert row["card_json"] == "" and row["features_json"] == "{}"
    assert row["feedback"] == 0 and row["feedback_at"] is None
    assert store.pushes_today(conn) >= 0


def test_fts_unavailable_degrades_to_like(tmp_path):
    conn = sqlite3.connect(tmp_path / "no_fts.db")
    conn.row_factory = sqlite3.Row

    class NoFtsConnection:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            if "CREATE VIRTUAL TABLE" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return self._real.execute(sql, params)

        def executescript(self, sql):
            return self._real.executescript(sql)

        def commit(self):
            return self._real.commit()

    store.init_db(NoFtsConnection(conn))
    assert store.FTS_TRIGRAM is False

    add_session(conn, "S_NOFTS")
    add_rows(conn, "S_NOFTS", [utt_row(0, "没有 FTS 也要能搜到这句话")])
    assert len(store.query_utterances(conn, {"session_id": "S_NOFTS", "q": "搜到这句"})) == 1
    assert len(store.query_utterances(conn, {"session_id": "S_NOFTS", "q": "FTS"})) == 1


def test_old_sqlite_version_disables_fts(monkeypatch, tmp_path):
    conn = sqlite3.connect(tmp_path / "old_sqlite.db")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(store.sqlite3, "sqlite_version_info", (3, 33, 0))
    store.init_db(conn)
    assert store.FTS_TRIGRAM is False
    add_session(conn, "S_OLDVER")
    add_rows(conn, "S_OLDVER", [utt_row(0, "旧 SQLite 也要能搜到")])
    assert len(store.query_utterances(conn, {"session_id": "S_OLDVER", "q": "旧 SQLite"})) == 1
