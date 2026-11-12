# -*- coding: utf-8 -*-
"""导出：Markdown 纪要 / SRT 字幕。

只导出数据库里真实存在的发言，不做二次加工——复盘的信任基础是"看到的就是当时听到的"。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Iterable

from . import store
from .models import SOURCE_MODE_META, fmt_ts


def _srt_ts(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _mode_name(code: str) -> str:
    return (SOURCE_MODE_META.get(code) or ("未知", ""))[0]


def to_srt(conn: sqlite3.Connection, sid: str) -> str:
    rows = store.session_transcript(conn, sid)
    out = []
    for i, r in enumerate(rows, 1):
        start = float(r["t_start"])
        end = max(float(r["t_end"]), start + 0.3)
        who = store.speaker_display(r["speaker_label"], r.get("person_name"),
                                    r.get("person_state"))
        text = (r["text"] or "").strip()
        out.append("%d\n%s --> %s\n%s\n" % (i, _srt_ts(start), _srt_ts(end),
                                            (who + "：" + text) if who else text))
    return "\n".join(out)


def to_markdown(conn: sqlite3.Connection, sid: str) -> str:
    s = store.get_session(conn, sid) or {}
    rows = store.session_transcript(conn, sid)
    spk = store.speaker_totals(conn, sid)
    marks = store.list_marks(conn, sid)
    dur = float(s.get("duration_s") or 0)
    out = ["# %s" % (s.get("title") or sid), ""]
    out.append("- 场次：`%s`" % sid)
    out.append("- 开始：%s　时长：%s" % (s.get("started_at") or "-", fmt_ts(dur)))
    out.append("- 采集：%s" % _mode_name(s.get("source_mode") or ""))
    out.append("- 发言：%d 条　导出时间：%s"
               % (len(rows), datetime.now().strftime("%Y-%m-%d %H:%M")))
    out.append("")
    if spk:
        out.append("## 说话人")
        out.append("")
        out.append("| 说话人 | 发言 | 时长 |")
        out.append("|---|---|---|")
        for p in spk:
            out.append("| %s | %d | %s |" % (
                store.speaker_display(p["speaker_label"], p.get("person_name"),
                                      p.get("person_state")),
                p["n"], fmt_ts(p["seconds"] or 0)))
        out.append("")
    if marks:
        out.append("## 打点")
        out.append("")
        for m in marks:
            out.append("- `%s` %s" % (fmt_ts(m["t"]), m.get("note") or ""))
        out.append("")
    out.append("## 逐字稿")
    out.append("")
    for r in rows:
        who = store.speaker_display(r["speaker_label"], r.get("person_name"),
                                    r.get("person_state"))
        flag = " ⚠重叠" if r.get("overlap") else ""
        out.append("**%s %s**%s　%s" % (fmt_ts(r["t_start"]), who, flag, (r["text"] or "").strip()))
        out.append("")
    return "\n".join(out)


def build(conn: sqlite3.Connection, sid: str, fmt: str) -> tuple[str, str, str]:
    """返回 (正文, MIME, 文件名)。"""
    fmt = (fmt or "md").lower()
    if fmt == "srt":
        return to_srt(conn, sid), "application/x-subrip; charset=utf-8", sid + ".srt"
    return to_markdown(conn, sid), "text/markdown; charset=utf-8", sid + ".md"
