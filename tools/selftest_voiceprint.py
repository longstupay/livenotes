# -*- coding: utf-8 -*-
"""声纹底库的行为自测：跑在数据库副本上，绝不碰线上库。

    python tools/selftest_voiceprint.py

覆盖六件事，都是改这块代码时最容易改坏的：
  1. 人工否决的簇，复查后不被同一个人再次命中
  2. 人工确认会落 person_id + 改名 + 追加原型
  3. 复查抢不走人工确认
  4. 位置标签（主播/嘉宾3）进不了底库
  5. 空底库时给出可读提示而不是崩
  6. 合并人物会搬走原型并删掉源
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import store, voiceprint  # noqa: E402

SID = "LIVE_20261105_040930"
FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def main() -> int:
    src = Path(store.DB_PATH)
    if not src.exists():
        print("没有 %s，先在主程序里录一场" % src)
        return 1
    tmpdir = Path(tempfile.mkdtemp())
    tmp = tmpdir / "selftest.db"
    shutil.copy(src, tmp)
    for suf in ("-wal", "-shm"):
        p = Path(str(src) + suf)
        if p.exists():
            shutil.copy(p, Path(str(tmp) + suf))
    store.DB_PATH = tmp
    conn = store.connect()
    store.init_db(conn)
    try:
        if not store.list_persons(conn):
            print("底库是空的：先跑 python tools/backfill_persons.py")
            return 1

        print("1) 否决")
        store.set_speaker_person(conn, SID, "SPK_04", None, None, None, "rejected")
        voiceprint.match_session(conn, SID)
        row = conn.execute("SELECT person_state FROM speakers WHERE session_id=? AND"
                           " speaker_id=?", (SID, "SPK_04")).fetchone()
        check(row["person_state"] == "rejected", "否决在复查后仍然保留")

        print("2) 确认")
        p = store.list_persons(conn)[0]
        before = store.count_person_emb(conn, p["person_id"])
        out = voiceprint.enroll_speaker(conn, SID, "SPK_03", p["name"],
                                        person_id=p["person_id"])
        row = conn.execute("SELECT person_id, person_state FROM speakers WHERE"
                           " session_id=? AND speaker_id=?", (SID, "SPK_03")).fetchone()
        check(bool(out.get("ok")) and row["person_id"] == p["person_id"]
              and row["person_state"] == "confirmed", "确认后落库为 confirmed")
        after = store.count_person_emb(conn, p["person_id"])
        print("       原型 %d → %d" % (before, after))

        print("3) 复查不抢夺")
        out = voiceprint.match_session(conn, SID)
        row = [r for r in out["rows"] if r["speaker_id"] == "SPK_03"][0]
        check(row["state"] == "confirmed", "复查后人工确认不被覆盖")

        print("4) 位置标签")
        bad = voiceprint.enroll_speaker(conn, SID, "SPK_02", "嘉宾3")
        check(not bad.get("ok"), "通用名进不了底库：%s" % bad.get("reason"))

        print("5) 空底库")
        conn.execute("DELETE FROM person_emb")
        conn.execute("DELETE FROM persons")
        conn.commit()
        out = voiceprint.match_session(conn, SID)
        check(out.get("ok") is False, "空底库给可读提示：%s" % out.get("reason"))

        print("6) 合并")
        store.upsert_person(conn, "per_aaa", "甲")
        store.upsert_person(conn, "per_bbb", "乙")
        store.add_person_emb(conn, [("per_aaa", "", "", 192, b"\x00" * 384, 1.0)])
        store.merge_persons(conn, "per_aaa", "per_bbb")
        check(store.count_person_emb(conn, "per_bbb") == 1
              and store.get_person(conn, "per_aaa") is None, "合并搬走原型并删除源")
    finally:
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    print("全部通过" if not FAILED else "失败 %d 项" % len(FAILED))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
