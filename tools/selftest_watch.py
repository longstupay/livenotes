# -*- coding: utf-8 -*-
"""「贴链接就开听」的离线自测：判定闸门 + 白名单 + 确认流程 + 命令路由。

    python tools/selftest_watch.py

不联网、不发飞书、不建房间：resolve / _start_room / send_* 全部换成本地替身。
覆盖：
  1 分享文案里捞链接（前后有中文、标点）
  2 抖音/快手/B 站直播间 -> 直接开
  3 陌生站点 -> 要确认（且子域跟着父域走、不许后缀骗过）
  4 个人主页 / 短视频页 / 首页 -> 退回，并说清楚为什么
  5 本机地址、带账号密码、非 80/443 -> 退回
  6 短链展开：落到直播页放行，落到视频页退回
  7 短链展不开时不误杀
  8 白名单写入/生效/遗忘
  9 确认码：一次性、过期、唯一时免码
 10 命令路由：链接、确认、/rooms、/stop、关注词不被链接吃掉
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import watchlink  # noqa: E402

FAILED: list[str] = []
TMP = ROOT / ".tmp"
TMP.mkdir(exist_ok=True)
watchlink.PATH = TMP / "watch_trust_selftest.json"


def check(cond: bool, msg: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def reset() -> None:
    try:
        watchlink.PATH.unlink()
    except OSError:
        pass


def d(text: str, resolver=None) -> dict:
    return watchlink.decide(text, resolver=resolver)


def main() -> int:
    reset()
    print("[1] 从分享文案里捞链接")
    share = ("7- 长按复制此条消息，打开抖音搜索，查看TA的直播间。"
             " https://live.douyin.com/123456?enter_from=share 复制结果，打开抖音")
    check(watchlink.extract(share) == "https://live.douyin.com/123456?enter_from=share",
          "捞到的是链接本身，不带后面的中文")
    check(watchlink.extract("没链接的一句话") == "", "没链接 -> 空")
    r = d(share)
    check(r["kind"] == "watch" and r["room"] == "123456", "抖音直播间 -> 直接开，房号 123456")
    check("enter_from" not in r["url"], "前端参数被剔掉（同一个房间不会算两路）")

    print("[2] 已知平台")
    check(d("https://live.kuaishou.com/u/3xabcDEF")["kind"] == "watch", "快手 /u/xxx -> 直接开")
    check(d("https://live.bilibili.com/10002")["kind"] == "watch", "B 站 -> 直接开")
    check(d("http://live.douyin.com/888888")["kind"] == "watch", "http 也收")

    print("[3] 陌生站点要确认")
    r = d("https://fan.example.com/live/8801")
    check(r["kind"] == "confirm" and r["site"] == "example.com", "陌生站点 -> confirm")
    check(r["ok"] is False, "confirm 不算 ok")
    check(d("https://live.douyin.com.evil.com/123456")["kind"] == "confirm",
          "douyin.com.evil.com 不能靠后缀混过去")
    check(d("https://evildouyin.com/123456")["kind"] == "confirm", "evildouyin.com 也不行")
    watchlink.allow("example.com", "selftest")
    check(d("https://fan.example.com/live/8801")["kind"] == "watch", "确认过之后直接开")
    check(d("https://other.example.com/live/9012")["kind"] == "watch", "子域一起放行")
    check(d("https://example.com.cn/live/9012")["kind"] == "confirm", "example.com.cn 是另一个站")

    print("[4] 不是直播间的链接")
    for url, why in [
        ("https://www.douyin.com/user/MS4wLjABAAAA", "个人主页"),
        ("https://www.douyin.com/video/7300000000", "短视频"),
        ("https://live.douyin.com/", "首页"),
    ]:
        r = d(url)
        check(r["kind"] == "reject" and r["reason"], "%s -> 退回：%s" % (why, r["reason"][:26]))
    r = d("https://live.kuaishou.com/profile/3xabc")
    check(r["kind"] == "reject", "快手个人主页 -> 退回")

    print("[5] 形式不对的")
    check(d("https://127.0.0.1:8910/room_fake.html")["kind"] == "reject", "本机地址 -> 退回")
    check(d("https://192.168.1.10:8765/live")["kind"] == "reject", "局域网地址 -> 退回")
    check(d("https://user:pw@live.douyin.com/123456")["kind"] == "reject", "带账号密码 -> 退回")
    check(d("https://live.douyin.com:8080/123456")["kind"] == "reject", "非标准端口 -> 退回")
    check("没看到链接" in d("随便说点什么")["reason"], "没链接 -> 告诉他怎么发")
    r = d("https://live.douyin.com/111 https://live.douyin.com/222")
    check("一个链接" in r["note"], "一次两个链接 -> 先开第一个并提示")

    print("[6] 短链")
    live_land = lambda u: ("https://live.douyin.com/654321", "")
    video_land = lambda u: ("https://www.douyin.com/video/7311111", "")
    r = d("https://v.douyin.com/iRNBho6u/", resolver=live_land)
    check(r["kind"] == "watch" and r["room"] == "654321", "短链 -> 直播页，房号取展开后的")
    check(r["resolved_from"] == "https://v.douyin.com/iRNBho6u/", "记下原始短链")
    r = d("https://v.douyin.com/iRNBho6u/", resolver=video_land)
    check(r["kind"] == "reject" and "短视频" in r["reason"], "短链 -> 视频页，退回并说明")
    check("已展开为" in r["note"], "同时告诉用户短链落到哪了")

    print("[6b] 抖音分享的真实落地页")
    reflow = lambda u: ("https://webcast.amemv.com/douyin/webcast/reflow/7700000000000000004"
                        "?did=MS4w&sec_user_id=MS4w&extra_params=%7B%7D", "")
    r = d("https://v.douyin.com/EQUfUaFLm1o/", resolver=reflow)
    check(r["kind"] == "watch", "落地页是 amemv.com 也算抖音，不再弹确认")
    # reflow 后面的 id 不是 web_rid，不能改写成 live.douyin.com/<id>；
    # 直取也直接从 reflow 页面取流，所以保留落地页。
    check(r["url"] == "https://webcast.amemv.com/douyin/webcast/reflow/7700000000000000004",
          "保留 reflow 落地页")
    check(r["room"] == "7700000000000000004", "房号从 path 里取")
    r2 = d("https://webcast.amemv.com/douyin/webcast/reflow/7700000000000000004?did=abc")
    check(r2["kind"] == "watch" and "did=" not in r2["url"], "直接贴落地页也能用，脏参数不带进来")
    check(all(x["kind"] == "watch" for x in (r, r2)), "同一场：短链和落地页归到同一个地址")

    print("[7] 短链展不开（离线）")
    r = d("https://v.douyin.com/iRNBho6u/", resolver=lambda u: (u, "timed out"))
    check(r["kind"] == "watch", "展不开就按原样放行，不误杀")
    check("没能展开" in r["note"], "同时告诉用户没展开")

    print("[8] 白名单存取")
    check(watchlink.trusted("douyin.com"), "预置平台算放行")
    check(not watchlink.trusted("nope.example"), "没记过的不算")
    watchlink.allow("nope.example", "手工")
    check(watchlink.trusted("nope.example"), "allow 之后生效")
    check(watchlink.forget("nope.example") is True, "forget 返回 True")
    check(not watchlink.trusted("nope.example"), "forget 之后失效")
    check(watchlink.forget("nope.example") is False, "再 forget 返回 False")

    print("[9] 确认码")
    p = watchlink.pending_new("https://fan.example.com/live/1", "example.com")
    check(len(p["code"]) == 4, "确认码 4 位")
    check(watchlink.pending_unique() is not None and watchlink.pending_unique()["code"] == p["code"],
          "只有一个待确认时，报「确认」就够")
    check(watchlink.pending_get(p["code"])["url"].endswith("/live/1"), "按码取回")
    taken = watchlink.pending_take(p["code"])
    check(taken and watchlink.pending_get(p["code"]) is None, "取一次就消失（防连点）")
    p2 = watchlink.pending_new("https://fan.example.com/live/2", "example.com")
    watchlink.pending_take(p2["code"])
    stale = {"code": "ZZZZ", "url": "x", "site": "x", "expires_at": time.time() - 1}
    watchlink._write({"domains": watchlink._read()["domains"], "pending": [stale]})
    check(watchlink.pending_get("ZZZZ") is None, "过期的不认")
    check(watchlink.pending_unique() is None, "过期的也不占唯一名额")

    print("[10] 命令路由")
    import tools.feishu_bot as fb  # noqa: E402

    sent: list[str] = []
    cards: list[dict] = []

    class FakeBot:
        def send_text(self, text, open_id=""):
            sent.append(text)
            return True

        def send_card(self, card, open_id=""):
            cards.append(card)
            return True

        def send_any(self, *a, **k):
            return True

    fb._BOT = FakeBot()
    fb._start_room = lambda url, resolved_from="": "STARTED " + url
    fb._rooms = lambda: [{"id": "rm1", "seq": 1, "url": "https://live.douyin.com/123456",
                          "state": "live", "name": "某某直播间"}]
    fb._api = lambda path, payload=None, timeout=6.0: {"ok": True, "rooms": fb._rooms()}

    check("STARTED" in fb._route("https://live.douyin.com/123456", "", ""), "贴链接 -> 开听")
    check("STARTED" in fb._route("关注这个 https://live.douyin.com/123456", "", ""),
          "文案里带「关注」也按链接走")
    check("已关注" in fb._route("关注 FDE、OPC", "", ""), "纯关键词命令照旧")
    check("正在听 1/5" in fb._route("/rooms", "", ""), "/rooms 列出来")
    check("1. CH01 🟢" in fb._route("/rooms", "", ""), "序号 + CH 编号 + 状态图标")

    cards.clear()
    out = fb._route("https://fan.wildsite.net/live/8801", "", "")
    check(out == "" and cards, "陌生站点 -> 只发卡片")
    code = cards[0]["elements"][0]["fields"][1]["text"]["content"].split("\n")[-1]
    check(len(code) == 4, "卡片里带确认码 %s" % code)
    check("确认 %s" % code in json_dump(cards[0]), "卡片里写清文字兜底")

    check("已允许" in fb._route("确认 %s" % code, "", ""), "回「确认 码」-> 放行并开听")
    check(watchlink.trusted("example.com"), "确认后进白名单")
    check(fb._route("确认 %s" % code, "", "") != "", "同一个码不能连用两次")

    print()
    if FAILED:
        print("失败 %d 项：" % len(FAILED))
        for f in FAILED:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


def json_dump(x) -> str:
    import json
    return json.dumps(x, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
