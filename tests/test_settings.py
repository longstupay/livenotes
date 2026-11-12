# -*- coding: utf-8 -*-
"""设置回归：v1 -> v2 迁移、连接档案、偏好钳制、表单/档案/环境变量优先级。"""
from __future__ import annotations

import json

import pytest

from app import settings


def _write(data):
    settings.path().parent.mkdir(parents=True, exist_ok=True)
    settings.path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_v1_flat_config_migrates_to_profile(isolated_paths):
    _write({"base": "https://api.deepseek.com/v1", "key": "sk-abcdef",
            "model": "deepseek-chat", "provider": "deepseek"})
    d = settings.load()
    assert d["version"] == 2 and d["active"] in d["profiles"]
    p = d["profiles"][d["active"]]
    assert p["name"] == "deepseek"
    assert p["base"] == "https://api.deepseek.com/v1"
    assert p["model"] == "deepseek-chat" and p["key"] == "sk-abcdef"
    assert p["temperature"] == 0.3
    st = settings.state()
    assert st["configured"] is True
    assert st["current"]["has_key"] is True and st["current"]["key_hint"].endswith("cdef")


def test_v2_passthrough_and_active_fallback(isolated_paths):
    _write({"version": 2, "active": "missing",
            "profiles": {"p1": {"name": "A", "base": "http://a/v1",
                                "model": "m", "key": "k"}}})
    d = settings.load()
    assert d["active"] == "p1"
    assert settings.state()["current"]["id"] == "p1"


def test_empty_corrupt_and_non_dict_file_are_safe(isolated_paths):
    assert settings.state() == {
        "active": "", "profiles": [], "current": None, "configured": False,
        "prefs": {"topics": 10, "direct_fallback": "", "open_mode": "",
                  "capture_mode": "", "close_action": "tray"},
        "direct_fallback": {"mode": "ask", "stored": "", "source": "default"},
    }
    settings.path().write_text("{oops", encoding="utf-8")
    assert settings.load() == {"version": 2, "active": "", "profiles": {}}
    settings.path().write_text("[1,2,3]", encoding="utf-8")
    assert settings.load()["profiles"] == {}


def test_upsert_creates_and_activates_profile(isolated_paths):
    out = settings.upsert({"new": True, "name": "本地", "provider": "ollama",
                           "base": "http://127.0.0.1:11434/v1/",
                           "model": "qwen", "key": "k", "temperature": 9})
    pid = out["saved_id"]
    assert out["active"] == pid
    prof = out["current"]
    assert prof["base"] == "http://127.0.0.1:11434/v1"
    assert prof["temperature"] == 1.5              # 钳到上限
    assert prof["configured"] is True


def test_upsert_without_id_updates_active_for_old_clients(isolated_paths):
    first = settings.upsert({"new": True, "name": "A", "base": "http://a",
                             "model": "m", "key": "k"})
    pid = first["saved_id"]
    out = settings.upsert({"model": "m2"})
    assert out["saved_id"] == pid
    assert len(out["profiles"]) == 1
    assert out["current"]["model"] == "m2"


def test_upsert_clear_key_and_default_name(isolated_paths):
    out = settings.upsert({"new": True, "base": "http://a", "model": "m", "key": "k"})
    pid = out["saved_id"]
    out = settings.upsert({"id": pid, "clear_key": True})
    assert out["current"]["has_key"] is False
    assert out["current"]["name"] == "未命名"
    assert out["configured"] is False


def test_activate_delete_duplicate(isolated_paths):
    a = settings.upsert({"new": True, "name": "A", "base": "http://a",
                         "model": "m", "key": "k"})["saved_id"]
    b = settings.upsert({"new": True, "name": "B", "base": "http://b",
                         "model": "m", "key": "k"})["saved_id"]
    assert settings.state()["active"] == b
    assert settings.activate(a)["active"] == a
    with pytest.raises(KeyError):
        settings.activate("nope")
    dup = settings.duplicate(a)
    assert dup["active"] not in (a, b)
    assert dup["current"]["name"].endswith("副本")
    after = settings.delete(dup["active"])
    assert dup["active"] not in [p["id"] for p in after["profiles"]]
    with pytest.raises(KeyError):
        settings.delete("nope")


def test_prefs_clamping_and_unknown_keys(isolated_paths):
    assert settings.prefs() == {"topics": 10, "direct_fallback": "", "open_mode": "",
                                "capture_mode": "", "close_action": "tray"}
    assert settings.set_prefs({"topics": 999})["topics"] == 40
    assert settings.set_prefs({"topics": -5})["topics"] == 3
    assert settings.set_prefs({"topics": "abc"})["topics"] == 3      # 非法值忽略
    out = settings.set_prefs({"unknown": 1, "topics": 12})
    assert out == {"topics": 12, "direct_fallback": "", "open_mode": "",
                   "capture_mode": "", "close_action": "tray"}
    _write({"version": 2, "profiles": {}, "prefs": {"topics": 100}})
    assert settings.prefs()["topics"] == 40
    _write({"version": 2, "profiles": {}, "prefs": {"topics": "x"}})
    assert settings.prefs()["topics"] == 10


def test_open_mode_pref(isolated_paths):
    assert settings.prefs()["open_mode"] == ""
    assert settings.set_prefs({"open_mode": "internal"})["open_mode"] == "internal"
    # 非法值不覆盖已有选择
    assert settings.set_prefs({"open_mode": "bogus"})["open_mode"] == "internal"
    assert settings.set_prefs({"open_mode": "external"})["open_mode"] == "external"


def test_capture_mode_pref(isolated_paths):
    assert settings.prefs()["capture_mode"] == ""
    assert settings.set_prefs({"capture_mode": "direct"})["capture_mode"] == "direct"
    assert settings.set_prefs({"capture_mode": "bogus"})["capture_mode"] == "direct"
    assert settings.set_prefs({"capture_mode": "external"})["capture_mode"] == "external"
    assert settings.set_prefs({"capture_mode": "browser"})["capture_mode"] == "browser"


def test_close_action_pref(isolated_paths):
    assert settings.prefs()["close_action"] == "tray"
    assert settings.set_prefs({"close_action": "quit"})["close_action"] == "quit"
    # 非法值不覆盖已有选择
    assert settings.set_prefs({"close_action": "bogus"})["close_action"] == "quit"
    _write({"version": 2, "profiles": {}, "prefs": {"close_action": "nope"}})
    assert settings.prefs()["close_action"] == "tray"


def test_direct_fallback_pref_and_env_precedence(isolated_paths, monkeypatch):
    monkeypatch.delenv("LIVENOTES_DIRECT_FALLBACK", raising=False)
    assert settings.direct_fallback() == {
        "mode": "ask", "stored": "", "source": "default"}
    assert settings.set_prefs({"direct_fallback": "auto"})["direct_fallback"] == "auto"
    assert settings.direct_fallback() == {
        "mode": "auto", "stored": "auto", "source": "prefs"}
    # 非法值不覆盖已有选择
    assert settings.set_prefs({"direct_fallback": "bogus"})["direct_fallback"] == "auto"
    # 环境变量用于脚本/临时覆盖，优先级高于设置页；且不会被写进偏好
    monkeypatch.setenv("LIVENOTES_DIRECT_FALLBACK", "none")
    assert settings.direct_fallback() == {
        "mode": "none", "stored": "auto", "source": "env"}
    assert settings.prefs()["direct_fallback"] == "auto"


def test_resolve_precedence(isolated_paths, monkeypatch):
    monkeypatch.setattr(settings, "LLM_BASE", "http://env")
    monkeypatch.setattr(settings, "LLM_KEY", "env-key")
    monkeypatch.setattr(settings, "LLM_MODEL", "env-model")
    # 环境变量兜底
    r = settings.resolve()
    assert (r["base"], r["key"], r["model"], r["source"]) == (
        "http://env", "env-key", "env-model", "env")
    assert r["configured"] is True
    # 档案优先于环境
    settings.upsert({"new": True, "name": "P", "base": "http://file/",
                     "model": "file-model", "key": "file-key"})
    r = settings.resolve()
    assert (r["base"], r["model"], r["source"]) == (
        "http://file", "file-model", "file")
    # 表单值优先于档案；空表单值不覆盖
    r = settings.resolve({"base": "http://form/", "model": "form-model"})
    assert (r["base"], r["model"], r["key"]) == ("http://form", "form-model", "file-key")
    r = settings.resolve({"base": "", "key": ""})
    assert r["base"] == "http://file" and r["key"] == "file-key"


def test_resolve_not_configured_when_any_field_missing(isolated_paths, monkeypatch):
    for name in ("LLM_BASE", "LLM_KEY", "LLM_MODEL"):
        monkeypatch.setattr(settings, name, "")
    settings.upsert({"new": True, "base": "http://a", "model": "m"})   # 没有 key
    r = settings.resolve()
    assert r["configured"] is False and r["source"] == "none"
