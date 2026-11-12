# -*- coding: utf-8 -*-
"""降级路径：没装 sherpa-onnx 时服务照常启动，只有实时监听返回可操作的错误。

CI 本来就只装 requirements-dev.txt（不含 sherpa-onnx），这里再用 import 拦截器
把「没装」变成确定的事实，避免开发机上装了依赖就把这条路径测糊了。

模型文件也一样：开发机多半已经跑过 tools/fetch_models.py，而 CI 是干净检出。
子进程把模型路径固定到临时目录，`ready` / `missing` 才不会随机器变红变绿。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import live


ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import json, sys, threading, urllib.error, urllib.request
from pathlib import Path

class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == "sherpa_onnx" or name.startswith("sherpa_onnx."):
            raise ModuleNotFoundError("No module named 'sherpa_onnx' (blocked by test)",
                                      name=name)
        return None

sys.meta_path.insert(0, Blocker())

from app import live, server, store

data = Path(sys.argv[1])
data.mkdir(parents=True, exist_ok=True)
# 和模型是否已下载解耦：固定成「模型不在」，ready=False / missing 非空才可复现。
models = data / "models"
live.SENSEVOICE_MODEL = models / "sensevoice.onnx"
live.SENSEVOICE_TOKENS = models / "tokens.txt"
live.VAD_MODEL = models / "silero_vad.onnx"
live.SPEAKER_MODEL = models / "campplus.onnx"
store.DB_PATH = data / "degraded.db"
store.ensure_dirs = lambda: None
conn = store.connect()
store.init_db(conn)
conn.close()

srv = server.make_server("127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port
out = {}
try:
    out["health"] = json.load(urllib.request.urlopen(base + "/api/health"))
    out["info"] = json.load(urllib.request.urlopen(base + "/api/live/info"))
    req = urllib.request.Request(base + "/api/live/start", data=b"{}",
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        urllib.request.urlopen(req)
        out["start"] = {"status": 200}
    except urllib.error.HTTPError as e:
        out["start"] = {"status": e.code, "body": json.loads(e.read().decode("utf-8"))}
finally:
    srv.shutdown()
    srv.server_close()
out["sherpa_loaded"] = "sherpa_onnx" in sys.modules
print(json.dumps(out, ensure_ascii=True))
'''


def test_server_starts_and_degrades_without_sherpa(tmp_path):
    res = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path)],
                         cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])

    assert out["sherpa_loaded"] is False
    assert out["health"]["ok"] is True
    assert out["info"]["ready"] is False
    assert out["info"]["missing"], "没装模型时必须报告缺了哪些文件"
    assert out["info"]["active"] == []

    start = out["start"]
    assert start["status"] == 503
    assert "requirements-live.txt" in start["body"]["hint"]
    assert start["body"]["error"]


def test_engine_load_mentions_install_command(monkeypatch):
    monkeypatch.setattr(live.Engine, "missing", staticmethod(lambda: []))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)    # import 时抛 ImportError
    with pytest.raises(live.LiveError) as ei:
        live.Engine().load()
    assert "pip install -r requirements-live.txt" in str(ei.value)


def test_engine_load_mentions_missing_models(monkeypatch):
    monkeypatch.setattr(live.Engine, "missing",
                        staticmethod(lambda: ["/models/sensevoice.onnx"]))
    with pytest.raises(live.LiveError) as ei:
        live.Engine().load()
    assert "sensevoice.onnx" in str(ei.value)
    assert "fetch_models.py" in str(ei.value)


def test_failed_start_does_not_leave_a_ghost_session(monkeypatch, isolated_paths):
    monkeypatch.setattr(live.Engine, "missing",
                        staticmethod(lambda: ["/models/missing.onnx"]))
    before = set(live._SESSIONS)
    with pytest.raises(live.LiveError):
        live.start("起不来的场次")
    assert set(live._SESSIONS) == before
