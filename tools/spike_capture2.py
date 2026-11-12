# -*- coding: utf-8 -*-
"""验证二：一个 Chrome 实例能不能抓多个标签页？多个实例能不能各抓各的？

D 一个实例 + 两个目标标签 + 两次 getDisplayMedia  -> 看两次拿到的是不是同一个源
E 两个实例，各自目标 + 各自开关                    -> 看是不是各抓各的
"""
from __future__ import annotations

import http.server
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8901
ORIGIN = "http://127.0.0.1:%d" % PORT
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
T1, T2 = "TEST-TARGET-ALPHA", "TEST-TARGET-BRAVO"

results: dict = {}
lock = threading.Lock()
got = threading.Event()

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>cap</title></head>
<body><pre id="l">…</pre>
<script>
const id = new URLSearchParams(location.search).get('id') || 'x';
const times = +(new URLSearchParams(location.search).get('n') || 1);
const out = [];
async function report(o){ try{ await fetch('/result',{method:'POST',body:JSON.stringify(Object.assign({id},o))}); }catch(e){} }
(async () => {
  for (let i = 0; i < times; i++) {
    try {
      const s = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      const v = s.getVideoTracks()[0], a = s.getAudioTracks()[0];
      out.push({ ok: true, video: v && v.label, audio: a && a.label, hasAudio: !!a });
    } catch (e) {
      out.push({ ok: false, error: e.name + ': ' + e.message });
    }
  }
  await report({ attempts: out });
})();
setTimeout(() => report({ attempts: out, timeout: true }), 12000);
</script></body></html>"""

TARGET = """<!doctype html><html><head><meta charset="utf-8"><title>%s</title></head>
<body style="font:14px sans-serif;padding:20px"><h3>%s</h3><div id="s">—</div>
<script>
const ctx = new AudioContext(); const osc = ctx.createOscillator(); const g = ctx.createGain();
g.gain.value = 0.03; osc.frequency.value = %d;
osc.connect(g); g.connect(ctx.destination); osc.start();
setInterval(()=>{document.getElementById('s').textContent="playing "+Math.round(ctx.currentTime)+"s"},500);
</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/cap":
            body = PAGE
        elif path == "/a":
            body = TARGET % (T1, T1, 440)
        elif path == "/b":
            body = TARGET % (T2, T2, 660)
        else:
            body = "<html><body>hi</body></html>"
        b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        d = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        with lock:
            results[d.get("id", "?")] = d
            if len(results) >= 1 and "timeout" not in d:
                got.set()
        self.send_response(204)
        self.end_headers()


def launch(extra, urls, profile):
    args = [CHROME, "--user-data-dir=%s" % (ROOT / "data" / profile),
            "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            "--window-position=-32000,-32000", "--window-size=900,600"] + extra + urls
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill(p):
    try:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def main():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
    except OSError:
        print("端口被占用")
        return 1
    finally:
        s.close()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print("=== D 一个实例，两个目标标签，抓两次 ===")
    results.clear(); got.clear()
    p = launch(['--auto-select-desktop-capture-source=%s' % T1],
               [ORIGIN + "/a", ORIGIN + "/b", ORIGIN + "/cap?id=D&n=2"], "_probe_d")
    got.wait(20); time.sleep(1); kill(p)
    d = results.get("D", {})
    for i, a in enumerate(d.get("attempts") or [], 1):
        print("   第%d次：%s" % (i, ("ok 视频=%s 音频=%s" % (a.get("video"), a.get("audio"))) if a.get("ok") else a.get("error")))

    print("\n=== E 两个实例，各自目标、各自开关 ===")
    results.clear(); got.clear()
    p1 = launch(['--auto-select-desktop-capture-source=%s' % T1],
                [ORIGIN + "/a", ORIGIN + "/cap?id=E1&n=1"], "_probe_e1")
    p2 = launch(['--auto-select-desktop-capture-source=%s' % T2],
                [ORIGIN + "/b", ORIGIN + "/cap?id=E2&n=1"], "_probe_e2")
    for _ in range(30):
        time.sleep(0.5)
        if len(results) >= 2:
            break
    kill(p1); kill(p2)
    for k in ("E1", "E2"):
        r = results.get(k, {})
        a = (r.get("attempts") or [{}])[0]
        print("   %s: %s" % (k, ("ok 视频=%s 音频=%s" % (a.get("video"), a.get("audio"))) if a.get("ok") else (a.get("error") or "无结果")))

    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
