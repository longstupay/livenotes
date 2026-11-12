# -*- coding: utf-8 -*-
"""主播业务画像：只做可从逐字稿算出来的事，不编造。

输入是一场的 utterances（说话人标签已由实时聚类给出，可人工改名）。
输出是维度 + 证据时间点，而不是一个"看起来精确"的总分。所有指标都能点回原文。
"""
from __future__ import annotations

import re
from typing import Any

from . import store

SCRIPT_GROUPS = {
    "welcome":    (r"欢迎|来了|新来的|家人们|宝子们|欢迎新", "欢迎承接"),
    "thanks":     (r"感谢|谢谢|礼物|火箭|嘉年华|小心心", "感谢打赏"),
    "follow":     (r"关注|粉丝团|灯牌|点亮|加团|关注一下", "引导关注"),
    "conversion": (r"下单|链接|小黄车|购物车|拍下|库存|优惠|福利|价格|秒杀|改价", "转化引导"),
    "retention":  (r"别走|马上|接下来|抽奖|福袋|整点|蹲一下|留在", "留人"),
    "guest":      (r"连麦|上麦|接麦|嘉宾|哥哥|姐姐", "连麦互动"),
}
RISK_RE = re.compile(r"微信|加\s*[Vv]|vx|私下|最低价|全网最低|全网第一|销量第一|包治|稳赚|返利")
FILLER_RE = re.compile(r"嗯+|啊+|呃+|那个|就是说|然后呢|反正")
Q_RE = re.compile(r"[?？]|(吗|呢|嘛|是不是|对不对|好不好|要不要|有没有)[。！!？?]*$")
CJK_RE = re.compile(r"[一-鿿A-Za-z0-9]")


def _chars(text: str) -> int:
    return len(CJK_RE.findall(text or ""))


def _fmt(seconds: float) -> str:
    s = max(0, int(seconds))
    return "%02d:%02d" % (s // 60, s % 60)


def analyze(conn, sid: str) -> dict:
    meta = store.get_meta(conn, sid)
    if not meta:
        return {"error": "场次不存在"}
    session = meta["session"]
    rows = store.query_utterances(conn, {"session_id": sid}, limit=5000)
    duration = float(session.get("duration_s") or 0) or (rows[-1]["t_end"] if rows else 0)

    # 主播识别：优先自动/人工标签，其次按发言时长最多的人
    seconds: dict[str, float] = {}
    labels: dict[str, str] = {}
    for r in rows:
        sid_ = r["speaker_id"]
        seconds[sid_] = seconds.get(sid_, 0.0) + max(0.0, r["t_end"] - r["t_start"])
        labels[sid_] = r["speaker_label"]
    host = next((k for k, v in labels.items() if "主播" in v), None)
    if host is None and seconds:
        host = max(seconds, key=seconds.get)
    host_rows = [r for r in rows if r["speaker_id"] == host]
    guest_rows = [r for r in rows if r["speaker_id"] != host]
    host_s = seconds.get(host, 0.0) if host else 0.0
    guest_s = sum(v for k, v in seconds.items() if k != host)

    host_text = "".join(r["text"] for r in host_rows)
    host_chars = _chars(host_text)
    host_n = len(host_rows)
    cpm = host_chars / (host_s / 60) if host_s >= 8 else 0.0
    avg_len = host_s / host_n if host_n else 0.0
    longest = max((r["t_end"] - r["t_start"] for r in host_rows), default=0.0)

    hits: dict[str, list[dict]] = {}
    for key, (pat, _label) in SCRIPT_GROUPS.items():
        rx = re.compile(pat)
        found = [r for r in host_rows if rx.search(r["text"])]
        hits[key] = [{"t": round(r["t_start"], 1), "text": r["text"]} for r in found[:4]]
    questions = [r for r in host_rows if Q_RE.search(r["text"])]
    fillers = FILLER_RE.findall(host_text)
    risks = [r for r in host_rows if RISK_RE.search(r["text"])]

    # 冷场：相邻发言间隔 > 20s（去掉开场前与结尾后的静默）
    cold, cold_total = 0, 0.0
    ordered = sorted(rows, key=lambda r: r["t_start"])
    for a, b in zip(ordered, ordered[1:]):
        gap = b["t_start"] - a["t_end"]
        if gap > 20:
            cold += 1
            cold_total += gap

    # 连麦互动：换手次数，以及嘉宾说完 5 秒内主播接话的比例
    switches = sum(1 for a, b in zip(ordered, ordered[1:]) if a["speaker_id"] != b["speaker_id"])
    replies, chances = 0, 0
    for a, b in zip(ordered, ordered[1:]):
        if a["speaker_id"] != host:
            chances += 1
            if b["speaker_id"] == host and 0 <= b["t_start"] - a["t_end"] <= 5:
                replies += 1

    dims = []

    def add(key, label, value, score, hint, ev=None):
        dims.append({"key": key, "label": label, "value": value,
                     "score": round(max(0.0, min(1.0, score)), 2), "hint": hint,
                     "evidence": ev or []})

    ratio = host_s / duration if duration else 0
    add("talk", "主播开口占比", "%.0f%%" % (ratio * 100),
        min(1.0, ratio / 0.65), "主播占全场人声的比例；太低说明话量不足，太高可能缺少互动",
        [{"t": round(r["t_start"], 1), "text": r["text"]} for r in host_rows[:2]])
    pace_score = 0.0
    if cpm:
        pace_score = 1.0 if 160 <= cpm <= 340 else max(0.0, 1 - abs(cpm - 250) / 400)
    add("pace", "语速", ("%d 字/分" % cpm) if cpm else "样本不足", pace_score,
        "中文直播常见 200~320 字/分；过快听感压迫，过慢容易掉人")
    add("interaction", "提问/互动", "%d 次提问" % len(questions),
        min(1.0, len(questions) / max(1.0, host_s / 120)), "平均每 2 分钟至少一次提问更容易带节奏",
        [{"t": round(r["t_start"], 1), "text": r["text"]} for r in questions[:3]])
    covered = sum(1 for k in SCRIPT_GROUPS if hits[k])
    add("script", "话术覆盖", "%d / %d 类" % (covered, len(SCRIPT_GROUPS)), covered / len(SCRIPT_GROUPS),
        "欢迎/感谢/关注/转化/留人/连麦六类话术的出现情况")
    add("cold", "冷场", "%d 次 · 共 %.0f 秒" % (cold, cold_total),
        max(0.0, 1 - cold_total / max(1.0, duration) * 3), "相邻发言超过 20 秒记为冷场")
    add("replies", "嘉宾接话", ("%d / %d" % (replies, chances)) if chances else "无连麦发言",
        (replies / chances) if chances else 1.0, "嘉宾说完 5 秒内主播接话的比例",
        [{"t": round(r["t_start"], 1), "text": r["text"]} for r in guest_rows[:2]])
    add("risk", "合规风险", "%d 处" % len(risks), 1 - min(1.0, len(risks) / 5),
        "命中导流/绝对化用语等简单词表，仅作提醒，不替代平台审核",
        [{"t": round(r["t_start"], 1), "text": r["text"]} for r in risks[:3]])

    weight = {"talk": .15, "pace": .1, "interaction": .2, "script": .25,
              "cold": .1, "replies": .1, "risk": .1}
    overall = sum(d["score"] * weight[d["key"]] for d in dims)
    grade = "A" if overall >= .8 else "B" if overall >= .6 else "C"

    stale = [d["key"] for d in dims if d["value"] == "样本不足"]
    caveats = [
        "这是从逐字稿统计出来的参考画像，不是对主播能力的最终评价。",
        "背景音乐、错字、说话人聚类错误都会影响指标；请结合原文证据看。",
    ]
    if stale:
        caveats.append("主播有效发言不足，语速指标仅供参考。")
    if ratio < .25:
        caveats.append("主播开口占比偏低，确认一下是否把嘉宾误标成了主播。")

    return {
        "session_id": sid,
        "session": {"title": session.get("title"), "duration_s": round(duration, 1),
                    "source_mode": session.get("source_mode")},
        "host": {"speaker_id": host, "label": labels.get(host, "未识别"),
                 "seconds": round(host_s, 1), "utterances": host_n,
                 "chars": host_chars},
        "guest": {"seconds": round(guest_s, 1), "utterances": len(guest_rows)},
        "overall": round(overall, 2), "grade": grade,
        "dimensions": dims,
        "script_hits": {k: v for k, v in hits.items() if v},
        "script_labels": {k: v[1] for k, v in SCRIPT_GROUPS.items()},
        "fillers": len(fillers),
        "cold": {"count": cold, "seconds": round(cold_total, 1)},
        "switches": switches,
        "caveats": caveats,
        "coverage": round(sum(max(0.0, r["t_end"] - r["t_start"]) for r in rows) / duration, 3)
                    if duration else 0,
    }

def cross(conn, label: str | None = None, days: int = 30) -> dict:
    """跨场次画像：把同一个「说话人标签」在多场的表现合起来看。

    诚实的边界：身份是按**标签名**对齐的（每场里都叫「主播」的那个），
    不是按声纹——同一场里改名成不同名字就对不上了。所以这里只做聚合统计，
    并明确告诉用户这个前提，不假装是"同一个人"。
    """
    rows = store.list_sessions(conn)
    names: dict[str, dict] = {}
    per_session: dict[str, list[dict]] = {}
    for s in rows:
        sid = s["id"]
        totals = store.speaker_totals(conn, sid)
        if not totals:
            continue
        utts = store.session_transcript(conn, sid)
        by_spk: dict[str, list[dict]] = {}
        for u in utts:
            by_spk.setdefault(u["speaker_id"], []).append(u)
        for t in totals:
            name = (t["speaker_label"] or "未命名").strip()
            if label and name != label:
                continue
            item = names.setdefault(name, {
                "label": name, "sessions": 0, "seconds": 0.0, "utterances": 0,
                "chars": 0, "questions": 0, "risk": 0, "fillers": 0, "session_ids": [],
            })
            us = by_spk.get(t["speaker_id"], [])
            chars = sum(_chars(u["text"]) for u in us)
            qs = sum(1 for u in us if Q_RE.search(u["text"] or ""))
            risk = sum(1 for u in us if RISK_RE.search(u["text"] or ""))
            fill = sum(1 for u in us if FILLER_RE.search(u["text"] or ""))
            secs = float(t["seconds"] or 0)
            item["sessions"] += 1
            item["seconds"] += secs
            item["utterances"] += int(t["n"] or 0)
            item["chars"] += chars
            item["questions"] += qs
            item["risk"] += risk
            item["fillers"] += fill
            item["session_ids"].append(sid)
            per_session.setdefault(name, []).append({
                "session_id": sid, "title": s["title"], "started_at": s["started_at"],
                "seconds": round(secs, 1), "chars": chars,
                "speed": round(chars / (secs / 60), 1) if secs > 0 else 0,
                "questions": qs,
            })

    out = []
    for name, it in names.items():
        secs = it["seconds"] or 1.0
        out.append({
            "label": name, "sessions": it["sessions"],
            "seconds": round(secs, 1),
            "utterances": it["utterances"], "chars": it["chars"],
            "speed": round(it["chars"] / (secs / 60), 1),
            "questions": it["questions"],
            "question_per_min": round(it["questions"] / (secs / 60), 2),
            "risk": it["risk"], "fillers": it["fillers"],
            "filler_rate": round(it["fillers"] / max(1, it["utterances"]) * 100, 1),
            "session_ids": it["session_ids"],
            "recent": sorted(per_session.get(name, []),
                             key=lambda x: x["started_at"] or "", reverse=True)[:10],
        })
    out.sort(key=lambda x: x["seconds"], reverse=True)
    return {
        "labels": out,
        "scanned_sessions": len(rows),
        "caveat": "按「说话人标签」对齐，不是声纹对齐：同一场里改过名的会被当成两个人，"
                  "不同场里同名也不保证是同一个人。",
    }

