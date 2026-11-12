# -*- coding: utf-8 -*-
"""AI 智能分类：把规则分类换成模型判断，但规则结果留底可还原。

为什么保留规则分类：它是零成本、离线、可复现的基线。模型可能更强，也可能更玄，
两条路都留着，用户切一下就能对比——这比"只有一种正确答案"诚实。

关于"增量"：每句在写回时盖一个 ai_fp（提示词版本 + 模型 + 主题集 + 原文的哈希）。
重跑时只处理指纹不符的行，直播追加的新句只标新增部分——不再整场重打一遍。
``force=True`` 才清空指纹全量重标。

关于"主题"：默认不再自己归纳。理解段（`analysis._llm_points`）已经把词表定好了，
这里只做闭集归类；只有在没有现成词表时才自行归纳一次兜底。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

from . import llm, settings, store
from .models import CATEGORY_META

BATCH = 120          # 一次请求处理的句数（紧凑输出后可以放大，321 句通常 3 片）
MAX_BATCH = 200
TOPIC_SAMPLE = 120          # 兜底归纳主题时给模型看的句数上限
_TICKS = chr(96) * 3
PROMPT_VERSION = "v2"       # v2: 输出改成 [id, 类别码, 主题下标]

CAT_CODE = {"substantive": 1, "question_consult": 2, "host_control": 3,
            "chitchat": 4, "ad": 5}
CODE_CAT = {v: k for k, v in CAT_CODE.items()}

PROMPT = """你是中文直播逐字稿的分类器。给每一句话打一个类别和一个话题。

类别只能用下面五个代号：
1 = substantive：实质讨论（观点、经验、知识、方案、业务细节、价格逻辑）
2 = question_consult：上麦咨询/提问（在提问、求助、咨询）
3 = host_control：主持控场（点名、欢迎、排队、麦序、过渡语）
4 = chitchat：闲聊（无信息量的寒暄、语气词、调侃、附和）
5 = ad：广告引流（加微信、导流、报价、下单、关注引导、优惠）

话题：不超过 6 个汉字；闲聊和控场可以留空。

只输出严格 JSON，不要代码块：
{"items":[{"id":1,"category":"substantive","topic":"设备预算"}]}
id 必须与输入一一对应，不许增删。"""


TOPIC_PROMPT = """你在给一场中文直播做主题归纳。
读下面的发言，把它们归纳成 **恰好 %d 个**主题（不要多也不要少）。

要求：
- 每个主题名不超过 6 个汉字，是名词短语（如「设备预算」「账号风控」「引流话术」）；
- 主题之间不要重叠，覆盖尽量多的内容；
- 不要输出「其他」「杂项」这类兜底主题；
- 按重要性从高到低排序。

只输出严格 JSON，不要代码块：
{"topics":["主题1","主题2",...]}
"""

# 闭集归类：紧凑编码，输入 [[id, 文本]] + topics 下标表，输出 [[id, 类别码, 主题下标]]
CLASSIFY_PROMPT = """你是中文直播逐字稿的分类器。给每一句话打一个类别代号，并归到给定的主题之一。

类别代号（只能填 1~5）：
1 实质讨论（观点、经验、知识、方案、业务细节、价格逻辑）
2 上麦咨询/提问（提问、求助、咨询）
3 主持控场（点名、欢迎、排麦、过渡语）
4 闲聊（寒暄、语气词、附和）
5 广告引流（加微信、导流、报价、下单、关注引导）

主题必须从 topics 数组里选，写它的**下标**（从 0 开始）；实在归不进任何一个写 -1。

输入格式：{"topics":["主题0","主题1"],"items":[[id,原文], ...]}
输出严格 JSON，不要代码块：
{"items":[[id,类别代号,主题下标], ...]}

硬性规则：
1. items 与输入一一对应，不许增删、不许改 id；
2. 每项就是三个数字的数组，不要写成对象；
3. 类别代号只能是 1~5 的整数。"""


def _clean(raw: str) -> str:
    body = (raw or "").strip()
    return re.sub("^" + _TICKS + "(?:json)?|" + _TICKS + "$", "", body, flags=re.M).strip()


def _sig(topics: list[str], model: str) -> str:
    """这一轮标注的"口径签名"：主题集/模型/提示词任一变了，旧标签就该作废重标。"""
    h = hashlib.sha1()
    h.update(("%s|%s|%s" % (PROMPT_VERSION, model or "", "|".join(topics))).encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def _row_fp(text: str, sig: str) -> str:
    h = hashlib.sha1()
    h.update((sig + "\x00" + (text or "")).encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def _propose_topics(rows: list[dict], n: int, usage: dict,
                    session_id: str = "") -> list[str]:
    """兜底：没有现成词表时自己归纳一套（正常流程里理解段已经给过了）。"""
    step = max(1, len(rows) // TOPIC_SAMPLE)
    sample = rows[::step][:TOPIC_SAMPLE]
    try:
        raw = llm.chat([
            {"role": "system", "content": TOPIC_PROMPT % n},
            {"role": "user", "content": json.dumps(
                {"items": [{"text": (r["text"] or "")[:120]} for r in sample]},
                ensure_ascii=False)},
        ], temperature=0.2, timeout=90, usage=usage,
            session_id=session_id, task="classify")
        data = json.loads(_clean(raw))
        topics: list[str] = []
        for t in (data.get("topics") or []):
            t = str(t).strip()[:12]
            if t and t not in topics:
                topics.append(t)
        return topics[:n]
    except Exception:
        return []


def _dirty_rows(conn: sqlite3.Connection, sid: str, sig: str) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT id,text,ai_fp FROM utterances WHERE session_id=? ORDER BY t_start", (sid,))]
    return [r for r in rows if (r.get("ai_fp") or "") != _row_fp(r["text"], sig)]


def _parse_items(items: list, topics: list[str]) -> list[tuple[int, str, str]]:
    """兼容两种输出：新格式 [id, 类别码, 主题下标]，旧格式 {"id","category","topic"}。

    兼容不是洁癖：换提示词之后如果某个中转站回退到旧格式，整场分类不该因此全废。
    """
    out: list[tuple[int, str, str]] = []
    for it in items or []:
        if isinstance(it, (list, tuple)) and len(it) >= 2:
            try:
                uid = int(it[0])
            except (TypeError, ValueError):
                continue
            raw_code = it[1]
            if isinstance(raw_code, str) and raw_code.strip().isdigit():
                raw_code = int(raw_code.strip())
            cat = CODE_CAT.get(raw_code) if isinstance(raw_code, int) else None
            if cat is None:
                cat = str(raw_code).strip()
            topic = ""
            if len(it) > 2 and isinstance(it[2], int) and 0 <= it[2] < len(topics):
                topic = topics[it[2]]
        elif isinstance(it, dict):
            try:
                uid = int(it.get("id"))
            except (TypeError, ValueError):
                continue
            cat = str(it.get("category") or "").strip()
            topic = str(it.get("topic") or "").strip()[:12]
        else:
            continue
        if cat not in CATEGORY_META:
            continue
        if topics and topic and topic not in topics:
            topic = _nearest_topic(topic, topics)
        out.append((uid, cat, topic))
    return out


def ai_classify(conn: sqlite3.Connection, sid: str, offset: int = 0,
                limit: int = BATCH, topics: list[str] | None = None,
                force: bool = False) -> dict:
    """分片 + 增量分类：一次只处理一批"还没标过 / 口径变了"的句子，前端循环调用。

    为什么分片：整场一次做完要连续调好几轮模型，几分钟的请求中间断一次就全白做，
    而且用户看不到任何进度。分片后每片十几秒、有进度、断了能从断点继续。
    为什么增量：口播直播是持续追加的，重跑时只补新增句子，实测能省掉大半调用。
    """
    cfg = settings.resolve()
    if not cfg["configured"]:
        raise RuntimeError("大模型还没配好：到「⚙ 设置」里填一份能用的")

    all_rows = [dict(r) for r in conn.execute(
        "SELECT id,text FROM utterances WHERE session_id=? ORDER BY t_start", (sid,))]
    total = len(all_rows)
    if not total:
        raise RuntimeError("这一场没有可分类的发言")

    offset = max(0, min(int(offset or 0), total))
    limit = max(1, min(MAX_BATCH, int(limit or BATCH)))
    usage: dict = {}
    t0 = time.time()
    topics_in = topics if isinstance(topics, list) and topics else None

    # ---- 一轮 run 的第一片：决定这一轮用哪套主题 ----
    proposed = False
    if offset == 0:
        if force:
            store.clear_ai_labels(conn, sid)     # 全部重标：先让每一句变脏
        stored = store.get_ai_topics(conn, sid)
        if force or (topics_in is None and not stored):
            topics = _propose_topics(all_rows, settings.prefs()["topics"], usage,
                                     session_id=sid)
            proposed = True
            store.set_ai_topics(conn, sid, topics)
        else:
            topics = topics_in if topics_in is not None else stored
    else:
        topics = topics_in if topics_in is not None else store.get_ai_topics(conn, sid)
    topics = topics or []

    sig = _sig(topics, cfg["model"] or "")
    dirty = _dirty_rows(conn, sid, sig)
    batch = dirty[:limit]
    if not batch:
        state = store.ai_state(conn, sid)
        return {"ok": True, "changed": 0, "offset": offset, "next_offset": state["labeled"],
                "total": total, "dirty_total": 0, "remaining": 0, "done": True,
                "topics": topics, "reused_topics": not proposed,
                "engine": "llm(" + (cfg["model"] or "configured") + ")",
                "usage": usage or None, "ms": int((time.time() - t0) * 1000),
                "prompt_version": PROMPT_VERSION,
                "reason": "没有需要标注的句子（都已按当前口径标过）"}

    # 写回之前先把规则分类存一份底：这是"还原"唯一的数据来源
    store.snapshot_rule_categories(conn, sid)

    payload = {"topics": topics,
               "items": [[r["id"], (r["text"] or "")[:160]] for r in batch]}
    hint = ""
    if topics:
        hint = "（主题表共 %d 个，下标 0~%d）" % (len(topics), len(topics) - 1)
    try:
        raw = llm.chat([
            {"role": "system", "content": CLASSIFY_PROMPT + hint},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], temperature=0.1, timeout=90, usage=usage, session_id=sid, task="classify")
        items = json.loads(_clean(raw)).get("items") or []
    except Exception as e:
        err = RuntimeError("这一片分类失败：%s（已经标好的 %d 句不会重做，可以直接重试）"
                           % (e, store.ai_state(conn, sid)["labeled"]))
        err.__cause__ = e            # 保留原始类型，HTTP 层才能区分 429 / 502 / 400
        raise err

    fps = {r["id"]: _row_fp(r["text"], sig) for r in batch}
    out: list[tuple[int, str, str, str, str]] = []
    for uid, cat, topic in _parse_items(items, topics):
        if uid not in fps:
            continue
        out.append((uid, cat, CATEGORY_META[cat][2], topic, fps[uid]))

    if out:
        store.apply_ai_categories(conn, sid, out)
    state = store.ai_state(conn, sid)
    # 剩余量按"写回之后还脏的行"算，而不是按批次减：模型偶尔会漏掉几个 id，
    # 用减法会提前报完成，那几句就永远停在规则分类上。
    left = len(_dirty_rows(conn, sid, sig))
    if left and not out:
        raise RuntimeError("这一片模型没有返回可用结果（0 条写入），先点「测试连接」确认模型正常")
    return {
        "ok": True, "changed": len(out), "offset": offset,
        "next_offset": state["labeled"], "total": total,
        "dirty_total": len(dirty), "remaining": left,
        "done": left == 0, "topics": topics, "reused_topics": not proposed,
        "engine": "llm(" + (cfg["model"] or "configured") + ")",
        "usage": usage or None, "ms": int((time.time() - t0) * 1000),
        "prompt_version": PROMPT_VERSION,
    }


def _nearest_topic(text: str, topics: list[str]) -> str:
    """模型没照主题列表写时，就近塞进最像的那个，避免又冒出几百个话题。"""
    if not text:
        return topics[0]
    for t in topics:
        if t in text or text in t:
            return t
    return topics[0]


def restore(conn: sqlite3.Connection, sid: str) -> dict:
    n = store.restore_rule_categories(conn, sid)
    return {"ok": True, "restored": n}
