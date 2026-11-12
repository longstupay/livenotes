# -*- coding: utf-8 -*-
"""采集代理：把「房间列表」变成一组受控 Chrome 实例，零点击采音。

    python tools/capture_agent.py            # 常驻，守着所有房间
    python tools/capture_agent.py --once     # 只跑一轮（调试用）
    python tools/capture_agent.py --status   # 看每个房间现在什么状态

每个房间一个独立 Chrome：
  独立 profile + 独立调试端口 + 独立标题标记 LVN-<roomId>
  启动参数 --auto-select-desktop-capture-source=LVN-<roomId>
  打开直播间页 -> 用 CDP 把标题钉成 LVN-<roomId> -> 打开 /capture

为什么钉标题：自动选源是按标题匹配的，而直播间的真实标题随时会变
（"正在直播"、下播、改名）。钉成我们自己的不可变标记，匹配就不再依赖外部世界。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:                      # 只有浏览器采集（CDP）用得到；直取/测试环境不装也能导入
    import websockets
except ImportError:       # pragma: no cover - 缺依赖时由 _ws_connect 明确报错
    websockets = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _data_dir() -> Path:
    """和 app/config.py 用同一个数据目录：压测/自测把运行数据放到临时目录时，
    浏览器 profile 也跟着走，否则 N 路测试会污染真实房间的缓存和登录态。"""
    raw = os.environ.get("LIVENOTES_DATA_DIR")
    if not raw:
        return ROOT / "data"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (Path.cwd() / p)


PROFILES = _data_dir() / "profiles"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

# 采集底座：per_room = 每个房间一个 Chrome（默认，稳）；shared = 一个浏览器
# 开多个房间标签页（省内存，实验性）。
CAPTURE_MODE = os.environ.get("LIVENOTES_CAPTURE_MODE", "per_room").strip().lower()

# 共享模式必须能加载扩展。Chrome 正式版从 137 起忽略 --load-extension，
# Edge 和 Chrome for Testing 仍然支持；所以默认优先 Edge，也可用环境变量指定。
SHARED_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome for Testing\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome for Testing\Application\chrome.exe",
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome for Testing" /
        "Application" / "chrome.exe"),
]
EXT_DIR = ROOT / "extensions" / "livehub"
SHARED_PROFILE = PROFILES / "shared"

LOCK_JS = """(() => {
  const T = %s;
  const pick = () => document.querySelector('title') ||
    (document.head && document.head.appendChild(document.createElement('title')));
  const fix = () => { const el = pick(); if (el && el.textContent !== T) el.textContent = T; };
  try {
    const el = pick();
    new MutationObserver(fix).observe(el, {childList:true, characterData:true, subtree:true});
    new MutationObserver(fix).observe(document.head || document.documentElement, {childList:true, subtree:true});
  } catch (e) {}
  fix();
  setInterval(fix, 500);
  return document.title;
})()"""


def magic(room_id: str) -> str:
    return "LVN-" + str(room_id).upper()


# 页面上出现这些文案，基本就是这场直播已经结束了。启动探活和运行中复查共用一份。
# 「服务器开小差了」故意不在这里：它是抖音的通用错误提示，直播间可能明明开着，
# 只是页面/接口临时抽风。把它当"没在播"会让用户看到完全错误的结论。
DEAD_MARKS = ("直播已结束", "主播已下播", "主播暂未开播", "直播已关闭",
              "直播间不存在", "页面不存在")
# 页面加载期间出现过这些文案：不判下播，只把超时原因说清楚，建议重试。
PAGE_ERROR_MARKS = ("服务器开小差了",)

# 运行中复查：每路每 RECHECK_S 秒看一眼页面还在不在播这场直播。
# 连续 END_CONFIRM 次判定为结束才收尾——单次抖动（切清晰度、缓冲、弹窗）不算。
RECHECK_S = float(os.environ.get("LIVENOTES_RECHECK_SECONDS", "60"))
END_CONFIRM = max(1, int(os.environ.get("LIVENOTES_END_CONFIRM", "2")))
# 采集自愈：会话超过这个秒数没有新音频块，且直播间页面还在播，就关掉旧采集页、
# 用同一个 session 重开。20 秒足够区分「页面卡一下」和「真断了」，又远早于
# 服务端 120 秒的收尾阈值——等收尾再救，缺口已经进了 WAV。
RECOVER_AFTER_S = float(os.environ.get("LIVENOTES_CAPTURE_RECOVER_SECONDS", "20"))
RECOVER_COOLDOWN_S = float(os.environ.get("LIVENOTES_CAPTURE_RECOVER_COOLDOWN", "60"))
# 断流自动收尾后，如果页面还在播，最多再自动重启几次；防止取流坏了时无限重开。
RESTART_MAX = max(1, int(os.environ.get("LIVENOTES_CAPTURE_RESTART_MAX", "3")))


# 每个房间一个 Chrome，把它那一路按进程静音：Windows 音量合成器那一层。
# 为什么不用 --mute-audio：那个开关掐的是 Chromium 内部的音频输出，
# 而 getDisplayMedia 抓的就是那一条流——实测恒定音源电平从 0.042 直接变 0.0。
# 系统层的静音在混音器之后，采集拿不到它：实测静音前后电平都是 0.04243。
MUTE_ROOMS = os.environ.get("LIVENOTES_MUTE_ROOMS", "1").lower() not in ("0", "false", "no")


def _profile_arg(cmdline: list[str] | None) -> str:
    """Chrome 命令行里的 --user-data-dir，规范化后用来精确认房间。

    不能拿整条命令行做子串匹配：rm0148201 会命中 rm01482010，多开时容易
    把别人的 Chrome 一起杀掉/静音。
    """
    for arg in cmdline or []:
        s = str(arg)
        if s.lower().startswith("--user-data-dir="):
            v = s.split("=", 1)[1].strip().strip('"')
            return os.path.normcase(str(Path(v)))
    return ""


def _owns_profile(cmdline: list[str] | None, profile: Path) -> bool:
    return _profile_arg(cmdline) == os.path.normcase(str(profile))


def _audio_sessions(profile: Path) -> list:
    """属于这个房间那个 Chrome 实例的全部音频会话。

    按命令行里的 --user-data-dir 精确认，不按进程名认：五个 Chrome 都叫 chrome.exe。
    pycaw 没装就返回空——静音失败不该让采集跟着挂掉。
    """
    try:
        import psutil
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return []
    pids = set()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "chrome.exe":
                continue
            if _owns_profile(p.info["cmdline"], profile):
                pids.add(p.info["pid"])
        except Exception:
            continue
    if not pids:
        return []
    out = []
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.pid in pids:
                out.append(s)
    except Exception:
        return []
    return out


def kill_profile(profile: Path) -> int:
    """杀掉还在用这个 profile 的 Chrome。

    一个房间只该有一个浏览器：上一次的实例还活着时再启动，新进程只会把标签页
    塞进旧实例然后自己退出——调试端口、采集页、会话全对不上，看起来就是
    "启动了但没在采"。点「重试」时最常撞见。
    """
    try:
        import psutil
    except Exception:
        return 0
    n = 0
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "chrome.exe":
                continue
            if _owns_profile(p.info["cmdline"], profile):
                p.kill()
                n += 1
        except Exception:
            continue
    return n


def mute_room(profile: Path, on: bool = True) -> int:
    n = 0
    for s in _audio_sessions(profile):
        try:
            s.SimpleAudioVolume.SetMute(1 if on else 0, None)
            n += 1
        except Exception:
            pass
    return n


# 加载中/失败时浏览器会拿整条 URL 当标题；平台的通用标题也不是房间名
_URLISH = re.compile(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/|$)", re.I)
_GENERIC = {
    "抖音直播电脑版_抖音直播网页版入口_抖音直播", "抖音直播", "快手直播",
    "哔哩哔哩直播", "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili",
}


_SUFFIX = re.compile(r"\s*[-–—|]\s*(抖音直播|快手直播|哔哩哔哩直播|B站直播)\s*$")


def clean_title(t: str) -> str:
    """把标签标题洗成"房间名"：挡掉 URL 和平台通用标题，去掉站点后缀。

    只砍已知的平台后缀，不能按 "-" 切：「示例ai落地派-企业AI赋能ai智能体」这种
    名字里本来就有连字符，一切就只剩前半截。
    """
    t = (t or "").strip()
    if not t or _URLISH.match(t) or t in _GENERIC or t.startswith("LVN-"):
        return ""
    return _SUFFIX.sub("", t).strip()


# 短链/reflow 页在浏览器里最终会落到这个形状。落地后回报给服务端，
# 房间列表里就能看到真实房号；重试时也直接用真实房号，不必再赌一次跳转。
_LIVE_URL = re.compile(r"^(https?://live\.douyin\.com)/(\d{5,})(?:[/?#]|$)", re.I)


def canonical_live_url(url: str) -> str:
    m = _LIVE_URL.match((url or "").strip())
    if not m:
        return ""
    return "https://live.douyin.com/" + m.group(2)


# 运行中复查用的一次性探针。只认「页面上最大的那个 video」= 主画面：
# 抖音下播后常常自动放推荐流，侧栏缩略图也会自己播，把它们都算进来就会误判。
RECHECK_JS = """(() => {
  const ok = x => !!x && !x.paused && !x.ended && x.readyState >= 2;
  const vs = Array.from(document.querySelectorAll('video'));
  const as = Array.from(document.querySelectorAll('audio'));
  let main = null, best = 0;
  for (const v of vs) {
    let r = {width: 0, height: 0};
    try { r = v.getBoundingClientRect(); } catch (e) {}
    const a = (r.width | 0) * (r.height | 0);
    if (a > best) { best = a; main = v; }
  }
  return JSON.stringify({
    playing: vs.filter(ok).length + as.filter(ok).length,
    main: main ? {
      area: best, playing: ok(main), paused: !!main.paused, ended: !!main.ended,
      dur: isFinite(main.duration) ? Math.round(main.duration) : -1
    } : null,
    url: location.href || '',
    text: (document.body ? document.body.innerText : '').slice(0, 2500)
  });
})()"""


def judge_ended(probe: dict, room: dict) -> str:
    """一次探针的结果 -> 「这场直播结束了」的原因；还在播就返回 ""。

    依据按可靠性排：
      1. 主画面在放「有头有尾」的视频。直播流是 MSE/MediaStream，duration 是
         Infinity；推荐流/点播是固定时长。这是抖音下播后「还在响」最典型的样子。
      2. 页面上写着「直播已结束」这类文案，而且没有在播的东西（不弹推荐时如此）。
      3. 抖音房间的地址已经不在 live.douyin.com/<房号>（跳到首页/推荐页）。
    其余情况（例如只是这一秒没在播）交给调用方按次数判定。
    """
    main = probe.get("main") or {}
    playing = int(probe.get("playing") or 0)
    text = str(probe.get("text") or "")
    url = str(probe.get("url") or "")
    # 「主画面」= 页面上最大的那个 video。侧栏缩略图也算 video，不能拿它们当证据。
    main_playing = bool(main.get("playing")) and int(main.get("area") or 0) >= 60000

    if main_playing:
        dur = main.get("dur")
        if isinstance(dur, (int, float)) and 0 < dur < 4 * 3600:
            return "主画面放的是有头有尾的视频（推荐流 / 点播），不是直播流"
    else:
        for m in DEAD_MARKS:
            if m in text:
                return "页面显示「%s」" % m
    want = canonical_live_url(str(room.get("resolved_url") or room.get("url") or ""))
    if want and url and not canonical_live_url(url):
        return "页面已经离开直播间（%s）" % url[:80]
    return ""


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).is_file():
            return p
    raise SystemExit("找不到 Chrome / Edge")


def find_shared_browser() -> str:
    """共享模式用的浏览器：必须支持 --load-extension。"""
    env = os.environ.get("LIVENOTES_SHARED_BROWSER", "").strip().strip('"')
    if env:
        if Path(env).is_file():
            return env
        raise SystemExit("LIVENOTES_SHARED_BROWSER 指向的文件不存在：%s" % env)
    for p in SHARED_BROWSER_CANDIDATES:
        if Path(p).is_file():
            return p
    raise SystemExit(
        "共享采集模式需要 Microsoft Edge（Chrome 正式版已禁用 --load-extension）。\n"
        "也可以装 Chrome for Testing，再用 LIVENOTES_SHARED_BROWSER 指定 chrome.exe 路径。")


def extension_id(profile: Path, ext_dir: Path, timeout: float = 20.0) -> str:
    """从浏览器自己的 Secure Preferences 里读出未打包扩展的 ID。

    --load-extension 加载的扩展 ID 是按绝对路径算的，不能硬编码；浏览器会把它
    连同 path 一起写进 Secure Preferences。用 path 反查最稳。
    """
    want = os.path.normcase(os.path.abspath(str(ext_dir)))
    deadline = time.time() + timeout
    while time.time() < deadline:
        for name in ("Secure Preferences", "Preferences"):
            f = profile / "Default" / name
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            settings = ((j.get("extensions") or {}).get("settings") or {})
            for eid, val in settings.items():
                p = str(val.get("path") or "")
                if not p:
                    continue
                try:
                    got = os.path.normcase(os.path.abspath(p))
                except Exception:
                    continue
                if got == want:
                    return eid
        time.sleep(0.5)
    return ""


def devtools_port(profile: Path, timeout: float = 15.0) -> int:
    """Chrome 实际监听的调试端口：读它自己写的 DevToolsActivePort，别猜。

    为什么不用 free_port() 的结果：端口是先 bind 再关掉拿到的，中间有可能被抢；
    Chrome 遇到端口被占时会另外挑一个，于是"我以为的端口"和"真的端口"对不上，
    代理就一直连不上，卡在"页面没加载出来"。这个文件才是唯一真相。
    """
    f = profile / "DevToolsActivePort"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            first = f.read_text(encoding="utf-8").splitlines()[0].strip()
            if first.isdigit():
                return int(first)
        except Exception:
            pass
        time.sleep(0.2)
    return 0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_json(url: str, method: str = "GET", timeout: float = 3.0):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode() or "{}"
    return json.loads(body)


class ServiceError(RuntimeError):
    """本地服务不可用/返回异常。

    单独一个类型是为了把"连不上本机 8765"和"抖音页面没开播"彻底分开：
    前者是代理自己的运行环境问题，不能让人误以为是抖音在拦。
    """


def _conn_hint(reason: str) -> str:
    low = (reason or "").lower()
    if "10061" in reason or "refused" in low or "积极拒绝" in reason:
        return ("服务没起 / 端口被别的进程占了 / 本机安全软件或代理没放行这个进程；"
                "先看服务窗口，再 netstat -ano | findstr :8765")
    if "10060" in reason or "timed out" in low or "超时" in reason:
        return "服务没响应：看服务窗口是否卡住，或防火墙拦了回环"
    if "10054" in reason or "reset" in low or "重置" in reason:
        return "连接被重置：可能是服务刚重启，也可能是安全软件拦截"
    return "确认服务窗口还在、8765 只被一个进程监听"


def api(base: str, path: str, payload: dict | None = None, method: str = "GET",
        timeout: float = 8.0):
    """请求本地服务。失败时把 URL、WinError、排查提示一起抛出来。

    旧版只抛 urllib 的裸异常，日志里只剩 "连不上服务（<urlopen error ...>）"，
    既不知道请求的是哪个地址，也不知道该查什么。
    """
    url = base.rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300].strip()
        except Exception:
            pass
        raise ServiceError("HTTP %s %s：%s%s" % (
            e.code, (e.reason or "").strip(), url,
            (" " + detail) if detail else "")) from e
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", None) or e)
        raise ServiceError("连不上 %s：%s（%s）" % (
            url, reason, _conn_hint(reason))) from e
    except (TimeoutError, socket.timeout) as e:
        raise ServiceError("请求超时 %s（%.1f 秒）：%s" % (
            url, timeout, _conn_hint(str(e)))) from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise ServiceError("返回的不是 JSON：%s（%r）" % (url, body[:200])) from e


def _ws_connect(ws_url: str, **kw):
    """连 CDP 的 WebSocket。缺 websockets 时给一句能照做的错误。"""
    if websockets is None:  # pragma: no cover - 只在没装依赖时走到
        raise RuntimeError("浏览器采集需要 websockets：pip install websockets")
    return websockets.connect(ws_url, **kw)


async def cdp_lock(ws_url: str, js: str, install: bool = False):
    """在页面里装标题锁：先跑一遍（当前文档），再挂到"每次新文档"上（页面内跳转）。

    addScriptToEvaluateOnNewDocument 只在第一次调，重复注册会叠出好几份 setInterval。
    """
    async with _ws_connect(ws_url, max_size=None, open_timeout=3) as ws:
        if install:
            await ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
            await ws.send(json.dumps({"id": 2,
                                      "method": "Page.addScriptToEvaluateOnNewDocument",
                                      "params": {"source": js}}))
        await ws.send(json.dumps({"id": 9, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "returnByValue": True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
            if m.get("id") == 9:
                return m


async def cdp_eval(ws_url: str, expression: str, timeout: float = 8.0):
    async with _ws_connect(ws_url, max_size=None, open_timeout=3) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expression, "returnByValue": True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if m.get("id") == 1:
                return m


async def cdp_eval_await(ws_url: str, expression: str, timeout: float = 12.0):
    """等 Promise 结果的 Runtime.evaluate。

    共享模式在面板页里调 chrome.runtime.sendMessage，返回的是 Promise；
    不设 awaitPromise 的话拿到的是 {}，不是真正的结果。
    """
    async with _ws_connect(ws_url, max_size=None, open_timeout=3) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expression,
                                             "returnByValue": True,
                                             "awaitPromise": True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if m.get("id") == 1:
                return m


class Agent:
    adopt_on_start = False
    # 上报给房间的采集引擎名；direct/shared 子类各自覆盖。
    engine = "per_room"

    def __init__(self, base: str, chrome: str | None = None,
                 need_browser: bool = True) -> None:
        self.base = base
        # direct 模式不需要浏览器：不能在这里 find_chrome()，否则只装了
        # ffmpeg/streamlink 的机器会在启动阶段就被 SystemExit 拦下。
        self.chrome = chrome or (find_chrome() if need_browser else "")
        self.running: dict[str, dict] = {}   # room_id -> {proc, port, profile, started, locked}
        # bring_up 一个房间可能几十秒（等页面、等音频会话），这段时间不能
        # 让其它房间的心跳断掉，否则 N 路时会互相把对方"看死"。
        self._beat_lock = threading.Lock()
        self._stop = threading.Event()
        self._beat_thread = threading.Thread(
            target=self._beat_loop, name="capture-heartbeat", daemon=True)
        self._beat_thread.start()

    def _beat_loop(self) -> None:
        while not self._stop.wait(5.0):
            for rid in list(self.running):
                with self._beat_lock:
                    info = self.running.get(rid)
                    if info is None:      # 这一轮刚好被 reap 掉了
                        continue
                    self._beat(rid, True, info)

    # ---------------------------------------------------------------- 上报
    def report(self, rid: str, state: str | None = None, stage: int | None = None,
               note: str | None = None, error: str | None = None,
               failed_stage: int | None = None, name: str | None = None,
               url: str | None = None, auto_stopped: bool | None = None,
               auto_stop_reason: str | None = None,
               session_id: str | None = None,
               engine: str | None = None,
               switch_ready: bool | None = None,
               switch_engine: str | None = None,
               switch_reason: str | None = None,
               switch_requested: bool | None = None,
               capture_event: dict | None = None) -> None:
        """把启动阶段写回服务端。网络抖动不该拖慢拉起流程，失败就算了。"""
        body: dict = {"id": rid, "source": "agent"}
        if name is not None:
            body["name"] = name
        if url is not None:
            body["url"] = url
        if session_id is not None:
            body["session_id"] = session_id
        if engine is None:
            engine = self.engine
        if engine:
            body["engine"] = engine
        if switch_ready is not None:
            body["switch_ready"] = bool(switch_ready)
        if switch_engine is not None:
            body["switch_engine"] = str(switch_engine)[:20]
        if switch_reason is not None:
            body["switch_reason"] = str(switch_reason)[:300]
        if switch_requested is not None:
            body["switch_requested"] = bool(switch_requested)
        if capture_event is not None:
            body["capture_event"] = capture_event
        if auto_stopped is not None:
            body["auto_stopped"] = bool(auto_stopped)
        if auto_stop_reason is not None:
            body["auto_stop_reason"] = auto_stop_reason[:200]
        if state:
            body["state"] = state
        if stage is not None:
            body["stage"] = int(stage)
        if note is not None:
            body["stage_note"] = note
        if error is not None:
            body["error"] = error
        if failed_stage is not None:
            body["failed_stage"] = int(failed_stage)
        try:
            api(self.base, "/api/rooms/state", body, timeout=2.5)
        except Exception:
            pass

    def apply_config(self, cfg: dict) -> None:
        """服务端下发的采集配置。默认引擎没有可热更新的项。"""

    def beat(self, rid: str, alive: bool, info: dict | None = None,
             reason: str = "") -> None:
        """浏览器存活心跳。只进服务端内存，不落房间文件。"""
        with self._beat_lock:
            self._beat(rid, alive, info, reason)

    def _beat(self, rid: str, alive: bool, info: dict | None = None,
              reason: str = "") -> None:
        """调用方必须已经持有 _beat_lock：reap 的 alive=False 要排在
        正在飞的心跳后面，不能被一条迟到的 True 又写回"活着"。"""
        body: dict = {"id": rid, "alive": bool(alive)}
        if info:
            proc = info.get("proc")
            body["pid"] = proc.pid if proc is not None else None
            body["adopted"] = bool(info.get("adopted"))
        if reason:
            body["reason"] = reason[:120]
        try:
            api(self.base, "/api/capture/status", body, timeout=2.5)
        except Exception:
            pass

    # ---------------------------------------------------------------- 启动
    def bring_up(self, room: dict, session: str = "") -> None:
        rid = room["id"]
        self.report(rid, "starting", stage=1, note="正在启动采集浏览器",
                    session_id=session or None)
        port = free_port()
        profile = PROFILES / rid
        profile.mkdir(parents=True, exist_ok=True)
        old = kill_profile(profile)
        if old:
            print("[%s] 清掉残留的 %d 个 Chrome 进程" % (rid, old), flush=True)
            time.sleep(1.0)
        # 静音的事不在这里做：--mute-audio 会把 Chromium 的音频输出整条掐掉，
        # 而 getDisplayMedia 抓的正是这条流——实测恒定音源电平直接变 0.0。
        # 真正管用的是系统那一层：按进程静音（见 mute_process / set_muted）。
        args = [self.chrome,
                "--user-data-dir=%s" % profile,
                "--no-first-run", "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required",
                "--window-size=1024,720",
                "--remote-debugging-port=%d" % port,
                "--auto-select-desktop-capture-source=%s" % magic(rid),
                # 采集页最怕被 Chrome 当成后台标签冻住/回收：关掉后台节流、
                # 遮挡降级和 Memory Saver。机器内存紧时尤其重要——Chrome 会
                # 优先拿「不出声」的页面开刀，正好是我们这一页。
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling",
                "--disable-features=CalculateNativeWinOcclusion,MemorySaver",
                room["url"]]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        real = devtools_port(profile)
        if real and real != port:
            print("[%s] 端口被占，Chrome 自己换了 %d -> %d" % (rid, port, real), flush=True)
            port = real
        self.running[rid] = {"proc": proc, "port": port, "profile": profile,
                             "started": time.time(), "locked": False, "cap_opened": False}
        print("[%s] 启动 Chrome pid=%s port=%d" % (rid, proc.pid, port), flush=True)
        self.beat(rid, True, self.running[rid])
        if not port:
            self.fail(rid, "浏览器没起来（没有调试端口）", 1)
            return

        self.report(rid, "starting", stage=2, note="正在打开直播间页面",
                    session_id=session or None)
        page = self.wait_page(port, "/", exclude="/capture")
        if not page:
            self.fail(rid, "浏览器起来了但页面没加载出来", 2)
            return

        why = self.probe_playing(rid, page)
        if why:
            self.fail(rid, why, 2)
            return

        # 名字放在探针之后读：这时候页面已经渲染完，标题才是"某某的抖音直播间"。
        # 拿不到名字说明页面不是直播间（或没加载出来）——不能带着空名字继续录，
        # 否则坏链接会一直占着一个"启动中"的房间，还白开一个采集页。
        name = self.wait_title(port, page.get("id"), 8.0)
        if not name:
            name = clean_title(room.get("name") or "")
        if not name:
            self.fail(rid, "页面没有给出房间名（可能不是直播间，或页面没加载出来）", 2)
            return
        if name != room.get("name"):
            self.report(rid, name=name[:60])
            room["name"] = name

        # 短链 / reflow 落地页在浏览器里最终会跳到 live.douyin.com/<房号>。
        # 把这个真实地址回报给服务端：列表里显示对房号，重试也不用再赌跳转。
        final_url = self.page_url(port, page.get("id"))
        resolved = canonical_live_url(final_url)
        if resolved and resolved != room.get("url"):
            self.report(rid, url=resolved)
            room["resolved_url"] = resolved

        if not self.lock_title(rid, page):
            self.fail(rid, "标题锁定失败（自动选源可能匹配不上）", 2)
            return

        cap_url = "%s/capture?room=%s&title=%s" % (
            self.base, rid, urllib.parse.quote(name or "未命名房间"))
        if session:
            # 断流重建：让采集页续接原来的 session，缺口留在一场里，不拆成两场。
            cap_url += "&session=" + urllib.parse.quote(session)
        self.report(rid, "starting", stage=3, note="已确认开播，正在打开采集页",
                    session_id=session or None)
        if not self.open_capture(rid, port, cap_url):
            self.fail(rid, "采集页没打开（自动选源可能匹配不上）", 3)
            return
        print("[%s] 标题已锁定 %s，采集页已打开（房间名：%s）" % (rid, magic(rid), name), flush=True)

        # 静音：等音频会话出现再按，最多试 20 秒。
        # 必须在 bring_up 里做——这个函数一跑就是几十秒，tick 根本轮不到。
        if MUTE_ROOMS:
            for _ in range(10):
                time.sleep(2)
                got = mute_room(profile)
                if got:
                    self.running[rid]["muted"] = True
                    print("[%s] 已静音（%d 路音频会话，不影响采集）" % (rid, got), flush=True)
                    break

    def wait_page(self, port: int, need: str, exclude: str = "") -> dict | None:
        for _ in range(60):
            time.sleep(0.4)
            try:
                pages = http_json("http://127.0.0.1:%d/json/list" % port, timeout=2)
            except Exception:
                continue
            for p in pages:
                if p.get("type") != "page":
                    continue
                u = p.get("url") or ""
                if need not in u or (exclude and exclude in u):
                    continue
                if u.startswith("chrome://"):
                    continue
                return p
        return None

    def wait_title(self, port: int, page_id: str, timeout: float = 12.0) -> str:
        """等页面把真名字写进标题。SPA 刚打开时 title 是空串或整条 URL。

        踩过的坑：加载中的标签标题就是 URL（live.douyin.com/123456789015），
        原来只挡了以 http 开头的，于是把 URL 当成房间名存了下来——飞书里就是
        "✅ 开好了：live.douyin.com/123456789015"。现在按形状挡，并且去掉
        "… - 抖音直播" 这种后缀。
        """
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                pages = http_json("http://127.0.0.1:%d/json/list" % port, timeout=2)
                cur = next((p for p in pages if p.get("id") == page_id), {})
                last = str(cur.get("title") or "")
                name = clean_title(last)
                if name:
                    return name
            except Exception:
                pass
            time.sleep(0.5)
        if last:
            print("[名字] 等不到可用的标题，最后看到：%r" % last[:60], flush=True)
        return ""

    def page_url(self, port: int, page_id: str) -> str:
        """标签页现在的真实地址（短链跳转后就是最终地址）。"""
        try:
            for p in http_json("http://127.0.0.1:%d/json/list" % port, timeout=2):
                if p.get("id") == page_id:
                    return str(p.get("url") or "")
        except Exception:
            pass
        return ""

    def lock_title(self, rid: str, page: dict) -> bool:
        """把标签标题钉成 LVN-<房间号>，直到浏览器侧真的读到它。

        踩过的坑：原来要求 document.readyState == "complete" 才注入。抖音直播页
        永远到不了 complete（长连接/流式资源一直挂着），于是永远不注入，标题永远
        抢不过来，报"标题锁定失败"。现在不看 readyState：
          - 注入的动作本身就能建 <title>（没有就 append 一个）；
          - setInterval(fix, 500) 不依赖 observer 挂在哪，head 被重建也能补回来；
          - 再挂一条 addScriptToEvaluateOnNewDocument，页面内跳转后自动重装。
        """
        magic_title = magic(rid)
        port = self.running[rid]["port"]
        js = "%s;" % (LOCK_JS % json.dumps(magic_title))
        for attempt in range(20):
            try:
                asyncio.run(cdp_lock(page["webSocketDebuggerUrl"], js, install=(attempt == 0)))
            except Exception as e:
                if attempt == 0:
                    print("[%s] 注入异常: %s" % (rid, e), flush=True)
            time.sleep(0.5)
            try:
                pages = http_json("http://127.0.0.1:%d/json/list" % port, timeout=2)
                cur = next((p for p in pages if p.get("id") == page.get("id")), {})
                if (cur.get("title") or "") == magic_title:
                    self.running[rid]["locked"] = True
                    return True
            except Exception:
                pass
        return False

    def probe_playing(self, rid: str, page: dict, timeout: float = 30.0) -> str:
        """页面里到底有没有在播。返回 "" 表示视频/音频真的在播，否则是给人看的原因。

        为什么需要这一步：标题钉住了不等于有声音。房间下播后，抖音会把页面渲染成
        推荐流，照样能建会话、照样拿到一条静音的音频轨，于是房间显示"监听中"却
        一句都识别不出来——比报错更让人困惑。

        为什么不能只看 <video> 个数：抖音的「直播加载中」页和错误页也挂着一个空
        <video>（readyState=0、paused=true）。旧逻辑把这种页面当开播，于是坏链接
        一路走到"采集页已打开"，占着房间却一句都录不到。现在必须真的在播才算过。
        """
        dead_mark = DEAD_MARKS
        deadline = time.time() + timeout
        seen_text = ""
        seen_title = ""
        seen_url = ""
        seen_page_error = ""
        while time.time() < deadline:
            js = (
                "(() => {"
                " const ok = x => !!x && !x.paused && !x.ended && x.readyState >= 2;"
                " const vs = Array.from(document.querySelectorAll('video'));"
                " const as = Array.from(document.querySelectorAll('audio'));"
                " return JSON.stringify({"
                "  n: vs.length, an: as.length,"
                "  playing: vs.filter(ok).length + as.filter(ok).length,"
                "  title: document.title || '', url: location.href || '',"
                "  text: (document.body ? document.body.innerText : '').slice(0, 2500)"
                " }); })()"
            )
            try:
                # 单次 eval 给短超时：页面卡住时不能让一次 8 秒的等待吃掉
                # 整个 30 秒窗口，否则明明 8 秒就开始播了也会被判失败。
                r = asyncio.run(cdp_eval(page["webSocketDebuggerUrl"], js, timeout=3.0))
                raw = (r.get("result", {}).get("result") or {}).get("value") or "{}"
                info = json.loads(raw)
            except Exception:
                info = {}
            if info.get("playing", 0) > 0:
                return ""
            seen_text = str(info.get("text") or "")
            seen_title = str(info.get("title") or "")
            seen_url = str(info.get("url") or "")
            for m in dead_mark:
                if m in seen_text:
                    return "直播间没在播（页面显示「%s」）" % m
            for m in PAGE_ERROR_MARKS:
                if m in seen_text:
                    seen_page_error = m
            time.sleep(1.5)
        if seen_page_error:
            # 别把临时错误说成下播：直播间可能开着，等一会儿重试就好。
            return ("抖音页面返回「%s」（临时故障，不一定是没开播）；"
                    "稍后点「重试」" % seen_page_error)
        if "直播加载中" in seen_text:
            return "一直停在「直播加载中」：这个直播间取不到流（可能已下播/链接失效/房间异常）"
        if not seen_text and not seen_title:
            return "页面 %.0f 秒都没渲染出内容（可能没开播，或被网络/风控挡住）" % timeout
        hint = seen_title or seen_url or "（没有标题）"
        return "页面 %.0f 秒没有在播放的视频/音频（标题：%s）" % (timeout, hint[:60])

    # ---------------------------------------------------------------- 运行中复查
    def live_page(self, rid: str, port: int) -> dict | None:
        """这个房间的直播标签页：标题被钉成 LVN-<rid> 的那一个（跳过采集页）。"""
        try:
            pages = http_json("http://127.0.0.1:%d/json/list" % port, timeout=2)
        except Exception:
            return None
        want = magic(rid)
        others = []
        for p in pages:
            if p.get("type") != "page":
                continue
            u = str(p.get("url") or "")
            if "/capture" in u or u.startswith("chrome://"):
                continue
            if str(p.get("title") or "") == want:
                return p
            others.append(p)
        # 标题锁可能被页面整页重建冲掉：退一步用「唯一那个不是采集页的标签」
        return others[0] if len(others) == 1 else None

    def probe_page(self, rid: str, info: dict) -> dict | None:
        port = int(info.get("port") or 0)
        if not port:
            return None
        page = self.live_page(rid, port)
        if not page:
            return None
        try:
            r = asyncio.run(cdp_eval(page["webSocketDebuggerUrl"], RECHECK_JS, timeout=3.0))
            raw = (r.get("result", {}).get("result") or {}).get("value") or "{}"
            return json.loads(raw)
        except Exception:
            return None            # 页面正在跳转 / 已经关掉：这一轮不下结论

    def recheck_end(self, room: dict, rid: str, info: dict) -> None:
        """直播间还在播吗？

        抖音下播后页面经常自动放推荐流，标签页的音频轨根本不会断——采集页会
        一直"录"下去，把别人的视频当成这一场转写。启动时那次探活管不到这里，
        所以运行中要按 RECHECK_S 定期再看一眼，连 END_CONFIRM 次才收尾。
        """
        if room.get("state") != "live":
            return
        now = time.time()
        if now - float(info.get("checked_at") or 0.0) < RECHECK_S:
            return
        info["checked_at"] = now
        probe = self.probe_page(rid, info)
        if probe is None:
            return
        why = judge_ended(probe, room)
        if not why and not int(probe.get("playing") or 0):
            why = "页面没有在播放的视频/音频"
        if not why:
            info["end_hits"] = 0
            return
        info["end_hits"] = int(info.get("end_hits") or 0) + 1
        print("[%s] 下播判断 %d/%d：%s" % (rid, info["end_hits"], END_CONFIRM, why),
              flush=True)
        if info["end_hits"] >= END_CONFIRM and not info.get("ended"):
            info["ended"] = True       # 上报失败（服务抖动）时别每轮都再来一遍
            self.end_room(rid, room, why)

    def end_room(self, rid: str, room: dict, why: str) -> None:
        """判定这一场结束了：先落房间（采集页 5 秒内会看到并自己收手），
        再把会话按正常流程停掉。浏览器由 tick 的 stopped 分支回收。"""
        print("[%s] 直播间已下播，自动收尾：%s" % (rid, why), flush=True)
        self.report(rid, "stopped", stage=6, note="自动收尾：" + why,
                    auto_stopped=True, auto_stop_reason=why)
        sid = str(room.get("session_id") or "")
        if sid:
            # 浏览器是被 taskkill 回收的，采集页来不及发 beforeunload，
            # 这里替它发：WAV 落盘、排队总结、发本场回顾。
            threading.Thread(target=self._stop_session, args=(sid,),
                             name="end-" + rid, daemon=True).start()

    def check_capture(self, room: dict, rid: str, info: dict) -> None:
        """块停了但直播间还在播：重开采集页，别等 120 秒收尾。

        服务端看的是「最后一块音频」的时间，代理看的是「页面还在不在播」；
        两边合起来才能区分「采集链路断了」和「真下播」。每 10 秒最多问一次
        服务端，避免每轮 tick 都打接口。
        """
        if room.get("state") != "live" or info.get("ended"):
            return
        sid = str(room.get("session_id") or "")
        if not sid:
            return
        now = time.time()
        if now - float(info.get("chunk_checked_at") or 0.0) < 10.0:
            return
        info["chunk_checked_at"] = now
        if now - float(info.get("recovered_at") or 0.0) < RECOVER_COOLDOWN_S:
            return
        try:
            st = api(self.base, "/api/live/status?id=" + urllib.parse.quote(sid))
        except Exception:
            return
        if not st.get("running"):
            return
        raw = st.get("last_chunk_ago_s")
        idle = float(raw) if raw is not None else 1e9
        if idle < RECOVER_AFTER_S:
            # 采集恢复正常：重启预算重新计，下一次真断了还有 3 次机会。
            info["auto_restarts"] = 0
            return
        probe = self.probe_page(rid, info)
        if probe is None:
            return                       # 页面正在跳转/关掉：下一轮再看
        if judge_ended(probe, room):
            return                       # 真下播了，交给 recheck_end 按次数收尾
        self.recover_capture(room, rid, info, idle)

    def recover_capture(self, room: dict, rid: str, info: dict, idle: float) -> None:
        """关掉卡死的采集页，用同一个 session 重开；直播间页面不动。"""
        sid = str(room.get("session_id") or "")
        info["recovered_at"] = time.time()
        print("[%s] 采集断流 %.0f 秒，重建采集页（会话 %s）" % (rid, idle, sid),
              flush=True)
        self.report(rid, "live", stage=6,
                    note="采集断流，正在重建采集（缺口约 %d 秒）" % int(idle))
        port = int(info.get("port") or 0)
        if not port:
            self.bring_up(room, session=sid)
            return
        try:
            pages = http_json("http://127.0.0.1:%d/json/list" % port, timeout=2)
        except Exception:
            pages = []
        has_live_page = False
        closed = False
        for p in pages:
            if p.get("type") != "page":
                continue
            u = str(p.get("url") or "")
            if "/capture" in u:
                # 先告诉旧采集页「这次关闭是重建，不要 stop 会话」，再关标签页。
                # 否则 beforeunload 可能把我们要续接的那一场停掉。
                ws = str(p.get("webSocketDebuggerUrl") or "")
                if ws:
                    try:
                        asyncio.run(cdp_eval(
                            ws, "window.__keepSessionOnUnload=true; true", timeout=2.0))
                    except Exception:
                        pass
                try:
                    urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/close/%s" % (port, p["id"]),
                        timeout=2).read()
                    closed = True
                except Exception:
                    pass
            elif u.startswith(("http://", "https://")):
                has_live_page = True
        if not has_live_page:
            # 直播间页也没了：整个浏览器重拉，带上原 session。
            self.bring_up(room, session=sid)
            return
        cap_url = "%s/capture?room=%s&title=%s&session=%s" % (
            self.base, rid, urllib.parse.quote(room.get("name") or "未命名房间"),
            urllib.parse.quote(sid))
        self.open_capture(rid, port, cap_url)
        if not closed:
            print("[%s] 没找到旧采集页，直接新开一个" % rid, flush=True)

    def restart_after_loss(self, room: dict, rid: str) -> bool:
        """断流自动收尾后，页面还在播就再拉一次采集。

        服务端 120 秒收尾是「别再挂着一个 running 的场次」，不等于「直播结束」。
        这次事故里直播播到早上 7 点，0:49 断流收尾后就再没人管了——后面 6 小时
        全丢。这里用页面探针确认还在播才重启；真下播不折腾。
        """
        if not room.get("auto_stopped"):
            return False
        reason = str(room.get("auto_stop_reason") or "")
        if "音频块" not in reason:
            return False
        info = self.running.get(rid)
        if not info:
            return False
        if int(info.get("auto_restarts") or 0) >= RESTART_MAX:
            return False
        probe = self.probe_page(rid, info)
        if probe is None:
            # 页面正在跳转 / CDP 一时没应答：先别杀浏览器，下一轮再探。
            # 连续几次都探不到（浏览器真死了）才交给 tick 回收。
            misses = int(info.get("restart_probe_misses") or 0) + 1
            info["restart_probe_misses"] = misses
            return misses <= 3
        info["restart_probe_misses"] = 0
        if judge_ended(probe, room):
            return False
        n = int(info.get("auto_restarts") or 0) + 1
        info["auto_restarts"] = n
        print("[%s] 断流收尾后页面仍在播，自动重启采集（第 %d 次）"
              % (rid, n), flush=True)
        # 先回到 pending：清掉上一场的 session_id 和自动收尾标记，否则采集页
        # 打开后会看到「房间已停止」立刻收手。
        self.report(rid, "pending", stage=0, note="断流自动重启采集")
        room["state"] = "pending"
        room["session_id"] = ""
        room["auto_stopped"] = False
        room["auto_stop_reason"] = ""
        try:
            self.bring_up(room)
            # bring_up 会换一份新的 info，把重启预算带过去，否则上限永远不生效。
            new_info = self.running.get(rid)
            if new_info is not None:
                new_info["auto_restarts"] = n
        except Exception as e:
            traceback.print_exc()
            self.fail(rid, "断流重启失败：%s" % e, 1)
        return True

    def _stop_session(self, sid: str) -> None:
        try:
            api(self.base, "/api/live/stop", {"session_id": sid}, timeout=20)
        except Exception:
            pass        # 服务端即使超时也把收尾跑完了；实在没停，断流看门狗兜底

    def open_capture(self, rid: str, port: int, cap_url: str) -> bool:
        try:
            http_json("http://127.0.0.1:%d/json/new?%s" % (
                port, urllib.parse.quote(cap_url, safe="")), method="PUT", timeout=5)
        except Exception:
            try:
                http_json("http://127.0.0.1:%d/json/new?%s" % (
                    port, urllib.parse.quote(cap_url, safe="")), method="GET", timeout=5)
            except Exception as e:
                print("[%s] 打不开采集页: %s" % (rid, e), flush=True)
                return False
        self.running[rid]["cap_opened"] = True
        return True

    def fail(self, rid: str, msg: str, stage: int = 1) -> None:
        print("[%s] 失败（第 %d 步）：%s" % (rid, stage, msg), flush=True)
        self.report(rid, "failed", stage=stage, failed_stage=stage, error=msg,
                    switch_ready=False, switch_requested=False,
                    switch_reason="")
        # 失败了就别留着一个 300MB 的浏览器空转：重试时会重新拉一个
        self.reap(rid, "启动失败")

    # ---------------------------------------------------------------- 守护
    def reap(self, rid: str, reason: str = "") -> None:
        info = self.running.pop(rid, None)
        if not info:
            return
        # 和心跳线程共用一把锁：先 pop 再发 alive=False，正在飞的那条 True
        # 一定排在它前面，不会被延迟写回"活着"。
        with self._beat_lock:
            self._beat(rid, False, info, reason or "browser closed")
        if info.get("muted") and info.get("profile"):
            mute_room(info["profile"], False)
        proc = info.get("proc")
        if proc is None:      # 接管来的：浏览器是上一个代理进程拉起的，这里不管
            return
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if reason:
            print("[%s] 回收：%s" % (rid, reason), flush=True)

    def session_alive(self, room: dict) -> bool:
        sid = str(room.get("session_id") or "")
        if not sid:
            return False
        try:
            st = api(self.base, "/api/live/status?id=" + urllib.parse.quote(sid))
            return bool(st.get("running"))
        except Exception:
            return False

    def adopt(self, rooms: list[dict]) -> None:
        """代理重启后，把还在采的房间接过来，别再拉一个浏览器。

        采集真正干活的是浏览器里的那个采集页，代理只负责把浏览器拉起来。
        所以代理重启后原来的采集照旧在传音频；这时再 bring_up 一次，会变成
        同一个房间两路 Chrome、两场会话、两份录音——重启代理本是日常操作，
        不能每次都留下这种烂摊子。
        """
        for r in rooms:
            rid = r["id"]
            if rid in self.running or r.get("state") not in ("live", "starting"):
                continue
            if not self.session_alive(r):
                continue
            # 端口从浏览器自己写的 DevToolsActivePort 里读回来：不留着 0，
            # 否则接管来的这一路做不了「还在播吗」的复查。
            self.running[rid] = {"proc": None,
                                 "port": devtools_port(PROFILES / rid, timeout=1.0),
                                 "profile": PROFILES / rid,
                                 "started": time.time(), "locked": True,
                                 "cap_opened": True, "adopted": True}
            self.beat(rid, True, self.running[rid])
            print("[%s] 接管已在跑的采集（不重复拉浏览器）" % rid, flush=True)

    def tick(self, rooms: list[dict]) -> None:
        alive_ids = {r["id"] for r in rooms}
        # 房间被删了 / 已停止 -> 关掉对应浏览器
        for rid in list(self.running):
            if rid not in alive_ids:
                self.reap(rid, "房间已删除")
        for r in rooms:
            rid = r["id"]
            if r.get("state") in ("stopped",):
                # 断流自动收尾 ≠ 下播：页面还在播就再拉一次，别让后半夜整段丢掉。
                if rid in self.running and self.restart_after_loss(r, rid):
                    continue
                if rid in self.running:
                    self.reap(rid, "已停止")
                continue
            if r.get("state") == "failed":
                # 采集页自己报了失败：别再留一个空转的浏览器，重试会重新拉。
                if rid in self.running:
                    self.reap(rid, "采集失败")
                continue
            if r.get("state") == "pending" and rid in self.running:
                # 用户点重试时旧浏览器可能还活着（采集页报错但还没被回收）：
                # pending 的含义是「重新拉起」，先关旧的，别让 alive 判断把它跳过。
                self.reap(rid, "重新排队")

            info = self.running.get(rid)
            if info and (info.get("proc") is None or info["proc"].poll() is None):
                # 活着：音频会话要等页面开始播才出现，所以每轮试一次，成功就不再试
                self.beat(rid, True, info)
                if MUTE_ROOMS and not info.get("muted") and info.get("profile"):
                    got = mute_room(info["profile"])
                    if got:
                        info["muted"] = True
                        print("[%s] 已静音（%d 路音频会话）" % (rid, got), flush=True)
                self.recheck_end(r, rid, info)
                self.check_capture(r, rid, info)
                continue
            if info:
                print("[%s] 浏览器退出了，重新拉起" % rid, flush=True)
                self.reap(rid, "浏览器退出")

            if r.get("state") in ("pending", "starting", "live"):
                try:
                    # 浏览器退出但会话还活着时，带上原 session 续接，别把一场拆成两场。
                    self.bring_up(r, session=str(r.get("session_id") or ""))
                except Exception as e:
                    traceback.print_exc()
                    self.fail(rid, "拉起失败：%s" % e, 1)
                time.sleep(2.0)   # 串行启动：同时拉多个会互相抢初始化

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._beat_thread.join(timeout=2.0)
        except Exception:
            pass
        for rid in list(self.running):
            self.reap(rid, "进程退出")


class SharedBrowser:
    """一个浏览器 + 多个房间标签页（省内存的采集底座）。

    扩展装在这个浏览器里；每个房间是一个静音标签页，页面内注入的采集器对
    主 <video> 调 captureStream()，PCM 经扩展 service worker 回传本地服务。
    代理通过浏览器里常驻的扩展面板页发消息控制它 —— 面板页是普通页面，
    CDP 随时连得上；service worker 睡了，消息会把它叫醒。
    """

    def __init__(self, exe: str, base: str) -> None:
        self.exe = exe
        self.base = base
        self.proc = None
        self.port = 0
        self.profile = SHARED_PROFILE
        self.ext_id = ""
        self.panel_ws = ""
        self.ready = False
        self._lock = threading.Lock()

    def alive(self) -> bool:
        if not self.port:
            return False
        if self.proc is not None and self.proc.poll() is not None:
            return False
        return True

    def _url(self, path: str, port: int | None = None) -> str:
        return "http://127.0.0.1:%d%s" % (port or self.port, path)

    def ensure(self) -> None:
        with self._lock:
            if self.alive():
                try:
                    http_json(self._url("/json/version"), timeout=1.5)
                    self._ensure_panel()
                    if self.ready or self._extension_ready():
                        self.ready = True
                        return
                    # 浏览器在、扩展没起来：多半是上次非正常退出被 Edge 禁用了。
                    print("[shared] 浏览器里的扩展没加载，重启浏览器", flush=True)
                except Exception:
                    pass
                self._shutdown_browser()
            # 代理重启过：先接上上一次留下的浏览器，别重复拉一个。
            port = devtools_port(self.profile, timeout=1.0)
            if port:
                try:
                    http_json(self._url("/json/version", port), timeout=1.5)
                    eid = extension_id(self.profile, EXT_DIR, timeout=3.0)
                    if eid:
                        self.port, self.ext_id = port, eid
                        self._ensure_panel()
                        if self._extension_ready():
                            self.ready = True
                            print("[shared] 接管已在运行的浏览器 port=%d" % port, flush=True)
                            return
                        self._shutdown_browser()
                except Exception:
                    pass
            self._launch()

    def _shutdown_browser(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        else:
            # 接管来的：按 profile 认进程，别误杀别的 Edge
            kill_profile(self.profile)
        self.proc = None
        self.port = 0
        self.panel_ws = ""
        self.ready = False

    def _extension_ready(self) -> bool:
        """面板页真的能用 chrome.runtime 才算扩展加载成功。

        只读 Secure Preferences 不够：Edge 会把上次强杀过的未打包扩展标记为
        禁用，ID 还在，但扩展根本没起来，面板页打开的是错误页。
        """
        if not self.panel_ws:
            return False
        try:
            r = asyncio.run(cdp_eval_await(self.panel_ws, "typeof chrome.runtime", timeout=3.0))
        except Exception:
            return False
        val = ((r.get("result") or {}).get("result") or {}).get("value")
        return val == "object"

    def _clear_disabled_extension(self) -> None:
        """扩展被 Edge 标记为禁用时，整份 Secure Preferences 删掉让它重建。

        Secure Preferences 带 MAC，改不了单条记录。这是专用采集 profile，
        丢的只是扩展层的受保护设置，不影响 Cookie/登录态。
        """
        f = self.profile / "Default" / "Secure Preferences"
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return
        want = os.path.normcase(os.path.abspath(str(EXT_DIR)))
        for val in (((j.get("extensions") or {}).get("settings") or {}).values()):
            p = str(val.get("path") or "")
            if not p or os.path.normcase(os.path.abspath(p)) != want:
                continue
            if val.get("disable_reasons"):
                try:
                    f.unlink()
                    print("[shared] 扩展被浏览器标记为禁用（上次非正常退出），"
                          "已重置扩展状态", flush=True)
                except Exception:
                    pass
            return

    def _launch(self) -> None:
        self.profile.mkdir(parents=True, exist_ok=True)
        old = kill_profile(self.profile)
        if old:
            print("[shared] 清掉残留的 %d 个浏览器进程" % old, flush=True)
            time.sleep(1.0)
        # 旧端口文件会让 devtools_port 立刻返回过期端口，先删掉。
        try:
            (self.profile / "DevToolsActivePort").unlink()
        except Exception:
            pass
        self._clear_disabled_extension()
        port = free_port()
        args = [
            self.exe,
            "--user-data-dir=%s" % self.profile,
            "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1024,720",
            "--remote-debugging-port=%d" % port,
            # 共享模式的关键：必须把扩展带起来。Chrome 正式版会忽略这两个参数。
            "--load-extension=%s" % EXT_DIR,
            "--disable-extensions-except=%s" % EXT_DIR,
            # 后台标签页也要正常出声/出块，别被 Chrome 冻住。
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--disable-features=CalculateNativeWinOcclusion,MemorySaver",
            "about:blank",
        ]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        # Edge 有时不写 DevToolsActivePort，别死等它：同时探请求的端口，
        # 谁先可用用谁。两边都等 20 秒会把每次启动白白拖长。
        real = 0
        deadline = time.time() + 20
        while time.time() < deadline:
            real = devtools_port(self.profile, timeout=0.3)
            if real:
                break
            try:
                http_json(self._url("/json/version", port), timeout=0.5)
                real = port
                break
            except Exception:
                time.sleep(0.2)
        self.port = real or port
        if not real:
            raise RuntimeError("浏览器起来了但调试端口没响应")
        print("[shared] 启动浏览器 pid=%s port=%d" % (self.proc.pid, self.port),
              flush=True)
        self.ext_id = extension_id(self.profile, EXT_DIR)
        if not self.ext_id:
            raise RuntimeError(
                "扩展没有加载：这个浏览器不支持 --load-extension。"
                "请用 Microsoft Edge，或用 LIVENOTES_SHARED_BROWSER 指定 Chrome for Testing。")
        self._ensure_panel()
        if not self._extension_ready():
            raise RuntimeError(
                "扩展没有加载（面板页里没有 chrome.runtime）。"
                "请用 Microsoft Edge，或用 LIVENOTES_SHARED_BROWSER 指定 Chrome for Testing。")
        self.ready = True
        print("[shared] 扩展已加载 %s" % self.ext_id, flush=True)

    def _ensure_panel(self) -> None:
        if not self.port or not self.ext_id:
            return
        panel = "chrome-extension://%s/panel.html" % self.ext_id
        for attempt in range(30):
            try:
                pages = http_json(self._url("/json/list"), timeout=2)
            except Exception:
                pages = []
            for p in pages:
                if p.get("type") == "page" and str(p.get("url") or "").startswith(panel):
                    self.panel_ws = str(p.get("webSocketDebuggerUrl") or "")
                    return
            if attempt == 0:
                try:
                    http_json(self._url("/json/new?" + urllib.parse.quote(panel, safe="")),
                              method="PUT", timeout=5)
                except Exception:
                    try:
                        http_json(self._url(
                            "/json/new?" + urllib.parse.quote(panel, safe="")),
                            method="GET", timeout=5)
                    except Exception:
                        pass
            time.sleep(0.3)
        raise RuntimeError("打不开扩展面板页（%s）" % panel)

    def call(self, msg: dict, timeout: float = 15.0) -> dict:
        """在面板页里把消息发给扩展 service worker，等结果。"""
        try:
            self.ensure()
        except Exception as e:
            return {"error": "共享浏览器不可用：%s" % e}
        expr = "(async () => await chrome.runtime.sendMessage(%s))()" % json.dumps(
            msg, ensure_ascii=False)
        try:
            r = asyncio.run(cdp_eval_await(self.panel_ws, expr, timeout=timeout))
        except Exception as e:
            # 面板页可能被关掉/导航走：下一次 ensure() 会重开。
            self.panel_ws = ""
            return {"error": "控制页失联：%s" % e}
        res = r.get("result") or {}
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"].get("text") or ""
            if "sendMessage" in detail or "chrome.runtime" in detail:
                self.ready = False
            return {"error": "扩展调用失败：%s" % detail}
        val = (res.get("result") or {}).get("value")
        return val if isinstance(val, dict) else {"error": "扩展返回异常：%r" % (val,)}

    def _bring_to_front(self, ws_url: str) -> None:
        async def go():
            async with _ws_connect(ws_url, max_size=None, open_timeout=3) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Page.bringToFront"}))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                    if m.get("id") == 1:
                        return
        try:
            asyncio.run(go())
        except Exception:
            pass

    def activate(self, url_hint: str) -> bool:
        """用 CDP 把标签页切到前台。

        chrome.tabs.update({active:true}) 在窗口没聚焦时不可靠：实测返回成功，
        但 active 仍是 false，抖音也就不会更新标题。Page.bringToFront 可以。
        """
        want = canonical_live_url(url_hint)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                pages = http_json(self._url("/json/list"), timeout=2)
            except Exception:
                pages = []
            for p in pages:
                if p.get("type") != "page":
                    continue
                u = str(p.get("url") or "")
                if (url_hint and u == url_hint) or (want and canonical_live_url(u) == want):
                    self._bring_to_front(str(p.get("webSocketDebuggerUrl") or ""))
                    return True
            time.sleep(0.3)
        return False

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass


class SharedAgent(Agent):
    """共享浏览器模式的代理：房间 = 标签页，采集 = 页面内 captureStream。"""

    adopt_on_start = True
    engine = "shared"

    def __init__(self, base: str, browser: SharedBrowser) -> None:
        super().__init__(base, chrome=browser.exe)
        self.shared = browser

    def _call(self, msg: dict, timeout: float = 15.0) -> dict:
        return self.shared.call(msg, timeout=timeout)

    def bring_up(self, room: dict, session: str = "") -> None:
        rid = room["id"]
        self.report(rid, "starting", stage=1, note="正在打开直播间标签页",
                    session_id=session or None)
        try:
            self.shared.ensure()
        except Exception as e:
            self.fail(rid, "共享浏览器没起来：%s" % e, 1)
            return
        op = self._call({"type": "page-open", "url": room["url"]})
        tab_id = int(op.get("tabId") or 0)
        if not tab_id:
            self.fail(rid, "标签页没打开：%s" % (op.get("error") or "未知错误"), 1)
            return
        info = {"proc": self.shared.proc, "port": self.shared.port,
                "profile": self.shared.profile, "tab_id": tab_id,
                "started": time.time(), "locked": True, "cap_opened": True,
                "shared": True}
        self.running[rid] = info
        self.beat(rid, True, info)
        print("[%s] 共享标签页 tab=%d" % (rid, tab_id), flush=True)

        self.report(rid, "starting", stage=2, note="正在确认直播画面",
                    session_id=session or None)
        deadline = time.time() + 45.0
        name = ""
        resolved = ""
        why = ""
        last_text = ""
        last_page_error = ""
        saw_play = False
        while time.time() < deadline:
            time.sleep(1.0)
            p = self._call({"type": "page-probe", "tabId": tab_id})
            if p.get("gone"):
                why = "标签页已经关掉"
                break
            probe = p.get("probe") or {}
            t = clean_title(str(p.get("title") or probe.get("title") or ""))
            if t:
                name = t
            u = canonical_live_url(str(probe.get("url") or p.get("url") or ""))
            if u:
                resolved = u
            if int(probe.get("playing") or 0) > 0:
                saw_play = True
                # 页面已经在播，但后台标签页的标题可能还是 URL：等标题出来再走，
                # 否则会把一个正在播的房间误判成"没有房间名"。
                if name:
                    break
                # 抖音只有在标签页可见时才更新标题；用 CDP 切一次前台推它一把。
                self.shared.activate(str(probe.get("url") or room.get("url") or ""))
            last_text = str(probe.get("text") or "")
            for m in DEAD_MARKS:
                if m in last_text:
                    why = "直播间没在播（页面显示「%s」）" % m
                    break
            if not why:
                for m in PAGE_ERROR_MARKS:
                    if m in last_text:
                        last_page_error = m
            if why:
                break
        if not why and not saw_play and last_page_error:
            # 临时故障不是下播：别让用户以为主播关播了。
            why = ("抖音页面返回「%s」（临时故障，不一定是没开播）；稍后重试"
                   % last_page_error)
        if why:
            self.fail(rid, why, 2)
            return
        if not name:
            name = clean_title(room.get("name") or "")
        if not name:
            self.fail(rid, "页面没有给出房间名（可能不是直播间，或页面没加载出来）", 2)
            return
        if name != room.get("name"):
            self.report(rid, name=name[:60])
            room["name"] = name
        if resolved and resolved != room.get("url"):
            self.report(rid, url=resolved)
            room["resolved_url"] = resolved

        self.report(rid, "starting", stage=3, note="已确认开播，正在启动音频采集",
                    session_id=session or None)
        st = self._call({"type": "page-start", "tabId": tab_id,
                         "session": session or "", "title": name}, timeout=25.0)
        if not st.get("ok"):
            self.fail(rid, "启动采集失败：%s" % (st.get("error") or "未知错误"), 3)
            return
        sid = str(st.get("sid") or "")
        info["sid"] = sid
        if sid:
            room["session_id"] = sid
            self.report(rid, "live", stage=6, note="音频采集已启动", session_id=sid)
        else:
            self.report(rid, "starting", stage=4, note="采集已启动，等待会话建立")
        print("[%s] 采集已启动（房间名：%s，session=%s）" % (rid, name, sid or "-"),
              flush=True)
        # 房间标签页已经渲染并开始采集了，把共享窗口切回控制页，别让用户看到
        # 一排直播间；后台标签页的采集不受影响（实测持续出块）。
        self._call({"type": "page-focus-panel"}, timeout=5.0)

    def probe_page(self, rid: str, info: dict) -> dict | None:
        tab_id = int(info.get("tab_id") or 0)
        if not tab_id:
            return None
        p = self._call({"type": "page-probe", "tabId": tab_id})
        if p.get("gone"):
            return None
        return p.get("probe") or None

    def reap(self, rid: str, reason: str = "") -> None:
        info = self.running.pop(rid, None)
        if not info:
            return
        with self._beat_lock:
            self._beat(rid, False, info, reason or "browser closed")
        tab_id = int(info.get("tab_id") or 0)
        if tab_id and self.shared.alive():
            # 先让页面内采集收手并停会话，再关标签页；顺序反了会丢掉收尾。
            self._call({"type": "page-stop", "tabId": tab_id, "keepSession": False},
                       timeout=10.0)
            self._call({"type": "page-close", "tabId": tab_id}, timeout=5.0)
        if reason:
            print("[%s] 回收：%s" % (rid, reason), flush=True)

    def recover_capture(self, room: dict, rid: str, info: dict, idle: float) -> None:
        """关掉卡死的页面内采集，用同一个 session 重注入；标签页不动。"""
        sid = str(room.get("session_id") or info.get("sid") or "")
        info["recovered_at"] = time.time()
        print("[%s] 采集断流 %.0f 秒，重建页面内采集（会话 %s）" % (rid, idle, sid),
              flush=True)
        self.report(rid, "live", stage=6,
                    note="采集断流，正在重建采集（缺口约 %d 秒）" % int(idle))
        tab_id = int(info.get("tab_id") or 0)
        tab = self._call({"type": "page-tab", "tabId": tab_id}) if tab_id else {}
        if not tab.get("ok"):
            self.bring_up(room, session=sid)
            return
        st = self._call({"type": "page-start", "tabId": tab_id,
                         "session": sid, "title": room.get("name") or ""},
                        timeout=25.0)
        if not st.get("ok"):
            self.bring_up(room, session=sid)

    def adopt(self, rooms: list[dict]) -> None:
        """代理重启后，接上共享浏览器里还在采的标签页，别重复开。"""
        if not self.shared.port and not devtools_port(self.shared.profile, timeout=0.5):
            return
        try:
            self.shared.ensure()
        except Exception:
            return
        stats = self._call({"type": "page-stats"})
        caps = {int(c.get("tabId") or 0): c for c in (stats.get("caps") or [])}
        for r in rooms:
            rid = r["id"]
            if rid in self.running or r.get("state") not in ("live", "starting"):
                continue
            if not self.session_alive(r):
                continue
            sid = str(r.get("session_id") or "")
            tab_id = next((t for t, c in caps.items()
                           if str(c.get("sid") or "") == sid), 0)
            if not tab_id:
                continue
            self.running[rid] = {
                "proc": self.shared.proc, "port": self.shared.port,
                "profile": self.shared.profile, "tab_id": tab_id,
                "started": time.time(), "locked": True, "cap_opened": True,
                "adopted": True, "shared": True,
            }
            self.beat(rid, True, self.running[rid])
            print("[%s] 接管共享浏览器里已在采的标签页 tab=%d" % (rid, tab_id),
                  flush=True)

    def tick(self, rooms: list[dict]) -> None:
        alive_ids = {r["id"] for r in rooms}
        for rid in list(self.running):
            if rid not in alive_ids:
                self.reap(rid, "房间已删除")

        # 共享浏览器死了：所有房间一起重来。这是共享模式的代价：先把状态清掉，
        # 下面的循环会带着原 session 逐个重开标签页。
        if self.running and not self.shared.alive():
            print("[shared] 浏览器退出了，重建所有采集", flush=True)
            for rid in list(self.running):
                info = self.running.pop(rid, None)
                if info:
                    with self._beat_lock:
                        self._beat(rid, False, info, "shared browser exited")

        # 一次拿齐标签页和采集状态，别每个房间各问一次。
        tabs: dict[int, dict] = {}
        caps: dict[int, dict] = {}
        if self.running:
            try:
                self.shared.ensure()
                tabs = {int(t.get("tabId") or 0): t
                        for t in (self._call({"type": "page-tabs"}).get("tabs") or [])}
                caps = {int(c.get("tabId") or 0): c
                        for c in (self._call({"type": "page-stats"}).get("caps") or [])}
            except Exception:
                pass

        for r in rooms:
            rid = r["id"]
            if r.get("state") == "stopped":
                if rid in self.running and self.restart_after_loss(r, rid):
                    continue
                if rid in self.running:
                    self.reap(rid, "已停止")
                continue
            if r.get("state") == "failed":
                if rid in self.running:
                    self.reap(rid, "采集失败")
                continue
            if r.get("state") == "pending" and rid in self.running:
                self.reap(rid, "重新排队")

            info = self.running.get(rid)
            if info:
                tab_id = int(info.get("tab_id") or 0)
                tab = tabs.get(tab_id)
                if tab:
                    self.beat(rid, True, info)
                    cap = caps.get(tab_id)
                    if cap:
                        sid = str(cap.get("sid") or "")
                        if sid and sid != str(r.get("session_id") or ""):
                            # 服务重启后扩展自己补建了新会话：把房间对到新 id，
                            # 否则代理会一直拿旧 id 判"没在跑"。
                            r["session_id"] = sid
                            info["sid"] = sid
                            self.report(rid, "live", stage=6,
                                        note="音频采集已启动", session_id=sid)
                    self.recheck_end(r, rid, info)
                    self.check_capture(r, rid, info)
                    continue
                self.reap(rid, "标签页已关闭")

            if r.get("state") in ("pending", "starting", "live"):
                try:
                    self.bring_up(r, session=str(r.get("session_id") or ""))
                except Exception as e:
                    traceback.print_exc()
                    self.fail(rid, "拉起失败：%s" % e, 1)
                time.sleep(1.0)

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._beat_thread.join(timeout=2.0)
        except Exception:
            pass
        for rid in list(self.running):
            self.reap(rid, "进程退出")
        self.shared.stop()


def _port_of(base: str) -> int:
    u = urllib.parse.urlparse(base)
    if u.port:
        return u.port
    return 443 if u.scheme == "https" else 80


def probe_service(base: str, timeout: float = 3.0) -> tuple[bool, str]:
    """启动时先验一次"代理 → 本机服务"，别等拉 Chrome 拉到一半才发现连不上。"""
    t0 = time.time()
    try:
        api(base, "/api/health", timeout=timeout)
        return True, "OK（%d ms）" % max(1, int((time.time() - t0) * 1000))
    except Exception as e:
        return False, str(e)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    a.base = a.base.rstrip("/")

    agent: Agent
    if CAPTURE_MODE == "direct":
        # 延迟导入：只有 direct 模式才需要 streamlink/ffmpeg 相关代码。
        from direct_capture import DirectAgent
        agent = DirectAgent(a.base)
        print("采集模式：直取抖音流（ffmpeg）", flush=True)
    elif CAPTURE_MODE == "shared":
        exe = find_shared_browser()
        agent = SharedAgent(a.base, SharedBrowser(exe, a.base))
        print("采集模式：共享浏览器（%s）" % exe, flush=True)
    else:
        agent = Agent(a.base)
        print("采集模式：每房间一个浏览器", flush=True)
    print("浏览器：%s" % (agent.chrome or "不需要（direct）"), flush=True)
    print("服务：%s" % a.base, flush=True)
    ok, msg = probe_service(a.base)
    print("服务连通性：%s" % msg, flush=True)
    if not ok:
        print("  ↑ 这是「采集代理 → 本机服务」的通路，和抖音无关；服务起来后会自动重连。",
              flush=True)

    last_err = ""
    fails = 0
    adopted = False
    try:
        while True:
            try:
                payload = api(a.base, "/api/rooms")
                rooms = payload.get("rooms") or []
                # 设置页可以热更新直取失败策略；不用重启采集代理。
                agent.apply_config(payload.get("capture") or {})
                if last_err:
                    print("服务已恢复：%s" % a.base, flush=True)
                    last_err = ""
                    fails = 0
            except Exception as e:
                fails += 1
                msg = str(e)
                if msg != last_err:
                    print("连不上服务：%s" % msg, flush=True)
                    print("  目标：%s/api/rooms（本机回环）" % a.base, flush=True)
                    print("  排查：同一窗口执行  curl %s/api/health" % a.base, flush=True)
                    print("        netstat -ano | findstr :%d   # 只应有一个 LISTENING"
                          % _port_of(a.base), flush=True)
                    print("  这不是抖音的问题；10 秒后重试", flush=True)
                    last_err = msg
                else:
                    print("仍连不上服务（第 %d 次）：%s；10 秒后重试" % (fails, msg), flush=True)
                time.sleep(10)
                continue
            if a.status:
                for r in rooms:
                    info = agent.running.get(r["id"])
                    alive = bool(info and (info.get("proc") is None
                                           or info["proc"].poll() is None))
                    print("  %-12s %-10s stage=%-2s %s %s" % (
                        r["id"], r.get("state"), r.get("stage", 0),
                        "采集在跑" if alive else "无采集",
                        r.get("name") or ""))
                return 0
            if not adopted:
                # 代理重启过、采集还在：先认领已在采的标签页/ffmpeg 会话，
                # 否则下一轮会重复开标签页、重复建会话。
                if getattr(agent, "adopt_on_start", False):
                    try:
                        agent.adopt(rooms)
                    except Exception:
                        traceback.print_exc()
                adopted = True
            agent.tick(rooms)
            if a.once:
                return 0
            time.sleep(3)
    except KeyboardInterrupt:
        print("\n[exit] 关闭中…")
    finally:
        agent.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))