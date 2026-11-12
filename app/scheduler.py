# -*- coding: utf-8 -*-
"""全局 LLM 任务调度：一个队列、固定并发、指数退避、token 预算、指纹幂等。

为什么需要这一层：
以前每场直播 `stop()` 都直接起一个线程打模型，每条 AI 分析也各起一个线程；
10 个直播间同时下播就是 10 个总结请求同时打出去，既没有上限也没有预算，
限流和账单都只能靠运气。现在所有模型调用先经过这里：

  submit(kind, session_id, payload)   业务任务入队（总结/润色/分类/自动收尾）
        │
        ├─ 指纹去重：同一份输入在跑 -> 复用同一个 Job；刚成功 -> 直接返回缓存
        ├─ 优先级：用户点击(1) > 润色/分类(2) > 自动收尾(5)
        ▼
   worker 池（LIVENOTES_LLM_CONCURRENCY，默认 2）
        │
        ▼
   llm.chat() —— 每次调用再经过并发槽、预算预占、429/5xx 指数退避

任务和用量都会落 SQLite（llm_jobs / llm_usage）：
  - 重启后仍能看到哪些任务失败了，可 POST /api/jobs/retry 重试；
  - 单场/全局 token 预算跨重启累计，不会因为重启把额度清零。
落库失败只打一行日志，调度继续在内存里跑——台账不能反过来拖垮业务。
"""
from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from . import logs, metrics, settings, store
from .config import (
    LLM_BACKOFF_BASE_S,
    LLM_BACKOFF_MAX_S,
    LLM_CONCURRENCY,
    LLM_DAILY_TOKEN_BUDGET,
    LLM_DEDUPE_TTL_S,
    LLM_EXPECTED_OUTPUT_TOKENS,
    LLM_JOB_HISTORY,
    LLM_MAX_ATTEMPTS,
    LLM_PRICE_INPUT_PER_MTOK,
    LLM_PRICE_OUTPUT_PER_MTOK,
    LLM_SESSION_TOKEN_BUDGET,
    LLM_TASK_TIMEOUT_S,
)

Handler = Callable[[str, dict], Any]
FINGERPRINT_VERSION = "1"      # 指纹算法升级时改这个，旧缓存自然失效

PRIORITY_USER = 1              # 用户点了按钮，等在前端
PRIORITY_ANALYSIS = 2          # 润色 / 逐句分类
PRIORITY_FINALIZE = 5          # 下播后的自动收尾，可以慢慢排队


class BudgetExceeded(RuntimeError):
    """单场或全局 token 预算不足。调用方应回退本地抽取式，而不是重试。"""

    def __init__(self, scope: str, used: int, limit: int, detail: str = ""):
        self.scope = scope
        self.used = int(used)
        self.limit = int(limit)
        self.detail = detail
        label = "本场" if scope == "session" else "今日全局"
        msg = "%s token 预算已用完（已用 %d / 上限 %d）" % (label, self.used, self.limit)
        if detail:
            msg += "；" + detail
        super().__init__(msg)


class JobFailed(RuntimeError):
    """Job.wait() 时任务已失败。cause 是处理器抛出的原始异常。"""

    def __init__(self, job: "Job"):
        self.job = job
        self.cause = job.error_obj
        super().__init__(job.error or "任务失败")
        if self.cause is not None:
            self.__cause__ = self.cause


class JobTimeout(RuntimeError):
    """等待任务完成超时；任务本身可能还在排队或执行。"""

    def __init__(self, job: "Job", timeout: float):
        self.job = job
        self.timeout = timeout
        super().__init__("任务 %s 在 %.0f 秒内未完成（当前状态：%s）"
                         % (job.id, timeout, job.status))


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _err(e: BaseException) -> str:
    text = str(e) or type(e).__name__
    return text[:500]


@dataclass
class Reservation:
    """一次模型调用的预算预占。成功 settle、失败 release，不能两边都做。"""

    session_id: str
    day: str
    task: str
    amount: int
    model: str = ""


@dataclass
class TaskResult:
    """处理器返回它，表示「结果可以给调用方，但别缓存」。

    典型场景：总结时模型调用失败，业务层降级成本地抽取式。这不是任务失败，
    但也不该把降级结果当成成功缓存 10 分钟，否则用户修好 key 再点还是旧结果。
    """
    value: Any
    cacheable: bool = True


@dataclass
class Job:
    id: str
    kind: str
    session_id: str
    fingerprint: str
    payload: dict
    priority: int = PRIORITY_FINALIZE
    cache_ttl: float = 0.0
    status: str = "queued"          # queued | running | succeeded | failed | cancelled
    attempts: int = 0
    error: str = ""
    error_obj: BaseException | None = field(default=None, repr=False)
    result: Any = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    queue_token: int = 0
    cached: bool = False
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    def wait(self, timeout: float | None = None) -> Any:
        if not self._done.wait(timeout):
            raise JobTimeout(self, float(timeout or 0))
        if self.status != "succeeded":
            raise JobFailed(self)
        return self.result

    def done(self) -> bool:
        return self._done.is_set()

    def public(self) -> dict:
        ms = 0
        if self.started_at and self.finished_at:
            ms = int((self.finished_at - self.started_at) * 1000)
        return {
            "id": self.id, "kind": self.kind, "session_id": self.session_id,
            "status": self.status, "attempts": self.attempts, "error": self.error,
            "priority": self.priority, "cached": self.cached, "ms": ms,
            "created_at": _iso(self.created_at), "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }


class Scheduler:
    def __init__(self, *, concurrency: int = LLM_CONCURRENCY,
                 max_attempts: int = LLM_MAX_ATTEMPTS,
                 backoff_base: float = LLM_BACKOFF_BASE_S,
                 backoff_max: float = LLM_BACKOFF_MAX_S,
                 session_budget: int = LLM_SESSION_TOKEN_BUDGET,
                 daily_budget: int = LLM_DAILY_TOKEN_BUDGET,
                 dedupe_ttl: float = LLM_DEDUPE_TTL_S,
                 task_timeout: float = LLM_TASK_TIMEOUT_S,
                 expected_output_tokens: int = LLM_EXPECTED_OUTPUT_TOKENS,
                 job_history: int = LLM_JOB_HISTORY,
                 persist: bool = True, name: str = "llm") -> None:
        self.concurrency = max(1, int(concurrency))
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_max = max(0.0, float(backoff_max))
        self.session_budget = max(0, int(session_budget))
        self.daily_budget = max(0, int(daily_budget))
        self.dedupe_ttl = max(0.0, float(dedupe_ttl))
        self.task_timeout = max(1.0, float(task_timeout))
        self.expected_output_tokens = max(1, int(expected_output_tokens))
        self.job_history = max(10, int(job_history))
        self.persist = bool(persist)
        self.name = name

        self._handlers: dict[str, Handler] = {}
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._queue: list[tuple[int, int, str, int]] = []
        self._seq = itertools.count()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._inflight: dict[str, str] = {}          # fingerprint -> job id
        self._done_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._stopping = False
        self._stop_evt = threading.Event()
        self._llm_slots = threading.BoundedSemaphore(self.concurrency)

        self._budget_lock = threading.Lock()
        self._used_session: dict[str, int] = {}
        self._used_day: dict[str, int] = {}
        self._reserved_session: dict[str, int] = {}
        self._reserved_day: dict[str, int] = {}
        self._loaded_sessions: set[str] = set()
        self._loaded_days: set[str] = set()
        self._persist_error_at = 0.0
        self._startup_marked = False

    # ------------------------------------------------------------ 注册 / 启动
    def register(self, kind: str, handler: Handler) -> None:
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        with self._lock:
            self._handlers[kind] = handler

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stopping = False
            if self.persist and not self._startup_marked:
                self._startup_marked = True
                try:
                    n = store.mark_interrupted_llm_jobs()
                    if n:
                        logs.log_event("llm.interrupted_jobs", count=int(n),
                                       msg="上次退出时未完成的任务已标记为可重试")
                except Exception as e:
                    self._log_persist_error("标记中断任务", e)
            for i in range(self.concurrency):
                th = threading.Thread(target=self._worker,
                                      name="%s-worker-%d" % (self.name, i + 1), daemon=True)
                self._workers.append(th)
                th.start()

    # ------------------------------------------------------------ 提交 / 查询
    def fingerprint(self, kind: str, session_id: str, payload: Any) -> str:
        """指纹 = 任务类型 + 场次 + 输入 + 当前模型 + 算法版本。

        输入里必须包含所有会改变模型输出的东西（筛选条件、内容摘要、提示词版本）；
        不包含 auto/force 这类「怎么执行」的开关，否则同一份内容会被算成两次。
        """
        model = ""
        try:
            model = str(settings.resolve().get("model") or "")
        except Exception:
            pass
        raw = json.dumps({
            "v": FINGERPRINT_VERSION, "kind": kind, "session": session_id or "",
            "payload": payload, "model": model,
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def submit(self, kind: str, session_id: str, payload: dict, *,
               priority: int = PRIORITY_FINALIZE,
               fingerprint_payload: Any | None = None,
               cache_ttl: float | None = None,
               force: bool = False) -> Job:
        """入队一个任务。返回的 Job 可能是一个已经在跑/已经成功的任务。

        force=True 只跳过「成功结果缓存」，仍会加入正在跑的同指纹任务——
        用户连点两下不该产生两次调用。
        """
        self.start()
        if self._stopping:
            raise RuntimeError("LLM 调度器已关闭")
        with self._lock:
            if kind not in self._handlers:
                raise KeyError("未注册的 LLM 任务类型：%s" % kind)
        fp = self.fingerprint(kind, session_id,
                              payload if fingerprint_payload is None else fingerprint_payload)
        ttl = self.dedupe_ttl if cache_ttl is None else max(0.0, float(cache_ttl))
        now = time.time()

        with self._lock:
            if not force and ttl > 0:
                hit = self._done_cache.get(fp)
                if hit:
                    jid, expires = hit
                    old = self._jobs.get(jid)
                    if old is not None and old.status == "succeeded" and expires > now:
                        self._done_cache.move_to_end(fp)
                        return self._cached_job(old)
                    self._done_cache.pop(fp, None)

            jid = self._inflight.get(fp)
            if jid:
                old = self._jobs.get(jid)
                if old is not None and old.status in ("queued", "running"):
                    if priority < old.priority:
                        # 用户点击插到排队中的自动收尾前面：重新压入一条更高优先级，
                        # 旧队列项靠 queue_token 识别并丢弃。
                        old.priority = int(priority)
                        if old.status == "queued":
                            self._push_locked(old)
                    return old
                self._inflight.pop(fp, None)

            job = Job(id="job_" + uuid.uuid4().hex[:12], kind=kind,
                      session_id=session_id or "", fingerprint=fp,
                      payload=payload or {}, priority=int(priority), cache_ttl=ttl)
            self._jobs[job.id] = job
            self._inflight[fp] = job.id
            # 先落台账再入队：否则 worker 可能在 INSERT 之前就跑完，
            # 后续 UPDATE 命中 0 行，最后 INSERT 又把状态写回 queued。
            self._persist_create(job)
            self._push_locked(job)
            self._trim_jobs_locked()
            metrics.METRICS.inc("llm.jobs_submitted", 1, session_id=session_id or "")
        return job

    def retry(self, job_id: str) -> Job:
        """重试一个失败/取消的任务。失败任务不占指纹，重试会真正再跑一次。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                if job.status not in ("failed", "cancelled"):
                    raise ValueError("只有失败或取消的任务可以重试")
                kind, sid = job.kind, job.session_id
                payload, priority = dict(job.payload), job.priority
            else:
                kind = sid = None
                payload, priority = None, PRIORITY_FINALIZE
        if payload is None:
            if not self.persist:
                raise KeyError(job_id)
            row = store.get_llm_job(job_id)
            if not row:
                raise KeyError(job_id)
            if row.get("status") not in ("failed", "cancelled"):
                raise ValueError("只有失败或取消的任务可以重试")
            kind, sid = row["kind"], row.get("session_id") or ""
            payload = row.get("payload") or {}
            priority = int(row.get("priority") or PRIORITY_FINALIZE)
        return self.submit(kind, sid, payload, priority=priority, force=True)

    def stats_summary(self) -> dict:
        with self._lock:
            running = sum(1 for j in self._jobs.values() if j.status == "running")
            queued = sum(1 for j in self._jobs.values() if j.status == "queued")
        return {"queue": queued, "running": running, "concurrency": self.concurrency}

    def get(self, job_id: str) -> "Job | None":
        """按 id 取内存里的任务；重启前的历史任务只在台账里，返回 None。"""
        with self._lock:
            return self._jobs.get(job_id)

    def busy(self) -> bool:
        with self._lock:
            return any(j.status in ("queued", "running") for j in self._jobs.values())

    def snapshot(self, limit: int = 50, session_id: str | None = None) -> dict:
        limit = max(1, min(200, int(limit)))
        with self._lock:
            mem = [j.public() for j in self._jobs.values()
                   if not session_id or j.session_id == session_id]
        mem.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        mem = mem[:limit]

        if self.persist:
            try:
                rows = store.list_llm_jobs(limit=limit, session_id=session_id)
            except Exception as e:
                self._log_persist_error("读取任务台账", e)
                rows = []
            seen = {j["id"] for j in mem}
            for r in rows:
                if r["id"] not in seen:
                    r["cached"] = False
                    r["ms"] = 0
                    mem.append(r)
            mem.sort(key=lambda x: x.get("created_at") or "", reverse=True)
            mem = mem[:limit]

        usage: list[dict] = []
        if self.persist:
            try:
                usage = store.llm_usage_breakdown(day=_today(),
                                                  session_id=session_id or None)
            except Exception as e:
                self._log_persist_error("读取用量台账", e)

        return {"jobs": mem, "stats": self.stats_summary(),
                "budgets": self.budget_status(session_id or ""), "usage": usage}

    # ------------------------------------------------------------ 预算
    def budget_status(self, session_id: str = "") -> dict:
        day = _today()
        with self._budget_lock:
            self._load_usage_locked(session_id, day)
            used_s = self._used_session.get(session_id, 0) if session_id else 0
            res_s = self._reserved_session.get(session_id, 0) if session_id else 0
            used_d = self._used_day.get(day, 0)
            res_d = self._reserved_day.get(day, 0)

        def block(used: int, reserved: int, limit: int) -> dict:
            return {
                "used": int(used), "reserved": int(reserved), "limit": int(limit),
                "remaining": (max(0, limit - used - reserved) if limit else None),
                "unlimited": not limit,
            }

        return {"day": day,
                "session": block(used_s, res_s, self.session_budget),
                "daily": block(used_d, res_d, self.daily_budget)}

    def reserve(self, session_id: str, task: str, prompt_tokens: int,
                output_tokens: int, model: str = "") -> Reservation:
        """调用前预占预算。预占而不是只查已用，才能挡住并发请求一起冲过上限。"""
        amount = max(1, int(prompt_tokens or 0) + int(output_tokens or 0))
        day = _today()
        with self._budget_lock:
            self._load_usage_locked(session_id, day)
            if self.session_budget and session_id:
                used = self._used_session.get(session_id, 0) + \
                    self._reserved_session.get(session_id, 0)
                if used + amount > self.session_budget:
                    raise BudgetExceeded("session", used, self.session_budget,
                                         "本次预计 %d token" % amount)
                self._reserved_session[session_id] = \
                    self._reserved_session.get(session_id, 0) + amount
            if self.daily_budget:
                used = self._used_day.get(day, 0) + self._reserved_day.get(day, 0)
                if used + amount > self.daily_budget:
                    if self.session_budget and session_id:
                        self._reserved_session[session_id] = max(
                            0, self._reserved_session.get(session_id, 0) - amount)
                    raise BudgetExceeded("daily", used, self.daily_budget,
                                         "本次预计 %d token" % amount)
                self._reserved_day[day] = self._reserved_day.get(day, 0) + amount
        return Reservation(session_id=session_id or "", day=day, task=task or "llm",
                           amount=amount, model=model or "")

    def settle(self, res: Reservation, usage: dict | None) -> None:
        """调用成功：把预占换成真实用量，并落台账。"""
        u = usage or {}
        prompt = max(0, int(u.get("prompt_tokens") or 0))
        completion = max(0, int(u.get("completion_tokens") or 0))
        actual = max(0, int(u.get("total_tokens") or 0))
        if actual <= 0:
            actual = res.amount
        with self._budget_lock:
            if res.session_id:
                self._reserved_session[res.session_id] = max(
                    0, self._reserved_session.get(res.session_id, 0) - res.amount)
                self._used_session[res.session_id] = \
                    self._used_session.get(res.session_id, 0) + actual
            self._reserved_day[res.day] = max(
                0, self._reserved_day.get(res.day, 0) - res.amount)
            self._used_day[res.day] = self._used_day.get(res.day, 0) + actual
        metrics.METRICS.inc("llm.calls", 1, session_id=res.session_id)
        metrics.METRICS.inc("llm.prompt_tokens", prompt, session_id=res.session_id)
        metrics.METRICS.inc("llm.completion_tokens", completion, session_id=res.session_id)
        metrics.METRICS.inc("llm.tokens", actual, session_id=res.session_id)
        cost = (prompt / 1e6 * LLM_PRICE_INPUT_PER_MTOK
                + completion / 1e6 * LLM_PRICE_OUTPUT_PER_MTOK)
        if cost:
            metrics.METRICS.inc("llm.cost", cost, session_id=res.session_id)
        if self.persist:
            try:
                store.record_llm_usage(res.day, res.session_id, res.task,
                                       prompt, completion, actual)
            except Exception as e:
                self._log_persist_error("记录 token 用量", e)

    def release(self, res: Reservation) -> None:
        """调用失败：归还预占，不产生用量。"""
        with self._budget_lock:
            if res.session_id:
                self._reserved_session[res.session_id] = max(
                    0, self._reserved_session.get(res.session_id, 0) - res.amount)
            self._reserved_day[res.day] = max(
                0, self._reserved_day.get(res.day, 0) - res.amount)

    def _load_usage_locked(self, session_id: str, day: str) -> None:
        if session_id and session_id not in self._loaded_sessions:
            self._loaded_sessions.add(session_id)
            used = 0
            if self.persist:
                try:
                    used = store.llm_usage_total(session_id=session_id)
                except Exception as e:
                    self._log_persist_error("读取本场用量", e)
            self._used_session[session_id] = int(used or 0)
        if day not in self._loaded_days:
            self._loaded_days.add(day)
            used = 0
            if self.persist:
                try:
                    used = store.llm_usage_total(day=day)
                except Exception as e:
                    self._log_persist_error("读取今日用量", e)
            self._used_day[day] = int(used or 0)

    # ------------------------------------------------------------ 并发槽 / 等待
    @contextmanager
    def llm_slot(self, timeout: float | None = None):
        """全局模型调用并发槽。任务 worker 和直接调用（如「换个标题」）共用。"""
        ok = self._llm_slots.acquire(timeout=timeout) if timeout else self._llm_slots.acquire()
        if not ok:
            raise TimeoutError("等待模型并发槽超时")
        try:
            yield
        finally:
            self._llm_slots.release()

    def sleep(self, seconds: float) -> None:
        """可被 shutdown 立刻打断的退避等待。"""
        if seconds > 0:
            self._stop_evt.wait(seconds)

    # ------------------------------------------------------------ worker
    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stopping:
                    self._cv.wait(0.5)
                if self._stopping and not self._queue:
                    return
                if not self._queue:
                    continue
                _, _, jid, token = heapq.heappop(self._queue)
            with self._lock:
                job = self._jobs.get(jid)
                if (job is None or job.status != "queued" or job.queue_token != token):
                    continue
                job.status = "running"
                job.started_at = time.time()
                job.attempts += 1
                handler = self._handlers.get(job.kind)
            self._persist_update(job.id, status="running",
                                 started_at=_iso(job.started_at), attempts=job.attempts)

            if handler is None:
                self._finish_failed(job, RuntimeError("没有注册任务类型：" + job.kind))
                continue
            try:
                result = handler(job.session_id, job.payload)
            except Exception as e:
                self._finish_failed(job, e)
            else:
                cacheable = True
                if isinstance(result, TaskResult):
                    cacheable, result = result.cacheable, result.value
                self._finish_ok(job, result, cacheable=cacheable)

    def _finish_ok(self, job: Job, result: Any, cacheable: bool = True) -> None:
        now = time.time()
        with self._lock:
            job.status = "succeeded"
            job.result = result
            job.finished_at = now
            job.error = ""
            job.error_obj = None
            if cacheable and job.cache_ttl > 0:
                self._done_cache[job.fingerprint] = (job.id, now + job.cache_ttl)
                self._done_cache.move_to_end(job.fingerprint)
                self._trim_done_cache_locked()
            if self._inflight.get(job.fingerprint) == job.id:
                self._inflight.pop(job.fingerprint, None)
        # 先落库再唤醒等待者：否则 HTTP 线程醒来立刻查 /api/jobs 会看到 running。
        self._persist_update(job.id, status="succeeded", finished_at=_iso(now))
        metrics.METRICS.inc("llm.jobs_succeeded", 1, session_id=job.session_id)
        if job.started_at:
            metrics.METRICS.observe("llm.job_ms", max(0.0, now - job.started_at) * 1000.0,
                                    session_id=job.session_id)
        with self._lock:
            job._done.set()

    def _finish_failed(self, job: Job, e: BaseException) -> None:
        now = time.time()
        with self._lock:
            job.status = "failed"
            job.error = _err(e)
            job.error_obj = e
            job.finished_at = now
            if self._inflight.get(job.fingerprint) == job.id:
                self._inflight.pop(job.fingerprint, None)
        self._persist_update(job.id, status="failed", error=job.error,
                             finished_at=_iso(now))
        with self._lock:
            job._done.set()
        metrics.METRICS.inc("llm.jobs_failed", 1, session_id=job.session_id)
        if job.started_at:
            metrics.METRICS.observe("llm.job_ms", max(0.0, now - job.started_at) * 1000.0,
                                    session_id=job.session_id)
        logs.log_event("llm.job_failed", level="error", session_id=job.session_id,
                       job_id=job.id, kind=job.kind, error=job.error,
                       attempts=job.attempts, budget=isinstance(e, BudgetExceeded))
        if not isinstance(e, BudgetExceeded) and e.__traceback__ is not None:
            traceback.print_exc()

    # ------------------------------------------------------------ 队列辅助（持锁调用）
    def _push_locked(self, job: Job) -> None:
        job.queue_token += 1
        heapq.heappush(self._queue,
                       (int(job.priority), next(self._seq), job.id, job.queue_token))
        self._cv.notify()

    def _cached_job(self, old: Job) -> Job:
        job = Job(id=old.id, kind=old.kind, session_id=old.session_id,
                  fingerprint=old.fingerprint, payload=old.payload,
                  priority=old.priority, status="succeeded", result=old.result,
                  created_at=old.created_at, started_at=old.started_at,
                  finished_at=old.finished_at, cached=True)
        job._done.set()
        return job

    def _trim_jobs_locked(self) -> None:
        while len(self._jobs) > self.job_history:
            victim_id, victim = next(iter(self._jobs.items()))
            if victim.status in ("queued", "running"):
                # 不淘汰还在跑的；把它挪到队尾，继续找下一个已完成的。
                self._jobs.move_to_end(victim_id)
                finished = [jid for jid, j in self._jobs.items()
                            if j.status not in ("queued", "running")]
                if not finished:
                    break
                victim_id = finished[0]
                victim = self._jobs[victim_id]
            self._jobs.pop(victim_id, None)
            hit = self._done_cache.get(victim.fingerprint)
            if hit and hit[0] == victim_id:
                self._done_cache.pop(victim.fingerprint, None)

    def _trim_done_cache_locked(self) -> None:
        now = time.time()
        for fp in list(self._done_cache.keys())[:50]:
            if self._done_cache[fp][1] <= now:
                self._done_cache.pop(fp, None)
        while len(self._done_cache) > self.job_history:
            self._done_cache.popitem(last=False)

    # ------------------------------------------------------------ 台账
    def _persist_create(self, job: Job) -> None:
        if not self.persist:
            return
        try:
            store.create_llm_job({
                "id": job.id, "kind": job.kind, "session_id": job.session_id,
                "fingerprint": job.fingerprint,
                "payload_json": json.dumps(job.payload, ensure_ascii=False, default=str),
                "status": job.status, "priority": job.priority,
                "attempts": job.attempts, "error": job.error,
                "created_at": _iso(job.created_at),
                "started_at": _iso(job.started_at),
                "finished_at": _iso(job.finished_at),
                "updated_at": _iso(time.time()),
            })
        except Exception as e:
            self._log_persist_error("写入任务台账", e)

    def _persist_update(self, job_id: str, **fields: Any) -> None:
        if not self.persist:
            return
        try:
            store.update_llm_job(job_id, **fields)
        except Exception as e:
            self._log_persist_error("更新任务台账", e)

    def _log_persist_error(self, what: str, e: BaseException) -> None:
        now = time.time()
        if now - self._persist_error_at < 60:
            return
        self._persist_error_at = now
        metrics.METRICS.inc("llm.persist_errors")
        logs.log_event("llm.persist_failed", level="warn", what=what,
                       error="%s: %s" % (type(e).__name__, _err(e)))

    # ------------------------------------------------------------ 关闭
    def shutdown(self, grace: float = 0.0) -> None:
        """停止接新任务。grace 秒内等队列跑完；没跑完的排队任务标记失败、可重试。

        正在执行的任务不强行打断（线程是 daemon，进程退出时由系统收尾）；
        下次启动时它们会被 mark_interrupted_llm_jobs 标成 failed。
        """
        with self._lock:
            self._stopping = True
            self._stop_evt.set()
            self._cv.notify_all()

        deadline = time.time() + max(0.0, float(grace or 0))
        while time.time() < deadline:
            with self._lock:
                active = any(j.status in ("queued", "running")
                             for j in self._jobs.values())
            if not active:
                break
            time.sleep(0.05)

        failed: list[str] = []
        with self._lock:
            for job in self._jobs.values():
                if job.status != "queued":
                    continue
                job.status = "failed"
                job.error = "服务退出，任务未执行（可重试）"
                job.error_obj = RuntimeError(job.error)
                job.finished_at = time.time()
                if self._inflight.get(job.fingerprint) == job.id:
                    self._inflight.pop(job.fingerprint, None)
                job._done.set()
                failed.append(job.id)
        for jid in failed:
            self._persist_update(jid, status="failed", error="服务退出，任务未执行（可重试）",
                                 finished_at=_iso(time.time()))
        for th in self._workers:
            th.join(timeout=1.0)


# 进程内单例。业务层用 app/tasks.py 的 submit_* 包装，不要直接拼任务。
SCHEDULER = Scheduler()


def register(kind: str, handler: Handler) -> None:
    SCHEDULER.register(kind, handler)


def submit(kind: str, session_id: str, payload: dict, **kwargs: Any) -> Job:
    return SCHEDULER.submit(kind, session_id, payload, **kwargs)


def budget_status(session_id: str = "") -> dict:
    return SCHEDULER.budget_status(session_id)
