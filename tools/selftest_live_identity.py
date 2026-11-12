# -*- coding: utf-8 -*-
"""直播中「疑似认人 / 飞书说话人」的离线自测。

    python tools/selftest_live_identity.py

跑在数据库副本上，不加载 ASR/VAD 模型，也不发飞书。覆盖：
  1. identify_session 只读：不写 speakers / person_emb
  2. 疑似→确认的稳定性规则（首次疑似、连续两次才去掉问号）
  3. 一人一场只占一个簇；人工改过名的簇不参与自动认人
  4. 飞书文案：人名 / 人名（疑似 0.68） / 嘉宾1（识别中）
  5. 接进 LiveSession 后 chip 用名字、位置标签仍保留
  6. recluster 后 reader / SRT / Markdown 显示「主播 · 火火（疑似）」
  7. 收工回顾（文本 + 卡片）也带身份
  8. 「不是他」只清 hint / 落 rejected，不注册
  9. 定时刷新后 chip 动态取名（不依赖下一条句子）
 10. 确认/注册时同场同人只留一个簇
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import export, live, reader, store, voiceprint  # noqa: E402

SID = "LIVE_20261105_043037"
FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def _fake(pid: str, name: str, score: float, raw: float,
          seconds: float = 40.0) -> dict:
    return {"ready": True, "seconds": seconds, "n": 20,
            "candidates": [{"person_id": pid, "name": name,
                            "score": score, "raw": raw}]}


def main() -> int:
    src = Path(store.DB_PATH)
    if not src.exists():
        print("没有 %s，先在主程序里录一场" % src)
        return 1
    tmpdir = ROOT / ".tmp"
    tmpdir.mkdir(exist_ok=True)
    db = tmpdir / "live_identity_selftest.db"
    for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        try:
            p.unlink()
        except OSError:
            pass
    shutil.copy(src, db)
    for suf in ("-wal", "-shm"):
        p = Path(str(src) + suf)
        if p.exists():
            shutil.copy(p, Path(str(db) + suf))
    store.DB_PATH = db
    conn = store.connect()
    store.init_db(conn)
    try:
        if not store.list_persons(conn):
            print("底库是空的：先跑 python tools/backfill_persons.py")
            return 1

        print("1) identify_session 只读")
        before_spk = [tuple(r) for r in conn.execute(
            "SELECT session_id,speaker_id,person_id,person_state,person_score,"
            "person_bg,display_name,verified FROM speakers ORDER BY session_id,speaker_id")]
        before_emb = store.count_person_emb(conn, store.list_persons(conn)[0]["person_id"])
        info = voiceprint.identify_session(conn, SID, top=1, min_seconds=5)
        after_spk = [tuple(r) for r in conn.execute(
            "SELECT session_id,speaker_id,person_id,person_state,person_score,"
            "person_bg,display_name,verified FROM speakers ORDER BY session_id,speaker_id")]
        after_emb = store.count_person_emb(conn, store.list_persons(conn)[0]["person_id"])
        check(bool(info) and any(x.get("candidates") for x in info.values()),
              "返回候选：%d 个簇" % len(info))
        check(before_spk == after_spk and before_emb == after_emb,
              "没有写 speakers / person_emb")
        top = (info.get("SPK_01") or {}).get("candidates") or [{}]
        check(top[0].get("name") == "天外飞仙",
              "已知簇 top1 = %s（%.2f）" % (top[0].get("name"), top[0].get("score") or 0))

        print("2) 疑似 → 确认")
        kw = dict(auto_score=0.30, auto_raw=0.55, auto_seconds=30,
                  confirm_n=2, cand_score=0.12)
        info1 = {"SPK_00": _fake("p1", "阿哲", 0.50, 0.80)}
        h1 = live._resolve_live_hints(info1, {}, {}, **kw)
        check(h1.get("SPK_00", {}).get("state") == "cand"
              and h1["SPK_00"]["streak"] == 1, "第一次只给疑似")
        h2 = live._resolve_live_hints(info1, h1, {}, **kw)
        check(h2.get("SPK_00", {}).get("state") == "auto"
              and h2["SPK_00"]["streak"] == 2, "连续两次同一人才去掉疑似")

        print("3) 占座与人工优先")
        collision = {"SPK_00": _fake("p1", "阿哲", 0.50, 0.80),
                     "SPK_01": _fake("p1", "阿哲", 0.40, 0.70)}
        hc = live._resolve_live_hints(collision, {}, {}, **kw)
        check("SPK_00" in hc and "SPK_01" not in hc, "同一人只占分数高的簇")
        hm = live._resolve_live_hints(info1, {}, {"SPK_00": "阿哲"}, **kw)
        check("SPK_00" not in hm, "人工改过名的簇不参与自动认人")

        print("4) 飞书文案")
        s = live.LiveSession("selftest")
        s.manual = {"SPK_00": "老王"}
        s.person_hint = {"SPK_01": {"name": "阿哲", "state": "cand",
                                    "score": 0.42}}
        s.pos_labels = {"SPK_01": "嘉宾1", "SPK_02": "嘉宾2"}
        ev = {"speaker_id": "SPK_00", "speaker_label": "主播"}
        s._apply_identity(ev)
        check(live._push_label(ev) == "老王", "人工名：%s" % live._push_label(ev))
        ev = {"speaker_id": "SPK_01", "speaker_label": "嘉宾1"}
        s._apply_identity(ev)
        check(live._push_label(ev) == "阿哲（疑似 0.42）",
              "疑似：%s" % live._push_label(ev))
        ev = {"speaker_id": "SPK_02", "speaker_label": "嘉宾2"}
        s._apply_identity(ev)
        check(live._push_label(ev) == "嘉宾2（识别中）",
              "未识别：%s" % live._push_label(ev))
        check(live._push_label({"speaker_label": ""}) == "未识别说话人",
              "空标签兜底：%s" % live._push_label({"speaker_label": ""}))
        check(live._push_label({"speaker_label": "", "speaker_state": "unknown"})
              == "未识别说话人（识别中）", "空标签 + 未知状态")

        print("5) 接进 LiveSession")
        class _C:
            def __init__(self, ids):
                self.ids = ids
                self.seconds = {i: 60.0 for i in ids}
                self.counts = {i: 20 for i in ids}

            def order(self):
                return list(self.ids)

        s2 = live.LiveSession("selftest-integration")
        s2.id = SID
        s2.running = True
        s2.clusters = _C(["SPK_00", "SPK_01", "SPK_02"])
        s2._hint_at = 0.0
        s2._refresh_hints(conn)
        h = s2.person_hint.get("SPK_01") or {}
        check(h.get("name") == "天外飞仙" and h.get("state") == "cand",
              "第一次：%s / %s" % (h.get("name"), h.get("state")))
        s2._refresh_labels(conn, "SPK_01")
        check(s2.labels.get("SPK_01") == "天外飞仙"
              and (s2.pos_labels.get("SPK_01") or "").startswith("嘉宾"),
              "chip 用名字，位置标签仍保留：%s / %s"
              % (s2.labels.get("SPK_01"), s2.pos_labels.get("SPK_01")))
        s2._hint_at = 0.0
        s2._refresh_hints(conn)
        h = s2.person_hint.get("SPK_01") or {}
        check(h.get("state") == "auto", "第二次去掉疑似：%s" % h.get("state"))
        ev = {"speaker_id": "SPK_01", "speaker_label": "嘉宾"}
        s2._apply_identity(ev)
        check(live._push_label(ev) == "天外飞仙",
              "飞书用上名字：%s" % live._push_label(ev))

        print("6) 逐字稿/导出带身份")
        rid = "LIVE_20261105_025257"
        r = reader.read(conn, rid)
        labels = {p["speaker_id"]: p["label"] for p in r["speakers"]}
        check(labels.get("SPK_01") == "主播 · 火火（疑似）",
              "reader 说话人：%s" % labels.get("SPK_01"))
        srt = export.to_srt(conn, rid)
        check("主播 · 火火（疑似）：" in srt, "SRT 带身份")
        md = export.to_markdown(conn, rid)
        check("| 主播 · 火火（疑似） |" in md, "Markdown 说话人表带身份")

        print("7) 收工回顾带身份")
        sess = {"id": "LIVE_TEST", "title": "T",
                "started_at": "2026-11-05T18:00:00",
                "started_utc": "2026-11-05T10:00:00+00:00",
                "tz_offset_min": 480}
        ev = {"speaker_id": "SPK_01", "speaker_label": "主播",
              "text": "这个项目下个月启动", "peak_t": 10.0}
        txt = live._digest_text(sess, [ev], 1, 0, {"SPK_01": ("火火", "cand")})
        check("主播 · 火火（疑似）" in txt, "回顾文本带身份")
        card = live._digest_card(sess, [ev], 1, 0, {"SPK_01": ("火火", "cand")})
        check("主播 · 火火（疑似）" in str(card), "回顾卡片带身份")

        print("8) 不是他：只清判断，不注册")
        s3 = live.LiveSession("reject")
        s3.id = "LIVE_20261105_025257"
        s3.pos_labels = {"SPK_01": "主播"}
        s3.labels = {"SPK_01": "火火"}
        s3.person_hint = {"SPK_01": {"person_id": "p", "name": "火火",
                                     "state": "cand", "score": 0.4, "streak": 1}}
        live._SESSIONS[s3.id] = s3
        try:
            out = live.reject_hint(s3.id, "SPK_01")
            row = conn.execute(
                "SELECT person_state FROM speakers WHERE session_id=? AND speaker_id=?",
                (s3.id, "SPK_01")).fetchone()
            check(out.get("label") == "主播" and "SPK_01" not in s3.person_hint
                  and "SPK_01" in s3.rejected and row["person_state"] == "rejected",
                  "清 hint + 落 rejected，不注册")
        finally:
            live._SESSIONS.pop(s3.id, None)

        print("9) 定时刷新后 chip 动态取名")
        s4 = live.LiveSession("status")
        s4.id = "LIVE_TEST_STATUS"
        s4.clusters = _C(["SPK_00"])
        s4.pos_labels = {"SPK_00": "主播"}
        s4.person_hint = {"SPK_00": {"name": "阿哲", "state": "cand",
                                     "score": 0.4}}
        st = s4.status()
        check(st["speakers"][0]["label"] == "阿哲"
              and st["speakers"][0]["person_state"] == "cand",
              "没走 _refresh_labels 也能显示名字：%s" % st["speakers"][0]["label"])

        print("10) 同场同人只留一个簇")
        p = conn.execute("SELECT person_id FROM persons WHERE name='天外飞仙'").fetchone()
        out = voiceprint.enroll_speaker(conn, SID, "SPK_01", "天外飞仙",
                                        person_id=p["person_id"])
        row = conn.execute(
            "SELECT person_id, person_state FROM speakers WHERE session_id=? AND speaker_id=?",
            (SID, "SPK_02")).fetchone()
        check("SPK_02" in out.get("cleared", []) and row["person_id"] is None
              and row["person_state"] == "", "重复的 SPK_02 被清掉")
        row = conn.execute(
            "SELECT person_id, person_state FROM speakers WHERE session_id=? AND speaker_id=?",
            (SID, "SPK_01")).fetchone()
        check(row["person_id"] == p["person_id"] and row["person_state"] == "confirmed",
              "保留的 SPK_01 落 confirmed")
    finally:
        conn.close()
        for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
            try:
                p.unlink()
            except OSError:
                pass

    print()
    print("全部通过" if not FAILED else "失败 %d 项" % len(FAILED))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
