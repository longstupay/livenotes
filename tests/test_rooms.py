# -*- coding: utf-8 -*-
"""房间状态机：启动阶段、失败阶段、稳定编号、采集端心跳。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from app import live, rooms, server
from tests import helpers


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(rooms, "PATH", tmp_path / "rooms.json")
    with rooms._CAP_LOCK:
        rooms._CAPTURE.clear()
    yield
    with rooms._CAP_LOCK:
        rooms._CAPTURE.clear()


def test_add_has_stage_fields_and_stable_seq():
    a, created = rooms.add("https://live.douyin.com/123?utm_source=x")
    assert created and a["state"] == "pending"
    assert a["stage"] == 0 and a["failed_stage"] is None
    assert a["saved"] is False and a["has_recording"] is False
    assert a["seq"] == 1
    assert a["url"] == "https://live.douyin.com/123"

    dup, created = rooms.add("https://live.douyin.com/123")
    assert not created and dup["id"] == a["id"]

    b, _ = rooms.add("https://live.douyin.com/456")
    assert b["seq"] == 2
    assert rooms.remove(a["id"])
    c, _ = rooms.add("https://live.douyin.com/789")
    # 删掉中间一路后，新房间继续用新编号，不复用 1
    assert c["seq"] == 3


def test_add_dedupes_short_link_and_resolved_forms():
    short = "https://v.douyin.com/HHbdNjaE0jU"
    reflow = "https://webcast.amemv.com/douyin/webcast/reflow/7700000000000000002"
    a, created = rooms.add(reflow, short)
    assert created
    assert a["resolved_from"] == short
    assert a["resolved_url"] == ""

    # 同一个分享文案再发一次：落地页地址相同 -> 命中
    b, created2 = rooms.add(reflow, short)
    assert not created2 and b["id"] == a["id"]

    # 换成了真实房号：靠分享原文也能命中（resolved_from 相同）
    c, created3 = rooms.add("https://live.douyin.com/123456789012", short)
    assert not created3 and c["id"] == a["id"]


def test_update_only_touches_given_fields_and_can_clear_null():
    room, _ = rooms.add("https://live.douyin.com/1")
    rooms.update(room["id"], state="starting", stage=2, stage_note="页面已打开")
    r = rooms.get(room["id"])
    assert (r["state"], r["stage"], r["stage_note"]) == ("starting", 2, "页面已打开")

    rooms.update(room["id"], state="failed", failed_stage=3, error="没有拿到音频")
    r = rooms.get(room["id"])
    assert (r["state"], r["failed_stage"], r["error"]) == ("failed", 3, "没有拿到音频")

    # 重试：显式 None 必须把上一次的 failed_stage 清掉
    rooms.update(room["id"], state="pending", failed_stage=None,
                 stage=0, error="", session_id="")
    r = rooms.get(room["id"])
    assert r["failed_stage"] is None and r["stage"] == 0 and r["error"] == ""
    assert r["name"] == ""          # 没传的字段保持原值


def test_capture_heartbeat_and_remove_cleanup():
    room, _ = rooms.add("https://live.douyin.com/2")
    rooms.capture_update(room["id"], True, pid=4321)
    st = rooms.capture_status()[room["id"]]
    assert st["alive"] is True and st["pid"] == 4321 and st["age_s"] >= 0

    rooms.capture_update(room["id"], False, reason="browser closed")
    st = rooms.capture_status()[room["id"]]
    assert st["alive"] is False and st["reason"] == "browser closed"

    assert rooms.remove(room["id"])
    assert room["id"] not in rooms.capture_status()


def test_get_by_session():
    room, _ = rooms.add("https://live.douyin.com/3")
    rooms.update(room["id"], state="live", session_id="LIVE_X")
    assert rooms.get_by_session("LIVE_X")["id"] == room["id"]
    assert rooms.get_by_session("") is None
    assert rooms.get_by_session("nope") is None


def test_close_room_marks_stopped_before_stopping_session(db, monkeypatch):
    """先落 stopped 再 live.stop：否则采集端会在收尾的几十秒里继续录。"""
    room, _ = rooms.add("https://live.douyin.com/4")
    rooms.update(room["id"], state="live", session_id="S1")
    pcm = live.AUDIO_DIR / "S1.pcm"
    pcm.write_bytes(b"\x00\x00" * 100)
    seen = {}

    def fake_stop(sid, finalize=True):
        seen["state"] = (rooms.get(room["id"]) or {}).get("state")
        seen["sid"] = sid
        # 模拟收尾落盘：live.stop 返回时 DB 里已经有 audio_path
        wav = live.AUDIO_DIR / "S1.wav"
        wav.write_bytes(b"RIFF")
        helpers.add_session(db, "S1", audio_path=str(wav), duration_s=1.0)
        return {}

    monkeypatch.setattr(server.live, "stop", fake_stop)
    out = server._close_room(db, room["id"])
    assert seen == {"state": "stopped", "sid": "S1"}
    assert out["state"] == "stopped" and out["has_recording"] is True


def test_reconcile_rooms_demotes_active_at_startup(db):
    """重启不是「接着采」：残留的 pending/starting/live 必须落成已中断。

    否则采集代理第一拍就把它们当待办，自动重开上次的直播间——这正是
    「重启后默认打开上一次直播间」的根因。
    """
    live_room, _ = rooms.add("https://live.douyin.com/501")
    rooms.update(live_room["id"], state="live", stage=6, session_id="S2",
                 switch_ready=True, switch_engine="shared")
    wav = live.AUDIO_DIR / "S2.wav"
    wav.write_bytes(b"RIFF")
    helpers.add_session(db, "S2", audio_path=str(wav), duration_s=1.0)

    starting, _ = rooms.add("https://live.douyin.com/502")
    rooms.update(starting["id"], state="starting", stage=2,
                 stage_note="正在解析抖音流地址")
    pending, _ = rooms.add("https://live.douyin.com/503")
    stopped, _ = rooms.add("https://live.douyin.com/504")
    rooms.update(stopped["id"], state="stopped", stage=6)
    failed, _ = rooms.add("https://live.douyin.com/505")
    rooms.update(failed["id"], state="failed", error="没有拿到音频")

    assert server.reconcile_rooms(db) == 3

    r = rooms.get(live_room["id"])
    assert r["state"] == "stopped" and r["interrupted"] is True
    assert r["stage"] == 6 and r["failed_stage"] is None
    assert r["has_recording"] is True and r["saved"] is True
    assert r["session_id"] == "S2"          # 保留：回放和详情还要用
    assert "录音已保存" in r["error"]
    assert r["switch_ready"] is False and r["switch_engine"] == ""

    s = rooms.get(starting["id"])
    assert s["state"] == "stopped" and s["interrupted"] is True
    assert "还没建立会话" in s["error"]

    assert rooms.get(pending["id"])["state"] == "stopped"
    # 用户自己停的、以及失败待处理的，不归「服务重启」管
    assert rooms.get(stopped["id"])["interrupted"] is False
    assert rooms.get(failed["id"])["state"] == "failed"

    # 收口后不能再有任何 active 状态：代理的 tick 只认这三种才会拉起
    assert not [x for x in rooms.list_rooms()
                if x.get("state") in ("pending", "starting", "live")]

    # 幂等：代理/服务再重启一次不会重复改写
    assert server.reconcile_rooms(db) == 0


def test_reconcile_skips_rooms_without_session_and_marks_no_recording(db):
    room, _ = rooms.add("https://live.douyin.com/506")
    rooms.update(room["id"], state="starting", stage=1)
    assert server.reconcile_rooms(db) == 1
    r = rooms.get(room["id"])
    assert r["state"] == "stopped"
    assert r["has_recording"] is False and r["saved"] is False
    # 重试会清掉中断标记，房间重新排队
    rooms.update(room["id"], state="pending", interrupted=False)
    r = rooms.get(room["id"])
    assert r["interrupted"] is False and r["state"] == "pending"


@pytest.fixture
def http_base():
    srv = server.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        server.live.stop_watchdog()


def test_retry_over_http_clears_interrupted(http_base, db):
    """前端「重试」走的就是这个接口：回到 pending 时必须清掉中断标记。"""
    room, _ = rooms.add("https://live.douyin.com/507")
    rooms.update(room["id"], state="live", stage=6, session_id="S3")
    assert server.reconcile_rooms(db) == 1

    body = json.dumps({"id": room["id"], "state": "pending", "error": ""}).encode()
    req = urllib.request.Request(
        http_base + "/api/rooms/state", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200

    r = rooms.get(room["id"])
    assert r["state"] == "pending" and r["interrupted"] is False
    assert r["session_id"] == "" and r["error"] == ""
    assert r["has_recording"] is False


def test_late_agent_report_cannot_revive_stopped_room(http_base, db):
    """停止后采集代理迟到的 starting/live 也不能把房间重新点亮。

    直取代理在停止与首块音频并发时会回报 live；旧版按 source=agent 放行，
    房间被点亮后 _renew_session 又会重建会话，停止就失效了。
    """
    room, _ = rooms.add("https://live.douyin.com/508")
    rooms.update(room["id"], state="live", stage=6, session_id="S4")
    assert server.reconcile_rooms(db) == 1      # 启动收口：stopped + interrupted

    for st in ("live", "starting", "failed"):
        body = json.dumps({"id": room["id"], "state": st, "source": "agent",
                           "session_id": "S4"}).encode()
        req = urllib.request.Request(
            http_base + "/api/rooms/state", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        assert data.get("ignored") == "stopped"
        r = rooms.get(room["id"])
        assert r["state"] == "stopped" and r["interrupted"] is True
        assert r["session_id"] == "S4"


# ---------------------------------------------------------------- 房间名
# 为什么要有名字：直取模式下房间从来没名字，前端只能退回显示域名
# （webcast.amemv.com），多路并存时没人认得出哪一路是哪个直播间，
# 停了也不知道该删哪个。

def test_add_keeps_share_label_as_provisional_name():
    """分享文案里的主播名先垫上，标成 share，等采集端换成真名。"""
    a, _ = rooms.add("https://live.douyin.com/601", name="示例主播")
    assert a["name"] == "示例主播"
    assert a["name_source"] == "share"

    # 没有名字时两个字段都是空的，前端才知道该退回域名
    b, _ = rooms.add("https://live.douyin.com/602")
    assert b["name"] == "" and b["name_source"] == ""


def test_reposting_link_fills_missing_name():
    """老房间没名字，重发一次分享链接应当把名字补上，而不是又退回域名。"""
    a, _ = rooms.add("https://live.douyin.com/603")
    assert a["name"] == ""
    again, created = rooms.add("https://live.douyin.com/603", name="示例ai落地派")
    assert not created and again["id"] == a["id"]
    assert again["name"] == "示例ai落地派" and again["name_source"] == "share"
    # 已经有了真名的房间，不能被后来的分享文案改写
    rooms.update(a["id"], name="主播 · 这场标题", name_source="page")
    keep, _ = rooms.add("https://live.douyin.com/603", name="别的名字")
    assert keep["name"] == "主播 · 这场标题"


def test_rename_api_wins_over_agent_reports(http_base, db):
    """人工改名后，采集端后来读到的名字不许盖回去。"""
    room, _ = rooms.add("https://live.douyin.com/604")
    body = json.dumps({"id": room["id"], "name": "示例主播 · 今晚聊 FDE"}).encode()
    req = urllib.request.Request(
        http_base + "/api/rooms/rename", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        assert json.loads(resp.read().decode())["room"]["name"] == "示例主播 · 今晚聊 FDE"

    r = rooms.get(room["id"])
    assert r["name"] == "示例主播 · 今晚聊 FDE" and r["name_source"] == "user"

    # 采集端再报名字：被忽略
    body = json.dumps({"id": room["id"], "source": "agent", "state": "live",
                       "name": "页面读到的名字"}).encode()
    req = urllib.request.Request(
        http_base + "/api/rooms/state", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
    assert rooms.get(room["id"])["name"] == "示例主播 · 今晚聊 FDE"


def test_rename_api_rejects_blank_name(http_base, db):
    room, _ = rooms.add("https://live.douyin.com/605")
    body = json.dumps({"id": room["id"], "name": "   "}).encode()
    req = urllib.request.Request(
        http_base + "/api/rooms/rename", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400


def test_heal_fake_live_url_uses_room_id_as_web_rid():
    """旧直取拿 reflow 的 room id 当 web_rid，拼出的地址要清掉。"""
    room = {
        "url": "https://webcast.amemv.com/douyin/webcast/reflow/"
               "7700000000000000001",
        "resolved_url": "https://live.douyin.com/7700000000000000001",
    }
    assert rooms._heal_fake_live_url(room) is True
    assert room["resolved_url"] == ""

    real = {
        "url": "https://webcast.amemv.com/douyin/webcast/reflow/"
               "7700000000000000001",
        "resolved_url": "https://live.douyin.com/123456789012",
    }
    assert rooms._heal_fake_live_url(real) is False
    assert real["resolved_url"] == "https://live.douyin.com/123456789012"


def test_list_rooms_heals_persisted_fake_url():
    """老数据里的假地址读取时就要清掉，不等房间重启。"""
    fake = {
        "id": "rm1",
        "url": "https://webcast.amemv.com/douyin/webcast/reflow/"
               "7700000000000000001",
        "resolved_url": "https://live.douyin.com/7700000000000000001",
        "state": "stopped",
    }
    rooms.PATH.write_text(json.dumps({"rooms": [fake]}, ensure_ascii=False),
                          encoding="utf-8")
    got = rooms.list_rooms()
    assert got[0]["resolved_url"] == ""
    saved = json.loads(rooms.PATH.read_text(encoding="utf-8"))
    assert saved["rooms"][0]["resolved_url"] == ""


def test_agent_name_report_marks_source_page(http_base, db):
    room, _ = rooms.add("https://live.douyin.com/606", name="分享文案里的主播名")
    body = json.dumps({"id": room["id"], "source": "agent", "state": "live",
                       "name": "示例主播 · 示例直播标题"}).encode()
    req = urllib.request.Request(
        http_base + "/api/rooms/state", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
    r = rooms.get(room["id"])
    assert r["name"] == "示例主播 · 示例直播标题"
    assert r["name_source"] == "page"
