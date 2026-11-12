# -*- coding: utf-8 -*-
"""D5：验证打包产物不依赖 Python，并能真正跑 ASR。

两种模式：

    # 1) 整个安装目录（壳 + resources/engine + models）
    python desktop/test/verify_packaged.py --install-dir "C:\\path\\to\\d5install"

    # 2) 只验 PyInstaller 的 engine.exe（还没有 NSIS 包时）
    python desktop/test/verify_packaged.py --engine "desktop/src-tauri/resources/engine/engine.exe"

脚本自己起进程、自己推 16k PCM、自己查 /api/live/utterances，不依赖 GUI 点击。
"""
from __future__ import annotations

import argparse
import array
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(port: int, path: str, timeout: float = 6.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.status, r.read()


def post(port: int, path: str, body: bytes, headers: dict, timeout: float = 10.0):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def wait_health(port: int, seconds: float = 90.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            code, _ = get(port, "/api/health", 2.0)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def log_tail(path: Path, n: int = 20) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except FileNotFoundError:
        return ""


def load_16k() -> array.array:
    path = HERE / "speech.wav"
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    src = array.array("h")
    src.frombytes(raw)
    if sys.byteorder == "big":
        src.byteswap()
    if rate == 16000:
        return src
    n = int(len(src) * 16000 / rate)
    out = array.array("h", [0]) * n
    for i in range(n):
        out[i] = src[int(i * rate / 16000)]
    return out


def feed_and_check(port: int, seconds: float = 12.0) -> tuple[str, int, list[str]]:
    """推一段真实语音，返回 (session_id, utterance 数, 文本)。"""
    samples = load_16k()
    code, body = post(
        port,
        "/api/live/start",
        json.dumps({"title": "D5 packaged", "source_mode": "tab"}).encode(),
        {"Content-Type": "application/json"},
    )
    if code not in (200, 201):
        raise SystemExit(f"live/start HTTP {code}: {body[:200]!r}")
    sid = json.loads(body).get("session_id", "")
    if not sid:
        raise SystemExit(f"live/start 没有 session_id: {body[:200]!r}")
    chunk = 4096
    idx = 0
    start = time.time()
    next_t = start
    while time.time() - start < seconds:
        if idx + chunk > len(samples):
            idx = 0
        block = samples[idx: idx + chunk]
        idx += chunk
        post(
            port,
            "/api/live/chunk",
            block.tobytes(),
            {"Content-Type": "application/octet-stream", "X-Live-Id": sid},
        )
        next_t += chunk / 16000
        delay = next_t - time.time()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.time()
    post(
        port,
        "/api/live/stop",
        json.dumps({"session_id": sid}).encode(),
        {"Content-Type": "application/json"},
    )
    time.sleep(4)
    _, body = get(port, f"/api/live/utterances?id={sid}&limit=50")
    rows = json.loads(body).get("rows", [])
    texts = [str(r.get("text") or "") for r in rows]
    return sid, len(rows), texts


def proc_rows() -> list[dict]:
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -match "
        "'livenotes|engine\\.exe|python|msedgewebview2' } | "
        "Select-Object ProcessId,Name,CommandLine,ExecutablePath | ConvertTo-Json -Compress"
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


def python_in_tree(marker: str) -> list[str]:
    """只看「壳拉起的 Python 引擎」（python.exe run.py），不看本脚本自己。"""
    bad = []
    marker = marker.lower()
    for r in proc_rows():
        name = str(r.get("Name") or "").lower()
        cmd = str(r.get("CommandLine") or "").lower()
        exe = str(r.get("ExecutablePath") or "").lower()
        if name == "python.exe" and "run.py" in cmd and (marker in cmd or marker in exe):
            bad.append(f"{r.get('ProcessId')} {r.get('CommandLine')}")
    return bad


def engine_procs() -> list[dict]:
    return [r for r in proc_rows() if str(r.get("Name") or "").lower() == "engine.exe"]


def run_engine_only(exe: Path, home: Path) -> int:
    home.mkdir(parents=True, exist_ok=True)
    data = home / "data"
    tmp = data / "tmp"
    (home / "logs").mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    port = free_port()
    models = exe.parent.parent / "models"
    env = os.environ.copy()
    env.update({
        "LIVENOTES_PORT": str(port),
        "LIVENOTES_HOST": "127.0.0.1",
        "LIVENOTES_NO_BROWSER": "1",
        "LIVENOTES_DATA_DIR": str(data),
        "LIVENOTES_MODELS_DIR": str(models),
        "TMP": str(tmp),
        "TEMP": str(tmp),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    log = (home / "logs" / "engine.out.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(exe)], cwd=str(exe.parent), env=env,
        stdout=log, stderr=subprocess.STDOUT, creationflags=0x08000000,
    )
    ok = False
    try:
        if not wait_health(port):
            print("FAIL: engine.exe 90s 内没有 /api/health 200")
            print(log_tail(home / "logs" / "engine.out.log", 40))
            return 1
        print(f"engine.exe health OK on {port}")
        code, _ = get(port, "/")
        print(f"GET / -> {code}")
        sid, n, texts = feed_and_check(port)
        print(f"packaged ASR: session={sid} utterances={n}")
        for t in texts[:5]:
            print(f"  {t}")
        python = python_in_tree(str(home))
        print(f"python processes in test tree: {python or 'none'}")
        ok = code == 200 and n > 0 and not python
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def run_install(install: Path, seconds: int) -> int:
    exe = install / "LiveNotes.exe"
    if not exe.is_file():
        exe = install / "livenotes-desktop.exe"
    if not exe.is_file():
        cands = list(install.glob("*.exe"))
        if not cands:
            print(f"FAIL: {install} 下没有 exe")
            return 1
        exe = cands[0]
    engine = install / "resources" / "engine" / "engine.exe"
    models = install / "resources" / "models"
    print(f"shell  : {exe}")
    print(f"engine : {engine} exists={engine.is_file()}")
    print(f"models : {models} exists={models.is_dir()}")
    if not engine.is_file():
        print("FAIL: 安装目录里没有 resources/engine/engine.exe")
        return 1

    # 不设 LIVENOTES_HOME，改把 LOCALAPPDATA 指到测试目录：这样走的是生产默认
    # 路径 `%LOCALAPPDATA%\LiveNotes`，验证的是真实数据目录公式。
    base = REPO / "desktop" / ".build" / "d5local"
    if base.exists():
        import shutil
        shutil.rmtree(base)
    base.mkdir(parents=True)
    home = base / "LiveNotes"
    env = os.environ.copy()
    for key in ("LIVENOTES_ENGINE_EXE", "LIVENOTES_SPIKE", "LIVENOTES_PYTHON",
                "LIVENOTES_HOME", "LIVENOTES_WEBVIEW_DIR"):
        env.pop(key, None)
    env.update({
        "LOCALAPPDATA": str(base),
        "LIVENOTES_AUTO_EXIT_MS": str((seconds + 25) * 1000),
        # 近似「没装 Python 的干净机器」：子进程 PATH 只留系统目录，
        # 并清掉 PYTHONHOME/PYTHONPATH。引擎仍应正常起来（PyInstaller 自带运行时）。
        "PATH": os.pathsep.join([
            str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "system32"),
            os.environ.get("SystemRoot", r"C:\Windows"),
        ]),
    })
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    logp = home / "logs" / "shell.log"
    proc = subprocess.Popen([str(exe)], cwd=str(install), env=env)
    try:
        deadline = time.time() + 120
        port = 0
        while time.time() < deadline:
            try:
                m = re.findall(r"engine ready on 127\.0\.0\.1:(\d+)",
                               logp.read_text(encoding="utf-8", errors="replace"))
                if m:
                    port = int(m[-1])
                    break
            except FileNotFoundError:
                pass
            if proc.poll() is not None:
                print("FAIL: 壳提前退出")
                print(log_tail(logp, 40))
                return 1
            time.sleep(0.5)
        if not port:
            print("FAIL: 120s 内 shell.log 没有 engine ready")
            print(log_tail(logp, 40))
            return 1
        print(f"shell started engine on 127.0.0.1:{port}")
        if not wait_health(port, 20):
            print("FAIL: /api/health 不是 200")
            return 1
        eng = engine_procs()
        print(f"engine.exe processes: {len(eng)}")
        for r in eng[:3]:
            print(f"  {r.get('ExecutablePath')}")
        python = python_in_tree(str(home)) + python_in_tree(str(install))
        print(f"python processes in test tree: {python or 'none'}")
        code, _ = get(port, "/")
        sid, n, texts = feed_and_check(port)
        print(f"packaged ASR: session={sid} utterances={n}")
        for t in texts[:5]:
            print(f"  {t}")
        db = home / "data" / "livenotes.db"
        print(f"data dir: {home / 'data'} db={db.is_file()} "
              f"logs={(home / 'logs').is_dir()}")
        ok = code == 200 and n > 0 and not python and db.is_file()
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        try:
            proc.wait(timeout=seconds + 40)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(3)
        left = engine_procs()
        print(f"engine.exe after shell exit: {len(left)}")
        if left:
            print("FAIL: 壳退出后 engine.exe 还在")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-dir")
    ap.add_argument("--engine")
    ap.add_argument("--seconds", type=int, default=30)
    args = ap.parse_args()
    if args.install_dir:
        return run_install(Path(args.install_dir).resolve(), args.seconds)
    if args.engine:
        return run_engine_only(Path(args.engine).resolve(),
                               REPO / "desktop" / ".build" / "d5engine")
    ap.error("需要 --install-dir 或 --engine")


if __name__ == "__main__":
    raise SystemExit(main())
