# -*- coding: utf-8 -*-
"""生成周报 / 复盘文档，可选直接建成飞书云文档。

    python tools/weekly_report.py --range 上周
    python tools/weekly_report.py --kind retro --range 7天
    python tools/weekly_report.py --since 2026-11-03 --until 2026-11-09 --out D:/tmp/report
    python tools/weekly_report.py --range 上周 --doc
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import feishu, report, store  # noqa: E402


def _date_arg(text: str) -> date:
    try:
        return date.fromisoformat(str(text).strip())
    except ValueError:
        raise argparse.ArgumentTypeError("日期格式：YYYY-MM-DD")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成直播周报 / 复盘")
    ap.add_argument("--kind", choices=sorted(report.KINDS), default="weekly")
    ap.add_argument("--range", default="", help="上周 / 本周 / 7天 / 11-03~11-09")
    ap.add_argument("--since", type=_date_arg, help="起始日期 YYYY-MM-DD")
    ap.add_argument("--until", type=_date_arg, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--out", default="", help="输出目录（默认 data/reports）")
    ap.add_argument("--no-write", action="store_true", help="只打印，不落地文件")
    ap.add_argument("--doc", action="store_true", help="顺手建成飞书云文档")
    args = ap.parse_args()

    if args.range:
        r = report.parse_range(args.range)
        since, until = r["since"], r["until"]
    else:
        since, until = args.since, args.until
        if since is None and until is not None:
            # 只给截止日期时，默认看截止日前 7 天；交给 build 的默认值会从
            # 「今天往前 7 天」起算，区间可能反向，生成一份莫名其妙的报告。
            since = until - timedelta(days=6)
        elif until is None:
            until = since
    if since and until and since > until:
        since, until = until, since

    conn = store.connect()
    try:
        store.init_db(conn)
        info = report.build(conn, kind=args.kind, since=since, until=until,
                            out_dir=(Path(args.out) if args.out else None),
                            write=not args.no_write)
    except ValueError as e:
        print("生成失败：%s" % e, file=sys.stderr)
        return 2
    finally:
        conn.close()

    st = info["stats"]
    print("%s：%d 场 · %s · %d 场有总结" % (
        info["title"], st["sessions"], report.duration_text(st["duration_s"]),
        st["with_summary"]))
    if info.get("path"):
        print("Markdown：%s" % info["path"])
    if args.doc:
        try:
            bot = feishu.Bot()
            out = bot.create_docx(
                info["title"], info["markdown"],
                share_with=(bot.owner_open_id or bot.open_id))
        except Exception as e:
            out = {"ok": False, "error": str(e)}
        if out.get("ok"):
            print("飞书云文档：%s" % out.get("url"))
            if out.get("shared"):
                print("已授权给：" + (bot.owner_open_id or bot.open_id))
            elif out.get("share_requested"):
                print("自动授权失败：%s" % out.get("share_error"), file=sys.stderr)
        elif out.get("url"):
            # 文档建出来了、正文没写进去：链接别丢，用户还能自己补内容。
            print("飞书云文档已建好，但正文没写进去（%s）：%s"
                  % (out.get("error"), out.get("url")), file=sys.stderr)
            if out.get("share_requested") and not out.get("shared"):
                print("自动授权失败：%s" % out.get("share_error"), file=sys.stderr)
            return 1
        else:
            print("云文档创建失败：%s" % out.get("error"), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
