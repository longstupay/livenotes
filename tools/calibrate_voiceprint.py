# -*- coding: utf-8 -*-
"""用你自己的库校准两个阈值（换设备、换场景、人变多之后都该重跑一次）。

    python tools/calibrate_voiceprint.py

它做两件事：

1. 把每个「已知是本人」的簇（人工确认过的）和「已知不是」的簇分别打分，打印分布。
   判据看两条：AUTO 要卡在「非本人」的 p99 之上，CAND 卡在 p95 附近。
2. 直接给出建议值，可以写进环境变量 LIVENOTES_VP_AUTO / LIVENOTES_VP_CAND。

注意：样本量少的时候（<20 对）这组数字只能当量级参考，别为了小数点后两位较劲。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app import store, voiceprint  # noqa: E402


def main() -> int:
    conn = store.connect()
    try:
        lib = voiceprint._library(conn)
        if not lib:
            print("底库是空的，先标几个人再来校准")
            return 1
        own = {(r["session_id"], r["speaker_id"]): r["person_id"] for r in conn.execute(
            "SELECT session_id, speaker_id, person_id FROM speakers "
            "WHERE person_id IS NOT NULL")}
        sids = [r["id"] for r in conn.execute("SELECT id FROM sessions")]
        same, other = [], []
        for sid in sids:
            clusters = voiceprint._session_clusters(conn, sid)
            for c in clusters:
                if c["n"] < voiceprint.MIN_UTTS or c["seconds"] < voiceprint.MIN_SECONDS:
                    continue
                bg = voiceprint._background(
                    c, [o for o in clusters if o["speaker_id"] != c["speaker_id"]])
                for pid, p in lib.items():
                    raw = voiceprint._similarity(p["P"], c["centroid"])
                    (same if own.get((sid, c["speaker_id"])) == pid else other).append(
                        (raw - bg, raw))
        if not same or not other:
            print("样本不足：本人 %d 对、非本人 %d 对。多确认几个人/几场再跑。"
                  % (len(same), len(other)))
            return 1
        sv = np.array([s for s, _ in same])
        ov = np.array([s for s, _ in other])
        sr = np.array([r for _, r in same])
        orr = np.array([r for _, r in other])
        print("本人 %d 对    得分 中位 %.3f  p10 %.3f   裸余弦 中位 %.3f"
              % (len(sv), np.median(sv), np.percentile(sv, 10), np.median(sr)))
        print("非本人 %d 对  得分 中位 %.3f  p95 %.3f  p99 %.3f   裸余弦 p99 %.3f"
              % (len(ov), np.median(ov), np.percentile(ov, 95), np.percentile(ov, 99),
                 np.percentile(orr, 99)))
        auto = round(float(max(np.percentile(ov, 99) + 0.02, 0.10)), 2)
        cand = round(float(max(np.percentile(ov, 95), 0.05)), 2)
        raw_floor = round(float(max(np.percentile(orr, 99) + 0.02, 0.40)), 2)
        print("\n建议：")
        print("  LIVENOTES_VP_AUTO=%.2f   （非本人 p99 之上）" % auto)
        print("  LIVENOTES_VP_CAND=%.2f   （非本人 p95 附近）" % cand)
        print("  LIVENOTES_VP_AUTO_MIN_RAW=%.2f （裸余弦下限，挡掉「谁都不像」的场次）"
              % raw_floor)
        print("\n当前生效：auto=%.2f cand=%.2f raw=%.2f"
              % (voiceprint.AUTO_THRESHOLD, voiceprint.CAND_THRESHOLD,
                 voiceprint.AUTO_MIN_RAW))
        print("\n提醒：这里的「非本人」里混着聚类拆出来的同一个人（标签噪声），"
              "所以真实误报率会低于上面这条线。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
