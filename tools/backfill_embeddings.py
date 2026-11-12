# -*- coding: utf-8 -*-
"""给旧场次补 声纹 embedding（这些场次是在"保存声纹"之前采的）。

    python tools/backfill_embeddings.py               # 所有缺声纹、但有 WAV 的场次
    python tools/backfill_embeddings.py LIVE_2026...  # 指定场次

只做"切音频 -> CAM++ 提取 -> 写 utterance_emb"，不动任何已有标签；
补完之后网页上的「重新分人」就能用。约 60 句/秒（CPU），一场 10 分钟约 20 秒。
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import sherpa_onnx  # noqa: E402

from app import store  # noqa: E402
from app.config import SPEAKER_MODEL  # noqa: E402


def _load_wav(path: Path):
    with wave.open(str(path), "rb") as w:
        rate, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise SystemExit("只支持 16bit PCM WAV：%s" % path)
    x = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, rate


def backfill(sid: str) -> int:
    conn = store.connect()
    try:
        s = store.get_session(conn, sid)
        if not s:
            print("  ! 场次不存在"); return 0
        if store.count_embeddings(conn, sid):
            print("  = 已有声纹，跳过"); return 0
        ap = s.get("audio_path")
        if not ap or not Path(ap).is_file():
            print("  ! 没有原始录音，无法补声纹"); return 0
        x, rate = _load_wav(Path(ap))
        rows = [dict(r) for r in conn.execute(
            "SELECT id,t_start,t_end FROM utterances WHERE session_id=? ORDER BY t_start",
            (sid,))]
        ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(SPEAKER_MODEL), num_threads=2, provider="cpu"))
        items = []
        for r in rows:
            a, b = int(r["t_start"] * rate), int(r["t_end"] * rate)
            seg = x[max(0, a):min(len(x), b)]
            if len(seg) < int(0.5 * rate):
                continue
            st = ex.create_stream()
            st.accept_waveform(rate, seg)
            st.input_finished()
            v = np.asarray(ex.compute(st), dtype="float32")
            v /= (np.linalg.norm(v) or 1.0)
            items.append((int(r["id"]), np.float16(v).tobytes(), int(v.shape[0])))
        store.save_embeddings(conn, sid, items)
        print("  + %d 条声纹" % len(items))
        return len(items)
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    conn = store.connect()
    try:
        if argv:
            targets = argv
        else:
            targets = [r["id"] for r in conn.execute(
                "SELECT id FROM sessions ORDER BY started_at")]
            targets = [t for t in targets if not store.count_embeddings(conn, t)]
    finally:
        conn.close()
    print("待处理 %d 场" % len(targets))
    for sid in targets:
        print("[%s]" % sid)
        backfill(sid)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
