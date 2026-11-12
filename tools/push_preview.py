# -*- coding: utf-8 -*-
"""预览飞书推送文案：不连飞书、不发消息，纯本地渲染。

    python tools/push_preview.py                 # 内置样例（不需要数据库）
    python tools/push_preview.py LIVE_20260101_120000   # 用库里真实场次

为什么要有这个脚本：飞书消息发出去撤不回，而"改完文案等下一场直播看效果"
一次要等几小时。把渲染和发送拆开，文案可以在这台机器上先看一百遍；
真实场次还能顺便确认"北京 / UTC 两个时间对不对、录音偏移对不对"。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import feedback, highlight, live, store, timefmt  # noqa: E402


def _fixture() -> tuple[dict, dict]:
    session = {"id": "LIVE_20260101_120000", "title": "示例直播复盘",
               "started_at": "2026-01-01T20:00:00",
               "started_utc": "2026-01-01T12:00:00+00:00", "tz_offset_min": 480}
    ev = {"session_id": session["id"], "peak_t": 549, "kind": "info", "level": "P0",
          "score": 3.1, "speaker_label": "阿哲", "speaker_state": "manual",
          "why": "抢麦；提到 2 处数字/金额；结论句",
          "text": "我建议先做一次模拟面试录音回听，看看真实情况。"}
    return session, ev


def _show(session: dict, events: list[dict], pushed: set[float]) -> None:
    print("=" * 60)
    print("【实时推送 · 纯文本（卡片失败时用它）】")
    print(live._push_text(events[0], session) if events else "（本场没有高光，不会推）")
    print()
    print("【实时推送 · 卡片 JSON（发给飞书的就是它）】")
    if events:
        print(json.dumps(live._push_card(events[0], session), ensure_ascii=False, indent=2))
    else:
        print("（无）")
    print()
    print("【收工回顾】")
    rest = [e for e in events if round(float(e["peak_t"]), 2) not in pushed]
    if not rest:
        print("（全都实时推过了，回顾静默）")
    else:
        n_pushed = sum(1 for e in events if round(float(e["peak_t"]), 2) in pushed)
        print(live._digest_text(session, rest[:3], len(events), n_pushed))
    print()


def _send_test(session: dict, ev: dict) -> int:
    """真发一张测试卡片：卡片 schema 只能在真实租户上验，本地渲染再像也没用。"""
    from app import feishu
    try:
        bot = feishu.Bot()
    except Exception as e:
        print("没发出去（凭证/依赖问题）：%s" % e)
        return 2
    ok = bot.send_card(live._push_card(ev, session))
    print("测试卡片：%s" % ("已发送，去飞书看一眼" if ok
                            else "发送失败（看上面的 code/msg，多半是 schema 或权限）"))
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    send = "--send" in argv
    argv = [a for a in argv if a != "--send"]
    if not argv:
        session, ev = _fixture()
        print("样例场次：%s（%s）" % (session["title"], timefmt.clock_line(session, 0)))
        events = [dict(ev, peak_t=p, score=s, speaker_label=lab) for p, s, lab in
                  [(549, 3.1, "阿哲"), (130, 2.9, "嘉宾"), (905, 2.7, "主播")]]
        _show(session, events, {549.0})
        return _send_test(session, events[0]) if send else 0

    sid = argv[0]
    conn = store.connect()
    try:
        session = store.get_session(conn, sid)
        if not session:
            print("没有这场：%s（先跑 python run.py 生成/录制）" % sid)
            return 2
        events = highlight.score_session(conn, sid)
        pushed = store.pushed_peaks(conn, sid)
        history = store.push_history(conn, sid)
        learned = feedback.details(conn)
    finally:
        conn.close()
    print("场次：%s（%s）" % (session.get("title"), timefmt.clock_line(session, 0)))
    print("已有推送记录：%d 条" % len(history))
    for h in history[-8:]:
        print("  %-10s %-8s %s" % (h["item"], h["status"], h["reason"] or "-"))
    learned = [x for x in learned if x["delta"]]
    if learned:
        print("推送偏好（已学）：" + "、".join(
            "%s%+.2f（%d👍/%d👎）" % (x["label"], x["delta"], x["good"], x["bad"])
            for x in learned))
    _show(session, events, pushed)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
