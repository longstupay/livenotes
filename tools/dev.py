# -*- coding: utf-8 -*-
"""开发用热重启：改了 app/*.py 自动重启，但绝不打断正在进行的实时采集。

    python run.py --reload      # 推荐：等价于 python tools/dev.py
    python tools/dev.py

为什么不是 importlib.reload：实时链路里有一个常驻 worker 线程和一个已经打开的
VAD/ONNX 会话，reload 出来的新模块管不到这些旧对象，容易出现"代码看着新、行为还是旧"，
比直接重启更难查。

为什么不无脑重启：正在采集的场次一杀进程就断了（音频在内存队列里、spool 只写了一半）。
所以这里先问一下 /api/live/info 的 active 列表：
  - 空闲 -> 立刻重启，1 秒左右回来；
  - 在录 -> 记一条"待重启"，等这场结束的第一次空闲再自动换代码，期间照常录音。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCH = (ROOT / "app", ROOT / "tools", ROOT / "run.py")
PORT = int(os.environ.get("LIVENOTES_PORT", "8765"))
HOST = os.environ.get("LIVENOTES_HOST", "127.0.0.1")
BASE = "http://%s:%d" % (HOST, PORT)
POLL_S = 0.8
READY_TIMEOUT = 40.0


def _snapshot() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for base in WATCH:
        if base.is_file():
            st = base.stat()
            out[str(base)] = (st.st_mtime_ns, st.st_size)
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


def _get(path: str, timeout: float = 1.5):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _service_state():
    """返回 {active, jobs}；探测失败返回 None（未知），绝不返回空状态。

    为什么不能把异常当成"没在录"：服务忙着跑 ASR / 长模型请求时，1.5 秒的探针很容易超时，
    一旦误判成空闲就重启，正在处理的请求会被当场掐断（前端看到的就是 Failed to fetch），
    录制也会断。宁可多等一轮再问。

    jobs 是全局 LLM 调度器里排队/执行中的任务数：下播收尾还在跑时也不该换代码，
    否则那场总结会被标成失败。
    """
    try:
        d = _get("/api/live/info", timeout=5.0)
    except Exception:
        return None
    llm_jobs = d.get("llm_jobs") or {}
    return {
        "active": list(d.get("active") or []),
        "jobs": int(llm_jobs.get("queue") or 0) + int(llm_jobs.get("running") or 0),
    }


def _port_busy() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0
    finally:
        s.close()


def _spawn(open_browser: bool) -> subprocess.Popen:
    env = os.environ.copy()
    suppressed = os.environ.get("LIVENOTES_NO_BROWSER", "").lower() in ("1", "true", "yes")
    env["LIVENOTES_NO_BROWSER"] = "0" if (open_browser and not suppressed) else "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen([sys.executable, str(ROOT / "run.py")], cwd=str(ROOT), env=env)


def _wait_ready(child: subprocess.Popen) -> bool:
    end = time.time() + READY_TIMEOUT
    while time.time() < end:
        if child.poll() is not None:
            return False
        try:
            if _get("/api/health", timeout=1.0).get("ok"):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _stop(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def _restart(child: subprocess.Popen, changed: list[str]) -> subprocess.Popen:
    rel = [str(Path(c).relative_to(ROOT)) if str(c).startswith(str(ROOT)) else c
           for c in changed]
    shown = "、".join(rel[:3]) + ("…" if len(rel) > 3 else "")
    print("\n[dev] 检测到改动：%s -> 重启服务" % shown, flush=True)
    _stop(child)
    # 端口等一小会儿再绑，避免上一进程的 socket 还没完全释放
    for _ in range(20):
        if not _port_busy():
            break
        time.sleep(0.1)
    new = _spawn(open_browser=False)
    if _wait_ready(new):
        print("[dev] 已重启（pid %d）%s" % (new.pid, BASE + "/"), flush=True)
    else:
        print("[dev] 新进程没能起来，翻上面的报错", flush=True)
    return new


def main() -> int:
    if _port_busy():
        print("[dev] 端口 %d 已被占用，说明已经有一个服务在跑。先关掉它再执行。"
              % PORT, flush=True)
        return 2
    print("[dev] 启动 %s（改 app/*.py 会自动重启；正在采集时不会打断）" % BASE, flush=True)
    child = _spawn(open_browser=True)
    if not _wait_ready(child):
        print("[dev] 首次启动失败，翻上面的报错", flush=True)
        _stop(child)
        return 1
    print("[dev] 就绪（pid %d），监听 app/ tools/ run.py 的 .py 改动" % child.pid, flush=True)

    snap = _snapshot()
    pending: list[str] | None = None
    waiting_note = False
    dead_noted = False
    try:
        while True:
            time.sleep(POLL_S)
            fresh = _snapshot()
            changed = [p for p in fresh if snap.get(p) != fresh[p]]
            if changed:
                snap = fresh
                pending = (pending or []) + changed
                waiting_note = False

            # 进程没了：可能是刚才重启时新代码起不来（语法错/端口没释放），
            # 也可能是运行时崩了。不要退出——保存下一次修改时自动再拉起来。
            if child.poll() is not None:
                if not dead_noted:
                    print("[dev] 服务进程已退出（code=%s）。改完再保存一次就会自动重试启动。"
                          % child.returncode, flush=True)
                    dead_noted = True
                if pending:
                    child = _restart(child, pending)
                    snap, pending, waiting_note = _snapshot(), None, False
                    dead_noted = child.poll() is not None
                continue

            if not pending:
                continue
            state = _service_state()
            if state is None:
                if not waiting_note:
                    print("[dev] 暂时探测不到服务状态（可能正在处理长请求），先不重启。"
                          "代码改动会保留，等能探测到再说。", flush=True)
                    waiting_note = True
                continue
            if state["active"]:
                if not waiting_note:
                    print("[dev] 代码已更新，但 %s 正在采集中；等这场结束再自动重启。"
                          % "、".join(state["active"]), flush=True)
                    waiting_note = True
                continue
            if state["jobs"]:
                if not waiting_note:
                    print("[dev] 代码已更新，但还有 %d 个 LLM 任务在排队/执行；等它们结束再重启。"
                          % state["jobs"], flush=True)
                    waiting_note = True
                continue
            child = _restart(child, pending)
            snap, pending, waiting_note = _snapshot(), None, False
            dead_noted = child.poll() is not None
    except KeyboardInterrupt:
        print("\n[dev] 收到 Ctrl+C，正在关闭服务…", flush=True)
        return 0
    finally:
        _stop(child)


if __name__ == "__main__":
    sys.exit(main())
