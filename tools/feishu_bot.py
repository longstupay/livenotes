# -*- coding: utf-8 -*-
"""飞书机器人常驻进程：长连接收消息，收到就回。

    python tools/feishu_bot.py
    python tools/feishu_bot.py --say "上线了"     # 先给记住的那个人发一条，测试发送通路

命令：
    贴链接（或 /watch 链接）  -> 开听。陌生站点第一次会弹卡片让你确认
    /状态 [CH]                -> 在听哪几路、有没有断流；CH02 只看一路
    /rooms                    -> 正在听哪几路，各自什么状态
    /stop 1 或 /stop CH02      -> 停掉一路（浏览器一起关）
    /白名单                   -> 已放行的站点
    关注 FDE、OPC / 关注列表 / 取消关注 X / 话题 X
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import (clips, feishu, profile, report, share, store, timefmt,
                 watchlink)  # noqa: E402
from app.config import BASE_URL, LIVE_STALL_WARN_S, PORT  # noqa: E402

MAX_ROOMS = watchlink.MAX_ROOMS

# P2 自然语言实验：默认关闭。打开后只对“白名单群聊里 @机器人”的消息生效，
# 私聊仍然只认命令——这是防误触发的底线。
NL_ENABLED = os.environ.get("FEISHU_NL_ENABLED", "0").lower() in ("1", "true", "yes", "on")

# 采集代理和本服务在这台机器上，优先走回环：BASE_URL 是给手机用的局域网地址，
# 服务只绑 127.0.0.1 时那个地址连不上。
_BASES = [b.rstrip("/") for b in (
    os.environ.get("LIVENOTES_API"),
    "http://127.0.0.1:%d" % PORT,
    BASE_URL,
) if b]
_BASE: str | None = None

_STATE = {"pending": ("⏳", "待启动"), "starting": ("⏳", "启动中"),
          "live": ("🟢", "监听中"), "failed": ("⚠️", "失败"),
          "stopped": ("⏸", "已停止")}
_ACTIVE = ("pending", "starting", "live")


def _api(path: str, payload: dict | None = None, timeout: float = 6.0) -> dict:
    """调本机服务。上次成功的地址优先，其它兜底——换端口时不用改两处。"""
    global _BASE
    order = ([_BASE] if _BASE else []) + [b for b in _BASES if b != _BASE]
    last: Exception | None = None
    for base in order:
        try:
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                base + path, data=data,
                method="POST" if payload is not None else "GET",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                _BASE = base
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            # 409/429 这类业务错误也是有效响应，别当成“服务没起来”去试下一个地址。
            try:
                data = json.loads(e.read().decode("utf-8") or "{}")
            except Exception:
                data = None
            if isinstance(data, dict) and ("ok" in data or "error" in data):
                _BASE = base
                return data
            last = e
        except Exception as e:
            last = e
    raise RuntimeError("本机服务没响应（%s）：%s" % (path, last))


def _rooms() -> list[dict]:
    return _api("/api/rooms").get("rooms") or []


def _ordered(rooms: list[dict]) -> list[dict]:
    """在听的在前面。序号（/stop 1）按这个顺序数。"""
    return sorted(rooms, key=lambda r: (r.get("state") not in _ACTIVE,
                                        r.get("created_at") or ""))


def _label(r: dict) -> str:
    icon, cn = _STATE.get(str(r.get("state")), ("·", str(r.get("state"))))
    name = r.get("name") or "（还在读名字）"
    if r.get("state") == "starting" and r.get("stage"):
        name += " · 第%s步" % r["stage"]
    if r.get("state") == "failed":
        if r.get("failed_stage") is not None:
            name += " · 第%s步失败" % r["failed_stage"]
        if r.get("error"):
            name += " · " + str(r["error"])[:40]
    return "%s %s %s" % (icon, name, cn)


def _ch(r: dict) -> str:
    """稳定展示编号：CH01、CH02。seq 加/删房间不会变。"""
    try:
        seq = int(r.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return "CH%02d" % seq if seq > 0 else "CH—"


def _ch_list(rooms: list[dict]) -> str:
    return "、".join("%s %s" % (_ch(r), r.get("name") or r.get("id") or "")
                    for r in _ordered(rooms))


def _hms(sec) -> str:
    sec = max(0, int(float(sec or 0)))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def _span(p: dict) -> str:
    """要点时间：「14:10–14:12，录音 0:49–1:17」。

    群里回头找「谁几点说的」用的是北京时间；录音内偏移留着，方便回听时对照。
    老简报没有墙上时间就只给偏移——宁可少一行，也不编一个时间。
    """
    off = "%s–%s" % (_hms(p.get("t_from")), _hms(p.get("t_to")))
    a, b = str(p.get("from_clock") or ""), str(p.get("to_clock") or "")
    if not a or not b:
        return off
    if a == b:
        return "%s，录音 %s" % (a, off)
    if a.startswith("次日 ") and b.startswith("次日 "):
        b = b[3:]
    elif len(a) > 6 and len(b) > 6 and a[:5] == b[:5] and a[5] == " " and b[5] == " ":
        b = b[6:]                      # 「11-10 00:05–00:07」日期只写一次
    return "%s–%s，录音 %s" % (a, b, off)


def _tz_note(data: dict) -> str:
    if ((data.get("coverage") or {}).get("clock_tz")
            or (data.get("window") or {}).get("clock_tz")):
        return "时间为北京时间"
    return ""


def _note_with_tz(data: dict, tail: str) -> str:
    tz = _tz_note(data)
    return ("%s · %s" % (tz, tail)) if tz else tail


def _ago(sec) -> str:
    if sec is None:
        return "—"
    sec = max(0, int(float(sec)))
    if sec < 60:
        return "%d 秒前" % sec
    if sec < 3600:
        return "%d 分钟前" % (sec // 60)
    return "%d 小时 %d 分前" % (sec // 3600, (sec % 3600) // 60)


_CH_RE = re.compile(r"^(?:CH)?0*(\d{1,3})$", re.I)


def _resolve_room(rooms: list[dict], target: str,
                  allow_plain_seq: bool = True) -> dict | None:
    """按 CH 编号 / 房间 id / URL 找房间，不按名字猜。

    allow_plain_seq=False 时，裸数字不再当 CH —— /stop 1 要继续表示
    “当前排序的第 1 路”，跟固定编号 CH01 不是一回事。
    """
    t = (target or "").strip()
    if not t:
        return None
    m = _CH_RE.match(t)
    if m and (allow_plain_seq or t.upper().startswith("CH")):
        seq = int(m.group(1))
        for r in rooms:
            try:
                if int(r.get("seq") or 0) == seq:
                    return r
            except (TypeError, ValueError):
                continue
        return None
    for r in rooms:
        if t == str(r.get("id") or ""):
            return r
    key = t.rstrip("/")
    for r in rooms:
        for u in (r.get("url"), r.get("resolved_url")):
            u = str(u or "").rstrip("/")
            if u and (key == u or (key.startswith(("http://", "https://")) and key in u)):
                return r
    return None


def _full_text(active: list[dict]) -> str:
    lines = ["已经 %d/%d 路了，先停一路再发：" % (len(active), MAX_ROOMS)]
    for i, r in enumerate(active, 1):
        lines.append("%d. %s %s" % (i, _ch(r), _label(r)))
    lines.append("停：/stop 1 或 /stop %s" % _ch(active[0]))
    return "\n".join(lines)


def _start_room(url: str, resolved_from: str = "", label: str = "") -> str:
    """把一条已通过判定/确认的链接变成一路监听。返回给人看的结果。

    label 是从分享文案里认出来的主播名：先垫成房间名，采集端落地后再换成
    「主播名 · 房间标题」。只为让"刚加进来那 10 秒"也认得出是哪一路。
    """
    rooms = _rooms()
    for r in rooms:
        # 短链/reflow 和真实房号都算同一个房间；分享原文用来兜住重复发送。
        same = (url in (r.get("url"), r.get("resolved_url"))
                or (resolved_from and resolved_from == r.get("resolved_from")))
        if not same:
            continue
        if r.get("state") in _ACTIVE:
            _, cn = _STATE.get(str(r.get("state")), ("", ""))
            return "已经在听了（%s）：%s\n看状态：/rooms" % (
                cn, r.get("name") or r.get("url") or "")
        _api("/api/rooms/state", {"id": r["id"], "state": "pending", "error": ""})
        return "重开了这一路：%s\n上次是「%s」。" % (
            url, _STATE.get(str(r.get("state")), ("", "?"))[1])

    active = [r for r in rooms if r.get("state") in _ACTIVE]
    if len(active) >= MAX_ROOMS:
        return _full_text(active)
    resp = _api("/api/rooms", {"url": url, "resolved_from": resolved_from,
                               "name": label})
    room = resp.get("room") or {}
    if not resp.get("created"):
        return "这一路已经在了：%s" % (room.get("name") or url)
    n = len(active) + 1
    # 回复里带上主播名：链接是一长串域名时，人只能靠这行字确认"开对了没有"。
    head = "🎧 已开始（第 %d/%d 路）%s\n" % (n, MAX_ROOMS,
                                          ("· " + label) if label else "")
    return (head + "%s\n页面正在打开，约 10 秒。开好了我喊你。" % url)


# ---------------------------------------------------------------- 卡片

def _confirm_card(p: dict) -> dict:
    """陌生站点确认卡。按钮是回调，飞书后台没订阅回调时静默无反应，
    所以卡片底部的文字兜底不能省。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": "🎧 新站点，要你点一下"}},
        "elements": [
            {"tag": "div", "fields": [
                {"is_short": True,
                 "text": {"tag": "lark_md", "content": "**站点**\n%s" % p["site"]}},
                {"is_short": True,
                 "text": {"tag": "lark_md", "content": "**确认码**\n%s" % p["code"]}},
            ]},
            {"tag": "div", "text": {"tag": "lark_md", "content": p["url"][:200]}},
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "这个站不在已知平台里（抖音 / 快手 / B 站）。确认后我会用本机浏览器"
                "打开它、录下里面的声音，并记成长期白名单——以后这个站的链接直接开听。"}},
            {"tag": "hr"},
            {"tag": "action", "actions": [
                {"tag": "button", "type": "primary",
                 "text": {"tag": "plain_text", "content": "允许并开始监听"},
                 "value": {"act": "allow", "code": p["code"]}},
                {"tag": "button", "type": "default",
                 "text": {"tag": "plain_text", "content": "不用了"},
                 "value": {"act": "deny", "code": p["code"]}},
            ]},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                "按钮没反应就回我：确认 %s（%s内有效）" % (p["code"], watchlink.ttl_text())}]},
        ],
    }


def _done_card(title: str, body: str, template: str = "green") -> dict:
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]}


def handle_card(value: dict, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> tuple[str, dict | None]:
    """点卡片按钮。返回 (toast, 原地替换的卡片)。

    简报类按钮要调模型或读库，不能在卡片回调里等——先回 toast，
    真正的生成放到后台线程，结果单独发一条消息。
    """
    act = str(value.get("act") or "")
    if act in ("share_confirm", "share_cancel", "share_clip"):
        threading.Thread(target=_share_action,
                         args=(act, value, open_id, chat_id, chat_type),
                         daemon=True).start()
        return ("正在处理…", None)
    if act.startswith("brief_"):
        if act == "brief_cancel":
            _take_nonce(value.get("nonce") or "", open_id)
            return ("好，已取消", None)
        threading.Thread(
            target=_brief_action,
            args=(act, value, open_id, chat_id, chat_type), daemon=True).start()
        return ("正在处理…", None)

    code = str(value.get("code") or "")
    p = watchlink.pending_get(code)
    if not p:
        return ("这张确认过期了，把链接再发一次就行。", None)

    if act == "deny":
        watchlink.pending_take(code)
        return ("好，已取消", _done_card(
            "已取消", "%s 不会进白名单，也不会开浏览器。" % p["site"], "grey"))

    rooms = _rooms()
    active = [r for r in rooms if r.get("state") in _ACTIVE]
    if len(active) >= MAX_ROOMS:
        return ("已满 %d 路，先停一路" % MAX_ROOMS, _done_card(
            "已满 %d 路" % MAX_ROOMS, _full_text(active) + "\n\n停好之后把链接再发一次，"
            "这个站已经记住了。", "orange"))

    watchlink.pending_take(code)
    watchlink.allow(p["site"], note="飞书确认")
    reply = _start_room(p["url"])
    print("[卡片] allow %s -> %s" % (p["site"], reply.splitlines()[0]), flush=True)
    return ("已允许，开始监听", _done_card("✅ 已允许这个站点", reply))


# ---------------------------------------------------------------- 文本

def _ts(x: float) -> str:
    x = max(0, int(x or 0))
    return "%d:%02d" % (x // 60, x % 60)


def _link(sid: str, t: float) -> str:
    return "%s/reader?session=%s&t=%d" % (BASE_URL, sid, int(t))


def _split_mixed(term: str) -> list[str]:
    """把「OPC政策」拆成 ["OPC","政策"]。中英文混排时用户往往连写，
    但 ASR 出来的是"OPC 的政策"，整串 LIKE 必然落空。"""
    parts = re.findall(r"[A-Za-z0-9]+|[一-鿿]+", term or "")
    return parts if len(parts) > 1 else []


def cmd_topic(words: str, limit: int = 5) -> str:
    """按关键词捞最近一场的段落。用 LIKE 而不是 FTS：
    两字中文词（"退款"）在 trigram 下会静默返回 0 条，这是本项目踩过的坑。
    """
    terms = [w for w in re.split(r"[、,，；;\s]+", words or "") if w]
    if not terms:
        return "用法：/话题 OPC政策\n多个词用逗号或顿号隔开。"
    conn = store.connect()
    try:
        like = " OR ".join(["(u.text LIKE ? OR u.topic LIKE ?)"] * len(terms))
        args: list = []
        for t in terms:
            args += ["%" + t + "%", "%" + t + "%"]
        # 跨场次查：刚开的场次往往只有几十句，只查"最近一场"会让人以为搜索坏了。
        # 用收到的顺序倒序排，最新说的排前面。
        cols = ("SELECT u.session_id, u.t_start, u.speaker_label, u.text, s.title, "
                "s.started_at, s.started_utc, s.tz_offset_min "
                "FROM utterances u JOIN sessions s ON s.id = u.session_id ")
        rows = conn.execute(
            cols + "WHERE s.deleted_at IS NULL AND (" + like + ") "
            "ORDER BY s.started_at DESC, u.t_start ASC LIMIT ?",
            tuple(args) + (limit,)).fetchall()
        note = ""
        if not rows:
            subs = [p for t in terms for p in _split_mixed(t)] or []
            if subs:
                like2 = " OR ".join(["(u.text LIKE ? OR u.topic LIKE ?)"] * len(subs))
                args2: list = []
                for t in subs:
                    args2 += ["%" + t + "%", "%" + t + "%"]
                rows = conn.execute(
                    cols + "WHERE s.deleted_at IS NULL AND (" + like2 + ") "
                    "ORDER BY s.started_at DESC, u.t_start ASC LIMIT ?",
                    tuple(args2) + (limit,)).fetchall()
                if rows:
                    note = "（整串没匹配到，按 %s 分开找的）" % "、".join(subs)
        if not rows:
            return "没找到「%s」。换个词试试（中文两字以上、专有名词效果最好）。" % "、".join(terms)
        out = ["「%s」共 %d 段（最近说的在前）%s：" % ("、".join(terms), len(rows), note)]
        for r in rows:
            # 时间给两份（北京 / UTC）：飞书里收到的偏移量对不上任何人的表，
            # 而"几点说的"才是用户回找的锚点。
            when = timefmt.clock_line(
                {"started_at": r["started_at"], "started_utc": r["started_utc"],
                 "tz_offset_min": r["tz_offset_min"]}, r["t_start"])
            out.append("\n%s · 录音 %s · %s · 《%s》\n「%s…」\n回听 %s" % (
                when, _ts(r["t_start"]), r["speaker_label"] or "",
                (r["title"] or "")[:14], (r["text"] or "")[:46],
                _link(r["session_id"], r["t_start"])))
        return "\n".join(out)
    finally:
        conn.close()


def cmd_rooms() -> str:
    rooms = _rooms()
    if not rooms:
        return "现在一路都没在听。把直播间链接直接粘给我就开始。"
    ordered = _ordered(rooms)
    active = [r for r in rooms if r.get("state") in _ACTIVE]
    lines = ["正在听 %d/%d 路" % (len(active), MAX_ROOMS)]
    for i, r in enumerate(ordered, 1):
        lines.append("%d. %s %s" % (i, _ch(r), _label(r)))
        if r.get("url"):
            lines.append("   %s" % r["url"])
    lines.append("")
    lines.append("状态：/状态 或 /状态 CH02")
    lines.append("停一路：/stop 1（上面的序号）或 /stop CH02（固定编号）")
    lines.append("管理页：%s/rooms" % BASE_URL)
    return "\n".join(lines)


_ENGINES = {"direct": "直取", "per_room": "独立浏览器", "shared": "共享浏览器"}


def _last_line(sess: dict) -> str:
    line = str(sess.get("last_line") or "").strip()
    if line:
        return line
    utts = sess.get("utterances") or []
    if utts:
        return str((utts[-1] or {}).get("text") or "").strip()
    return ""


def _status_one(r: dict, sess: dict | None, cap: dict | None) -> str:
    state = str(r.get("state") or "")
    chunk = sess.get("last_chunk_ago_s") if sess else None
    stalled = (state == "live" and chunk is not None
               and float(chunk) >= LIVE_STALL_WARN_S)
    if state == "live" and sess and sess.get("paused"):
        icon, cn = "⏸", "已暂停"
    elif stalled:
        icon, cn = "⚠️", "断流"
    else:
        icon, cn = _STATE.get(state, ("·", state or "未知"))
    lines = ["%s %s  %s %s" % (_ch(r), r.get("name") or "（还在读名字）", icon, cn)]

    if state == "failed":
        if r.get("failed_stage") is not None:
            lines.append("  停在：第 %s 步失败" % r["failed_stage"])
        lines.append("  原因：%s" % str(r.get("error") or "采集端没给原因")[:120])
        if r.get("switch_ready"):
            eng = _ENGINES.get(str(r.get("switch_engine") or ""), str(r.get("switch_engine") or ""))
            lines.append("  直取失败，可以在收听页切换到%s采集。" % eng)
        return "\n".join(lines)

    if state in ("pending", "starting"):
        step = r.get("stage")
        if step:
            note = str(r.get("stage_note") or "").strip()
            lines.append("  启动第 %s 步%s" % (step, ("：" + note) if note else ""))
        else:
            lines.append("  等采集端拉起浏览器…")
        return "\n".join(lines)

    if state == "stopped":
        lines.append("  已停止%s" % ("（自动收尾）" if r.get("auto_stopped") else ""))
        if r.get("auto_stop_reason"):
            lines.append("  原因：%s" % str(r["auto_stop_reason"])[:100])
        lines.append("  录音：%s" % ("已保存" if r.get("has_recording") else "没有录到"))
        sid = str(r.get("session_id") or "")
        if sid:
            lines.append("  回放：%s/reader?session=%s" % (BASE_URL, sid))
        return "\n".join(lines)

    sid = str(r.get("session_id") or "")
    if sid and not sess:
        lines.append("  会话丢失：服务可能刚重启，或采集页已关闭。")
        if r.get("has_recording"):
            lines.append("  历史录音还在：%s/reader?session=%s" % (BASE_URL, sid))
        return "\n".join(lines)

    if not sess:
        lines.append("  还没有会话，等采集端建会话。")
    else:
        lines.append("  已录 %s · %s 句%s" % (
            _hms(sess.get("elapsed")), int(sess.get("n_utterances") or 0),
            " · 已暂停" if sess.get("paused") else ""))
        last = _last_line(sess)
        if last:
            ago = sess.get("last_final_ago_s")
            lines.append("  最后一句：%s（%s）" % (
                last[:50], _ago(ago) if ago is not None else "刚开听"))
        else:
            lines.append("  还没有字幕，等主播开口。")
        if chunk is None:
            lines.append("  最近一块音频：—")
        else:
            broken = state == "live" and float(chunk) >= LIVE_STALL_WARN_S
            lines.append("  最近一块音频：%s%s" % (_ago(chunk), "  ⚠️ 断流" if broken else ""))
        eng = str(r.get("capture_engine") or "")
        if eng:
            lines.append("  采集：%s" % _ENGINES.get(eng, eng))

    if cap:
        if cap.get("alive") is False:
            why = str(cap.get("reason") or "").strip()
            lines.append("  采集代理：已退出%s" % (("（%s）" % why[:80]) if why else ""))
        elif float(cap.get("age_s") or 0) > 30:
            lines.append("  采集代理：心跳超时 %s" % _ago(cap.get("age_s")))
        else:
            lines.append("  采集代理：运行中")
    elif state == "live":
        lines.append("  采集代理：没有心跳")
    return "\n".join(lines)


def cmd_status(arg: str = "") -> str:
    """0 token 的现场状态：房间 + 会话 + 采集心跳，不调模型。"""
    rooms = _rooms()
    if not rooms:
        return "现在一路都没在听。把直播间链接发给我就开听。"

    target = (arg or "").strip()
    if target:
        room = _resolve_room(rooms, target)
        if not room:
            return "没有找到「%s」。现在有：%s" % (target, _ch_list(rooms))
        picked = [room]
    else:
        picked = [r for r in _ordered(rooms) if r.get("state") in _ACTIVE]
        if not picked:
            last = _ordered(rooms)[0]
            return ("现在没有在听。最近一路：%s %s\n发 /rooms 看全部。"
                    % (_ch(last), _label(last)))

    try:
        sessions = {str(s.get("id")): s
                    for s in (_api("/api/live/list").get("sessions") or [])}
    except Exception:
        sessions = {}
    try:
        capture = _api("/api/capture/status").get("rooms") or {}
    except Exception:
        capture = {}

    blocks = []
    for r in picked:
        sid = str(r.get("session_id") or "")
        sess = sessions.get(sid)
        # 单路时再问一次详情，拿到最新台词；多路只靠列表，避免 N 次请求。
        if sid and r.get("state") in _ACTIVE and (sess is not None or len(picked) == 1):
            try:
                fresh = _api("/api/live/status?id=%s" % urllib.parse.quote(sid))
                if fresh:
                    sess = fresh
            except Exception:
                pass
        blocks.append(_status_one(r, sess, capture.get(str(r.get("id") or ""))))
    if len(blocks) == 1:
        return blocks[0]
    return "🎧 %d 路状态\n\n%s" % (len(blocks), "\n\n".join(blocks))


def _send_text_target(text: str, open_id: str, chat_id: str = "",
                      chat_type: str = "p2p") -> bool:
    if not text:
        return False
    if chat_type == "group" and chat_id:
        fn = getattr(_BOT, "send_text_to_chat", None)
        if fn:
            return fn(chat_id, text)
    return _BOT.send_text(text, open_id)


def _span_text(start: float | None, duration: float) -> str:
    if start is None:
        return "最近 %d 秒" % int(duration)
    return "从 %s 起 %d 秒" % (_ts(start), int(duration))


# ---------------------------------------------------------------- 片段分享

_SHARE_PENDING: dict[str, dict] = {}
_SHARE_LOCK = threading.Lock()


def _share_nonce(payload: dict) -> str:
    nonce = secrets.token_hex(3).upper()
    with _SHARE_LOCK:
        now = time.time()
        for k in [k for k, v in _SHARE_PENDING.items()
                  if float(v.get("expires") or 0) < now]:
            _SHARE_PENDING.pop(k, None)
        _SHARE_PENDING[nonce] = dict(payload, expires=now + 300)
    return nonce


def _take_share(nonce: str) -> dict | None:
    key = str(nonce or "").upper()
    with _SHARE_LOCK:
        p = _SHARE_PENDING.pop(key, None)
    if not p or float(p.get("expires") or 0) < time.time():
        return None
    return p


def _share_confirm_card(room: dict, nonce: str, start: float | None,
                        duration: float) -> dict:
    body = ("将把 **%s %s** 的 %s 原声发到本群。\n"
            "音频转发后不可撤回，请确认群里的人都可听。"
            % (_ch(room), _md_safe(_room_name(room)),
               _span_text(start, duration)))
    return _brief_card(
        "🎧 要分享这一段原声吗", "orange", body,
        (_btn("发送", {"act": "share_confirm", "nonce": nonce}, True),
         _btn("取消", {"act": "share_cancel", "nonce": nonce})),
        "按钮没反应就回 /确认分享 %s（5 分钟内有效）" % nonce)


def _share_confirm_text(room: dict, nonce: str, start: float | None,
                        duration: float) -> str:
    return ("要把 %s %s 的 %s 原声发到本群吗？\n"
            "音频转发后不可撤回。回复 /确认分享 %s 开始（5 分钟内有效）。"
            % (_ch(room), _room_name(room), _span_text(start, duration), nonce))


def _short_err(text: str, limit: int = 100) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _friendly_net_error(text: str) -> str:
    """飞书 SDK 抛的是 requests 的长异常，给用户看要收敛成一句。"""
    s = str(text or "").lower()
    if "99991672" in s or "131003" in s or "no permission" in s or "permission" in s:
        return "应用没有上传权限，去后台开通 im:resource"
    if "timed out" in s or "timeout" in s:
        return "连接 open.feishu.cn 超时"
    if "connection" in s or "max retries" in s:
        return "连接 open.feishu.cn 失败"
    return _short_err(text) or "飞书接口没通"


def _share_retry_card(room: dict, start: float | None, duration: float,
                      message: str) -> dict:
    value = {"act": "share_clip", "room": room.get("id") or "",
             "seconds": int(duration)}
    if start is not None:
        value["start"] = float(start)
    return _brief_card(
        "⚠️ 分享没发出去", "red",
        _md_safe(message) + "\n\n片段在本机已经生成，重试不会重新截取。",
        (_btn("重试", value, True),),
        _note_with_tz(room, "还不行就发 /分享 %s（默认最近90秒）" % _ch(room)))


def _share_worker(room: dict, sid: str, start: float | None, duration: float,
                  open_id: str, chat_id: str = "", chat_type: str = "p2p") -> None:
    retry_start = start

    def say(text: str) -> None:
        try:
            _send_text_target(text, open_id, chat_id, chat_type)
        except Exception:
            traceback.print_exc()

    def fail(text: str) -> None:
        # 失败不占 30 秒冷却，用户点「重试」要能立刻发。
        clips.release(sid)
        try:
            _send_card_target(_share_retry_card(room, retry_start, duration, text),
                              text, open_id, chat_id, chat_type)
        except Exception:
            traceback.print_exc()
            say(text)

    try:
        ok, wait = clips.allow(sid)
        if not ok:
            say("刚分享过这一场，%d 秒后再试。" % max(1, int(wait)))
            return
        clip, err = clips.make_clip(sid, start, duration)
        if not clip:
            clips.release(sid)
            say("没切成片段：%s" % err)
            return
        # 「最近 N 秒」在这里才解析成具体起点。重试卡必须钉住这个起点，
        # 否则失败后过一会儿再点「重试」，会按新的末尾重新截一段——既不是
        # 用户刚才听到的那段，也和「片段已经生成」的提示对不上。
        retry_start = clip.start
        span = "%s–%s" % (_ts(clip.start), _ts(min(clip.total,
                                                    clip.start + clip.duration)))
        ms = int(clip.duration * 1000)
        sent, err_text = False, ""
        try:
            if chat_type == "group" and chat_id:
                sent = bool(_BOT.send_audio_to_chat(chat_id, clip.path, ms))
            else:
                sent = bool(_BOT.send_audio(clip.path, ms, open_id))
        except Exception as e:
            traceback.print_exc()
            err_text = str(e)
            sent = False
        if not sent:
            try:
                if chat_type == "group" and chat_id:
                    sent = bool(_BOT.send_file_to_chat(chat_id, clip.path))
                else:
                    sent = bool(_BOT.send_file(clip.path, open_id))
            except Exception as e:
                traceback.print_exc()
                err_text = err_text or str(e)
                sent = False
        if not sent:
            err_text = err_text or getattr(_BOT, "last_error", "")
            link = (("%s/live?session=%s&t=%d" if room.get("state") in _ACTIVE
                     else "%s/reader?session=%s&t=%d")
                    % (BASE_URL, sid, int(clip.start)))
            fail("飞书接口没发出去（%s）。\n%s 的片段已经生成，也可以先在本机回听：%s"
                 % (_friendly_net_error(err_text), span, link))
            return
        # 发送成功后再发抬头，避免出现「有抬头、没音频」的半条消息。
        say("🎧 %s · %s（%d 秒）\n《%s》"
            % (_ch(room), span, int(clip.duration), _room_name(room)[:24]))
        print("[分享] %s %s %s -> %s" % (_ch(room), sid, span,
                                       "群" if chat_type == "group" else "私聊"),
              flush=True)
    except Exception as e:
        traceback.print_exc()
        clips.release(sid)
        say("分享失败：%s" % _short_err(e))


def _share_action(act: str, value: dict, open_id: str, chat_id: str = "",
                  chat_type: str = "p2p") -> None:
    if act == "share_cancel":
        _take_share(value.get("nonce") or "")
        _send_text_target("好，已取消。", open_id, chat_id, chat_type)
        return
    if act == "share_confirm":
        p = _take_share(value.get("nonce") or "")
        if not p:
            _send_text_target("确认码过期或已经用过了。发 /分享 重新来一次。",
                              open_id, chat_id, chat_type)
            return
        room = _room_by_id(p.get("room_id") or "")
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        _share_worker(room, str(p.get("sid") or ""), p.get("start"),
                      float(p.get("duration") or 90), p.get("open_id") or open_id,
                      p.get("chat_id") or chat_id, p.get("chat_type") or chat_type)
        return
    if act == "share_clip":
        room = _room_by_id(value.get("room") or "")
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        sid = str(room.get("session_id") or "")
        if not sid:
            _send_text_target("这一路还没有会话，等几秒再试。",
                              open_id, chat_id, chat_type)
            return
        _share_worker(room, sid, value.get("start"),
                      float(value.get("seconds") or 90), open_id,
                      chat_id, chat_type)


def cmd_share(rest: str, open_id: str, chat_id: str = "",
              chat_type: str = "p2p", confirmed: bool = False) -> str:
    args = clips.parse_share_args("/分享 " + str(rest or ""))
    room, candidates, err = _pick_room_choice(args["ch"])
    if not room:
        if candidates:
            lines = ["有 %d 路可用，选一路再发：" % len(candidates)]
            for r in candidates[:5]:
                lines.append("%s %s（%s）" % (_ch(r), _room_name(r),
                                             _room_state_label(r)))
            lines.append("例：/分享 %s 90秒" % _ch(candidates[0]))
            return "\n".join(lines)
        return err or "没找到这一路。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "这一路还没有会话，等几秒再试。"
    if chat_type == "group" and chat_id and not confirmed:
        payload = {"room_id": room.get("id") or "", "sid": sid,
                   "start": args["start"], "duration": args["duration"],
                   "open_id": open_id, "chat_id": chat_id, "chat_type": chat_type}
        nonce = _share_nonce(payload)
        _send_card_target(
            _share_confirm_card(room, nonce, args["start"], args["duration"]),
            _share_confirm_text(room, nonce, args["start"], args["duration"]),
            open_id, chat_id, chat_type)
        return ""
    _send_text_target("⏳ 正在截取 %s 的 %s…"
                      % (_ch(room), _span_text(args["start"], args["duration"])),
                      open_id, chat_id, chat_type)
    threading.Thread(target=_share_worker,
                     args=(room, sid, args["start"], args["duration"],
                           open_id, chat_id, chat_type), daemon=True).start()
    return ""


def cmd_share_confirm(nonce: str, open_id: str, chat_id: str = "",
                      chat_type: str = "p2p") -> str:
    p = _take_share(nonce)
    if not p:
        return "确认码过期或已经用过了。发 /分享 重新来一次。"
    room = _room_by_id(p.get("room_id") or "")
    if not room:
        return "这个房间已经不在了，发 /rooms 看看。"
    _send_text_target("⏳ 正在截取 %s 的 %s…"
                      % (_ch(room), _span_text(p.get("start"),
                                               float(p.get("duration") or 90))),
                      open_id, chat_id, chat_type)
    threading.Thread(target=_share_worker,
                     args=(room, str(p.get("sid") or ""), p.get("start"),
                           float(p.get("duration") or 90), p.get("open_id") or open_id,
                           p.get("chat_id") or chat_id, p.get("chat_type") or chat_type),
                     daemon=True).start()
    return ""


def _room_candidates_text(candidates: list[dict], example: str) -> str:
    lines = ["有 %d 路可用，选一路再发：" % len(candidates)]
    for r in candidates[:5]:
        lines.append("%s %s（%s）" % (_ch(r), _room_name(r), _room_state_label(r)))
    lines.append(example % _ch(candidates[0]))
    return "\n".join(lines)


def _package_worker(room: dict, sid: str, open_id: str, chat_id: str = "",
                    chat_type: str = "p2p") -> None:
    def say(text: str) -> None:
        _send_text_target(text, open_id, chat_id, chat_type)

    conn = store.connect()
    try:
        info = share.build_package(conn, sid)
    except ValueError as e:
        say("打包失败：%s" % e)
        return
    except Exception as e:
        traceback.print_exc()
        say("打包失败：%s" % e)
        return
    finally:
        conn.close()
    try:
        zpath = share.zip_package(info["dir"])
    except Exception as e:
        traceback.print_exc()
        say("压缩失败：%s" % e)
        return
    mb = zpath.stat().st_size / 1048576.0
    say("📦 %s 分享包：%d 个文件，%.1f MB\n音频：%s"
        % (_ch(room), len(info["files"]) + 1, mb, info.get("audio") or "（无）"))
    sent = False
    try:
        if chat_type == "group" and chat_id:
            sent = bool(_BOT.send_file_to_chat(chat_id, zpath))
        else:
            sent = bool(_BOT.send_file(zpath, open_id))
    except Exception:
        traceback.print_exc()
        sent = False
    if not sent:
        say("文件没能发到飞书（可能超过大小上限）。本机路径：%s" % zpath)


def cmd_package(rest: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> str:
    if chat_type == "group" and chat_id:
        return "分享包里是整场原声，私聊我发 /分享包，避免直接发到群里。"
    room, candidates, err = _pick_room_choice(_room_arg(rest) or str(rest or "").strip())
    if not room:
        if candidates:
            return _room_candidates_text(candidates, "例：/分享包 %s")
        return err or "没找到这一路。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "这一路还没有会话，等几秒再试。"
    _send_text_target("⏳ 正在打包 %s（摘要 + 字幕 + 音频）…" % _ch(room),
                      open_id, chat_id, chat_type)
    threading.Thread(target=_package_worker,
                     args=(room, sid, open_id, chat_id, chat_type),
                     daemon=True).start()
    return ""


def _report_worker(kind: str, since, until, open_id: str, chat_id: str = "",
                   chat_type: str = "p2p") -> None:
    def say(text: str) -> None:
        _send_text_target(text, open_id, chat_id, chat_type)

    conn = store.connect()
    try:
        info = report.build(conn, kind=kind, since=since, until=until)
    except Exception as e:
        traceback.print_exc()
        say("生成失败：%s" % e)
        return
    finally:
        conn.close()
    if not info["sessions"]:
        say("这段时间（%s）没有可汇总的场次。" % report.range_label(since, until))
        return
    st = info["stats"]
    head = ("%s\n%d 场 · %s · %d 场有总结" % (
        info["title"], st["sessions"], report.duration_text(st["duration_s"]),
        st["with_summary"]))
    created = None
    fn = getattr(_BOT, "create_docx", None)
    if callable(fn):
        try:
            created = fn(info["title"], info["markdown"], share_with=open_id or "")
        except Exception:
            traceback.print_exc()
            created = None
    access = ""
    if isinstance(created, dict):
        if created.get("shared"):
            access = "（已授权给你）"
        elif created.get("share_requested"):
            access = "（自动授权失败：%s）" % (
                created.get("share_error") or "未知原因")
    if isinstance(created, dict) and created.get("ok"):
        say("%s\n飞书云文档%s：%s\n本机备份：%s" % (
            head, access, created.get("url") or "（没拿到链接）", info.get("path") or ""))
        return
    why = (created or {}).get("error") if isinstance(created, dict) else ""
    why = why or "机器人没有云文档权限或租户未开通"
    # create_docx 在「文档建好但正文写入失败」时会带回 url：这时的文档是空的，
    # 但链接本身有效，别把已经建出来的文档丢掉，让用户自己去补正文。
    partial = (created or {}).get("url") if isinstance(created, dict) else ""
    if partial:
        say("%s\n飞书云文档已建好%s，但正文没写进去（%s）：%s\n"
            "已生成 Markdown，可导入或粘贴到飞书文档。" % (
                head, access, why, partial))
    else:
        say("%s\n没能直接建飞书云文档（%s）。\n"
            "已生成 Markdown，可导入或粘贴到飞书文档。" % (head, why))
    path = info.get("path")
    if not path:
        return
    try:
        sent = bool(_BOT.send_file(path, open_id))
    except Exception:
        traceback.print_exc()
        sent = False
    if not sent:
        say("文件没能发到飞书。本机路径：%s" % path)


def _cmd_report(kind: str, rest: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> str:
    if chat_type == "group" and chat_id:
        return "周报/复盘会汇总多场内容，私聊我生成。"
    try:
        r = report.parse_range(rest)
    except ValueError as e:
        return str(e) + "\n例：/周报 上周、/周报 7天、/周报 11-03~11-09"
    _send_text_target("⏳ 正在汇总 %s…" % r["label"], open_id, chat_id, chat_type)
    threading.Thread(target=_report_worker,
                     args=(kind, r["since"], r["until"], open_id, chat_id, chat_type),
                     daemon=True).start()
    return ""


def cmd_weekly(rest: str, open_id: str, chat_id: str = "",
               chat_type: str = "p2p") -> str:
    return _cmd_report("weekly", rest, open_id, chat_id, chat_type)


def cmd_retro(rest: str, open_id: str, chat_id: str = "",
              chat_type: str = "p2p") -> str:
    return _cmd_report("retro", rest, open_id, chat_id, chat_type)


def cmd_minutes(rest: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> str:
    raw = str(rest or "").strip()
    m = re.search(r"https?://\S+", raw)
    url = m.group(0).rstrip("，。；;、)）】\"'") if m else ""
    target = ((raw[:m.start()] + " " + raw[m.end():]).strip() if m else raw)
    room, candidates, err = _pick_room_choice(_room_arg(target) or target)
    if not room:
        if candidates:
            return _room_candidates_text(candidates, "例：/妙记 %s " + (url or "<链接>"))
        return err or "没找到这一路。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "这一路还没有会话，等几秒再试。"
    if not url:
        return "用法：/妙记 CH01 https://xxx.feishu.cn/minutes/xxxx\n先上传 /分享包 里的音频，再把妙记链接发我。"
    conn = store.connect()
    try:
        store.set_minutes_url(conn, sid, url)
    finally:
        conn.close()
    return ("✅ 已记下 %s 的妙记链接：\n%s\n"
            "之后 /回听 %s 会优先给这个链接，简报卡片也会带上。"
            % (_ch(room), url, _ch(room)))


def cmd_replay(rest: str, open_id: str = "", chat_id: str = "",
               chat_type: str = "p2p") -> str:
    room, candidates, err = _pick_room_choice(_room_arg(rest) or str(rest or "").strip())
    if not room:
        if candidates:
            return _room_candidates_text(candidates, "例：/回听 %s")
        return err or "没找到这一路。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "这一路还没有会话。"
    conn = store.connect()
    try:
        s = store.get_session(conn, sid) or {}
    finally:
        conn.close()
    minutes = str(s.get("minutes_url") or "")
    if minutes:
        return "🎧 %s 整场回听（妙记）：\n%s" % (_ch(room), minutes)
    link = ("%s/live?session=%s" if room.get("state") in _ACTIVE
            else "%s/reader?session=%s") % (BASE_URL, sid)
    return ("🎧 %s 回听：\n%s\n"
            "这是局域网地址，转发给别人打不开；外发请先在妙记上传 /分享包，"
            "再发 /妙记 %s <链接>。" % (_ch(room), link, _ch(room)))


def _send_card_target(card: dict, text: str, open_id: str, chat_id: str = "",
                      chat_type: str = "p2p") -> bool:
    ok = False
    try:
        if chat_type == "group" and chat_id:
            fn = getattr(_BOT, "send_card_to_chat", None)
            ok = bool(fn(chat_id, card)) if fn else False
        else:
            ok = bool(_BOT.send_card(card, open_id))
    except Exception:
        traceback.print_exc()
        ok = False
    if not ok:
        return _send_text_target(text, open_id, chat_id, chat_type)
    return True


def _brief_api(kind: str, room_id: str = "", **params) -> dict:
    qs = {"kind": kind}
    if room_id:
        qs["room_id"] = room_id
    qs.update({k: v for k, v in params.items() if v is not None})
    timeout = float(params.get("timeout") or 45)
    return _api("/api/live/brief?" + urllib.parse.urlencode(qs),
                timeout=max(6.0, timeout + 5))


def _brief_error(data: dict) -> str:
    err = data.get("error") or {}
    # 服务端 404/500 的兜底响应是 {"error": "文本"}，不是 BriefError 的字典；
    # 错误处理不能再抛异常，否则用户只看到“正在整理…”然后没有下文。
    if not isinstance(err, dict):
        return str(err) or "简报生成失败"
    code = str(err.get("code") or "")
    msg = str(err.get("message") or "简报生成失败")
    if code in ("ambiguous_room", "room_not_found", "no_active_room"):
        return msg + "\n" + cmd_rooms()
    if code == "no_session":
        return msg + "\n发 /状态 看现场，或等采集端重新建会话。"
    if code == "busy":
        wait = err.get("retry_after_s")
        return msg + (("\n约 %d 秒后再试。" % int(wait)) if wait else "")
    return msg


def _spawn(fn, err_to: str, *args, **kwargs) -> None:
    def _run():
        try:
            fn(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            try:
                _send_text_target("简报没生成成功，稍后再试。", err_to)
            except Exception:
                pass
    threading.Thread(target=_run, daemon=True).start()


def _room_name(room: dict) -> str:
    return str(room.get("name") or "未命名直播间")


def _pick_room_choice(target: str) -> tuple[dict | None, list[dict], str]:
    """房间选择：唯一可用的直接返回，多路返回候选，不再用链接让用户猜。"""
    rooms = _rooms()
    if not rooms:
        return None, [], "现在一路都没在听。把直播间链接发给我就开听。"
    if target:
        room = _resolve_room(rooms, target)
        if not room:
            return None, [], "没有找到「%s」。现在有：%s" % (target, _ch_list(rooms))
        return room, [], ""
    active = [r for r in _ordered(rooms) if r.get("state") in _ACTIVE]
    ready = [r for r in active if r.get("session_id")]
    if len(ready) == 1:
        return ready[0], [], ""
    if len(ready) > 1:
        return None, ready, ""
    if active:
        return None, [], "这一路正在启动，还没有会话。等几秒再发一次。"
    history = [r for r in _ordered(rooms) if r.get("session_id")]
    if len(history) == 1:
        return history[0], [], ""
    if len(history) > 1:
        return None, history, ""
    return None, [], "现在还没有可用的会话。发 /状态 看现场，或贴直播间链接。"


def _room_by_id(room_id: str) -> dict | None:
    for r in _rooms():
        if str(r.get("id") or "") == str(room_id or ""):
            return r
    return None


# ---------------------------------------------------------------- 简报格式

def _md_safe(s: str) -> str:
    return re.sub(r"[<>]", "", str(s or "")).replace("`", "'").strip()


def _topics_text(data: dict) -> str:
    w = data.get("window") or {}
    cov = data.get("coverage") or {}
    room = data.get("room") or {}
    tz = "北京时间" if w.get("clock_tz") else ""
    head = "📻 %s · 最近 %d 分钟（%s–%s%s）" % (
        room.get("ch") or "CH—", int(w.get("minutes") or 10),
        w.get("from_clock") or "—", w.get("to_clock") or "—",
        (" " + tz) if tz else "")
    lines = [head]
    extra = "%d 句 · 纳入 %d 句" % (int(cov.get("matched") or 0),
                                   int(cov.get("included") or 0))
    if cov.get("last_utterance_ago_s") is not None:
        extra += " · 最后一句 %s" % _ago(cov["last_utterance_ago_s"])
    lines.append(extra)
    if data.get("cached"):
        ts = str(data.get("cached_at") or data.get("as_of") or "")
        if len(ts) >= 19:
            lines.append("缓存于 %s · 发 /刷新 重做" % ts[11:19])
    if data.get("notice"):
        lines.append("提示：" + str(data["notice"].get("message") or ""))
    for i, t in enumerate(data.get("topics") or [], 1):
        lines.append("")
        lines.append("%d. %s（%s）" % (
            i, _md_safe(t.get("title")), _span(t)))
        lines.append("   " + _md_safe(t.get("gist")))
        ev = t.get("evidence") or []
        if ev:
            lines.append("   「%s」" % _md_safe(ev[0].get("quote")))
            lines.append("   回听 %s" % ev[0].get("reader_url"))
    if data.get("caveats"):
        lines.append("")
        lines.append("说明：" + "；".join(str(x) for x in data["caveats"][:2]))
    if data.get("minutes_url"):
        lines.append("")
        lines.append("整场回听 %s" % data["minutes_url"])
    return "\n".join(lines)


def _summary_text(data: dict) -> str:
    room = data.get("room") or {}
    cov = data.get("coverage") or {}
    as_of = str(data.get("as_of") or "")
    head = "📝 %s · 截至 %s" % (room.get("ch") or "CH—", as_of[11:19] or "—")
    if cov.get("start_clock"):
        head += " · 开播 %s（北京时间）· 已录 %s" % (
            cov["start_clock"], _hms(cov.get("t_to")))
    else:
        head += " · 开播 %s" % _hms(cov.get("t_to"))
    lines = [head]
    line = "覆盖 %d 句 / %s" % (int(cov.get("included") or 0), _hms(cov.get("t_to")))
    if cov.get("sampled_rows"):
        line += " · 抽样 %d 句" % int(cov["sampled_rows"])
    if cov.get("excluded_low_value_total"):
        line += " · 剔除 %d 句低价值" % int(cov["excluded_low_value_total"])
    lines.append(line)
    if data.get("cached"):
        ts = str(data.get("cached_at") or data.get("as_of") or "")
        if len(ts) >= 19:
            lines.append("缓存于 %s · 发 /刷新 重做" % ts[11:19])
    if data.get("notice"):
        lines.append("提示：" + str(data["notice"].get("message") or ""))
    if data.get("overview"):
        lines.append("")
        lines.append("概述：" + _md_safe(data["overview"]))
    for i, p in enumerate(data.get("points") or [], 1):
        lines.append("")
        lines.append("%d. %s（%s）" % (
            i, _md_safe(p.get("title")), _span(p)))
        lines.append("   " + _md_safe(p.get("gist")))
        ev = p.get("evidence") or []
        if ev:
            lines.append("   「%s」" % _md_safe(ev[0].get("quote")))
            lines.append("   回听 %s" % ev[0].get("reader_url"))
    if data.get("caveats"):
        lines.append("")
        lines.append("说明：" + "；".join(str(x) for x in data["caveats"][:3]))
    if data.get("minutes_url"):
        lines.append("")
        lines.append("整场回听 %s" % data["minutes_url"])
    return "\n".join(lines)


def _btn(label: str, value: dict, primary: bool = False) -> dict:
    return {"tag": "button", "type": "primary" if primary else "default",
            "text": {"tag": "plain_text", "content": label}, "value": value}


def _brief_card(title: str, template: str, body: str,
                buttons: tuple[dict, ...] = (), note: str = "") -> dict:
    elements: list[dict] = [{"tag": "div",
                             "text": {"tag": "lark_md", "content": body}}]
    if buttons:
        elements.append({"tag": "action", "actions": list(buttons)})
    if note:
        elements.append({"tag": "note",
                         "elements": [{"tag": "plain_text", "content": note}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def _topics_card(data: dict) -> dict:
    room = data.get("room") or {}
    w = data.get("window") or {}
    rid = room.get("room_id") or ""
    minutes = int(w.get("minutes") or 10)
    body = _topics_text(data).split("\n", 2)[-1]   # 去掉标题两行，卡片头已有
    return _brief_card(
        "📻 %s · 最近 %d 分钟" % (room.get("ch") or "CH—", minutes), "blue", body,
        (_btn("刷新", {"act": "brief_refresh", "kind": "topics",
                       "room": rid, "minutes": minutes}),
         _btn("分享最近90秒", {"act": "share_clip", "room": rid, "seconds": 90}),
         _btn("整场总结", {"act": "brief_summary", "room": rid,
                          "confirm": 1}, True)),
        _note_with_tz(data, "按钮没反应就发 /近聊 %s" % (room.get("ch") or "CH02")))


def _summary_card(data: dict) -> dict:
    room = data.get("room") or {}
    rid = room.get("room_id") or ""
    body = _summary_text(data).split("\n", 2)[-1]
    return _brief_card(
        "📝 %s · 阶段性总结" % (room.get("ch") or "CH—"), "green", body,
        (_btn("刷新", {"act": "brief_refresh", "kind": "summary", "room": rid}, True),
         _btn("分享最近90秒", {"act": "share_clip", "room": rid, "seconds": 90})),
        _note_with_tz(data, "按钮没反应就发 /刷新；这是阶段性总结，不是最终稿"))


def _brief_confirm_card(room: dict, nonce: str, rows: int) -> dict:
    body = ("将整理 **%s %s** 开播至今的内容（约 %d 句）。\n"
            "预计 1 次模型调用、15~30 秒；结果只用于这次问答，不会改场次名。"
            % (_ch(room), _md_safe(room.get("name") or ""), max(0, int(rows or 0))))
    return _brief_card(
        "📝 要生成阶段性总结吗", "orange", body,
        (_btn("生成", {"act": "brief_confirm", "nonce": nonce,
                       "room": room.get("id") or ""}, True),
         _btn("不用了", {"act": "brief_cancel", "nonce": nonce})),
        "按钮没反应就发 /确认简报 %s（5 分钟内有效）" % nonce)


def _brief_confirm_text(room: dict, nonce: str, rows: int) -> str:
    return ("要整理 %s %s 开播至今的内容（约 %d 句）吗？\n"
            "回复 /确认简报 %s 开始（5 分钟内有效）。"
            % (_ch(room), room.get("name") or "", max(0, int(rows or 0)), nonce))


def _status_icon(state: str) -> str:
    return _STATE.get(state, ("·", state))[0]


def _console_card(data: dict) -> dict:
    rows = data.get("rooms") or []
    elements: list[dict] = []
    for r in rows:
        bits = ["**%s %s**  %s" % (r.get("ch") or "CH—",
                                   _md_safe(r.get("name") or ""),
                                   _status_icon(r.get("room_state")))]
        if r.get("elapsed") is not None:
            bits.append("已录 %s · %s 句" % (_hms(r.get("elapsed")),
                                            int(r.get("n_utterances") or 0)))
        if r.get("last_line"):
            bits.append("最后一句 %s" % _md_safe(str(r["last_line"])[:36]))
        if r.get("capture_engine"):
            bits.append("采集：%s" % _ENGINES.get(str(r["capture_engine"]),
                                                str(r["capture_engine"])))
        if r.get("stalled"):
            bits.append("⚠️ 断流 %s" % _ago(r.get("last_chunk_ago_s")))
        elements.append({"tag": "div",
                         "text": {"tag": "lark_md", "content": "\n".join(bits)}})
        acts = [_btn("状态", {"act": "brief_status", "room": r.get("room_id")}),
                _btn("近10分钟", {"act": "brief_topics", "room": r.get("room_id"),
                                "minutes": 10}),
                _btn("总结", {"act": "brief_summary", "room": r.get("room_id"),
                             "confirm": 1}, True)]
        if r.get("session_id"):
            acts.append(_btn("分享90秒", {"act": "share_clip",
                                         "room": r.get("room_id"), "seconds": 90}))
            acts.append({"tag": "button", "type": "default",
                         "text": {"tag": "plain_text", "content": "回听"},
                         "url": "%s/reader?session=%s" % (BASE_URL, r["session_id"])})
        elements.append({"tag": "action", "actions": acts})
    if not elements:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": "现在一路都没在听。"}})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content":
                     "按钮没反应就发 /状态、/近聊 CH02、/总结 CH02"}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "turquoise",
                       "title": {"tag": "plain_text",
                                 "content": "🎛 直播助手 · %d 路" % len(rows)}},
            "elements": elements}


def _status_card(data: dict) -> dict:
    """状态卡：控制台卡片去掉“全部房间”语义，只强调当前几路在听。"""
    card = _console_card(data)
    live = sum(1 for r in data.get("rooms") or []
               if r.get("room_state") == "live")
    card["header"]["template"] = "blue"
    card["header"]["title"]["content"] = "🎧 直播助手 · %d 路在听" % live
    return card


def _console_text(data: dict) -> str:
    rows = data.get("rooms") or []
    if not rows:
        return "现在一路都没在听。"
    lines = ["🎛 直播助手 · %d 路" % len(rows)]
    for r in rows:
        line = "%s %s  %s" % (r.get("ch") or "CH—", r.get("name") or "",
                              _status_icon(r.get("room_state")))
        if r.get("elapsed") is not None:
            line += " · 已录 %s · %s 句" % (_hms(r.get("elapsed")),
                                           int(r.get("n_utterances") or 0))
        if r.get("stalled"):
            line += " · ⚠️ 断流"
        lines.append(line)
    lines.append("单路：/状态 CH02、/近聊 CH02、/总结 CH02")
    return "\n".join(lines)


def _sessions_map() -> dict[str, dict]:
    try:
        return {str(s.get("id") or ""): s
                for s in (_api("/api/live/list").get("sessions") or [])}
    except Exception:
        return {}


def _room_state_label(room: dict) -> str:
    state = str(room.get("state") or "")
    if state == "live":
        return "🟢 直播中"
    if state in ("pending", "starting"):
        return "⏳ 启动中"
    if state == "failed":
        return "⚠️ 失败"
    if state == "stopped":
        return "⏸ 已停止"
    return _status_icon(state) + " " + str(_STATE.get(state, ("", state))[1])


def _room_session_bits(room: dict, sessions: dict[str, dict]) -> list[str]:
    s = sessions.get(str(room.get("session_id") or ""))
    if not s:
        return []
    bits: list[str] = []
    if s.get("elapsed") is not None:
        bits.append("已录 %s · %s 句" % (_hms(s.get("elapsed")),
                                        int(s.get("n_utterances") or 0)))
    last = str(s.get("last_line") or "")
    if not last:
        utts = s.get("utterances") or []
        if utts:
            last = str((utts[-1] or {}).get("text") or "")
    if last:
        bits.append("最后一句 %s" % _md_safe(last[:36]))
    return bits


def _room_picker_card(kind: str, rooms: list[dict], minutes: int,
                      sessions: dict[str, dict], need_at: bool = False) -> dict:
    elements: list[dict] = []
    for i, r in enumerate(rooms, 1):
        lines = ["**%s %s**  %s" % (_ch(r), _md_safe(_room_name(r)),
                                   _room_state_label(r))]
        lines.extend(_room_session_bits(r, sessions))
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": "\n".join(lines)}})
        rid = r.get("id") or ""
        if kind == "summary":
            btn = _btn("生成这路总结", {
                "act": "brief_summary", "room": rid, "confirm": 1}, i == 1)
        else:
            btn = _btn("看这路", {
                "act": "brief_topics", "room": rid,
                "minutes": int(minutes or 10)}, i == 1)
        elements.append({"tag": "action", "actions": [btn]})
    if kind == "summary":
        title = "📝 选一路生成阶段性总结"
        note = ("预计 1 次模型调用、15~30 秒；按钮没反应就回 1 或 %s；"
                "发 /总结 %s 会再确认一次" % (_ch(rooms[0]), _ch(rooms[0])))
    else:
        title = "📻 最近 %d 分钟看哪一路" % int(minutes or 10)
        note = ("按钮没反应就回 1 或 %s；也可以发 /近聊 %s %d分钟"
                % (_ch(rooms[0]), _ch(rooms[0]), int(minutes or 10)))
    if need_at:
        note += "；群里回编号要先 @我"
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                                                  "content": note}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "green" if kind == "summary" else "blue",
                       "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def _room_choice_text(kind: str, rooms: list[dict], minutes: int,
                      sessions: dict[str, dict], need_at: bool = False) -> str:
    if kind == "summary":
        lines = ["有 %d 路可用，回复编号继续生成总结：" % len(rooms)]
        tail = "5 分钟内有效；也可以发 /总结 %s（会再确认一次）" % _ch(rooms[0])
    else:
        lines = ["有 %d 路可用，回复编号看最近 %d 分钟：" % (
            len(rooms), int(minutes or 10))]
        tail = "5 分钟内有效；也可以发 /近聊 %s %d分钟" % (
            _ch(rooms[0]), int(minutes or 10))
    for i, r in enumerate(rooms, 1):
        bits = [_room_state_label(r)]
        bits.extend(_room_session_bits(r, sessions))
        lines.append("%d. %s %s（%s）" % (
            i, _ch(r), _room_name(r), " · ".join(bits)))
    lines.append(tail + ("；群里回编号要先 @我" if need_at else ""))
    return "\n".join(lines)


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}
_PENDING_CHOICE: dict[str, dict] = {}
_LAST_CHOICE_ROOM: dict[str, str] = {}


def _clear_room_choice(open_id: str) -> None:
    with _BRIEF_LOCK:
        _PENDING_CHOICE.pop(open_id or "", None)


def _prefer_room(rooms: list[dict], open_id: str) -> list[dict]:
    """上次选过的那一路排第一：按钮更顺手，编号仍以 CHxx 为准。"""
    rid = _LAST_CHOICE_ROOM.get(open_id or "")
    if not rid:
        return list(rooms)
    for i, r in enumerate(rooms):
        if str(r.get("id") or "") == rid:
            return [r] + rooms[:i] + rooms[i + 1:]
    return list(rooms)


def _normalize_choice(text: str) -> str:
    """把「1 / 第1路 / 第一路 / CH01 / １」统一成待选表里的键。"""
    s = unicodedata.normalize("NFKC", str(text or "")).strip()
    s = re.sub(r"\s+", "", s).strip("，。！!?？")
    m = re.fullmatch(r"(?:选|第)?([一二三四五六七八九])(?:路|个|号)?", s)
    if m:
        return str(_CN_NUM[m.group(1)])
    m = re.fullmatch(r"(?:选|第)?0*(\d{1,3})(?:路|个|号)?", s)
    if m:
        return str(int(m.group(1)))
    m = re.fullmatch(r"CH0*(\d{1,3})", s, re.I)
    if m:
        return "CH%02d" % int(m.group(1))
    return s.upper()


def _looks_like_choice(text: str) -> bool:
    s = unicodedata.normalize("NFKC", str(text or "")).strip()
    return bool(re.fullmatch(
        r"(?:选|第)?(?:[一二三四五六七八九]|\d{1,3})(?:路|个|号)?|CH\s*0*\d{1,3}",
        s, re.I))


def _take_room_choice(t: str, open_id: str, chat_id: str = "",
                      chat_type: str = "p2p") -> str | None:
    """文字兜底：回复编号就接着做，不需要再发一遍完整命令。

    没在等选择时返回 None，让调用方继续走普通路由；选项不对时保留待选，
    用户可以马上再回一个编号。
    """
    key = open_id or ""
    with _BRIEF_LOCK:
        p = _PENDING_CHOICE.get(key)
        expired = bool(p and float(p.get("expires") or 0) < time.time())
        if expired:
            _PENDING_CHOICE.pop(key, None)
    if expired:
        if _looks_like_choice(t):
            return "上次的选择已过期（5 分钟）。发 /总结 或 /近聊 重新选。"
        return None
    if not p:
        return None
    rid = str((p.get("tokens") or {}).get(_normalize_choice(t)) or "")
    if not rid:
        if _looks_like_choice(t):
            return ("没有「%s」这个选项。回复 1~%d，或 CH01 这样的编号。"
                    % (str(t).strip(), len(p.get("rids") or [])))
        return None
    with _BRIEF_LOCK:
        if _PENDING_CHOICE.get(key) is not p:
            # 另一条消息/另一个线程已经消费了这次选择；别生成两遍。
            return ""
        _PENDING_CHOICE.pop(key, None)
    _LAST_CHOICE_ROOM[key] = rid
    room = _room_by_id(rid)
    if not room:
        return "这一路已经不在了。发 /状态 看现场。"
    target = _ch(room) if _ch(room) != "CH—" else rid
    if p.get("kind") == "summary":
        return cmd_summary(target, open_id, chat_id, chat_type,
                           skip_confirm=bool(p.get("skip_confirm", True)))
    return cmd_topics("%s %d分钟" % (target, int(p.get("minutes") or 10)),
                      open_id, chat_id, chat_type)


def _ask_room(kind: str, rooms: list[dict], minutes: int, open_id: str,
              chat_id: str = "", chat_type: str = "p2p") -> None:
    """多路时不猜：发一张选择卡，同时留下 5 分钟的文字编号兜底。

    点到/回复某一路就是明确选择，生成总结时不再二次确认。
    """
    rooms = _prefer_room(list(rooms), open_id)
    sessions = _sessions_map()
    tokens: dict[str, str] = {}
    for i, r in enumerate(rooms, 1):
        rid = str(r.get("id") or "")
        if not rid:
            continue
        tokens[str(i)] = rid
        tokens["第%d路" % i] = rid
        cn = next((k for k, v in _CN_NUM.items() if v == i), "")
        if cn:
            tokens["第%s路" % cn] = rid
        ch = _ch(r)
        if ch != "CH—":
            tokens[ch] = rid
    now = time.time()
    with _BRIEF_LOCK:
        for k in [k for k, v in _PENDING_CHOICE.items()
                  if float(v.get("expires") or 0) < now]:
            _PENDING_CHOICE.pop(k, None)
        _PENDING_CHOICE[open_id] = {
            "kind": kind, "minutes": int(minutes or 10), "tokens": tokens,
            "rids": [str(r.get("id") or "") for r in rooms],
            "skip_confirm": True, "at": now,
            "expires": now + 300,
        }
    text = _room_choice_text(kind, rooms, minutes, sessions,
                             need_at=chat_type == "group")
    card = _room_picker_card(kind, rooms, minutes, sessions,
                             need_at=chat_type == "group")
    _send_card_target(card, text, open_id, chat_id, chat_type)


# ---------------------------------------------------------------- 确认码 / 上一条

_BRIEF_PENDING: dict[str, dict] = {}
_BRIEF_LOCK = threading.Lock()
_LAST_BRIEF: dict[str, dict] = {}


def _new_nonce(room_id: str, open_id: str, kind: str = "summary") -> str:
    nonce = secrets.token_hex(3).upper()
    with _BRIEF_LOCK:
        now = time.time()
        for k in [k for k, v in _BRIEF_PENDING.items()
                  if now - float(v.get("at") or 0) > 300]:
            _BRIEF_PENDING.pop(k, None)
        _BRIEF_PENDING[nonce] = {"room_id": room_id, "open_id": open_id,
                                 "kind": kind, "at": now}
    return nonce


def _take_nonce(nonce: str, open_id: str) -> dict | None:
    key = str(nonce or "").upper()
    with _BRIEF_LOCK:
        p = _BRIEF_PENDING.get(key)
        if not p:
            return None
        if time.time() - float(p.get("at") or 0) > 300:
            _BRIEF_PENDING.pop(key, None)
            return None
        # 别人的确认码不能用，而且不能因为别人试了一次就把主人的码吃掉。
        if open_id and p.get("open_id") and open_id != p["open_id"]:
            return None
        _BRIEF_PENDING.pop(key, None)
    return p


# ---------------------------------------------------------------- 生成任务

def _topics_worker(room: dict, minutes: int, open_id: str, chat_id: str = "",
                   chat_type: str = "p2p", refresh: bool = False,
                   ack: bool = True) -> None:
    if ack:
        _send_text_target("⏳ 正在整理 %s 最近 %d 分钟…" % (_ch(room), minutes),
                          open_id, chat_id, chat_type)
    try:
        data = _brief_api("topics", room.get("id") or "", minutes=minutes,
                          refresh=1 if refresh else 0, timeout=60)
    except Exception as e:
        _send_text_target("简报没生成成功：%s" % e, open_id, chat_id, chat_type)
        return
    if not data.get("ok"):
        _send_text_target(_brief_error(data), open_id, chat_id, chat_type)
        return
    _attach_minutes(data, room)
    _LAST_BRIEF[open_id] = {"kind": "topics", "room_id": room.get("id") or "",
                            "minutes": minutes, "at": time.time()}
    _send_card_target(_topics_card(data), _topics_text(data),
                      open_id, chat_id, chat_type)


def _attach_minutes(data: dict, room: dict) -> None:
    """把已回填的妙记链接塞进简报数据，卡片/纯文本都能用同一套渲染。"""
    sid = str((data.get("room") or {}).get("session_id")
              or room.get("session_id") or "")
    if not sid:
        return
    try:
        conn = store.connect()
        try:
            data["minutes_url"] = (store.get_session(conn, sid) or {}).get(
                "minutes_url") or ""
        finally:
            conn.close()
    except Exception:
        traceback.print_exc()


def _finish_summary(data: dict, room: dict, open_id: str, chat_id: str,
                    chat_type: str) -> None:
    _attach_minutes(data, room)
    _LAST_BRIEF[open_id] = {"kind": "summary", "room_id": room.get("id") or "",
                            "minutes": 0, "at": time.time()}
    _send_card_target(_summary_card(data), _summary_text(data),
                      open_id, chat_id, chat_type)


def _summary_worker(room: dict, open_id: str, chat_id: str = "",
                    chat_type: str = "p2p", refresh: bool = False,
                    confirmed: bool = False, ack: bool = True,
                    skip_confirm: bool = False) -> None:
    rid = room.get("id") or ""
    if not confirmed and not refresh:
        try:
            dry = _brief_api("summary", rid, dry=1, timeout=10)
        except Exception as e:
            _send_text_target("简报没生成成功：%s" % e, open_id, chat_id, chat_type)
            return
        if not dry.get("ok"):
            _send_text_target(_brief_error(dry), open_id, chat_id, chat_type)
            return
        if dry.get("cached"):
            _finish_summary(dry, room, open_id, chat_id, chat_type)
            return
        if dry.get("would_use_llm") and not skip_confirm:
            nonce = _new_nonce(rid, open_id)
            _send_card_target(_brief_confirm_card(room, nonce, int(dry.get("rows") or 0)),
                              _brief_confirm_text(room, nonce, int(dry.get("rows") or 0)),
                              open_id, chat_id, chat_type)
            return
    if ack:
        _send_text_target("⏳ 正在生成 %s 的阶段性总结…" % _ch(room),
                          open_id, chat_id, chat_type)
    try:
        data = _brief_api("summary", rid, refresh=1 if (refresh or confirmed) else 0,
                          timeout=90)
    except Exception as e:
        _send_text_target("总结没生成成功：%s" % e, open_id, chat_id, chat_type)
        return
    if not data.get("ok"):
        _send_text_target(_brief_error(data), open_id, chat_id, chat_type)
        return
    _finish_summary(data, room, open_id, chat_id, chat_type)


# ---------------------------------------------------------------- 命令

def parse_topics_args(rest: str) -> tuple[str | None, int]:
    target, minutes = None, 10
    for tok in (rest or "").split():
        m = re.fullmatch(r"(\d{1,3})(?:分钟|m)", tok, re.I)
        if m:
            minutes = max(1, min(120, int(m.group(1))))
        elif re.fullmatch(r"(?:CH)?\d+", tok, re.I):
            target = tok
        else:
            raise ValueError("用法：/近聊 [CH] [30分钟]")
    return target, minutes


def cmd_topics(rest: str, open_id: str, chat_id: str = "",
               chat_type: str = "p2p", refresh: bool = False) -> str:
    try:
        target, minutes = parse_topics_args(rest)
    except ValueError as e:
        return str(e)
    _clear_room_choice(open_id)
    room, candidates, err = _pick_room_choice(target)
    if err:
        return err
    if candidates:
        _ask_room("topics", candidates, minutes, open_id, chat_id, chat_type)
        return ""
    if not room.get("session_id"):
        return "这一路还没有会话。发 /状态 看现场。"
    _LAST_CHOICE_ROOM[open_id or ""] = str(room.get("id") or "")
    _spawn(_topics_worker, open_id, room, minutes, open_id, chat_id, chat_type, refresh, True)
    return ""


def cmd_summary(rest: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p", refresh: bool = False,
                skip_confirm: bool = False) -> str:
    target = (rest or "").strip() or None
    _clear_room_choice(open_id)
    room, candidates, err = _pick_room_choice(target)
    if err:
        return err
    if candidates:
        _ask_room("summary", candidates, 0, open_id, chat_id, chat_type)
        return ""
    if not room.get("session_id"):
        return "这一路还没有会话。发 /状态 看现场。"
    _LAST_CHOICE_ROOM[open_id or ""] = str(room.get("id") or "")
    _spawn(_summary_worker, open_id, room, open_id, chat_id, chat_type,
           refresh, False, True, skip_confirm=skip_confirm)
    return ""


def cmd_confirm(nonce: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> str:
    p = _take_nonce(nonce, open_id)
    if not p:
        return "确认码过期或已经用过了。发 /总结 重新来一次。"
    room = _room_by_id(p.get("room_id") or "")
    if not room:
        return "这个房间已经不在了，发 /rooms 看看现在有哪些。"
    _spawn(_summary_worker, open_id, room, open_id, chat_id, chat_type, True, True, True)
    return ""


def cmd_refresh(open_id: str, chat_id: str = "", chat_type: str = "p2p") -> str:
    last = _LAST_BRIEF.get(open_id)
    if not last or time.time() - float(last.get("at") or 0) > 1800:
        return "还没有可刷新的简报。先发 /近聊 或 /总结。"
    if last.get("kind") == "status":
        return cmd_status(last.get("target") or "")
    room = _room_by_id(last.get("room_id") or "")
    if not room:
        return "上一条的房间已经不在了，发 /rooms 看看现在有哪些。"
    if last.get("kind") == "topics":
        _spawn(_topics_worker, open_id, room, int(last.get("minutes") or 10),
               open_id, chat_id, chat_type, True, True)
    else:
        _spawn(_summary_worker, open_id, room, open_id, chat_id, chat_type, True, True, True)
    return ""


def cmd_console(open_id: str, chat_id: str = "", chat_type: str = "p2p") -> str:
    try:
        data = _brief_api("status", timeout=10)
    except Exception as e:
        return "状态没取到：%s" % e
    if not data.get("ok"):
        return _brief_error(data)
    if not (data.get("rooms") or []):
        return "现在一路都没在听。"
    _send_card_target(_status_card(data), _console_text(data),
                      open_id, chat_id, chat_type)
    return ""


def _sub_key(open_id: str, chat_id: str = "", chat_type: str = "p2p") -> str:
    dest = chat_id if (chat_type == "group" and chat_id) else ""
    return "%s|%s" % (open_id, dest)


def cmd_subscribe(arg: str, open_id: str, chat_id: str = "",
                  chat_type: str = "p2p") -> str:
    m = re.search(r"\d+", arg or "")
    minutes = max(5, min(60, int(m.group()))) if m else 10
    dest = chat_id if (chat_type == "group" and chat_id) else ""
    _SUBS[_sub_key(open_id, chat_id, chat_type)] = {
        "minutes": minutes, "next": time.monotonic() + minutes * 60,
        "open_id": open_id, "chat_id": dest,
        "chat_type": "group" if dest else "p2p"}
    return ("已订阅：每 %d 分钟把在听房间的近聊摘要发到%s。\n退出：/退订"
            % (minutes, "这个群" if dest else "私聊"))


def cmd_unsubscribe(open_id: str) -> str:
    keys = [k for k in _SUBS if k.split("|", 1)[0] == open_id]
    for k in keys:
        _SUBS.pop(k, None)
    return "已退订。" if keys else "本来就没有订阅。"


_SUBS: dict[str, dict] = {}


def _subscription_loop() -> None:
    while True:
        time.sleep(15)
        now = time.monotonic()
        due = [sub for sub in list(_SUBS.values())
               if now >= float(sub.get("next") or 0)]
        for sub in due:
            sub["next"] = now + int(sub.get("minutes") or 10) * 60
            try:
                rooms = [r for r in _ordered(_rooms())
                         if r.get("state") in _ACTIVE and r.get("session_id")]
            except Exception:
                continue
            for room in rooms[:MAX_ROOMS]:
                try:
                    _topics_worker(room, int(sub.get("minutes") or 10),
                                   str(sub.get("open_id") or ""),
                                   str(sub.get("chat_id") or ""),
                                   str(sub.get("chat_type") or "p2p"),
                                   False, False)
                except Exception:
                    traceback.print_exc()


def handle_menu(key: str, open_id: str) -> None:
    key = str(key or "").lower()
    if key in ("status", "状态"):
        reply = cmd_status()
    elif key in ("rooms", "房间"):
        reply = cmd_rooms()
    elif key in ("topics", "近聊", "最近10分钟"):
        reply = cmd_topics("", open_id)
    elif key in ("summary", "总结"):
        reply = cmd_summary("", open_id, skip_confirm=True)
    elif key in ("console", "控制台"):
        reply = cmd_console(open_id)
    else:
        reply = _help_text()
    if reply:
        _send_text_target(reply, open_id)


def _brief_action(act: str, value: dict, open_id: str, chat_id: str = "",
                  chat_type: str = "p2p") -> None:
    room = _room_by_id(value.get("room") or "")
    if act == "brief_status":
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        _LAST_BRIEF[open_id] = {"kind": "status",
                                "target": room.get("id") or "",
                                "room_id": room.get("id") or "",
                                "minutes": 0, "at": time.time()}
        _send_text_target(cmd_status(room.get("id") or ""),
                          open_id, chat_id, chat_type)
        return
    if act == "brief_topics":
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        _clear_room_choice(open_id)
        _LAST_CHOICE_ROOM[open_id] = str(room.get("id") or "")
        try:
            minutes = int(value.get("minutes") or 10)
        except (TypeError, ValueError):
            minutes = 10
        _topics_worker(room, minutes, open_id, chat_id, chat_type)
        return
    if act == "brief_summary":
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        _clear_room_choice(open_id)
        _LAST_CHOICE_ROOM[open_id] = str(room.get("id") or "")
        # 选择卡/控制台按钮本身就是明确选择：不再弹二次确认。
        _summary_worker(room, open_id, chat_id, chat_type,
                        skip_confirm=bool(value.get("confirm")))
        return
    if act == "brief_refresh":
        last = _LAST_BRIEF.get(open_id)
        if not room or not last:
            _send_text_target("还没有可刷新的简报。先发 /近聊 或 /总结。",
                              open_id, chat_id, chat_type)
            return
        kind = value.get("kind") or last.get("kind")
        if kind == "topics":
            _topics_worker(room, int(value.get("minutes") or last.get("minutes") or 10),
                           open_id, chat_id, chat_type, True, True)
        else:
            _summary_worker(room, open_id, chat_id, chat_type,
                            refresh=True, confirmed=True)
        return
    if act == "brief_confirm":
        p = _take_nonce(value.get("nonce") or "", open_id)
        if not p:
            _send_text_target("确认码过期或已经用过了。发 /总结 重新来一次。",
                              open_id, chat_id, chat_type)
            return
        room = _room_by_id(p.get("room_id") or "")
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        _summary_worker(room, open_id, chat_id, chat_type,
                        refresh=True, confirmed=True)


def cmd_stop(arg: str) -> str:
    rooms = _rooms()
    if not rooms:
        return "现在没有在听的房间。"
    ordered = _ordered(rooms)
    arg = (arg or "").strip()
    if not arg:
        return "停哪一路？\n" + cmd_rooms()
    room = None
    if arg.isdigit():
        # 裸数字保留旧语义：按当前排序数第几路
        i = int(arg)
        if 1 <= i <= len(ordered):
            room = ordered[i - 1]
    else:
        # CH02 / 房间 id / URL
        room = _resolve_room(rooms, arg, allow_plain_seq=False)
    if not room:
        return "没找到「%s」。\n%s" % (arg, cmd_rooms())
    _api("/api/rooms/close", {"id": room["id"]})
    return "已停：%s %s\n对应的浏览器也关了。" % (_ch(room), _label(room))


def cmd_trust() -> str:
    added = watchlink.trust_list()
    lines = ["已知平台（默认放行）：" + "、".join(watchlink.PRESET_SITES)]
    if added:
        lines.append("你确认过的：")
        for site, meta in added.items():
            lines.append("· %s（%s）" % (site, (meta or {}).get("added_at", "")[:10]))
    else:
        lines.append("还没确认过任何陌生站点。")
    pend = watchlink.pending_count()
    if pend:
        lines.append("有 %d 个待确认（%s内有效）。" % (pend, watchlink.ttl_text()))
    return "\n".join(lines)


_YES = re.compile(r"^(确认|同意|允许|可以|ok|okay|yes|y)\s*([A-Za-z0-9]{4})?$", re.I)
_NO = re.compile(r"^(取消|不用了|算了|no|n)\s*([A-Za-z0-9]{4})?$", re.I)


def _answer_pending(text: str) -> str | None:
    """「确认 / 确认 K7Q2 / 取消」——卡片按钮失效时的兜底通道。"""
    m = _YES.match(text) or _NO.match(text)
    if not m:
        return None
    want_ok = bool(_YES.match(text))
    code = (m.group(2) or "").upper()
    p = watchlink.pending_get(code) if code else watchlink.pending_unique()
    if not p:
        n = watchlink.pending_count()
        if n > 1:
            codes = watchlink.pending_codes()
            return "有 %d 个待确认，回的时候带上确认码，比如：确认 %s" % (n, codes[0])
        return "现在没有等你确认的东西。直接把直播间链接粘给我就行。"
    if not want_ok:
        watchlink.pending_take(p["code"])
        return "好，已取消。%s 不会进白名单。" % p["site"]
    watchlink.pending_take(p["code"])
    watchlink.allow(p["site"], note="飞书确认")
    return "✅ 已允许 %s，以后这个站的链接直接开听。\n%s" % (
        p["site"], _start_room(p["url"], label=str(p.get("label") or "")))


def cmd_watch(text: str, open_id: str = "", chat_id: str = "",
              chat_type: str = "p2p") -> str:
    _clear_room_choice(open_id)
    try:
        return _cmd_watch(text, open_id, chat_id, chat_type)
    except RuntimeError as e:
        return ("本机服务没起来，接不上：%s\n先 python run.py，起来之后把链接再发一次。" % e)


def _cmd_watch(text: str, open_id: str = "", chat_id: str = "",
               chat_type: str = "p2p") -> str:
    d = watchlink.decide(text, resolver=watchlink.resolve)
    note = (d.get("note") + "\n") if d.get("note") else ""
    if d["kind"] == "reject":
        return note + d["reason"]
    if d["kind"] == "confirm":
        p = watchlink.pending_new(d["url"], d["site"], open_id,
                                  label=d.get("label") or "")
        fallback = ("%s 这个站我还不认识，要你点一下确认。\n"
                    "回复「确认 %s」就开（%s内有效）。"
                    % (note + d["url"], p["code"], watchlink.ttl_text()))
        # 卡片发不出去（版本/权限）时退回文字。群聊里也回到群里，
        # 否则用户那边一片安静；私聊保持原来的“返回文字由调用方发”。
        if chat_type == "group" and chat_id:
            _send_card_target(_confirm_card(p), fallback, open_id, chat_id, chat_type)
            return ""
        if _BOT.send_card(_confirm_card(p), open_id):
            return ""
        return fallback
    return note + _start_room(d["url"], d.get("resolved_from") or "",
                              label=d.get("label") or "")


_BOT: feishu.Bot  # 在 main() 里赋值，handle_text 用它发消息


def handle_text(text: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> None:
    print("[收到] %s | %s" % ((open_id or "")[:12], text.replace("\n", " ")[:120]), flush=True)
    t = (text or "").strip()
    low = t.lower()

    try:
        reply = _route(t, low, open_id, chat_id, chat_type)
    except Exception as e:
        traceback.print_exc()
        reply = "出错了：%s" % e
    if not reply:
        return
    ok = _send_text_target(reply, open_id, chat_id, chat_type)
    print("[回复] %s  %s" % ("成功" if ok else "失败", reply.splitlines()[0]), flush=True)


_HINT_DAY: dict[str, str] = {}


def _help_text() -> str:
    return ("贴一个直播间链接给我就开始听（抖音/快手/B 站都行）。\n"
            "第一次遇到陌生网站我会弹卡片让你确认，点一次就记住。\n\n"
            "/状态 [CH] 在听哪几路、有没有断流\n"
            "/近聊 [CH] [10分钟] 最近在聊什么（默认 10 分钟）\n"
            "/总结 [CH] 开播至今的阶段性总结（先确认再生成）\n"
            "/分享 [CH] [90秒] 把最近一段原声发到飞书（群聊需确认）\n"
            "/分享 CH01 1:30 2分钟 从第 1:30 起截 2 分钟\n"
            "/周报 [上周|7天|11-03~11-09] 汇总多场生成周报（私聊）\n"
            "/复盘 [范围] 生成可填结论/行动的复盘模板（私聊）\n"
            "/刷新 重做上一条简报（绕过缓存）\n"
            "/控制台 一张卡片看所有房间和按钮\n"
            "/订阅 [分钟] /退订 定时把近聊摘要发给你\n"
            "/rooms 房间列表（带 CH 编号）\n"
            "/stop 1 或 /stop CH02 停掉一路\n"
            "/白名单 看过哪些站点被放行\n"
            "关注 FDE、OPC 给关键词加权\n"
            "/话题 OPC政策 搜历史场次里的原话\n\n"
            "群聊里需要 @我，而且只认白名单群；私聊只有主人能用。")


def _daily_hint(open_id: str) -> str:
    """未识别的文本默认不回。每人每天最多提示一次帮助，避免聊天被刷屏。"""
    key = open_id or "-"
    today = date.today().isoformat()
    if _HINT_DAY.get(key) == today:
        return ""
    _HINT_DAY[key] = today
    return "没看懂这条，我只认命令和直播间链接。\n" + _help_text()


def _room_arg(text: str) -> str:
    m = re.search(r"(?i)CH\s*0*(\d{1,3})", text or "")
    return ("CH%02d" % int(m.group(1))) if m else ""


def _nl_route(t: str, open_id: str, chat_id: str, chat_type: str) -> str:
    """P2 自然语言实验：只在白名单群、且 @ 了机器人时到这里。

    仍然只认几个明确意图；认不出来就保持沉默，不在群里接闲聊。
    """
    if not (NL_ENABLED and chat_type == "group" and chat_id):
        return ""
    room = _room_arg(t)
    if any(w in t for w in ("总结", "复盘", "概括", "梳理", "讲了什么", "说了什么")):
        return cmd_summary(room, open_id, chat_id, chat_type)
    if any(w in t for w in ("近聊", "话题", "聊什么", "在聊", "最近")):
        m = re.search(r"(\d{1,3})\s*分钟", t)
        rest = room
        if m:
            rest = ("%s %s分钟" % (room, m.group(1))).strip()
        return cmd_topics(rest, open_id, chat_id, chat_type)
    if any(w in t for w in ("状态", "断流", "还在听", "正常吗", "怎么样")):
        return cmd_status(room)
    return ""


def _route(t: str, low: str, open_id: str = "", chat_id: str = "",
           chat_type: str = "p2p") -> str:
    # 顺序要紧：先认「确认/取消」，再认链接，最后才是关键词命令——
    # 分享文案里常带"关注"两个字，先判关注会把链接吃掉。
    got = _answer_pending(t)
    if got is not None:
        return got

    for verb in ("/watch", "watch", "/听"):
        if low.startswith(verb):
            rest = t[len(verb):].strip(" ：:")
            if not rest:
                return ("用法：把直播间链接直接发给我，或者 /watch <链接>。\n"
                        "抖音、快手、B 站的直播间都行。")
            return cmd_watch(rest, open_id, chat_id, chat_type)

    if watchlink.extract(t):
        return cmd_watch(t, open_id, chat_id, chat_type)

    parts = t.split(None, 1)
    head = parts[0].lower() if parts else ""

    for verb in ("/近聊", "/最新", "/topics"):
        if low.startswith(verb):
            return cmd_topics(t[len(verb):].strip(" ：:"), open_id, chat_id, chat_type)
    for verb in ("/确认分享", "/share_confirm"):
        if low.startswith(verb):
            return cmd_share_confirm(t[len(verb):].strip(" ：:"), open_id,
                                     chat_id, chat_type)
    for verb in ("/分享包", "/package"):
        if low.startswith(verb):
            return cmd_package(t[len(verb):].strip(" ：:"), open_id,
                               chat_id, chat_type)
    for verb in ("/妙记", "/minutes"):
        if low.startswith(verb):
            return cmd_minutes(t[len(verb):].strip(" ：:"), open_id,
                               chat_id, chat_type)
    for verb in ("/周报", "/weekly"):
        if low.startswith(verb):
            return cmd_weekly(t[len(verb):].strip(" ：:"), open_id,
                              chat_id, chat_type)
    for verb in ("/复盘", "/retro"):
        if low.startswith(verb):
            return cmd_retro(t[len(verb):].strip(" ：:"), open_id,
                             chat_id, chat_type)
    for verb in ("/回听", "/replay"):
        if low.startswith(verb):
            return cmd_replay(t[len(verb):].strip(" ：:"), open_id,
                              chat_id, chat_type)
    for verb in ("/分享", "/share"):
        if low.startswith(verb):
            return cmd_share(t[len(verb):].strip(" ：:"), open_id,
                             chat_id, chat_type)
    for verb in ("/总结", "/summary"):
        if low.startswith(verb):
            return cmd_summary(t[len(verb):].strip(" ：:"), open_id, chat_id, chat_type)
    for verb in ("/刷新", "/refresh"):
        if low.startswith(verb):
            return cmd_refresh(open_id, chat_id, chat_type)
    for verb in ("/确认简报", "/确认总结"):
        if low.startswith(verb):
            return cmd_confirm(t[len(verb):].strip(" ：:"), open_id, chat_id, chat_type)
    for verb in ("/控制台", "/console", "/菜单"):
        if low.startswith(verb):
            return cmd_console(open_id, chat_id, chat_type)
    # 订阅会持续发消息，不能因为聊天里出现“订阅”两个字就触发。
    if (t.startswith("/订阅") or t == "订阅"
            or re.match(r"^订阅\s*\d", t)):
        return cmd_subscribe(t.split("订阅", 1)[-1].strip(" ：:"), open_id,
                             chat_id, chat_type)
    if t.startswith("/退订") or t == "退订":
        return cmd_unsubscribe(open_id)

    for verb in ("/状态", "/status"):
        if low.startswith(verb):
            target = t[len(verb):].strip(" ：:")
            if open_id:
                _LAST_BRIEF[open_id] = {"kind": "status", "target": target,
                                        "room_id": "", "minutes": 0,
                                        "at": time.time()}
            return cmd_status(target)

    if t.startswith(("/rooms", "/房间", "/列表", "房间列表")):
        return cmd_rooms()
    for verb in ("/stop", "/停", "停掉"):
        if t.startswith(verb):
            return cmd_stop(t[len(verb):].strip(" ：:"))
    if t.startswith(("/白名单", "/信任", "白名单")):
        return cmd_trust()

    if t.startswith(("/关注列表", "关注列表")):
        ws = profile.keywords()
        return ("当前关注词：" + "、".join(ws)) if ws else "还没有关注词。发送：关注 FDE、OPC"
    if t.startswith(("/取消关注", "取消关注")):
        rest = t.split("关注", 1)[-1].strip(" ：:")
        ws = profile.remove(rest)
        return "已移除。" + (("现在：" + "、".join(ws)) if ws else "现在没有关注词了。")
    if t.startswith(("/关注", "关注")):
        rest = t.split("关注", 1)[-1].strip(" ：:")
        if not rest:
            return "用法：关注 FDE、OPC"
        ws = profile.add(rest)
        return "已关注：" + "、".join(ws) + "\n命中这些词的句子会加权，更容易被推。"
    if t.startswith(("/话题", "话题")):
        return cmd_topic(t.split("话题", 1)[-1].strip(" ：:"))

    if head in ("/hi", "/help", "/帮助") or t in ("你好", "在吗", "哈喽", "帮助"):
        return _help_text()
    # 选择卡的文字兜底：只在确实有 pending 时生效，不影响普通聊天。
    choice = _take_room_choice(t, open_id, chat_id, chat_type)
    if choice is not None:
        return choice
    if chat_type == "group" and chat_id:
        return _nl_route(t, open_id, chat_id, chat_type)
    return _daily_hint(open_id)


# ---------------------------------------------------------------- 状态回执

def _notify(text: str) -> None:
    try:
        ok = _BOT.send_text(text)
        print("[通知] %s  %s" % ("成功" if ok else "失败", text.splitlines()[0]), flush=True)
    except Exception:
        traceback.print_exc()


def _watch_rooms() -> None:
    """开好了 / 没打开，都要主动说一声。

    房间里从 starting 变 live 靠的是采集代理回报，用户看不到中间过程；
    没有这条回执，他只能猜"到底听上了没有"。
    """
    seen: dict[str, str] = {}
    primed = False
    while True:
        try:
            rooms = _rooms()
        except Exception:
            time.sleep(10)
            continue
        for r in rooms:
            rid, st = str(r.get("id")), str(r.get("state"))
            prev = seen.get(rid)
            seen[rid] = st
            if not primed or prev == st:
                continue
            if st == "live" and prev != "live":
                _notify("✅ 开好了：%s\n%s\n看实时：%s/live"
                        % (r.get("name") or "这个房间", r.get("url") or "", BASE_URL))
            elif st == "failed" and prev != "failed":
                _notify("⚠️ 没打开：%s\n原因：%s\n可能已下播，或链接不是直播间。"
                        "把链接再发一次我就重试。"
                        % (r.get("url") or "", r.get("error") or "采集端没给原因"))
            elif (st == "stopped" and prev in ("live", "starting")
                  and r.get("auto_stopped")):
                # 自动收尾（断流看门狗 / 采集代理判定已下播）：人已经睡了，
                # 得说一声，不然早上只看到三个「已停止」，不知道是录完了还是断了一夜。
                _notify("⚠️ 已自动收尾：%s\n原因：%s\n录音和逐字稿已经保存，可以回放：%s"
                        % (r.get("name") or "这个房间",
                           r.get("auto_stop_reason") or r.get("error") or "没收到音频块",
                           BASE_URL))
        primed = True
        time.sleep(4)


def main(argv: list[str]) -> int:
    global _BOT
    try:
        _BOT = feishu.Bot()
    except Exception as e:
        print("启动失败：%s" % e)
        return 2
    print("[feishu] app_id=%s… open_id=%s" % (
        _BOT.app_id[:10], ("已记录" if _BOT.open_id else "还没有（先在飞书里发一句话）")),
        flush=True)

    for i, a in enumerate(argv):
        if a == "--say" and i + 1 < len(argv):
            print("[测试] 发送：%s" % argv[i + 1])
            print("[测试] %s" % ("成功" if _BOT.send_text(argv[i + 1]) else "失败"))
            return 0

    threading.Thread(target=_watch_rooms, daemon=True).start()
    threading.Thread(target=_subscription_loop, daemon=True).start()
    try:
        feishu.run(_BOT, handle_text, handle_card, handle_menu)
    except KeyboardInterrupt:
        print("\n[exit] 已停止")
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
