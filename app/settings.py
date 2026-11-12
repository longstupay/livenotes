# -*- coding: utf-8 -*-
"""大模型设置：多份"连接档案"，存在 data/settings.json，环境变量兜底。

为什么要多档案：中转站、官方、本地 Ollama 各有各的地址和 Key，
切一次改一堆字段既容易出错也会丢原来的配置。现在一个档案一套地址+模型+Key，
点一下切换，总结和自动标题都用当前激活的那份。

文件格式（v2）：
{
  "version": 2,
  "active": "p3f9c1",
  "profiles": {"p3f9c1": {"name":"DeepSeek 官方","provider":"deepseek",
                          "base":"https://api.deepseek.com/v1","model":"deepseek-chat",
                          "key":"sk-...","temperature":0.3}}
}
旧的单配置文件会自动迁移成一份档案。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from .config import DATA_DIR, LLM_BASE, LLM_KEY, LLM_MODEL, LLM_TIMEOUT, LLM_TOTAL_TIMEOUT

_PATH = Path(os.environ.get("LIVENOTES_SETTINGS") or (DATA_DIR / "settings.json"))
_LOCK = threading.RLock()      # set_prefs 里会再调 load()，必须可重入，否则死锁
_FIELDS = ("name", "provider", "base", "model", "temperature")


def path() -> Path:
    return _PATH


def _temp(v: Any) -> float:
    try:
        return max(0.0, min(1.5, float(v)))
    except (TypeError, ValueError):
        return 0.3


def _new_id() -> str:
    return "p" + uuid.uuid4().hex[:6]


def _write(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(_PATH, 0o600)
    except OSError:
        pass


def _migrate(raw: dict) -> dict:
    """v1（扁平单配置）-> v2（档案）。"""
    if raw.get("profiles"):
        raw.setdefault("version", 2)
        return raw
    if not any(raw.get(k) for k in ("base", "key", "model")):
        out = {"version": 2, "active": "", "profiles": {}}
        # 没有连接档案不等于没有设置：全局偏好（AI 主题数）独立存在，
        # 不能因为一次 load 就把它们丢掉（用户会看到"改了数字下次又变回去"）。
        if isinstance(raw.get("prefs"), dict):
            out["prefs"] = raw["prefs"]
        return out
    pid = _new_id()
    name = raw.get("provider") or (raw.get("base") or "").split("//")[-1].split("/")[0] or "默认配置"
    prof = {k: raw[k] for k in _FIELDS if raw.get(k) not in (None, "")}
    prof["name"] = name
    if raw.get("key"):
        prof["key"] = raw["key"]
    prof.setdefault("temperature", 0.3)
    return {"version": 2, "active": pid, "profiles": {pid: prof}}


def load() -> dict[str, Any]:
    with _LOCK:
        try:
            raw = json.loads(_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        d = _migrate(raw)
        profs = d.get("profiles") or {}
        if d.get("active") not in profs:
            d["active"] = next(iter(profs), "")
        return d


def _profile_view(pid: str, p: dict) -> dict[str, Any]:
    base = str(p.get("base") or "").rstrip("/")
    return {
        "id": pid,
        "name": p.get("name") or p.get("provider") or "未命名",
        "provider": p.get("provider") or "",
        "base": base, "model": p.get("model") or "",
        "temperature": _temp(p.get("temperature", 0.3)),
        "has_key": bool(p.get("key")),
        "key_hint": ("…" + str(p["key"])[-4:]) if p.get("key") else "",
        "configured": bool(base and p.get("model") and p.get("key")),
    }


DEFAULT_PREFS = {
    "topics": 10,             # AI 分类归纳出的主题数
    "direct_fallback": "",    # direct 失败策略；空 = 没在设置页选过，跟随环境变量/默认
    "open_mode": "",          # 桌面壳新窗口：external / internal；空 = 未选（桌面默认 internal）
    "capture_mode": "",       # 桌面采集：browser / direct / external；空 = 未选（桌面默认 browser）
    "close_action": "tray",   # 桌面壳关窗口：tray = 最小化到托盘 / quit = 退出程序
}

FALLBACK_MODES = ("ask", "auto", "per_room", "shared", "none")
OPEN_MODES = ("external", "internal")
CAPTURE_MODES = ("browser", "direct", "external")
CLOSE_ACTIONS = ("tray", "quit")


def _fallback_mode(v: Any) -> str:
    s = str(v or "").strip().lower()
    return s if s in FALLBACK_MODES else ""


def _open_mode(v: Any) -> str:
    s = str(v or "").strip().lower()
    return s if s in OPEN_MODES else ""


def _capture_mode(v: Any) -> str:
    s = str(v or "").strip().lower()
    return s if s in CAPTURE_MODES else ""


def _close_action(v: Any) -> str:
    s = str(v or "").strip().lower()
    return s if s in CLOSE_ACTIONS else ""


def prefs() -> dict[str, Any]:
    """与连接档案无关的全局偏好：AI 主题数、direct 失败策略、桌面新窗口方式。"""
    d = load()
    p = dict(DEFAULT_PREFS)
    p.update({k: v for k, v in (d.get("prefs") or {}).items() if k in DEFAULT_PREFS})
    try:
        p["topics"] = max(3, min(40, int(p["topics"])))
    except (TypeError, ValueError):
        p["topics"] = DEFAULT_PREFS["topics"]
    p["direct_fallback"] = _fallback_mode(p.get("direct_fallback"))
    p["open_mode"] = _open_mode(p.get("open_mode"))
    p["capture_mode"] = _capture_mode(p.get("capture_mode"))
    p["close_action"] = _close_action(p.get("close_action")) or "tray"
    return p


def direct_fallback() -> dict[str, str]:
    """direct 失败后的行为：环境变量 > 设置页 > 默认 ask。

    环境变量留给脚本/临时覆盖；没用环境变量时，设置页的选择才生效，
    这样改设置立即生效，不用重启采集代理。
    """
    stored = _fallback_mode(prefs().get("direct_fallback"))
    env = _fallback_mode(os.environ.get("LIVENOTES_DIRECT_FALLBACK"))
    mode = env or stored or "ask"
    return {"mode": mode, "stored": stored,
            "source": "env" if env else ("prefs" if stored else "default")}


def set_prefs(patch: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        d = load()
        p = dict(DEFAULT_PREFS)
        p.update(d.get("prefs") or {})
        for k, v in (patch or {}).items():
            if k not in DEFAULT_PREFS:
                continue
            if k == "topics":
                try:
                    v = max(3, min(40, int(v)))
                except (TypeError, ValueError):
                    continue
            elif k == "direct_fallback":
                v = _fallback_mode(v)
                if not v:
                    continue
            elif k == "open_mode":
                v = _open_mode(v)
                if not v:
                    continue
            elif k == "capture_mode":
                v = _capture_mode(v)
                if not v:
                    continue
            elif k == "close_action":
                v = _close_action(v)
                if not v:
                    continue
            p[k] = v
        d["prefs"] = p
        _write(d)
    return prefs()


def state() -> dict[str, Any]:
    """给前端的完整状态：所有档案 + 当前激活的那个 + 全局偏好。"""
    d = load()
    profs = [_profile_view(pid, p) for pid, p in (d.get("profiles") or {}).items()]
    active = d.get("active") or ""
    cur = next((p for p in profs if p["id"] == active), None)
    return {"active": active, "profiles": profs,
            "current": cur, "configured": bool(cur and cur["configured"]),
            "prefs": prefs(), "direct_fallback": direct_fallback()}


def upsert(patch: dict[str, Any]) -> dict[str, Any]:
    """新建或更新一份档案。patch 里不带 id 就是新建。"""
    d = load()
    profs = d.setdefault("profiles", {})
    pid = str(patch.get("id") or "").strip()
    created = False
    if not pid or pid not in profs:
        if patch.get("new"):
            pid = _new_id()
            profs[pid] = {}
            created = True
        elif d.get("active") in profs:
            # 兼容老客户端（保存时没带 id）：更新当前档案，而不是凭空造一份空 Key 的
            pid = d["active"]
        else:
            pid = _new_id()
            profs[pid] = {}
            created = True
    cur = profs[pid]
    for k in _FIELDS:
        if k in patch and patch[k] is not None:
            cur[k] = patch[k]
    if "temperature" in cur:
        cur["temperature"] = _temp(cur["temperature"])
    base = str(cur.get("base") or "").strip()
    if base:
        cur["base"] = base.rstrip("/")
    if patch.get("key"):
        cur["key"] = str(patch["key"]).strip()
    if patch.get("clear_key"):
        cur.pop("key", None)
    if not cur.get("name"):
        cur["name"] = cur.get("provider") or "未命名"
    if created or not d.get("active"):
        d["active"] = pid              # 新建的立刻生效，符合直觉
    _write(d)
    out = state()
    out["saved_id"] = pid
    return out


def activate(pid: str) -> dict[str, Any]:
    d = load()
    if pid not in (d.get("profiles") or {}):
        raise KeyError(pid)
    d["active"] = pid
    _write(d)
    return state()


def delete(pid: str) -> dict[str, Any]:
    d = load()
    profs = d.get("profiles") or {}
    if pid not in profs:
        raise KeyError(pid)
    profs.pop(pid)
    if d.get("active") == pid:
        d["active"] = next(iter(profs), "")
    _write(d)
    return state()


def duplicate(pid: str) -> dict[str, Any]:
    d = load()
    src = (d.get("profiles") or {}).get(pid)
    if not src:
        raise KeyError(pid)
    new_id = _new_id()
    cp = dict(src)
    cp["name"] = (str(src.get("name") or "配置") + " 副本")[:40]
    d["profiles"][new_id] = cp
    d["active"] = new_id
    _write(d)
    return state()


def resolve(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """表单值 > 当前档案 > 环境变量。"""
    d = load()
    p = (d.get("profiles") or {}).get(d.get("active") or "", {})
    o = overrides or {}
    base = str(o.get("base") or p.get("base") or LLM_BASE or "").rstrip("/")
    key = str(o.get("key") or p.get("key") or LLM_KEY or "")
    model = str(o.get("model") or p.get("model") or LLM_MODEL or "")
    return {
        "base": base, "key": key, "model": model,
        "temperature": _temp(p.get("temperature", 0.3)),
        "timeout": LLM_TIMEOUT,
        "total_timeout": LLM_TOTAL_TIMEOUT,
        "configured": bool(base and key and model),
        "source": "file" if (p.get("base") and p.get("key")) else
                  ("env" if (LLM_BASE and LLM_KEY and LLM_MODEL) else "none"),
    }


def public() -> dict[str, Any]:
    """兼容旧调用：等价于 state()。"""
    return state()
