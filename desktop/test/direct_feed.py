# -*- coding: utf-8 -*-
"""D4 的 2 路「direct」输入：不经过 WebView2，直接向 Engine 推 16k 单声道 PCM。

    python desktop/test/direct_feed.py --title "D4 direct 1" --seconds 3600

引擎的 /api/live/chunk 只认裸 PCM + X-Live-Id；这里把 speech.wav
（22.05k 单声道）线性重采样到 16k，按 4096 样本（256ms）实时节奏发送。
"""
from __future__ import annotations

import argparse
import array
import json
import os
import re
import sys
import time
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
HOME = Path(os.environ.get("LIVENOTES_HOME") or (LOCALAPPDATA / "LiveNotes"))
SHELL_LOG = HOME / "logs" / "shell.log"

TARGET = 16000
CHUNK = 4096


def port_from_log() -> int | None:
    try:
        text = SHELL_LOG.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    ports = re.findall(r"engine ready on 127\.0\.0\.1:(\d+)", text)
    return int(ports[-1]) if ports else None


def post(port: int, path: str, body: bytes, headers: dict, timeout: float = 8.0):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def load_16k(path: Path) -> array.array:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1, "需要单声道 wav"
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    src = array.array("h")
    src.frombytes(raw)
    if sys.byteorder == "big":
        src.byteswap()
    if rate == TARGET:
        return src
    n = int(len(src) * TARGET / rate)
    out = array.array("h", [0]) * n
    for i in range(n):
        out[i] = src[int(i * rate / TARGET)]
    print(f"[direct] resample {rate} -> {TARGET}, {len(src)} -> {n} samples", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="D4 direct")
    ap.add_argument("--seconds", type=float, default=3600)
    ap.add_argument("--port", type=int)
    ap.add_argument("--file", default=str(HERE / "speech.wav"))
    args = ap.parse_args()

    port = args.port or port_from_log()
    if not port:
        deadline = time.time() + 30
        while not port and time.time() < deadline:
            time.sleep(0.5)
            port = port_from_log()
    if not port:
        print("FAIL: no engine port", flush=True)
        return 1

    samples = load_16k(Path(args.file))
    if not samples:
        print("FAIL: empty wav", flush=True)
        return 1

    status, body = post(
        port,
        "/api/live/start",
        json.dumps({"title": args.title, "source_mode": "tab"}).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    sid = json.loads(body.decode("utf-8")).get("session_id", "")
    if not sid:
        print(f"FAIL: live/start {status} {body[:200]!r}", flush=True)
        return 1
    print(f"[direct] {args.title} session={sid} port={port}", flush=True)

    start = time.time()
    next_t = start
    idx = 0
    sent = 0
    errors = 0
    try:
        while time.time() - start < args.seconds:
            if idx + CHUNK > len(samples):
                idx = 0
            block = samples[idx: idx + CHUNK]
            idx += CHUNK
            if len(block) < CHUNK:
                block = array.array("h", list(block) + [0] * (CHUNK - len(block)))
            try:
                post(
                    port,
                    "/api/live/chunk",
                    block.tobytes(),
                    {
                        "Content-Type": "application/octet-stream",
                        "X-Live-Id": sid,
                    },
                )
                sent += 1
            except Exception as e:
                errors += 1
                if errors <= 5:
                    print(f"[direct] chunk error: {e}", flush=True)
            next_t += CHUNK / TARGET
            delay = next_t - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.time()
            if sent and sent % 240 == 0:
                el = time.time() - start
                print(
                    f"[direct] {args.title} sent={sent} errors={errors} "
                    f"elapsed={el:.0f}s",
                    flush=True,
                )
    except KeyboardInterrupt:
        pass

    try:
        post(
            port,
            "/api/live/stop",
            json.dumps({"session_id": sid}).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
    except Exception as e:
        print(f"[direct] stop error: {e}", flush=True)
    print(f"[direct] {args.title} done sent={sent} errors={errors}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
