# -*- coding: utf-8 -*-
"""显著性打分：把一场的逐句发言，算成「信息密度」与「情绪高潮」两条流。

为什么用 z 分数而不是绝对分：不同直播间的音量与语速基线差得很远（喊麦房比安静
访谈高 20dB、语速快一倍），任何绝对阈值都会得到"喊麦房全天 P0、安静访谈全天 P2"。
所以每个特征先对齐到本场的滑动基线（中位数 + MAD），再比较。

两条流为什么分开而不加权成一个分数：它们的时效性不同。
  info    干货（金额、结论、答疑）—— 晚 30 秒知道不亏，可以进简报
  arousal 有戏（语速陡增、抢麦、短句连发）—— 晚了就没意义，值得立刻叫人

W1 只做离线：整场算完 → 「本场最值得听的 N 段」→ 人工验精度。
W2 再把同一套特征接到实时链路（_on_segment）和飞书推送，打分函数不用改。
"""
from __future__ import annotations

import bisect
import os
import re
import sqlite3
import wave
from pathlib import Path
from typing import Any

from . import analysis, feedback, profile, store
from .config import PUSH_COOLDOWN_S

try:  # numpy 只用于音频能量；没装时退化为纯文本特征，其余功能不受影响
    import numpy as _np
except ImportError:
    _np = None

# ---------------------------------------------------------------- 可调参数

TH = float(os.environ.get("LIVENOTES_HL_TH", "1.5"))          # 进入高光的 z 门槛
PUSH_TH = float(os.environ.get("LIVENOTES_HL_PUSH_TH", "2.5"))  # W2：值得主动叫人
MERGE_S = float(os.environ.get("LIVENOTES_HL_MERGE_S", "25"))  # 相邻高光句合并窗口
LIMIT = int(os.environ.get("LIVENOTES_HL_LIMIT", "12"))        # 每场最多留几段
WINDOW_S = float(os.environ.get("LIVENOTES_HL_WINDOW_S", "600"))  # 滑动基线窗口 ±5min
PAD_BEFORE, PAD_AFTER = 20.0, 10.0   # 事件窗口留白，试听时能听到上文

_NUM_RE = re.compile(r"\d+(?:\.\d+)?|[一二三四五六七八九十百千万亿两]{1,4}"
                     r"(?=[块元岁个天月年次人单条倍成万亿])")

INFO_W = {"num": 0.40, "concl": 0.32, "qa": 0.22, "shift": 0.16, "chars": 0.18}
AROUSAL_W = {"rate": 0.45, "rms": 0.30, "burst": 0.25, "interrupt": 0.30}


# ---------------------------------------------------------------- 基础统计

def _med_mad(xs: list[float]) -> tuple[float, float]:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0, 0.0
    med = s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])
    dev = sorted(abs(v - med) for v in s)
    mad = dev[n // 2] if n % 2 else 0.5 * (dev[n // 2 - 1] + dev[n // 2])
    return float(med), float(mad)


def _std(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return (sum((v - m) ** 2 for v in xs) / (n - 1)) ** 0.5


def _z(values: list[float | None], times: list[float]) -> tuple[list[float], list[float]]:
    """对本场每个点取 ±WINDOW_S 内的样本做稳健标准化。

    返回 (z 列表, 每个点的滑动中位数)。中位数后面给 why 用（"语速 1.9× 基线"）。
    """
    n = len(values)
    if not n:
        return [], []
    clean = [v for v in values if v is not None]
    if not clean:
        return [0.0] * n, [0.0] * n
    gmed, gmad = _med_mad(clean)
    gscale = 1.4826 * gmad or _std(clean) or 1.0

    if _np is not None:
        # numpy 路径：实时链路每次喂一句都要重算整窗，纯 Python 的 median/MAD 会拖垮它
        arr = _np.array([_np.nan if v is None else v for v in values], dtype="float64")
        tsz = _np.array(times, dtype="float64")
        ok = ~_np.isnan(arr)
        zs: list[float] = []
        meds: list[float] = []
        for i, v in enumerate(values):
            if v is None or _np.isnan(arr[i]):
                zs.append(0.0)
                meds.append(gmed)
                continue
            lo = int(_np.searchsorted(tsz, tsz[i] - WINDOW_S / 2))
            hi = int(_np.searchsorted(tsz, tsz[i] + WINDOW_S / 2, side="right"))
            sample = arr[lo:hi][ok[lo:hi]]
            if sample.size >= 15:
                med = float(_np.median(sample))
                mad = float(_np.median(_np.abs(sample - med)))
                scale = 1.4826 * mad
                if scale <= 1e-6:
                    scale = float(sample.std(ddof=1)) if sample.size > 1 else 0.0
            else:
                med, scale = gmed, gscale
            zs.append((v - med) / (scale or 1e-6))
            meds.append(med)
        return zs, meds

    zs: list[float] = []
    meds: list[float] = []
    for i, v in enumerate(values):
        if v is None:
            zs.append(0.0)
            meds.append(gmed)
            continue
        lo = bisect.bisect_left(times, times[i] - WINDOW_S / 2)
        hi = bisect.bisect_right(times, times[i] + WINDOW_S / 2)
        sample = [values[j] for j in range(lo, hi) if values[j] is not None]
        if len(sample) >= 15:
            med, mad = _med_mad(sample)
            scale = 1.4826 * mad or _std(sample) or 1.0
        else:
            med, scale = gmed, gscale
        zs.append((v - med) / (scale or 1e-6))
        meds.append(med)
    return zs, meds


def _z_at(values: list[float | None], times: list[float], idx: int,
          window_s: float = WINDOW_S, min_n: int = 15) -> tuple[float, float]:
    """只给第 idx 个点算 z。实时链路每次喂一句都要用，不能把整窗重算一遍。"""
    v = values[idx]
    if v is None:
        return 0.0, 0.0
    lo = bisect.bisect_left(times, times[idx] - window_s / 2)
    hi = bisect.bisect_right(times, times[idx] + window_s / 2)
    sample = [values[j] for j in range(lo, hi) if values[j] is not None]
    if len(sample) < min_n:
        return 0.0, 0.0
    if _np is not None:
        a = _np.asarray(sample, dtype="float64")
        med = float(_np.median(a))
        mad = float(_np.median(_np.abs(a - med)))
        scale = 1.4826 * mad
        if scale <= 1e-6:
            scale = float(a.std(ddof=1)) if a.size > 1 else 0.0
    else:
        med, mad = _med_mad(sample)
        scale = 1.4826 * mad or _std(sample) or 0.0
    return (v - med) / (scale or 1e-6), med


# ---------------------------------------------------------------- 音频能量

def _energies(audio_path: str | Path | None, frame_s: float = 0.5) -> list[float] | None:
    """整场按 0.5s 一帧算 RMS。流式读，不把整段音频读进内存。"""
    if _np is None or not audio_path:
        return None
    p = Path(audio_path)
    if not p.is_file():
        return None
    try:
        with wave.open(str(p), "rb") as w:
            sr, nch = w.getframerate(), w.getnchannels()
            step = max(1, int(sr * frame_s))
            out: list[float] = []
            while True:
                raw = w.readframes(step)
                if not raw:
                    break
                a = _np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
                if nch > 1:
                    a = a.reshape(-1, nch).mean(axis=1)
                out.append(float(_np.sqrt((a * a).mean())))
        return out
    except (OSError, wave.Error, ValueError):
        return None


def _mean_energy(energies: list[float] | None, frame_s: float,
                 t0: float, t1: float) -> float | None:
    if not energies:
        return None
    lo = max(0, int(t0 / frame_s))
    hi = min(len(energies), max(lo + 1, int(t1 / frame_s) + 1))
    if lo >= len(energies):
        return None
    seg = energies[lo:hi]
    return sum(seg) / len(seg) if seg else None


# ---------------------------------------------------------------- 逐句特征

def _cjk_len(text: str) -> int:
    return sum(1 for ch in (text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _text_feats(text: str) -> dict:
    t = (text or "").strip()
    return {
        "chars": _cjk_len(t),
        "num": min(4, len(_NUM_RE.findall(t))),
        "concl": min(2, sum(1 for w in analysis.CONCLUSION_HINTS if w in t)),
        "question": 1 if ("?" in t or "？" in t
                          or any(w in t for w in analysis.QUESTION_HINTS)) else 0,
        "short": 1 if _cjk_len(t) <= 6 else 0,
    }


class FeatureStream:
    """逐句算特征，带一点上下文状态（上一句结束时间、上一位说话人、提问锚点）。

    离线跑整场和在线接实时都用这一个实现 —— 两套代码算出来的分数迟早会对不上，
    而"实时和事后结论不一致"是最难查的那种 bug。
    """

    def __init__(self, energies: list[float] | None = None, frame_s: float = 0.5) -> None:
        self.energies = energies
        self.frame_s = frame_s
        self.prev_end: float | None = None
        self.prev_spk = ""
        self.prev_topic = ""
        self.short_run = 0
        self.q_speaker = ""
        self.q_time = -999.0

    def push(self, r: dict) -> dict:
        t0, t1 = float(r["t_start"]), float(r["t_end"])
        dur = max(0.4, t1 - t0)
        tf = _text_feats(r["text"])
        gap = (t0 - self.prev_end) if self.prev_end is not None else 99.0
        # 0.25s 是正常接话；真正的抢麦是几乎没有间隙。库里 overlap 标记更准，优先用。
        interrupt = 1 if ((r.get("overlap") or gap < 0.12)
                          and self.prev_spk and self.prev_spk != r["speaker_id"]) else 0
        self.short_run = self.short_run + 1 if tf["short"] else 0
        shift = 1 if (self.prev_topic and r.get("topic") and r["topic"] != self.prev_topic) else 0
        qa = 1 if (self.q_speaker and self.q_speaker != r["speaker_id"]
                   and t0 - self.q_time <= 40) else 0
        if tf["question"]:
            self.q_speaker, self.q_time = r["speaker_id"], t0
        elif t0 - self.q_time > 40:
            self.q_speaker = ""
        feat = {
            "id": r.get("id"),
            "t_start": t0, "t_end": t1,
            "speaker_id": r.get("speaker_id") or "",
            "speaker_label": r.get("speaker_label") or "",
            "text": (r.get("text") or "").strip(),
            "topic": r.get("topic") or "",
            "value_tag": r.get("value_tag") or "normal",
            "category": r.get("category") or "substantive",
            "chars": tf["chars"], "num": tf["num"], "concl": tf["concl"],
            "qa": qa, "shift": shift, "interrupt": interrupt, "burst": self.short_run,
            # 太短的句子算不出有意义的语速：0.6 秒说 6 个字会得到 10 字/秒，
            # 直接把"嗯对对你你说"顶成全场第一。这类句子语速特征置空（z=0）。
            "rate": (tf["chars"] / dur) if (dur >= 1.2 and tf["chars"] >= 4) else None,
            "rms": _mean_energy(self.energies, self.frame_s, t0, t1),
            "dur": dur,
        }
        self.prev_end, self.prev_spk = t1, r["speaker_id"]
        self.prev_topic = r.get("topic") or self.prev_topic
        return feat


def _features(rows: list[dict], energies: list[float] | None, frame_s: float) -> list[dict]:
    """把一串发言变成打分要用的特征。纯计算，不碰数据库。"""
    st = FeatureStream(energies, frame_s)
    return [st.push(r) for r in rows]


def _gate(f: dict) -> float:
    """广告/寒暄/控场话术压权重：它们的语速也快、音量也大，不压就会天天霸榜。"""
    if f["value_tag"] == "low":
        return 0.35
    if f["category"] == "ad":
        return 0.25
    if f["category"] == "chitchat":
        return 0.30
    if f["category"] == "host_control":
        return 0.55
    return 1.0


# ---------------------------------------------------------------- 打分

def score_rows(rows: list[dict], audio_path: str | Path | None = None) -> list[dict]:
    """给每句打分。返回带 info_z / arousal_z / score / why 的行。"""
    if not rows:
        return []
    frame_s = 0.5
    energies = _energies(audio_path, frame_s)
    feats = _features(rows, energies, frame_s)
    return _score_feats(feats)


def _score_feats(feats: list[dict]) -> list[dict]:
    """对一批已算好特征的行打分。离线整场、在线滚动窗口都调这一个。"""
    if not feats:
        return feats
    times = [f["t_start"] for f in feats]

    z_num, _ = _z([float(f["num"]) for f in feats], times)
    z_concl, _ = _z([float(f["concl"]) for f in feats], times)
    z_qa, _ = _z([float(f["qa"]) for f in feats], times)
    z_shift, _ = _z([float(f["shift"]) for f in feats], times)
    z_chars, _ = _z([float(f["chars"]) for f in feats], times)
    z_rate, med_rate = _z([f["rate"] for f in feats], times)
    z_burst, _ = _z([float(f["burst"]) for f in feats], times)
    z_interrupt, _ = _z([float(f["interrupt"]) for f in feats], times)
    has_rms = any(f["rms"] is not None for f in feats)
    z_rms, _ = _z([f["rms"] for f in feats], times) if has_rms else ([0.0] * len(feats), [])

    aw = dict(AROUSAL_W)
    if not has_rms:
        aw.pop("rms", None)
    wsum = sum(aw.values()) or 1.0

    for i, f in enumerate(feats):
        _combine(f, {
            "num": z_num[i], "concl": z_concl[i], "qa": z_qa[i], "shift": z_shift[i],
            "chars": z_chars[i], "rate": z_rate[i],
            "burst": z_burst[i], "interrupt": z_interrupt[i],
            "rms": z_rms[i] if has_rms else None,
        }, med_rate[i] if i < len(med_rate) else 0.0)
    return feats


def _combine(f: dict, zv: dict, med_rate: float = 0.0) -> dict:
    """把一串 z 值按权重合成 info_z / arousal_z / 总分 / 类型 / 理由。

    单句就能算，所以离线（整批 z）和实时（只算当前句的 z）共用这一份公式——
    公式一旦分叉，实时和事后结论就会对不上。
    """
    has_rms = zv.get("rms") is not None
    aw = dict(AROUSAL_W)
    if not has_rms:
        aw.pop("rms", None)
    wsum = sum(aw.values()) or 1.0
    info = (INFO_W["num"] * zv.get("num", 0) + INFO_W["concl"] * zv.get("concl", 0)
            + INFO_W["qa"] * zv.get("qa", 0) + INFO_W["shift"] * zv.get("shift", 0)
            + INFO_W["chars"] * zv.get("chars", 0))
    arousal = (aw.get("rate", 0) * zv.get("rate", 0)
               + aw.get("rms", 0) * (zv.get("rms") or 0)
               + aw.get("burst", 0) * zv.get("burst", 0)
               + aw.get("interrupt", 0) * zv.get("interrupt", 0))
    if not has_rms:
        arousal *= 1.0 / wsum * sum(AROUSAL_W.values())
    g = _gate(f)
    # 相关性：命中关注词直接加在信息轴上，并跟着降权系数一起衰减
    # （广告里出现"退款"也不该因为这个词就上榜）。
    f["kw"] = profile.hits(f.get("text") or "")
    kw_boost = profile.boost(f.get("text") or "") * g
    f["info_z"] = round(info * g + kw_boost, 2) + 0.0     # +0.0 消掉 -0.0
    f["arousal_z"] = round(arousal * g, 2) + 0.0
    f["score"] = round(max(f["info_z"], f["arousal_z"]), 2) + 0.0
    f["med_rate"] = med_rate
    f["kind"] = ("both" if (f["info_z"] >= TH and f["arousal_z"] >= TH)
                 else "info" if f["info_z"] >= TH
                 else "live" if f["arousal_z"] >= TH else "none")
    f["why"] = _why(f)
    return f


def _why(f: dict) -> str:
    """用人话解释「为什么这段值得听」。模板拼装，不调 LLM。"""
    parts: list[str] = []
    if f.get("kw"):
        parts.append("命中关注词 " + "、".join(f["kw"][:2]))
    base = f.get("med_rate") or 0.0
    if base > 0.5 and f.get("rate") and f["rate"] > 1.6 * base:
        parts.append("语速 %.1f× 基线" % (f["rate"] / base))
    if f.get("interrupt"):
        parts.append("抢麦")
    if f.get("num", 0) >= 2:
        parts.append("提到 %d 处数字/金额" % f["num"])
    if f.get("concl"):
        parts.append("结论句")
    if f.get("qa"):
        parts.append("接住了提问")
    if f.get("burst", 0) >= 3:
        parts.append("连着 %d 句短促" % f["burst"])
    if f.get("shift") and f.get("topic"):
        parts.append("话题转向「%s」" % f["topic"])
    if f.get("chars", 0) >= 60:
        parts.append("%d 字长段陈述" % f["chars"])
    return "；".join(parts[:3])


def to_events(feats: list[dict], limit: int = LIMIT, th: float = TH) -> list[dict]:
    """把高分句合并成「事件窗口」：25 秒内连着高，算同一件事，不刷屏。"""
    hot = [f for f in feats if f["score"] >= th]
    if not hot:
        return []
    hot.sort(key=lambda x: x["t_start"])
    groups: list[list[dict]] = []
    for f in hot:
        if groups and f["t_start"] - groups[-1][-1]["t_end"] <= MERGE_S:
            groups[-1].append(f)
        else:
            groups.append([f])
    events: list[dict] = []
    for g in groups:
        # 整组都是"你要你你说"这种碎片时，不构成值得回听的一段。
        solid = [f for f in g if f["chars"] >= 8]
        if not solid:
            continue
        peak = max(solid, key=lambda x: x["score"])
        both = peak["info_z"] >= th and peak["arousal_z"] >= th
        kind = "both" if both else ("info" if peak["info_z"] >= th else "live")
        # P0 留给 W2 的推送预算：分数够高才打断。目前只做排序展示，不涉及打扰。
        level = "P0" if peak["score"] >= PUSH_TH else "P1"
        events.append({
            "session_id": peak.get("session_id", ""),
            "t_start": round(max(0.0, g[0]["t_start"] - PAD_BEFORE), 2),
            "t_end": round(g[-1]["t_end"] + PAD_AFTER, 2),
            "peak_t": round(peak["t_start"], 2),
            "kind": kind, "level": level,
            "score": peak["score"],
            "info_z": peak["info_z"], "arousal_z": peak["arousal_z"],
            "why": peak["why"] or "综合得分靠前",
            "speaker_id": peak.get("speaker_id", ""),
            "speaker_label": peak["speaker_label"],
            "text": peak["text"],
            "n_utts": len(g),
            "features_json": _json({
                "rate": None if peak.get("rate") is None else round(peak["rate"], 2),
                "med_rate": round(peak.get("med_rate") or 0.0, 2),
                "rms": None if peak["rms"] is None else round(peak["rms"], 4),
                "num": peak["num"], "concl": peak["concl"], "qa": peak["qa"],
                "interrupt": peak["interrupt"], "burst": peak["burst"],
                "chars": peak["chars"], "audio": _np is not None and peak["rms"] is not None,
            }),
        })
    events.sort(key=lambda e: e["score"], reverse=True)
    return _mix(events, limit)


def _mix(events: list[dict], limit: int, live_ratio: float = 0.35) -> list[dict]:
    """按分数排序会出事：干货句（101 条）远多于有戏句（37 条），纯排序会把
    「情绪高潮」整条轴挤没。这里给 live/both 留固定配额，两条轴都有位置。"""
    if len(events) <= limit:
        return events
    live = [e for e in events if e["kind"] in ("live", "both")]
    info = [e for e in events if e["kind"] == "info"]
    # 只有情绪确实高的才算"有戏"。弱 live 句（大家只是随口接话）不该占名额，
    # 否则榜单会被"明白我明白就是刚刚的意思"这种句子挤满。
    strong = [e for e in live if e["arousal_z"] >= 2.0]
    quota = min(len(strong), max(1, int(round(limit * live_ratio))))
    picked = strong[:quota] + info[:max(0, limit - quota)]
    if len(picked) < limit:
        picked += [e for e in events if e not in picked][:limit - len(picked)]
    picked.sort(key=lambda e: e["score"], reverse=True)
    return picked[:limit]


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------- 对外

def score_session(conn: sqlite3.Connection, sid: str, limit: int = LIMIT) -> list[dict]:
    """算一场的高光事件，不写库。给评测脚本和页面预览用。"""
    rows = store.list_utterances_for_highlight(conn, sid)
    if not rows:
        return []
    sess = store.get_session(conn, sid) or {}
    feats = score_rows(rows, sess.get("audio_path"))
    for f in feats:
        f["session_id"] = sid
    return to_events(feats, limit=limit)


def build(conn: sqlite3.Connection, sid: str, limit: int = LIMIT) -> dict:
    """算完并落库；同时把逐句特征回写到 utterances.features_json（W2 流式要用）。"""
    rows = store.list_utterances_for_highlight(conn, sid)
    if not rows:
        return {"session_id": sid, "n": 0, "highlights": [], "audio": False}
    sess = store.get_session(conn, sid) or {}
    feats = score_rows(rows, sess.get("audio_path"))
    for f in feats:
        f["session_id"] = sid
    pairs = []
    for f in feats:
        if f.get("id") is None:
            continue
        pairs.append((_json({"rate": None if f.get("rate") is None else round(f["rate"], 2),
                             "rms": f["rms"],
                             "chars": f["chars"], "num": f["num"], "concl": f["concl"],
                             "qa": f["qa"], "interrupt": f["interrupt"],
                             "burst": f["burst"], "kind": f["kind"]}), f["id"]))
    store.save_utterance_features(conn, pairs)
    events = to_events(feats, limit=limit)
    n = store.replace_highlights(conn, sid, events)
    return {"session_id": sid, "n": n, "highlights": events,
            "audio": any(f["rms"] is not None for f in feats)}

# ---------------------------------------------------------------- 实时

class OnlineScorer:
    """实时打分：基线只取最近 WINDOW_S 秒，样本不足时退回绝对阈值。

    离线和在线的唯一差别是"基线从哪来"：离线有整场，在线只有过去。
    直接套 z 分数会在开场把第一句判成全场最高（样本 1 个，MAD 为 0），
    所以前 min_history 句只认"绝对就很强"的信号 —— 宁可晚十分钟才开始推，
    也不要在开场推一堆废话把信任烧掉。
    """

    def __init__(self, window_s: float = 900.0, min_history: int = 15,
                 th: float = 2.5,
                 cooldown_s: float = PUSH_COOLDOWN_S,
                 hold_s: float = 45.0, immediate: float = 3.2,
                 bias: dict[str, float] | None = None) -> None:
        self.stream = FeatureStream(None)   # 实时没有整段音频，能量特征留空
        self.window_s = window_s
        self.min_history = min_history
        self.th = th
        self.cooldown_s = cooldown_s
        self.hold_s = hold_s
        self.immediate = immediate
        # 从 👍/👎 学来的门槛偏置（app/feedback.py）。场次开始时定一次，
        # 中途变了也不改口：同一场里两句话用两套标准，用户会觉得"时灵时不灵"。
        self.bias = dict(bias or {})
        self.last_skip: dict | None = None   # 被门槛/冷却拦下的那条，给台账用
        self.buf: list[dict] = []
        self.pending: dict | None = None
        self.last_event_t = -1e9
        self.events: list[dict] = []

    @property
    def warm(self) -> bool:
        return len(self.buf) >= self.min_history

    def feed(self, row: dict) -> dict | None:
        """喂一句。返回一个事件就代表"该叫人了"，否则 None。

        不"一碰到阈值就叫"：同一件事会连着好几句都过线，先按阈值碰到的那句
        往往不是最好的那句。所以先攒 HOLD_S 秒，只推这一段里分数最高的一句——
        这和事后榜"合并相邻句、取峰值"的规则一致，实时和事后的结论才对得上。
        分数极高时（IMMEDIATE）不等，立刻推。
        """
        f = self.stream.push(row)
        self.buf.append(f)
        cut = f["t_start"] - self.window_s
        while self.buf and self.buf[0]["t_start"] < cut:
            self.buf.pop(0)

        cold = not self.warm
        if cold:
            score, kind, info_z, arousal_z, why = self._cold(f)
            f["score"], f["kind"] = score, kind
            f["info_z"], f["arousal_z"] = info_z, arousal_z
            f["why"] = why
        else:
            self._score_one(f)
            score, kind = f["score"], f["kind"]

        out = None
        # ① 攒够了，把这一段里最好的那句推出去，然后清空（不清会重复推同一句）
        if self.pending and f["t_start"] - self.pending["t0"] > self.hold_s:
            out = self._emit(self.pending["best"])
            self.pending = None

        hot = score >= self.th and f["chars"] >= 8
        if hot and score >= self.immediate:
            if out is None:
                out = self._emit(f)
            self.pending = None
        elif hot:
            if self.pending is None:
                self.pending = {"t0": f["t_start"], "best": f}
            elif f["score"] > self.pending["best"]["score"]:
                self.pending["best"] = f
        return out

    def flush(self) -> dict | None:
        """场次结束时把还压着的那一条放出来。"""
        if not self.pending:
            return None
        best = self.pending["best"]
        self.pending = None
        return self._emit(best)

    def poll(self, now_t: float, silence_s: float = 0.0,
             force: bool = False) -> dict | None:
        """没有新句子时，也能把压着的那条放出来。

        feed() 的发法依赖「下一句到来」：直播一旦冷场、或者用户直接关掉标签页，
        最好的那句会一直留在 pending 里，直到点「结束」——这正是「只有点结束
        才推送」的观感来源（旧版更糟：结束时也没人调 flush，直接丢了）。
        silence_s 是"已经多久没收到音频"，用墙钟算，暂停/关页也能兜住。
        """
        if not self.pending:
            return None
        if force:
            self.last_event_t = -1e9      # 收尾那条不再让冷却拦：这是本场最后一句
        elif now_t - self.pending["t0"] <= self.hold_s and silence_s <= self.hold_s:
            return None
        return self.flush()

    def _score_one(self, f: dict) -> None:
        times = [x["t_start"] for x in self.buf]
        idx = len(self.buf) - 1
        zv: dict = {}
        med_rate = 0.0
        for key in ("num", "concl", "qa", "shift", "chars", "rate", "burst", "interrupt"):
            z, med = _z_at([x.get(key) for x in self.buf], times, idx, self.window_s)
            zv[key] = z
            if key == "rate":
                med_rate = med
        zv["rms"] = None                     # 实时拿不到整段能量，音频轴留空
        _combine(f, zv, med_rate)

    def _emit(self, f: dict) -> dict | None:
        # 先看"这类高光值不值得打断"：标过没用的那类，要在本场更突出才推。
        bonus = feedback.bias(f, f.get("why") or "", self.bias)
        if f.get("score", 0) + bonus < PUSH_TH:
            return self._skip(f, "learned_bias")
        if f["t_start"] - self.last_event_t < self.cooldown_s:
            return self._skip(f, "cooldown")
        self.last_skip = None
        self.last_event_t = f["t_start"]
        ev = {
            "session_id": f.get("session_id", ""),
            "t_start": round(max(0.0, f["t_start"] - PAD_BEFORE), 2),
            "t_end": round(f["t_end"] + PAD_AFTER, 2),
            "peak_t": round(f["t_start"], 2),
            "kind": f.get("kind") or "info",
            "level": "P0" if f.get("score", 0) + bonus >= PUSH_TH else "P1",
            "score": f.get("score", 0), "info_z": f.get("info_z", 0),
            "arousal_z": f.get("arousal_z", 0),
            "why": f.get("why") or "综合得分靠前",
            "speaker_id": f.get("speaker_id", ""),
            "speaker_label": f.get("speaker_label", ""), "text": f.get("text", ""),
            "n_utts": 1, "cold": not self.warm,
            # 反馈闭环用：把这句的特征带出去，下一场才知道"这类"是哪类
            "bias": bonus,
            "features": {k: f.get(k) for k in
                         ("num", "concl", "qa", "interrupt", "burst", "chars",
                          "rate", "med_rate")},
        }
        self.events.append(ev)
        return ev

    def _skip(self, f: dict, reason: str) -> None:
        """记下"这次没推"的原因，供推送台账展示。调用方取走即清。"""
        self.last_skip = {
            "reason": reason,
            "peak_t": round(float(f.get("t_start") or 0.0), 2),
            "score": float(f.get("score") or 0.0),
            "why": f.get("why") or "",
            "features": {k: f.get(k) for k in
                         ("num", "concl", "qa", "interrupt", "burst", "chars",
                          "rate", "med_rate")},
        }
        return None

    def _cold(self, f: dict) -> tuple[float, str, float, float, str]:
        """冷启动：还没有基线，只认"绝对值就高"的信号。"""
        info = 0.0
        if f["num"] >= 4:
            info += 1.6
        elif f["num"] >= 3:
            info += 1.0
        elif f["num"] == 2:
            info += 0.5
        if f["concl"]:
            info += 0.5
        if f["qa"]:
            info += 0.4
        if f["chars"] >= 60:
            info += 0.4
        aro = 0.0
        if f.get("rate") and f["rate"] >= 5.5:
            aro += 1.6
        if f["interrupt"]:
            aro += 1.0
        if f["burst"] >= 4:
            aro += 0.8
        g = _gate(f)
        info, aro = round(info * g, 2) + 0.0, round(aro * g, 2) + 0.0
        # 冷启动封顶：没有基线时不该出现"和事后同样可信"的高分，
        # 否则开场第一句的 3.2 分就能触发即时推送。
        info, aro = min(info, 2.4), min(aro, 2.4)
        score = round(max(info, aro), 2) + 0.0
        kind = ("both" if (info >= self.th and aro >= self.th)
                else "info" if info >= self.th
                else "live" if aro >= self.th else "none")
        parts = []
        if f["num"] >= 2:
            parts.append("提到 %d 处数字/金额" % f["num"])
        if f["concl"]:
            parts.append("结论句")
        if f.get("rate") and f["rate"] >= 5.0:
            parts.append("语速 %.1f 字/秒" % f["rate"])
        if f["interrupt"]:
            parts.append("抢麦")
        return score, kind, info, aro, ("冷启动基线：" + "；".join(parts[:2]) if parts else "")

