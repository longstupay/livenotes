# -*- coding: utf-8 -*-
"""运行期状态：单实例锁、engine.json 接管信息、父进程看门狗。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import types

from app import runtime


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(runtime, "ENGINE_JSON", tmp_path / "runtime" / "engine.json")
    monkeypatch.setattr(runtime, "LOCK_PATH", tmp_path / "runtime" / "engine.lock")


def test_acquire_writes_engine_json_and_release_cleans(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ok, info = runtime.acquire(61011, "127.0.0.1")
    try:
        assert ok is True
        assert info["pid"] == os.getpid() and info["port"] == 61011
        on_disk = json.loads(runtime.ENGINE_JSON.read_text(encoding="utf-8"))
        assert on_disk["pid"] == os.getpid()
        assert on_disk["data_dir"] == str(tmp_path)
        assert runtime.read_info() == on_disk
    finally:
        runtime.release()
    assert not runtime.ENGINE_JSON.exists()
    assert runtime.read_info() is None


def test_acquire_reclaims_stale_engine_once(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runtime.RUNTIME_DIR.mkdir(parents=True)
    runtime.ENGINE_JSON.write_text(
        json.dumps({"pid": 999999, "port": 1}), encoding="utf-8")

    real_lock = runtime._lock
    calls = {"n": 0}

    def flaky(f):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("held by stale engine")
        real_lock(f)

    monkeypatch.setattr(runtime, "_lock", flaky)
    monkeypatch.setattr(runtime, "_healthy", lambda info: False)
    killed = []
    monkeypatch.setattr(runtime, "_terminate",
                        lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(runtime, "time", types.SimpleNamespace(sleep=lambda s: None))

    ok, info = runtime.acquire(2222, "127.0.0.1")
    try:
        assert ok is True and info["port"] == 2222
        assert killed == [999999]
    finally:
        runtime.release()


def test_acquire_gives_up_when_holder_healthy(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    runtime.RUNTIME_DIR.mkdir(parents=True)
    runtime.ENGINE_JSON.write_text(
        json.dumps({"pid": 1234, "port": 61011}), encoding="utf-8")

    def held(f):
        raise OSError("held")

    monkeypatch.setattr(runtime, "_lock", held)
    monkeypatch.setattr(runtime, "_healthy", lambda info: True)
    monkeypatch.setattr(runtime, "time", types.SimpleNamespace(sleep=lambda s: None))
    ok, info = runtime.acquire(2222, "127.0.0.1")
    assert ok is False
    assert info == {"pid": 1234, "port": 61011}


def test_parent_pid_from_env_and_argv(monkeypatch):
    monkeypatch.delenv("LIVENOTES_PARENT_PID", raising=False)
    monkeypatch.setattr(sys, "argv", ["run.py"])
    assert runtime.parent_pid() == 0
    monkeypatch.setenv("LIVENOTES_PARENT_PID", "4321")
    assert runtime.parent_pid() == 4321
    monkeypatch.delenv("LIVENOTES_PARENT_PID")
    monkeypatch.setattr(sys, "argv", ["run.py", "--parent-pid", "8765"])
    assert runtime.parent_pid() == 8765
    monkeypatch.setattr(sys, "argv", ["run.py", "--parent-pid"])
    assert runtime.parent_pid() == 0


def test_pid_alive_current_and_exited():
    assert runtime.pid_alive(os.getpid()) is True
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert runtime.pid_alive(p.pid) is False


def test_watch_parent_fires_after_death(monkeypatch):
    dead = []
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)
    runtime.watch_parent(1, lambda: dead.append(1), interval=0.01)
    assert dead == [1]


def test_watch_parent_stops_with_event(monkeypatch):
    dead = []
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)
    ev = threading.Event()
    ev.set()
    runtime.watch_parent(1, lambda: dead.append(1), interval=0.01, stop_event=ev)
    assert dead == []
