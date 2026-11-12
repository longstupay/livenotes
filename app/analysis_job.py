# -*- coding: utf-8 -*-
"""「AI 分析」任务：一次点击 = 一条 run，后台线程按阶段跑，进度落库。

为什么要有这一层：
以前"总结"和"分类"是两个按钮、两条前端循环，用户点完一个还得再点一个，
关掉页面就断，刷新后不知道跑到哪。这里把它们编排成一条服务端任务——
UI 只负责发起和轮询，跑多久、跑到哪、断了怎么办，都由 run 自己记录。

阶段顺序：**理解 → 标注 → 汇总**，对应 P1 / P2 / P3。

  P1 understanding  1 次调用：标题 / 备选名 / 概述 / 要点 / **主题词表** / 主讲人。
                    这一步必须在标注之前：它定下的词表就是后面标注的闭集。
  P2 labeling       1~3 次调用：按 P1 的词表逐句归类，增量（只标没标过的）。
  P3 rollup         0 次调用：用最新标签重算统计与证据，"按分类"视图直接由标签生成，
                    并给总结换一个新指纹——否则标完再点「生成总结」会判定过期，重烧一遍。

失败语义（比"一次全做完"宽松）：
  某一段抛错 → 记在该段上，后一段照常跑（P1 挂了 P2 会自己兜底提主题，
  P2 挂了 P3 仍能把已有标签汇总出来）→ 整体状态 partial 而不是 failed；
  只有全挂才是 failed。已经写进库的结果不回滚，随时可用。
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime

from . import analysis, classify, scheduler, settings, store

# 顺序即执行顺序：理解 -> 标注 -> 汇总
STAGES: dict[str, list[tuple[str, str]]] = {
    "full":    [("summary", "理解与主题"), ("label", "逐句标注"), ("rollup", "汇总")],
    "label":   [("label", "逐句标注")],
    "summary": [("summary", "理解与主题"), ("rollup", "汇总")],
}

_EVENTS: dict[str, threading.Event] = {}
_RUN_JOBS: dict[str, str] = {}          # run_id -> scheduler job id（排队时防误判超时）
_LOCK = threading.Lock()

STALE_SECONDS = 600          # 10 分钟没有任何进度更新 = 线程已经没了
PCT_LO, PCT_HI = 3, 97       # 两头留一点给"排队"和"收尾"


def _stage_defs(kind: str) -> list[dict]:
    return [{"key": k, "label": lb, "status": "pending", "detail": "", "ms": 0}
            for k, lb in STAGES.get(kind, STAGES["full"])]


def _set_stage(conn, run_id: str, stages: list[dict], key: str, status: str,
               detail: str = "", ms: int = 0) -> None:
    for s in stages:
        if s["key"] == key:
            s["status"] = status
            if detail:
                s["detail"] = detail
            if ms:
                s["ms"] = ms
    store.update_run(conn, run_id, stages_json=stages)


def _span(i: int, n: int) -> tuple[float, float]:
    step = (PCT_HI - PCT_LO) / max(1, n)
    return PCT_LO + step * i, PCT_LO + step * (i + 1)


def _global_filters() -> dict:
    """整场口径。和前端「按内容」那一路保持一致，这样任务跑完再点总结能直接命中缓存。"""
    return {"categories": [], "speakers": [], "q": "", "min_conf": None,
            "exclude_low_value": True, "exclude_overlap": True}


def start(conn, sid: str, kind: str = "full") -> dict:
    """发起一条 run。同一场已有在跑的任务时直接返回它（幂等，不排队第二条）。"""
    kind = kind if kind in STAGES else "full"
    if not store.get_session(conn, sid):
        raise RuntimeError("场次不存在")
    if not settings.resolve()["configured"]:
        raise RuntimeError("大模型还没配好：到「⚙ 设置」里填一份能用的")

    # 查"有没有在跑"和"建一条新的"必须在同一把锁里：HTTP 是 ThreadingHTTPServer，
    # 两次点击各自一个线程，只查不锁的话两边都看到"没有在跑"，就会同一场跑两条、
    # 烧两份 token，还互相覆盖进度。
    with _LOCK:
        active = store.get_active_run(conn, sid)
        if active:
            return {"run": active, "already": True}

        run = store.create_run(conn, sid, kind, _stage_defs(kind))
        ev = threading.Event()
        _EVENTS[run["id"]] = ev
        try:
            # 不再每条 run 起一个线程直接跑：交给全局调度器排队，worker 池有限。
            # run 的阶段进度仍然写 analysis_runs，前端照旧轮询 /api/analysis/status。
            job = scheduler.submit(
                "analysis_run", sid, {"run_id": run["id"], "kind": kind},
                priority=scheduler.PRIORITY_ANALYSIS, cache_ttl=0)
        except Exception as e:
            _EVENTS.pop(run["id"], None)
            store.update_run(conn, run["id"], status="failed", error=str(e)[:300],
                             message="排队失败：%s" % e, finished_at=store.now())
            raise
        _RUN_JOBS[run["id"]] = job.id
    return {"run": store.get_run(conn, run["id"]), "already": False,
            "job": job.public()}


def _add_usage(total: dict, usage: dict | None) -> None:
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = (usage or {}).get(k)
        if isinstance(v, int):
            total[k] = total.get(k, 0) + v


def _do_label(conn, run_id: str, sid: str, stages: list[dict], ev: threading.Event,
              usage: dict, lo: float, hi: float) -> tuple[bool, str | None]:
    """逐句标注：分片 + 增量。返回 (是否跑完, 错误信息)。"""
    _set_stage(conn, run_id, stages, "label", "running")
    offset, topics, rounds = 0, None, 0
    while not ev.is_set():
        try:
            r = classify.ai_classify(conn, sid, offset=offset, limit=200, topics=topics)
        except Exception as e:
            traceback.print_exc()
            _set_stage(conn, run_id, stages, "label", "failed", str(e)[:200])
            return False, "标注失败：%s" % e
        _add_usage(usage, r.get("usage"))
        topics = r.get("topics") or topics
        offset = r.get("next_offset") or offset
        total = r.get("total") or max(1, offset)
        rounds += 1
        pct = lo + (hi - lo) * min(1.0, offset / max(1, total))
        msg = "逐句标注 %d/%d" % (offset, total)
        if topics:
            msg += " · %d 个主题" % len(topics)
        store.update_run(conn, run_id, progress=int(min(99, pct)), message=msg)
        _set_stage(conn, run_id, stages, "label", "running", "已标 %d/%d" % (offset, total))
        if r.get("done"):
            if not topics and not r.get("changed"):
                # 没有可标的内容（全是极短场次 / 模型判定无信息），不算失败
                _set_stage(conn, run_id, stages, "label", "done", "无可标注内容")
            else:
                _set_stage(conn, run_id, stages, "label", "done",
                           "%d 句 · %d 个主题" % (offset, len(topics or [])))
            return True, None
        if rounds > 200:                     # 防御：别真跑成死循环
            _set_stage(conn, run_id, stages, "label", "failed", "超过 200 批")
            return False, "标注未收敛（超过 200 批）"
    return False, None                        # 被取消


def _do_summary(conn, run_id: str, sid: str, stages: list[dict],
                usage: dict, lo: float, hi: float) -> tuple[bool, str | None]:
    _set_stage(conn, run_id, stages, "summary", "running")
    store.update_run(conn, run_id, progress=int(lo), message="正在理解整场：标题 / 概述 / 主题…")
    t0 = time.time()
    try:
        p = analysis.make_summary(conn, sid, _global_filters(), "content", "auto")
    except Exception as e:
        traceback.print_exc()
        _set_stage(conn, run_id, stages, "summary", "failed", str(e)[:200],
                   int((time.time() - t0) * 1000))
        return False, "总结失败：%s" % e
    _add_usage(usage, p.get("usage"))
    if p.get("cached"):
        detail = "缓存命中 · 0 token"
    else:
        tok = (p.get("usage") or {}).get("total_tokens")
        detail = ("%s" % (p.get("engine") or "")) + ((" · %d tokens" % tok) if tok else "")
    _set_stage(conn, run_id, stages, "summary", "done", detail,
               int((time.time() - t0) * 1000))
    store.update_run(conn, run_id, progress=int(hi))
    return True, None


def _do_rollup(conn, run_id: str, sid: str, stages: list[dict],
               lo: float, hi: float) -> tuple[bool, str | None]:
    """P3：0 次模型调用。标签变了，统计、覆盖率、证据里的类别快照、总结指纹都要重算。"""
    _set_stage(conn, run_id, stages, "rollup", "running")
    store.update_run(conn, run_id, progress=int(lo), message="汇总：重算统计与证据…")
    t0 = time.time()
    try:
        analysis.finalize(conn, sid, _global_filters())
    except Exception as e:
        traceback.print_exc()
        _set_stage(conn, run_id, stages, "rollup", "failed", str(e)[:200],
                   int((time.time() - t0) * 1000))
        return False, "汇总失败：%s" % e
    _set_stage(conn, run_id, stages, "rollup", "done", "统计与证据已按新标签重算 · 0 token",
               int((time.time() - t0) * 1000))
    store.update_run(conn, run_id, progress=int(hi))
    return True, None


def _worker(run_id: str, sid: str, kind: str, ev: threading.Event) -> None:
    conn = None
    stages = _stage_defs(kind)
    usage: dict = {}
    errors: list[str] = []
    done_any = False
    try:
        # 连接放在 try 里：连不上库也要走到 finally，否则 _EVENTS 里那条永远不会被
        # 清掉，run 会一直挂在 queued，只能等 10 分钟的僵尸清理。
        conn = store.connect()
        store.update_run(conn, run_id, status="running", started_at=store.now(),
                         message="开始分析…", progress=2)
        n = len(stages)
        for i, st in enumerate(stages):
            if ev.is_set():
                break
            lo, hi = _span(i, n)
            ok = True
            if st["key"] == "label":
                ok, err = _do_label(conn, run_id, sid, stages, ev, usage, lo, hi)
            elif st["key"] == "rollup":
                ok, err = _do_rollup(conn, run_id, sid, stages, lo, hi)
            else:
                ok, err = _do_summary(conn, run_id, sid, stages, usage, lo, hi)
            done_any = done_any or ok
            if err:
                errors.append(err)

        canceled = ev.is_set()
        if canceled:
            for s in stages:
                if s["status"] == "running":
                    s["status"] = "canceled"
            status, msg = "canceled", "已取消（已经跑完的部分保留）"
        elif errors and not done_any:
            status, msg = "failed", "分析失败：" + errors[0]
        elif errors:
            status, msg = "partial", "部分完成：" + "；".join(errors)
        else:
            status, msg = "done", "分析完成"
        store.update_run(conn, run_id, status=status,
                         progress=0 if status == "canceled" else 100,
                         message=msg, usage_json=usage, error="；".join(errors),
                         stages_json=stages, finished_at=store.now())
    except Exception as e:                    # 兜底：别把 run 永远挂在 running
        traceback.print_exc()
        try:
            store.update_run(conn, run_id, status="failed", error=str(e)[:300],
                             message="分析异常：%s" % e, finished_at=store.now(),
                             stages_json=stages)
        except Exception:
            pass
    finally:
        with _LOCK:
            _EVENTS.pop(run_id, None)
            _RUN_JOBS.pop(run_id, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _run_job(sid: str, payload: dict) -> None:
    """调度器 worker 的入口：把一条 run 交给原来的阶段执行器。"""
    run_id = str(payload.get("run_id") or "")
    kind = str(payload.get("kind") or "full")
    if not run_id:
        raise RuntimeError("分析任务缺少 run_id")
    with _LOCK:
        ev = _EVENTS.get(run_id)
        if ev is None:
            # 进程重启后从任务台账重试时，内存里的 Event 已经没了；补一个。
            ev = threading.Event()
            _EVENTS[run_id] = ev
    _worker(run_id, sid, kind, ev)


scheduler.register("analysis_run", _run_job)


def _is_stale(run: dict) -> bool:
    ts = run.get("updated_at") or run.get("started_at") or run.get("created_at") or ""
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() > STALE_SECONDS
    except (ValueError, TypeError):
        return False


def status(conn, sid: str, run_id: str | None = None) -> dict:
    run = store.get_run(conn, run_id) if run_id else store.get_latest_run(conn, sid)
    if run and run["active"]:
        # 排队中被调度器压着不等于线程死了：先看它的 job 还在不在。
        # 只有 job 已经结束/丢失，且长时间没有进度，才标 interrupted。
        job_id = _RUN_JOBS.get(run["id"])
        job = scheduler.SCHEDULER.get(job_id) if job_id else None
        queued_alive = (run.get("status") == "queued" and job is not None
                        and job.status in ("queued", "running"))
        if not queued_alive and _is_stale(run):
            # 线程已经不在了（进程被杀、机器睡眠），别让前端一直转圈
            store.update_run(conn, run["id"], status="interrupted", progress=0,
                             message="任务已中断（长时间没有进度）",
                             error="任务已中断（长时间没有进度）", finished_at=store.now())
            run = store.get_run(conn, run["id"])
    return {"run": run, "active": bool(run and run["active"])}


def cancel(conn, sid: str) -> dict:
    run = store.get_active_run(conn, sid)
    if not run:
        return {"ok": False, "reason": "没有正在跑的任务"}
    with _LOCK:
        ev = _EVENTS.get(run["id"])
    if ev:
        ev.set()                      # 线程会在下一个批次边界停下
    else:
        store.update_run(conn, run["id"], status="interrupted", finished_at=store.now(),
                         error="任务线程已不存在", message="已中断")
    return {"ok": True, "run_id": run["id"]}
