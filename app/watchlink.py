# -*- coding: utf-8 -*-
"""「贴链接就开听」的判定：这条链接能不能听、要不要先让人点一下确认。

为什么要这道闸：/watch 会把用户给的任意 URL 交给一个真浏览器去打开、录音。
没有闸的时候，分享文案里夹着的一条短链就能让采集代理去开一个陌生站点。
所以按顺序过四道闸，任何一道不过都给出人话理由，不建房间、也不弹卡片：

  1 形式  只收 http/https；不认内嵌账号密码；端口只认 80/443
  2 落地  短链先跟一次跳转，落到 /video/ 之类就直接说"这是短视频，不是直播间"
  3 形状  path 要像某个直播间，不能是首页 / 个人主页 / 视频页
  4 域名  已知平台直接开；陌生域名第一次要用户点"允许"，之后长期免问

容量（最多几路）不在这里判：那需要房间列表，在调用方（tools/feishu_bot.py）。

本模块只读判定结果，写白名单发生在 allow() 里；建房间由调用方负责。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable

from .config import DATA_DIR
from .rooms import normalize_url

PATH = DATA_DIR / "watch_trust.json"
_LOCK = threading.Lock()

# 单机跑得动的路数。5 路实测约 3.7GB 内存 / 0.74 核，再多就是拿稳定性换数量。
MAX_ROOMS = int(os.environ.get("LIVENOTES_MAX_ROOMS", "5"))

# 已知平台：不用问，直接开。按可注册域名匹配，子域一并算（live./www./v. 都通）。
PRESET_SITES = ("douyin.com", "amemv.com", "kuaishou.com", "bilibili.com",
                "huya.com", "douyu.com")

# 分享出来的短链落地页不是 douyin.com，而是字节自己的 webcast 站：
#   https://v.douyin.com/xxx/  ->  https://webcast.amemv.com/douyin/webcast/reflow/<id>
# 注意：reflow 后面的 <id> 是**分享/回流 id，不是直播间的 web_rid**。
# 实测 reflow/7700000000000000002 在浏览器里会落到 live.douyin.com/123456789012；
# 如果按 <id> 直接换成 live.douyin.com/7700000000000000002，抖音会回「服务器开小差了」。
# 所以这里只把一堆 did / sec_user_id / share_id 参数去掉，保留能用的落地页，房号交给浏览器。
_CANON = [
    (re.compile(r"^/douyin/webcast/reflow/(\d+)$"),
     "https://webcast.amemv.com/douyin/webcast/reflow/{0}"),
    (re.compile(r"^/follow/live/(\d+)$"), "https://live.douyin.com/{0}"),
]

# 短链：域名本身就说明"这是个跳转"，必须先展开才知道落点
SHORT_HOSTS = ("v.douyin.com", "v.kuaishou.com", "v.huoshan.com", "b23.tv")

# 明确不是"某个直播间"的路径。先否掉再判形状，理由才说得准。
_DENY_PATH = re.compile(
    r"^/(user|video|note|share/(video|user|note|live)|profile|short-video|discover|"
    r"search|hot|follow|explore|feed)(/|$)", re.I)

# 各平台"直播间"的 path 形状：(正则, 说明)。room_key 取第 1 个捕获组。
_SITE_SHAPE: dict[str, tuple[re.Pattern, str]] = {
    "douyin.com": (re.compile(r"^/(\d{5,})/?$"), "抖音直播间一般是 live.douyin.com/房间号"),
    "kuaishou.com": (re.compile(r"^/u/([A-Za-z0-9_-]{4,})/?$"), "快手直播间是 live.kuaishou.com/u/xxx"),
    "bilibili.com": (re.compile(r"^/(\d{3,})/?$"), "B 站直播间是 live.bilibili.com/房间号"),
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_URL_RE = re.compile(r"https?://[^\s<>\"'）】」，。；、]+", re.I)
_BARE_RE = re.compile(r"(?:[a-z0-9-]+\.)+[a-z]{2,}/[^\s<>\"'）】」，。；、]*", re.I)
_TAIL = ".,:;!?、，。；：！？)]}）】」》"

# 分享文案里的主播名：【示例主播】正在直播。同一句话里还有【抖音】（让人
# 打开 App 用），所以先认「】正在直播」这个紧邻结构，再退回按黑名单过滤。
# 这只是"先垫个名字"：真名由采集端落地后从页面读，认错也就错几秒。
_SHARE_NAME = re.compile(r"【([^】\n]{1,40})】\s*(?:正在直播|直播中|的直播间)")
_SHARE_ANY = re.compile(r"【([^】\n]{1,40})】")
_NOT_A_NAME = {"抖音", "快手", "b站", "哔哩哔哩", "bilibili", "小红书",
               "视频号", "淘宝", "直播", "电商"}

PENDING_TTL_S = int(os.environ.get("LIVENOTES_CONFIRM_TTL", "600"))
PENDING_MAX = 3


class Reject(Exception):
    """链接不合格。str(e) 就是给用户看的那句话。"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 链接

def extract_all(text: str) -> list[str]:
    """从一段话里捞出所有链接。抖音的分享文案是"一串话 + 链接 + 一串话"。"""
    found = _URL_RE.findall(text or "")
    if not found:
        found = _BARE_RE.findall(text or "")
    out: list[str] = []
    for u in found:
        u = u.rstrip(_TAIL)
        if u and u not in out:
            out.append(u)
    return out


def extract(text: str) -> str:
    got = extract_all(text)
    return got[0] if got else ""


def share_label(text: str) -> str:
    """分享文案里的主播名，认不出来返回空串。

    为什么要它：房间建好到采集端落地有 5~10 秒，这段时间没有名字，收听页
    只能显示域名；而分享文案里其实已经写着【主播名】。先垫一个上去，
    这段时间也认得出是哪一路。它不是真名，采集端读到页面后会覆盖掉。
    """
    t = text or ""
    m = _SHARE_NAME.search(t)
    if not m:
        for cand in _SHARE_ANY.finditer(t):
            if cand.group(1).strip().lower() not in _NOT_A_NAME:
                m = cand
                break
    if not m:
        return ""
    name = re.sub(r"\s+", " ", m.group(1)).strip()
    return name[:30]


def site_of(host: str) -> str:
    """可注册域名。example.com.cn 这类要留三段，否则白名单会把整个 com.cn 放进来。"""
    host = (host or "").lower().strip(".")
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return host
    two_level = ("com.cn", "net.cn", "org.cn", "gov.cn", "com.hk", "com.tw", "co.jp", "co.uk")
    if ".".join(parts[-2:]) in two_level:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_short(host: str) -> bool:
    return (host or "").lower() in SHORT_HOSTS


def _allow_local() -> bool:
    return os.environ.get("LIVENOTES_ALLOW_LOCAL", "").lower() in ("1", "true", "yes")


def _parse(url: str, _canon: bool = True) -> dict:
    """拆 URL 并做"形式"这道闸。不合格抛 Reject。

    _canon=False 只给内部换标准地址用：换完不能再套一遍 _CANON，
    否则 reflow 会自己映射到自己，无限递归。
    """
    u = normalize_url(url)
    if not u:
        raise Reject("链接是空的。")
    p = urllib.parse.urlsplit(u)
    if p.scheme not in ("http", "https"):
        raise Reject("只认 http/https 的直播链接。")
    if "@" in p.netloc:
        raise Reject("链接里带了账号密码，我不替你打开这种。")
    host = (p.hostname or "").lower().strip(".")
    if not host:
        raise Reject("这个链接里没有站名，我打不开。")

    local = False
    try:
        ip = ipaddress.ip_address(host)
        local = ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        local = host == "localhost" or host.endswith(".local")
    try:
        port = p.port
    except ValueError:
        raise Reject("端口写错了。")
    if (port not in (None, 80, 443) or local) and not _allow_local():
        raise Reject("这是本机/内网地址，不是直播间链接。")

    path = p.path or "/"
    site = site_of(host)
    if _canon and site in ("amemv.com", "douyin.com"):
        for pat, tpl in _CANON:
            m = pat.match(path.rstrip("/") or "/")
            if m:
                return _parse(tpl.format(*m.groups()), _canon=False)
    return {"url": u, "host": host, "site": site, "path": path,
            "scheme": p.scheme, "port": port}


def resolve(url: str, timeout: float = 5.0) -> tuple[str, str]:
    """跟一次跳转，把短链换成落地页。返回 (落地 URL, 错误)。

    网络不通时原样返回 —— 搬去没网的地方调试，不该连链接都贴不进去。
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl() or url
            try:
                r.read(2048)
            except Exception:
                pass
            return final, ""
    except urllib.error.HTTPError as e:
        loc = ""
        try:
            loc = e.headers.get("Location") or ""
        except Exception:
            pass
        return (loc or url), ""
    except Exception as e:
        return url, str(e)[:60]


def _shape(p: dict) -> str:
    """"形状"这道闸：这条 path 是不是"某个直播间"。返回 room_key，不合格抛 Reject。"""
    path = p["path"]
    if path in ("/", ""):
        raise Reject("这看着是网站首页，不是某个直播间。")
    if _DENY_PATH.search(path):
        raise Reject("这是个人主页/短视频页，不是直播间。直播间链接长这样："
                     "https://live.douyin.com/123456")
    rule = _SITE_SHAPE.get(p["site"])
    if rule:
        pat, hint = rule
        m = pat.match(path)
        if not m:
            raise Reject("这个链接不像直播间。%s" % hint)
        return m.group(1)
    segs = [s for s in path.split("/") if s]
    if not segs or len(segs[-1]) < 3:
        raise Reject("这个链接不像某个直播间，我不敢直接开。")
    return segs[-1]


# ---------------------------------------------------------------- 白名单

def _read() -> dict:
    if not PATH.is_file():
        return {"domains": {}, "pending": []}
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except ValueError:
        return {"domains": {}, "pending": []}
    data.setdefault("domains", {})
    data.setdefault("pending", [])
    return data


def _write(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def trusted(site: str) -> bool:
    if not site:
        return False
    if site in PRESET_SITES:
        return True
    return site in _read()["domains"]


def _host_trusted(host: str) -> bool:
    """逐级往上问：live.douyin.com -> douyin.com。子域跟着父域走。"""
    h = (host or "").lower().strip(".")
    while h:
        if trusted(h):
            return True
        if h.count(".") < 1:
            return False
        h = h.split(".", 1)[1]
    return False


def allow(site: str, note: str = "") -> dict:
    with _LOCK:
        data = _read()
        data["domains"][site] = {"added_at": now(), "note": note or ""}
        _write(data)
        return data["domains"][site]


def forget(site: str) -> bool:
    with _LOCK:
        data = _read()
        if site not in data["domains"]:
            return False
        del data["domains"][site]
        _write(data)
        return True


def trust_list() -> dict:
    return _read()["domains"]


# ---------------------------------------------------------------- 待确认

_ALPHABET = "ACDEFGHJKLMNPQRSTUVWXY3456789"      # 去掉 0/O/1/I/B，念出来不会错


def _codes() -> set[str]:
    return {str(p.get("code")) for p in _read()["pending"]}


def pending_new(url: str, site: str, open_id: str = "", label: str = "") -> dict:
    with _LOCK:
        data = _read()
        # 同一个链接再发一次，不该多出第二个确认码：卡片会在聊天里叠成一堆
        for p in data["pending"]:
            if p.get("url") == url and float(p.get("expires_at") or 0) > time.time():
                p["expires_at"] = time.time() + PENDING_TTL_S
                _write(data)
                return p
        taken = _codes()
        code = "".join(secrets.choice(_ALPHABET) for _ in range(4))
        while code in taken:
            code = "".join(secrets.choice(_ALPHABET) for _ in range(4))
        item = {"code": code, "url": url, "site": site, "open_id": open_id,
                # 分享文案里的主播名：用户点「允许」之后才建房间，名字得
                # 跟着这条待确认记录过去，否则那段空窗期又只剩域名。
                "label": str(label or "")[:30],
                "created_at": now(), "expires_at": time.time() + PENDING_TTL_S}
        # 只留最近几条：确认码是给人念的，堆一屏等于没有
        alive = [p for p in data["pending"] if float(p.get("expires_at") or 0) > time.time()]
        data["pending"] = (alive + [item])[-PENDING_MAX:]
        _write(data)
        return item


def pending_get(code: str) -> dict | None:
    code = (code or "").strip().upper()
    for p in _read()["pending"]:
        if str(p.get("code")) == code and float(p.get("expires_at") or 0) > time.time():
            return p
    return None


def pending_take(code: str) -> dict | None:
    code = (code or "").strip().upper()
    with _LOCK:
        data = _read()
        keep, hit = [], None
        for p in data["pending"]:
            if str(p.get("code")) == code and float(p.get("expires_at") or 0) > time.time():
                hit = p
            else:
                keep.append(p)
        if hit:
            data["pending"] = keep
            _write(data)
        return hit


def pending_codes() -> list[str]:
    return sorted({str(p.get("code")) for p in _read()["pending"]
                   if float(p.get("expires_at") or 0) > time.time()})


def pending_unique() -> dict | None:
    """只有一个待确认时，用户回一句"确认"就够了，不用抄确认码。"""
    alive = [p for p in _read()["pending"] if float(p.get("expires_at") or 0) > time.time()]
    return alive[0] if len(alive) == 1 else None


def pending_count() -> int:
    return len([p for p in _read()["pending"] if float(p.get("expires_at") or 0) > time.time()])


def pending_ttl_min() -> int:
    return max(1, PENDING_TTL_S // 60)


# ---------------------------------------------------------------- 判定

def _append_note(old: str, new: str) -> str:
    return (old + "\n" + new) if old else new


def decide(text: str, resolver: Callable[[str], tuple[str, str]] | None = None) -> dict:
    """把一条消息判成三选一：直接开（watch）/ 先确认（confirm）/ 退回去（reject）。

    返回字典而不是抛异常：调用方要拿 note、room_key 去拼回复。
    """
    out: dict[str, Any] = {"ok": False, "kind": "reject", "url": "", "host": "",
                           "site": "", "room": "", "reason": "", "note": "",
                           "resolved_from": "", "label": share_label(text)}
    links = extract_all(text)
    if not links:
        out["reason"] = ("没看到链接。把直播间链接直接粘给我就行，"
                         "比如 https://live.douyin.com/123456")
        return out
    if len(links) > 1:
        out["note"] = "一次发一个链接就行，我先开第一个。"

    raw = links[0]
    try:
        p = _parse(raw)
    except Reject as e:
        out["reason"] = str(e)
        return out

    # 短链：先跟一次跳转，落在视频页/主页的，这时候就能说清楚
    short_unresolved = False
    if resolver is not None and is_short(p["host"]):
        final, err = resolver(raw)
        if err:
            # 展不开就没法判形状，按原样交给采集端——离线时不该连短链都贴不进来
            short_unresolved = True
            out["note"] = _append_note(out["note"],
                                       "（短链没能展开：%s，按原样试）" % err)
        elif final and final != raw:
            try:
                p2 = _parse(final)
            except Reject as e:
                out.update(url=final, host=p["host"], site=p["site"])
                out["reason"] = "短链跳到 %s，%s" % (urllib.parse.urlsplit(final).netloc, e)
                return out
            out["resolved_from"] = raw
            p = p2
            out["note"] = _append_note(out["note"], "（短链已展开为 %s）" % p2["url"][:70])

    out.update(url=p["url"], host=p["host"], site=p["site"])
    try:
        out["room"] = _shape(p)
    except Reject as e:
        if not short_unresolved:
            out["reason"] = str(e)
            return out
        out["room"] = p["path"].strip("/") or p["host"]

    if _host_trusted(p["host"]):
        out.update(ok=True, kind="watch")
        return out

    out.update(kind="confirm")
    return out


def ttl_text() -> str:
    return "%d 分钟" % pending_ttl_min()
