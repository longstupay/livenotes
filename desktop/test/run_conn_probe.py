"""诊断：壳页（tauri.localhost）能否访问引擎 HTTP API。

隔离数据目录 + 独立实例，不改真实配置。用 WebView2 远程调试口在壳页里
直接 fetch 一次，并把浏览器的 CORS / 网络报错原样带回来。

用法（仓库根目录）：
    python -I desktop/test/run_conn_probe.py
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
RUN_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Temp" / "livenotes-conn"
HOME = RUN_DIR / "home"

try:
    from websockets.sync.client import connect as ws_connect
except Exception as e:  # pragma: no cover
    print(f"[conn] websockets unavailable: {e}")
    sys.exit(2)


def log(msg: str) -> None:
    print(f"[conn] {msg}", flush=True)


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


PROBE_JS = r"""
(async () => {
  const out = {
    href: location.href,
    origin: location.origin,
    engine: window.__LN_ENGINE__ || '',
    entry: window.__LN_ENTRY__ || '',
    api: (typeof API === 'string' ? API : '(no API var)'),
    online: (typeof S === 'object' ? S.online : '(no S)'),
    statusText: (document.querySelector('#engineStatus') || {}).textContent || '',
  };
  const t0 = performance.now();
  try {
    const r = await fetch(out.api + '/api/health', { cache: 'no-store' });
    out.health = { status: r.status, type: r.type, body: (await r.text()).slice(0, 160), ms: Math.round(performance.now() - t0) };
  } catch (e) {
    out.healthErr = String((e && e.name) || 'Error') + ': ' + String((e && e.message) || e);
  }
  try {
    const r = await fetch(out.api + '/api/rooms', { cache: 'no-store' });
    out.rooms = { status: r.status, body: (await r.text()).slice(0, 120) };
  } catch (e) {
    out.roomsErr = String((e && e.name) || 'Error') + ': ' + String((e && e.message) || e);
  }
  out.apiResources = performance.getEntriesByType('resource')
    .filter((e) => e.name.indexOf('/api/') >= 0)
    .map((e) => ({ name: e.name, duration: Math.round(e.duration), size: e.transferSize }))
    .slice(0, 6);
  return JSON.stringify(out);
})()
"""


def cdp_collect(ws, expr: str, timeout: float = 30.0):
    """执行表达式，同时收集浏览器 Log / 控制台事件。"""
    events: list[dict] = []
    seq = getattr(cdp_collect, "seq", 100) + 1
    cdp_collect.seq = seq
    ws.send(json.dumps({"id": seq, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "awaitPromise": True,
                                   "returnByValue": True, "userGesture": True}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(ws.recv(timeout=timeout))
        if msg.get("id") == seq:
            return msg.get("result", {}), events
        if msg.get("method") in ("Log.entryAdded", "Runtime.exceptionThrown",
                                 "Runtime.consoleAPICalled"):
            events.append(msg)
    return {}, events


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
            log("cdp target not found; targets=" + json.dumps(
                [(t.get("type"), t.get("url"), t.get("title")) for t in targets()],
                ensure_ascii=False))
            return 1
        log(f"cdp {target.get('url')}")
        with ws_connect(target["webSocketDebuggerUrl"], open_timeout=10,
                        max_size=8 * 1024 * 1024) as ws:
            ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
            ws.send(json.dumps({"id": 1, "method": "Log.enable"}))
            time.sleep(3.0)  # 让壳跑完几轮轮询
            res, events = cdp_collect(ws, PROBE_JS, timeout=30)
        value = (res.get("result") or {}).get("value")
        print(json.dumps(json.loads(value) if value else res, ensure_ascii=False, indent=2), flush=True)
        for ev in events:
            m = ev.get("method")
            p = ev.get("params") or {}
            if m == "Log.entryAdded":
                e = p.get("entry") or {}
                print(f"[{m}] {e.get('source')}/{e.get('level')}: {e.get('text')}", flush=True)
            elif m == "Runtime.exceptionThrown":
                d = (p.get("exceptionDetails") or {})
                print(f"[{m}] {d.get('text')} {(d.get('exception') or {}).get('description','')}", flush=True)
        shell_log = HOME / "logs" / "shell.log"
        if shell_log.exists():
            print("--- shell.log tail ---", flush=True)
            print("\n".join(shell_log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]), flush=True)
        return 0
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        out.close()


if __name__ == "__main__":
    sys.exit(main())
