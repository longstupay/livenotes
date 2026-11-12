# -*- coding: utf-8 -*-
"""时间显示：把「录音里的第几秒」换算成「几点几分」，北京与 UTC 各给一份。

为什么值得单独一个模块，而不是在推送函数里写一行 datetime.now()：

1. 库里 sessions.started_at 是 naive 本地时间（历史数据全是这个形状）。
   直接 astimezone() 是按"此刻运行这台机器的时区"解释的——用户把笔记本带去
   出差、或者把服务挪到 UTC 机房，同一场直播就会换算出两个不同的"北京时间"。
   所以新场次在开始那一刻就把 started_utc + tz_offset_min 一起落库，
   之后无论机器时区怎么变，换算都以录制当时的偏移为准。

2. 北京时间没有夏令时，固定 +08:00；唯一无歧义的是 UTC。文案里两个都给。
   两个都带日期：跨零点时只写 09:04 会被当成今天上午九点。

3. 长直播的「录音内时间」不能只有 mm:ss：2 小时 5 分会写成 125:03，
   看着像另一场的编号。超过 1 小时一律 h:mm:ss。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 北京时间 = UTC+8，无夏令时。用固定偏移而不是 ZoneInfo("Asia/Shanghai")：
# 不依赖系统 tzdata，Windows 上也不会因为缺时区库而抛异常。
CN_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
UTC_TZ = timezone.utc


def local_offset_min() -> int:
    """本机当前相对 UTC 的偏移（分钟）。东八区 = 480。"""
    off = datetime.now().astimezone().utcoffset() or timedelta(0)
    return int(off.total_seconds() // 60)


def _parse(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def start_utc(session: dict | None) -> datetime:
    """场次开始的那一刻，统一成 aware UTC。

    优先级：落库的 started_utc > started_at + 落库的时区偏移 > 按本机时区解释。
    最后一档只服务历史数据（那时候还没存时区），不追求完美，但不许抛异常。
    """
    s = session or {}
    dt = _parse(s.get("started_utc"))
    if dt is not None:
        return dt.astimezone(UTC_TZ) if dt.tzinfo else dt.replace(tzinfo=UTC_TZ)
    dt = _parse(s.get("started_at")) or datetime.now()
    if dt.tzinfo is not None:
        return dt.astimezone(UTC_TZ)
    off = s.get("tz_offset_min")
    if off is not None:
        try:
            tz = timezone(timedelta(minutes=int(off)))
            return dt.replace(tzinfo=tz).astimezone(UTC_TZ)
        except (TypeError, ValueError):
            pass
    return dt.astimezone(UTC_TZ)          # naive：按本机时区解释


def wall(session: dict | None, t: float) -> dict:
    """录音内第 t 秒 -> {cn, utc} 两个 aware datetime。"""
    try:
        t = max(0.0, float(t or 0.0))
    except (TypeError, ValueError):
        t = 0.0
    at = start_utc(session) + timedelta(seconds=t)
    return {"cn": at.astimezone(CN_TZ), "utc": at.astimezone(UTC_TZ)}


def _mdhms(dt: datetime, now: datetime | None = None) -> str:
    """11-05 17:04:38；不是今年的再加年份，避免去年今日分不清。"""
    now = now or datetime.now(CN_TZ)
    fmt = "%m-%d %H:%M:%S" if dt.year == now.year else "%Y-%m-%d %H:%M:%S"
    return dt.strftime(fmt)


def clock_line(session: dict | None, t: float) -> str:
    """一行给全：11-05 17:04:38 北京 / 09:04:38 UTC。

    北京和 UTC 同一天时日期只出现一次，省出来的宽度留给说话人和理由。
    """
    w = wall(session, t)
    cn, utc = w["cn"], w["utc"]
    if cn.date() == utc.date():
        return "%s 北京 / %s UTC" % (_mdhms(cn), utc.strftime("%H:%M:%S"))
    return "%s 北京 / %s UTC" % (_mdhms(cn), _mdhms(utc))


def clock_pair(session: dict | None, t: float) -> tuple[str, str]:
    """(北京, UTC) 两个完整时间串，给需要分列展示的地方（回顾列表、表格）。"""
    w = wall(session, t)
    return _mdhms(w["cn"]), _mdhms(w["utc"])


def hm_span(session: dict | None, t_from: float, t_to: float) -> tuple[str, str]:
    """录音内一段 -> 北京时间的起止（分钟精度）。

    同一天只写 HH:MM；跨过开播那天写「次日 HH:MM」，再远写「MM-DD HH:MM」。
    群聊里回头找「谁几点说的」经常隔天，只写 09:04 会被当成今天上午。
    """
    base = start_utc(session).astimezone(CN_TZ).date()

    def one(t: float) -> str:
        dt = wall(session, t)["cn"]
        days = (dt.date() - base).days
        if days <= 0:
            return dt.strftime("%H:%M")
        if days == 1:
            return "次日 " + dt.strftime("%H:%M")
        return dt.strftime("%m-%d %H:%M")

    return one(t_from), one(t_to)


def start_hm(session: dict | None) -> str:
    """开播那一刻的北京时间：11-09 14:09；不是今年才带年份。"""
    dt = start_utc(session).astimezone(CN_TZ)
    fmt = "%m-%d %H:%M" if dt.year == datetime.now(CN_TZ).year else "%Y-%m-%d %H:%M"
    return dt.strftime(fmt)


def offset(t: float) -> str:
    """录音内偏移：9:09 / 1:02:05。"""
    try:
        sec = max(0, int(float(t or 0.0)))
    except (TypeError, ValueError):
        sec = 0
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))
