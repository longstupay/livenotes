# -*- coding: utf-8 -*-
"""术语表：让 ASR 把 FDE、OPC 这类行话写对，也让搜索认得它。

为什么不能只靠"关注词"：关注词回答的是"我关心什么"，它只能加权打分；
而 ASR 把 FDE 听成"艾弗迪"、把 OPC 写成"O P C"时，加多少分都救不回来，
检索也搜不到。术语表补的正是"正确写法 + 常见误听"这一层。

三层能力，按顺序生效：
  1. 规范写法：大小写、空格、点划（F D E / F-D-E / fde -> FDE）；
  2. 别名替换：显式登记的误听写法（"欧匹西" -> OPC）；
  3. 拉丁文模糊纠错：全大写 token 与术语编辑距离 <=1 时替换（FDI -> FDE）。
     只对全大写 token 做，避免把 for/the 这类普通词改坏。

关注词会自动并入术语（只做规范写法，不猜别名）——少一个"要去哪里配"的心智负担。
文件 data/glossary.json，按 mtime 缓存，改完下一句就生效，不用重启。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from . import profile
from .config import DATA_DIR

PATH = Path(os.environ.get("LIVENOTES_GLOSSARY") or (DATA_DIR / "glossary.json"))
_MAX_TERMS = 500


def _read() -> list[dict]:
    if not PATH.is_file():
        return []
    try:
        data = json.loads(PATH.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        return []
    raw = data.get("terms") if isinstance(data, dict) else data
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"term": item}
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        if not term:
            continue
        aliases = [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()]
        out.append({"term": term, "aliases": aliases,
                    "note": str(item.get("note") or "").strip()})
    return out[:_MAX_TERMS]


_cache: dict = {"key": None, "entries": []}


def _key() -> tuple:
    try:
        gm = PATH.stat().st_mtime if PATH.is_file() else 0
    except OSError:
        gm = 0
    try:
        pm = profile.PATH.stat().st_mtime if profile.PATH.is_file() else 0
    except OSError:
        pm = 0
    return (gm, pm)


def entries() -> list[dict]:
    """术语表 + 关注词（关注词只作为规范写法，不带别名）。"""
    k = _key()
    if _cache["key"] != k:
        items = _read()
        seen = {e["term"].lower() for e in items}
        for w in profile.keywords():
            if w.lower() not in seen:
                items.append({"term": w, "aliases": [], "note": "关注词"})
        _cache["entries"] = items
        _cache["key"] = k
    return [dict(e) for e in _cache["entries"]]


def save(items: list[dict]) -> list[dict]:
    clean: list[dict] = []
    seen: set[str] = set()
    for item in items or []:
        term = str(item.get("term") or "").strip()
        if not term or term.lower() in seen:
            continue
        seen.add(term.lower())
        aliases = [str(a).strip() for a in (item.get("aliases") or [])
                   if str(a).strip() and str(a).strip().lower() != term.lower()]
        clean.append({"term": term, "aliases": unique(aliases),
                      "note": str(item.get("note") or "").strip()})
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps({"terms": clean[:_MAX_TERMS]}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    # 两个缓存都要废掉：只清 _cache 的话，correct() 读的 _rules 还挂在旧规则上，
    # 而它在 mtime 没变（同一秒内保存、或 exFAT/SMB 这种粗粒度时间戳）时不会重建。
    _cache["key"] = None
    _rules_cache["key"] = None
    return clean


def unique(seq: list[str]) -> list[str]:
    out: list[str] = []
    low: set[str] = set()
    for s in seq:
        if s.lower() not in low:
            low.add(s.lower())
            out.append(s)
    return out


def terms() -> list[str]:
    return [e["term"] for e in entries()]


def _is_ascii_term(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9+#._-]*", s or ""))


def _ascii_pattern(s: str) -> re.Pattern:
    """FDE / F D E / F-D-E / F.D.E 都认；前后不能粘着别的字母数字。"""
    chars = [re.escape(ch) for ch in s]
    body = r"[\s._\-·]*".join(chars)
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _alias_pattern(s: str) -> re.Pattern:
    if _is_ascii_term(s):
        return _ascii_pattern(s)
    return re.compile(re.escape(s))


_rules_cache: dict = {"key": None, "rules": [], "canon": set(), "ascii_terms": []}


def _rules():
    k = _key()
    if _rules_cache["key"] == k:
        return _rules_cache
    rules: list[tuple[re.Pattern, str, int]] = []
    canon: set[str] = set()
    ascii_terms: list[str] = []
    for e in entries():
        term = e["term"]
        canon.add(term.lower())
        if _is_ascii_term(term):
            ascii_terms.append(term)
            rules.append((_ascii_pattern(term), term, len(term)))
        for alias in e.get("aliases") or []:
            if not alias or alias.lower() == term.lower():
                continue
            rules.append((_alias_pattern(alias), term, len(alias)))
    # 长的先替换，避免 "OPC" 先把 "OPC 政策" 里的部分改掉再匹配别名
    rules.sort(key=lambda x: -x[2])
    _rules_cache.update({"key": k, "rules": [(p, r) for p, r, _n in rules],
                         "canon": canon, "ascii_terms": ascii_terms})
    return _rules_cache


def _lev(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 1:
        return 2
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]{2,}")


def correct(text: str) -> str:
    """把一句 ASR 文本按术语表纠错。幂等，异常时原样返回。"""
    if not text:
        return text
    try:
        r = _rules()
    except Exception:
        return text
    out = text
    for pat, rep in r["rules"]:
        try:
            out = pat.sub(rep, out)
        except re.error:
            continue
    # 只在全大写 token 上做模糊纠错：FDI -> FDE 值得，the -> FDE 不值得
    ascii_terms = r["ascii_terms"]
    if ascii_terms:
        def _fix(m: re.Match) -> str:
            tok = m.group(0)
            if not tok.isupper() or tok.lower() in r["canon"]:
                return tok
            best, bestd = None, 2
            for t in ascii_terms:
                if t.lower() == tok.lower():
                    return t
                d = _lev(tok, t)
                if d < bestd:
                    best, bestd = t, d
            return best if best and bestd <= 1 else tok
        out = _TOKEN.sub(_fix, out)
    return out


def expand_query(q: str) -> list[str]:
    """检索用：把 "OPC政策" 拆成 OPC + 政策，再补术语的别名。"""
    q = (q or "").strip()
    if not q:
        return []
    out = [q]
    low = q.lower()
    try:
        for e in entries():
            term = e["term"]
            if low == term.lower() or low in [a.lower() for a in e.get("aliases") or []]:
                out.append(term)
                out.extend(e.get("aliases") or [])
    except Exception:
        pass
    parts = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", q)
    if len(parts) > 1:
        out.extend(parts)
    return unique([x for x in out if x])[:12]


def reapply(conn: sqlite3.Connection, session_id: str | None = None) -> dict:
    """把术语修正补到已存的逐字稿上（人工改过的句子跳过）。"""
    where = " WHERE edited_at IS NULL" if _has_edited(conn) else ""
    params: list = []
    if session_id:
        where += " AND session_id=?" if where else " WHERE session_id=?"
        params.append(session_id)
    rows = conn.execute("SELECT id,text FROM utterances" + where, params).fetchall()
    changed = 0
    for r in rows:
        new = correct(r["text"] or "")
        if new and new != (r["text"] or ""):
            conn.execute("UPDATE utterances SET text=?, ai_fp=NULL, features_json=NULL "
                         "WHERE id=?", (new, r["id"]))
            changed += 1
    conn.commit()
    return {"scanned": len(rows), "changed": changed}


def _has_edited(conn: sqlite3.Connection) -> bool:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(utterances)")}
    return "edited_at" in cols
