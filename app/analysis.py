# -*- coding: utf-8 -*-
"""低价值识别、四维总结、以及「总结必须能点回原文」的证据闸门。

设计红线：任何一条总结要点，如果没有绑定到真实存在的 utterance id，
就直接丢弃，不允许模型凭空生成一句看起来像结论的话。
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import llm, scheduler, settings, store, titles
from .config import LLM_SUMMARY_MAX_ROWS, SUMMARY_BUCKET_SECONDS
from .models import CATEGORY_META, category_label, fmt_ts

# ---------------------------------------------------------------- 分类 / 降权

AD_HINTS = ["加微信", "加个微信", "私信", "小黄车", "下单", "优惠券", "福利", "扣1", "扣个1",
            "报价", "代做", "代购", "报名", "扫码", "vx", "威信", "链接", "带货", "团购",
            "立减", "试听", "课程", "关注一下", "点个关注", "走平台"]
HOST_HINTS = ["上麦", "下一位", "麦序", "有请", "连一下", "把麦", "欢迎来到", "咱们今天",
              "扣个", "扣 1", "稍等啊", "没点关注", "开麦", "下麦", "家人们", "排一下队"]
QUESTION_HINTS = ["请问", "想问", "问一下", "怎么办", "能不能", "有没有", "该不该", "要不要",
                  "求助", "咨询", "适合吗", "怎么", "吗？", "吗?", "能连麦", "老师我"]
CHITCHAT_HINTS = ["哈哈", "在吗", "吃了吗", "晚上好", "早上好", "无聊", "点歌", "唱首",
                  "么么哒", "来啦", "打卡", "水一下", "困了", "好冷清", "有人吗", "饿死",
                  "声音好听", "默默", "路过", "嘿嘿"]
CONCLUSION_HINTS = ["建议", "因为", "所以", "结论", "重点", "第一", "第二", "注意", "别",
                    "先", "一定要", "关键", "数据", "复盘", "风险", "其实"]

TOPIC_RULES = [
    ("职场跳槽与面试", ["跳槽", "离职", "offer", "Offer", "面试", "简历", "入职", "试用期", "汇报线", "外包"]),
    ("薪资与谈判",     ["薪资", "涨薪", "工资", "谈薪", "期望薪资", "预算区间", "劝退", "赔偿"]),
    ("情感与关系",     ["分手", "对象", "恋爱", "相亲", "结婚", "冷暴力", "相处", "关系", "异地"]),
    ("副业与变现",     ["副业", "变现", "接单", "自媒体", "选题", "完播", "账号", "限流", "违规", "抽成"]),
    ("租房与生活",     ["租房", "房租", "房东", "押金", "合同", "搬家", "通勤"]),
]

FILLER_RE = re.compile(r"^(嗯|啊|哦|对|就是|然后|那个|吧|哈+)[\s，。、！~]*$")

# 改提示词/改编码格式/改送模型的句子范围就 +1，否则旧总结会被指纹误判为"还能用"。
# v3：长直播改为按时间分层抽样（不再只取前 350 句），旧缓存必须作废。
SUMMARY_PROMPT_VERSION = "v3"


def _pack_rows(rows: list[dict], char_limit: int = 200,
               with_topic: bool = True, with_cat: bool = True) -> dict:
    """把逐句压成"表 + 位置数组"，省掉每行重复的 JSON 键名。

    实测一场 321 句：原来的对象数组序列化 38,554 字符，其中原文只占 6,071；
    换成下表 + 数组后 13,000 字符出头（-66%），而模型要读的信息一条没少。

    u 的每一项：[id, 起始秒, 说话人下标, (类别下标), (主题下标), 原文]

    with_cat=False 用于「理解段」：那一步发生在标注之前，逐句类别还是关键词规则给的，
    喂给模型既占 token 又误导它，不如只给说话人和原文。
    """
    spk: list[str] = []
    cat: list[str] = []
    top: list[str] = []
    spk_i: dict[str, int] = {}
    cat_i: dict[str, int] = {}
    top_i: dict[str, int] = {}
    u: list[list] = []
    for r in rows:
        s = str(r.get("speaker_label") or "")
        c = str(r.get("category") or "")
        t = str(r.get("topic") or "") if with_topic else ""
        if s not in spk_i:
            spk_i[s] = len(spk); spk.append(s)
        if with_cat and c not in cat_i:
            cat_i[c] = len(cat); cat.append(c)
        if with_topic and t not in top_i:
            top_i[t] = len(top); top.append(t)
        item = [r["id"], round(float(r.get("t_start") or 0.0), 1), spk_i[s]]
        if with_cat:
            item.append(cat_i[c])
        if with_topic:
            item.append(top_i[t])
        item.append(str(r.get("text") or "")[:char_limit])
        u.append(item)
    out: dict[str, Any] = {"spk": spk}
    if with_cat:
        out["cat"] = cat
    if with_topic:
        out["top"] = top
    out["u"] = u
    return out


def _even_pick(items: list[dict], n: int) -> list[dict]:
    if n <= 0 or not items:
        return []
    if n >= len(items):
        return list(items)
    step = len(items) / float(n)
    return [items[min(len(items) - 1, int(i * step))] for i in range(n)]


def sample_rows(rows: list[dict], max_rows: int) -> list[dict]:
    """长直播分层抽样：按 10 分钟分桶均匀取，再保留最后 30 句。

    为什么不是 rows[:max_rows]：那样只覆盖开头，长直播后半场根本没进模型，
    总结自然"只总结了前半场"。按时间分桶能保证首尾都在，调用次数却不变。
    """
    max_rows = max(30, int(max_rows))
    if len(rows) <= max_rows:
        return list(rows)
    latest = rows[-30:]
    pool = rows[:-30]
    remaining = max(0, max_rows - len(latest))
    buckets: dict[int, list[dict]] = {}
    for r in pool:
        buckets.setdefault(int(float(r.get("t_start") or 0) // 600), []).append(r)
    per = max(1, remaining // max(1, len(buckets)))
    picked: list[dict] = []
    for b in buckets.values():
        picked.extend(_even_pick(b, min(per, len(b))))
    picked = list({int(r["id"]): r for r in picked}.values())
    if len(picked) < remaining:
        chosen = {int(r["id"]) for r in picked}
        rest = [r for r in pool if int(r["id"]) not in chosen]
        picked.extend(_even_pick(rest, remaining - len(picked)))
    out = list({int(r["id"]): r for r in picked + latest}.values())
    return sorted(out, key=lambda r: (float(r.get("t_start") or 0), int(r["id"])))


def classify(text: str, overlap: bool = False, asr_conf: float = 1.0) -> dict[str, Any]:
    """返回分类、价值档、重要度、话题、降权原因。规则优先，可替换为 LLM 分类器。"""
    t = (text or "").strip()
    if overlap or asr_conf < 0.45 or not t:
        return dict(category="chitchat", value_tag="low", importance=0.0,
                    topic="", low_value_reason="多人重叠 / ASR 置信度过低，不纳入总结")
    hits = lambda words: [w for w in words if w in t]  # noqa: E731
    ad, host, q, chat = hits(AD_HINTS), hits(HOST_HINTS), hits(QUESTION_HINTS), hits(CHITCHAT_HINTS)
    if ad:
        cat, reason = "ad", "广告/引流关键词：" + "、".join(ad[:3])
    elif host and len(t) <= 40:
        cat, reason = "host_control", "主持控场话术，无实质信息"
    elif FILLER_RE.match(t) or (len(t) <= 6 and not q):
        cat, reason = "chitchat", "无信息量短句/语气词"
    elif chat and len(t) <= 24 and not q:
        cat, reason = "chitchat", "寒暄闲聊：" + "、".join(chat[:2])
    elif q and len(t) <= 60:
        cat, reason = "question_consult", "提问类发言，作为话题锚点保留、总结时降权"
    else:
        cat, reason = "substantive", ""

    value_tag = CATEGORY_META[cat][2]
    base = {"high": 0.72, "normal": 0.45, "low": 0.12}[value_tag]
    importance = base + min(0.18, len(t) / 400) + 0.10 * bool(hits(CONCLUSION_HINTS))
    if cat == "ad":
        importance = min(importance, 0.18)
    if cat == "chitchat":
        importance = min(importance, 0.15)

    topic = ""
    best = 0
    for name, kws in TOPIC_RULES:
        n = sum(1 for k in kws if k in t)
        if n > best:
            best, topic = n, name
    return dict(category=cat, value_tag=value_tag,
                importance=round(min(1.0, importance), 3), topic=topic or "综合/未归类",
                low_value_reason=reason)


# ---------------------------------------------------------------- 作用域

@dataclass
class Scope:
    session: dict
    filters: dict
    mode: str
    all_rows: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)


def session_digest(conn: sqlite3.Connection, sid: str) -> str:
    """本场内容的稳定摘要，用来判断「同一份稿子」是否变过。

    取所有会进模型输入的字段：说话人 id/显示名、分类、价值档、重叠标记、
    置信度、话题、原文。改名、改分类、改话题、文本变化都会让摘要变，
    重复点击才会重新生成；单纯刷新页面不会。
    """
    h = hashlib.sha1()
    h.update(("v2|" + str(sid or "") + "|").encode("utf-8"))
    for r in conn.execute(
            "SELECT id, speaker_id, speaker_label, category, value_tag, overlap, "
            "asr_conf, topic, text FROM utterances WHERE session_id=? ORDER BY id",
            (sid,)):
        h.update(("%s|%s|%s|%s|%s|%s|%s|%s|%s;" % tuple(r)).encode("utf-8"))
    return h.hexdigest()[:20]


def _scope_sid(scope: "Scope") -> str:
    return str(scope.session.get("id") or scope.filters.get("session_id") or "")


def _wall(session: dict, t: float) -> str:
    try:
        dt = datetime.fromisoformat(session["started_at"]) + timedelta(seconds=t)
        return dt.strftime("%H:%M:%S")
    except (ValueError, KeyError, TypeError):
        return fmt_ts(t)


def build_scope(conn: sqlite3.Connection, sid: str, filters: dict, mode: str) -> Scope:
    session = store.get_session(conn, sid) or {}
    base = dict(filters)
    base.update(session_id=sid, exclude_low_value=False, exclude_overlap=False, min_conf=None)
    all_rows = store.query_utterances(conn, base, limit=20000)

    final = dict(filters)
    final.update(session_id=sid,
                 exclude_low_value=bool(filters.get("exclude_low_value", True)),
                 exclude_overlap=True, min_conf=0.45)
    rows = store.query_utterances(conn, final, limit=20000)

    by_cat: dict[str, int] = {}
    overlap_s = 0.0
    low_conf = 0
    for r in all_rows:
        if r["overlap"]:
            overlap_s += max(0.0, r["t_end"] - r["t_start"])
        elif r["asr_conf"] < 0.45:
            low_conf += 1
        elif r["value_tag"] == "low" and final["exclude_low_value"]:
            by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1

    untranscribed = float(session.get("untranscribed_seconds") or 0)
    coverage = {
        "matched": len(all_rows),
        "included": len(rows),
        "excluded_low_value": by_cat,
        "excluded_low_value_total": sum(by_cat.values()),
        "excluded_overlap_s": round(overlap_s, 1),
        "excluded_low_conf": low_conf,
        "untranscribed_s_whole_session": untranscribed,
        "coverage_ratio": round(len(rows) / len(all_rows), 3) if all_rows else 0.0,
    }

    caveats: list[str] = []
    if session.get("source_mode") == "mixed":
        caveats.append(
            "本场为混音兜底：说话人是聚类标签而非实名，重叠语音已剔除；"
            "总结只覆盖可转写片段，不代表全场完整内容。")
    if session.get("source_mode") == "demo":
        caveats.append("当前为演示数据，仅用于验证检索/总结/回溯链路，不是真实采集结果。")
    if overlap_s > 0:
        caveats.append(f"当前范围内有 {overlap_s:.0f} 秒重叠语音未转写，相关发言不参与结论。")
    if untranscribed > 0:
        caveats.append(f"整场另有约 {untranscribed / 60:.0f} 分钟无人声/未转写区间（冷场、BGM、丢帧）。")
    if all_rows and len(rows) / len(all_rows) < 0.5:
        caveats.append("当前筛选剔除了过半发言，结论可能偏向高价值片段，请结合原文复核。")
    if not rows:
        caveats.append("当前条件下没有可用发言，无法生成有证据支撑的总结。")

    w = session.get("warnings")
    if isinstance(w, str):
        try:
            w = json.loads(w)
        except json.JSONDecodeError:
            w = []
    for item in w or []:
        caveats.append("⚠ " + str(item))

    return Scope(session=session, filters=final, mode=mode,
                 all_rows=all_rows, rows=rows, coverage=coverage, caveats=caveats)


# ---------------------------------------------------------------- 总结

def _groups(rows: list[dict], mode: str) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = {}
    for r in rows:
        if mode == "time":
            k = f"T{int(r['t_start'] // SUMMARY_BUCKET_SECONDS):04d}"
        elif mode == "category":
            k = r["category"]
        else:  # content | filtered
            k = r.get("topic") or "综合/未归类"
        g.setdefault(k, []).append(r)
    return g


def _group_title(key: str, rows: list[dict], mode: str, scope: Scope) -> str:
    if mode == "time":
        b = int(key[1:]) * SUMMARY_BUCKET_SECONDS
        return f"{fmt_ts(b)} – {fmt_ts(b + SUMMARY_BUCKET_SECONDS)} 时段要点"
    if mode == "category":
        return f"【{category_label(key)}】相关发言"
    return f"话题：{key}"


def _evidence(row: dict, scope: Scope) -> dict:
    return {
        "id": row["id"],
        "t_start": row["t_start"], "t_end": row["t_end"],
        "timecode": fmt_ts(row["t_start"]),
        "wall_time": _wall(scope.session, row["t_start"]),
        "speaker_id": row["speaker_id"], "speaker_label": row["speaker_label"],
        "category": row["category"], "topic": row["topic"],
        "quote": row["text"][:90],
        "audio_url": f"/api/audio?session_id={scope.session.get('id','')}"
                     f"&t_start={row['t_start']:.1f}&t_end={row['t_end']:.1f}",
    }


_TITLE_RULES = """
另外顺手给这场直播起个名字（不额外调用模型，就在这次输出里）：
- 6~16 个汉字，名词短语或判断句；不要疑问句、不要问号；
- 不写日期、时间、人数（系统已有，写了就重复）；
- 不用「本期/这期/今天/这场/直播/分享/干货/复盘」开头；
- 不加引号、书名号、句末标点；
- 必须能和别的场次区分：写清「谁 × 什么主题 × 什么场景」，不要只写主题；
- title_alts 给 2 个角度不同的备选（主题+对象 / 结论 / 场景冲突）；
- 全是闲聊、广告、控场、没有可命名内容时，title 写 null。"""


_LLM_GROUP_PROMPT = """你在整理一份多人语音直播的可检索纪要，按类别分组。
下面的发言已经按类别归好。请为每个类别分别提炼要点。

输入格式（为了省 token 用了紧凑编码，字段含义如下）：
- cat: 类别 key 表，输出里的 category 直接写这里的字符串；
- u: 逐句数组，每项 = [id, 类别下标, 原文]，类别下标指向 cat 表。

类别含义：
- substantive 实质讨论（观点、经验、方案、业务细节）
- question_consult 上麦咨询（提问、求助、咨询）
- host_control 主持控场（点名、欢迎、排麦、过渡）
- chitchat 闲聊（寒暄、附和）
- ad 广告引流（导流、报价、加微信）

输出严格 JSON（不要 markdown 代码块）：
{"title":"这场直播的名字","title_alts":["备选名1","备选名2"],"overview":"一段话概述，不超过120字","groups":[{"category":"substantive","points":[{"title":"不超过20字","gist":"不超过100字","evidence_ids":[整数id,整数id]}]}]}

硬性规则：
1. 每个类别最多 2 个 point；没有信息量的类别可以整组不出现。
2. evidence_ids 必须来自**该类别下**的发言，每个 point 至少 2 个。
3. gist 必须由这些原文直接支持，禁止编造因果、数字或结论。
""" + _TITLE_RULES


def _llm_groups(scope: Scope, usage: dict | None = None,
                errors: list | None = None) -> tuple[str, list[dict], Any, list] | None:
    """按类别分组的 LLM 要点：每个类别单独出点，证据必须落在该类别的发言里。"""
    cfg = settings.resolve()
    if not cfg["configured"]:
        return None
    by_cat: dict[str, list[dict]] = {}
    for r in sample_rows(scope.rows, LLM_SUMMARY_MAX_ROWS):
        by_cat.setdefault(r["category"], []).append(r)
    cats = [c for c in by_cat if len(by_cat[c]) >= 2]
    if not cats:
        return None
    by_id = {r["id"]: r for c in cats for r in by_cat[c]}
    cat_of = {r["id"]: c for c in cats for r in by_cat[c]}
    cat_i = {c: i for i, c in enumerate(cats)}
    try:
        content = llm.chat([
            {"role": "system", "content": _LLM_GROUP_PROMPT},
            {"role": "user", "content": json.dumps({
                "cat": cats,
                "u": [[r["id"], cat_i[r["category"]], r["text"][:200]]
                      for c in cats for r in by_cat[c][:120]],
            }, ensure_ascii=False)},
        ], temperature=0.2, usage=usage,
            session_id=_scope_sid(scope), task="summary")
        content = re.sub(r"^(" + chr(96) * 3 + r")(?:json)?|(" + chr(96) * 3 + r")$", "",
                         content.strip(), flags=re.M).strip()
        data = json.loads(content)
    except Exception as e:
        if errors is not None:
            errors.append(e)
        return None

    groups: list[dict] = []
    for g in data.get("groups", [])[:6]:
        cat = str(g.get("category") or "")
        if cat not in by_cat:
            continue
        pts: list[dict] = []
        for p in (g.get("points") or [])[:2]:
            ids = [int(i) for i in p.get("evidence_ids", [])
                   if str(i).isdigit() and int(i) in by_id and cat_of.get(int(i)) == cat]
            ev = [_evidence(by_id[i], scope) for i in ids]
            if len(ev) < 2:      # 证据闸门：必须落在同一类别，且至少 2 条
                continue
            pts.append({
                "title": str(p.get("title", "未命名"))[:40],
                "gist": str(p.get("gist", ""))[:300],
                "t_from": min(e["t_start"] for e in ev), "t_to": max(e["t_end"] for e in ev),
                "importance": 0.0, "utterance_count": len(ev),
                "speakers": sorted({e["speaker_label"] for e in ev}), "evidence": ev,
            })
        if pts:
            groups.append({"category": cat, "label": category_label(cat),
                           "count": len(by_cat[cat]), "points": pts})
    if not groups:
        return None
    return (str(data.get("overview", ""))[:400], groups,
            data.get("title"), data.get("title_alts") or [])


def _extractive_groups(scope: Scope) -> list[dict]:
    """不调模型的分组：每个类别取信息量最高的原句，最多 2 条要点。"""
    out: list[dict] = []
    for cat, rs in sorted(_groups(scope.rows, "category").items(), key=lambda kv: -len(kv[1])):
        ranked = sorted(rs, key=lambda r: (r["importance"], len(r["text"])), reverse=True)
        pts: list[dict] = []
        for i in range(0, min(len(ranked), 6), 3):
            chunk = ranked[i:i + 3]
            if not chunk:
                continue
            ev = [_evidence(r, scope) for r in chunk]
            gist = "；".join(r["text"].strip()[:80] for r in ranked[i:i + 2] if r["text"].strip())
            pts.append({
                "title": _group_title(cat, chunk, "category", scope) + ("" if i == 0 else "（续）"),
                "gist": gist or "（该类别没有明显高价值原句）",
                "t_from": min(r["t_start"] for r in chunk), "t_to": max(r["t_end"] for r in chunk),
                "importance": round(sum(r["importance"] for r in chunk) / len(chunk), 3),
                "utterance_count": len(chunk),
                "speakers": sorted({r["speaker_label"] for r in chunk}), "evidence": ev,
            })
        if pts:
            out.append({"category": cat, "label": category_label(cat), "count": len(rs),
                        "points": pts[:2]})
    return out


def _label_groups(conn: sqlite3.Connection, sid: str, scope: Scope) -> list[dict]:
    """「按分类」视图的本地版（0 次模型调用）：分组直接来自标签，标题带主导主题。

    为什么不再调模型：类别是标签给的，证据是原句，这两样都已经在库里；
    再让模型把同样的东西复述一遍，唯一多出来的是文案，代价是一次完整调用。
    要文案时用户可以显式选「强制用大模型」，那时才走 `_llm_groups`。
    """
    out: list[dict] = []
    by_cat = _groups(scope.rows, "category")
    for cat, rs in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        if cat not in CATEGORY_META:
            continue
        tally: dict[str, int] = {}
        for r in rs:
            t = (r.get("topic") or "").strip()
            if t:
                tally[t] = tally.get(t, 0) + 1
        tops = [t for t, _ in sorted(tally.items(), key=lambda kv: -kv[1])[:3]]
        head = category_label(cat) + (" · " + tops[0] if tops else "")
        ranked = sorted(rs, key=lambda r: (r["importance"], len(r["text"])), reverse=True)
        pts: list[dict] = []
        for i in range(0, min(len(ranked), 6), 3):
            chunk = ranked[i:i + 3]
            if not chunk:
                continue
            ev = [_evidence(r, scope) for r in chunk]
            gist = "；".join(r["text"].strip()[:80] for r in chunk[:2] if r["text"].strip())
            pts.append({
                "title": head + ("" if i == 0 else "（续）"),
                "gist": gist or "（该类别没有明显高价值原句）",
                "t_from": min(r["t_start"] for r in chunk), "t_to": max(r["t_end"] for r in chunk),
                "importance": round(sum(r["importance"] for r in chunk) / len(chunk), 3),
                "utterance_count": len(chunk),
                "speakers": sorted({r["speaker_label"] for r in chunk}), "evidence": ev,
            })
        if pts:
            out.append({"category": cat, "label": category_label(cat), "count": len(rs),
                        "topics": tops, "points": pts[:2]})
    return out
def _extractive_points(scope: Scope) -> list[dict]:
    points: list[dict] = []
    for key, rs in _groups(scope.rows, scope.mode).items():
        if not rs:
            continue
        ranked = sorted(rs, key=lambda r: (r["importance"], len(r["text"])), reverse=True)
        ev = [_evidence(r, scope) for r in ranked[:3]]
        gist_parts = [r["text"].strip() for r in ranked[:2] if r["text"].strip()]
        gist = "；".join(p[:80] for p in gist_parts)
        points.append({
            "title": _group_title(key, rs, scope.mode, scope),
            "gist": gist or "（该组无明显高价值原句）",
            "t_from": min(r["t_start"] for r in rs),
            "t_to": max(r["t_end"] for r in rs),
            "importance": round(sum(r["importance"] for r in rs) / len(rs), 3),
            "utterance_count": len(rs),
            "speakers": sorted({r["speaker_label"] for r in rs}),
            "evidence": ev,
        })
    return sorted(points, key=lambda p: (p["utterance_count"], p["importance"]), reverse=True)[:14]


_LLM_PROMPT = """你在整理一份多人语音直播的可检索纪要。只允许使用下面给出的原始发言，禁止补充外部知识、禁止推测。
这一步是「理解整场」，同时还要定下这场直播的主题词表，后面给每句话打标签只能从这套词里选。

输入格式（为了省 token 用了紧凑编码，字段含义如下）：
- spk: 说话人表；u: 逐句数组，每项 = [id, 起始秒, 说话人下标, 原文]。
  id 是稳定标识，evidence_ids 里必须用第一列的 id，不要用行号。

输出严格 JSON（不要 markdown 代码块）：
{"title":"这场直播的名字","title_alts":["备选名1","备选名2"],"overview":"一段话概述，不超过120字",
 "topics":["主题1","主题2","主题3"],
 "host_speaker":spk 表里的下标,
 "points":[{"title":"话题名，不超过20字","gist":"讨论要点，不超过100字","evidence_ids":[整数id数组，2~4个]}]}

topics 规则：
- 6~10 个，名词短语、每个不超过 6 个汉字（如「设备预算」「账号风控」「引流话术」）；
- 互相不重叠，覆盖尽量多的内容；按重要性从高到低；
- 不要「其他」「杂项」这类兜底主题；句子里找不到依据就不要硬编。
- 后面会拿这套词表给每一句归类，所以每个主题必须能落在一批具体句子上。

硬性规则：
1. evidence_ids 中的 id 必须来自给定发言；每个 point 至少 2 个 id。
2. gist 必须能由这些 id 对应的原文直接支持，不得编造因果或数字。
3. 只保留有信息量的话题；闲聊、广告、控场话术不单独成点。
4. 最多 8 个 point。""" + _TITLE_RULES


# 低于这个规模就不值得烧 token：短录音直接用本地抽取式
LLM_MIN_ROWS = 15
LLM_MIN_CHARS = 400


def _worth_llm(rows: list[dict]) -> tuple[bool, str]:
    n = len(rows)
    chars = sum(len(r.get("text") or "") for r in rows)
    if n < LLM_MIN_ROWS or chars < LLM_MIN_CHARS:
        return False, "内容太少（%d 条 / %d 字），本次用本地抽取式，不消耗 token" % (n, chars)
    return True, ""


def _fallback_reason(errors: list, fallback: str = "本地抽取式") -> str:
    """把「为什么没用上模型」说清楚：预算、限流、格式错误要能区分。"""
    if not errors:
        return "大模型调用失败，已回退%s" % fallback
    e = errors[-1]
    if isinstance(e, scheduler.BudgetExceeded):
        return "%s，已回退%s" % (e, fallback)
    text = str(e).strip()
    return "大模型调用失败（%s），已回退%s" % (
        text[:120] if text else type(e).__name__, fallback)


def _clean_topics(raw: Any, limit: int = 10) -> list[str]:
    out: list[str] = []
    for t in (raw or []):
        if isinstance(t, dict):
            t = t.get("name") or t.get("topic") or ""
        t = str(t).strip().strip("「」\"'")
        if not t or t in out:
            continue
        if len(t) > 12:
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _llm_points(scope: Scope, usage: dict | None = None,
                errors: list | None = None) -> tuple | None:
    """理解段（P1）：一次调用同时出「标题 / 概述 / 要点 / 主题词表 / 主讲人」。

    为什么合并到这通调用里：标题、概述、主题三件事读的是同一份内容，分开调就是
    同一段话让模型读三遍。主题顺带产出的另一个好处是——后面逐句打标签时不用再
    单独提一次主题，词表天然一致。
    """
    cfg = settings.resolve()
    if not cfg["configured"]:
        return None
    candidates = sample_rows(scope.rows, LLM_SUMMARY_MAX_ROWS)
    if not candidates:
        return None
    id_set = {r["id"] for r in candidates}
    try:
        # 这一步在标注之前跑，逐句类别还是关键词规则的，不喂给模型（省 token 也免误导）
        packed = _pack_rows(candidates, char_limit=200, with_topic=False, with_cat=False)
        packed["mode"] = scope.mode
        content = llm.chat([
            {"role": "system", "content": _LLM_PROMPT},
            {"role": "user", "content": json.dumps(packed, ensure_ascii=False)},
        ], temperature=0.2, usage=usage,
            session_id=_scope_sid(scope), task="summary")
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
        data = json.loads(content)
        by_id = {r["id"]: r for r in candidates}
        points: list[dict] = []
        for p in data.get("points", [])[:8]:
            ids = [int(i) for i in p.get("evidence_ids", []) if str(i).isdigit() and int(i) in id_set]
            ev = [_evidence(by_id[i], scope) for i in ids]
            # ---- 证据闸门：无有效证据的要点一律丢弃 ----
            if len(ev) < 2:
                continue
            points.append({
                "title": str(p.get("title", "未命名话题"))[:40],
                "gist": str(p.get("gist", ""))[:300],
                "t_from": min(e["t_start"] for e in ev),
                "t_to": max(e["t_end"] for e in ev),
                "importance": 0.0, "utterance_count": len(ev),
                "speakers": sorted({e["speaker_label"] for e in ev}),
                "evidence": ev,
            })
        if not points:
            return None
        topics = _clean_topics(data.get("topics"))
        host = ""
        idx = data.get("host_speaker")
        labels = packed.get("spk") or []
        if isinstance(idx, int) and 0 <= idx < len(labels):
            want = labels[idx]
            host = next((r["speaker_id"] for r in candidates
                         if r.get("speaker_label") == want), "")
        return (str(data.get("overview", ""))[:300], points,
                data.get("title"), data.get("title_alts") or [], topics, host)
    except Exception as e:                  # 网络/格式/限流都当"没有 LLM"，回退抽取式
        if errors is not None:
            errors.append(e)
        return None


def _norm_filters(f: dict) -> dict:
    """只留真正影响结论的筛选字段，并补上默认值。

    指纹里直接扔整个 filters 会出事：/api/summarize 会补 exclude_low_value/exclude_overlap，
    /api/summarize/estimate 不补，两边算出来的指纹就不一样，UI 说"能复用"、真跑却重新调模型。
    """
    out: dict[str, Any] = {}
    out["t_from"] = None if f.get("t_from") is None else round(float(f["t_from"]), 1)
    out["t_to"] = None if f.get("t_to") is None else round(float(f["t_to"]), 1)
    out["categories"] = sorted(str(x) for x in (f.get("categories") or []))
    out["speakers"] = sorted(str(x) for x in (f.get("speakers") or []))
    out["q"] = str(f.get("q") or "").strip()
    out["exclude_low_value"] = True if f.get("exclude_low_value") is None else bool(f["exclude_low_value"])
    out["exclude_overlap"] = True if f.get("exclude_overlap") is None else bool(f["exclude_overlap"])
    return out


def _fingerprint(rows: list[dict], mode: str, filters: dict, model: str) -> str:
    """总结输入指纹：原文 + 分类/主题 + 模型 + 提示词版本 + 筛选，任何一项变了就该重算。

    为什么不只看文本：分类改过一次，同样几句原文分到的组会不同，结论也该不同——
    这正是"分类白打了、总结还在吃旧标签"的根源，指纹必须把它算进去。
    """
    h = hashlib.sha1()
    h.update(("%s|%s|%s" % (SUMMARY_PROMPT_VERSION, mode, model)).encode("utf-8", "ignore"))
    h.update(json.dumps(_norm_filters(filters), sort_keys=True, ensure_ascii=False)
             .encode("utf-8", "ignore"))
    for r in rows:
        h.update(("%d|%.2f|%s|%s|%s|%s" % (
            int(r["id"]), float(r.get("t_start") or 0.0), r.get("speaker_label") or "",
            r.get("category") or "", r.get("topic") or "", r.get("text") or "",
        )).encode("utf-8", "ignore"))
    return h.hexdigest()[:20]


def estimate(conn: sqlite3.Connection, sid: str, filters: dict | None = None,
             mode: str = "content") -> dict:
    """生成前先问一句：这次会不会烧 token、大概多少。UI 用它决定提示文案。"""
    f = dict(filters or {})
    f.setdefault("session_id", sid)
    scope = build_scope(conn, sid, f, mode)
    rows = scope.rows
    ok, why = _worth_llm(rows)
    cfg = settings.resolve()
    if not cfg["configured"]:
        ok, why = False, "还没配置大模型（⚙ 设置）"
    budget = scheduler.budget_status(sid)
    if ok and budget["session"]["limit"] and budget["session"]["remaining"] <= 0:
        ok, why = False, ("本场 token 预算已用完（已用 %d / 上限 %d），本次用本地抽取式"
                          % (budget["session"]["used"], budget["session"]["limit"]))
    elif ok and budget["daily"]["limit"] and budget["daily"]["remaining"] <= 0:
        ok, why = False, ("今日全局 token 预算已用完（已用 %d / 上限 %d），本次用本地抽取式"
                          % (budget["daily"]["used"], budget["daily"]["limit"]))
    fp = _fingerprint(rows, mode, f, cfg["model"] or "") if ok else ""
    hit = store.summary_by_fingerprint(conn, sid, mode, fp) if fp else None
    if hit:
        ok, why = False, "输入没变，可以直接复用上次结果（0 token）"
    if mode == "category":
        # 按分类视图由标签在本地汇总，不调模型；要模型写文案得显式选「强制用大模型」
        ok, why = False, "分类视图由标签本地生成（0 token）；要模型写文案请选「强制用大模型」"
    return {
        "rows": len(rows),
        "chars": sum(len(r.get("text") or "") for r in rows),
        "would_use_llm": ok, "why": why, "model": cfg["model"], "configured": cfg["configured"],
        "cached": bool(hit), "fingerprint": fp,
        "thresholds": {"rows": LLM_MIN_ROWS, "chars": LLM_MIN_CHARS},
        "budget": budget,
    }


def make_summary(conn: sqlite3.Connection, sid: str, filters: dict,
                 mode: str = "content", engine_pref: str = "auto") -> dict:
    mode = mode if mode in ("content", "time", "category", "filtered") else "content"
    scope = build_scope(conn, sid, filters, mode)

    speakers = sorted({r["speaker_label"] for r in scope.rows})
    cats: dict[str, int] = {}
    for r in scope.rows:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    stats = {
        "included_utterances": len(scope.rows),
        "speakers": len(speakers), "speaker_labels": speakers,
        "categories": cats,
        "t_from": min((r["t_start"] for r in scope.rows), default=0.0),
        "t_to": max((r["t_end"] for r in scope.rows), default=0.0),
    }

    engine = "extractive"
    overview = ""
    points = _extractive_points(scope)

    # ---- 指纹与缓存：输入没变就直接返回上次结果，一次模型都不调 ----
    # 只有"本来会调模型"的情况才值得查缓存；抽取式本来就不花钱，重算也无所谓。
    fp = ""
    if engine_pref in ("auto", "llm") and _worth_llm(scope.rows)[0]:
        fp = _fingerprint(scope.rows, mode, filters, settings.resolve()["model"] or "")
        if engine_pref == "auto":
            hit = store.summary_by_fingerprint(conn, sid, mode, fp)
            if hit:
                hit = dict(hit)
                hit["cached"] = True
                hit["usage"] = None
                hit["fingerprint"] = fp
                # 缓存的是"当时的名字"，用户后来可能改过名；标题条要按当前库里的状态画，
                # 否则会出现"总结说叫 A、顶上说叫 B"。
                st = titles.state(conn, sid)
                hit["session_title"] = st["title"]
                hit["title_source"] = st["source"]
                hit["title_applied"] = bool(st["title"]) and st["title"] == hit.get("title")
                hit["engine_reason"] = ("输入没变（文本 / 分类 / 主题 / 模型 / 筛选都没动），"
                                        "直接复用上次结果，本次 0 token")
                return hit

    usage: dict = {}
    llm_errors: list = []
    reason = ""
    groups = None
    llm = None
    name_title: Any = None      # 模型顺手起的名字：与总结同一次调用，输入零增量、输出 +约 40 token
    name_alts: list = []
    topics: list[str] = []
    host_speaker: str = ""
    if mode == "category":
        # 按分类：真正的按类分组，而不是把五类混在一起。
        # 默认（auto）走本地汇总：标签已经在库里，分组、计数、证据都能直接算出来，
        # 没必要再调一次模型；只有用户明确选「强制用大模型」才写要点文案。
        if engine_pref == "llm":
            t0 = time.time()
            g = _llm_groups(scope, usage, llm_errors)
            usage["ms"] = int((time.time() - t0) * 1000)
            if g:
                overview, groups, name_title, name_alts = g
                engine = "llm(" + (settings.resolve()["model"] or "configured") + ")"
                reason = "按你的选择，用大模型写分类要点"
            else:
                reason = _fallback_reason(llm_errors, "本地汇总")
        if not groups:
            groups = _label_groups(conn, sid, scope)
            if engine_pref == "auto":
                reason = reason or "按 AI 标签本地汇总（0 token）"
            elif engine_pref == "extractive":
                reason = "你选了「只用本地抽取」"
        points = [p for g in groups for p in g["points"]]
        if not overview:
            n_ai = store.ai_state(conn, sid)["labeled"]
            overview = (f"本场共纳入 {len(scope.rows)} 条有效发言，按 {len(groups)} 个类别组织，"
                        f"共 {len(points)} 组要点；每个要点都绑定该类别下的原句。"
                        + ("标签来自 AI 标注。" if n_ai else "标签来自关键词规则。"))
    elif engine_pref in ("auto", "llm"):
        ok, why = (_worth_llm(scope.rows) if engine_pref == "auto" else (True, ""))
        if not ok:
            reason = why
        else:
            t0 = time.time()
            llm = _llm_points(scope, usage, llm_errors)
            usage["ms"] = int((time.time() - t0) * 1000)
            reason = ("内容足够，自动使用大模型" if llm
                      else _fallback_reason(llm_errors))
    if llm:
        overview, points, name_title, name_alts, topics, host_speaker = llm
        engine = "llm(" + (settings.resolve()["model"] or "configured") + ")"
    elif mode != "category" and engine_pref == "llm":
        engine = "extractive(LLM 不可用，已回退)"
    elif mode != "category" and engine_pref == "extractive":
        reason = "你选了「只用本地抽取」"

    # ---- 把 P1 产出的主题词表落库：后续逐句标注只能用这套词 ----
    # 只有"这句还没被标过"的场次才写：否则改词表会让所有已标句子的指纹失效，
    # 用户只是点了下总结，结果整场标签作废重标，这代价没人愿意付。
    whole_scope = (mode == "content"
                   and not str(filters.get("q") or "").strip()
                   and not filters.get("categories") and not filters.get("speakers")
                   and filters.get("t_from") is None and filters.get("t_to") is None)
    if topics and whole_scope and store.ai_state(conn, sid)["labeled"] == 0:
        store.set_ai_topics(conn, sid, topics)

    if not overview:
        if not scope.rows:
            overview = "当前条件下没有可用发言，无法生成总结。"
        else:
            overview = (f"本场共纳入 {len(scope.rows)} 条有效发言，涉及 {len(speakers)} 个说话人、"
                        f"{len(points)} 个要点分组；已剔除广告、闲聊、控场及重叠语音。"
                        "下列要点均由原文原句构成，点「查看原文」可回溯对应时间段。")
    caveats = list(scope.caveats)
    if engine.startswith("llm") and len(scope.rows) > LLM_SUMMARY_MAX_ROWS:
        caveats.append("内容较长，按时间分层抽样 %d/%d 句送模型；结论覆盖全段，细节可能省略。"
                       % (LLM_SUMMARY_MAX_ROWS, len(scope.rows)))
    if engine.startswith("extractive"):
        caveats.append("本地抽取式总结：要点为高权重原句组合，未做生成式改写；配置 LLM 环境变量后可切换。")

    # ---- 顺手起名：同一份内容只读一遍，名字与「能不能改」的决定都交给 titles ----
    engine_kind = "llm" if engine.startswith("llm") else "extractive"
    enough = _worth_llm(scope.rows)[0]
    whole = whole_scope
    if not name_title:
        name_title = titles.structural(conn, sid)     # 没调模型也能给个结构化的候选
        name_kind = "structural"
    else:
        name_kind = "llm"
    named = titles.apply_summary(conn, sid, title=name_title, alts=name_alts,
                                 engine_kind=engine_kind, whole=whole, enough=enough,
                                 title_kind=name_kind)

    payload = {
        "session_id": sid, "scope": mode, "engine": engine,
        "engine_kind": engine_kind,
        "engine_model": settings.resolve()["model"],
        "engine_reason": reason,
        # 有值说明「本来要调模型，但失败了/预算不够」，业务层据此不缓存降级结果
        "llm_error": (str(llm_errors[-1])[:200] if llm_errors else ""),
        "budget": scheduler.budget_status(sid),
        "usage": usage or None,
        "fingerprint": fp,
        "cached": False,
        "groups": groups if mode == "category" else None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "filter": filters, "overview": overview, "stats": stats,
        "coverage": scope.coverage, "caveats": caveats, "points": points,
        "topics": topics or store.get_ai_topics(conn, sid),
        "host_speaker": host_speaker,
        "title": named["title"], "title_alts": named["alts"],
        "title_applied": named["applied"], "title_source": named["source"],
        "title_reason": named["reason"], "session_title": named["current"],
    }
    # 只给真正走成了模型的结果盖指纹：模型失败退回抽取式时不能盖，
    # 否则下次命中缓存，会把"一次失败"永久固化成这场唯一的总结。
    saved_fp = fp if engine_kind == "llm" else ""
    payload["fingerprint"] = saved_fp
    store.save_summary(conn, sid, mode, filters, engine, payload, saved_fp)
    return payload


WHOLE_FILTERS = {"categories": [], "speakers": [], "q": "", "min_conf": None,
                 "exclude_low_value": True, "exclude_overlap": True}


def _summary_for_filters(conn: sqlite3.Connection, sid: str,
                         filters: dict) -> dict | None:
    """取一条"口径和这次一样"的总结，而不是"最新的一条"。

    为什么不能只取最新：用户可能先跑整场，又跑一次带筛选的（写在后面）。
    汇总段盲取最新，就会拿筛选后的要点去重算整场统计、并按整场口径盖章，
    之后整场总结命中缓存，返回的却是筛选内容——错得很难看出来。
    """
    want = _norm_filters(filters)
    for r in store.recent_summaries(conn, sid, "content", 20):
        try:
            got = _norm_filters(json.loads(r["filter_json"] or "{}"))
        except (ValueError, TypeError):
            continue
        if got != want:
            continue
        try:
            return {"id": r["id"], "payload": json.loads(r["payload_json"] or "{}")}
        except (ValueError, TypeError):
            return None
    return None


def finalize(conn: sqlite3.Connection, sid: str, filters: dict | None = None) -> dict:
    """P3 汇总（**0 次模型调用**）：标签写完之后，用最新数据重算统计与证据，并换新指纹。

    必须换指纹的原因：理解段跑在标注之前，它的指纹是按"当时的标签"算出来的。
    标完不更新，下一次点「生成总结」会认为结果过期再调一次模型——白烧一遍。
    换完之后，这一份总结就是"当前标签下的正确结果"，再点直接命中缓存。

    证据里的 category/topic 是快照，也一并按 id 重新取，否则会出现
    "左边筛选说这是广告，右边证据写着实质讨论"。
    """
    filters = dict(filters or WHOLE_FILTERS)
    scope = build_scope(conn, sid, filters, "content")
    row = _summary_for_filters(conn, sid, filters)
    payload = dict((row or {}).get("payload") or {})

    if not payload:
        # 理解段没跑成：本地抽一份，至少让面板有东西可看（不补调模型）
        payload = {
            "session_id": sid, "scope": "content",
            "engine": "extractive", "engine_kind": "extractive",
            "engine_model": settings.resolve()["model"],
            "engine_reason": "理解段没有产出结果，这里用本地抽取补齐（0 token）",
            "usage": None, "cached": False,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "filter": filters, "overview": "", "stats": {}, "coverage": {},
            "caveats": list(scope.caveats), "points": _extractive_points(scope),
            "topics": [], "host_speaker": "",
            "title": None, "title_alts": [], "title_applied": False,
            "title_source": "", "title_reason": "", "session_title": "",
        }

    # ---- 证据重取：标签变了，evidence 里的类别/主题快照要跟着变 ----
    pts = payload.get("points") or []
    ids: list[int] = []
    for p in pts:
        for e in (p.get("evidence") or []):
            try:
                ids.append(int(e["id"]))
            except (KeyError, TypeError, ValueError):
                pass
    rows = {r["id"]: r for r in store.get_evidence(conn, sid, ids)}
    for p in pts:
        keep = []
        for e in (p.get("evidence") or []):
            try:
                keep.append(int(e["id"]))
            except (KeyError, TypeError, ValueError):
                pass
        ev = [_evidence(rows[i], scope) for i in keep if i in rows]
        if len(ev) < 2:              # 证据闸门照旧：不足两条宁可留着原样也不改写
            continue
        p["evidence"] = ev
        p["t_from"] = min(e["t_start"] for e in ev)
        p["t_to"] = max(e["t_end"] for e in ev)
        p["speakers"] = sorted({e["speaker_label"] for e in ev})
        p["utterance_count"] = len(ev)

    speakers = sorted({r["speaker_label"] for r in scope.rows})
    cats: dict[str, int] = {}
    for r in scope.rows:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    payload["stats"] = {
        "included_utterances": len(scope.rows),
        "speakers": len(speakers), "speaker_labels": speakers,
        "categories": cats,
        "t_from": min((r["t_start"] for r in scope.rows), default=0.0),
        "t_to": max((r["t_end"] for r in scope.rows), default=0.0),
    }
    payload["coverage"] = scope.coverage
    payload["caveats"] = list(scope.caveats) + (
        ["本地抽取式总结：要点为高权重原句组合，未做生成式改写。"]
        if str(payload.get("engine", "")).startswith("extractive") else [])
    payload["filter"] = filters
    payload["topics"] = store.get_ai_topics(conn, sid)
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["cached"] = False
    fp = _fingerprint(scope.rows, "content", filters, settings.resolve()["model"] or "")
    # 和 make_summary 同一条规矩：只有真正走成模型的结果才盖指纹。
    # 理解段挂了、这里是本地抽取兜底时若也盖章，下次「生成总结」就会命中缓存，
    # 把"一次失败"永久固化成这场唯一的总结（0 token 且看起来一切正常）。
    saved_fp = fp if str(payload.get("engine") or "").startswith("llm") else ""
    payload["fingerprint"] = saved_fp
    store.save_summary(conn, sid, "content", filters,
                       str(payload.get("engine") or "extractive"), payload, saved_fp)
    return payload
