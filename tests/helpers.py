# -*- coding: utf-8 -*-
"""测试共用的小工具：造场次、造发言、造高光行。"""
from __future__ import annotations

from typing import Any

from app import store

DEFAULT_SESSION = {
    "title": "抖音直播 11-05 20:00",
    "started_at": "2026-11-05T20:00:00",
    "duration_s": 120.0,
    "source_mode": "tab",
    "sample_rate": 16000,
    "audio_path": None,
    "warnings": [],
}


def add_session(conn, sid: str = "S_TEST", **kw: Any) -> dict:
    row = dict(DEFAULT_SESSION)
    row.update(kw)
    row["id"] = sid
    store.insert_session(conn, row)
    return row


def add_rows(conn, sid: str, rows: list[dict]) -> list[int]:
    out = []
    for i, r in enumerate(rows):
        base = {
            "session_id": sid,
            "t_start": float(i * 5),
            "t_end": float(i * 5 + 2),
            "speaker_id": "SPK_00",
            "speaker_label": "主播",
            "text": "默认发言内容",
            "asr_conf": 0.9,
            "overlap": 0,
            "category": "substantive",
            "value_tag": "normal",
            "importance": 0.5,
            "topic": "",
            "low_value_reason": "",
        }
        base.update(r)
        out.append(base)
    return store.insert_utterances(conn, out)


def utt_row(i: int, text: str, *, t: float | None = None, speaker: str = "SPK_00",
            label: str = "主播", **kw: Any) -> dict:
    t = float(i * 10) if t is None else float(t)
    return {
        "t_start": t, "t_end": t + 2.0,
        "speaker_id": speaker, "speaker_label": label, "text": text, **kw,
    }


def feats(t: float, *, score: float = 2.0, info: float = 0.0, arousal: float = 0.0,
          chars: int = 20, text: str = "一段有信息量的发言内容", **kw: Any) -> dict:
    """highlight._combine 之后的一行特征；to_events/_mix 只读这些键。"""
    base = {
        "id": int(t),
        "t_start": float(t), "t_end": float(t) + 2.0, "peak_t": float(t),
        "speaker_id": "SPK_00", "speaker_label": "主播", "text": text,
        "topic": "", "value_tag": "normal", "category": "substantive",
        "chars": chars, "num": 0, "concl": 0, "qa": 0, "shift": 0,
        "interrupt": 0, "burst": 0, "rate": 4.0, "rms": None,
        "med_rate": 4.0, "dur": 2.0,
        "score": score, "info_z": info, "arousal_z": arousal,
        "kind": "info" if info >= arousal else "live", "why": "结论句",
    }
    base.update(kw)
    return base
