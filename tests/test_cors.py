# -*- coding: utf-8 -*-
"""桌面壳跨域：只放行本机 origin，普通网站不能读引擎响应。"""
from __future__ import annotations

import pytest

from app.server import _trusted_origin


@pytest.mark.parametrize("origin", [
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://127.0.0.1:1430",      # tauri dev 的 devUrl
    "http://localhost:5173",
    "https://127.0.0.1:8443",
    "http://[::1]:9000",
])
def test_loopback_origins_allowed(origin):
    assert _trusted_origin(origin) is True


@pytest.mark.parametrize("origin", [
    "",
    "http://example.com",
    "https://evil.example",
    "http://127.0.0.1.evil.com",
    "http://localhost.evil.com:1430",
    "file://",
    "null",
])
def test_remote_origins_rejected(origin):
    assert _trusted_origin(origin) is False
