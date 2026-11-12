# -*- coding: utf-8 -*-
"""高光事件流的精度验收工具。

    python tools/highlight_eval.py                 # 全部场次：重算 + 生成标注表
    python tools/highlight_eval.py --sid LIVE_xxx  # 只跑一场
    python tools/highlight_eval.py --min-utts 50   # 跳过太短的场次（默认 40）
    python tools/highlight_eval.py --report        # 读回标注表，算 precision@K

标注表在 data/eval/<sid>.md，每条前面是 "- [ ]"，你回听一遍把认可的改成 "- [x]"，
然后跑 --report。默认校验「推 5 条里中了几条」（precision@5）——单用户场景下这是
唯一有意义的指标：每天只会点开几条，漏掉不致命，推错才致命。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import highlight, store  # noqa: E402

OUT = ROOT / "data" / "eval"


def _ts(x: float) -> str:
    x = max(0, int(x or 0))
    return "%d:%02d" % (x // 60, x % 60)


def _safe(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name)[:40]


def make(conn, sid: str, top: int) -> dict:
    out = highlight.build(conn, sid, limit=max(top, 12))
    sess = store.get_session(conn, sid) or {}
    rows = store.list_utterances_for_highlight(conn, sid)
    events = out["highlights"][:top]
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / ("%s_%s.md" % (sid, _safe(sess.get("title") or "")))
    lines = [
        "# 高光标注：%s" % (sess.get("title") or sid),
        "",
        "- 场次：%s" % sid,
        "- 时长：%s · %d 句 · 音频特征：%s" % (
            _ts(sess.get("duration_s") or 0), len(rows),
            "有" if out.get("audio") else "无（纯文本）"),
        "- 规则：回听后把认可的行 [ ] 改成 [x]，然后跑 python tools/highlight_eval.py --report",
        "",
    ]
    for i, e in enumerate(events, 1):
        lines += [
            "- [ ] **#%d** %s [%s/%s] 分 %.1f（信息 %.1f / 情绪 %.1f）" % (
                i, _ts(e["peak_t"]), e["level"], e["kind"], e["score"],
                e["info_z"], e["arousal_z"]),
            "  - 理由：%s" % (e["why"] or "—"),
            "  - %s：%s" % (e["speaker_label"] or "—", (e["text"] or "").strip()),
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"sid": sid, "title": sess.get("title") or sid, "path": path,
            "events": events, "audio": out.get("audio"), "n": out.get("n")}


def report(k: int = 5) -> int:
    """直接读网页端点的 👍 / 👎，不再依赖命令行标注。"""
    conn = store.connect()
    store.init_db(conn)
    try:
        sids = [r["session_id"] for r in conn.execute(
            "SELECT session_id FROM highlights WHERE feedback<>0 "
            "GROUP BY session_id ORDER BY MAX(feedback_at) DESC")]
        if not sids:
            print("还没有任何标注。打开 /highlights?session=<sid>，点几下 👍 / 👎 再回来。")
            return 1
        print("%-44s %5s %5s %7s" % ("场次", "推K", "命中", "精确率"))
        print("-" * 68)
        tot_hit = tot_n = 0
        for sid in sids:
            rows = [dict(r) for r in conn.execute(
                "SELECT feedback FROM highlights WHERE session_id=? "
                "ORDER BY score DESC LIMIT ?", (sid, k))]
            good = sum(1 for r in rows if int(r["feedback"] or 0) > 0)
            sess = store.get_session(conn, sid) or {}
            tot_hit += good
            tot_n += len(rows)
            print("%-44s %5d %5d %6.0f%%" % (
                (sess.get("title") or sid)[:44], len(rows), good,
                100.0 * good / max(1, len(rows))))
        print("-" * 68)
        print("合计：%d/%d = %.0f%%（目标 ≥80%%：推 5 条至少 4 条值得听）" % (
            tot_hit, tot_n, 100.0 * tot_hit / max(1, tot_n)))
        return 0
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", default="")
    ap.add_argument("--min-utts", type=int, default=40)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("-k", type=int, default=5)
    a = ap.parse_args(argv)

    if a.report:
        return report(a.k)

    conn = store.connect()
    store.init_db(conn)
    try:
        sess = store.list_sessions(conn)
        if a.sid:
            sess = [s for s in sess if s["id"] == a.sid]
        else:
            sess = [s for s in sess if (s["n_utterances"] or 0) >= a.min_utts]
        if not sess:
            print("没有符合条件的场次")
            return 1
        print("共 %d 场，开始重算…\n" % len(sess))
        for s in sess:
            r = make(conn, s["id"], a.top)
            print("== %s  %s" % (s["id"], (s.get("title") or "")[:34]))
            print("   音频特征：%s  事件数：%s" % ("有" if r["audio"] else "无", r["n"]))
            for i, e in enumerate(r["events"], 1):
                print("   %d. %6s %-4s 分%4.1f  %-24s | %s" % (
                    i, _ts(e["peak_t"]), e["level"], e["score"],
                    (e["why"] or "—")[:24], (e["text"] or "")[:40]))
            print()
        print("标注表已写入：%s" % OUT)
        print("回听并勾选后，跑：python tools/highlight_eval.py --report")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
