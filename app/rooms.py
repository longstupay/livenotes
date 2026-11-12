# -*- coding: utf-8 -*-
"""监听房间：存 URL、状态、会话映射。

用户只粘贴链接，房间名由采集端从页面里读出来回填 —— 不让人抄标题。

状态机：pending -> starting -> live，任何一步失败落到 failed 并记原因。
启动阶段不再靠前端猜：采集代理在拉起浏览器 / 打开页面 / 打开采集页时上报
stage，采集页在取音频、建会话、首块入库时继续上报；失败时带 failed_stage。
stage 取值 1..6，对应前端六步（1 浏览器、2 页面、3 音频、4 会话、5 入库、6 完成）。

采集浏览器的存活不写进 rooms.json：代理每轮 tick 往 /api/capture/status
发心跳，服务只在内存里留最近一次，避免每 3 秒重写一次房间文件。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from typing import Any

from .config import DATA_DIR

PATH = DATA_DIR / "rooms.json"
_LOCK = threading.Lock()

# 采集代理心跳：room_id -> 最近一次上报（只在内存里，服务重启后自然清空）
_CAP_LOCK = threading.Lock()
_CAPTURE: dict[str, dict] = {}

# 这些参数只跟分享渠道/播放器有关，不影响房间本身；不剔掉会导致同一个房间被当成两个
_TRACKING = re.compile(
    r"^(utm_|spm|from|enter_|share_|is_|did_|timestamp|sec_uid|u_code|"
    r"web_id|previous_page|enter_from|camera_id|camera_type|match_id)", re.I)

# 直取旧版本把 reflow 里的 room id 当成 web_rid，拼出 live.douyin.com/<room id>。
# 抖音对那个地址只回「服务器开小差了」——所以它绝不能拿来当页面地址。
_REFLOW_ID = re.compile(r"/reflow/(\d{6,})")
_LIVE_PAGE = re.compile(r"^https?://live\.douyin\.com/(\d{6,})/?(?:[?#].*)?$", re.I)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    """规范化：补协议、去追踪参数、去尾斜杠、host 小写。"""
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return u
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not _TRACKING.match(k)]
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path,
                                    urllib.parse.urlencode(q), ""))


def _read() -> list[dict]:
    if not PATH.is_file():
        return []
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return data.get("rooms") or []


def _write(rooms: list[dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps({"rooms": rooms}, ensure_ascii=False, indent=2), encoding="utf-8")


def _heal_fake_live_url(room: dict) -> bool:
    """清掉旧直取拼出来的假 live 地址（room id 当 web_rid 的那个）。

    不清的话「显示页面」会一直打开一个没有房间的地址，抖音只回
    「服务器开小差了」；清掉后前端退回打开 reflow 页，采集端下一次上报
    会写回真实的 live.douyin.com/<web_rid>。
    """
    live = _LIVE_PAGE.match(str(room.get("resolved_url") or "").strip())
    reflow = _REFLOW_ID.search(str(room.get("url") or ""))
    if not (live and reflow) or live.group(1) != reflow.group(1):
        return False
    room["resolved_url"] = ""
    room["updated_at"] = now()
    return True


def _heal_rooms(rooms: list[dict]) -> bool:
    changed = False
    for r in rooms:
        changed = _heal_fake_live_url(r) or changed
    return changed


def list_rooms() -> list[dict]:
    with _LOCK:
        rooms = _read()
        if _heal_rooms(rooms):
            _write(rooms)
        return rooms


def get(room_id: str) -> dict | None:
    for r in list_rooms():
        if r.get("id") == room_id:
            return r
    return None


def get_by_session(session_id: str) -> dict | None:
    if not session_id:
        return None
    for r in list_rooms():
        if r.get("session_id") == session_id:
            return r
    return None


def add(url: str, resolved_from: str = "", name: str = "") -> tuple[dict, bool]:
    """返回 (房间, 是否新建)。同一个链接重复提交不会产生第二路。

    resolved_from 是分享/短链原文：同一个分享文案再发一次时，落地页地址
    可能被规范化成不同形式，用它兜住重复。

    name 是从分享文案里认出来的主播名：先垫上，采集端落地后会换成
    「主播名 · 房间标题」的真名（name_source=share -> page）。
    """
    norm = normalize_url(url)
    if not norm:
        raise ValueError("链接是空的")
    src = normalize_url(resolved_from) if resolved_from else ""
    hint = str(name or "").strip()[:60]
    with _LOCK:
        rooms = _read()
        healed = _heal_rooms(rooms)
        for r in rooms:
            # 同一个房间可能一次是短链/reflow、一次是真实房号；都算命中。
            if not (norm in (r.get("url"), r.get("resolved_url"))
                    or (src and src == r.get("resolved_from"))):
                continue
            # 老房间还没有名字时，顺手用这次分享文案里的主播名补上，
            # 免得重发一次链接反而又退回显示域名。
            if hint and not str(r.get("name") or "").strip():
                r.update(name=hint, name_source="share", updated_at=now())
                _write(rooms)
            elif healed:
                _write(rooms)
            return r, False
        # seq 是稳定的展示编号（CH01…），加/删别的房间不会让它变。
        # 老记录没有 seq，前端会退回按当前顺序编号。
        seq = max([int(r.get("seq") or 0) for r in rooms] + [0]) + 1
        # 同一秒内连加几路、或删了又加时，HHMMSS + len%10 会撞 id。
        # 房间 id 同时是 Chrome profile 名，撞了就是两路共用一个浏览器。
        stamp = datetime.now().strftime("%H%M%S")
        used = {str(r.get("id") or "") for r in rooms}
        rid = "rm" + stamp + str(len(rooms) % 10)
        if rid in used:
            rid = "rm" + stamp + uuid.uuid4().hex[:4]
            while rid in used:
                rid = "rm" + stamp + uuid.uuid4().hex[:4]
        room = {
            "id": rid,
            "url": norm,
            # 采集浏览器落地后的真实房号（live.douyin.com/<web_rid>）。
            # 短链/reflow 只是入口，用它去重、显示和重试都更可靠。
            "resolved_url": "",
            # 用户贴进来的分享/短链原文，重复发送时用来去重
            "resolved_from": src,
            # 展示名。空着的话前端只能退回显示域名（webcast.amemv.com），
            # 于是「哪一路是哪个直播间」就没人说得清。
            "name": hint,
            # 名字是哪来的：share（分享文案垫的）/ page（采集端从页面读到的
            # 真名）/ user（人工改的，采集端不许覆盖）。空 = 还没有名字。
            "name_source": "share" if hint else "",
            "state": "pending",
            "error": "",
            "session_id": "",
            # 最近一次采集引擎：direct / shared / per_room。空 = 还没上报。
            "capture_engine": "",
            # 直取失败后不再自动切浏览器：这里亮出「可以切」，等收听页确认后
            # switch_requested 才置位，采集代理下一轮 tick 才会真的拉起浏览器。
            "switch_ready": False,
            "switch_engine": "",
            "switch_reason": "",
            "switch_requested": False,
            "created_at": now(),
            "updated_at": now(),
            "seq": seq,
            "stage": 0,
            "failed_stage": None,
            "stage_note": "",
            "stage_at": now(),
            "saved": False,
            "has_recording": False,
            # 服务重启时还没正常结束、被收口成 stopped 的房间。前端据此
            # 显示「已中断」而不是普通的「已停止」；重试会清掉。
            "interrupted": False,
        }
        rooms.append(room)
        _write(rooms)
        return room, True


def update(room_id: str, **fields: Any) -> dict | None:
    """按字段更新房间。

    显式传 None 会把字段清成 null（例如重试时清掉 failed_stage）；不传的
    字段保持原值。调用方应只传本次真的收到的字段。
    """
    with _LOCK:
        rooms = _read()
        for r in rooms:
            if r.get("id") == room_id:
                r.update(fields)
                if set(fields) & {"state", "stage", "failed_stage", "stage_note"}:
                    r["stage_at"] = now()
                r["updated_at"] = now()
                _write(rooms)
                return r
    return None


def remove(room_id: str) -> bool:
    with _LOCK:
        rooms = _read()
        left = [r for r in rooms if r.get("id") != room_id]
        if len(left) == len(rooms):
            return False
        _write(left)
    with _CAP_LOCK:
        _CAPTURE.pop(room_id, None)
    return True


# ---------------------------------------------------------------- 采集端心跳

def capture_update(room_id: str, alive: bool, pid: int | None = None,
                   adopted: bool = False, reason: str = "") -> dict:
    """记录采集代理的一次心跳。age_s 由读取时现算，避免时钟漂移。"""
    rid = str(room_id or "")
    if not rid:
        raise ValueError("缺少房间 id")
    try:
        pid_i = int(pid) if pid else None
    except (TypeError, ValueError):
        pid_i = None
    entry = {
        "alive": bool(alive),
        "pid": pid_i,
        "adopted": bool(adopted),
        "reason": str(reason or "")[:200],
        "at": now(),
        "_mono": time.monotonic(),
    }
    with _CAP_LOCK:
        _CAPTURE[rid] = entry
        if len(_CAPTURE) > 200:
            oldest = min(_CAPTURE, key=lambda k: _CAPTURE[k]["_mono"])
            _CAPTURE.pop(oldest, None)
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def capture_status() -> dict[str, dict]:
    with _CAP_LOCK:
        out: dict[str, dict] = {}
        now_mono = time.monotonic()
        for rid, e in _CAPTURE.items():
            item = {k: v for k, v in e.items() if not k.startswith("_")}
            item["age_s"] = round(max(0.0, now_mono - e["_mono"]), 1)
            out[rid] = item
        return out
