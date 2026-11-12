# -*- coding: utf-8 -*-
"""OpenAI 兼容接口的最小客户端：一次 chat、测试连通性、拉模型列表。

所有业务调用都经过全局调度器（app/scheduler.py）：
  - 并发上限：同时最多 N 个请求，10 场一起下播不会一起打出去；
  - 指数退避：429 / 5xx / 网络错误自动重试，401 / 400 / 解析错误不重试；
  - token 预算：调用前按 prompt 估算预占，返回后按 usage 结算；
  - 用量台账：按天 + 场次 + 任务落库，重启不清零。
探测类接口（测试连接 / 拉模型列表 / 逐个试）不占预算、不排队，
它们本来就是用来排查配置的，不该被预算挡住。
"""
from __future__ import annotations

import json
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import logs, metrics
from . import scheduler as _scheduler
from . import settings


# 对外的异常基类。保留 RuntimeError 继承，老的 except 分支不用改。
class LLMError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = bool(retryable)
        self.retry_after = retry_after


# 预算不足不是"模型坏了"：调用方应回退本地抽取式，而不是重试。
BudgetExceeded = _scheduler.BudgetExceeded

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def estimate_tokens(text: str) -> int:
    """粗估 token：中文按 ~1 token/字，英文按 ~3.2 字符/token。宁高勿低。"""
    text = text or ""
    cjk = len(_CJK_RE.findall(text))
    other = max(0, len(text) - cjk)
    return int(cjk * 1.05 + other / 3.2) + 1


def estimate_messages(messages: list[dict]) -> int:
    n = 3
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str):
            n += estimate_tokens(content) + 4
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    n += estimate_tokens(part["text"]) + 4
    return n


def _retry_delay(attempt: int, retry_after: float | None = None,
                 base: float | None = None, cap: float | None = None) -> float:
    """第 attempt 次失败后的等待：base * 2^(n-1)，等抖动，封顶 cap。

    服务端给了 Retry-After 就优先听它的（仍受 cap 约束）。
    """
    base = _scheduler.SCHEDULER.backoff_base if base is None else max(0.0, base)
    cap = _scheduler.SCHEDULER.backoff_max if cap is None else max(0.0, cap)
    if retry_after and retry_after > 0:
        return min(cap, retry_after) if cap > 0 else retry_after
    if base <= 0:
        return 0.0
    raw = base * (2 ** max(0, attempt - 1))
    if cap > 0:
        raw = min(cap, raw)
    # equal jitter：一半固定、一半随机，避免多个任务同一秒一起重试
    return raw * (0.5 + random.random() * 0.5)


def _http_error(e: urllib.error.HTTPError) -> LLMError:
    detail = ""
    try:
        detail = e.read().decode("utf-8", "ignore")[:200]
    except Exception:
        pass
    retryable = e.code in (408, 409, 425, 429) or 500 <= e.code < 600
    retry_after = None
    try:
        raw = e.headers.get("Retry-After") if e.headers else None
        if raw:
            retry_after = float(raw)
    except (TypeError, ValueError, AttributeError):
        retry_after = None
    return LLMError("HTTP %s %s" % (e.code, detail), status=e.code,
                    retryable=retryable, retry_after=retry_after)


def _post_chat(cfg: dict, payload: dict, timeout: int,
               total_timeout: float | None = None) -> dict:
    """真正发请求。单独拆出来，测试可以替换它，不必碰网络。

    total_timeout 是**整次调用的硬上限**。只设 socket timeout 是不够的：
    中转站只要一直慢吞吞地吐字节，urlopen 就永远等不到超时。到点直接放弃，
    把线程丢在后台（daemon），让调用方按可重试错误处理。
    """
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
        headers=_headers(cfg["key"]))
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                box["body"] = json.loads(resp.read().decode("utf-8"))
        except BaseException as e:          # 线程里抛什么都要带回主线程
            box["err"] = e

    limit = float(total_timeout or cfg.get("total_timeout") or 75)
    th = threading.Thread(target=_run, name="llm-chat", daemon=True)
    th.start()
    th.join(limit)
    if th.is_alive():
        raise LLMError("模型响应超时（%.0f 秒未返回，已放弃这次调用）" % limit,
                       retryable=True)

    err = box.get("err")
    if isinstance(err, urllib.error.HTTPError):
        raise _http_error(err) from err
    if isinstance(err, (urllib.error.URLError, TimeoutError, socket.timeout, OSError)):
        raise LLMError("网络错误：%s" % (str(err) or type(err).__name__),
                       retryable=True) from err
    if isinstance(err, ValueError):
        # 200 但返回 HTML/半截 JSON：多半是中转站抽风，但重试会再计一次费，不自动重试
        raise LLMError("模型返回不是合法 JSON：%s" % str(err)[:120],
                       retryable=False) from err
    if err is not None:
        raise LLMError(str(err) or type(err).__name__, retryable=False) from err

    body = box.get("body")
    if not isinstance(body, dict):
        raise LLMError("返回格式不对：不是 JSON 对象（%s）" % type(body).__name__,
                       retryable=False)
    return body


def _usage_from(body: dict, text: str, prompt_est: int) -> dict:
    got: dict[str, int] = {}
    u = body.get("usage") if isinstance(body, dict) else None
    if isinstance(u, dict):
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = u.get(k)
            if isinstance(v, int) and v >= 0:
                got[k] = v
    if not got.get("total_tokens"):
        p = got.get("prompt_tokens") or prompt_est
        c = got.get("completion_tokens")
        if c is None:
            c = estimate_tokens(text or "")
        got.update(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)
    else:
        got.setdefault("prompt_tokens", prompt_est)
        got.setdefault("completion_tokens",
                       max(0, got["total_tokens"] - got["prompt_tokens"]))
    return got


def _headers(key: str) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = "Bearer " + key
    return h


def chat(messages: list[dict], temperature: float | None = None,
         timeout: int | None = None, max_tokens: int | None = None,
         overrides: dict | None = None, usage: dict | None = None,
         total_timeout: float | None = None, *,
         session_id: str = "", task: str = "",
         max_attempts: int | None = None) -> str:
    """返回第一条回复的正文。任何失败都抛 RuntimeError（LLMError），调用方负责降级。

    session_id/task 用于预算与台账：同一个场次的总结、分类、润色会共享单场预算。
    overrides 用于"表单填了但还没保存"时的一次性测试。
    """
    cfg = settings.resolve(overrides)
    if not cfg["configured"]:
        raise LLMError("大模型未配置")
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "temperature": cfg["temperature"] if temperature is None else temperature,
        "messages": messages,
        "stream": False,          # 有些中转站默认走流式，非流式解析会直接 502
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens

    prompt_est = estimate_messages(messages)
    output_est = int(max_tokens) if max_tokens else _scheduler.SCHEDULER.expected_output_tokens
    # 预占失败会直接抛 BudgetExceeded，不会发出请求。
    res = _scheduler.SCHEDULER.reserve(session_id, task or "llm",
                                       prompt_est, output_est, model=cfg["model"])
    attempts = max(1, int(max_attempts or _scheduler.SCHEDULER.max_attempts))
    last: LLMError | None = None
    try:
        for attempt in range(1, attempts + 1):
            try:
                with _scheduler.SCHEDULER.llm_slot(
                        timeout=_scheduler.SCHEDULER.task_timeout):
                    body = _post_chat(cfg, payload, timeout or cfg["timeout"],
                                      total_timeout)
                try:
                    text = body["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    detail = body.get("error") if isinstance(body, dict) else None
                    raise LLMError("模型返回格式不对：%s" % str(detail or body)[:160],
                                   retryable=False) from e
                got = _usage_from(body, text or "", prompt_est)
                if usage is not None:
                    usage.update(got)
                _scheduler.SCHEDULER.settle(res, got)
                return text
            except LLMError as e:
                last = e
                if not e.retryable or attempt >= attempts:
                    break
                delay = _retry_delay(attempt, e.retry_after)
                metrics.METRICS.inc("llm.retries", 1, session_id=session_id or "")
                logs.log_event("llm.retry", level="warn", session_id=session_id or "",
                               attempt=attempt, attempts=attempts, delay_s=round(delay, 2),
                               error=str(e))
                _scheduler.SCHEDULER.sleep(delay)
        if last is None:
            last = LLMError("大模型调用失败")
        raise last
    except BaseException:
        # 成功时 settle 已经归还预占；走到这里说明一次都没成功。
        _scheduler.SCHEDULER.release(res)
        raise


def _probe_one(cfg: dict, model: str, timeout: int = 15) -> dict[str, Any]:
    """最小请求：不带 max_tokens（有些供应商会因此拒绝），只问一句 ping。"""
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}],
               "temperature": 0, "stream": False}
    t0 = time.time()
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
        headers=_headers(cfg["key"]))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"]
        return {"model": model, "ok": True, "ms": int((time.time() - t0) * 1000),
                "reply": (text or "").strip()[:30]}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        return {"model": model, "ok": False, "ms": int((time.time() - t0) * 1000),
                "error": "HTTP %s %s" % (e.code, detail)}
    except Exception as e:
        return {"model": model, "ok": False, "ms": int((time.time() - t0) * 1000),
                "error": str(e) or type(e).__name__}


def test_connection(overrides: dict | None = None) -> dict[str, Any]:
    cfg = settings.resolve(overrides)
    missing = [n for n, k in (("接口地址", "base"), ("模型", "model"), ("API Key", "key"))
               if not cfg[k]]
    if missing:
        return {"ok": False, "error": "还差：" + "、".join(missing)}
    r = _probe_one(cfg, cfg["model"])
    r["error"] = r.get("error") or ""
    return {"ok": r["ok"], "ms": r["ms"], "model": r["model"],
            "reply": r.get("reply", ""), "error": r.get("error") or None}


def probe_many(overrides: dict | None, models: list[str], limit: int = 12) -> dict[str, Any]:
    """逐个模型试一遍。中转站的模型列表常常一半是死的，这正是给它们用的。"""
    cfg = settings.resolve(overrides)
    if not cfg["base"]:
        return {"ok": False, "error": "先填接口地址", "results": []}
    if not cfg["key"]:
        return {"ok": False, "error": "先填 API Key", "results": []}
    todo = [m for m in models if m][:limit]
    if not todo:
        return {"ok": False, "error": "没有可试的模型", "results": []}
    results = [_probe_one(cfg, m, timeout=12) for m in todo]
    return {"ok": True, "results": results}


def list_models(overrides: dict | None = None) -> dict[str, Any]:
    cfg = settings.resolve(overrides)
    if not cfg["base"]:
        return {"ok": False, "error": "先填接口地址"}
    try:
        req = urllib.request.Request(cfg["base"] + "/models", headers=_headers(cfg["key"]))
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        items = [str(m.get("id") or "") for m in (body.get("data") or [])]
        return {"ok": True, "models": [m for m in items if m][:200]}
    except Exception as e:            # 拉列表不是必须的，失败就退化成手填
        return {"ok": False, "error": str(e)}
