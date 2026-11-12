# -*- coding: utf-8 -*-
"""导出一场的分享包（摘要 + SRT + 音频），用于人工上传飞书妙记。

    python tools/share_package.py --session LIVE_20261109_140948
    python tools/share_package.py --session LIVE_... --out D:/tmp/share --no-audio
    python tools/share_package.py --session LIVE_... --minutes https://xxx.feishu.cn/minutes/xxx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import share, store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="导出一场的分享包")
    ap.add_argument("--session", required=True, help="场次 id，如 LIVE_20261109_140948")
    ap.add_argument("--out", default="", help="输出目录（默认 data/share/<session>）")
    ap.add_argument("--no-audio", action="store_true", help="只导出摘要/字幕，不带音频")
    ap.add_argument("--no-zip", action="store_true", help="只生成目录，不压 zip")
    ap.add_argument("--minutes", default="", help="顺手回填妙记分享链接")
    args = ap.parse_args()

    conn = store.connect()
    try:
        store.init_db(conn)
        if not store.get_session(conn, args.session):
            print("场次不存在：%s" % args.session, file=sys.stderr)
            return 2
        if args.minutes:
            store.set_minutes_url(conn, args.session, args.minutes)
        info = share.build_package(
            conn, args.session,
            out_dir=(Path(args.out) if args.out else None),
            include_audio=not args.no_audio)
        zpath = None if args.no_zip else share.zip_package(info["dir"])
    finally:
        conn.close()

    print("目录：%s" % info["dir"])
    print("文件：%s" % "、".join(info["files"]))
    if zpath:
        print("压缩包：%s（%.1f MB）" % (zpath, zpath.stat().st_size / 1048576.0))
    if args.minutes:
        print("妙记链接：%s" % args.minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
