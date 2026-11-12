# -*- coding: utf-8 -*-
"""飞书片段：解析「/分享 CH01 90秒」，从 spool/WAV/m4a 切一段 opus。

片段是分享的主产品：对方听到的就是那 90 秒，不需要一个能拖时间轴的外部分享页。
所以这里只做一件事——给定会话和时间范围，产出一个可上传飞书的 opus 文件。
"""
from __future__ import annotations

import re
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import audio, store
from .config import AUDIO_DIR, CLIPS_DIR, SAMPLE_RATE

DEFAULT_SECONDS = 90.0
MIN_SECONDS = 5.0
MAX_SECONDS = 300.0
CACHE_TTL_S = 120.0
_RATE_LOCK = threading.Lock()
_LAST_SHARE: dict[str, float] = {}


def parse_seconds(token: str) -> float | None:
    """把 90秒 / 1分30秒 / 3分钟 / 1:30 / 00:01:30 统一成秒。"""
    s = str(token or "").strip().lower().replace("：", ":")
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", s)
    if m:
        a, b, c = m.groups()
        if c is not None:
            return int(a) * 3600 + int(b) * 60 + int(c)
        return int(a) * 60 + int(b)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:小时|h|hour)", s)
    if m:
        return float(m.group(1)) * 3600
    m = re.fullmatch(r"(?:(\d+)\s*分(?:钟)?)?\s*(\d+(?:\.\d+)?)\s*秒?", s)
    if m and (m.group(1) is not None or "秒" in s or s.endswith("s")):
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:分钟|分|m|min)", s)
    if m:
        return float(m.group(1)) * 60
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|sec|秒)", s)
    if m:
        return float(m.group(1))
    return None


def _token_kind(token: str) -> str:
    s = str(token or "").strip().lower()
    if re.fullmatch(r"\d{1,2}:\d{1,2}(?::\d{1,2})?", s):
        return "start"          # 1:30 这种写法默认是"从 1:30 开始"
    return "duration"


def parse_share_args(text: str) -> dict:
    """返回 {ch, start, duration, error}。

    规则：
      /分享 CH01 90秒           -> 最近 90 秒
      /分享 CH01 1:30           -> 从 1:30 开始，默认 90 秒
      /分享 CH01 1:30 2分钟      -> 从 1:30 开始，2 分钟
      /分享 CH01 2分钟           -> 最近 2 分钟
    """
    raw = str(text or "").strip()
    raw = re.sub(r"^/(?:分享|share)\b", "", raw, flags=re.I).strip(" ：:")
    m = re.search(r"(?i)CH\s*0*(\d{1,3})", raw)
    ch = ("CH%02d" % int(m.group(1))) if m else ""
    if m:
        raw = (raw[:m.start()] + " " + raw[m.end():]).strip()

    tokens = [t for t in re.split(r"[\s,，、;；]+", raw) if t]
    times: list[tuple[str, float]] = []
    for t in tokens:
        v = parse_seconds(t)
        if v is not None:
            times.append((_token_kind(t), v))

    start: float | None = None
    duration = DEFAULT_SECONDS
    if len(times) == 1:
        kind, v = times[0]
        if kind == "start":
            start = v
        else:
            duration = v
    elif len(times) >= 2:
        start = times[0][1]
        duration = times[1][1]

    duration = max(MIN_SECONDS, min(MAX_SECONDS, float(duration)))
    if start is not None:
        start = max(0.0, float(start))
    return {"ch": ch, "start": start, "duration": duration, "error": ""}


@dataclass
class Clip:
    path: Path
    start: float
    duration: float
    total: float
    source: str = ""


def source_for(sid: str, conn=None) -> tuple[Path | None, float, str]:
    """优先正在写入的 spool；结束后用数据库里的 audio_path。

    bot 和服务是两个进程，不能用服务端内存里的 session 对象，所以按约定
    路径找 spool，再去库里读 audio_path。
    """
    sid = str(sid or "")
    if not sid:
        return None, 0.0, ""
    spool = AUDIO_DIR / (sid + ".pcm")
    try:
        if spool.is_file() and spool.stat().st_size > 0:
            return spool, spool.stat().st_size / (SAMPLE_RATE * 2.0), "pcm"
    except OSError:
        pass
    own = conn is None
    conn = conn or store.connect()
    try:
        s = store.get_session(conn, sid) or {}
        ap = Path(str(s.get("audio_path") or ""))
        if ap.is_file():
            return ap, float(s.get("duration_s") or 0.0), ap.suffix.lower().lstrip(".")
    finally:
        if own:
            conn.close()
    return None, 0.0, ""


def make_clip(sid: str, start: float | None, duration: float,
              conn=None) -> tuple[Clip | None, str]:
    """切一段 opus。返回 (clip, error)，error 是给用户看的中文原因。"""
    src, total, kind = source_for(sid, conn=conn)
    if not src:
        return None, "这一场没有可用的录音（可能刚开场、已清理，或还没落盘）。"
    if total <= 0:
        total = max(0.0, float(duration))
    # start=None 表示「最近一段」：从已录末尾往前推，而不是从 0:00 开始。
    # 卡片上的「分享最近90秒」和 /分享 CH01 90秒 都走这条。
    if start is None:
        start = max(0.0, total - float(duration))
    start = max(0.0, float(start))
    if start >= total:
        return None, "起点超过了已录时长（本场约 %s）。" % _hms(total)
    duration = max(0.2, min(MAX_SECONDS, float(duration), total - start))
    if duration <= 0.2:
        return None, "这一段还太短，等几秒再试。"

    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    key = "%s_%d_%d" % (sid, int(round(start * 10)), int(round(duration * 10)))
    dst = CLIPS_DIR / (key + ".opus")
    try:
        if dst.is_file() and dst.stat().st_size > 0 and \
                time.time() - dst.stat().st_mtime <= CACHE_TTL_S:
            return Clip(dst, start, duration, total, kind), ""
    except OSError:
        pass
    ok, err = audio.extract_opus(src, dst, start, duration)
    if not ok:
        traceback.print_exc()
        return None, "截取失败：%s" % (err or "ffmpeg 不可用")
    return Clip(dst, start, duration, total, kind), ""


def allow(sid: str, cooldown_s: float = 30.0) -> tuple[bool, float]:
    """同一场两次分享的冷却；返回 (是否允许, 还要等几秒)。"""
    sid = str(sid or "")
    now = time.monotonic()
    with _RATE_LOCK:
        last = _LAST_SHARE.get(sid, 0.0)
        if now - last < cooldown_s:
            return False, max(0.0, cooldown_s - (now - last))
        _LAST_SHARE[sid] = now
        return True, 0.0


def reset_rate_limit() -> None:
    with _RATE_LOCK:
        _LAST_SHARE.clear()


def release(sid: str) -> None:
    """发送失败时退回冷却名额，让用户能立刻重试。"""
    with _RATE_LOCK:
        _LAST_SHARE.pop(str(sid or ""), None)


def _hms(seconds: float) -> str:
    s = max(0, int(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, sec)) if h else ("%d:%02d" % (m, sec))
