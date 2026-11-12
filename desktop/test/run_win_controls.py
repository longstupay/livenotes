"""自绘标题栏验收：最大化 / 还原 / 最小化 / 关闭四个按钮真的作用到窗口。

无边框窗口没有系统标题条，窗口行为全部由壳页按钮走 Tauri IPC，
所以这里不只看 DOM，还用 Win32 的 IsZoomed / IsIconic 读真实窗口状态。
用法（仓库根目录）：
    python desktop/test/run_win_controls.py
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop" / "test"))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ui = load("run_ui_shots", ROOT / "desktop" / "test" / "run_ui_shots.py")
win_shot = load("win_shot", ROOT / "desktop" / "test" / "win_shot.py")
user32 = win_shot.user32
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

SW_RESTORE = 9


def rect(hwnd: int) -> tuple[int, int, int, int]:
    r = win_shot.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def main() -> int:
    if not ui.EXE.exists():
        ui.log(f"missing exe: {ui.EXE}")
        return 2
    ui.RUN_DIR.mkdir(parents=True, exist_ok=True)
    spike = ui.load_spike()
    spike.HOME = ui.HOME
    spike.prepare_home()
    try:
        (ui.HOME / "webview2" / "main" / "EBWebView" / "DevToolsActivePort").unlink()
    except OSError:
        pass
    run_exe = ui.RUN_DIR / ui.EXE.name
    shutil.copy2(ui.EXE, run_exe)

    env = os.environ.copy()
    env.update({
        "LIVENOTES_HOME": str(ui.HOME),
        "LIVENOTES_NO_HELPERS": "1",
        "LIVENOTES_RESUME_ROOMS": "1",
        "LIVENOTES_ALLOW_MULTI": "1",
        "LIVENOTES_CLOSE_TO_TRAY": "0",
        "LIVENOTES_AUTO_EXIT_MS": "120000",
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":
            "--remote-debugging-port=0 --remote-allow-origins=*",
    })
    out = (ui.RUN_DIR / "win.out").open("wb")
    proc = subprocess.Popen([str(run_exe)], cwd=str(ui.RUN_DIR), env=env,
                            stdout=out, stderr=subprocess.STDOUT)
    try:
        target = None
        deadline = time.time() + 60
        while time.time() < deadline and proc.poll() is None:
            for t in ui.targets():
                if t.get("type") == "page" and "shell.html" in (t.get("url") or ""):
                    target = t
                    break
            if target:
                break
            time.sleep(0.5)
        if not target:
            ui.log("cdp target not found")
            return 1

        hwnd = 0
        for _ in range(40):
            try:
                hwnd = win_shot.find_window(proc.pid, "LiveNotes")
                break
            except Exception:
                time.sleep(0.25)
        ui.log(f"hwnd {hwnd} rect {rect(hwnd) if hwnd else None}")

        from websockets.sync.client import connect as ws_connect
        with ws_connect(target["webSocketDebuggerUrl"], open_timeout=10,
                        max_size=4 * 1024 * 1024) as ws:
            ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
            for _ in range(60):
                if ui.cdp_eval(ws, "typeof window.__LN_SHELL__", timeout=5) == "object":
                    break
                time.sleep(0.25)
            time.sleep(0.8)

            ui.cdp_eval(ws, "document.getElementById('winMax').click()")
            time.sleep(0.8)
            maxed = rect(hwnd)
            ui.log(f"maximize zoomed={bool(user32.IsZoomed(hwnd))} rect {maxed}")

            ui.cdp_eval(ws, "document.getElementById('winMax').click()")
            time.sleep(0.8)
            ui.log(f"restore zoomed={bool(user32.IsZoomed(hwnd))} rect {rect(hwnd)}")

            ui.cdp_eval(ws, "document.getElementById('winMin').click()")
            time.sleep(0.8)
            ui.log(f"minimize iconic={bool(user32.IsIconic(hwnd))}")
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.6)

            ui.cdp_eval(ws, "document.getElementById('winClose').click()")
            gone = False
            for _ in range(40):
                if proc.poll() is not None:
                    gone = True
                    break
                time.sleep(0.25)
            ui.log(f"close exited={gone} code={proc.poll()}")
            return 0 if gone else 1
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        out.close()


if __name__ == "__main__":
    sys.exit(main())
