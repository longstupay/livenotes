"""M1 单窗口壳 spike：启动 debug 壳，按驱动打点截图，收集结论证据。

用法（在仓库根目录）：
    python -I desktop/test/run_m1_spike.py

前置：`desktop/build.cmd build` 已产出 target/debug/livenotes-desktop.exe。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from win_shot import capture as win_capture
except Exception:  # 截图只是证据，失败时回退到 PowerShell
    win_capture = None
EXE = ROOT / "desktop" / "src-tauri" / "target" / "debug" / "livenotes-desktop.exe"
BOOT_LOG = ROOT / "desktop" / "src-tauri" / "boot.log"
SHOTS = ROOT / "docs" / "prototypes" / "shots"
PS1 = ROOT / "desktop" / "test" / "shot_window.ps1"
APP_OUT = Path(tempfile.gettempdir()) / "livenotes-m1-app.out"
SHOT_SRC = ROOT / "desktop" / "test" / "win_shot.cs"
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")


def ensure_shot_exe() -> Path | None:
    """编译一次截图小工具；比每次 PowerShell Add-Type 快两个数量级。"""
    exe = RUN_DIR / "win_shot.exe"
    try:
        if exe.exists() and exe.stat().st_mtime >= SHOT_SRC.stat().st_mtime:
            return exe
        r = subprocess.run(
            [str(CSC), "-nologo", "-r:System.Drawing.dll", "-out:" + str(exe), str(SHOT_SRC)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and exe.exists():
            log(f"built {exe.name}")
            return exe
        log(f"shot helper build failed: {(r.stderr or r.stdout).strip()[:300]}")
    except Exception as e:
        log(f"shot helper unavailable: {e}")
    return None
# 工作区里的 exe 被沙箱禁止写 %LOCALAPPDATA%，WebView2 建不了 profile。
# 复制到 Temp 再跑；调试版仍按编译期 CARGO_MANIFEST_DIR 找仓库里的 run.py。
RUN_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Temp" / "livenotes-m1"
HOME = RUN_DIR / "home"

# 打点 -> (截图名, 截图前等待秒数)。第一个参数选 m1（remote IPC）或 m1local（本地壳）。
MODE = sys.argv[1] if len(sys.argv) > 1 else "m1"
SHOT_AT = {
    "m1: state=child-visible": ("m1-a-child-visible.png", 1.5),
    "m1: state=child-hidden": ("m1-b-child-hidden.png", 1.5),
    "m1: state=child-moved": ("m1-c-child-moved.png", 1.5),
    "m1: state=child-closed": ("m1-d-child-closed.png", 1.5),
}
if MODE == "m1local":
    SHOT_AT = {
        "m1local: state=shell-visible": ("m1local-a-shell-child.png", 1.5),
        "m1local: state=child-hidden": ("m1local-b-hidden.png", 1.5),
        "m1local: state=child-shown": ("m1local-c-shown.png", 1.5),
    }
if MODE == "m1shell":
    SHOT_AT = {
        "m1shell: state=listen": ("m1shell-a-listen.png", 1.8),
        "m1shell: state=wall": ("m1shell-b-wall.png", 1.8),
        "m1shell: state=palette": ("m1shell-c-palette.png", 1.8),
        "m1shell: state=page": ("m1shell-d-page.png", 1.8),
        "m1shell: state=workbench": ("m1shell-e-workbench.png", 1.8),
        "m1shell: state=files": ("m1shell-f-files.png", 1.8),
        "m1shell: state=people": ("m1shell-g-people.png", 1.8),
        "m1shell: state=settings": ("m1shell-h-settings.png", 1.8),
    }


def log(msg: str) -> None:
    print(f"[m1] {msg}", flush=True)


DEMO_ROOMS = [
    {
        "id": "rmdemo01", "seq": 1,
        "url": "https://live.douyin.com/123456789013",
        "resolved_url": "", "resolved_from": "",
        "name": "深夜情感连麦 · 阿哲", "state": "live", "error": "",
        "session_id": "LIVE_DEMO_MAIN", "stage": 6,
        "capture_engine": "shared", "saved": False, "has_recording": False,
    },
    {
        "id": "rmdemo02", "seq": 2,
        "url": "https://live.douyin.com/123456789014",
        "resolved_url": "", "resolved_from": "",
        "name": "职场夜话 · 小鹿", "state": "live", "error": "",
        "session_id": "LIVE_DEMO_SECOND", "stage": 6,
        "capture_engine": "direct", "saved": False, "has_recording": False,
    },
    {
        "id": "rmdemo03", "seq": 3,
        "url": "https://live.bilibili.com/10003",
        "resolved_url": "", "resolved_from": "",
        "name": "副业答疑 · 老陈", "state": "starting", "error": "",
        "session_id": "", "stage": 3, "stage_note": "正在取音频",
        "capture_engine": "per_room", "saved": False, "has_recording": False,
    },
    {
        "id": "rmdemo04", "seq": 4,
        "url": "https://live.kuaishou.com/u/demo-radio",
        "resolved_url": "", "resolved_from": "",
        "name": "声优电台", "state": "failed",
        "error": "采集浏览器被系统回收（0x80070005）。可以重试，或到设置里切换采集方式。",
        "session_id": "", "stage": 2, "failed_stage": 2,
        "capture_engine": "shared", "saved": False, "has_recording": False,
    },
    {
        "id": "rmdemo05", "seq": 5,
        "url": "https://live.douyin.com/112233445566",
        "resolved_url": "", "resolved_from": "",
        "name": "职场 / 情感连麦（演示场）", "state": "stopped", "error": "",
        "session_id": "DEMO_20261104_2000", "stage": 6,
        "capture_engine": "direct", "saved": True, "has_recording": True,
    },
]


def prepare_home() -> None:
    """给验收准备一套隔离的数据：合成房间 + 一场可浏览的演示会话。"""
    data = HOME / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "rooms.json").write_text(
        json.dumps({"rooms": DEMO_ROOMS}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.environ["LIVENOTES_DATA_DIR"] = str(data)
    # 每次从干净的库开始，截图里的场次列表才是可复现的演示数据。
    for suffix in ("", "-wal", "-shm"):
        try:
            (data / ("livenotes.db" + suffix)).unlink()
        except OSError:
            pass
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from app import seed_demo, store
        conn = store.connect()
        try:
            store.init_db(conn)
            if not store.get_session(conn, seed_demo.SESSION_ID):
                seed_demo.seed(conn)
        finally:
            conn.close()
        log(f"seeded data at {data}")
    except Exception as e:  # 数据只影响工作台截图，不该挡住壳的验收
        log(f"seed failed: {e}")


def read_new(path: Path, offset: int) -> tuple[list[str], int]:
    if not path.exists():
        return [], offset
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
        return data.decode("utf-8", "replace").splitlines(), offset + len(data)


def shot(pid: int, name: str) -> bool:
    out = SHOTS / name
    helper = ensure_shot_exe()
    if helper is not None:
        try:
            r = subprocess.run([str(helper), str(pid), str(out), "LiveNotes /"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                log(f"shot {name}: {r.stdout.strip()}")
                return True
            log(f"helper shot {name} failed: {(r.stderr or r.stdout).strip()[:200]}")
        except Exception as e:
            log(f"helper shot {name} error: {e}")
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PS1),
        "-AppPid",
        str(pid),
        "-Out",
        str(out),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        log(f"shot {name} timed out")
        return False
    if r.returncode != 0:
        log(f"shot {name} failed: {(r.stderr or r.stdout).strip()[:300]}")
        return False
    log(f"shot {name}: {r.stdout.strip()}")
    return True


def main() -> int:
    if not EXE.exists():
        log(f"missing exe: {EXE}")
        return 2
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if MODE == "m1shell":
        prepare_home()
    run_exe = RUN_DIR / EXE.name
    shutil.copy2(EXE, run_exe)
    log(f"copied exe -> {run_exe}")
    SHOTS.mkdir(parents=True, exist_ok=True)
    start_offset = BOOT_LOG.stat().st_size if BOOT_LOG.exists() else 0
    offset = start_offset

    env = os.environ.copy()
    env.update(
        LIVENOTES_SPIKE=MODE,
        LIVENOTES_AUTO_EXIT_MS="110000",
        LIVENOTES_ALLOW_MULTI="1",
        LIVENOTES_NO_BROWSER="1",
    )
    if MODE == "m1shell":
        env.update(
            LIVENOTES_HOME=str(HOME),
            LIVENOTES_NO_HELPERS="1",
            LIVENOTES_RESUME_ROOMS="1",
        )
    out = APP_OUT.open("wb")
    log(f"launch {run_exe.name}")
    proc = subprocess.Popen(
        [str(run_exe)],
        cwd=str(RUN_DIR),
        env=env,
        stdout=out,
        stderr=subprocess.STDOUT,
    )
    log(f"pid={proc.pid}")

    seen: set[str] = set()
    done = False
    deadline = time.time() + 260
    pending: tuple[str, float, str] | None = None
    while time.time() < deadline:
        lines, offset = read_new(BOOT_LOG, offset)
        for line in lines:
            if "m1:" in line or "m1local:" in line or "spike-report" in line:
                log(line.strip())
            if any(x in line for x in ("m1: done", "m1local: done", "m1shell: done")):
                done = True
            for mark, (name, delay) in SHOT_AT.items():
                if mark in line and mark not in seen:
                    seen.add(mark)
                    pending = (name, time.time() + delay, mark)
        if pending and time.time() >= pending[1] and proc.poll() is None:
            shot(proc.pid, pending[0])
            pending = None
        if done and proc.poll() is not None:
            break
        if proc.poll() is not None and not done:
            log(f"app exited early rc={proc.returncode}")
            break
        time.sleep(0.4)

    if pending and proc.poll() is None:
        shot(proc.pid, pending[0])

    if proc.poll() is None:
        log("app still running; terminating")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    out.close()

    lines, _ = read_new(BOOT_LOG, start_offset)
    tail = "\n".join(lines[-120:])
    missing = [m for m in SHOT_AT if m not in seen]
    log(f"done={done} missed={missing or 'none'} shots_dir={SHOTS}")
    if not done:
        log("--- boot.log tail ---")
        print(tail, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
