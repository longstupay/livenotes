# -*- coding: utf-8 -*-
"""抖音直取采集：streamlink 解析 FLV -> ffmpeg 拉 16k 单声道 PCM -> /api/live/chunk。

为什么单独一个模块：浏览器采集的会话是采集页自己建的，direct 没有页面，
必须由代理自己调 /api/live/start；把它和 SharedAgent 混在一个类里会让
「谁建会话、谁停会话」变得不清楚。这里只做抖音，B 站/快手等以后按同样
的 resolver 接口加，不改变主循环。

失败策略：
  - 解析失败 / ffmpeg 退出 / 15 秒没有成功送出的音频块 -> 用同一个 session
    重新解析、重启 ffmpeg，最多 3 次；
  - 3 次仍失败 -> 默认把房间标成 failed 并亮出「切换浏览器采集」，等用户在
    收听页确认后才切（设置页或 LIVENOTES_DIRECT_FALLBACK=ask）；设 auto 才是
    旧的无感回退（per_room，不可用再 shared），none 则只失败不提供切换。
  - 会话先留着（服务端看门狗 120 秒后收尾），用户马上点切换能接着同一场录。
"""
from __future__ import annotations

import array
import gzip
import html
import http.client
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request

FALLBACK_MODES = ("ask", "auto", "per_room", "shared", "none")

try:  # 作为 tools.direct_capture 导入（测试）
    from .capture_agent import (Agent, SharedAgent, SharedBrowser, api,
                                find_chrome, find_shared_browser)
except ImportError:  # 作为 python tools/capture_agent.py 的依赖导入
    from capture_agent import (Agent, SharedAgent, SharedBrowser, api,
                               find_chrome, find_shared_browser)

CHUNK_SAMPLES = 4096
CHUNK_BYTES = CHUNK_SAMPLES * 2       # s16le：256ms
DIRECT_WARN_S = 10.0                  # 先提示，不重建
DIRECT_RECOVER_AFTER_S = 15.0         # 超过这个时间没有成功送块就重建
DIRECT_RETRY_S = 10.0                 # 解析/启动失败后的退避
DIRECT_MAX_RESTARTS = 3
FIRST_CHUNK_WAIT_S = 8.0
RESOLVE_TIMEOUT_S = 20.0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ResolveError(RuntimeError):
    """解析不出可播放的流地址。"""


class SessionGone(RuntimeError):
    """服务端返回 404：会话已结束/服务重启过。"""


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def is_douyin(url: str) -> bool:
    """direct 只做抖音；B 站/快手等直接交给浏览器，不浪费一次解析超时。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d)
               for d in ("douyin.com", "amemv.com", "iesdouyin.com"))


_LIVE_RE = re.compile(r"^https?://live\.douyin\.com/(\d+)", re.I)
_REFLOW_RE = re.compile(r"/(?:douyin/webcast/reflow|share/live)/(\d+)")
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
              "Mobile/15E148 Safari/604.1")


_REFLOW_BASE = "https://webcast.amemv.com/douyin/webcast/reflow/"
# reflow 页里没有播放器元素时，用这些文案判断是「没开播」还是「抖音临时报错」。
# 「服务器开小差了」不能算下播：它只是抖音的通用错误提示，直播间可能开着。
_OFFLINE_MARKS = ("直播已结束", "主播已下播", "主播暂未开播", "直播已关闭")
_PAGE_ERROR_MARKS = ("服务器开小差了", "直播间不存在", "页面不存在")


def _landing_url(url: str, timeout: float = 10.0) -> str:
    """抖音直播间链接 -> 浏览器能打开、HTML 里带播放地址的 reflow 页。

    reflow 是分享落地页，页面里直接嵌着 <webcast-reflow-player url="...flv">。
    live.douyin.com 现在把房间数据挪进了客户端脚本，streamlink 扒不到；
    reflow 页反而稳定，所以直取以它为主。短链先跟一次跳转，落到 reflow
    或 live 页后再换回 reflow 形状。返回空串表示不是抖音 / 跳转失败。
    """
    u = (url or "").strip()
    m = _LIVE_RE.match(u)
    if m:
        return _REFLOW_BASE + m.group(1)
    m = _REFLOW_RE.search(urllib.parse.urlsplit(u).path)
    if m:
        return _REFLOW_BASE + m.group(1)
    if not is_douyin(u):
        return ""
    try:
        req = urllib.request.Request(u, headers={"User-Agent": _MOBILE_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = resp.geturl()
    except Exception:
        return ""
    m = _LIVE_RE.match(final)
    if m:
        return _REFLOW_BASE + m.group(1)
    m = _REFLOW_RE.search(urllib.parse.urlsplit(final).path)
    if m:
        return _REFLOW_BASE + m.group(1)
    return ""


def canonical_douyin_url(url: str, timeout: float = 10.0) -> str:
    """直播链接 -> 真能打开的 live.douyin.com/<web_rid>；拿不到返回空串。

    注意：reflow 后面的 <id> 是 room id，**不是** web_rid。旧实现拿它直接拼
    live.douyin.com/<id>，实测打开就是「服务器开小差了」——直播间明明开着，
    页面却什么都没有。现在只认 reflow 页 owner 里的 webRid；读不到就返回空，
    让调用方退回 reflow 页，绝不伪造一个看着像、实际没有房间的地址。
    """
    u = (url or "").strip()
    m = _LIVE_RE.match(u)
    if m:
        return "https://live.douyin.com/" + m.group(1)
    landing = _landing_url(u, timeout)
    if not landing:
        return ""
    try:
        text, _cost = _fetch_reflow(landing, timeout)
    except Exception:
        return ""
    return page_url(parse_room_meta(text))


def _clean_url(u: str) -> str:
    """HTML 属性 / JSON 字符串里的播放地址 -> ffmpeg 能直接吃的 URL。"""
    u = html.unescape(str(u or ""))
    return u.replace("\\u0026", "&").replace("\\/", "/").strip()


def _pick_stream_url(text: str) -> str:
    """reflow 页 -> 播放地址。优先 FLV，其次 HLS。"""
    m = re.search(r'<webcast-reflow-player[^>]*\burl="([^"]+)"', text, re.I)
    if m:
        return _clean_url(m.group(1))
    # 页面后半段是 RSC 内嵌 JSON，播放地址是转义过的 \"flv\":\"https:\/\/...\"。
    flat = text.replace('\\"', '"').replace("\\/", "/")
    for key in ("flv", "hls"):
        m = re.search(r'"%s"\s*:\s*"(https?://[^"]+)"' % key, flat, re.I)
        if m:
            return _clean_url(m.group(1))
    m = re.search(r'https?://[^\s"\'<>]+\.(?:flv|m3u8)[^\s"\'<>]*', text, re.I)
    return _clean_url(m.group(0)) if m else ""


def _fetch_reflow(landing: str, timeout: float) -> tuple[str, float]:
    req = urllib.request.Request(landing, headers={
        "User-Agent": _MOBILE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
    })
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace"), time.monotonic() - t0


# ---------------------------------------------------------------- 房间名
# 直取只从 reflow 页里拿播放地址，页头那堆房间信息就白丢了——结果收听页没有
# 名字可用，只能退回显示域名（webcast.amemv.com），既认不出是谁在播，停了也
# 不知道该删哪一路。其实同一份 HTML 的首屏 JSON 里就写着：
#   "room":{"id":…,"idStr":"…","title":"示例直播标题",…}
#   "owner":{"id":…,"nickname":"示例主播",…}
# 所以不用另开浏览器、不用请求接口，解析一次就够了。名字是锦上添花：
# 抠不到就返回空，绝不能因为它影响录音。
_TITLE_RE = re.compile(r'"title"\s*:\s*"((?:[^"\\]|\\.){0,200})"')
_NICK_RE = re.compile(
    r'"owner"\s*:\s*\{[^{}]{0,400}?"nickname"\s*:\s*"((?:[^"\\]|\\.){0,80})"')
_ROOM_RE = re.compile(r'"room"\s*:\s*\{')
# live.douyin.com/<web_rid> 里的 web_rid 是主播的网页房号，和 reflow 页里的
# room id 不是一回事：拿 room id 去拼 live 地址，抖音会回「服务器开小差了」。
# reflow 页首屏 JSON 的 owner 对象里带着真正的 webRid。
_OWNER_RE = re.compile(r'"owner"\s*:\s*\{')
_WEB_RID_RE = re.compile(r'"webRid"\s*:\s*"(\d{4,})"')
_META_CACHE: dict[str, tuple[float, dict]] = {}
_META_TTL_S = 60.0        # 同一个房间一分钟内不重复抓：开一路只该花这一次
_META_MAX = 32            # 房间上限是 5，留余量；超出直接清空，不做 LRU


def _json_str(raw: str) -> str:
    """JSON 字符串字面量 -> 真字符串（\\uXXXX、&amp; 这类都能吃）。"""
    try:
        return json.loads('"' + raw + '"')
    except ValueError:
        return html.unescape(raw)


def _tidy(s: str) -> str:
    """页面上抓来的文本：去掉换行和多余空白，挡掉 $undefined 这类占位。"""
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    if not t or t.startswith("$") or t.lower() in ("undefined", "null", "none"):
        return ""
    return t


def parse_room_meta(text: str) -> dict:
    """reflow 页 HTML -> {"nickname", "title", "web_rid"}。

    页面后半段是 RSC 的内嵌 JSON，引号被转义成 \\"，先摊平再匹配，正则只
    需要认一种写法。抽成纯函数是为了能拿真页面的样本离线测。web_rid 是
    live.douyin.com 的房号，和 reflow 路径里的 room id 不是一回事。
    """
    flat = (text or "").replace('\\"', '"').replace("\\/", "/")
    out: dict[str, str] = {}
    nick = _NICK_RE.search(flat)
    if nick:
        out["nickname"] = _tidy(_json_str(nick.group(1)))
    # "room":{ 在页面里不止一处，取第一个真的带 title 的：标题不在 room 对象
    # 里的那些多半是状态/配置片段，硬取会拿到不相干的字符串。窗口只给 2000
    # 字符：真实页面里 title 就在 id/idStr/status 后面几十个字符，给太大反而
    # 容易越过这个对象、抓到隔壁对象的 title。
    for m in _ROOM_RE.finditer(flat):
        t = _TITLE_RE.search(flat[m.end(): m.end() + 2000])
        if t:
            title = _tidy(_json_str(t.group(1)))
            if title:
                out["title"] = title
                break
    if not out.get("nickname"):
        # 兜底：owner 对象结构变了，就认页面里第一个像人名的 nickname。
        m = re.search(r'"nickname"\s*:\s*"((?:[^"\\]|\\.){0,80})"', flat)
        if m:
            out["nickname"] = _tidy(_json_str(m.group(1)))
    # 房间的 web_rid：页面里观众/粉丝团对象也可能带 webRid，先锚定 owner，
    # 只从 owner 对象往后这一段里取第一个，避免拿到别人的号。
    owner = _OWNER_RE.search(flat)
    if owner:
        rid = _WEB_RID_RE.search(flat[owner.end(): owner.end() + 20000])
        if rid:
            out["web_rid"] = rid.group(1)
    return {k: v for k, v in out.items() if v}


def page_url(meta: dict) -> str:
    """房间信息 -> 能在浏览器里打开的直播间地址；拿不到 web_rid 返回空串。

    绝不能拿 reflow 里的 room id 顶替 web_rid：那个地址抖音只回
    「服务器开小差了」，看起来像直播间开着却没有任何画面。
    """
    rid = str((meta or {}).get("web_rid") or "").strip()
    return "https://live.douyin.com/" + rid if rid.isdigit() else ""


def _remember_meta(landing: str, meta: dict) -> None:
    """缓存一次抓取到的房间信息（直取解析完顺手留给 page_url 用）。"""
    if not landing or not meta:
        return
    if len(_META_CACHE) >= _META_MAX:
        _META_CACHE.clear()
    _META_CACHE[landing] = (time.time(), dict(meta))


def room_meta(url: str, timeout: float = 8.0) -> dict:
    """链接 -> 房间信息（主播名 / 标题 / web_rid）。抓不到就返回空字典。

    开播时补名字、算「显示页面」地址都会用它；同一个房间 60 秒内只抓一次，
    所以直取失败后重试、自愈重建都不会为同一张 reflow 页反复联网。
    """
    try:
        landing = _landing_url(url)
    except Exception:
        return {}
    if not landing:
        return {}
    hit = _META_CACHE.get(landing)
    if hit and time.time() - hit[0] < _META_TTL_S:
        return dict(hit[1])
    try:
        text, _cost = _fetch_reflow(landing, timeout)
    except Exception:
        return {}
    meta = parse_room_meta(text)
    _remember_meta(landing, meta)
    return dict(meta)


def display_name(meta: dict, limit: int = 60) -> str:
    """房间名：主播名 · 房间标题（缺哪个用哪个）。

    超长先砍标题尾巴、保住主播名——卡片和飞书里都是靠主播名认人的。
    上限 60 和服务端 name[:60] 对齐，别让好名字在最后一步被硬切。
    """
    nick = _tidy(meta.get("nickname"))
    title = _tidy(meta.get("title"))
    if not nick and not title:
        return ""
    if not title:
        return nick[:limit]
    if not nick:
        return title[:limit]
    head = nick + " · "
    room = limit - len(head)
    if len(title) <= room:
        return head + title
    if room < 4:                 # 主播名自己就快吃满额度了，标题只能留很短
        return (head + title[:max(1, room - 1)] + "…")[:limit]
    return head + title[:room - 1] + "…"


def _resolve_streamlink(target: str, timeout: float) -> tuple[str, str]:
    """旧解析路径：streamlink 的抖音插件。reflow 页结构变化时兜底用。"""
    if not _module_available("streamlink"):
        raise ResolveError("未安装 streamlink")
    cmd = [sys.executable, "-m", "streamlink", "--loglevel", "error",
           "--stream-url", target, "best"]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise ResolveError("streamlink 解析超时（%.0f 秒）" % timeout)
    except FileNotFoundError as e:
        raise ResolveError("启动 streamlink 失败：%s" % e)
    out = [ln.strip() for ln in (r.stdout or "").splitlines()
           if ln.strip().startswith("http")]
    if r.returncode != 0 or not out:
        err = ((r.stderr or "").strip().splitlines() or ["streamlink 解析失败"])[-1]
        raise ResolveError(err[:300])
    return out[-1], "streamlink %.1fs" % (time.monotonic() - t0)


def resolve_douyin(url: str, timeout: float = RESOLVE_TIMEOUT_S) -> tuple[str, str]:
    """抖音直播页 -> 可播放的 FLV/HLS 地址。返回 (stream_url, note)。

    先抓 reflow 页拿页面里现成的播放地址（约 1 秒，不依赖 streamlink）；
    实在拿不到才用 streamlink 兜底。页面明确写着「直播已结束」时直接报下播，
    不再白跑一次 streamlink。
    """
    landing = _landing_url(url)
    if not landing:
        raise ResolveError("无法从链接里找到抖音直播间房号")

    reflow_err = ""
    meta: dict = {}
    try:
        text, cost = _fetch_reflow(landing, timeout)
    except Exception as e:
        reflow_err = "抓取直播页失败：%s" % (str(e)[:120] or type(e).__name__)
    else:
        # 同一次抓取顺手把房间信息（含 web_rid）缓存下来：后面算页面地址、
        # 补房间名都不再为同一张页面多跑一次 HTTP。
        meta = parse_room_meta(text)
        _remember_meta(landing, meta)
        stream = _pick_stream_url(text)
        if stream:
            return stream, "reflow %.1fs" % cost
        for mark in _OFFLINE_MARKS:
            if mark in text:
                raise ResolveError("直播间当前没有开播（页面显示「%s」）" % mark)
        for mark in _PAGE_ERROR_MARKS:
            if mark in text:
                raise ResolveError(
                    "抖音页面返回「%s」（临时故障，不一定是没开播），稍后重试" % mark)
        reflow_err = "reflow 页里没有找到直播流地址"

    live = page_url(meta)
    if not live:
        # 用户直接贴的就是 live.douyin.com/<web_rid>：这个地址本身可信，reflow
        # 页抓不到时仍可交给 streamlink 兜底；reflow 链接则没有可信房号，不冒险。
        m = _LIVE_RE.match(str(url or "").strip())
        if m:
            live = "https://live.douyin.com/" + m.group(1)
    if not live:
        raise ResolveError(reflow_err)
    try:
        return _resolve_streamlink(live, timeout)
    except ResolveError as e:
        raise ResolveError("%s；streamlink 兜底也失败：%s" % (reflow_err, e))


def spawn_ffmpeg(stream: str) -> subprocess.Popen:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("找不到 ffmpeg，请安装并加入 PATH")
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
           "-i", stream, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0, creationflags=_NO_WINDOW)


def pcm_rms(data: bytes) -> float:
    arr = array.array("h")
    arr.frombytes(data[: len(data) // 2 * 2])
    if not arr:
        return 0.0
    return math.sqrt(sum(x * x for x in arr) / len(arr)) / 32768.0


def post_chunk(base: str, sid: str, data: bytes, timeout: float = 5.0) -> None:
    """POST 一块裸 PCM。404 单独抛，调用方据此重建会话。"""
    u = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(u.hostname or "127.0.0.1",
                                      u.port or 80, timeout=timeout)
    try:
        conn.request("POST", "/api/live/chunk", body=data, headers={
            "Content-Type": "application/octet-stream",
            "X-Live-Id": sid,
            "Content-Length": str(len(data)),
        })
        resp = conn.getresponse()
        body = resp.read(300)
        if resp.status == 404:
            raise SessionGone("会话已失效")
        if resp.status >= 400:
            raise RuntimeError("HTTP %s %s" % (
                resp.status, body.decode("utf-8", "replace")[:120]))
    finally:
        conn.close()


def _kill_proc(proc) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
    except Exception:
        pass


class DirectAgent(Agent):
    """房间 = 一个 ffmpeg 子进程；失败默认停在「待切换」，用户确认后才用浏览器。"""

    adopt_on_start = True
    engine = "direct"

    def __init__(self, base: str) -> None:
        # direct 不需要 Chrome；只有用户确认切换后才懒加载浏览器。
        super().__init__(base, need_browser=False)
        self.fallback: Agent | None = None
        self.fallback_engine = ""
        self.fallback_rooms: set[str] = set()
        # ask（默认）：失败后等人确认；auto：旧的无感回退；none：只报失败。
        mode = os.environ.get("LIVENOTES_DIRECT_FALLBACK", "").strip().lower()
        self.fallback_mode = mode if mode in FALLBACK_MODES else "ask"
        self.auto_restarts: dict[str, int] = {}
        self.restart_at: dict[str, float] = {}

    def apply_config(self, cfg: dict) -> None:
        """设置页热更新失败策略；环境变量仍优先，方便脚本临时覆盖。"""
        mode = str((cfg or {}).get("direct_fallback") or "").strip().lower()
        if mode not in FALLBACK_MODES:
            return
        env = os.environ.get("LIVENOTES_DIRECT_FALLBACK", "").strip().lower()
        if env in FALLBACK_MODES:
            mode = env
        if mode != self.fallback_mode:
            print("直取失败处理：%s -> %s" % (self.fallback_mode, mode), flush=True)
            self.fallback_mode = mode
            # 没在用的回退代理直接丢掉，下一次按新策略重新选引擎；
            # 已经有房间在回退浏览器里跑时保留，避免把正在录的一路打断。
            if not self.fallback_rooms and self.fallback is not None:
                old, self.fallback, self.fallback_engine = (
                    self.fallback, None, "")
                try:
                    old.shutdown()
                except Exception:
                    pass

    # ---------------------------------------------------------------- 工具
    def _title(self, room: dict) -> str:
        name = str(room.get("name") or "").strip()
        if not name:
            tail = str(room.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            name = "抖音 " + (tail or "直播")
        return "监听中心 · " + name[:60]

    def _capture_event(self, rid: str, kind: str, **extra) -> None:
        ev = {"kind": kind}
        ev.update(extra)
        self.report(rid, engine="direct", capture_event=ev)

    def _new_info(self, proc, sid: str, stream: str, adopted: bool = False) -> dict:
        now = time.time()
        return {
            "proc": proc, "engine": "direct", "sid": sid, "stream": stream,
            "started": now, "last_chunk_at": now, "last_ok_at": now,
            "chunks": 0, "restarts": 0, "first_audio_ms": None,
            "first_chunk": threading.Event(), "live_reported": False,
            "warned": False,
            "retry_at": 0.0, "renew_at": 0.0, "send_errors": 0,
            "adopted": adopted, "fallback": False, "rms": 0.0,
            "gen": 0, "recovering": False, "renewing": False,
            "eof": False, "stderr_tail": "",
        }

    def _start_reader(self, rid: str, room: dict, info: dict) -> None:
        gen = int(info.get("gen") or 0)
        threading.Thread(target=self._read_loop, args=(rid, room, info, gen),
                         name="direct-" + rid, daemon=True).start()

    def _start_stderr(self, proc, info: dict, gen: int) -> None:
        threading.Thread(target=self._drain_stderr, args=(proc, info, gen),
                         name="direct-err", daemon=True).start()

    def _drain_stderr(self, proc, info: dict, gen: int) -> None:
        try:
            while True:
                line = proc.stderr.readline()
                if not line:
                    return
                if int(info.get("gen") or 0) != gen:
                    return
                text = line.decode("utf-8", "replace").strip()
                if text:
                    info["stderr_tail"] = (str(info.get("stderr_tail") or "")
                                           + text + "\n")[-800:]
        except Exception:
            return

    def _report_page_url(self, rid: str, room: dict) -> str:
        """把真正的 live.douyin.com/<web_rid> 回报给引擎，供「显示页面」打开。

        直取只需要 reflow 页，但那里面的 room id 不是 web_rid；旧实现拿它拼
        地址，前端打开只会看到「服务器开小差了」。这里用 reflow 页 owner 的
        webRid；拿不到就不报，前端会退回打开 reflow 页。
        """
        try:
            meta = room_meta(room.get("url") or "", timeout=5.0)
        except Exception:
            return ""
        target = page_url(meta)
        if not target or target == room.get("url"):
            return ""
        room["resolved_url"] = target
        self.report(rid, url=target)
        return target

    # ---------------------------------------------------------------- 启动
    def bring_up(self, room: dict, session: str = "") -> None:
        rid = room["id"]
        if rid in self.running:
            return
        self.report(rid, "starting", stage=1, note="正在解析抖音流地址",
                    engine="direct", session_id=session or None)
        if not is_douyin(room.get("url") or ""):
            # 先判断再建会话：非抖音链接让浏览器自己建 tab 会话，
            # 不要留下一个 source_mode=direct 的空场次。
            self._fallback(room, {"sid": session or ""},
                           "direct 只支持抖音，改用浏览器采集")
            return
        # 先把名字补上再建会话：会话标题（飞书推送、导出文件名都用它）拼的是
        # 房间名，晚一步就得改名两遍。用户手动改过的名字（name_source=user）
        # 和采集端已经写过的真名都不要动，免得改了又变回去。
        # 名字只值一次 HTTP、拿不到就算了——再糟也不能让它挡住开播。
        src = str(room.get("name_source") or "")
        if src == "share" or (src == "" and not str(room.get("name") or "").strip()):
            try:
                nm = display_name(room_meta(room.get("url") or "", timeout=5.0))
            except Exception:
                nm = ""
            if nm and nm != room.get("name"):
                room["name"] = nm
                room["name_source"] = "page"
                self.report(rid, name=nm)
        sid = str(session or "")
        if not sid:
            try:
                res = api(self.base, "/api/live/start", {
                    "title": self._title(room), "source_mode": "direct",
                }, timeout=60)
                sid = str(res.get("session_id") or "")
            except Exception as e:
                self.fail(rid, "建会话失败：%s" % e, 1)
                return
            if not sid:
                self.fail(rid, "建会话没返回 session_id", 1)
                return
            # 建会话期间用户可能已经点了停止/删除：这条会话没有房间接管，
            # 先收掉，别让它变成孤儿（停止后迟到的上报也不该再点亮房间）。
            st = self._room_state(rid)
            if st in ("stopped", "failed", "__missing__"):
                self._stop_session(sid)
                return
            self.report(rid, "starting", stage=1,
                        note="已建会话，正在解析抖音流地址",
                        engine="direct", session_id=sid)

        t0 = time.monotonic()
        try:
            stream, note = resolve_douyin(room["url"])
        except Exception as e:
            self._capture_event(rid, "resolve_failed", error=str(e)[:200])
            self._fallback_or_fail(room, sid, "解析失败：%s" % e, 1)
            return
        resolve_ms = int((time.monotonic() - t0) * 1000)
        self._capture_event(rid, "resolve_ok", ms=resolve_ms, note=note)
        # 解析时已经抓过 reflow 页，meta 在缓存里：顺手把真实房号报给引擎，
        # 「显示页面」才有正确的 live.douyin.com 地址可开。
        self._report_page_url(rid, room)
        self.report(rid, "starting", stage=2,
                    note="已解析流地址（%.1f 秒），正在启动 ffmpeg"
                         % (resolve_ms / 1000.0),
                    engine="direct", session_id=sid)

        try:
            proc = spawn_ffmpeg(stream)
        except Exception as e:
            self._fallback_or_fail(room, sid, "ffmpeg 启动失败：%s" % e, 2)
            return

        info = self._new_info(proc, sid, stream)
        info["resolve_ms"] = resolve_ms
        self.running[rid] = info
        self.beat(rid, True, info)
        self._start_stderr(proc, info, 0)
        self._start_reader(rid, room, info)
        print("[%s] 直取已启动（session=%s，解析 %.1fs）"
              % (rid, sid, resolve_ms / 1000.0), flush=True)

        if not info["first_chunk"].wait(FIRST_CHUNK_WAIT_S):
            self.report(rid, "starting", stage=4,
                        note="ffmpeg 已启动，等待第一块音频",
                        engine="direct", session_id=sid)

    def _read_loop(self, rid: str, room: dict, info: dict, gen: int) -> None:
        proc = info.get("proc")
        if proc is None or proc.stdout is None:
            return
        buf = bytearray()
        try:
            while True:
                data = proc.stdout.read(CHUNK_BYTES)
                if not data:
                    break
                if int(info.get("gen") or 0) != gen:
                    return
                buf.extend(data)
                while len(buf) >= CHUNK_BYTES:
                    chunk = bytes(buf[:CHUNK_BYTES])
                    del buf[:CHUNK_BYTES]
                    if int(info.get("gen") or 0) != gen:
                        return
                    self._on_chunk(rid, room, info, chunk)
        except Exception:
            if int(info.get("gen") or 0) == gen:
                traceback.print_exc()
        finally:
            if int(info.get("gen") or 0) == gen:
                info["eof"] = True

    def _on_chunk(self, rid: str, room: dict, info: dict, chunk: bytes) -> None:
        sid = str(info.get("sid") or "")
        if not sid:
            return
        info["rms"] = pcm_rms(chunk)
        info["chunks"] = int(info.get("chunks") or 0) + 1
        info["last_chunk_at"] = time.time()
        if not info["first_chunk"].is_set():
            info["first_audio_ms"] = int(
                (time.time() - float(info.get("started") or time.time())) * 1000)
            info["first_chunk"].set()
            self.auto_restarts.pop(rid, None)
            self._capture_event(rid, "first_audio", ms=info["first_audio_ms"])
        try:
            post_chunk(self.base, sid, chunk)
            info["last_ok_at"] = time.time()
            info["send_errors"] = 0
            if not info.get("live_reported"):
                # 只有服务端真的收下音频块，才把房间报成 live。旧顺序先报再发：
                # 用户停止后迟到的第一块会把 stopped 重新点亮，_renew_session
                # 再看到 live 就重建会话——「点了停止还在录」会从这里复活。
                info["live_reported"] = True
                self.report(rid, "live", stage=6, note="音频采集已启动",
                            engine="direct", session_id=sid,
                            switch_ready=False, switch_requested=False,
                            switch_reason="")
        except SessionGone:
            self._renew_session(rid, room, info)
        except Exception as e:
            n = int(info.get("send_errors") or 0) + 1
            info["send_errors"] = n
            if n == 1 or n % 50 == 0:
                print("[%s] 发送音频块失败（第 %d 次）：%s" % (rid, n, e), flush=True)

    def _room_state(self, rid: str) -> str:
        """问服务端这个房间现在是什么状态。查不到返回空串。

        只有服务端明确给出了房间列表、里面却没有这个 id，才算「房间已删除」；
        响应形状不对（缺 rooms 字段）一律按「不知道」处理——宁可按原计划
        继续，也不要在刚建好会话后误把用户还在启动的房间收掉。
        """
        try:
            payload = api(self.base, "/api/rooms", timeout=2.5)
        except Exception:
            return ""
        rooms = (payload or {}).get("rooms")
        if not isinstance(rooms, list):
            return ""
        for r in rooms:
            if str(r.get("id") or "") == rid:
                return str(r.get("state") or "")
        return "__missing__"

    def _renew_session(self, rid: str, room: dict, info: dict) -> None:
        """服务重启/会话被清后 404：只有房间还在听时才建新会话接着录。

        用户点「停止」也会让会话 404；旧版不看房间状态就重建，结果
        「点了停止还在录」。房间已停止/删除时直接回收 ffmpeg。
        """
        if info.get("renewing"):
            return
        now = time.time()
        if now < float(info.get("renew_at") or 0.0):
            return
        st = self._room_state(rid)
        if st not in ("live", "starting"):
            if st in ("stopped", "failed", "__missing__"):
                self.reap(rid, "房间已停止/删除", stop_session=False)
            else:
                # 服务暂时不可达：下一块再试，但先压一个重试间隔，别让读线程
                # 每个音频块都打一次 2.5 秒超时的 /api/rooms。
                info["renew_at"] = now + 3.0
            return
        info["renewing"] = True
        info["renew_at"] = now + 3.0
        try:
            res = api(self.base, "/api/live/start", {
                "title": self._title(room), "source_mode": "direct",
            }, timeout=60)
            sid = str(res.get("session_id") or "")
            if not sid:
                return
            info["sid"] = sid
            info["first_chunk"].clear()
            info["live_reported"] = False
            self.report(rid, "live", stage=6, note="会话已失效，已重建",
                        engine="direct", session_id=sid)
            print("[%s] 会话已失效，重建 session=%s" % (rid, sid), flush=True)
        except Exception as e:
            print("[%s] 会话失效，重建失败：%s" % (rid, e), flush=True)
        finally:
            info["renewing"] = False

    # ---------------------------------------------------------------- 守护
    def _check(self, room: dict, info: dict) -> None:
        if info.get("fallback") or info.get("recovering") or info.get("renewing"):
            return
        rid = room["id"]
        now = time.time()
        proc = info.get("proc")
        if proc is not None and proc.poll() is not None:
            tail = str(info.get("stderr_tail") or "").strip().splitlines()
            why = "ffmpeg 退出（rc=%s%s）" % (
                proc.returncode, ("：" + tail[-1][:160]) if tail else "")
            self._recover(room, info, why)
            return
        if info.get("eof"):
            self._recover(room, info, "ffmpeg 输出结束")
            return
        last_ok = float(info.get("last_ok_at") or info.get("started") or now)
        idle = now - last_ok
        if idle >= DIRECT_RECOVER_AFTER_S:
            if now < float(info.get("retry_at") or 0.0):
                return
            self._recover(room, info, "%.0f 秒没有成功送出音频块" % idle)
            return
        if idle >= DIRECT_WARN_S and not info.get("warned"):
            info["warned"] = True
            self.report(rid, "live", stage=6,
                        note="采集断流：%.0f 秒没有音频块" % idle,
                        engine="direct", session_id=info.get("sid"))
        elif idle < 5:
            info["warned"] = False

    def _recover(self, room: dict, info: dict, reason: str) -> None:
        rid = room["id"]
        if info.get("recovering"):
            return
        info["recovering"] = True
        try:
            n = int(info.get("restarts") or 0) + 1
            if n > DIRECT_MAX_RESTARTS:
                self._fallback(room, info, "重建 %d 次仍失败：%s"
                               % (DIRECT_MAX_RESTARTS, reason))
                return
            info["restarts"] = n
            print("[%s] 直取断流：%s；第 %d 次重建" % (rid, reason, n), flush=True)
            self._capture_event(rid, "reconnect", reason=reason[:200])
            self.report(rid, "live", stage=6,
                        note="采集断流，正在重建（第 %d 次）" % n,
                        engine="direct", session_id=info.get("sid"))

            old = info.get("proc")
            info["gen"] = int(info.get("gen") or 0) + 1
            info["proc"] = None
            info["eof"] = False
            info["warned"] = False
            info["first_chunk"].clear()
            info["chunks"] = 0
            info["last_ok_at"] = time.time()
            _kill_proc(old)

            try:
                stream, note = resolve_douyin(room["url"])
            except Exception as e:
                info["retry_at"] = time.time() + DIRECT_RETRY_S
                self.report(rid, "live", stage=6,
                            note="重新解析失败，%d 秒后重试：%s"
                                 % (int(DIRECT_RETRY_S), str(e)[:120]),
                            engine="direct", session_id=info.get("sid"))
                return
            info["retry_at"] = 0.0
            try:
                proc = spawn_ffmpeg(stream)
            except Exception as e:
                info["retry_at"] = time.time() + DIRECT_RETRY_S
                self.report(rid, "live", stage=6,
                            note="ffmpeg 重启失败，%d 秒后重试：%s"
                                 % (int(DIRECT_RETRY_S), str(e)[:120]),
                            engine="direct", session_id=info.get("sid"))
                return
            info["proc"] = proc
            info["stream"] = stream
            info["started"] = time.time()
            info["last_ok_at"] = time.time()
            self.beat(rid, True, info)
            self._start_stderr(proc, info, int(info["gen"]))
            self._start_reader(rid, room, info)
            print("[%s] 直取已重建（%s）" % (rid, note), flush=True)
        finally:
            info["recovering"] = False

    # ---------------------------------------------------------------- 回退
    def _get_fallback(self) -> Agent | None:
        if self.fallback is not None:
            return self.fallback
        mode = self.fallback_mode
        # auto / ask 先试独立浏览器（最稳），不可用再试共享；显式 shared 则反过来。
        order = (["shared", "per_room"] if mode == "shared"
                 else ["per_room", "shared"] if mode in ("per_room", "auto", "ask")
                 else [])
        for engine in order:
            try:
                if engine == "shared":
                    exe = find_shared_browser()
                    self.fallback = SharedAgent(self.base, SharedBrowser(exe, self.base))
                    print("直取回退：共享浏览器（%s）" % exe, flush=True)
                else:
                    self.fallback = Agent(self.base)
                    print("直取回退：每房间一个浏览器", flush=True)
                self.fallback_engine = engine
                return self.fallback
            except SystemExit as e:
                print("浏览器回退不可用（%s）：%s" % (engine, e), flush=True)
        self.fallback = None
        return None

    def _fallback_or_fail(self, room: dict, sid: str, reason: str,
                          stage: int) -> None:
        """启动阶段失败：按 fallback_mode 决定是等人确认还是直接切浏览器。"""
        self._fallback(room, {"sid": sid}, reason, stage=stage)

    def _probe_fallback_engine(self) -> str:
        """只探测「能不能切」，不提前拉起浏览器。"""
        mode = self.fallback_mode
        if mode == "none":
            return ""
        if mode == "shared":
            try:
                find_shared_browser()
                return "shared"
            except SystemExit:
                pass
        try:
            find_chrome()
            return "per_room"
        except SystemExit:
            pass
        if mode in ("shared", "auto", "ask"):
            try:
                find_shared_browser()
                return "shared"
            except SystemExit:
                pass
        return ""

    def _switch_to_browser(self, room: dict, confirmed: bool = True) -> None:
        """切到浏览器采集。confirmed=False 用于设置页改成 auto 后的补切。"""
        rid = room["id"]
        reason = str(room.get("switch_reason") or room.get("error") or "直取失败")
        fb = self._get_fallback()
        if fb is None:
            why = "直取失败：%s（没有可用的浏览器采集）" % reason
            self.report(rid, "failed", stage=room.get("stage") or 1,
                        failed_stage=room.get("failed_stage") or 1, error=why,
                        engine="direct", switch_ready=False,
                        switch_requested=False, switch_reason=reason[:200])
            return
        sid = str(room.get("session_id") or "")
        if sid and not self.session_alive(room):
            # 旧会话可能已被断流看门狗收尾：让浏览器自己新建一场，别硬塞旧 id。
            sid = ""
        self._capture_event(rid, "switch_confirmed" if confirmed else "fallback",
                            reason=reason[:200], engine=self.fallback_engine)
        action = "用户确认切换" if confirmed else "按设置自动切换"
        print("[%s] %s，改用浏览器采集：%s" % (rid, action, reason), flush=True)
        label = "共享浏览器" if self.fallback_engine == "shared" else "独立浏览器"
        self.report(rid, "starting", stage=1,
                    note=("已确认切换，正在启动%s" % label
                          if confirmed else "按设置自动切换采集方式"),
                    engine=self.fallback_engine, session_id=sid or None,
                    error="", switch_ready=False, switch_requested=False,
                    switch_reason="")
        self.fallback_rooms.add(rid)
        try:
            fb.bring_up(room, session=sid)
        except Exception as e:
            traceback.print_exc()
            self.fallback_rooms.discard(rid)
            fb.fail(rid, "浏览器回退失败：%s" % e, 1)
            if sid:
                self._stop_session(sid)

    def _fallback(self, room: dict, info: dict, reason: str,
                  stage: int = 1) -> None:
        rid = room["id"]
        sid = str(info.get("sid") or room.get("session_id") or "")
        if self.fallback_mode == "ask":
            # 不自动开浏览器：把失败和切换入口亮在收听页，等用户点头。
            engine = self._probe_fallback_engine()
            self._capture_event(rid, "switch_needed", reason=reason[:200],
                                engine=engine)
            why = "直取失败：%s" % reason
            if not engine:
                why += "（也没有找到可用的浏览器采集）"
            print("[%s] 直取失败，等待用户确认切换：%s" % (rid, reason), flush=True)
            self.report(rid, "failed", stage=stage, failed_stage=stage,
                        error=why, engine="direct", session_id=sid or None,
                        switch_ready=bool(engine), switch_engine=engine,
                        switch_reason=reason[:200], switch_requested=False)
            # 会话先留着（看门狗 120 秒后才收尾）：用户马上点切换能接着同一场录。
            self.reap(rid, "等待用户确认切换", stop_session=False)
            return
        fb = self._get_fallback()
        if fb is None:
            self._capture_event(rid, "fallback", reason=reason[:200])
            self.fail(rid, reason, stage)
            if sid:
                self._stop_session(sid)
            return
        self._capture_event(rid, "fallback", reason=reason[:200])
        print("[%s] 直取失败，回退浏览器：%s" % (rid, reason), flush=True)
        self.report(rid, "starting", stage=1,
                    note="直取失败，切换浏览器采集：%s" % reason[:120],
                    engine=self.fallback_engine,
                    session_id=sid or None,
                    switch_ready=False, switch_requested=False,
                    switch_reason="")
        self.reap(rid, "回退浏览器", stop_session=False)
        self.fallback_rooms.add(rid)
        try:
            fb.bring_up(room, session=sid)
        except Exception as e:
            traceback.print_exc()
            self.fallback_rooms.discard(rid)
            fb.fail(rid, "浏览器回退失败：%s" % e, 1)
            if sid:
                self._stop_session(sid)

    # ---------------------------------------------------------------- 回收
    def reap(self, rid: str, reason: str = "", stop_session: bool = False) -> None:
        info = self.running.pop(rid, None)
        if not info:
            return
        with self._beat_lock:
            self._beat(rid, False, info, reason or "direct stopped")
        info["gen"] = int(info.get("gen") or 0) + 1
        _kill_proc(info.get("proc"))
        if stop_session and info.get("sid"):
            self._stop_session(info["sid"])
        if reason:
            print("[%s] 回收：%s" % (rid, reason), flush=True)

    def _stop_session(self, sid: str) -> None:
        try:
            api(self.base, "/api/live/stop", {"session_id": sid}, timeout=20)
        except Exception:
            pass        # 服务端即使超时也把收尾跑完了；没停的话看门狗兜底

    def fail(self, rid: str, msg: str, stage: int = 1) -> None:
        print("[%s] 直取失败（第 %d 步）：%s" % (rid, stage, msg), flush=True)
        info = self.running.get(rid) or {}
        sid = str(info.get("sid") or "")
        self.report(rid, "failed", stage=stage, failed_stage=stage, error=msg,
                    engine="direct", session_id=sid or None,
                    switch_ready=False, switch_requested=False, switch_reason="")
        self.reap(rid, "启动失败", stop_session=False)
        if sid:
            self._stop_session(sid)

    # ---------------------------------------------------------------- 主循环
    def tick(self, rooms: list[dict]) -> None:
        alive_ids = {r["id"] for r in rooms}
        for rid in list(self.running):
            if rid not in alive_ids:
                self.reap(rid, "房间已删除")
        for rid in list(self.auto_restarts):
            if rid not in alive_ids:
                self.auto_restarts.pop(rid, None)
                self.restart_at.pop(rid, None)

        # 已经回退的房间交给浏览器代理；传空列表也要 tick 一次，让它回收
        # 已删除/已停止的房间。
        if self.fallback is not None:
            fb_rooms = [r for r in rooms if r["id"] in self.fallback_rooms]
            self.fallback.tick(fb_rooms)
            for rid in list(self.fallback_rooms):
                if rid not in alive_ids:
                    self.fallback_rooms.discard(rid)

        for r in rooms:
            rid = r["id"]
            if rid in self.fallback_rooms:
                continue
            if r.get("switch_requested") and r.get("switch_ready"):
                # 用户在收听页确认了「切换到浏览器采集」：优先于当前策略。
                self._switch_to_browser(r)
                continue
            if (r.get("state") == "failed" and r.get("switch_ready")
                    and self.fallback_mode in ("auto", "per_room", "shared")):
                # 设置页刚改成自动切换：已经停在待切换的房间也立刻补切。
                self._switch_to_browser(r, confirmed=False)
                continue
            if (r.get("state") == "failed" and r.get("switch_ready")
                    and self.fallback_mode == "none"):
                # 策略改成「只报失败」：撤掉切换入口，别让页面继续让人点。
                self.report(rid, "failed", switch_ready=False, switch_engine="",
                            switch_reason="", switch_requested=False)
                continue
            state = r.get("state")
            if state == "stopped":
                if rid in self.running:
                    self.reap(rid, "已停止")
                if (r.get("auto_stopped")
                        and "音频块" in str(r.get("auto_stop_reason") or "")):
                    self._maybe_restart(r)
                continue
            if state == "failed":
                if rid in self.running:
                    self.reap(rid, "采集失败")
                continue
            if state == "pending" and rid in self.running:
                self.reap(rid, "重新排队")

            info = self.running.get(rid)
            if info is not None:
                self._check(r, info)
                continue
            if state in ("pending", "starting", "live"):
                try:
                    self.bring_up(r, session=str(r.get("session_id") or ""))
                except Exception as e:
                    traceback.print_exc()
                    self.fail(rid, "拉起失败：%s" % e, 1)
                time.sleep(1.0)

    def _maybe_restart(self, room: dict) -> None:
        """看门狗因断流收尾后，流还在就重新拉起；真下播不折腾。"""
        rid = room["id"]
        n = int(self.auto_restarts.get(rid) or 0)
        if n >= DIRECT_MAX_RESTARTS:
            return
        now = time.time()
        if now < float(self.restart_at.get(rid) or 0.0):
            return
        self.restart_at[rid] = now + 60.0
        try:
            resolve_douyin(room["url"])
        except Exception:
            return
        self.auto_restarts[rid] = n + 1
        print("[%s] 断流收尾后流仍在，重新拉起直取（第 %d 次）"
              % (rid, n + 1), flush=True)
        self.report(rid, "pending", stage=0,
                    note="断流收尾后重新拉起直取", engine="direct",
                    session_id="", auto_stopped=False, auto_stop_reason="")
        room["state"] = "pending"
        room["session_id"] = ""
        room["auto_stopped"] = False
        room["auto_stop_reason"] = ""

    # ---------------------------------------------------------------- 接管
    def adopt(self, rooms: list[dict]) -> None:
        direct_rooms: list[dict] = []
        fallback_rooms: list[dict] = []
        for r in rooms:
            if r.get("state") not in ("live", "starting"):
                continue
            eng = str(r.get("capture_engine") or "")
            # 空 engine 是老数据/切换模式前留下的房间：按浏览器接管，
            # 避免和还在跑的采集页抢同一个房间。
            if eng == "direct":
                direct_rooms.append(r)
            else:
                fallback_rooms.append(r)
        for r in direct_rooms:
            self._adopt_direct(r)
        if fallback_rooms:
            fb = self._get_fallback()
            if fb is not None:
                for r in fallback_rooms:
                    self.fallback_rooms.add(r["id"])
                fb.adopt(fallback_rooms)

    def _adopt_direct(self, room: dict) -> None:
        rid = room["id"]
        if rid in self.running:
            return
        sid = str(room.get("session_id") or "")
        if not sid or not self.session_alive(room):
            return
        try:
            stream, note = resolve_douyin(room["url"])
            self._report_page_url(rid, room)
            proc = spawn_ffmpeg(stream)
        except Exception as e:
            print("[%s] 接管直取失败：%s" % (rid, e), flush=True)
            return
        info = self._new_info(proc, sid, stream, adopted=True)
        self.running[rid] = info
        self.beat(rid, True, info)
        self._start_stderr(proc, info, 0)
        self._start_reader(rid, room, info)
        print("[%s] 接管已在跑的直取采集（session=%s，%s）"
              % (rid, sid, note), flush=True)

    def shutdown(self) -> None:
        for info in list(self.running.values()):
            info["gen"] = int(info.get("gen") or 0) + 1
            _kill_proc(info.get("proc"))
        self.running.clear()
        super().shutdown()
        if self.fallback is not None:
            self.fallback.shutdown()
