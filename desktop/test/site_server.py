# -*- coding: utf-8 -*-
"""D2/D3 spike 用的本地「直播间」测试站。

- /room/a、/room/b：同一个 origin 的两个房间页，带真实 <video> + 音轨
  （speech.mp4，由 Windows SAPI 合成），用来验证 captureStream 链路；
- 页面加载时写入 demo_login cookie，第二个房间能读到就说明 WebView2
  共享 data_directory 生效（D2 的「一次登录所有房间共用」）；
- /speech.mp4、/speech.wav：静态媒体。

    python desktop/test/site_server.py
"""
from __future__ import annotations

import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PORT = 8899

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>测试直播间 __ROOM__</title>
<style>
  :root{--ink:#1d2330;--muted:#6b7480;--line:#e6e9ef}
  *{box-sizing:border-box}
  body{margin:0;background:#0f1420;color:#e9edf6;
    font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
  .top{display:flex;align-items:center;gap:10px;padding:12px 16px;background:#171e2c;
    border-bottom:1px solid #263149;font-size:13px}
  .dot{width:8px;height:8px;border-radius:50%;background:#e5484d;box-shadow:0 0 0 4px rgba(229,72,77,.18)}
  .grow{flex:1}
  .who{font-size:12px;color:#9fb0cc;background:#111827;border:1px solid #263149;
    padding:4px 9px;border-radius:999px;font-variant-numeric:tabular-nums}
  .who.ok{color:#7ce2b0;border-color:#1d5c43}
  main{max-width:960px;margin:0 auto;padding:18px}
  .player{background:#0b0f18;border:1px solid #263149;border-radius:14px;overflow:hidden}
  video{display:block;width:100%;max-height:58vh;background:#000}
  .meta{display:flex;gap:16px;align-items:center;padding:10px 14px;font-size:12px;color:#9fb0cc;flex-wrap:wrap}
  .badge{font-size:11px;padding:2px 8px;border-radius:999px;background:#2a1620;color:#ff9aa2}
  .hint{margin-top:14px;font-size:11.5px;line-height:1.9;color:#7d8ba3}
  code{color:#9fc3ff}
</style></head>
<body>
<div class="top">
  <span class="dot"></span><b>测试直播间 __ROOM__</b>
  <span class="grow"></span>
  <span class="who" id="who">检查登录…</span>
</div>
<main>
  <div class="player">
    <video id="v" src="/speech.mp4" autoplay loop playsinline controls></video>
    <div class="meta">
      <span class="badge">LIVE · 模拟</span>
      <span>真实的 &lt;video&gt; + AAC 音轨</span>
      <span id="media-state">media: -</span>
    </div>
  </div>
  <div class="hint">
    这个页面故意跑在独立的 origin（<code>127.0.0.1:8899</code>），用来验证两件事：<br>
    1. 远程页面拿不到 Tauri IPC，但注入脚本仍能通过 <code>chrome.webview.postMessage</code> 回传 PCM；<br>
    2. 房间 A/B 共用 WebView2 data_directory，A 写入的 <code>demo_login</code> cookie，B 应能直接读到。
  </div>
</main>
<script>
(function () {
  var room = "__ROOM__";
  var had = document.cookie.indexOf("demo_login=") >= 0;
  if (!had) document.cookie = "demo_login=alice; path=/; max-age=86400; samesite=lax";
  var now = document.cookie || "";
  var who = document.getElementById("who");
  who.textContent = (had ? "已共享登录" : "首次写入登录") + " · " + (now || "(cookie 空)");
  who.className = "who" + (had ? " ok" : "");
  document.body.dataset.cookie = now;
  function report(state) {
    try {
      if (window.chrome && window.chrome.webview) {
        window.chrome.webview.postMessage(JSON.stringify({
          type: "capture_state", room: room, state: state,
          title: document.title, cookie: now
        }));
      }
    } catch (e) {}
  }
  report("login_probe");
  var v = document.getElementById("v");
  v.addEventListener("playing", function () { document.getElementById("media-state").textContent = "media: playing " + v.videoWidth + "x" + v.videoHeight; });
  v.addEventListener("pause", function () { document.getElementById("media-state").textContent = "media: paused"; });
  v.addEventListener("error", function () { document.getElementById("media-state").textContent = "media: error " + (v.error && v.error.code); });
  // 远程页面允许显示自己的状态；真正的采集由壳注入脚本负责。
})();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print("[site] " + fmt % args, flush=True)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/room/"):
            room = (path.rsplit("/", 1)[-1] or "a").upper()
            body = PAGE.replace("__ROOM__", room).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
            return
        if path == "/login":
            self.send_response(302)
            self.send_header("Set-Cookie", "demo_login=alice; Path=/; Max-Age=86400; SameSite=Lax")
            self.send_header("Location", "/room/a")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in ("/speech.mp4", "/speech.wav"):
            target = ROOT / path.lstrip("/")
            if target.is_file():
                self._send_file(target)
            else:
                self._send(404, "text/plain; charset=utf-8", b"missing media; run desktop/test/make_media.ps1")
            return
        self._send(200, "text/html; charset=utf-8",
                   b"<h1>LiveNotes capture test site</h1><p><a href='/room/a'>/room/a</a> | "
                   b"<a href='/room/b'>/room/b</a> | <a href='/room/c'>/room/c</a></p>")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, target: Path) -> None:
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, ctype, data)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[site] http://127.0.0.1:{PORT}/room/a  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
