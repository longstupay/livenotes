# -*- coding: utf-8 -*-
"""基座 / overlay 分层回归。

覆盖四件事：

1. **静态优先级**：同一路径 overlay 优先、基座兜底，网页版（无 overlay）行为不变；
2. **宿主清单**：``/api/host/manifest`` 在两种模式下都能安全返回，坏路径被丢掉；
3. **页面不被覆盖**：overlay 只能换静态资源，页面 HTML 永远来自基座；
4. **分层检查**：``tools/check_web_layers.py`` 对当前仓库必须通过，且能抓到违规样例。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app import live, server, webroot

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "desktop" / "web"


@pytest.fixture
def http_base():
    srv = server.make_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        srv.shutdown()
        srv.server_close()
        live.stop_watchdog()


@pytest.fixture
def base_mode():
    webroot.configure(None)
    try:
        yield
    finally:
        webroot.configure(None)


@pytest.fixture
def overlay_mode():
    webroot.configure(OVERLAY)
    try:
        yield OVERLAY
    finally:
        webroot.configure(None)


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------- 静态优先级
def test_overlay_wins_static(http_base, overlay_mode):
    status, body = _get(http_base + "/static/css/paper.css")
    assert status == 200
    assert "Paper 主题".encode("utf-8") in body


def test_base_file_still_served_with_overlay(http_base, overlay_mode):
    status, body = _get(http_base + "/static/css/index.css")
    assert status == 200
    assert b"--spk-0" in body


def test_overlay_only_file_404_without_overlay(http_base, base_mode):
    status, _ = _get(http_base + "/static/css/paper.css")
    assert status == 404


def test_web_mode_has_no_overlay_class(http_base, base_mode):
    """网页版拿到的页面必须是基座那份（不带任何 overlay 标记）。"""
    status, body = _get(http_base + "/")
    assert status == 200
    assert b'data-ln-region="topbar"' in body


# ---------------------------------------------------------------- 宿主清单
def test_manifest_web_mode(base_mode):
    m = webroot.manifest()
    assert m["overlay"] is False
    assert m["contract"] >= 1
    assert m["css"] == [] and m["js"] == []
    assert m["profile"]["host"] == "web"


def test_manifest_overlay(overlay_mode):
    m = webroot.manifest()
    assert m["overlay"] is True
    assert m["host"] == "desktop"
    assert "/static/css/paper.css" in m["css"]
    assert "/static/js/desktop.js" in m["js"]
    assert "native-windows" in m["profile"]["capabilities"]
    assert m["warnings"] == []


def test_manifest_endpoint(http_base, overlay_mode):
    status, body = _get(http_base + "/api/host/manifest")
    assert status == 200
    data = json.loads(body)
    assert data["overlay"] is True


def test_manifest_drops_bad_paths(tmp_path, monkeypatch):
    (tmp_path / "css").mkdir()
    (tmp_path / "host.json").write_text(json.dumps({
        "css": ["/static/css/missing.css", "css/relative.css", "/etc/passwd"],
        "js": [],
        "profile": {"host": "desktop"},
    }), encoding="utf-8")
    webroot.configure(tmp_path)
    try:
        m = webroot.manifest()
    finally:
        webroot.configure(None)
    assert m["overlay"] is True
    assert m["css"] == []
    assert len(m["warnings"]) == 3


# ---------------------------------------------------------------- 页面不被覆盖
def test_overlay_cannot_replace_page(tmp_path, http_base):
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "skin.css").write_text("html{--x:1}", encoding="utf-8")
    (tmp_path / "index.html").write_text("<html>OVERLAY</html>", encoding="utf-8")
    webroot.configure(tmp_path)
    try:
        status, body = _get(http_base + "/")
    finally:
        webroot.configure(None)
    assert status == 200
    assert b"OVERLAY" not in body


# ---------------------------------------------------------------- 目录穿越
@pytest.mark.parametrize("path", [
    "/static/../app/config.py",
    "/static/%2e%2e/app/config.py",
    "/static/css/../../app/config.py",
])
def test_traversal_blocked(http_base, overlay_mode, path):
    status, body = _get(http_base + path)
    assert status == 404
    assert b"LLM_BASE" not in body


# ---------------------------------------------------------------- 分层检查
def test_layer_checker_passes():
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "check_web_layers.py")],
                         cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr


def test_layer_checker_catches_branch(tmp_path):
    """往基座里塞一个 if (desktop)，检查必须红。"""
    repo = tmp_path / "repo"
    (repo / "app" / "web" / "js").mkdir(parents=True)
    (repo / "desktop" / "web" / "css").mkdir(parents=True)
    (repo / "app" / "web" / "js" / "bad.js").write_text(
        "if (window.__LN_DESKTOP__) { console.log('desktop') }", encoding="utf-8")
    (repo / "desktop" / "web" / "host.json").write_text("{}", encoding="utf-8")
    (repo / "tools").mkdir()
    shutil.copy(ROOT / "tools" / "check_web_layers.py", repo / "tools" / "check_web_layers.py")
    res = subprocess.run([sys.executable, str(repo / "tools" / "check_web_layers.py")],
                         cwd=str(repo), capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60)
    assert res.returncode == 1
    assert "__LN_DESKTOP__" in res.stdout


def test_overlay_js_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 Node")
    for js in (OVERLAY / "js").glob("*.js"):
        res = subprocess.run([node, "--check", str(js)], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=30)
        assert res.returncode == 0, "%s\n%s" % (js.name, res.stderr)
