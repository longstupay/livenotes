# -*- coding: utf-8 -*-
"""把历史场次里「人工确认过的昵称」补种进跨场次的人物底库，然后全部复查一遍。

    python tools/backfill_persons.py            # 补种 + 全库复查
    python tools/backfill_persons.py --dry      # 只看会种哪些人，不写库
    python tools/backfill_persons.py --match    # 只复查，不补种

为什么需要它：改名即注册是从这一刻起才生效的，之前标过的名字只有一个字符串，
没存声纹。但它们对应的 utterance_emb 还在，所以能一次性把老资产捞回来。

「主播 / 嘉宾2 / 说话人1 / SPK_03」这类位置标签会被跳过——不同场次的「嘉宾1」
根本不是同一个人，拿它当身份会把底库污染成垃圾桶。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import store, voiceprint  # noqa: E402


def main(argv: list[str]) -> int:
    dry = "--dry" in argv
    only_match = "--match" in argv
    conn = store.connect()
    try:
        if not only_match:
            rows = [dict(r) for r in conn.execute(
                "SELECT session_id, speaker_id, display_name FROM speakers "
                "WHERE verified=1 ORDER BY session_id")]
            print("人工确认过的标签 %d 个" % len(rows))
            will, skip = [], []
            for r in rows:
                name = str(r["display_name"] or "")
                (skip if voiceprint.is_generic_name(name) else will).append(
                    (r["session_id"], r["speaker_id"], name))
            if skip:
                print("跳过 %d 个位置标签：%s" % (len(skip), "、".join(sorted({s[2] for s in skip}))))
            print("将注册 %d 个：" % len(will))
            for sid, spk, name in will:
                n = store.count_embeddings(conn, sid)
                print("  %-22s %-7s %-16s (%d 句声纹)" % (sid, spk, name, n))
            if dry:
                return 0
            done = 0
            for sid, spk, name in will:
                out = voiceprint.enroll_speaker(conn, sid, spk, name)
                if out.get("ok"):
                    done += 1
                    print("  + %s → %s（%d 句，新增 %d 条原型）"
                          % (name, out["person_id"], out["utterances"], out["added"]))
                else:
                    print("  ! %s：%s" % (name, out.get("reason")))
            print("注册完成 %d 个" % done)

        lib = store.list_persons(conn)
        print("\n底库现在有 %d 人" % len(lib))
        for p in lib[:20]:
            print("  %-16s %d 条原型  %d 场" % (p["name"], p["n_protos"], p["n_sessions"]))

        out = voiceprint.match_all(conn)
        print("\n复查完成：%d 场有命中" % out["sessions_with_match"])
        for d in out["detail"]:
            if d.get("error"):
                print("  ! %s %s" % (d["session_id"], d["error"]))
            elif d.get("matched"):
                print("  %s 命中 %d 人" % (d["session_id"], d["matched"]))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
