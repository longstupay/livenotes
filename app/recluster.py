# -*- coding: utf-8 -*-
"""结束后全量重聚类：把一整场的声纹重新分成 k 个人。

为什么不继续用在线贪心：实时链路必须"来一句判一句"，只能拿固定阈值去比，
一旦第 4 个人出现、或 BGM 把相似度整体压低，就再也纠不回来。这一层拿全场的
embedding 一起看，用轮廓系数选 K，能纠正整场，也能反复重跑。

算法选型：
  - 球面 k-means（余弦）而不是层次聚类：几千句时 O(n·k·iters) 仍是线性量级，
    层次聚类的 O(n^3) 在 20 分钟的直播里会跑到分钟级。
  - 轮廓系数用抽样计算（<=800 个点），避免 n² 的内存。
  - 自动 K 之外还做"小簇吸收"：只有 1~2 句的簇多半是 BGM/抢话噪声，
    并回最近的大簇，否则会凭空多出几个"嘉宾5"。
"""
from __future__ import annotations

import bisect
import sqlite3
from typing import Any

import numpy as np

from . import store

MIN_CLUSTER_SECONDS = 2.0     # 累计发言短于这个时长的簇视为噪声
SIL_SAMPLE = 800              # 轮廓系数抽样点数
KMAX_HARD = 10                # UI 的调色板也是 10 色


def _norm(E: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(E, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return E / n


def kmeans_cos(E: np.ndarray, k: int, iters: int = 40, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """球面 k-means++ 初始化，返回 (labels, centers)。"""
    n = len(E)
    rng = np.random.default_rng(seed)
    centers = [E[rng.integers(n)]]
    dist = 1.0 - E @ centers[0]
    for _ in range(1, k):
        p = np.clip(dist, 0, None)
        total = float(p.sum())
        if total <= 1e-12:
            centers.append(E[rng.integers(n)])
        else:
            centers.append(E[rng.choice(n, p=p / total)])
        dist = np.minimum(dist, 1.0 - E @ centers[-1])
    C = _norm(np.vstack(centers))
    labels = None
    for _ in range(iters):
        new = np.argmax(E @ C.T, axis=1)
        if labels is not None and np.array_equal(new, labels):
            break
        labels = new
        for j in range(len(C)):
            m = labels == j
            if m.any():
                v = E[m].mean(axis=0)
                nv = float(np.linalg.norm(v))
                C[j] = v / (nv or 1.0)
    return labels, C


def merge_tiny(E: np.ndarray, labels: np.ndarray, min_size: int) -> np.ndarray:
    """把过小的簇并到最近的其它簇，直到没有小簇或只剩一个簇。编号最后压实。"""
    labels = labels.copy()
    while True:
        uniq = np.unique(labels)
        if len(uniq) <= 1:
            break
        sizes = {int(u): int((labels == u).sum()) for u in uniq}
        small = [u for u, c in sizes.items() if c < min_size]
        if not small:
            break
        j = min(small, key=lambda u: sizes[u])
        targets = [u for u in uniq if u != j]
        if not targets:
            break
        centers = _norm(np.vstack([E[labels == u].mean(axis=0) for u in targets]))
        pick = np.argmax(E[labels == j] @ centers.T, axis=1)
        labels[labels == j] = np.array([targets[p] for p in pick])
    # 压实编号，保证是 0..k-1 的连续整数（后面按下标取数组）
    remap = {int(u): i for i, u in enumerate(np.unique(labels))}
    return np.array([remap[int(v)] for v in labels], dtype=int)


def silhouette(E: np.ndarray, labels: np.ndarray, seed: int = 0) -> float:
    """抽样轮廓系数；距离用 1-cos。"""
    n = len(E)
    k = int(labels.max()) + 1
    if k < 2:
        return -1.0
    if n > SIL_SAMPLE:
        idx = np.random.default_rng(seed).choice(n, size=SIL_SAMPLE, replace=False)
    else:
        idx = np.arange(n)
    D = 1.0 - E[idx] @ E.T                     # (m, n)
    vals = []
    for r, i in enumerate(idx):
        own = labels == labels[i]
        cnt = int(own.sum())
        if cnt <= 1:
            continue
        a = float(D[r][own].sum() / (cnt - 1))
        b = min(float(D[r][labels == t].mean()) for t in range(k) if t != labels[i])
        vals.append((b - a) / max(a, b, 1e-9))
    return float(np.mean(vals)) if vals else -1.0


def _cluster_ok(E: np.ndarray, labels: np.ndarray, seconds: np.ndarray) -> bool:
    k = int(labels.max()) + 1
    sizes = np.bincount(labels, minlength=k)
    if (sizes < 3).any():
        return False
    for j in range(k):
        if float(seconds[labels == j].sum()) < MIN_CLUSTER_SECONDS:
            return False
    return True


def choose(E: np.ndarray, seconds: np.ndarray, k: int | None = None) -> tuple[np.ndarray, int, float]:
    """返回 (labels, k, silhouette)。k 为 None 时按轮廓系数自动选。"""
    n = len(E)
    min_size = max(3, int(round(n * 0.01)))
    if n < 6:
        return np.zeros(n, dtype=int), 1, 1.0
    if k:
        k = max(1, min(int(k), KMAX_HARD, n))
        labels, _ = kmeans_cos(E, k)
        labels = merge_tiny(E, labels, min_size)   # 小簇被并掉后实际簇数可能少于 k
        return labels, int(labels.max()) + 1, 1.0
    kmax = min(KMAX_HARD, max(2, n // 6))
    best: tuple[float, np.ndarray, int] | None = None
    for kk in range(2, kmax + 1):
        labels, _ = kmeans_cos(E, kk)
        labels = merge_tiny(E, labels, min_size)
        if not _cluster_ok(E, labels, seconds):
            continue
        score = silhouette(E, labels)
        # 分数接近时偏向更少的簇，避免把一个人拆成两个
        if best is None or score > best[0] + 0.01:
            best = (score, labels, int(labels.max()) + 1)
    if best is None:
        labels, _ = kmeans_cos(E, 2)
        labels = merge_tiny(E, labels, min_size)
        return labels, int(labels.max()) + 1, 0.0
    return best[1], best[2], best[0]


def recluster_session(conn: sqlite3.Connection, sid: str, k: int | None = None) -> dict[str, Any]:
    """按已存声纹重新分人并写回数据库。"""
    embs = store.load_embeddings(conn, sid)
    if not embs:
        raise ValueError("本场没有保存声纹数据（旧版本采集的场次），无法重新分人。")

    rows = {int(r["id"]): dict(r) for r in conn.execute(
        "SELECT id,t_start,t_end,speaker_id,speaker_label FROM utterances "
        "WHERE session_id=?", (sid,))}
    utt_ids = [i for i, _, _ in embs if i in rows]
    if len(utt_ids) < 4:
        raise ValueError("本场可用的声纹太少（%d 条），无法重新分人。" % len(utt_ids))

    dim = embs[0][2]
    E = np.frombuffer(b"".join(b for i, b, _ in embs if i in rows), dtype="<f2")
    E = _norm(E.reshape(len(utt_ids), dim).astype(np.float32))
    seconds = np.array([max(0.0, rows[i]["t_end"] - rows[i]["t_start"]) for i in utt_ids])

    labels, nk, score = choose(E, seconds, k)

    # 统计每簇：累计时长、句数、以及"原来主要来自哪个旧说话人"
    old_ids = {i: rows[i]["speaker_id"] for i in utt_ids}
    old_verified = {r["speaker_id"]: r["display_name"] for r in conn.execute(
        "SELECT speaker_id,display_name FROM speakers WHERE session_id=? AND verified=1", (sid,))}
    per: list[dict] = []
    for j in range(nk):
        m = labels == j
        members = [utt_ids[t] for t in np.flatnonzero(m)]
        secs = float(seconds[m].sum())
        hist: dict[str, int] = {}
        for u in members:
            o = old_ids.get(u, "")
            hist[o] = hist.get(o, 0) + 1
        dom = max(hist.items(), key=lambda kv: kv[1])[0] if hist else ""
        share = (hist.get(dom, 0) / len(members)) if members else 0.0
        per.append({"members": members, "seconds": secs, "count": len(members),
                    "dom": dom, "share": share})
    order = sorted(range(nk), key=lambda j: per[j]["seconds"], reverse=True)

    # 编号按发言时长排序：SPK_00 是话最多的（通常就是主播）
    labels_by_cluster: dict[int, tuple[str, str, int]] = {}
    used: set[str] = set()

    # 第一轮：旧的人工昵称按"覆盖最多成员"的簇继承；同一个昵称只能被一个簇拿走，
    # 否则重分之后会出现两个都叫「嘉宾1」的人（真实踩过的坑）。
    cands = sorted(range(nk), key=lambda j: per[j]["count"] *
                   (per[j]["share"] if per[j]["dom"] in old_verified else 0.0), reverse=True)
    name_of: dict[int, str] = {}
    verified_of: dict[int, int] = {}
    for j in cands:
        d, nm = per[j]["dom"], ""
        if d in old_verified and per[j]["share"] >= 0.5:
            nm = old_verified[d]
        if nm and nm not in used:
            name_of[j] = nm
            verified_of[j] = 1
            used.add(nm)

    # 第二轮：其余按发言时长自动命名，跳过已被占用的名字
    guest = 0
    for rank, j in enumerate(order):
        if j in name_of:
            continue
        if rank == 0 and "主播" not in used:
            nm = "主播"
        else:
            # 统一用「嘉宾N」。实时链路里第一个嘉宾叫「嘉宾」，重聚类后如果沿用这个
            # 裸标签，就会出现「嘉宾 / 嘉宾2 / 嘉宾3」这种读起来别扭的混排。
            while True:
                guest += 1
                nm = "嘉宾%d" % guest
                if nm not in used:
                    break
        name_of[j] = nm
        used.add(nm)

    for rank, j in enumerate(order):
        labels_by_cluster[j] = ("SPK_%02d" % rank, name_of[j], verified_of.get(j, 0))

    updates = [(labels_by_cluster[int(labels[t])][0], labels_by_cluster[int(labels[t])][1], uid)
               for t, uid in enumerate(utt_ids)]

    # 没存声纹的短句按时间就近继承，避免留下孤儿标签
    pos_of = {uid: t for t, uid in enumerate(utt_ids)}
    embedded = set(utt_ids)
    orphan = [i for i in rows if i not in embedded]
    if orphan:
        anchor = sorted((rows[i]["t_start"], i) for i in utt_ids)
        times = [a[0] for a in anchor]
        for oid in orphan:
            p = bisect.bisect_left(times, rows[oid]["t_start"])
            near = anchor[min(p, len(anchor) - 1)][1]
            sid_new, lab_new, _ = labels_by_cluster[int(labels[pos_of[near]])]
            updates.append((sid_new, lab_new, oid))

    before: dict[str, int] = {}
    for r in rows.values():
        before[r["speaker_label"]] = before.get(r["speaker_label"], 0) + 1

    store.apply_speakers(conn, sid, updates)
    store.replace_speakers(conn, sid, [(sid_new, lab, ver)
                                       for sid_new, lab, ver in labels_by_cluster.values()])

    # 分完人之后立刻拿底库复查一遍：这一簇是不是以前见过的某个人。
    # 放在这里而不是实时链路，是因为簇级比对需要整场的句子都齐了才准。
    # 失败不影响重聚类结果，只在返回里记一笔。
    vp: dict[str, Any] = {}
    try:
        from . import voiceprint
        vp = voiceprint.match_session(conn, sid)
    except Exception as e:      # 底库为空、numpy 缺失等都不该让「重新分人」整体失败
        vp = {"ok": False, "reason": str(e)}

    speakers = [{"speaker_id": labels_by_cluster[j][0], "label": labels_by_cluster[j][1],
                 "seconds": round(per[j]["seconds"], 1), "count": per[j]["count"]}
                for j in order]
    # 把命中的人名贴回每个说话人，前端一条链路就能显示「嘉宾2 · 老王(疑似)」
    hit = {r["speaker_id"]: r for r in vp.get("rows", []) if r.get("person_id")}
    for sp in speakers:
        h = hit.get(sp["speaker_id"])
        sp["person_id"] = h["person_id"] if h else ""
        sp["person_name"] = h["name"] if h else ""
        sp["person_state"] = h["state"] if h else ""
        sp["person_score"] = h["score"] if h else None
    return {
        "session_id": sid, "k": nk, "silhouette": round(score, 3),
        "speakers": speakers, "changed": len(updates),
        "before": before,
        "voiceprint": vp,
        "engine": "CAM++ 声纹 + 球面 k-means（自动选 K）",
        "note": "重叠/抢话段仍可能归错；可在左侧「身份校正」改成真实昵称。",
    }
