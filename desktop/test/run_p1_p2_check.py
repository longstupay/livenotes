"""P1/P2 验收：工作台内切场次、批量、回收站入口、清理中心、字幕行右键菜单。

隔离数据目录 + 独立实例 + CDP；只在测试库里操作，不碰真实用户数据。
用法（仓库根目录）：
    python desktop/test/run_p1_p2_check.py
"""

from __future__ import annotations

import importlib.util
import json
from contextlib import contextmanager
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
RUN_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Temp" / "livenotes-p1p2"
HOME = RUN_DIR / "home"
SECOND = "SECOND_P1P2"

try:
    from websockets.sync.client import connect as ws_connect
except Exception as e:  # pragma: no cover
    print(f"[p1p2] websockets unavailable: {e}")
    sys.exit(2)


def log(msg: str) -> None:
    print(f"[p1p2] {msg}", flush=True)


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Cdp:
    def __init__(self, ws):
        self.ws = ws
        self.seq = 100

    def send(self, method: str, params: dict | None = None) -> int:
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        return self.seq

    def wait(self, mid: int, timeout: float = 20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.time())))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise AssertionError(msg["error"])
                return msg.get("result") or {}
        raise AssertionError(f"CDP timeout for {mid}")

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0):
        return self.wait(self.send(method, params), timeout)

    def eval(self, expr: str, ctx: int | None = None, timeout: float = 20.0):
        p = {"expression": expr, "awaitPromise": True, "returnByValue": True,
             "userGesture": True}
        if ctx:
            p["contextId"] = ctx
        res = self.call("Runtime.evaluate", p, timeout)
        if "exceptionDetails" in res:
            raise AssertionError(json.dumps(res["exceptionDetails"], ensure_ascii=False))
        return (res.get("result") or {}).get("value")

    def wait_value(self, expr: str, want, ctx: int | None = None,
                   timeout: float = 25.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = self.eval(expr, ctx, timeout=5)
            except Exception as e:
                last = f"ERR {e}"
            if callable(want):
                if want(last):
                    return last
            elif last == want:
                return last
            time.sleep(0.3)
        raise AssertionError(f"timeout {expr!r}; last={last!r}")



@contextmanager
def iframe_client(ui, match, timeout: float = 15.0):
    """工作台/文件中心是跨源 OOPIF：在它自己的 CDP target 上开一条连接。"""
    deadline = time.time() + timeout
    target = None
    while time.time() < deadline:
        for t in ui.targets():
            u = t.get("url") or ""
            if t.get("type") == "iframe" and "127.0.0.1" in u and match(u):
                target = t
                break
        if target:
            break
        time.sleep(0.3)
    if not target:
        raise AssertionError("iframe target not found")
    with ws_connect(target["webSocketDebuggerUrl"], open_timeout=10,
                    max_size=16 * 1024 * 1024) as ws:
        f = Cdp(ws)
        f.call("Runtime.enable")
        yield f


def seed_second_session() -> None:
    from app import store
    conn = store.connect()
    try:
        store.init_db(conn)
        if not store.get_session(conn, SECOND):
            store.insert_session(conn, {
                "id": SECOND, "title": "P1/P2 第二场 · 仅文字",
                "started_at": "2026-11-11T12:00:00", "duration_s": 120.0,
                "source_mode": "direct", "sample_rate": 16000, "audio_path": None,
                "speakers": [{"speaker_id": "SPK_00", "display_name": "主播"}],
            })
            store.insert_utterances(conn, [
                {"session_id": SECOND, "t_start": 10.0, "t_end": 12.0,
                 "speaker_id": "SPK_00", "speaker_label": "主播",
                 "text": "这是第二场，用来验证工作台内切场次不重载。",
                 "asr_conf": 0.95, "overlap": 0, "category": "substantive",
                 "value_tag": "normal", "importance": 0.7, "topic": "",
                 "low_value_reason": ""},
            ])
    finally:
        conn.close()


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
    seed_second_session()
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
        "LIVENOTES_RESUME_ROOMS": "0",
        "LIVENOTES_ALLOW_MULTI": "1",
        "LIVENOTES_CLOSE_TO_TRAY": "0",
        "LIVENOTES_AUTO_EXIT_MS": "240000",
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
                        max_size=16 * 1024 * 1024) as ws:
            cdp = Cdp(ws)
            cdp.call("Runtime.enable")
            cdp.call("Page.enable")
            cdp.wait_value("typeof window.__LN_SHELL__", "object")
            cdp.wait_value("window.__LN_SHELL__.state().sessions", lambda n: n >= 2)
            # 1) 工作台内切场次：iframe 不重载
            cdp.eval("window.__LN_SHELL__.view('workbench')")
            cdp.wait_value("window.__LN_SHELL__.state().workbenchReady", True)
            st0 = cdp.eval("JSON.stringify(window.__LN_SHELL__.state())")
            st0 = json.loads(st0)
            loads0 = st0["frameLoads"]
            src0 = cdp.eval("document.getElementById('frame').src")
            first_sid = cdp.eval("(S.sessions.find(s=>s.is_demo)||{}).id || ''")
            assert first_sid, "demo session missing"
            log(f"workbench loaded sid={st0['selectedSession']} loads={loads0}; demo={first_sid}")
            # 先点演示场，再点第二场：两次都应该只发消息，不重载 iframe。
            cdp.eval(f"""document.querySelector('#sideList [data-session="{first_sid}"]').click()""")
            cdp.wait_value("window.__LN_SHELL__.state().workbenchSid", first_sid)
            cdp.eval(f"""document.querySelector('#sideList [data-session="{SECOND}"]').click()""")
            cdp.wait_value("window.__LN_SHELL__.state().workbenchSid", SECOND)
            st1 = json.loads(cdp.eval("JSON.stringify(window.__LN_SHELL__.state())"))
            src1 = cdp.eval("document.getElementById('frame').src")
            log(f"switched sid={st1['selectedSession']} loads={st1['frameLoads']} src={src1}")
            assert st1["frameLoads"] == loads0, (loads0, st1["frameLoads"])
            # 一次都没重载：iframe src 应保持初次加载的地址，内容靠消息原地切。
            assert src1 == src0, (src0, src1)

            # 2) 批量选择与取消
            cdp.eval("document.querySelector('[data-batch-toggle]').click()")
            cdp.wait_value("window.__LN_SHELL__.state().batchMode", True)
            cdp.eval(f"""document.querySelector('#sideList [data-session="{SECOND}"]').click()""")
            cdp.wait_value("window.__LN_SHELL__.state().batchSelected", 1)
            cdp.eval("document.querySelector('[data-batch-cancel]').click()")
            cdp.wait_value("window.__LN_SHELL__.state().batchMode", False)

            # 3) 工作台侧栏回收站入口
            cdp.eval("document.querySelector('[data-open-trash]').click()")
            cdp.wait_value("window.__LN_SHELL__.state().view", "files")
            src_trash = cdp.eval("document.getElementById('frame').src")
            log(f"trash frame: {src_trash}")
            assert "tab=trash" in src_trash, src_trash
            time.sleep(1.2)

            # 4) iframe 内：清理中心 tab 能出候选
            with iframe_client(ui, lambda u: "library" in u) as f:
                f.wait_value("!!document.querySelector('#tabs [data-v=\"cleanup\"]')", True)
                f.eval("document.querySelector('#tabs [data-v=\"cleanup\"]').click()")
                f.wait_value("document.querySelector('#tabs button.on').textContent", "体检清理")
                f.wait_value("document.querySelectorAll('#rows tr').length",
                             lambda n: n >= 1)
                card = f.eval("document.getElementById('cards').innerText")
                reasons = f.eval("document.getElementById('rows').innerText")
                log(f"cleanup cards: {card!r}")
                assert "待清理候选" in card, card
                assert "演示数据" in reasons, reasons
                # 默认全选 → 移到回收站 → 底部撤销，候选应回来。
                f.eval("window.__lnConfirm=window.confirm;window.confirm=function(){return true}")
                f.eval("document.getElementById('bulkTrash').click()")
                f.wait_value("document.querySelectorAll('#rows tr').length", 0)
                f.wait_value("document.getElementById('undoBar').classList.contains('show')",
                             True)
                f.eval("document.getElementById('undoBtn').click()")
                f.wait_value("document.querySelectorAll('#rows tr').length",
                             lambda n: n >= 1)
                f.eval("window.confirm=window.__lnConfirm")
                log("cleanup trash + undo ok")

            # 5) 回工作台，验字幕行右键菜单
            cdp.eval("window.__LN_SHELL__.view('workbench')")
            cdp.wait_value("window.__LN_SHELL__.state().workbenchReady", True)
            cdp.eval(f"""document.querySelector('#sideList [data-session="{first_sid}"]').click()""")
            cdp.wait_value("window.__LN_SHELL__.state().workbenchSid", first_sid)
            with iframe_client(ui, lambda u: "library" not in u) as f:
                f.wait_value("document.querySelectorAll('#list .urow').length",
                             lambda n: n >= 2)

                def row_menu():
                    f.eval("""(() => { document.querySelector('#list .urow').dispatchEvent(
                      new MouseEvent('contextmenu',{bubbles:true,clientX:220,clientY:240})); })()""")
                    f.wait_value("!!document.querySelector('.ln-ctx')", True)

                row_menu()
                menu = f.eval("document.querySelector('.ln-ctx').innerText")
                log(f"subtitle menu: {menu!r}")
                for want in ("改说话人", "标记高价值", "在全库搜索"):
                    assert want in menu, (want, menu)
                f.eval("""[...document.querySelectorAll('.ln-ctx-item')]
                  .find(b=>b.textContent.includes('标记高价值')).click()""")
                f.wait_value("!!document.querySelector('#list .urow.manual')", True)

                row_menu()
                f.wait_value("document.querySelector('.ln-ctx').innerText.includes('取消高价值标记')",
                             True)
                f.eval("document.body.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}))")
                # 改说话人子菜单
                row_menu()
                f.eval("""[...document.querySelectorAll('.ln-ctx-item')]
                  .find(b=>b.textContent.includes('改说话人')).click()""")
                f.wait_value("!!document.querySelector('.ln-ctx')", True)
                sub = f.eval("document.querySelector('.ln-ctx').innerText")
                log(f"speaker menu: {sub!r}")
                assert "新建说话人" in sub, sub
                f.eval("document.body.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}))")

                # 输入框右键菜单（顶栏在 embed 里隐藏，但元素和菜单逻辑仍在）
                f.eval("""(() => { const q=document.getElementById('gq');
                  q.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,
                    clientX:200,clientY:120})); })()""")
                f.wait_value("!!document.querySelector('.ln-ctx')", True)
                imenu = f.eval("document.querySelector('.ln-ctx').innerText")
                log(f"input menu: {imenu!r}")
                assert "粘贴" in imenu and "全选" in imenu, imenu
                f.eval("document.body.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}))")

                # 选中文本 → iframe 内全库搜索面板
                f.eval("""(() => { const t=document.querySelector('#list .urow .txt');
                  const r=document.createRange(); r.selectNodeContents(t);
                  const s=getSelection(); s.removeAllRanges(); s.addRange(r);
                  t.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,
                    clientX:260,clientY:300})); })()""")
                f.wait_value("!!document.querySelector('.ln-ctx')", True)
                smenu = f.eval("document.querySelector('.ln-ctx').innerText")
                log(f"selection menu: {smenu!r}")
                assert "定位到这句话" in smenu and "搜索" in smenu, smenu
                f.eval("""[...document.querySelectorAll('.ln-ctx-item')]
                  .find(b=>b.textContent.includes('搜索')).click()""")
                f.wait_value("!!document.querySelector('.ln-search')", True)
                f.wait_value("document.querySelectorAll('.ln-s-row').length",
                             lambda n: n >= 1)
                log("search overlay rows: " + str(
                    f.eval("document.querySelectorAll('.ln-s-row').length")))
                f.eval("document.querySelector('.ln-s-head [data-x]').click()")

            ui.shot(proc.pid, "impl-workbench-menu.png")
            log("PASS")
        return 0
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        out.close()


if __name__ == "__main__":
    sys.exit(main())
