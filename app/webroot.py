# -*- coding: utf-8 -*-
"""Web 资源分层：基座 ``app/web`` + 可选的宿主皮肤 overlay（如 ``desktop/web``）。

这样分层的理由写在 ``docs/29-page-host-contract.md``，一句话：

- 基座只出「标准网页版 + 宿主适配器」，任何浏览器打开都一模一样；
- 桌面专属的观感、文案、壳内交互放在 overlay 目录，物理上不在基座里；
- 同一路径 overlay 优先，所以 overlay 换掉 ``static/css/paper.css`` 就够了，
  不需要在基座里写 ``if (desktop)`` 分支。

overlay 只允许覆盖 ``static/`` 下的静态资源；页面 HTML 永远来自基座
（否则就成了分叉，而不是皮肤）。manifest（``host.json``）告诉宿主适配器
要加载哪些 CSS/JS、自己是什么宿主、有哪些能力。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import APP_DIR, WEB_DIR

#: overlay 根目录下的清单文件名
MANIFEST_NAME = "host.json"
#: 协议版本：宿主适配器与 overlay 都用它对齐
CONTRACT_VERSION = 1

_overlay: Path | None = None
_manifest_cache: dict[str, Any] | None = None


def _detect() -> Path | None:
    """确定 overlay 目录。

    1. ``LIVENOTES_WEB_OVERLAY``（``run.py --web-overlay`` 会设它）；
    2. 打包态：``app/web-overlay`` 与 ``app/web`` 同级，PyInstaller 把 overlay
       随 ``--add-data`` 放进 ``_internal/app/``，不需要壳再传参数；
    3. 都没有 = 纯网页版。
    """
    env = os.environ.get("LIVENOTES_WEB_OVERLAY", "").strip()
    if env:
        return Path(env).resolve()
    # PyInstaller --add-data 的落点是 ``_internal/<dest>``；打包脚本把桌面皮肤
    # 放在 ``app/web-overlay``，与 ``app/web`` 同级。两个位置都认，手工铺平也能跑。
    for bundled in (APP_DIR / "web-overlay", APP_DIR.parent / "web-overlay"):
        if bundled.is_dir():
            return bundled.resolve()
    return None


def configure(path: str | Path | None = None) -> Path | None:
    """显式指定/清空 overlay。传 ``None`` 表示回到纯网页版。

    测试和 ``--web-overlay`` 走这里；传进来的目录必须存在，写错要当场报错，
    不能静默退回基座——那样桌面会「看起来正常但没有皮肤」，很难查。
    """
    global _overlay, _manifest_cache
    if path is None:
        env = _detect()
        _overlay = env if env and env.is_dir() else None
        _manifest_cache = None
        return _overlay
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError("web overlay 目录不存在：%s" % p)
    _overlay = p
    _manifest_cache = None
    return _overlay


def overlay_dir() -> Path | None:
    if _overlay is None:
        configure(None)
    return _overlay


def is_inside(path: Path) -> bool:
    """``path`` 是否落在基座或 overlay 目录里（防目录穿越）。"""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in (overlay_dir(), WEB_DIR.resolve()):
        if root is not None and resolved.is_relative_to(root):
            return True
    return False


def resolve_static(rel: str) -> Path | None:
    """按 ``/static/`` 之后的相对路径找文件：overlay 优先，基座兜底。"""
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    if not rel or rel.startswith("../") or "/../" in rel:
        return None
    for root in (overlay_dir(), WEB_DIR):
        if root is None:
            continue
        candidate = (root / rel).resolve()
        if candidate.is_file() and candidate.is_relative_to(root.resolve()):
            return candidate
    return None


def _clean_assets(raw: Any, kind: str) -> tuple[list[str], list[str]]:
    """manifest 里的 css/js 只收 ``/static/`` 且真的存在的本地文件。"""
    out: list[str] = []
    bad: list[str] = []
    items = raw if isinstance(raw, list) else []
    for item in items:
        s = str(item or "").strip()
        if not s.startswith("/static/"):
            bad.append("%s 必须以 /static/ 开头：%r" % (kind, item))
            continue
        if resolve_static(s[len("/static/"):]) is None:
            bad.append("%s 指向不存在的文件：%s" % (kind, s))
            continue
        if s not in out:
            out.append(s)
    return out, bad


def manifest() -> dict[str, Any]:
    """``/api/host/manifest`` 的响应：没 overlay 时也要能安全返回。"""
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache
    root = overlay_dir()
    empty = {"overlay": False, "version": 0, "contract": CONTRACT_VERSION,
             "host": "web", "css": [], "js": [], "profile": {"host": "web",
             "capabilities": [], "copy": {}}, "warnings": []}
    if root is None:
        _manifest_cache = empty
        return empty

    raw: dict[str, Any] = {}
    path = root / MANIFEST_NAME
    warnings: list[str] = []
    if path.is_file():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                raw = parsed
            else:
                warnings.append("%s 顶层必须是对象" % MANIFEST_NAME)
        except (OSError, ValueError) as e:
            warnings.append("%s 解析失败：%s" % (MANIFEST_NAME, e))
    else:
        warnings.append("overlay 里没有 %s，只叠加静态文件" % MANIFEST_NAME)

    css, bad_css = _clean_assets(raw.get("css"), "css")
    js, bad_js = _clean_assets(raw.get("js"), "js")
    warnings.extend(bad_css + bad_js)

    profile = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}
    profile = {
        "host": str(profile.get("host") or raw.get("host") or "desktop")[:40],
        "name": str(profile.get("name") or raw.get("name") or "")[:80],
        "capabilities": [str(c)[:40] for c in (profile.get("capabilities") or [])
                         if isinstance(c, (str, int))][:32],
        "copy": {str(k)[:80]: str(v)[:400]
                 for k, v in (profile.get("copy") or {}).items()
                 if isinstance(v, str)} if isinstance(profile.get("copy"), dict) else {},
    }

    out = {
        "overlay": True,
        "version": int(raw.get("version") or 1),
        "contract": CONTRACT_VERSION,
        "host": profile["host"],
        "name": profile["name"],
        "css": css,
        "js": js,
        "profile": profile,
        "warnings": warnings,
    }
    _manifest_cache = out
    return out
