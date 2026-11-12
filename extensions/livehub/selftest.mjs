// offscreen.js 的自检：按 offscreen 文档的真实能力边界（只有 chrome.runtime）
// 把它跑一遍，确认「开始采集 → 送块 → 停止」不会调用越界 API。
//   node extensions/livehub/selftest.mjs
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "offscreen.js"), "utf8");

const calls = [];        // 记录 runtime.sendMessage 的所有调用
const fetches = [];      // 记录 fetch
const nodes = [];        // 记录创建的 AudioWorkletNode，用来模拟出块
let listeners = [];

const chrome = {
  runtime: {
    onMessage: { addListener: (fn) => listeners.push(fn) },
    getURL: (p) => "chrome-extension://fake/" + p,
    sendMessage: async (msg) => {
      calls.push(msg);
      if (msg.type === "mute" || msg.type === "close-tab") return { ok: true };
      return { ok: true };
    }
  }
  // 故意不提供 storage / tabs / notifications —— 与真实 offscreen 一致
};

class FakeCtx {
  constructor(o) { this.sampleRate = (o && o.sampleRate) || 16000; this.state = "running";
    this.audioWorklet = { addModule: async () => {} }; this.destination = {}; }
  createMediaStreamSource() { return { connect() {} }; }
  createGain() { return { gain: { value: 0 }, connect() {} }; }
  close() { this.state = "closed"; return Promise.resolve(); }
}
class FakeNode {
  constructor() { this.port = { onmessage: null }; nodes.push(this); }
  connect() {} disconnect() {}
}

const stream = {
  getAudioTracks: () => [{ onended: null, stop() {} }],
  getTracks: () => [{ stop() {} }]
};
const sandbox = {
  chrome,
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  AudioContext: FakeCtx,
  AudioWorkletNode: FakeNode,
  navigator: { mediaDevices: { getUserMedia: async () => stream } },
  fetch: async (url, opt) => {
    fetches.push({ url, opt });
    if (url.endsWith("/api/health")) return { ok: true };
    if (url.endsWith("/api/live/start")) return { ok: true, status: 201, json: async () => ({ session_id: "LIVE_TEST" }) };
    if (url.endsWith("/api/live/chunk")) return { ok: true, status: 200 };
    if (url.endsWith("/api/live/stop")) return { ok: true, status: 200 };
    return { ok: true, status: 200, json: async () => ({}) };
  },
  ArrayBuffer, Int16Array, Date, Promise, Math, String, Number, JSON, Error
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "offscreen.js" });

const send = (msg) => new Promise((resolve) => {
  for (const fn of listeners) {
    const r = fn(msg, {}, resolve);
    if (r === true) return;   // 异步响应
  }
  resolve(undefined);
});

const ok = (cond, label) => { console.log((cond ? "  ok   " : "  FAIL ") + label); if (!cond) process.exitCode = 1; };

console.log("1) 加载不崩（以前 chrome.storage 在这里直接抛）");
ok(listeners.length > 0, "注册了消息监听");
ok(!/chrome\.storage\./.test(src), "没有调用 chrome.storage.*（注释里提到不算）");
ok(!/chrome\.tabs\./.test(src), "没有调用 chrome.tabs.*（注释里提到不算）");

console.log("2) 握手 + 开始采集");
const ping = await send({ type: "ping" });
ok(ping && ping.ok, "ping 应答");
const started = await send({ type: "start", streamId: "s1", tabId: 7, title: "测试直播间", url: "https://live.test/1" });
ok(started && started.ok, "start 返回 ok");
await new Promise((r) => setTimeout(r, 30));
ok(calls.some((c) => c.type === "mute" && c.muted === false), "开始前先取消静音（muted:false）");
ok(fetches.some((f) => f.url.endsWith("/api/live/start")), "向服务端建了会话");

console.log("3) 出块 → 上行");
nodes[0].port.onmessage({ data: { pcm: new ArrayBuffer(8192), rms: 0.2 } });
await new Promise((r) => setTimeout(r, 50));
const chunk = fetches.find((f) => f.url.endsWith("/api/live/chunk"));
ok(!!chunk, "音频块发到了 /api/live/chunk");
ok(!!chunk && chunk.opt.headers["X-Live-Id"] === "LIVE_TEST", "带着会话 id");

console.log("4) 停止 → 先静音、再关标签页");
const stats1 = await send({ type: "stats" });
ok(stats1.caps.length === 1 && stats1.caps[0].sid === "LIVE_TEST", "stats 里这一路已入库");
await send({ type: "stop", tabId: 7 });
await new Promise((r) => setTimeout(r, 60));
ok(calls.some((c) => c.type === "mute" && c.muted === true), "停止时静音标签页");
ok(calls.some((c) => c.type === "close-tab"), "按设置关闭标签页");
ok(fetches.some((f) => f.url.endsWith("/api/live/stop")), "通知服务端收尾");

console.log("5) 停止后状态");
const stats2 = await send({ type: "stats" });
ok(stats2.caps.length === 0, "活动列表清空");
ok(stats2.dead.length === 1, "断流记录保留（面板可见）");

// offscreen 里有 setInterval（探活、看门狗），Node 下会让进程挂着不退；跑完直接收工
process.exit(process.exitCode || 0);