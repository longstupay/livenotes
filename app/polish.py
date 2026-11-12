# -*- coding: utf-8 -*-
"""逐字稿润色：删口头禅、补标点，不改事实。

硬性前提：**原文永远不动**。润色结果单独存在 polished 表里，阅读页只做显示层切换，
随时能切回原话对照；导出和 SRT 仍然用原文。

为什么要分批：一场 400 句能拆出 80 多段，一段一次调用既慢又贵，所以 8 段一批，
让模型按 id 返回，缺的段前端自动回退原文。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from . import llm, reader, settings, store

BATCH = 8
MAX_PARAS = 400
_TICKS = chr(96) * 3          # 模型有时会用 markdown 代码块包 JSON，得剥掉
PROMPT_VERSION = "v2"         # 改提示词就改这个，否则旧结果会被缓存复用

PROMPT = """你是中文直播逐字稿的编辑。原稿是语音识别结果，混着口头禅、重复、断句错误和半截话。
你的任务：把它整理成**通顺、能一口气读完**的一段话，像人写完的口语稿。

必须遵守：
1. 删：口头禅、语气词、结巴、无意义重复（呃 / 嗯 / 啊 / 就是说 / 这个这个 / 我我我 / 对对对）。
2. 补：补全被口语打断的句子结构（主语、谓语、宾语、连接词），修掉识别造成的错位和半截话。
3. 调：允许调整语序、拆分长句、合并碎句，只要意思不变。
4. 不许：增加原文没有的事实、数字、人名、案例、结论；不许替说话人补充观点；不许概括成摘要。
5. 数字、价格、人名、公司名、产品名必须原样保留。
6. 可以比原文短，但不要明显变长（不超过原文 1.2 倍），也不要压缩成三言两语。
7. 实在听不出完整意思的段落，做最小修补，宁可保留原样，也不要编。

示例：
输入：呃，那个，我们我们这边其实，就是说，做了三年了，然后，然后呢，主要做那个，家政这一块。
输出：我们这边其实已经做了三年，主要做家政这一块。

只输出严格 JSON，不要代码块：
{"items":[{"id":1,"text":"改写后的文本"}]}
id 必须与输入一一对应，不许增删。"""


def _fingerprint(paras: list[dict], engine: str) -> str:
    h = hashlib.sha1()
    h.update(("%s|%s|%s|%s|%d" % (PROMPT_VERSION, engine, reader.GAP_SECONDS,
                                   reader.MAX_CHARS, len(paras))).encode())
    for p in paras:
        h.update(("%.2f|%s|%s" % (p["t_start"], p["speaker_id"], p["text"])).encode())
    return h.hexdigest()[:20]


def _current(conn: sqlite3.Connection, sid: str) -> tuple[list[dict], str, str]:
    cfg = settings.resolve()
    data = reader.read(conn, sid)
    paras = data["paragraphs"][:MAX_PARAS]
    return paras, _fingerprint(paras, cfg["model"]), cfg["model"]


def cached(conn: sqlite3.Connection, sid: str) -> dict | None:
    """只返回"段落集合、模型、提示词版本"都没变的缓存。"""
    try:
        paras, fp, _ = _current(conn, sid)
    except ValueError:
        return None
    if not paras:
        return None
    row = store.get_polished(conn, sid)
    if not row or row["fingerprint"] != fp:
        return None
    return {"ok": True, "cached": True, "engine": row["engine"],
            "paragraphs": row["payload"].get("paragraphs", [])}


def polish(conn: sqlite3.Connection, sid: str, force: bool = False) -> dict:
    cfg = settings.resolve()
    if not cfg["configured"]:
        raise RuntimeError("大模型还没配好：到「⚙ 设置」里填一份能用的（点「测试连接」确认）")
    paras, fp, model = _current(conn, sid)
    if not paras:
        raise RuntimeError("这一场没有可读的正文")
    if not force:
        hit = cached(conn, sid)
        if hit:
            return hit

    out: list[dict] = []
    failed = 0
    last_error: Exception | None = None
    for start in range(0, len(paras), BATCH):
        batch = paras[start:start + BATCH]
        payload = {"items": [{"id": start + k + 1, "speaker": p["label"], "text": p["text"]}
                             for k, p in enumerate(batch)]}
        try:
            raw = llm.chat([
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ], temperature=0.35, timeout=90, session_id=sid, task="polish")
            body = (raw or "").strip()
            body = re.sub("^" + _TICKS + "(?:json)?|" + _TICKS + "$", "", body, flags=re.M).strip()
            items = json.loads(body).get("items") or []
            by_id = {int(it["id"]): str(it.get("text") or "").strip()
                     for it in items if str(it.get("id", "")).strip().isdigit()}
        except Exception as e:
            failed += 1                      # 这一批失败就跳过，前端用原文兜底
            last_error = e
            # 预算用完 / key 无效 / 模型不存在：后面的批次只会同样失败，别把整场重试一遍。
            if isinstance(e, llm.BudgetExceeded) or (
                    isinstance(e, llm.LLMError) and not e.retryable):
                break
            continue
        for k, p in enumerate(batch):
            i = start + k
            new = by_id.get(i + 1, "")
            # 太短 = 在概括（禁止），太长 = 在加料（禁止）
            src = max(1, len(p["text"]))
            if new and src * 0.35 <= len(new) <= src * 1.2 + 12:
                out.append({"i": i, "text": new})

    if not out:
        detail = str(last_error).strip() if last_error else ""
        err = RuntimeError("润色失败：%s" % (
            detail or "模型没有返回可用的结果，先点「测试连接」确认模型正常"))
        err.__cause__ = last_error  # 保留原始类型（预算 429 / 限流 502）
        raise err
    engine = "llm(" + model + ")"
    store.save_polished(conn, sid, fp, engine, {"paragraphs": out})
    return {"ok": True, "cached": False, "engine": engine, "paragraphs": out,
            "stats": {"paragraphs": len(paras), "polished": len(out), "failed_batches": failed}}
