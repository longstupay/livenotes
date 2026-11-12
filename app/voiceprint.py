# -*- coding: utf-8 -*-
"""跨场次的「认人」：把声纹和人绑定，再拿它去每一场里复查。

和 app/recluster.py 的分工要说清楚，否则很容易改乱：
  recluster.py  只解决「这一场里有几个人」——纯聚类，不知道名字，出 SPK_00/01
  voiceprint.py 解决「这一场里的 SPK_00 是不是张三」——有底库，跨场次

三个刻意的设计选择，都是在实测数据上被教育出来的（见 docs/05-voiceprint.md）：

1. 打分不是裸余弦，而是「减去本场背景」。
   同一个直播间的设备/BGM/码率会把所有人的向量一起往一个方向拉，实测跨场次
   互不相同的两个人裸余弦能到 0.4~0.6，直接卡 0.45 必然全错。减去本场其它簇
   的平均相似度，等于把「这个房间的底色」扣掉。

2. 比的是「簇」不是「句」。
   单句 1~2 秒的声纹方差极大（实测同一人句子两两余弦中位数只有 0.59），
   一簇几十句取鲁棒质心之后才稳。所以这里只吃已经分好人的簇。

3. 阈值分两档，且默认偏保守。
   自动认定（auto）要求足够高，宁可漏；中间地带一律给「疑似」交给人点一下。
   宁可不报，也不能天天报错——报错三次用户就不信这个功能了。
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from typing import Any

import numpy as np

from . import logs
from . import store

# ---- 阈值（全部可用环境变量覆盖，方便用真实数据校准后固化）
# 归一化分 = 裸余弦 - 本场背景。
# 这两个数是拿本项目真实库跑出来的（见 tools/calibrate_voiceprint.py）：
#   已知是本人 28 对，中位 0.51；已知不是本人 168 对，中位 -0.05，p95 0.14，p99 0.23。
# 取 0.30 意味着「非本人误报约 1%」，取 0.12 作候选线把 95% 分位的噪声挡在外面。
# 上线后拿自己的确认/否决数据重跑校准脚本即可改这两个环境变量，不用改代码。
AUTO_THRESHOLD = float(os.environ.get("LIVENOTES_VP_AUTO", "0.30"))
CAND_THRESHOLD = float(os.environ.get("LIVENOTES_VP_CAND", "0.12"))
# 自动认定的额外门槛：太短的簇就算分数高也只给「疑似」，避免 3 句话就下结论
AUTO_MIN_SECONDS = float(os.environ.get("LIVENOTES_VP_AUTO_MIN_S", "20"))
# 裸余弦下限。只有归一化分会出问题：某一场的簇彼此都不像时 bg 接近 0，
# 一个「谁都不太像」的 0.35 也能凑到 0.30 分。实测真正确定的那几对裸余弦都在
# 0.77 以上，模糊的都在 0.4 附近，中间是一道很宽的沟，加个下限就干净了。
AUTO_MIN_RAW = float(os.environ.get("LIVENOTES_VP_AUTO_MIN_RAW", "0.55"))
# 身份的判定权在人手里：同名的两次标注 = 同一个人，一定合并。
# 但「往底库里追加原型」是另一回事——声纹差太多时不追加，否则一次手滑就把整个
# 人的原型集污染成「一听就像所有人」。实测踩过：两个不同的人被同名合并后，
# 那个人的原型覆盖了两个音色，之后每一场都能误命中 0.6 分。
ATTACH_THRESHOLD = float(os.environ.get("LIVENOTES_VP_ATTACH", "0.45"))

MIN_UTTS = 3               # 少于 3 句的簇不参与自动比对
MIN_SECONDS = 5.0          # 累计发言不足 5 秒的簇不参与自动比对
MAX_PROTOS = 8             # 每人最多留 8 条原型
ENROLL_MIN_UTTS = 2        # 人工点名时，2 句就够注册（人已经确认过了，容忍度高）
ENROLL_MIN_SECONDS = 2.0
SKIP_DUP_PROTO = 0.95      # 和已有原型像到这个程度就不重复存

# 「主播 / 嘉宾2 / 说话人1 / SPK_03」这类是位置标签，不是人名。
# 拿它当身份会把不同场次的两个人合成一个，必须挡掉。
GENERIC_NAMES = {"主播", "嘉宾", "运营", "说话人", "未知", "路人", "观众",
                 "网友", "连麦", "用户", "有人", "未识别"}


def is_generic_name(name: str) -> bool:
    import re
    n = (name or "").strip()
    if not n:
        return True
    if n in GENERIC_NAMES:
        return True
    if re.fullmatch(r"(嘉宾|说话人|用户|观众)\s*\d+", n):
        return True
    if re.fullmatch(r"SPK[_-]?\d+", n, re.I):
        return True
    return False


# ------------------------------------------------------------------ 向量工具

def _mat(blobs: list[tuple[bytes, int]]) -> np.ndarray:
    dim = blobs[0][1]
    M = np.frombuffer(b"".join(b for b, _ in blobs), dtype="<f2").astype(np.float32)
    return _norm(M.reshape(len(blobs), dim))


def _norm(M: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(M, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return M / n


def robust_centroid(M: np.ndarray, rounds: int = 2) -> np.ndarray:
    """按中位相似度剔掉离群句再取均值。

    直接用算术平均有个隐患：一簇里混进两三句别人的话（抢话/回音），质心就被拉偏，
    整簇从此对不上任何人。这里迭代两次「只保留比中位更像的那些句子」。
    """
    c = _norm(M.mean(axis=0))
    for _ in range(rounds):
        sims = M @ c
        keep = sims >= float(np.median(sims))
        if int(keep.sum()) < 2:
            break
        c = _norm(M[keep].mean(axis=0))
    return c


def pick_protos(M: np.ndarray, k: int = 5) -> list[np.ndarray]:
    """挑几条最能代表这个人的样本：先要像质心，再要求彼此不像（覆盖不同语气/工况）。"""
    c = robust_centroid(M)
    order = np.argsort(-(M @ c))
    out: list[np.ndarray] = []
    for i in order:
        v = M[int(i)]
        if out and max(float(v @ p) for p in out) >= SKIP_DUP_PROTO:
            continue
        out.append(v)
        if len(out) >= k:
            break
    return out or [c]


def _new_person_id(conn: sqlite3.Connection) -> str:
    for _ in range(20):
        pid = "per_" + secrets.token_hex(3)
        if not conn.execute("SELECT 1 FROM persons WHERE person_id=?", (pid,)).fetchone():
            return pid
    raise RuntimeError("生成人物 id 失败")


# ------------------------------------------------------------------ 注册

def enroll_speaker(conn: sqlite3.Connection, sid: str, speaker_id: str, name: str,
                   person_id: str | None = None, force_new: bool = False) -> dict[str, Any]:
    """把「这一场里的这个簇」注册成某个真人。

    这是整个库的唯一入口——不需要单独录一段、不需要人对着麦念数字。
    你在界面上把 SPK_02 改名叫「老王」的那一下，就是注册动作；
    同名的人再标一次，会自动并进同一个人（声纹够像的话）。
    """
    name = (name or "").strip()[:40]
    if not name:
        return {"ok": False, "reason": "名字为空"}
    if is_generic_name(name):
        return {"ok": False, "reason": "「%s」是位置标签不是人名，不入底库" % name}

    rows = {int(r["id"]): float(r["dur"] or 0.0) for r in conn.execute(
        "SELECT id, t_end-t_start AS dur FROM utterances WHERE session_id=? AND speaker_id=?",
        (sid, speaker_id))}
    embs, total = [], 0.0
    for i, b, d in store.load_embeddings(conn, sid):
        if i in rows:
            embs.append((b, d))
            total += rows[i]
    if len(embs) < ENROLL_MIN_UTTS:
        return {"ok": False, "reason": "这一簇只有 %d 句带声纹，不够注册" % len(embs)}
    if total < ENROLL_MIN_SECONDS:
        return {"ok": False, "reason": "累计发言太短（%.1f 秒），声纹不可信" % total}

    M = _mat(embs)
    centroid = robust_centroid(M)

    explicit = bool(person_id)      # 人点名要这个人：直接采信，不再问声纹像不像
    target = person_id
    created = False
    best = -1.0
    if not target:
        same = [dict(r) for r in conn.execute(
            "SELECT person_id,name FROM persons WHERE name=?", (name,))]
        for p in same:
            protos = store.person_protos(conn, p["person_id"])
            if not protos:
                continue
            P = _mat([(b, d) for _, d, b, _q in protos])
            s = _similarity(P, centroid)
            if s > best:
                best, target = s, p["person_id"]
        if target and force_new:
            target, created = None, True
        if not target:
            target, created = _new_person_id(conn), True
        if created and name in {r["name"] for r in
                                conn.execute("SELECT name FROM persons WHERE name=?", (name,))}:
            name = (name + "·2")[:40]
    store.upsert_person(conn, target, name)
    # 一人一场只占一个簇：确认/注册时把同场其它簇上的同一个人清掉。
    # 放在这里而不是每个 API 里，rename / confirm / backfill 三条入口就都覆盖了。
    cleared = store.clear_person_claims(conn, sid, target, speaker_id)

    have = store.person_protos(conn, target)
    P = _mat([(b, d) for _, d, b, _q in have]) if have else None
    if P is not None and best < 0 and not created:
        best = _similarity(P, centroid)           # 显式确认时也算一个分，回报用
    quality = min(1.0, max(0.2, total / 60.0))     # 说满一分钟就算满可信
    items = []
    # 按名字自动挂到某个人、且新簇跟它像得不够 → 只建立身份关联，不往底库塞样本。
    # 人工点名确认（explicit）不受这个限制：人都说了是，就该学进去。
    too_far = (not explicit) and P is not None and best < ATTACH_THRESHOLD
    if too_far:
        logs.log_event("voiceprint.too_far", level="warn", name=name,
                       best=round(float(best), 2), threshold=ATTACH_THRESHOLD)
    for v in ([] if too_far else pick_protos(M)):
        if P is not None and float((P @ v).max()) >= SKIP_DUP_PROTO:
            continue
        items.append((target, sid, speaker_id, int(v.shape[0]),
                      np.float16(v).tobytes(), quality))
    if items:
        store.add_person_emb(conn, items)
    store.trim_person_emb(conn, target, MAX_PROTOS)
    store.set_speaker_person(conn, sid, speaker_id, target, None, None, "confirmed")
    return {"ok": True, "person_id": target, "name": name, "created": created,
            "cleared": cleared,
            "added": len(items), "utterances": len(embs), "seconds": round(total, 1),
            "score": round(best, 3) if not created else None}


# ------------------------------------------------------------------ 检索

def _session_clusters(conn: sqlite3.Connection, sid: str) -> list[dict[str, Any]]:
    rows = {int(r["id"]): dict(r) for r in conn.execute(
        "SELECT id, speaker_id, speaker_label, t_start, t_end FROM utterances WHERE session_id=?",
        (sid,))}
    bucket: dict[str, dict] = {}
    for uid, blob, dim in store.load_embeddings(conn, sid):
        u = rows.get(uid)
        if not u:
            continue
        g = bucket.setdefault(u["speaker_id"], {"label": u["speaker_label"], "items": []})
        g["items"].append((blob, dim, float(u["t_end"]) - float(u["t_start"])))
    out = []
    for spk, g in bucket.items():
        M = _mat([(b, d) for b, d, _ in g["items"]])
        out.append({
            "speaker_id": spk,
            "label": g["label"],
            "n": len(g["items"]),
            "seconds": sum(t for _, _, t in g["items"]),
            "M": M,
            "centroid": robust_centroid(M),
        })
    return out


def _library(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    names = {r["person_id"]: r["name"] for r in conn.execute("SELECT person_id,name FROM persons")}
    bucket: dict[str, list] = {}
    for pid, dim, blob, quality in store.person_protos(conn):
        if pid in names:
            bucket.setdefault(pid, []).append((blob, dim))
    out = {}
    for pid, items in bucket.items():
        out[pid] = {"person_id": pid, "name": names[pid], "P": _mat(items), "n_protos": len(items)}
    return out


def explain(conn: sqlite3.Connection, sid: str, speaker_id: str,
            top: int = 5) -> list[dict[str, Any]]:
    """某一场某一簇，和底库每个人的分数（含背景）。给「为什么是这个人」用。"""
    clusters = {c["speaker_id"]: c for c in _session_clusters(conn, sid)}
    c = clusters.get(speaker_id)
    if c is None:
        return []
    bg = _background(c, [x for x in clusters.values() if x["speaker_id"] != speaker_id])
    lib = _library(conn)
    out = []
    for pid, p in lib.items():
        raw = _similarity(p["P"], c["centroid"])
        out.append({"person_id": pid, "name": p["name"], "raw": round(raw, 3),
                    "bg": round(bg, 3), "score": round(raw - bg, 3),
                    "n_protos": p["n_protos"]})
    out.sort(key=lambda d: -d["score"])
    return out[:top]


def identify_session(conn: sqlite3.Connection, sid: str, top: int = 3,
                     min_utts: int = MIN_UTTS,
                     min_seconds: float = MIN_SECONDS) -> dict[str, dict[str, Any]]:
    """只读打分：给实时链路用，**不写库、不占座、不改 speakers**。

    和 match_session 的分工：
      match_session   结束后的权威复算：会清空/写回 person_state，处理人工确认与否决
      identify_session 直播中的「疑似」：只回答"现在这个簇最像谁"，调用方自己决定
                       要不要展示、要不要等下一次确认

    直播每 20 秒调一次，不能像 match_session 那样动数据库——那会把人工确认、
    后续复算和实时展示搅在一起，而且一次手滑就会写进底库。
    """
    lib = _library(conn)
    clusters = _session_clusters(conn, sid)
    out: dict[str, dict[str, Any]] = {}
    if not lib or not clusters:
        return out
    for c in clusters:
        item: dict[str, Any] = {
            "speaker_id": c["speaker_id"], "label": c["label"],
            "n": c["n"], "seconds": round(c["seconds"], 1),
            "ready": c["n"] >= min_utts and c["seconds"] >= min_seconds,
            "candidates": [],
        }
        if not item["ready"]:
            out[c["speaker_id"]] = item
            continue
        bg = _background(c, [x for x in clusters if x["speaker_id"] != c["speaker_id"]])
        scored = []
        for pid, p in lib.items():
            raw = _similarity(p["P"], c["centroid"])
            scored.append({"person_id": pid, "name": p["name"],
                           "raw": round(raw, 4), "score": round(raw - bg, 4)})
        scored.sort(key=lambda d: -d["score"])
        item["bg"] = round(bg, 4)
        item["candidates"] = scored[:top]
        out[c["speaker_id"]] = item
    return out


def _similarity(protos: np.ndarray, centroid: np.ndarray) -> float:
    """和一个人比：取最像的两条原型求平均。

    为什么不用最大值：原型条数多的人天然占便宜（8 条里总有一条撞上），实测会让
    常驻主播把每一场都「吃」掉。取前二平均后，非本人的分数分布明显收窄。
    """
    s = protos @ centroid
    k = min(2, int(s.size))
    return float(np.sort(s)[-k:].mean())


def _background(target: dict, others: list[dict]) -> float:
    """本场的「底色」：这一簇和其它簇的平均相似度。

    其它簇太少时这个估计不可信（一个人说话的场子根本没有背景），
    此时退回一个保守常数——宁可把分数压下来漏报，也不要胡说。
    """
    sims = [float(target["centroid"] @ o["centroid"]) for o in others if o["n"] >= MIN_UTTS]
    if not sims:                       # 全场只有一个人说话，没有参照系
        return 0.35
    return float(np.mean(sims))


def match_session(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    """拿整场的簇去底库查一遍。返回写回了几条、以及每个簇的判定。"""
    lib = _library(conn)
    if not lib:
        return {"ok": False, "reason": "底库还是空的：先在任意一场里把说话人改成真实昵称",
                "matched": 0, "rows": []}
    clusters = _session_clusters(conn, sid)
    if not clusters:
        return {"ok": False, "reason": "这一场没有声纹数据", "matched": 0, "rows": []}

    store.clear_session_persons(conn, sid)
    # 历史遗留的「一人多簇」：同一场里同一个人被 confirmed 到两个簇上（人工点过两次）。
    # 重跑检索时只保留分高的那个，其余清掉——否则"一人一座"只对新判定生效，旧脏数据永远在。
    for d in conn.execute(
            "SELECT person_id FROM speakers WHERE session_id=? AND person_id IS NOT NULL"
            " AND person_state='confirmed' GROUP BY person_id HAVING COUNT(*)>1",
            (sid,)).fetchall():
        keep = conn.execute(
            "SELECT speaker_id FROM speakers WHERE session_id=? AND person_id=?"
            " AND person_state='confirmed' ORDER BY COALESCE(person_score,0) DESC, speaker_id"
            " LIMIT 1", (sid, d["person_id"])).fetchone()
        if keep:
            store.clear_person_claims(conn, sid, d["person_id"], keep["speaker_id"])
    locked = {r["speaker_id"]: r["person_state"] for r in conn.execute(
        "SELECT speaker_id, person_state FROM speakers WHERE session_id=?", (sid,))}
    cands = []
    for c in clusters:
        c["locked"] = locked.get(c["speaker_id"], "") in ("confirmed", "rejected")
        if c["n"] < MIN_UTTS or c["seconds"] < MIN_SECONDS:
            c["skip"] = "样本太少（%d 句 / %.1f 秒）" % (c["n"], c["seconds"])
            continue
        if c["locked"]:            # 人工已经定性过，别再自动改
            c["skip"] = "已人工%s" % ("确认" if locked.get(c["speaker_id"]) == "confirmed" else "否决")
            continue
        bg = _background(c, [x for x in clusters if x["speaker_id"] != c["speaker_id"]])
        best_pid, best_score, best_raw = None, -9.9, 0.0
        for pid, p in lib.items():
            raw = _similarity(p["P"], c["centroid"])
            s = raw - bg
            if s > best_score:
                best_pid, best_score, best_raw = pid, s, raw
        strong = (best_score >= AUTO_THRESHOLD and best_raw >= AUTO_MIN_RAW
                  and c["seconds"] >= AUTO_MIN_SECONDS)
        c.update({"bg": bg, "raw": best_raw, "score": best_score,
                  "person_id": best_pid if best_score >= CAND_THRESHOLD else None,
                  "state": "auto" if strong else
                           ("cand" if best_score >= CAND_THRESHOLD else "")})
        cands.append(c)

    # 一个人在一场里只能占一个簇：按分数从高到低抢座位，避免同一人被认成两处。
    # 人工钉死的先占座，自动判定不能从它手里再抢一个簇。
    pinned_ids = {r["speaker_id"]: dict(r) for r in conn.execute(
        "SELECT speaker_id, person_id, person_score, person_state FROM speakers "
        "WHERE session_id=? AND person_id IS NOT NULL AND person_state='confirmed'", (sid,))}
    used: set[str] = {p["person_id"] for p in pinned_ids.values()}
    rows = []
    for c in sorted([x for x in cands if x["person_id"]], key=lambda d: -d["score"]):
        if c["person_id"] in used:
            c["person_id"], c["state"] = None, ""
            c["note"] = "同一人在本场已占用另一个簇"
        else:
            used.add(c["person_id"])
    # 人工钉死的行不改，但要原样报出来，否则页面上会显示成「没认出来」
    pinned = pinned_ids
    for c in clusters:
        pid, state = c.get("person_id"), c.get("state", "") or ""
        score, bg = float(c.get("score", 0.0)), float(c.get("bg", 0.0))
        if not pid and c["speaker_id"] in pinned:
            p = pinned[c["speaker_id"]]
            pid, state = p["person_id"], "confirmed"
            score = float(p["person_score"] or 0.0)
        if pid:
            if not (c.get("locked") and c["speaker_id"] in pinned):
                store.set_speaker_person(conn, sid, c["speaker_id"], pid,
                                         round(score, 4), round(bg, 4),
                                         "auto" if state == "auto" else "cand")
        rows.append({"speaker_id": c["speaker_id"], "track": c.get("label", ""),
                     "person_id": pid, "name": lib[pid]["name"] if pid in lib else "",
                     "state": state,
                     "score": round(score, 3),
                     "raw": round(float(c.get("raw", 0.0)), 3),
                     "bg": round(bg, 3),
                     "n": c["n"], "seconds": round(c["seconds"], 1),
                     "skip": c.get("skip", ""), "note": c.get("note", "")})
    return {"ok": True, "matched": sum(1 for r in rows if r["person_id"]),
            "auto": sum(1 for r in rows if r["state"] == "auto"),
            "rows": rows,
            "thresholds": {"auto": AUTO_THRESHOLD, "candidate": CAND_THRESHOLD}}


def match_all(conn: sqlite3.Connection) -> dict[str, Any]:
    """重跑所有有录音的场次。补注册完底库之后用一次即可。"""
    sids = [r["id"] for r in conn.execute(
        "SELECT id FROM sessions WHERE id NOT LIKE 'DEMO%' ORDER BY started_at")]
    done = 0
    detail = []
    for sid in sids:
        try:
            out = match_session(conn, sid)
            if out.get("ok") and out.get("matched"):
                done += 1
                detail.append({"session_id": sid, "matched": out["matched"]})
        except Exception as e:      # 单场失败不能拖垮整批
            detail.append({"session_id": sid, "error": str(e)})
    return {"ok": True, "sessions_with_match": done, "detail": detail}
