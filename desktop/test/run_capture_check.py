"""后台采集冒烟检查：browser 采集模式下，待启动房间不应再弹出第二个窗口。

做法：用隔离数据目录放一条 pending 房间（capture_mode=browser），启动壳，
等 room watcher 把它交给应用内浏览器，然后枚举该进程的可见顶层窗口。
预期：只有主窗口 "LiveNotes /"，采集控制台窗口存在但不可见。

用法（仓库根目录）：
    python -I desktop/test/run_capture_check.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop" / "src-tauri" / "target" / "debug" / "livenotes-desktop.exe"
RUN_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Temp" / "livenotes-capture"
HOME = RUN_DIR / "home"
DATA = HOME / "data"

ROOM = {
    "id": "rmcapture01", "seq": 1,
    "url": "https://live.douyin.com/123456789013",
    "resolved_url": "", "resolved_from": "",
    "name": "后台采集冒烟房间", "state": "pending", "error": "",
    "session_id": "", "stage": 1,
    "capture_engine": "shared", "saved": False, "has_recording": False,
}


def log(msg: str) -> None:
    print(f"[capture] {msg}", flush=True)


def prepare() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "rooms.json").write_text(
        json.dumps({"rooms": [ROOM]}, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA / "settings.json").write_text(json.dumps({
        "version": 2, "active": "", "profiles": {},
        "prefs": {"capture_mode": "browser"},
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def visible_windows(pid: int) -> list[tuple[str, int, int]]:
    """该进程的可见顶层窗口：标题 + 宽高。"""
    user32 = ctypes.windll.user32
    out: list[tuple[str, int, int]] = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 2)
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        out.append((buf.value, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(CB(cb), 0)
    return out


def main() -> int:
    if not EXE.exists():
        log(f"missing exe: {EXE}")
        return 2
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    prepare()
    run_exe = RUN_DIR / EXE.name
    shutil.copy2(EXE, run_exe)
    shell_log = HOME / "logs" / "shell.log"
    start = shell_log.stat().st_size if shell_log.exists() else 0

    env = os.environ.copy()
    env.update({
        "LIVENOTES_HOME": str(HOME),
        "LIVENOTES_DATA_DIR": str(DATA),
        "LIVENOTES_NO_HELPERS": "1",
        "LIVENOTES_RESUME_ROOMS": "1",
        "LIVENOTES_ALLOW_MULTI": "1",
        "LIVENOTES_AUTO_EXIT_MS": "90000",
    })
    out = (RUN_DIR / "app.out").open("wb")
    log(f"launch {run_exe.name}")
    proc = subprocess.Popen([str(run_exe)], cwd=str(RUN_DIR), env=env,
                            stdout=out, stderr=subprocess.STDOUT)
    log(f"pid={proc.pid}")

    deadline = time.time() + 60
    handed = False
    while time.time() < deadline and proc.poll() is None:
        if shell_log.exists():
            with shell_log.open("rb") as f:
                f.seek(start)
                text = f.read().decode("utf-8", "replace")
            for line in text.splitlines():
                if "room watcher: rmcapture01 -> desktop tab" in line:
                    log(line.strip())
                    handed = True
                    break
        if handed:
            break
        time.sleep(0.5)
    time.sleep(3)

    wins = visible_windows(proc.pid)
    for title, w, h in wins:
        log(f"visible window: {title!r} {w}x{h}")
    ok = handed and len(wins) == 1 and "LiveNotes" in wins[0][0]
    log(f"handed={handed} visible={len(wins)} ok={ok}")

    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, text=True)
    out.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
