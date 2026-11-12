# -*- coding: utf-8 -*-
"""验证五（改）：CDP 锁标题 —— 用 defineProperty 覆盖，并把 CDP 返回打出来。"""
from __future__ import annotations
import asyncio, http.server, json, subprocess, threading, time, urllib.request
from pathlib import Path
import websockets

ROOT = Path(__file__).resolve().parent.parent
HTTP_PORT, CDP_PORT = 8912, 9334
ORIGIN = "http://127.0.0.1:%d" % HTTP_PORT
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
MAGIC = "LVN-ROOM-TEST-7"
result: dict = {}

ROOM = """<!doctype html><html><head><meta charset="utf-8"><title>真实标题-会变</title></head>
<body><h2>目标房间 523Hz</h2><script>
const c=new AudioContext(),o=c.createOscillator(),g=c.createGain();
g.gain.value=.05;o.frequency.value=523;o.connect(g);g.connect(c.destination);o.start();
setInterval(()=>{document.title="真实标题-会变 "+(Math.floor(Date.now()/1000)%100)},800);
</script></body></html>"""

CAP = """<!doctype html><html><head><meta charset="utf-8"><title>cap</title></head><body><script>
(async()=>{try{
 const s=await navigator.mediaDevices.getDisplayMedia({video:true,audio:true});
 const a=s.getAudioTracks()[0];let f=0;
 if(a){const ctx=new AudioContext(),src=ctx.createMediaStreamSource(s),an=ctx.createAnalyser();
  an.fftSize=4096;src.connect(an);await new Promise(r=>setTimeout(r,700));
  const b=new Float32Array(an.frequencyBinCount);an.getFloatFrequencyData(b);
  let mi=0;for(let i=1;i<b.length;i++)if(b[i]>b[mi])mi=i;f=Math.round(mi*ctx.sampleRate/an.fftSize);}
 await fetch('/result',{method:'POST',body:JSON.stringify({ok:true,freq:f,hasAudio:!!a})});
}catch(e){await fetch('/result',{method:'POST',body:JSON.stringify({ok:false,error:e.name+': '+e.message})});}})();
</script></body></html>"""

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self):
        body = ROOM if self.path.startswith("/room") else (CAP if self.path.startswith("/cap") else "ok")
        b=body.encode(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n=int(self.headers.get("Content-Length") or 0)
        result.update(json.loads(self.rfile.read(n).decode() or "{}")); self.send_response(204); self.end_headers()

def http_json(u):
    with urllib.request.urlopen(u, timeout=10) as r: return json.loads(r.read().decode())

async def send(ws, method, params=None):
    async with websockets.connect(ws, max_size=None) as s:
        await s.send(json.dumps({"id":1,"method":method,"params":params or {}}))
        while True:
            m=json.loads(await s.recv())
            if m.get("id")==1: return m

async def main():
    srv=http.server.ThreadingHTTPServer(("127.0.0.1",HTTP_PORT),H)
    threading.Thread(target=srv.serve_forever,daemon=True).start()
    p=subprocess.Popen([CHROME,"--user-data-dir=%s"%(ROOT/"data"/"_probe_c6"),
        "--no-first-run","--no-default-browser-check","--autoplay-policy=no-user-gesture-required",
        "--window-position=-32000,-32000","--window-size=800,500",
        "--remote-debugging-port=%d"%CDP_PORT,
        "--auto-select-desktop-capture-source=%s"%MAGIC, ORIGIN+"/room"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    pages=None
    for _ in range(60):
        time.sleep(0.5)
        try:
            pages=http_json("http://127.0.0.1:%d/json/list"%CDP_PORT)
            if pages: break
        except Exception: pass
    page=next((x for x in pages if x.get("type")=="page" and "/room" in (x.get("url") or "")),None)
    if not page: print("找不到房间标签"); p.kill(); return 1

    # 等页面 ready
    for _ in range(30):
        r=await send(page["webSocketDebuggerUrl"],"Runtime.evaluate",
                     {"expression":"document.readyState","returnByValue":True})
        st=r.get("result",{}).get("result",{}).get("value")
        if st=="complete": break
        time.sleep(0.3)
    print("readyState =", st)

    # 直接改 <title> 节点，并用 MutationObserver 立刻改回去（页面自己每 0.8 秒想改回真实标题）
    lock = ("""(() => {
      const T = %s;
      const el = document.querySelector('title') || document.head.appendChild(document.createElement('title'));
      const fix = () => { if (el.textContent !== T) el.textContent = T; };
      new MutationObserver(fix).observe(el, {childList:true, characterData:true, subtree:true});
      new MutationObserver(fix).observe(document.head, {childList:true, subtree:true});
      fix();
      return document.title;
    })()""") % json.dumps(MAGIC)
    r=await send(page["webSocketDebuggerUrl"],"Runtime.evaluate",{"expression":lock,"returnByValue":True})
    print("注入返回:", json.dumps(r.get("result",{}))[:300])

    time.sleep(2.0)
    after=next((x for x in http_json("http://127.0.0.1:%d/json/list"%CDP_PORT) if x.get("id")==page["id"]),{})
    print("2 秒后标题: %r (期望 %r)" % (after.get("title"), MAGIC))

    ver=http_json("http://127.0.0.1:%d/json/version"%CDP_PORT)
    await send(ver["webSocketDebuggerUrl"],"Target.createTarget",{"url":ORIGIN+"/cap"})
    for _ in range(40):
        time.sleep(0.5)
        if result: break
    try: subprocess.run(["taskkill","/PID",str(p.pid),"/T","/F"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except Exception: pass
    srv.shutdown()

    ok_title = after.get("title")==MAGIC
    print("\n=== 结论 ===")
    print("   标题锁定:", "✅" if ok_title else "❌ 实际 %r" % after.get("title"))
    if result.get("ok"):
        print("   自动选源: ✅ 主频=%sHz 音频=%s（目标 523Hz）" % (result.get("freq"), result.get("hasAudio")))
    else:
        print("   自动选源: ❌ %s" % (result.get("error") or "无结果"))
    return 0 if ok_title and result.get("ok") else 1

if __name__=="__main__":
    raise SystemExit(asyncio.run(main()))