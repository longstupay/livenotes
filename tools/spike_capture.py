# -*- coding: utf-8 -*-
"""验证：受控 Chrome 能否零点击采集指定标签页的音频。

三个变量分开测：
  A 基线          无策略、无开关  -> 预期：没有用户手势就被拒
  B 只加策略      免手势，但没选源 -> 预期：卡在选源弹窗（或被拒）
  C 策略 + 选源   两者都开        -> 期望：自动选中目标标签页并拿到音频

只写 HKCU 的 Chrome 策略（不用管理员），跑完一定还原。
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8899
ORIGIN = "http://127.0.0.1:%d" % PORT
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
POLICY_KEY = r"Software\Policies\Google\Chrome\ScreenCaptureWithoutGestureAllowedForOrigins"
TARGET_TITLE = "TEST-TARGET-TAB-42"

result: dict = {}
done = threading.Event()

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Capture Test</title></head>
<body style="font:14px sans-serif;padding:20px">
<h3>采集测试</h3><pre id="log">准备中…</pre>
<script>
const log = (s) => { document.getElementById('log').textContent += "\\n" + s; };
async function report(o){
  try { await fetch('/result', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(o)}); } catch(e){}
}
setTimeout(async () => {
  log("调用 getDisplayMedia（无用户手势）…");
  const t0 = performance.now();
  try {
    const s = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
    const v = s.getVideoTracks()[0], a = s.getAudioTracks()[0];
    log("成功：video=" + (v && v.label) + " audio=" + (a && a.label));
    report({ ok: true, video: v ? v.label : null, audio: a ? a.label : null,
             hasAudio: !!a, ms: Math.round(performance.now() - t0) });
  } catch (e) {
    log("失败：" + e.name + " " + e.message);
    report({ ok: false, error: e.name + ": " + e.message });
  }
}, 900);
setTimeout(() => report({ ok: false, error: "timeout-8s（多半弹了选源框）" }), 8000);
</script></body></html>"""

TARGET = """<!doctype html><html><head><meta charset="utf-8"><title>%s</title></head>
<body style="font:14px sans-serif;padding:20px">
<h3>这是一个正在播放声音的目标标签页</h3><div id="s">—</div>
<script>
const ctx = new AudioContext();
const osc = ctx.createOscillator(); const g = ctx.createGain();
g.gain.value = 0.03; osc.frequency.value = 440;
osc.connect(g); g.connect(ctx.destination); osc.start();
setInterval(() => {
  document.getElementById('s').textContent = "播放中 " + Math.round(ctx.currentTime) + "s / state=" + ctx.state;
}, 500);
</script></body></html>""" % TARGET_TITLE


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = (PAGE if self.path != "/target" else TARGET).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            result.update(json.loads(self.rfile.read(n).decode("utf-8") or "{}"))
        except ValueError:
            result["error"] = "bad json"
        done.set()
        self.send_response(204)
        self.end_headers()


def set_policy(on: bool) -> str:
    try:
        if on:
            winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, POLICY_KEY, 0, winreg.KEY_WRITE)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, POLICY_KEY, 0, winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "1", 0, winreg.REG_SZ, ORIGIN)
            return "已写入"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Policies\Google\Chrome",
                            0, winreg.KEY_ALL_ACCESS) as k:
            winreg.DeleteKey(k, "ScreenCaptureWithoutGestureAllowedForOrigins")
        return "已删除"
    except FileNotFoundError:
        return "本来就没有"
    except OSError as e:
        return "失败：%s" % e


def free_port(p: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def run_case(label: str, policy: bool, auto_select: bool) -> None:
    result.clear()
    done.clear()
    action = set_policy(policy)
    profile = ROOT / "data" / ("_chrome_probe_" + ("p" if policy else "n") + ("a" if auto_select else "s"))
    args = [CHROME,
            "--user-data-dir=%s" % profile,
            "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            "--window-position=-32000,-32000", "--window-size=900,600"]
    if auto_select:
        args.append('--auto-select-desktop-capture-source=%s' % TARGET_TITLE)
    args += [ORIGIN + "/target", ORIGIN + "/"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    got = done.wait(timeout=20)
    time.sleep(0.4)
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    set_policy(False)

    print("\n== %s ==" % label)
    print("   策略：%s   选源开关：%s" % (action, "开" if auto_select else "关"))
    if not got:
        print("   结果：20 秒内页面没回报（Chrome 起不来？）")
        return
    if result.get("ok"):
        print("   ✅ 成功  视频源=%s" % result.get("video"))
        print("      音频=%s  hasAudio=%s  耗时=%sms" % (
            result.get("audio"), result.get("hasAudio"), result.get("ms")))
    else:
        print("   ❌ 失败  %s" % result.get("error"))


def main() -> int:
    if not free_port(PORT):
        print("端口 %d 被占用" % PORT)
        return 1
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("本地测试页：%s/" % ORIGIN)
    print("目标标签页标题：%s（会自动播放 440Hz）" % TARGET_TITLE)
    try:
        run_case("A 基线（无策略、无选源）", policy=False, auto_select=False)
        run_case("B 只有免手势策略", policy=True, auto_select=False)
        run_case("C 策略 + 自动选源", policy=True, auto_select=True)
    finally:
        print("\n还原策略：", set_policy(False))
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
