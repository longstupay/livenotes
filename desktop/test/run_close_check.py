"""关窗/退出语义验收。

1. 有录音 + 默认「最小化到托盘」：点关闭 -> 出现三选弹窗 -> 选托盘 ->
   窗口隐藏但进程还活着 -> 再点关闭 -> 选「停止并退出」-> 进程退出。
2. 没有录音 + 设置「退出程序」：点关闭 -> 直接退出。

用法（仓库根目录）：
    python desktop/test/run_close_check.py
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
import urllib.request
from ctypes import wintypes
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop" / "test"))

import psutil  # noqa: E402

ui = None


def load_ui():
    global ui
    spec = importlib.util.spec_from_file_location(
        "run_ui_shots", ROOT / "desktop" / "test" / "run_ui_shots.py")
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)
    return ui


user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int


def visible_windows(pid: int) -> list[tuple[bool, str, str]]:
    found: list[tuple[bool, str, str]] = []

    def cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(max(n + 2, 2))
            user32.GetWindowTextW(hwnd, buf, len(buf))
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, len(cls))
            found.append((bool(user32.IsWindowVisible(hwnd)), buf.value, cls.value))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def prepare(home: Path, rooms: list[dict], prefs: dict | None = None) -> None:
    spike = ui.load_spike()
    spike.HOME = home
    spike.prepare_home()
    data = home / "data"
    shutil.rmtree(data / "runtime", ignore_errors=True)
    (data / "rooms.json").write_text(
        json.dumps({"rooms": rooms}, ensure_ascii=False), encoding="utf-8")
    if prefs:
        (data / "settings.json").write_text(
            json.dumps({"version": 2, "profiles": {}, "prefs": prefs},
                       ensure_ascii=False), encoding="utf-8")
    else:
        try:
            (data / "settings.json").unlink()      # 回到默认 close_action=tray
        except OSError:
            pass
    try:
        (home / "webview2" / "main" / "EBWebView" / "DevToolsActivePort").unlink()
    except OSError:
        pass


def launch(home: Path, auto_exit_ms: int = 120000) -> subprocess.Popen:
    ui.RUN_DIR.mkdir(parents=True, exist_ok=True)
    run_exe = ui.RUN_DIR / ui.EXE.name
    shutil.copy2(ui.EXE, run_exe)
    env = os.environ.copy()
    env.update({
        "LIVENOTES_HOME": str(home),
        "LIVENOTES_NO_HELPERS": "1",
        "LIVENOTES_NO_FEISHU": "1",
        "LIVENOTES_RESUME_ROOMS": "0",
        "LIVENOTES_ALLOW_MULTI": "1",
        "LIVENOTES_AUTO_EXIT_MS": str(auto_exit_ms),
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":
            "--remote-debugging-port=0 --remote-allow-origins=*",
    })
    out = (ui.RUN_DIR / "close-check.out").open("wb")
    proc = subprocess.Popen([str(run_exe)], cwd=str(ui.RUN_DIR), env=env,
                            stdout=out, stderr=subprocess.STDOUT)
    proc._ln_out = out  # type: ignore[attr-defined]
    return proc


def connect(proc: subprocess.Popen):
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
        return None
    ws = ui.ws_connect(target["webSocketDebuggerUrl"], open_timeout=10,
                       max_size=8 * 1024 * 1024).__enter__()
    ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
    for _ in range(80):
        if ui.cdp_eval(ws, "typeof window.__LN_SHELL__", timeout=5) == "object":
            return ws
        time.sleep(0.25)
    return ws


def wait_exit(proc: subprocess.Popen, timeout: float) -> int | None:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def phase_active_room() -> int:
    home = ui.HOME
    prepare(home, [])
    proc = launch(home)
    ws = None
    try:
        ws = connect(proc)
        if ws is None:
            ui.log("phase1: shell target not found")
            return 1
        # 引擎启动时会 reconcile 掉上次残留的 live 房间，所以房间要等引擎
        # 起来之后再写；壳每 3 秒轮询 /api/rooms，写文件即可生效。
        (home / "data" / "rooms.json").write_text(json.dumps(
            {"rooms": [{"id": "r1", "seq": 1, "url": "https://live.example.com/1",
                        "title": "测试直播", "state": "live", "session_id": ""}]},
            ensure_ascii=False), encoding="utf-8")
        action = ui.cdp_eval(ws, "window.__LN_SHELL__.closeAction()")
        ui.log("phase1 closeAction=" + str(action))
        if action != "tray":
            ui.log("phase1: expected default close_action=tray")
            return 1
        # 等壳的轮询把房间读进来
        ok = False
        for _ in range(30):
            if ui.cdp_eval(ws, "window.__LN_SHELL__.state().live") == 1:
                ok = True
                break
            time.sleep(0.5)
        if not ok:
            ui.log("phase1: room not visible in shell state")
            return 1
        ui.log(f"phase1 window visible before close: {visible_windows(proc.pid)}")
        # 走真实路径：JS 调窗口 close -> Rust CloseRequested -> prevent_close
        # -> shell_close_requested -> 壳页回执并弹窗。
        ui.cdp_eval(ws, "window.__TAURI__.window.getCurrentWindow().close(); true")
        time.sleep(0.8)
        modal = ui.cdp_eval(ws, "JSON.stringify({hidden:document.getElementById('modal').hidden,"
                                "text:document.getElementById('modalSheet').innerText})")
        ui.log("phase1 modal=" + str(modal))
        if not modal or '"hidden":false' not in str(modal).replace(" ", ""):
            ui.log("phase1: close modal did not open")
            return 1
        ui.shot(proc.pid, "close-modal.png")
        clicked = ui.cdp_eval(
            ws, "(()=>{const b=[...document.querySelectorAll('#modalSheet button')]"
                ".find(x=>x.textContent.includes('最小化到托盘'));if(b){b.click();return true}return false})()")
        ui.log("phase1 click tray=" + str(clicked))
        time.sleep(3.2)
        alive = proc.poll() is None
        vis = visible_windows(proc.pid)
        ui.log(f"phase1 after tray: alive={alive} windows={vis}")
        main_visible = any(v and t.startswith("LiveNotes") for v, t, _c in vis)
        if not alive or main_visible:
            ui.log("phase1: expected hidden window with app still running")
            return 1
        # 再关一次：这次选「停止并退出」
        ui.cdp_eval(ws, "window.__TAURI__.window.getCurrentWindow().close(); true")
        time.sleep(0.8)
        clicked = ui.cdp_eval(
            ws, "(()=>{const b=[...document.querySelectorAll('#modalSheet button')]"
                ".find(x=>x.textContent.includes('停止并退出'));if(b){b.click();return true}return false})()")
        ui.log("phase1 click stop-quit=" + str(clicked))
        code = wait_exit(proc, 25)
        ui.log(f"phase1 app exit code={code}")
        if code is None:
            return 1
        time.sleep(1.0)
        rooms = json.loads((home / "data" / "rooms.json").read_text(encoding="utf-8"))
        ui.log("phase1 rooms after quit: " + json.dumps(
            [{"id": r.get("id"), "state": r.get("state")} for r in rooms.get("rooms", [])],
            ensure_ascii=False))
        leftovers = [p.pid for p in psutil.process_iter(["pid", "name", "cmdline"])
                     if p.info["name"] == "python.exe"
                     and "run.py" in " ".join(p.info["cmdline"] or [])]
        ui.log(f"phase1 python leftovers: {leftovers}")
        return 0 if not leftovers else 1
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if proc.poll() is None:
            proc.kill()
        proc._ln_out.close()  # type: ignore[attr-defined]


def phase_quit_setting() -> int:
    home = ui.HOME
    prepare(home, [], {"close_action": "quit"})
    proc = launch(home)
    ws = None
    try:
        ws = connect(proc)
        if ws is None:
            ui.log("phase2: shell target not found")
            return 1
        for _ in range(30):
            if ui.cdp_eval(ws, "window.__LN_SHELL__.closeAction()") == "quit":
                break
            time.sleep(0.4)
        ui.log("phase2 closeAction=" + str(ui.cdp_eval(ws, "window.__LN_SHELL__.closeAction()")))
        ui.cdp_eval(ws, "window.__TAURI__.window.getCurrentWindow().close(); true")
        code = wait_exit(proc, 20)
        ui.log(f"phase2 app exit code={code}")
        return 0 if code is not None else 1
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if proc.poll() is None:
            proc.kill()
        proc._ln_out.close()  # type: ignore[attr-defined]


def main() -> int:
    load_ui()
    if not ui.EXE.exists():
        ui.log(f"missing exe: {ui.EXE}")
        return 2
    r1 = phase_active_room()
    ui.log(f"phase1 result={r1}")
    if r1 != 0:
        return r1
    r2 = phase_quit_setting()
    ui.log(f"phase2 result={r2}")
    return r2


if __name__ == "__main__":
    sys.exit(main())
