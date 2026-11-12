# -*- coding: utf-8 -*-
"""验证四：多个受控 Chrome 实例，各自抓各自的标签页（用频率验明身份）。"""
from __future__ import annotations

import http.server, json, socket, subprocess, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8905
ORIGIN = "http://127.0.0.1:%d" % PORT
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ROOMS = [("TEST-ROOM-X", 440), ("TEST-ROOM-Y", 660)]

results: dict = {}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>cap4</title></head>
<body><script>
const id = new URLSearchParams(location.search).get('id');
async function dominant(stream) {
  const ctx = new AudioContext(); const src = ctx.createMediaStreamSource(stream);
  const an = ctx.createAnalyser(); an.fftSize = 4096; src.connect(an);
  await new Promise(r => setTimeout(r, 700));
  const b = new Float32Array(an.frequencyBinCount); an.getFloatFrequencyData(b);
  let mi = 0; for (let i = 1; i < b.length; i++) if (b[i] > b[mi]) mi = i;
  const f = Math.round(mi * ctx.sampleRate / an.fftSize);
  try { await ctx.close(); } catch(e){}
  return f;
}
(async () => {
  try {
    const s = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
    const a = s.getAudioTracks()[0];
    const f = a ? await dominant(s) : 0;
    await fetch('/result', {method:'POST', body: JSON.stringify({id, ok:true, freq:f, hasAudio:!!a})});
    s.getTracks().forEach(t => t.stop());
  } catch (e) {
    await fetch('/result', {method:'POST', body: JSON.stringify({id, ok:false, error:e.name+': '+e.message})});
  }
})();
</script></body></html>"""

TARGET = """<!doctype html><html><head><meta charset="utf-8"><title>%s</title></head>
<body style="font:18px sans-serif;padding:30px"><h2>%s · %dHz</h2>
<script>
const c = new AudioContext(); const o = c.createOscillator(); const g = c.createGain();
g.gain.value = 0.05; o.frequency.value = %d; o.connect(g); g.connect(c.destination); o.start();
</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/cap":
            body = PAGE
        elif p.startswith("/t/"):
            name, f = ROOMS[int(p.split("/")[2])]
            body = TARGET % (name, name, f, f)
        else:
            body = "<html>hi</html>"
        b = body.encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        d = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        results[d.get("id", "?")] = d
        self.send_response(204); self.end_headers()


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    procs = []
    t0 = time.time()
    for i, (name, _f) in enumerate(ROOMS):
        args = [CHROME, "--user-data-dir=%s" % (ROOT / "data" / ("_probe_e%d" % i)),
                "--no-first-run", "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required",
                "--window-position=-32000,-32000", "--window-size=800,500",
                "--auto-select-desktop-capture-source=%s" % name,
                ORIGIN + "/t/%d" % i, ORIGIN + "/cap?id=E%d" % i]
        procs.append(subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(2.0)          # 错开启动：同时拉起两个 Chrome 会互相抢初始化

    for _ in range(100):
        time.sleep(0.5)
        if len(results) >= len(ROOMS): break
    elapsed = time.time() - t0
    for p in procs:
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception: pass
    srv.shutdown()

    print("=== 两个实例，各自抓各自的房间 ===")
    ok = 0
    for i, (name, f) in enumerate(ROOMS):
        r = results.get("E%d" % i, {})
        if r.get("ok"):
            got = r.get("freq")
            mark = "✅" if abs(got - f) < 60 else "⚠️"
            if mark == "✅": ok += 1
            print("   %s 实例%d 期望 %s(%dHz) -> 实际 %dHz 音频=%s" % (
                mark, i, name, f, got, r.get("hasAudio")))
        else:
            print("   ❌ 实例%d 失败：%s" % (i, r.get("error") or "无结果"))
    print("\n   同时启动到全部出结果用时 %.1f 秒；各抓各的：%d/%d" % (elapsed, ok, len(ROOMS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())