# -*- coding: utf-8 -*-
"""飞书命令 <-> 本机服务 的接口自测：不联网、不发飞书、不开浏览器。

    python tools/selftest_watch_api.py

起一个真的 HTTP 服务（进程内，随机端口），把房间表指到 .tmp，然后像飞书那样
一条条发命令进去，验证：
  1 陌生站点 -> 弹确认卡，房间没被建
  2 回「确认 码」-> 白名单生效 + 房间真的进了服务
  3 同一个链接再发 -> 不重复建，回「已经在听」
  4 /rooms 能列出名字和状态
  5 /stop 1 -> 服务里那一路变成 stopped
  6 满 5 路时第 6 条链接被挡住，并给出停哪一路的提示
  7 卡片发不出去时退回文字（不能一声不响）
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import rooms, watchlink  # noqa: E402
from app.server import make_server  # noqa: E402

FAILED: list[str] = []
TMP = ROOT / ".tmp"
TMP.mkdir(exist_ok=True)
rooms.PATH = TMP / "rooms_selftest_api.json"
watchlink.PATH = TMP / "trust_selftest_api.json"


def check(cond: bool, msg: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


class FakeBot:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.cards: list[dict] = []
        self.card_ok = True

    def send_text(self, text, open_id=""):
        self.texts.append(text)
        return True

    def send_card(self, card, open_id=""):
        self.cards.append(card)
        return self.card_ok


def main() -> int:
    for p in (rooms.PATH, watchlink.PATH):
        try:
            p.unlink()
        except OSError:
            pass

    srv = make_server("127.0.0.1", 8811)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    import tools.feishu_bot as fb  # noqa: E402

    fb._BASE = None
    fb._BASES = ["http://127.0.0.1:%d" % port]
    bot = FakeBot()
    fb._BOT = bot

    print("[1] 陌生站点先确认")
    out = fb._route("https://fan.wildsite.net/live/8801", "", "")
    check(out == "" and len(bot.cards) == 1, "只发卡片，不建房间")
    check(rooms.list_rooms() == [], "服务里确实没有房间")
    card = bot.cards[0]
    fields = card["elements"][0]["fields"]
    code = fields[1]["text"]["content"].split("\n")[-1]
    check(fields[0]["text"]["content"].endswith("wildsite.net"), "卡片写清站点")
    allow_btn = card["elements"][4]["actions"][0]
    check(allow_btn["value"] == {"act": "allow", "code": code}, "按钮带回调值")

    print("[2] 回「确认 码」")
    out = fb._route("确认 %s" % code, "", "")
    check("已允许" in out and "已开始" in out, "放行并开听")
    check(watchlink.trusted("wildsite.net"), "白名单记下了")
    rs = rooms.list_rooms()
    check(len(rs) == 1 and rs[0]["state"] == "pending", "服务里有一条待启动")

    print("[3] 同一链接再发")
    out = fb._route("https://fan.wildsite.net/live/8801", "", "")
    check("已经在听" in out, "不重复建")
    check(len(rooms.list_rooms()) == 1, "房间数还是 1")

    print("[4] /rooms")
    rooms.update(rs[0]["id"], state="live", name="野站直播间")
    out = fb._route("/rooms", "", "")
    check("正在听 1/5" in out, "计数对")
    check("🟢 野站直播间 监听中" in out, "名字 + 状态")
    check("https://fan.wildsite.net/live/8801" in out, "带链接")

    print("[5] 满员")
    for i in range(2, 6):
        r, _ = rooms.add("https://live.douyin.com/10000%d" % i)
        rooms.update(r["id"], state="live", name="抖音 %d" % i)
    out = fb._route("https://live.douyin.com/999999", "", "")
    check("已经 5/5 路" in out and "停：/stop 1" in out, "第 6 条被挡住")
    check(len(rooms.list_rooms()) == 5, "没有偷偷建第 6 路")

    print("[6] /stop 1")
    out = fb._route("/stop 1", "", "")
    check("已停" in out, "停掉了第 1 路")
    states = sorted(r["state"] for r in rooms.list_rooms())
    check(states.count("stopped") == 1, "服务里那一路是 stopped")

    print("[7] 卡片发不出去")
    bot.card_ok = False
    with open(watchlink.PATH, "w", encoding="utf-8") as f:
        json.dump({"domains": {}, "pending": []}, f)
    out = fb._route("https://another.newsite.org/live/7001", "", "")
    check("确认" in out and len(out) > 10, "退回文字，告诉用户怎么确认")
    bot.card_ok = True

    srv.shutdown()
    print()
    if FAILED:
        print("失败 %d 项：" % len(FAILED))
        for f in FAILED:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
