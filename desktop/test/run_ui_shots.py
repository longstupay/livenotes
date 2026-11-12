"""实现验收：启动新壳，截「收听 / 工作台 / 文件 / 人物」四屏。

隔离数据目录 + 独立实例；截图走 Win32 GDI（frameless 窗口同样按矩形抓）。
用法（仓库根目录）：
    python desktop/test/run_ui_shots.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop" / "src-tauri" / "target" / "debug" / "livenotes-desktop.exe"
RUN_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Temp" / "livenotes-ui"
HOME = RUN_DIR / "home"
OUT = ROOT / "docs" / "prototypes" / "shots"

try:
    from websockets.sync.client import connect as ws_connect
except Exception as e:  # pragma: no cover
    print(f"[ui] websockets unavailable: {e}")
    sys.exit(2)


def log(msg: str) -> None:
    print(f"[ui] {msg}", flush=True)


def load_spike():
    spec = importlib.util.spec_from_file_location(
        "run_m1_spike", ROOT / "desktop" / "test" / "run_m1_spike.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def devtools_port() -> int:
    f = HOME / "webview2" / "main" / "EBWebView" / "DevToolsActivePort"
    try:
        return int(f.read_text(encoding="utf-8").splitlines()[0])
    except Exception:
        return 0


def targets() -> list[dict]:
    port = devtools_port()
    if not port:
        return []
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return []


def cdp_eval(ws, expr: str, timeout: float = 20.0):
    mid = cdp_eval.seq = getattr(cdp_eval, "seq", 100) + 1
    ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "awaitPromise": True,
                                   "returnByValue": True, "userGesture": True}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(ws.recv(timeout=timeout))
        if msg.get("id") == mid:
            res = msg.get("result", {})
            if "exceptionDetails" in res:
                return {"error": str(res["exceptionDetails"].get("text"))}
            return (res.get("result") or {}).get("value")
    return None


def shot(pid: int, name: str) -> bool:
    sys.path.insert(0, str(ROOT / "desktop" / "test"))
    try:
        import win_shot
    except Exception as e:
        log(f"shot helper unavailable: {e}")
        return False
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        r = win_shot.capture(pid, OUT / name, "LiveNotes")
        log(f"shot {name} {r}")
        return True
    except Exception as e:
        log(f"shot {name} failed: {e}")
        return False


def main() -> int:
    if not EXE.exists():
        log(f"missing exe: {EXE}")
        return 2
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    spike = load_spike()
    spike.HOME = HOME
    spike.prepare_home()
    try:
        (HOME / "webview2" / "main" / "EBWebView" / "DevToolsActivePort").unlink()
    except OSError:
        pass
    run_exe = RUN_DIR / EXE.name
    shutil.copy2(EXE, run_exe)

    env = os.environ.copy()
    env.update({
        "LIVENOTES_HOME": str(HOME),
        "LIVENOTES_NO_HELPERS": "1",
        "LIVENOTES_RESUME_ROOMS": "1",
        "LIVENOTES_ALLOW_MULTI": "1",
        "LIVENOTES_CLOSE_TO_TRAY": "0",
        "LIVENOTES_AUTO_EXIT_MS": "120000",
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":
            "--remote-debugging-port=0 --remote-allow-origins=*",
    })
    out = (RUN_DIR / "app.out").open("wb")
    log(f"launch {run_exe.name}")
    proc = subprocess.Popen([str(run_exe)], cwd=str(RUN_DIR), env=env,
                            stdout=out, stderr=subprocess.STDOUT)
    try:
        target = None
        deadline = time.time() + 60
        while time.time() < deadline and proc.poll() is None:
            for t in targets():
                if t.get("type") == "page" and "shell.html" in (t.get("url") or ""):
                    target = t
                    break
            if target:
                break
            time.sleep(0.5)
        if not target:
            log("cdp target not found")
            return 1
        log(f"cdp {target.get('url')}")
        with ws_connect(target["webSocketDebuggerUrl"], open_timeout=10,
                        max_size=8 * 1024 * 1024) as ws:
            ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
            for _ in range(60):
                if cdp_eval(ws, "typeof window.__LN_SHELL__", timeout=5) == "object":
                    break
                time.sleep(0.25)
            time.sleep(1.2)
            shot(proc.pid, "impl-listen.png")

            log("winapi " + str(cdp_eval(
                ws, "typeof (window.__TAURI__ && __TAURI__.window && __TAURI__.window.getCurrentWindow)")))
            for view, name in [("workbench", "impl-workbench.png"),
                               ("files", "impl-files.png"),
                               ("people", "impl-people.png")]:
                cdp_eval(ws, f"window.__LN_SHELL__.view('{view}')")
                time.sleep(2.2)
                log(f"{view} theme=" + str(cdp_eval(ws, "document.documentElement.dataset.theme")))
                log(f"{view} frame=" + str(cdp_eval(ws, "document.getElementById('frame').src")))
                shot(proc.pid, name)

            cdp_eval(ws, "window.__LN_SHELL__.view('listen')")
            time.sleep(1.2)
            state = cdp_eval(ws, "JSON.stringify(window.__LN_SHELL__.state())")
            log(f"state {state}")
        return 0
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        out.close()


if __name__ == "__main__":
    sys.exit(main())
