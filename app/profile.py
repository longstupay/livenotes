# -*- coding: utf-8 -*-
"""用户口味：关注词（relevance 维度）。

显著性回答"这段有没有料"，关注词回答"跟我有没有关系"。两条轴缺一条，
高光就会变成"通用精华榜"而不是"我的精华榜"。

为什么用词表而不是话题分类：话题分类是预先定义的 6 类，覆盖不了
"FDE""OPC"这种行话。你说什么词，就关注什么词，零配置成本。

存 data/profile.json，改完立即生效（按 mtime 自动重载，不用重启服务）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import DATA_DIR

PATH = DATA_DIR / "profile.json"
# 命中 1 个词加多少分。0.6 太小：一句"就做OPC了"原本 0.88 分，加完 1.48 还够不到
# 1.5 的上榜线，会让人觉得"明明关注了却看不到"。0.9 能进榜，但不至于赖在榜首。
BOOST_ONE = float(os.environ.get("LIVENOTES_KW_BOOST", "0.9"))
_div = re.compile(r"[、,，;；/\s]+")


def _read() -> dict:
    if not PATH.is_file():
        return {}
    try:
        return json.loads(PATH.read_text(encoding="utf-8")) or {}
    except ValueError:
        return {}


_cache: dict = {"mtime": None, "words": []}


def keywords() -> list[str]:
    """当前关注词。按文件 mtime 缓存，改了立即生效。"""
    try:
        mtime = PATH.stat().st_mtime
    except OSError:
        return []
    if _cache["mtime"] != mtime:
        data = _read()
        ws = [str(w).strip() for w in (data.get("keywords") or []) if str(w).strip()]
        _cache["words"] = ws
        _cache["mtime"] = mtime
    return list(_cache["words"])


def save(words: list[str]) -> list[str]:
    """去重、保序、清空则可删文件。"""
    seen: list[str] = []
    for w in words:
        w = str(w).strip()
        if w and w not in seen:
            seen.append(w)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    data = _read()
    data["keywords"] = seen
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _cache["mtime"] = None if not seen else PATH.stat().st_mtime
    _cache["words"] = seen
    return seen


def add(text: str) -> list[str]:
    """从一句话里拆出词加进去，比如「FDE、OPC 政策」。"""
    new = [w for w in _div.split(text or "") if w]
    return save(keywords() + new)


def remove(text: str) -> list[str]:
    drop = {w for w in _div.split(text or "") if w}
    return save([w for w in keywords() if w not in drop])


def hits(text: str) -> list[str]:
    """这句命中了哪些关注词（大小写不敏感）。"""
    t = (text or "").lower()
    if not t:
        return []
    return [w for w in keywords() if w.lower() in t]


def boost(text: str) -> float:
    """相关性加分：命中 1 个 +0.9，2 个及以上 +1.8。"""
    n = len(hits(text))
    if n <= 0:
        return 0.0
    return BOOST_ONE if n == 1 else BOOST_ONE * 2
