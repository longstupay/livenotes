# -*- coding: utf-8 -*-
"""把网页上的 👍/👎 变成推送预算的先验：你标过"没用"的那类，少打断你。

为什么需要：高光打分回答的是"这段内容像不像值得听"，不是"这个人觉得值
不值得听"。前者靠特征就够了，后者只能靠反馈。没有闭环的话，用户每标一次
👎，下一次同样的句式还会照样响——久了他会把通知关掉，而不是继续标。

规则刻意做得极简、可解释、可回退：
  - 只看有 feedback（±1）的高光，按特征标签聚合，不训练模型；
  - 一个标签至少 3 个样本才允许影响决策，避免一票否决；
  - 好评率 <= 1/3 → 推送门槛 +0.4；>= 2/3 → 门槛 -0.2（更愿意推）；
  - 一条事件的偏置最多 ±0.6，防止某个标签绑架全部判断。

偏置表现为"分数门槛"而不是"直接拉黑"：标过没用的那类不是永远不推，
而是要在本场里更突出（更偏离基线）才值得打断。这样热点仍然漏不掉。
"""
from __future__ import annotations

import json
import sqlite3

# 标签 -> 人话。顺序即展示顺序（先讲信息，再讲情绪）。
TAGS = {
    "num": "提到数字/金额",
    "concl": "结论句",
    "qa": "接住提问",
    "chars": "长段陈述",
    "rate": "语速陡增",
    "interrupt": "抢麦",
    "burst": "短句连发",
}

MIN_SAMPLES = 3
CAP = 0.6


def tags_of(features: dict | None = None, why: str = "") -> set[str]:
    """一句话/一条事件的标签。优先用结构化特征，老数据退回解析 why 文案。

    特征来自 highlight 的逐句打分（features_json），所以"标过没用的那类"
    能被下一场复用；why 只是兜底，不要把中文文案当协议。
    """
    out: set[str] = set()
    f = features or {}
    try:
        if float(f.get("num") or 0) >= 1:
            out.add("num")
    except (TypeError, ValueError):
        pass
    if f.get("concl"):
        out.add("concl")
    if f.get("qa"):
        out.add("qa")
    if f.get("interrupt"):
        out.add("interrupt")
    try:
        if float(f.get("burst") or 0) >= 3:
            out.add("burst")
        if float(f.get("chars") or 0) >= 60:
            out.add("chars")
        rate, med = float(f.get("rate") or 0), float(f.get("med_rate") or 0)
        if rate and med > 0 and rate >= 1.5 * med:
            out.add("rate")
    except (TypeError, ValueError):
        pass
    # why 是补充而不是替代：老数据的 features_json 可能缺键，而人话里
    # 仍然写着"结论句/抢麦"。两边都认，取并集。
    if why:
        for tag, needle in (("num", "提到"), ("concl", "结论句"), ("qa", "接住"),
                            ("chars", "字长段"), ("rate", "语速"),
                            ("interrupt", "抢麦"), ("burst", "连着")):
            if needle in why:
                out.add(tag)
    return out


def _delta(good: int, bad: int) -> float:
    n = good + bad
    if n < MIN_SAMPLES:
        return 0.0
    p = good / n
    if p <= 0.34:
        base = -0.4
    elif p >= 0.67:
        base = 0.2
    else:
        return 0.0
    # 样本越多越敢调，6 条封顶；3 条时只给一半力度。
    return round(base * min(1.0, n / 6.0), 2)


def _verdicts(conn: sqlite3.Connection) -> list[tuple[set[str], int]]:
    """所有人工反馈，按"一次判断"聚成 (标签集合, ±1)。

    两个来源：高光榜的网页打标、卡片上的 👍/👎。同一段（session + peak）两边都标过时
    以卡片为准——那是一次更即时的判断，而且就发生在推送当下。
    """
    out: list[tuple[set[str], int]] = []
    seen: set[tuple[str, float]] = set()
    for r in conn.execute(
            "SELECT session_id,item,feedback,features_json FROM push_log "
            "WHERE feedback<>0 AND item LIKE 'hl:%'"):
        try:
            peak = round(float(str(r["item"])[3:]), 2)
        except (TypeError, ValueError):
            continue
        try:
            feats = json.loads(r["features_json"] or "{}")
        except (TypeError, ValueError):
            feats = {}
        tags = tags_of(feats, "")
        if tags:
            out.append((tags, 1 if int(r["feedback"]) > 0 else -1))
        seen.add((str(r["session_id"]), peak))
    for r in conn.execute(
            "SELECT session_id,peak_t,feedback,features_json,why FROM highlights "
            "WHERE feedback<>0"):
        try:
            peak = round(float(r["peak_t"] or 0), 2)
        except (TypeError, ValueError):
            continue
        if (str(r["session_id"]), peak) in seen:
            continue
        try:
            feats = json.loads(r["features_json"] or "{}")
        except (TypeError, ValueError):
            feats = {}
        out.append((tags_of(feats, r["why"] or ""), 1 if int(r["feedback"]) > 0 else -1))
    return out


def details(conn: sqlite3.Connection) -> list[dict]:
    """每个标签的 👍/👎 与偏置，按"最该少推"排前面。给页面和排查用。"""
    agg: dict[str, list[int]] = {t: [0, 0] for t in TAGS}
    for tags, verdict in _verdicts(conn):
        for t in tags:
            if t in agg:
                agg[t][0 if verdict > 0 else 1] += 1
    out = []
    for tag, (good, bad) in agg.items():
        d = _delta(good, bad)
        if good + bad == 0:
            continue
        out.append({"tag": tag, "label": TAGS[tag], "good": good, "bad": bad,
                    "delta": d})
    out.sort(key=lambda x: (x["delta"], x["tag"]))
    return out


def deltas(conn: sqlite3.Connection) -> dict[str, float]:
    """场次开始时取一次：{标签: 门槛偏置}。之后反馈变了也不中途改口。"""
    return {x["tag"]: x["delta"] for x in details(conn) if x["delta"]}


def bias(features: dict | None, why: str, table: dict[str, float] | None) -> float:
    """一条事件的合计偏置，正数更容易推、负数更难。"""
    if not table:
        return 0.0
    total = sum(table.get(t, 0.0) for t in tags_of(features, why))
    return round(max(-CAP, min(CAP, total)), 2)
