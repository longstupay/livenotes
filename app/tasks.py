# -*- coding: utf-8 -*-
"""业务 LLM 任务：把「总结 / 润色 / AI 分类 / 下播收尾」登记到全局调度器。

调度器本身不认识业务，只认 kind + payload + handler；这个模块是唯一的注册处。
好处：
  - 任务台账里存的是可序列化的 payload，重启后仍能按 kind 找回处理器重试；
  - 指纹只包含"会改变输出"的输入（筛选条件、内容摘要、提示词版本、模型），
    不包含 auto/force 这类执行开关，所以「下播自动总结」和「用户紧接着点生成总结」
    会命中同一次调用，而不是把同一份稿子传两遍。
"""
from __future__ import annotations

import traceback

from . import (analysis, classify, logs, polish, reader, recluster, scheduler,
               store, titles)

_FILTER_KEYS = ("t_from", "t_to", "categories", "speakers", "q",
                "exclude_low_value", "exclude_overlap", "min_conf")


def normalize_filters(filters: dict | None) -> dict:
    """把 HTTP 传来的筛选条件收敛成稳定形状，指纹才不会因为键顺序/None 抖动。"""
    f = dict(filters or {})
    out = {k: f.get(k) for k in _FILTER_KEYS}
    out["categories"] = sorted(str(x) for x in (out.get("categories") or []))
    out["speakers"] = sorted(str(x) for x in (out.get("speakers") or []))
    out["q"] = str(out.get("q") or "").strip()[:120]
    for key in ("exclude_low_value", "exclude_overlap"):
        v = out.get(key)
        out[key] = True if v is None else bool(v)
    out["min_conf"] = out.get("min_conf")
    return out


def _digest(sid: str) -> str:
    conn = store.connect()
    try:
        return analysis.session_digest(conn, sid)
    finally:
        conn.close()


def summary_payload(filters: dict, mode: str, engine: str, digest: str,
                    auto: bool = False, recluster: bool = False) -> dict:
    return {
        "filters": normalize_filters(filters), "mode": mode, "engine": engine,
        "digest": digest, "prompt": analysis.SUMMARY_PROMPT_VERSION,
        "auto": bool(auto), "recluster": bool(recluster),
    }


def fingerprint_payload(payload: dict) -> dict:
    """只保留真正会改变模型输出的输入。

    - auto / recluster：只影响失败后的本地命名、以及收尾前要不要重聚类，
      不改变模型输入；
    - exclude_overlap / min_conf：build_scope 里被强制为 True / 0.45，
      用户勾不勾、传什么值都不会改变送给模型的内容，不能拿来拆分指纹。
    """
    fp = {k: v for k, v in payload.items() if k not in ("auto", "recluster")}
    filters = dict(fp.get("filters") or {})
    filters.pop("exclude_overlap", None)
    filters.pop("min_conf", None)
    fp["filters"] = filters
    return fp


# ---------------------------------------------------------------- 总结

def _run_summary(sid: str, payload: dict) -> dict | scheduler.TaskResult:
    conn = store.connect()
    try:
        if payload.get("recluster"):
            # 收尾任务先按整场声纹重分人，再总结；用户手动点总结不重聚类。
            try:
                recluster.recluster_session(conn, sid)
            except Exception as e:
                logs.log_event("live.recluster_failed", level="warn",
                               session_id=sid, error=str(e))
        f = dict(payload.get("filters") or {})
        f["session_id"] = sid
        out = analysis.make_summary(conn, sid, f, payload.get("mode") or "content",
                                    payload.get("engine") or "auto")
        if payload.get("auto"):
            logs.log_event("live.finalize_done", session_id=sid,
                           engine=str(out.get("engine") or ""),
                           llm_error=bool(out.get("llm_error")))
        if out.get("llm_error"):
            # 模型调用失败后降级成了本地抽取式：结果可以返回，但别当成成功缓存，
            # 否则用户修好 key / 额度后再点，10 分钟内拿到的还是这份降级稿。
            return scheduler.TaskResult(out, cacheable=False)
        return out
    except Exception:
        # 自动收尾失败时别让场次一直叫「直播监听 11-05 03:41」：先落一份本地抽取式
        # 总结（0 token），再用本地信息命名。仍然把失败抛出去，任务状态是 failed，
        # 用户可以重试，而不是被缓存成"已完成"。
        if payload.get("auto"):
            try:
                f = dict(payload.get("filters") or {})
                f["session_id"] = sid
                analysis.make_summary(conn, sid, f, "content", "extractive")
                logs.log_event("live.fallback_summary", level="warn", session_id=sid)
            except Exception:
                traceback.print_exc()
            try:
                r = titles.local_apply(conn, sid)
                logs.log_event("live.local_title", session_id=sid,
                               title=r.get("title"), reason=r.get("reason"))
            except Exception:
                traceback.print_exc()
        raise
    finally:
        conn.close()


def submit_summary(sid: str, filters: dict, mode: str = "content",
                   engine: str = "auto", *, auto: bool = False,
                   recluster: bool = False,
                   force: bool = False) -> scheduler.Job:
    payload = summary_payload(filters, mode, engine, _digest(sid), auto, recluster)
    return scheduler.submit(
        "summary", sid, payload,
        priority=scheduler.PRIORITY_FINALIZE if auto else scheduler.PRIORITY_USER,
        fingerprint_payload=fingerprint_payload(payload), force=force)


def submit_finalize(sid: str) -> scheduler.Job:
    """下播后的自动收尾。与用户默认「按内容」总结使用同一套筛选条件。"""
    return submit_summary(sid, dict(analysis.WHOLE_FILTERS), "content", "auto",
                          auto=True, recluster=True)


# ---------------------------------------------------------------- 润色

def _run_polish(sid: str, payload: dict) -> dict:
    conn = store.connect()
    try:
        return polish.polish(conn, sid, force=bool(payload.get("force")))
    finally:
        conn.close()


def submit_polish(sid: str, force: bool = False) -> scheduler.Job:
    payload = {
        "digest": _digest(sid), "prompt": polish.PROMPT_VERSION,
        "gap": reader.GAP_SECONDS, "max_chars": reader.MAX_CHARS,
        "force": bool(force),
    }
    fp_payload = {k: v for k, v in payload.items() if k != "force"}
    return scheduler.submit(
        "polish", sid, payload, priority=scheduler.PRIORITY_ANALYSIS,
        fingerprint_payload=fp_payload, force=force)


# ---------------------------------------------------------------- AI 分类

def _run_classify(sid: str, payload: dict) -> dict:
    conn = store.connect()
    try:
        return classify.ai_classify(
            conn, sid,
            offset=int(payload.get("offset") or 0),
            limit=int(payload.get("limit") or classify.BATCH),
            topics=payload.get("topics"),
            force=bool(payload.get("force")))
    finally:
        conn.close()


def classify_fingerprint_payload(payload: dict) -> dict:
    """分类的指纹不包含内容摘要。

    分类本身会改 utterances.category，而摘要包含 category——把摘要放进指纹，
    任务跑起来后摘要就变了，用户连点第二下反而对不上正在跑的任务。
    分片位置 + 话题 + 提示词版本足以定义「同一片」；跑完后 cache_ttl=0，
    再点仍然会重新分类（例如先还原了规则）。
    """
    return {k: v for k, v in payload.items() if k != "digest"}


def submit_classify(sid: str, offset: int = 0, limit: int | None = None,
                    topics: list | None = None, force: bool = False) -> scheduler.Job:
    topics_n = sorted(str(t) for t in topics) if isinstance(topics, list) else None
    payload = {
        "offset": max(0, int(offset or 0)),
        "limit": max(1, int(limit or classify.BATCH)),
        "topics": topics_n, "digest": _digest(sid),
        "prompt": classify.PROMPT_VERSION, "force": bool(force),
    }
    # 分类是分片推进的：同一片正在跑时复用，跑完后再点应该允许重新分类（比如先还原了规则）。
    return scheduler.submit(
        "classify", sid, payload, priority=scheduler.PRIORITY_ANALYSIS,
        fingerprint_payload=classify_fingerprint_payload(payload),
        cache_ttl=0)


scheduler.register("summary", _run_summary)
scheduler.register("polish", _run_polish)
scheduler.register("classify", _run_classify)
