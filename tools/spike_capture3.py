# -*- coding: utf-8 -*-
"""验证三：一个 Chrome 实例能否同时/连续抓到 N 个不同标签页。

每个目标标签播不同频率（440/660/880Hz），抓到音频后做 FFT 看主频，
就能知道这一次到底抓的是哪个标签页 —— 比看 stream id 可靠。
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
PORT = 8903
ORIGIN = "http://127.0.0.1:%d" % PORT
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
TARGETS = [("TEST-ROOM-A", 440), ("TEST-ROOM-B", 660), ("TEST-ROOM-C", 880)]

results: dict = {}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>cap3</title></head>
<body><pre id="l">…</pre><script>
const n = +(new URLSearchParams(location.search).get('n') || 3);
async function dominant(stream) {
  const ctx = new AudioContext();
  const src = ctx.createMediaStreamSource(stream);
  const an = ctx.createAnalyser(); an.fftSize = 4096; src.connect(an);
  await new Promise(r => setTimeout(r, 700));
  const buf = new Float32Array(an.frequencyBinCount);
  an.getFloatFrequencyData(buf);
  let mi = 0; for (let i = 1; i < buf.length; i++) if (buf[i] > buf[mi]) mi = i;
  const f = mi * ctx.sampleRate / an.fftSize;
  try { await ctx.close(); } catch(e){}
  return Math.round(f);
}
(async () => {
  const out = [];
  for (let i = 0; i < n; i++) {
    try {
      const s = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      const a = s.getAudioTracks()[0];
      const f = a ? await dominant(s) : 0;
      out.push({ ok: true, freq: f, hasAudio: !!a });
      s.getTracks().forEach(t => t.stop());
    } catch (e) { out.push({ ok: false, error: e.name + ': ' + e.message }); }
  }
  await fetch('/result', {method:'POST', body: JSON.stringify({out})});
})();
setTimeout(() => fetch('/result', {method:'POST', body: JSON.stringify({out: [], timeout: true})}), 25000);
</script></body></html>"""

TARGET = """<!doctype html><html><head><meta charset="utf-8"><title>%s</title></head>
<body style="font:18px sans-serif;padding:30px"><h2>%s · %dHz</h2>
<script>
const ctx = new AudioContext(); const o = ctx.createOscillator(); const g = ctx.createGain();
g.gain.value = 0.05; o.frequency.value = %d;
o.connect(g); g.connect(ctx.destination); o.start();
</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/cap":
            body = PAGE
        elif p.startswith("/t/"):
            i = int(p.split("/")[2])
            name, f = TARGETS[i]
            body = TARGET % (name, name, f, f)
        else:
            body = "<html>hi</html>"
        b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        results.update(json.loads(self.rfile.read(n).decode("utf-8") or "{}"))
        self.send_response(204)
        self.end_headers()


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    urls = [ORIGIN + "/t/%d" % i for i in range(len(TARGETS))]
    urls.append(ORIGIN + "/cap?n=3")
    args = [CHROME, "--user-data-dir=%s" % (ROOT / "data" / "_probe_c3"),
            "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            "--window-position=-32000,-32000", "--window-size=900,600",
            "--auto-select-desktop-capture-source=%s" % TARGETS[0][0]] + urls
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(60):
        time.sleep(0.5)
        if results:
            break
    try:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    srv.shutdown()

    name_of = {440: "TEST-ROOM-A", 660: "TEST-ROOM-B", 880: "TEST-ROOM-C"}
    out = results.get("out") or []
    print("=== 一个实例，3 个目标标签，连抓 3 次 ===")
    if not out:
        print("   没有结果（timeout=%s）" % results.get("timeout"))
        return 1
    seen = []
    for i, r in enumerate(out, 1):
        if r.get("ok"):
            f = r.get("freq")
            near = min(name_of, key=lambda k: abs(k - f))
            label = name_of[near] if abs(near - f) < 60 else "未知(%dHz)" % f
            print("   第%d次：%3dHz -> %s" % (i, f, label))
            seen.append(label)
        else:
            print("   第%d次：%s" % (i, r.get("error")))
    uniq = sorted(set(seen))
    print("\n   抓到 %d 次，不同标签页 %d 个：%s" % (len(seen), len(uniq), "、".join(uniq) or "无"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
