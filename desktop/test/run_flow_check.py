"""交互流程冒烟：用 WebView2 远程调试口驱动本地壳，验证添加/重试/停止/命令面板。

只跑在隔离数据目录上（复用 m1 验收的合成房间），不会碰真实配置。

用法（仓库根目录）：
    python -I desktop/test/run_flow_check.py
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
RUN_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Temp" / "livenotes-flows"
HOME = RUN_DIR / "home"
PROFILE_HOME = HOME  # 实际启动用的 LIVENOTES_HOME（复用 m1 隔离数据）
CDP_PORT = 0  # 0 = 让 WebView2 自己挑端口，从 DevToolsActivePort 读回

try:
    from websockets.sync.client import connect as ws_connect
except Exception as e:  # pragma: no cover
    print(f"[flow] websockets unavailable: {e}")
    sys.exit(2)


def log(msg: str) -> None:
    print(f"[flow] {msg}", flush=True)


def load_spike():
    spec = importlib.util.spec_from_file_location(
        "run_m1_spike", ROOT / "desktop" / "test" / "run_m1_spike.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def devtools_port() -> int:
    f = PROFILE_HOME / "webview2" / "main" / "EBWebView" / "DevToolsActivePort"
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


def cdp_eval(ws, expr: str, timeout: float = 30.0) -> dict:
    mid = cdp_eval.seq = getattr(cdp_eval, "seq", 0) + 1
    ws.send(json.dumps({
        "id": mid, "method": "Runtime.evaluate",
        "params": {"expression": expr, "awaitPromise": True,
                   "returnByValue": True, "userGesture": True},
    }))
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(ws.recv(timeout=timeout))
        if msg.get("id") == mid:
            res = msg.get("result", {})
            if "exceptionDetails" in res:
                raise RuntimeError(res["exceptionDetails"].get("text") or res["exceptionDetails"])
            return res.get("result", {}).get("value")
    raise TimeoutError("cdp eval timed out")


FLOW_JS = r"""
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const S = window.__LN_SHELL__;
  const out = { initial: S.state() };

  // 1) 添加直播间：走壳的 POST + CORS
  S.add();
  document.getElementById('addInput').value = 'https://live.bilibili.com/99999999';
  document.getElementById('addGo').click();
  await sleep(2600);
  out.afterAdd = S.state();
  out.addToast = document.getElementById('toast').textContent;
  out.addSelected = document.querySelector('.room-row.on .room-name')?.textContent || '';

  // 2) 命令面板：Ctrl+K、方向键、Esc
  S.listen();
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
  await sleep(250);
  out.paletteOpen = !document.getElementById('palette').hidden;
  out.paletteCount = document.querySelectorAll('#pList [data-pi]').length;
  const pi = document.getElementById('pInput');
  pi.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
  pi.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
  out.paletteActive = document.querySelector('#pList .on')?.textContent.trim() || '';
  pi.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await sleep(200);
  out.paletteClosed = document.getElementById('palette').hidden;

  // 3) 重试异常房间（合成数据里的 CH04；按 id 选，避免排序影响）
  document.querySelector('.room-row[data-room="rmdemo04"]').click();
  await sleep(300);
  out.retryTarget = S.state().selected;
  document.getElementById('stageRetry').click();
  await sleep(1400);
  out.afterRetry = S.state();
  out.retryToast = document.getElementById('toast').textContent;
  try {
    const rr = await (await fetch(window.__LN_ENGINE__ + '/api/rooms')).json();
    out.retryState = (rr.rooms || []).find(r => r.id === 'rmdemo04')?.state || '';
  } catch (e) { out.retryState = 'fetch-error'; }

  // 4) 单路停止：先看确认框，再确认
  S.select(0);
  await sleep(250);
  S.stopModal();
  await sleep(200);
  out.stopModal = document.querySelector('#modal h3')?.textContent || '';
  document.querySelector('[data-mconfirm="stop"]').click();
  await sleep(1600);
  out.afterStop = S.state();
  out.stopToast = document.getElementById('toast').textContent;
  out.stopState = document.querySelector('.room-row.on .room-sub .state')?.textContent || '';

  // 5) 停止全部：只开确认框然后取消，验证弹层与计数
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
  await sleep(150);
  const p2 = document.getElementById('pInput');
  p2.value = '全部';
  p2.dispatchEvent(new KeyboardEvent('input', { bubbles: true }));
  await sleep(150);
  const stopItem = document.querySelector('#pList [data-pi]');
  out.stopAllItem = stopItem?.textContent.trim() || '';
  stopItem?.click();
  await sleep(300);
  out.stopAllModal = document.querySelector('#modal h3')?.textContent || '';
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await sleep(150);
  out.modalClosed = document.getElementById('modal').hidden;

  out.final = S.state();
  return out;
})()
"""


def main() -> int:
    if not EXE.exists():
        log(f"missing exe: {EXE}")
        return 2
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    global PROFILE_HOME
    spike = load_spike()
    spike.prepare_home()
    PROFILE_HOME = spike.HOME
    try:
        (PROFILE_HOME / "webview2" / "main" / "EBWebView" / "DevToolsActivePort").unlink()
    except OSError:
        pass
    run_exe = RUN_DIR / EXE.name
    shutil.copy2(EXE, run_exe)

    env = os.environ.copy()
    env.update({
        "LIVENOTES_HOME": str(spike.HOME),
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
        deadline = time.time() + 40
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
            for _ in range(60):
                if cdp_eval(ws, "typeof window.__LN_SHELL__", timeout=5) == "object":
                    break
                time.sleep(0.25)
            res = cdp_eval(ws, FLOW_JS, timeout=45)
        print(json.dumps(res, ensure_ascii=False, indent=2), flush=True)
        ok = (
            res["afterAdd"]["rooms"] == res["initial"]["rooms"] + 1
            and res["paletteOpen"] and res["paletteClosed"]
            and res.get("retryTarget") == "rmdemo04" and res.get("retryState") != "failed"
            and "停止" in res["stopModal"]
            and res["afterStop"]["rooms"] == res["afterAdd"]["rooms"]
            and "已停止" in res.get("stopState", "")
            and "停止全部" in res["stopAllModal"] and res["modalClosed"]
        )
        log(f"ok={ok}")
        return 0 if ok else 1
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        out.close()


if __name__ == "__main__":
    sys.exit(main())
