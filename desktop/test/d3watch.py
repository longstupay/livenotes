# -*- coding: utf-8 -*-
"""D3 外部观察器：按秒采样引擎的 RMS(level) 与字幕数，标出静音前后。

    python desktop/test/d3watch.py --seconds 80
    python desktop/test/d3watch.py --port 52282 --seconds 80

静音事件从 shell.log 里的 `room_mute ... muted=true ... is_muted=true`
读取；脚本只做观察，不依赖壳自己报成功。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
HOME = Path(os.environ.get("LIVENOTES_HOME") or (LOCALAPPDATA / "LiveNotes"))
SHELL_LOG = HOME / "logs" / "shell.log"


def api(port: int, path: str, timeout: float = 4.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def port_from_log() -> int | None:
    try:
        text = SHELL_LOG.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    ports = re.findall(r"engine ready on 127\.0\.0\.1:(\d+)", text)
    return int(ports[-1]) if ports else None


def mute_time_from_log() -> float | None:
    """返回 shell.log 中最后一次 muted=true 的 unix 时间戳。"""
    try:
        text = SHELL_LOG.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    hits = re.findall(
        r"^\[(\d+)\] room_mute room-a muted=true .*is_muted=true",
        text,
        re.MULTILINE,
    )
    return float(hits[-1]) if hits else None


def running_session(port: int) -> str | None:
    try:
        sessions = api(port, "/api/live/list").get("sessions", [])
    except Exception:
        return None
    for s in sessions:
        if s.get("running"):
            return str(s.get("id"))
    return sessions[0].get("id") if sessions else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=80)
    ap.add_argument("--port", type=int)
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    port = args.port or port_from_log()
    if not port:
        # 允许在壳启动前就把 watcher 挂上：等 engine ready 行出现。
        deadline = time.time() + 30
        while not port and time.time() < deadline:
            time.sleep(0.5)
            port = port_from_log()
    if not port:
        print("FAIL: 没有 engine port（shell.log 里没有 'engine ready on'）")
        return 1
    print(f"engine port {port}, home {HOME}")

    start = time.time()
    mute_at: float | None = None
    samples: list[dict] = []
    sid = None
    while time.time() - start < args.seconds:
        t = time.time() - start
        if mute_at is None:
            mute_at = mute_time_from_log()
        if not sid:
            sid = running_session(port)
        row = {"t": round(t, 1), "sid": sid, "level": None, "utt": None,
               "gap": None, "last": ""}
        if sid:
            try:
                st = api(port, f"/api/live/status?id={sid}")
                row.update(
                    level=st.get("level"),
                    utt=st.get("n_utterances"),
                    gap=st.get("last_chunk_ago_s"),
                    last=(st.get("last_line") or "")[:40],
                )
            except Exception as e:
                row["last"] = f"(status err {e})"
        samples.append(row)
        muted = mute_at is not None and time.time() >= mute_at
        print(
            f"[{t:6.1f}s] muted={str(muted):5s} level={row['level']} "
            f"utt={row['utt']} gap={row['gap']} {row['last']}",
            flush=True,
        )
        time.sleep(args.interval)

    print("\n--- summary ---")
    if mute_at is None:
        mute_at = mute_time_from_log()
    if mute_at is None:
        print("WARN: 没看到 is_muted=true 的 room_mute 记录")
        return 2
    mute_t = mute_at - start
    pre = [s for s in samples if s["t"] < mute_t - 1 and s["level"] is not None]
    post = [s for s in samples if s["t"] >= mute_t + 1 and s["level"] is not None]
    pre_levels = [float(s["level"]) for s in pre if s["level"] is not None]
    post_levels = [float(s["level"]) for s in post if s["level"] is not None]
    pre_utt = max((s["utt"] or 0 for s in pre), default=0)
    post_utt = max((s["utt"] or 0 for s in post), default=0)
    max_gap = max((float(s["gap"]) for s in samples if s["gap"] is not None), default=0.0)

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    print(f"mute at t≈{mute_t:.1f}s")
    print(f"level pre : mean={mean(pre_levels):.5f} max={max(pre_levels, default=0):.5f} n={len(pre_levels)}")
    print(f"level post: mean={mean(post_levels):.5f} max={max(post_levels, default=0):.5f} n={len(post_levels)}")
    print(f"utterances: pre_max={pre_utt} post_max={post_utt} (+{post_utt - pre_utt})")
    print(f"max chunk gap: {max_gap:.1f}s")

    pre_mean = mean(pre_levels)
    post_mean = mean(post_levels)
    ok = (
        bool(post_levels)
        and post_mean > 0.0005
        and post_mean > pre_mean * 0.25
        and post_utt >= pre_utt
        and max_gap < 30
    )
    print("PASS: 静音后仍有 RMS，字幕未倒退" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
