# -*- coding: utf-8 -*-
"""场次标题：总结顺手起名、候选复用、以及「这个名能不能自动改」的状态机。

为什么单独一层：标题有三种来源（模型 / 结构化 / 原话摘录）和五种归属
（default / auto / auto_local / user / legacy）。散在 reader、live、analysis 里
各写一套，必然出现「总结里叫一个名、改名框里叫另一个名、用户改完又被自动刷掉」。

一条硬规则：标题只由「整场、内容足够」的总结来定；筛选后的局部总结、内容太少的场次
可以给候选，但不许动正式名。
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import llm, settings, store

MAX_TITLE = 24          # 再长就装不进列表、也读不完
MIN_TITLE = 4           # 少于 4 个字基本是「嗯」「对对」这类残句
MIN_ROWS_TO_NAME = 15   # 与 analysis.LLM_MIN_ROWS 对齐：内容太少不自动命名

DEFAULT_PREFIXES = ("抖音直播", "直播监听", "标签页直播")

# 开头常见的口水词，逐层剥掉再当标题
_LEAD = re.compile(r"^(?:呃+|嗯+|啊+|哦+|唉+|那个+|这个+|就是说|就是|然后|所以|反正|其实|"
                   r"对对+|好的|行|那|我我|你你)+")
_TAIL_PUNCT = "。！？，、；：,.!?;:"
_WRAP = "\"'“”‘’《》「」『』【】[]()（）<> "
# 起名时最没信息量的开场白：这些词一出现，标题就废了
_BAD_LEAD = re.compile(r"^(?:本期|这期|今天|这场|本次|这个视频|直播|分享|干货|复盘|连麦|"
                       r"欢迎|咱们|我们)[：:·\s\-—]*")
_NOISE = re.compile(r"[【】\[\]「」『』《》<>]")
_WS = re.compile(r"\s+")


def is_default_title(title: str | None) -> bool:
    return str(title or "").strip().startswith(DEFAULT_PREFIXES)


def source_of(title: str | None) -> str:
    """老库没有 title_source 时的推断：默认名 = 系统起的，其它一律当用户起的。"""
    return "default" if is_default_title(title) else "legacy"


def clean_title(raw: Any, limit: int = MAX_TITLE) -> str | None:
    """把模型输出洗成一个能进列表的名字；不合格返回 None（绝不硬凑）。"""
    t = str(raw or "").strip()
    if not t:
        return None
    t = t.splitlines()[0].strip() if t.splitlines() else t
    for _ in range(3):
        t = t.strip(_WRAP)
    t = _NOISE.sub("", t)
    t = _WS.sub(" ", t).strip()
    t = t.strip(_TAIL_PUNCT + " ")
    # 去掉「本期/直播」这类开场白，但别把整句削没了
    without = _BAD_LEAD.sub("", t).strip(_TAIL_PUNCT + " ")
    if len(without) >= MIN_TITLE:
        t = without
    if len(t) > limit:
        t = t[:limit].rstrip(_TAIL_PUNCT + " ")
    if len(t) < MIN_TITLE:
        return None
    if t == "未命名场次" or not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", t):
        return None
    return t


def clean_alts(alts: Any, main: str | None) -> list[str]:
    out: list[str] = []
    for a in (alts or [])[:4]:
        c = clean_title(a)
        if c and c != main and c not in out:
            out.append(c)
    return out[:3]


# ---------------------------------------------------------------- 本地候选

def clean_quote(text: str, limit: int = 14) -> str:
    """把一句 ASR 原文洗成能当候选的短句：剥口水词、截到第一个句读。"""
    t = (text or "").strip()
    for _ in range(4):
        m = _LEAD.match(t)
        if not m:
            break
        t = t[m.end():].lstrip(_TAIL_PUNCT + " 　")
    if not t:
        return ""
    cut = len(t)
    for k, ch in enumerate(t):
        if ch in _TAIL_PUNCT and k >= 6:
            cut = k
            break
    t = t[:min(cut, limit)].strip()
    return t if len(t) >= 4 else ""


def pick_quotes(rows: list[dict], n: int = 2) -> list[str]:
    """早/中/晚各取一句信息量最高的，保证备选不是一个调。"""
    rows = [r for r in rows if (r.get("text") or "").strip()]
    if not rows:
        return []
    third = max(1, len(rows) // 3)
    buckets = [rows[:third], rows[third:2 * third], rows[2 * third:]]
    scored = []
    for b in buckets:
        if not b:
            continue
        scored.append(max(b, key=lambda r: (float(r.get("importance") or 0.5),
                                            len(r.get("text") or ""))))
    scored.sort(key=lambda r: float(r.get("importance") or 0.5), reverse=True)
    out: list[str] = []
    for r in scored:
        q = clean_quote(r.get("text") or "")
        if q and q not in out:
            out.append(q)
        if len(out) >= n:
            break
    return out


def structural(conn: sqlite3.Connection, sid: str) -> str:
    """没有模型时的兜底：人数 + 最高频话题，比裸「5 人连麦」多一档信息。"""
    spk = [p for p in store.speaker_totals(conn, sid) if float(p.get("seconds") or 0) > 0]
    n = len(spk)
    head = "%d 人连麦" % n if n >= 2 else ("单人直播" if n == 1 else "未识别说话人")
    topic = ""
    try:
        tally = store.topic_tally(conn, sid)
    except Exception:
        tally = []
    if tally:
        topic = tally[0][0]
    return (head + " · " + topic) if topic else head


def llm_title(rows: list[dict], session_id: str = "") -> str | None:
    """显式「换一个」时才走这条路；总结里的标题不重复调模型。"""
    if not rows or not settings.resolve()["configured"]:
        return None
    step = max(1, len(rows) // 40)
    sample = rows[::step][:40]
    prompt = ("你是中文直播运营。下面是一场直播的逐字稿片段，给这场直播起一个"
              "回看列表里能用的名字。只输出一行标题，不要解释。规则：\n"
              "1. 6~16 个汉字，名词短语或判断句，不要疑问句、不要问号；\n"
              "2. 不写日期、时间、人数（系统已有）；\n"
              "3. 不用「本期/这期/今天/这场/直播/分享/干货/复盘」开头；\n"
              "4. 不加引号、书名号、句末标点；\n"
              "5. 必须能和别的场次区分：写清「谁 × 什么主题 × 什么场景」。")
    try:
        text = llm.chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": "\n".join((r.get("text") or "")[:120] for r in sample)},
        ], temperature=0.4, timeout=20, max_tokens=48,
            session_id=session_id, task="title")
    except Exception:
        return None
    return clean_title(text)


# ---------------------------------------------------------------- 写回状态机

def state(conn: sqlite3.Connection, sid: str) -> dict:
    s = store.get_session(conn, sid) or {}
    try:
        alts = json.loads(s.get("title_alts_json") or "[]")
    except ValueError:
        alts = []
    return {
        "title": s.get("title") or "",
        "source": s.get("title_source") or source_of(s.get("title")),
        "alts": [a for a in alts if isinstance(a, str)],
    }


def apply_summary(conn: sqlite3.Connection, sid: str, *, title: Any, alts: Any,
                  engine_kind: str, whole: bool, enough: bool,
                  title_kind: str = "llm") -> dict:
    """把这次总结起的名字并进场次。返回 {applied, title, alts, source, reason, current}。

    规则（等价于设计文档 3.3 的状态机）：
      default     + 够内容  -> 自动命名
      auto/auto_local       -> 允许静默更新，但不许用降级结果覆盖模型名
      user/legacy           -> 永不覆盖，只存候选
      局部总结 / 内容太少    -> 不动正式名，只存候选
    """
    st = state(conn, sid)
    src = st["source"]
    cur = st["title"]
    main = clean_title(title)
    clean = clean_alts(alts, main)

    def out(applied: bool, reason: str) -> dict:
        return {"applied": applied, "title": main, "alts": clean,
                "source": src, "reason": reason, "current": cur}

    if not whole:
        return out(False, "局部筛选的总结，不拿来改场次名")
    if not main:
        return out(False, "内容不足，这次没起名")
    if src in ("user", "legacy", "demo"):
        _save_alts(conn, sid, clean)
        return out(False, "这场已经有人命名，只更新候选")
    if not enough:
        _save_alts(conn, sid, clean)
        return out(False, "内容太少，先保留默认名")
    if title_kind != "llm" and "·" not in main:
        # 本地只能算出「6 人连麦」这种，还不足以顶替默认名，留着当候选
        _save_alts(conn, sid, clean)
        return out(False, "本地只能给出人数，先保留默认名")

    if src == "default":
        new_src = "auto" if engine_kind == "llm" else "auto_local"
        store.set_title(conn, sid, main, new_src, clean)
        src, cur = new_src, main
        return out(True, "已按内容命名")

    # auto / auto_local：系统起的名，可以更新，但不能越改越差
    if engine_kind != "llm" and src == "auto":
        _save_alts(conn, sid, clean)
        return out(False, "本地抽取的候选不如现有模型名，未覆盖")
    if main == cur:
        _save_alts(conn, sid, clean)
        return out(False, "名字没变")
    new_src = "auto" if engine_kind == "llm" else "auto_local"
    store.set_title(conn, sid, main, new_src, clean)
    src, cur = new_src, main
    return out(True, "已更新为新总结的名字")


def _save_alts(conn: sqlite3.Connection, sid: str, alts: list[str]) -> None:
    if alts:
        store.set_title_alts(conn, sid, alts)


def adopt(conn: sqlite3.Connection, sid: str, title: Any) -> dict:
    """用户点「采用」= 用户命名，此后不再自动覆盖。"""
    t = clean_title(title, limit=60)
    if not t:
        raise ValueError("标题不合适（太短或只有标点）")
    store.rename_session(conn, sid, t, source="user")
    return {"ok": True, "session": store.get_session(conn, sid)}


def local_apply(conn: sqlite3.Connection, sid: str, min_rows: int = MIN_ROWS_TO_NAME) -> dict:
    """总结没跑成时的兜底：只用本地信息（人数 + 话题）起名，0 token。

    只在还是默认名时动手；用户起过的名字一个都不碰。
    """
    st = state(conn, sid)
    if st["source"] != "default":
        return {"applied": False, "reason": "不是默认名，不动", "title": st["title"]}
    rows = store.session_transcript(conn, sid)
    if len(rows) < min_rows:
        return {"applied": False, "reason": "内容太少（%d 条）" % len(rows), "title": st["title"]}
    t = structural(conn, sid)
    if not t:
        return {"applied": False, "reason": "算不出结构化标题", "title": st["title"]}
    store.set_title(conn, sid, t, "auto_local", [])
    return {"applied": True, "reason": "本地命名", "title": t}
