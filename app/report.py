# -*- coding: utf-8 -*-
"""周报 / 复盘：把一段时间内的多场直播汇总成一篇可沉淀的文档。

为什么单独成模块，而不是复用单场导出：

1. **周报的单位是「多场」**，要回答的是「这一周聊了什么、哪几场值得回看」；
   单场导出是逐字稿，直接拼接只会得到一份更长的逐字稿。
2. **云端只放能公开的内容**：摘要、要点、原话、妙记链接。局域网 reader 地址
   一律不写进文档——转发出去打不开，等于给了个假入口。
3. **没有总结的场次也要有东西**：优先用已生成的总结；没有就退回高光片段，
   再没有就取高权重原句。报告不能因为某一场没点过「生成总结」就整段空白。

复盘模板在此基础上留出「结论与行动」人工填写区；周报只陈述事实。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

from . import store, timefmt
from .config import REPORTS_DIR
from .models import category_label

KINDS = {"weekly": "直播周报", "retro": "直播复盘"}
MAX_POINTS = 4
MAX_TOPICS = 6
MAX_QUOTE = 120
MAX_SESSIONS = 30
DETAIL_MIN_S = 180       # 短于 3 分钟且没生成过总结的场次只列标题，不展开
REPORT_RETENTION_DAYS = 30

_WEEKDAYS = "一二三四五六日"
_RANGE_SEP = re.compile(r"\s*(?:~|～|至|到|—|－)\s*")
_GENERIC_TOPICS = {"", "综合", "综合/未归类", "未归类", "其他", "其它"}


def _day(raw) -> date | None:
    """从 started_at（本地 naive ISO）取日期。解析不了就当不在范围内。"""
    s = str(raw or "").strip()
    if len(s) >= 10:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _parse_day_token(token: str, today: date) -> date:
    t = str(token or "").strip().replace("/", "-").replace(".", "-")
    parts = t.split("-")
    try:
        if len(parts) == 3:
            year = int(parts[0])
            # 三位以内的年份多半是把「11-03-09」这种写法拆错了；静默生成一篇
            # 公元 11 年的报告比直接报错更糟。
            if year < 1000:
                raise ValueError
            return date(year, int(parts[1]), int(parts[2]))
        if len(parts) == 2:
            return date(today.year, int(parts[0]), int(parts[1]))
    except (TypeError, ValueError):
        pass
    raise ValueError("日期看不懂：%s" % token)


def parse_range(text: str = "", today: date | None = None) -> dict:
    """解析用户给的范围，返回 {since, until, label}。

    支持：空（最近 7 天）、本周、上周、今天、最近 N 天 / N天、
    2026-11-03、2026-11-03~2026-11-09、11-03~11-09。
    """
    raw = str(text or "").strip()
    today = today or date.today()
    low = raw.lower().replace(" ", "")
    if not raw:
        since, until = today - timedelta(days=6), today
    elif low in ("本周", "这周", "thisweek"):
        since, until = _week_start(today), today
    elif low in ("上周", "lastweek"):
        this_monday = _week_start(today)
        since, until = this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    elif low in ("今天", "今日", "today"):
        since = until = today
    else:
        m = re.fullmatch(r"(?:最近)?(\d{1,3})\s*(?:天|d|day|days)", low)
        if m:
            n = max(1, min(366, int(m.group(1))))
            since, until = today - timedelta(days=n - 1), today
        else:
            parts = _RANGE_SEP.split(raw, maxsplit=1)
            if len(parts) == 2 and parts[0] and parts[1]:
                since = _parse_day_token(parts[0], today)
                until = _parse_day_token(parts[1], today)
            elif parts and len(parts) == 1:
                since = until = _parse_day_token(parts[0], today)
            else:
                raise ValueError("范围看不懂。例：上周、7天、11-03~11-09")
    if since > until:
        since, until = until, since
    return {"since": since, "until": until, "label": range_label(since, until)}


def range_label(since: date, until: date) -> str:
    if since == until:
        return since.isoformat()
    if since.year == until.year:
        # 只省略年份，不省略月份：写成「11-03 ~ 09」会被读成 3 号到 9 号，
        # 跨月时更歧义。
        return "%s ~ %s" % (since.isoformat(), until.strftime("%m-%d"))
    return "%s ~ %s" % (since.isoformat(), until.isoformat())


def _day_label(d: date) -> str:
    return "%s 周%s" % (d.strftime("%m-%d"), _WEEKDAYS[d.weekday()])


def _date_span(since: date, until: date) -> str:
    if since == until:
        return since.isoformat()
    if since.year == until.year:
        return "%s 至 %s" % (since.isoformat(), until.strftime("%m-%d"))
    return "%s 至 %s" % (since.isoformat(), until.isoformat())


def duration_text(seconds: float) -> str:
    s = max(0, int(round(float(seconds or 0))))
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h and m:
        return "%d 小时 %d 分" % (h, m)
    if h:
        return "%d 小时" % h
    return "%d 分钟" % max(1, m) if s else "0 分钟"


def _clean(text, limit: int = 0) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:limit] if limit and len(s) > limit else s


def _point_title(text, limit: int = 30) -> str:
    t = _clean(text, limit)
    if t in _GENERIC_TOPICS or t.startswith("话题：综合"):
        return ""
    return t


def _overview_text(text) -> str:
    """过滤单场总结里面向网页的套话（「点查看原文」在云文档里没有意义）。"""
    s = _clean(text, 200)
    if not s or "点「查看原文」" in s or s.startswith("本场共纳入"):
        return ""
    return s


def _whole_filters(f: dict) -> bool:
    return (not str(f.get("q") or "").strip()
            and not f.get("categories") and not f.get("speakers")
            and f.get("t_from") is None and f.get("t_to") is None)


def _whole_summary(conn, sid: str) -> dict:
    """取最近一份「整场、无筛选」的已生成总结。

    只认整场口径：筛选后的总结是某个片段的结论，拿进周报会把局部当全局。
    最近一份没有要点时继续往前找，避免一条空结果挡住更早的完整总结。
    """
    for r in store.recent_summaries(conn, sid, "content", 20):
        try:
            f = json.loads(r.get("filter_json") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(f, dict) or not _whole_filters(f):
            continue
        try:
            payload = json.loads(r.get("payload_json") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and (payload.get("points") or payload.get("overview")):
            return payload
    return {}


def _session_topics(conn, sid: str, payload: dict) -> list[str]:
    raw = list((payload or {}).get("topics") or [])
    if not raw:
        raw = list(store.get_ai_topics(conn, sid) or [])
    if not raw:
        raw = [t for t, _ in store.topic_tally(conn, sid)]
    out: list[str] = []
    for t in raw:
        t = _clean(t, 20)
        if t and t not in out and t not in _GENERIC_TOPICS:
            out.append(t)
    return out[:MAX_TOPICS]


def _points_from_summary(payload: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for p in (payload.get("points") or [])[:MAX_POINTS * 2]:
        if not isinstance(p, dict):
            continue
        title = _point_title(p.get("title"))
        gist = _clean(p.get("gist"), 160)
        ev = p.get("evidence") or []
        quote = speaker = ""
        qt = None
        if ev and isinstance(ev[0], dict):
            e = ev[0]
            quote = _clean(e.get("quote") or e.get("text"), MAX_QUOTE)
            speaker = _clean(e.get("speaker_label") or e.get("speaker"), 24)
            try:
                qt = float(e.get("t_start"))
            except (TypeError, ValueError):
                qt = None
        if not (title or gist or quote):
            continue
        key = (title + "|" + gist + "|" + quote).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            t_from = float(p.get("t_from"))
        except (TypeError, ValueError):
            t_from = qt
        try:
            t_to = float(p.get("t_to"))
        except (TypeError, ValueError):
            t_to = None
        out.append({"title": title, "gist": gist, "quote": quote,
                    "speaker": speaker, "t_from": t_from, "t_to": t_to,
                    "quote_t": qt})
        if len(out) >= MAX_POINTS:
            break
    return out


def _points_from_highlights(conn, sid: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for h in store.list_highlights(conn, sid, limit=MAX_POINTS * 2):
        quote = _clean(h.get("text"), MAX_QUOTE)
        if not quote or quote.lower() in seen:
            continue
        seen.add(quote.lower())
        out.append({
            "title": _clean(h.get("why") or "高光片段", 20),
            "gist": "",
            "quote": quote,
            "speaker": _clean(h.get("speaker_label"), 24),
            "t_from": float(h.get("t_start") or h.get("peak_t") or 0),
            "t_to": float(h.get("t_end") or 0) or None,
            "quote_t": float(h.get("t_start") or h.get("peak_t") or 0),
        })
        if len(out) >= MAX_POINTS:
            break
    return out


def _points_from_utterances(conn, sid: str) -> list[dict]:
    """最后兜底：按重要度取原句。不调模型、不改库，只读。"""
    rows = [dict(r) for r in conn.execute(
        "SELECT t_start,t_end,speaker_label,text,topic,category,importance "
        "FROM utterances WHERE session_id=? AND COALESCE(overlap,0)=0 "
        "AND COALESCE(category,'') IN ('substantive','question_consult') "
        "AND LENGTH(COALESCE(text,''))>=8 "
        "ORDER BY importance DESC, LENGTH(text) DESC LIMIT ?",
        (sid, MAX_POINTS * 3))]
    picked: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        quote = _clean(r.get("text"), MAX_QUOTE)
        if not quote or quote.lower() in seen:
            continue
        seen.add(quote.lower())
        title = _point_title(r.get("topic"), 20) or category_label(
            str(r.get("category") or ""))
        picked.append({
            "title": title or "要点", "gist": "", "quote": quote,
            "speaker": _clean(r.get("speaker_label"), 24),
            "t_from": float(r.get("t_start") or 0),
            "t_to": float(r.get("t_end") or 0) or None,
            "quote_t": float(r.get("t_start") or 0),
        })
        if len(picked) >= MAX_POINTS:
            break
    return sorted(picked, key=lambda p: p.get("t_from") or 0)


def _fallback_overview(session: dict, topics: list[str],
                       has_summary: bool = False) -> str:
    if topics:
        return "本场主要围绕「%s」展开。" % "」「".join(topics[:3])
    n = int(session.get("n_utterances") or 0)
    if n:
        return ("本场共 %d 条发言。" % n if has_summary
                else "本场共 %d 条发言，还没有生成总结。" % n)
    return "本场没有可用字幕。"


def _material(conn, session: dict) -> dict:
    sid = str(session.get("id") or "")
    payload = _whole_summary(conn, sid)
    points = _points_from_summary(payload)
    source = "总结"
    if not points:
        points = _points_from_highlights(conn, sid)
        source = "高光"
    if not points:
        points = _points_from_utterances(conn, sid)
        source = "原句"
    topics = _session_topics(conn, sid, payload)
    overview = _overview_text(payload.get("overview")) or _fallback_overview(
        session, topics, has_summary=bool(payload))
    d = _day(session.get("started_at"))
    return {
        "session_id": sid,
        "title": _clean(session.get("title"), 80) or sid,
        "day": d,
        "started_at": str(session.get("started_at") or ""),
        "duration_s": float(session.get("duration_s") or 0),
        "n_utterances": int(session.get("n_utterances") or 0),
        "minutes_url": str(session.get("minutes_url") or "").strip(),
        "overview": overview,
        "topics": topics,
        "points": points,
        "source": source if points else "",
        "has_summary": bool(payload),
    }


def sessions_between(conn, since: date, until: date) -> list[dict]:
    out = []
    for s in store.list_sessions(conn):
        d = _day(s.get("started_at"))
        if d is None or not (since <= d <= until):
            continue
        if int(s.get("n_utterances") or 0) <= 0:
            continue
        s = dict(s)
        s["_day"] = d
        out.append(s)
    out.sort(key=lambda s: (str(s.get("started_at") or ""), str(s.get("id") or "")))
    return out


def _group_days(materials: list[dict]) -> "OrderedDict[date, list[dict]]":
    groups: "OrderedDict[date, list[dict]]" = OrderedDict()
    for m in materials:
        groups.setdefault(m["day"], []).append(m)
    return groups


def _span_text(p: dict) -> str:
    try:
        t0 = float(p.get("t_from") or 0)
    except (TypeError, ValueError):
        t0 = 0.0
    try:
        t1 = float(p.get("t_to") or 0)
    except (TypeError, ValueError):
        t1 = 0.0
    start = timefmt.offset(t0)
    if t1 > t0 + 0.5:
        return "%s–%s" % (start, timefmt.offset(t1))
    return start


def _point_line(p: dict) -> str:
    head = "**%s**（%s）" % (_point_title(p.get("title")) or "要点", _span_text(p))
    body = re.sub(r"[。；;，,]+$", "", _clean(p.get("gist"), 160))
    quote = _clean(p.get("quote"), MAX_QUOTE)
    who = _clean(p.get("speaker"), 24)
    parts = [body] if body else []
    if quote and quote not in body:
        parts.append("原话：「%s」%s" % (quote, (" —— " + who) if who else ""))
    elif quote and who:
        parts.append("—— " + who)
    return "- %s：%s" % (head, "；".join(parts)) if parts else "- %s" % head


def _material_stats(materials: list[dict]) -> dict:
    total = sum(m["duration_s"] for m in materials)
    days = len({m["day"] for m in materials})
    detail = [m for m in materials if m.get("detail")]
    return {
        "sessions": len(materials),
        "days": days,
        "duration_s": total,
        "detail": len(detail),
        "short": sum(1 for m in materials if m.get("compact_reason") == "short"),
        "with_summary": sum(1 for m in materials if m["has_summary"]),
        "with_minutes": sum(1 for m in materials if m["minutes_url"]),
        "without_summary": [m for m in materials if not m["has_summary"]],
        "without_minutes": [m for m in materials if not m["minutes_url"]],
        "topics": _cross_topics(materials),
    }


def _cross_topics(materials: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for m in materials:
        for t in m["topics"]:
            groups.setdefault(t, []).append(m)
    rows = [(t, ms) for t, ms in groups.items() if len(ms) >= 2]
    rows.sort(key=lambda kv: (-len(kv[1]), kv[0]))
    return rows[:8]


def _topic_summary(topics: list[tuple[str, list[dict]]]) -> str:
    if not topics:
        return "没有跨场重复的话题。"
    return "、".join("%s（%d 场）" % (t, len(ms)) for t, ms in topics)


def _coverage_lines(stats: dict) -> list[str]:
    lines = []
    if stats.get("short"):
        lines.append("- 展开：%d 场重点；%d 场短场（<3 分钟）只列标题"
                     % (stats.get("detail", 0), stats["short"]))
    if stats.get("truncated"):
        lines.append("- 重点场次过多：只展开 %d 场，另有 %d 场只列标题"
                     % (stats.get("detail", 0), stats["truncated"]))
    if stats["with_summary"] < stats["sessions"]:
        names = "、".join("《%s》" % m["title"] for m in stats["without_summary"][:3])
        more = "" if len(stats["without_summary"]) <= 3 else " 等"
        lines.append("- 未生成总结：%s%s（已尽量用高光/原句兜底）" % (names, more))
    if stats["with_minutes"] < stats["sessions"]:
        names = "、".join("《%s》" % m["title"] for m in stats["without_minutes"][:3])
        more = "" if len(stats["without_minutes"]) <= 3 else " 等"
        lines.append("- 未回填妙记：%s%s" % (names, more))
    return lines


def _short_line(materials: list[dict]) -> str:
    names = "、".join("《%s》（%s）" % (m["title"], duration_text(m["duration_s"]))
                     for m in materials)
    return "未展开：" + names


def _session_head(m: dict) -> list[str]:
    meta = ["时长：%s" % duration_text(m["duration_s"])]
    if m["n_utterances"]:
        meta.append("发言 %d 条" % m["n_utterances"])
    lines = ["### 《%s》" % m["title"], "", "- " + " · ".join(meta)]
    if m["minutes_url"]:
        lines.append("- 妙记：[打开回放](%s)" % m["minutes_url"])
    return lines


def _session_body(m: dict) -> list[str]:
    lines = ["", "**概述**：%s" % m["overview"], ""]
    if m["points"]:
        if m["source"] in ("高光", "原句"):
            why = ("本场没有已生成总结" if not m["has_summary"]
                   else "总结里没有可用的要点")
            lines.append("**关键内容**（%s，以下为自动提取）" % why)
        else:
            lines.append("**关键内容**")
        lines += [_point_line(p) for p in m["points"]]
    else:
        lines.append("**关键内容**：这场还没有可用的总结或高光片段。")
    return lines


def render_weekly(materials: list[dict], since: date, until: date,
                  stats: dict) -> str:
    title = "%s · %s" % (KINDS["weekly"], range_label(since, until))
    lines = [
        "# " + title, "",
        "> %s，共 %d 场、%s。" % (
            _date_span(since, until), stats["sessions"],
            duration_text(stats["duration_s"])),
        "> 内容来自逐字稿与已生成总结；时间码为录音内偏移，回放请用各场妙记链接。",
        "", "## 概览", "",
        "- 场次：%d 场，分布在 %d 天" % (stats["sessions"], stats["days"]),
        "- 总时长：%s（平均每场 %s）" % (
            duration_text(stats["duration_s"]),
            duration_text(stats["duration_s"] / max(1, stats["sessions"]))),
        "- 素材：%d 场有已生成总结，%d 场有妙记回放" % (
            stats["with_summary"], stats["with_minutes"]),
        "- 高频话题：%s" % _topic_summary(stats["topics"]),
    ]
    cov = _coverage_lines(stats)
    if cov:
        lines += cov
    lines += ["", "## 重点场次", ""]
    detail = [m for m in materials if m.get("detail")]
    if not detail:
        lines.append("（没有超过 3 分钟或生成过总结的场次）")
    for m in detail:
        tail = " · [妙记](%s)" % m["minutes_url"] if m["minutes_url"] else ""
        lines.append("- %s · 《%s》 · %s%s" % (
            _day_label(m["day"]), m["title"], duration_text(m["duration_s"]), tail))
    for d, group in _group_days(materials).items():
        lines += ["", "## " + _day_label(d), ""]
        shown = [m for m in group if m.get("detail")]
        rest = [m for m in group if not m.get("detail")]
        for i, m in enumerate(shown):
            if i:
                lines.append("")
            lines += _session_head(m) + _session_body(m)
        if rest:
            if shown:
                lines.append("")
            lines.append(_short_line(rest))
    lines += ["", "## 跨场观察", "",
              "- 重复话题：%s" % _topic_summary(stats["topics"])]
    return "\n".join(lines).rstrip() + "\n"


def render_retro(materials: list[dict], since: date, until: date,
                 stats: dict) -> str:
    title = "%s · %s" % (KINDS["retro"], range_label(since, until))
    lines = [
        "# " + title, "",
        "> %s，共 %d 场、%s。复盘模板：事实部分自动汇总，结论与行动留给人填。" % (
            _date_span(since, until), stats["sessions"],
            duration_text(stats["duration_s"])),
        "", "## 一、数据面", "",
        "- 场次：%d 场，分布在 %d 天" % (stats["sessions"], stats["days"]),
        "- 总时长：%s（平均每场 %s）" % (
            duration_text(stats["duration_s"]),
            duration_text(stats["duration_s"] / max(1, stats["sessions"]))),
        "- 素材：%d 场有已生成总结，%d 场有妙记回放" % (
            stats["with_summary"], stats["with_minutes"]),
        "- 高频话题：%s" % _topic_summary(stats["topics"]),
    ]
    cov = _coverage_lines(stats)
    if cov:
        lines += cov
    lines += ["", "## 二、逐场回顾", ""]
    for d, group in _group_days(materials).items():
        lines += ["### " + _day_label(d), ""]
        shown = [m for m in group if m.get("detail")]
        rest = [m for m in group if not m.get("detail")]
        for m in shown:
            lines += _session_head(m) + _session_body(m) + [""]
        if rest:
            lines += [_short_line(rest), ""]
    lines += ["## 三、跨场观察", "",
              "- 重复话题：%s" % _topic_summary(stats["topics"]),
              "- 场次分布：%s" % "、".join(
                  "%s %d 场" % (_day_label(d), len(g))
                  for d, g in _group_days(materials).items()),
              "", "## 四、结论与行动（待人工确认）", "",
              "### 效果好的", "", "- （待填）", "",
              "### 需要改进的", "", "- （待填）", "",
              "### 下周动作", "", "- [ ] （待填）"]
    return "\n".join(lines).rstrip() + "\n"


def build(conn, kind: str = "weekly", since: date | None = None,
          until: date | None = None, out_dir: Path | str | None = None,
          write: bool = True) -> dict:
    """生成报告，返回 {kind,title,since,until,markdown,path,sessions,stats}。"""
    kind = (kind or "weekly").lower()
    if kind not in KINDS:
        raise ValueError("未知报告类型：%s" % kind)
    today = date.today()
    since = since or (today - timedelta(days=6))
    until = until or today
    if since > until:
        since, until = until, since
    sessions = sessions_between(conn, since, until)
    materials = [_material(conn, s) for s in sessions]
    for m in materials:
        m["detail"] = bool(m["has_summary"] or m["duration_s"] >= DETAIL_MIN_S)
        m["compact_reason"] = "" if m["detail"] else "short"
    # 重点场次太多时按「有总结优先、时长次之」保留，其余只列标题；
    # 这样不会像「只取最近 N 场」那样把前几天整个丢掉。
    detail = [m for m in materials if m["detail"]]
    truncated = max(0, len(detail) - MAX_SESSIONS)
    if truncated:
        keep = {m["session_id"] for m in sorted(
            detail, key=lambda m: (m["has_summary"], m["duration_s"]),
            reverse=True)[:MAX_SESSIONS]}
        for m in detail:
            if m["session_id"] not in keep:
                m["detail"] = False
                m["compact_reason"] = "cap"
    stats = _material_stats(materials)
    stats["truncated"] = truncated
    markdown = (render_retro if kind == "retro" else render_weekly)(
        materials, since, until, stats)
    path = None
    if write:
        out = Path(out_dir) if out_dir else REPORTS_DIR
        out.mkdir(parents=True, exist_ok=True)
        path = out / ("%s_%s_%s.md" % (kind, since.isoformat(), until.isoformat()))
        path.write_text(markdown, encoding="utf-8")
    return {
        "kind": kind,
        "title": "%s · %s" % (KINDS[kind], range_label(since, until)),
        "since": since, "until": until,
        "markdown": markdown, "path": path,
        "sessions": materials, "stats": stats,
    }


def cleanup(days: int = REPORT_RETENTION_DAYS) -> int:
    """清掉过期报告；报告是中间产物，需要长期留存时复制到别处。"""
    if not REPORTS_DIR.is_dir():
        return 0
    cutoff = time.time() - max(1, int(days)) * 86400
    n = 0
    for p in REPORTS_DIR.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                n += 1
        except OSError:
            pass
    return n
