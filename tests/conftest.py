# -*- coding: utf-8 -*-
"""pytest 公共夹具。

所有可写路径（数据库、音频、设置、术语、关注词）都指向 tmp_path，
测试永远不会碰仓库里的 data/；模块级 mtime 缓存也在每个用例前清空，
避免"上个用例改过文件、下个用例读到旧缓存"这种假绿。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for p in (str(ROOT), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app import audio as audio_mod  # noqa: E402
from app import (clips, glossary, live, logs, metrics, profile, report,  # noqa: E402
                 settings, store)


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """每个用例一套独立的本地文件。"""
    data = tmp_path / "data"
    audio = data / "audio"
    data.mkdir(parents=True, exist_ok=True)
    audio.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(store, "DB_PATH", data / "livenotes.db")
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    monkeypatch.setattr(live, "AUDIO_DIR", audio)
    monkeypatch.setattr(live, "DATA_DIR", data)
    monkeypatch.setattr(clips, "AUDIO_DIR", audio)
    monkeypatch.setattr(clips, "CLIPS_DIR", data / "clips")
    monkeypatch.setattr(audio_mod, "CLIPS_DIR", data / "clips")
    monkeypatch.setattr(live, "FINISH_LOG", data / "finalize.log")
    monkeypatch.setattr(logs, "LOG_DIR", data / "logs")
    monkeypatch.setattr(report, "REPORTS_DIR", data / "reports")
    monkeypatch.setattr(glossary, "PATH", data / "glossary.json")
    monkeypatch.setattr(profile, "PATH", data / "profile.json")
    monkeypatch.setattr(settings, "_PATH", data / "settings.json")

    metrics.METRICS.reset()
    glossary._cache.update({"key": None, "entries": []})
    glossary._rules_cache.update({"key": None, "rules": [], "canon": set(),
                                  "ascii_terms": []})
    profile._cache.update({"mtime": None, "words": []})
    live._SESSIONS.clear()
    yield data
    live._SESSIONS.clear()


@pytest.fixture
def db(isolated_paths):
    """初始化好的临时 SQLite 连接。"""
    conn = store.connect()
    store.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()
