# -*- coding: utf-8 -*-
"""全文稿：把逐字稿从"一条一句"重排成"一段一人"的连续文本。

为什么要另做一层而不是前端直接拼：合并规则（换人断、静音断、超长断）是文本语义，
放在后端才能保证导出、打印、以及以后接 LLM 时用的是同一套分段。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import store, titles

GAP_SECONDS = 8.0      # 同一个人停顿超过这个时长就另起一段
MAX_CHARS = 320        # 单段上限，避免一个人连讲五分钟变成一堵字墙
                       # （420 实测仍然一屏全是字，320 大约 4~6 句，读起来才像段落）
MIN_CHARS = 12         # 太短的段（"对。""嗯。"）先留着，前端会并到下一段


def _join(a: str, b: str) -> str:
    """拼接两句：去掉后一句开头重复的标点，中英之间补空格。"""
    b = (b or "").strip()
    if not a:
        return b
    if not b:
        return a
    if b[0] in "，。、！？；：,.!?;:":
        b = b[1:].lstrip()
    if not b:
        return a
    if a[-1].isascii() and b[0].isascii():
        return a + " " + b
    return a + b


def build_paragraphs(rows: list[dict], gap: float = GAP_SECONDS,
                     max_chars: int = MAX_CHARS) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        t0, t1 = float(r["t_start"]), float(r["t_end"])
        cur = out[-1] if out else None
        if (cur and cur["speaker_id"] == r["speaker_id"]
                and t0 - cur["t_end"] <= gap
                and len(cur["text"]) + len(text) <= max_chars):
            cur["text"] = _join(cur["text"], text)
            cur["t_end"] = max(cur["t_end"], t1)
            cur["n"] += 1
            cur["low"] = cur["low"] and r.get("value_tag") == "low"
        else:
            out.append({
                "speaker_id": r["speaker_id"],
                "label": store.speaker_display(r.get("speaker_label"),
                                               r.get("person_name"),
                                               r.get("person_state")),
                "t_start": t0, "t_end": t1, "n": 1,
                "text": text, "low": r.get("value_tag") == "low",
            })
    return out


def read(conn: sqlite3.Connection, sid: str, speakers: list[str] | None = None,
         exclude_low: bool = False) -> dict[str, Any]:
    session = store.get_session(conn, sid)
    if not session:
        raise ValueError("场次不存在")
    rows = store.session_transcript(conn, sid)
    total = len(rows)
    if speakers:
        rows = [r for r in rows if r["speaker_id"] in speakers]
    if exclude_low:
        rows = [r for r in rows if r.get("value_tag") != "low"]
    paras = build_paragraphs(rows)
    spk = store.speaker_totals(conn, sid)
    by_id = {p["speaker_id"]: p for p in spk}
    for p in paras:
        p["seconds"] = round(max(0.0, p["t_end"] - p["t_start"]), 1)
        p["color_index"] = (int("".join(ch for ch in p["speaker_id"] if ch.isdigit()) or 0))
    secs = sum(float(r["t_end"]) - float(r["t_start"]) for r in rows)
    chars = sum(len(r["text"] or "") for r in rows)
    return {
        "session": {
            "id": sid, "title": session["title"], "started_at": session["started_at"],
            # 时区一起给前端：时间码的 title 要显示"这是几点"，不能只给录音内偏移
            "started_utc": session.get("started_utc"),
            "tz_offset_min": session.get("tz_offset_min"),
            "duration_s": session["duration_s"], "source_mode": session["source_mode"],
            "audio_path": session["audio_path"],
        },
        "speakers": [{"speaker_id": p["speaker_id"],
                      "label": store.speaker_display(p["speaker_label"],
                                                     p.get("person_name"),
                                                     p.get("person_state")),
                      "seconds": round(float(p["seconds"] or 0), 1), "n": p["n"]}
                     for p in spk],
        "paragraphs": paras,
        "stats": {
            "utterances": len(rows), "utterances_total": total,
            "paragraphs": len(paras), "speech_seconds": round(secs, 1),
            "chars": chars,
            "chars_per_min": round(chars / (secs / 60), 1) if secs > 0 else 0,
        },
        "params": {"gap": GAP_SECONDS, "max_chars": MAX_CHARS},
    }


# ---------------------------------------------------------------- 标题建议

def _cached_titles(conn: sqlite3.Connection, sid: str) -> tuple[list[str], str]:
    """最近一次总结里顺手起的名字，以及它是不是模型起的（llm / local）。

    这是省 token 的关键：总结已经把内容读透了，改名框直接读这份结果，
    不再为了"起名"把稿子再传一遍模型。
    """
    row = store.latest_summary(conn, sid, "content")
    if not row:
        return [], ""
    p = row.get("payload") or {}
    kind = "llm" if p.get("engine_kind") == "llm" else "local"
    out: list[str] = []
    for t in [p.get("title")] + list(p.get("title_alts") or []):
        if isinstance(t, str) and t.strip() and t.strip() not in out:
            out.append(t.strip())
    return out, kind


def suggest_title(conn: sqlite3.Connection, sid: str, fresh: bool = False) -> dict[str, Any]:
    """给这一场起个名字的候选，按来源分组。

    读取顺序：总结起的名字（0 次模型调用）-> 原话摘录 -> 结构化标题。
    只有 fresh=True（用户点「换一个」）且配了大模型时，才现场再起一次。
    """
    session = store.get_session(conn, sid) or {}
    rows = store.session_transcript(conn, sid)
    cur = str(session.get("title") or "").strip()
    fallback = cur or "未命名场次"
    if not rows:
        return {"title": fallback, "alternatives": [], "source": "none",
                "groups": [], "cached": False}

    cached, kind = _cached_titles(conn, sid)
    # 只有模型起的名字才进「AI 起的名」；本地抽取的名字混进去会误导用户
    ai = [t for t in cached if kind == "llm" and t != cur]
    local = [t for t in cached if kind == "local" and t != cur]
    fresh_title = ""
    if fresh:
        good = [r for r in rows if r.get("value_tag") != "low"] or rows
        fresh_title = titles.llm_title(good, session_id=sid) or ""
        if fresh_title and fresh_title not in ai:
            ai.insert(0, fresh_title)

    groups: list[dict] = []
    if ai:
        groups.append({"kind": "ai", "label": "AI 起的名", "items": ai[:3]})
    quotes = [q for q in titles.pick_quotes(rows, 2) if q != cur]
    if quotes:
        groups.append({"kind": "quote", "label": "原话摘录", "items": quotes[:2]})
    st = titles.structural(conn, sid)
    structural_items = list(local[:1])
    if st and st != cur and st not in structural_items:
        structural_items.append(st)
    if structural_items:
        groups.append({"kind": "structural", "label": "结构信息（本地）",
                       "items": structural_items[:2]})

    flat: list[str] = []
    for g in groups:
        for it in g["items"]:
            if it not in flat:
                flat.append(it)
    if not flat:
        flat = [fallback]

    return {
        "title": flat[0],
        "alternatives": flat[1:4],
        "source": "llm" if (ai or fresh_title) else "extract",
        "groups": groups,
        "cached": bool(cached) and not fresh_title,
    }
