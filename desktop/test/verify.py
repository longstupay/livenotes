# -*- coding: utf-8 -*-
"""D1–D4 的外部观察器。

壳不暴露额外测试接口；这个脚本从 **壳日志 + Engine HTTP** 两侧取证，
所以它验证的是真实运行状态，而不是壳自己报的「我觉得成功了」。

    python desktop/test/verify.py d1
    python desktop/test/verify.py d2
    python desktop/test/verify.py d3
    python desktop/test/verify.py d4 --seconds 600
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
HOME = Path(os.environ.get("LIVENOTES_HOME") or (LOCALAPPDATA / "LiveNotes"))
SHELL_LOG = HOME / "logs" / "shell.log"
ENGINE_LOG = HOME / "logs" / "engine.log"
DATA_DIR = HOME / "data"


def log_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def engine_port() -> int | None:
    ports = re.findall(r"engine ready on 127\.0\.0\.1:(\d+)", log_text(SHELL_LOG))
    return int(ports[-1]) if ports else None


def api(port: int, path: str, timeout: float = 6.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        body = r.read()
        return json.loads(body.decode("utf-8"))


def api_status(port: int, path: str, timeout: float = 6.0) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status
    except Exception as e:
        return getattr(e, "code", 0) or 0


def proc_rows() -> list[dict]:
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -match "
        "'msedgewebview2|livenotes-desktop|engine.exe|python' } | "
        "Select-Object ProcessId,Name,CommandLine,WorkingSetSize | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if not out:
            return []
        rows = json.loads(out)
        return rows if isinstance(rows, list) else [rows]
    except Exception:
        return []


def mem_mb() -> float:
    """只统计这个壳的进程：壳 + 它的引擎 + 它 profile 下的 WebView2。"""
    marker = str(HOME).replace("/", "\\").lower()
    total = 0
    for row in proc_rows():
        name = str(row.get("Name") or "").lower()
        cmd = str(row.get("CommandLine") or "").lower()
        ws = int(row.get("WorkingSetSize") or 0)
        if name == "livenotes-desktop.exe":
            total += ws
        elif name == "msedgewebview2.exe" and marker in cmd:
            total += ws
        elif name == "engine.exe":
            total += ws
        elif name == "python.exe" and "run.py" in cmd and "tauri2-shell-plan-0bff19" in cmd:
            total += ws
    return round(total / 1e6, 1)


def engine_pids() -> list[int]:
    pids = []
    worktree = "tauri2-shell-plan-0bff19"
    for row in proc_rows():
        cmd = str(row.get("CommandLine") or "")
        name = str(row.get("Name") or "").lower()
        # 只认这个 worktree 的引擎，避免把仓库主目录里正在跑的 run.py --lan 算进来。
        if name == "python.exe" and "run.py" in cmd and worktree in cmd:
            pids.append(int(row["ProcessId"]))
        if name == "engine.exe":
            pids.append(int(row["ProcessId"]))
        if name == "livenotes-desktop.exe":
            pids.append(int(row["ProcessId"]))
    return pids


def webview_pids() -> list[int]:
    return [int(r["ProcessId"]) for r in proc_rows()
            if str(r.get("Name", "")).lower() == "msedgewebview2.exe"]


def d1() -> int:
    ok = True
    port = engine_port()
    print(f"shell.log : {SHELL_LOG}")
    print(f"engine.log: {ENGINE_LOG}")
    print(f"data dir  : {DATA_DIR} exists={DATA_DIR.is_dir()}")
    if not port:
        print("FAIL: shell.log 里没有 'engine ready on 127.0.0.1:PORT'")
        return 1
    print(f"engine port: {port}")
    code = api_status(port, "/api/health")
    print(f"GET /api/health -> {code}")
    ok &= code == 200
    code = api_status(port, "/")
    print(f"GET / -> {code}")
    ok &= code == 200
    pids = engine_pids()
    print(f"processes (engine run.py + shell): {pids}")
    ok &= bool(pids)
    print("engine.log tail:")
    print("\n".join(log_text(ENGINE_LOG).splitlines()[-6:]))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def d2() -> int:
    text = log_text(SHELL_LOG)
    lines = [ln for ln in text.splitlines() if "room room-" in ln]
    for ln in lines[-20:]:
        print(ln)
    a = [ln for ln in lines if "room room-a" in ln]
    b = [ln for ln in lines if "room room-b" in ln]
    a_cookie = any("demo_login=alice" in ln for ln in a)
    b_cookie = any("demo_login=alice" in ln for ln in b)
    print(f"room A reported login cookie: {a_cookie}")
    print(f"room B reported login cookie: {b_cookie}")
    print(f"webview2 processes: {len(webview_pids())}")
    ok = bool(a and b) and a_cookie and b_cookie
    print("PASS: 两个房间共用登录 profile" if ok else
          "FAIL: 还没看到 B 房间共享到 cookie（先打开 A 再打开 B）")
    return 0 if ok else 1


def d3() -> int:
    port = engine_port()
    if not port:
        print("FAIL: 没有 engine port")
        return 1
    sessions = api(port, "/api/live/list").get("sessions", [])
    if not sessions:
        print("FAIL: 没有在跑的会话（先在控制台点『建会话并打开 A』）")
        return 1
    ok = False
    for s in sessions:
        sid = s.get("id")
        print(f"session {sid}: running={s.get('running')} "
              f"n_utterances={s.get('n_utterances')} "
              f"last_chunk_ago_s={s.get('last_chunk_ago_s')} "
              f"level={s.get('level')} last_line={s.get('last_line')!r}")
        if (s.get("n_utterances") or 0) > 0:
            ok = True
        rows = api(port, f"/api/live/utterances?id={sid}&limit=5").get("rows", [])
        for r in rows[-5:]:
            print(f"  [{r.get('t_start')}] {r.get('speaker_label') or r.get('speaker_id')}: {r.get('text')}")
    print("PASS: 页面音频已进入 ASR 并产生字幕" if ok else
          "FAIL: 会话在跑但还没有字幕（检查 A 页右上角徽标、chunk 统计）")
    return 0 if ok else 1


def d4(seconds: int, interval: float, expect: int) -> int:
    port = engine_port()
    if not port:
        deadline = time.time() + 30
        while not port and time.time() < deadline:
            time.sleep(0.5)
            port = engine_port()
    if not port:
        print("FAIL: 没有 engine port")
        return 1
    start = time.time()
    max_mem = 0.0
    max_gap: dict[str, float] = {}
    stalls: list[tuple] = []
    seen: set[str] = set()
    print(f"monitoring {seconds}s, interval={interval}s, expect={expect or 'any'} sessions ...")
    try:
        while time.time() - start < seconds:
            elapsed = time.time() - start
            try:
                sessions = api(port, "/api/live/list").get("sessions", [])
            except Exception as e:
                sessions = []
                if elapsed > 60:
                    stalls.append((round(elapsed), "api", str(e)[:80]))
            mem = mem_mb()
            max_mem = max(max_mem, mem)
            parts = []
            for s in sessions:
                sid = str(s.get("id") or "")
                seen.add(sid)
                raw_gap = s.get("last_chunk_ago_s")
                if raw_gap is None:
                    gap = 0.0
                    if elapsed > 60:
                        stalls.append((round(elapsed), sid[:8], "no chunk yet"))
                else:
                    gap = float(raw_gap)
                    max_gap[sid] = max(max_gap.get(sid, 0.0), gap)
                    if gap > 60 and elapsed > 60:
                        stalls.append((round(elapsed), sid[:8], f"gap {gap:.0f}s"))
                parts.append(
                    f"{sid[:8]} gap={gap:5.1f}s utt={s.get('n_utterances')}"
                )
            print(
                f"[{elapsed:7.1f}s] mem={mem:7.1f}MB n={len(sessions)} "
                + " | ".join(parts),
                flush=True,
            )
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    print(f"\nmax shell+engine+webview2 working set: {max_mem:.1f} MB")
    for sid, gap in sorted(max_gap.items()):
        print(f"max chunk gap {sid}: {gap:.1f}s")
    print(f"sessions seen: {len(seen)}")
    print(f"stalls: {stalls[:12]}")
    ok = (
        max_mem < 3500
        and not stalls
        and (expect <= 0 or len(seen) >= expect)
    )
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["d1", "d2", "d3", "d4"])
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--expect", type=int, default=0)
    args = ap.parse_args()
    if args.which == "d1":
        return d1()
    if args.which == "d2":
        return d2()
    if args.which == "d3":
        return d3()
    return d4(args.seconds, args.interval, args.expect)


if __name__ == "__main__":
    raise SystemExit(main())
