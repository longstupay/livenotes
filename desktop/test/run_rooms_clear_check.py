"""验收：在听列表的「清除已停止」与「移除这一路」真的会改列表。

隔离数据目录 + 独立实例，用 CDP 驱动壳页面；只验行为，不碰真实用户数据。
用法（仓库根目录）：
    python desktop/test/run_rooms_clear_check.py
"""

from __future__ import annotations

import importlib.util
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
RUN_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Temp" / "livenotes-rooms-check"
HOME = RUN_DIR / "home"

try:
    from websockets.sync.client import connect as ws_connect
except Exception as e:  # pragma: no cover
    print(f"[rooms] websockets unavailable: {e}")
    sys.exit(2)


def log(msg: str) -> None:
    print(f"[rooms] {msg}", flush=True)


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wait_value(ws, expr: str, want, timeout: float = 20.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = ui.cdp_eval(ws, expr, timeout=5)
        if callable(want):
            if want(last):
                return last
        elif last == want:
            return last
        time.sleep(0.3)
    raise AssertionError(f"timeout waiting {expr!r}; last={last!r}")


def eval_json(ws, expr: str):
    raw = ui.cdp_eval(ws, f"JSON.stringify((() => ({expr}))())", timeout=10)
    if not isinstance(raw, str):
        raise AssertionError(f"eval failed for {expr!r}: {raw!r}")
    return json.loads(raw)


def main() -> int:
    if not EXE.exists():
        log(f"missing exe: {EXE}")
        return 2
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    global ui
    ui = load(ROOT / "desktop" / "test" / "run_ui_shots.py", "run_ui_shots")
    ui.HOME = HOME
    spike = ui.load_spike()
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
        # 不真的恢复采集，避免拉起浏览器和 ASR；这里只验列表动作。
        "LIVENOTES_RESUME_ROOMS": "0",
        "LIVENOTES_ALLOW_MULTI": "1",
        "LIVENOTES_CLOSE_TO_TRAY": "0",
        "LIVENOTES_AUTO_EXIT_MS": "180000",
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
            for t in ui.targets():
                if t.get("type") == "page" and "shell.html" in (t.get("url") or ""):
                    target = t
                    break
            if target:
                break
            time.sleep(0.5)
        if not target:
            log("cdp target not found")
            return 1
        with ws_connect(target["webSocketDebuggerUrl"], open_timeout=10,
                        max_size=8 * 1024 * 1024) as ws:
            ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
            wait_value(ws, "typeof window.__LN_SHELL__", "object")
            wait_value(ws, "window.__LN_SHELL__.state().rooms", 5)
            wait_value(ws, "!!document.querySelector('#sideFoot [data-clear-stopped]')", True)
            foot = ui.cdp_eval(ws, "document.getElementById('sideFoot').innerText", timeout=5)
            log(f"sidefoot: {foot}")
            assert "清除已停止 1" in foot, foot
            # 引擎启动后会把没有真正采集的旧房间收成 stopped，等它稳定再取快照。
            time.sleep(2.0)
            ids = eval_json(ws, "S.rooms.map(r => ({id: r.id, state: r.state, "
                                "kind: stateInfo(r).k, error: r.error || ''}))")
            failed = [r["id"] for r in ids if r["kind"] == "failed"]
            stopped = [r["id"] for r in ids if r["kind"] == "stopped"]
            log(f"rooms: {ids}")
            assert "rmdemo04" in failed, failed
            assert "rmdemo05" in stopped, stopped
            assert "rmdemo04" not in stopped, stopped

            # 1) 清除已停止：只移除 stopped，不动失败的那路
            ui.cdp_eval(ws, "document.querySelector('#sideFoot [data-clear-stopped]').click()", timeout=5)
            wait_value(ws, "document.getElementById('modal').hidden", False)
            modal = ui.cdp_eval(ws, "document.getElementById('modal').innerText", timeout=5)
            log(f"clear modal: {modal!r}")
            assert f"清除 {len(stopped)} 路已停止的直播间" in modal, (stopped, modal)
            ui.cdp_eval(ws, "document.querySelector('[data-mconfirm=\"clear\"]').click()", timeout=5)
            wait_value(ws, "window.__LN_SHELL__.state().rooms",
                       len(ids) - len(stopped))
            left = eval_json(ws, "S.rooms.map(r => r.id)")
            log(f"after clear: {left}")
            for sid in stopped:
                assert sid not in left, (sid, left)
            assert "rmdemo04" in left, left
            assert ui.cdp_eval(ws, "!!document.querySelector('#sideFoot [data-clear-stopped]')") is False
            before_remove = len(left)

            # 2) 移除这一路：失败的那路从列表消失，场次列表不受影响
            before_sessions = ui.cdp_eval(ws, "window.__LN_SHELL__.state().sessions", timeout=5)
            ui.cdp_eval(ws, "roomMenu('rmdemo04', 220, 320)", timeout=5)
            wait_value(ws, "document.getElementById('ctxMenu').hidden", False)
            menu = ui.cdp_eval(ws, "document.getElementById('ctxMenu').innerText", timeout=5)
            log(f"room menu: {menu!r}")
            assert "移除这一路" in menu, menu
            ui.cdp_eval(ws, """[...document.querySelectorAll('#ctxMenu .ctx-item')]
                .find(el => el.textContent.includes('移除这一路')).click()""", timeout=5)
            wait_value(ws, "document.getElementById('modal').hidden", False)
            modal = ui.cdp_eval(ws, "document.getElementById('modal').innerText", timeout=5)
            log(f"remove modal: {modal!r}")
            assert "录下的内容会保留" in modal, modal
            ui.cdp_eval(ws, "document.querySelector('[data-mconfirm=\"remove\"]').click()", timeout=5)
            wait_value(ws, "window.__LN_SHELL__.state().rooms", before_remove - 1)
            left = eval_json(ws, "S.rooms.map(r => r.id)")
            log(f"after remove: {left}")
            assert "rmdemo04" not in left, left
            after_sessions = ui.cdp_eval(ws, "window.__LN_SHELL__.state().sessions", timeout=5)
            assert before_sessions == after_sessions, (before_sessions, after_sessions)
            log(f"sessions unchanged: {after_sessions}")

            ui.shot(proc.pid, "impl-rooms-cleared.png")
            log("PASS")
        return 0
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        out.close()


if __name__ == "__main__":
    sys.exit(main())
