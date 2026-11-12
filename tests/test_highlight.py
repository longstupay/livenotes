# -*- coding: utf-8 -*-
"""显著性打分回归：滑动基线、冷启动封顶、合并窗口、双轴配额与反馈偏置。"""
from __future__ import annotations

import math
import wave
from pathlib import Path

import pytest
from helpers import feats

from app import highlight, profile


def _row(t: float, text: str, *, dur: float = 5.0, speaker: str = "SPK_00",
         topic: str = "", overlap: int = 0, category: str = "substantive",
         value_tag: str = "normal") -> dict:
    return {
        "id": int(t * 100), "t_start": t, "t_end": t + dur,
        "speaker_id": speaker, "speaker_label": "主播", "text": text,
        "topic": topic, "overlap": overlap,
        "category": category, "value_tag": value_tag,
        "importance": 0.5, "asr_conf": 0.9,
    }


# ---------------------------------------------------------------- 基础统计

def test_med_mad_even_odd_and_empty():
    assert highlight._med_mad([]) == (0.0, 0.0)
    assert highlight._med_mad([1, 2, 100]) == (2.0, 1.0)
    assert highlight._med_mad([1, 2, 3, 4]) == (2.5, 1.0)


def test_rolling_z_uses_local_baseline_and_flags_peak():
    times = [i * 10.0 for i in range(20)]
    values = [1.0] * 19 + [5.0]
    zs, meds = highlight._z(values, times)
    assert meds[-1] == pytest.approx(1.0)
    assert zs[-1] > 3.0
    assert all(abs(z) < 1.0 for z in zs[:-1])


def test_too_few_samples_fall_back_to_global_baseline():
    zs, meds = highlight._z([1.0, 2.0, 3.0], [0.0, 10.0, 20.0])
    # 窗口里样本 <15：退到全场中位数/ MAD，而不是把第一个点判成峰值
    assert meds == [2.0, 2.0, 2.0]
    assert zs[2] == pytest.approx((3.0 - 2.0) / (1.4826 * 1.0), rel=1e-6)


def test_none_values_score_zero():
    zs, meds = highlight._z([1.0, None, 3.0], [0.0, 10.0, 20.0])
    assert zs[1] == 0.0
    assert meds[1] == 2.0


def test_numpy_and_pure_python_paths_agree(monkeypatch):
    """实时（纯 Python/`_z_at`）和离线（numpy/`_z`）不能算出两套分数。"""
    values = [1.0, 2.0, 3.0, 4.0, 5.0] * 6
    times = [i * 10.0 for i in range(len(values))]
    z_np, med_np = highlight._z(values, times)
    monkeypatch.setattr(highlight, "_np", None)
    z_py, med_py = highlight._z(values, times)
    assert z_np == pytest.approx(z_py, abs=1e-6)
    assert med_np == pytest.approx(med_py, abs=1e-6)
    assert highlight._z_at(values, times, 17)[0] == pytest.approx(z_py[17], abs=1e-6)


# ---------------------------------------------------------------- 逐句特征

def test_feature_stream_context_features():
    st = highlight.FeatureStream()
    a = st.push(_row(0.0, "嗯", dur=1.0, speaker="A"))
    assert a["chars"] == 1 and a["burst"] == 1
    assert a["rate"] is None                    # 太短不计算语速
    assert a["interrupt"] == 0                  # 第一句没有参照

    b = st.push(_row(1.05, "请问这个方案怎么做", dur=2.0, speaker="B", topic="方案"))
    assert b["qa"] == 0                         # 提问句本身不算"接住"
    assert b["burst"] == 0
    assert b["interrupt"] == 1                  # 0.05s 接话 + 换人
    assert b["rate"] == pytest.approx(len("请问这个方案怎么做") / 2.0)

    c = st.push(_row(5.0, "是的，这个方法可行，关键是现金流", dur=2.0,
                     speaker="A", topic="方案"))
    assert c["qa"] == 1                         # 40 秒内换了人回应提问
    assert c["shift"] == 0                      # 同话题不转

    d = st.push(_row(8.0, "另外说说面试", dur=2.0, speaker="B", topic="面试"))
    assert d["shift"] == 1
    assert d["interrupt"] == 0


def test_short_burst_tracking_resets():
    st = highlight.FeatureStream()
    st.push(_row(0.0, "好", dur=1.0, speaker="A", topic="x"))
    st.push(_row(1.0, "对", dur=1.0, speaker="A", topic="x"))
    assert st.push(_row(2.0, "是", dur=1.0, speaker="A", topic="x"))["burst"] == 3
    assert st.push(_row(10.0, "这是一句完整的长陈述", dur=2.0, speaker="A",
                        topic="x"))["burst"] == 0


def test_gate_penalises_low_value_categories():
    assert highlight._gate({"value_tag": "low", "category": "substantive"}) == 0.35
    assert highlight._gate({"value_tag": "normal", "category": "ad"}) == 0.25
    assert highlight._gate({"value_tag": "normal", "category": "chitchat"}) == 0.30
    assert highlight._gate({"value_tag": "normal", "category": "host_control"}) == 0.55
    assert highlight._gate({"value_tag": "normal", "category": "substantive"}) == 1.0


# ---------------------------------------------------------------- 合成与事件

def test_keyword_boost_is_gated_by_value(isolated_paths):
    profile.save(["OPC"])
    zv = {"num": 0, "concl": 0, "qa": 0, "shift": 0, "chars": 0,
          "rate": 0, "burst": 0, "interrupt": 0, "rms": None}
    normal = highlight._combine({"text": "OPC 报价", "value_tag": "normal",
                                 "category": "substantive", "chars": 8}, dict(zv))
    assert normal["kw"] == ["OPC"]
    assert normal["info_z"] == pytest.approx(profile.BOOST_ONE)
    low = highlight._combine({"text": "OPC 广告", "value_tag": "low",
                              "category": "ad", "chars": 8}, dict(zv))
    assert low["info_z"] == pytest.approx(0.32)


def test_combine_kind_and_why():
    base = {"text": "第一，注意现金流", "value_tag": "normal",
            "category": "substantive", "chars": 30, "num": 2, "concl": 1,
            "interrupt": 1, "qa": 0, "burst": 3, "shift": 0,
            "rate": 8.0, "med_rate": 4.0, "topic": ""}
    zv = {"num": 3.0, "concl": 2.0, "qa": 0, "shift": 0, "chars": 2.0,
          "rate": 3.0, "burst": 2.0, "interrupt": 2.0, "rms": None}
    f = highlight._combine(base, zv, med_rate=4.0)
    assert f["kind"] == "both"
    assert "提到 2 处数字/金额" in f["why"]
    assert "语速 2.0× 基线" in f["why"]
    assert "抢麦" in f["why"]


def test_to_events_merges_adjacent_and_pads_window():
    f1 = feats(0, score=2.0, chars=20, session_id="S")
    f2 = feats(20, score=2.4, chars=20, session_id="S")
    f3 = feats(100, score=2.8, chars=20, session_id="S")
    events = highlight.to_events([f1, f2, f3], limit=10, th=1.5)
    assert len(events) == 2
    first = next(e for e in events if e["peak_t"] == 20)
    second = next(e for e in events if e["peak_t"] == 100)
    assert first["n_utts"] == 2
    assert first["t_start"] == 0 and first["t_end"] == 32   # 前 20s / 后 10s 留白
    assert first["level"] == "P1"
    assert second["level"] == "P0"


def test_to_events_drops_fragment_only_groups():
    frag = [feats(0, score=2.6, chars=3, text="对对对", session_id="S"),
            feats(5, score=2.6, chars=4, text="嗯是的", session_id="S")]
    assert highlight.to_events(frag) == []


def test_to_events_respects_merge_boundary():
    a = feats(0, score=2.0, chars=20, session_id="S")     # t_end = 2
    b = feats(27.0, score=2.0, chars=20, session_id="S")  # 差 25s：同一段
    c = feats(27.01, score=2.0, chars=20, session_id="S")
    assert len(highlight.to_events([a, b])) == 1
    assert len(highlight.to_events([a, c])) == 2


def test_mix_reserves_quota_for_live_events():
    info = [feats(i * 10, score=1.0 + i * 0.01, info=2.0, arousal=0.0,
                  kind="info", session_id="S") for i in range(30)]
    live = [feats(1000 + i, score=3.0, info=0.0, arousal=3.0,
                  kind="live", session_id="S") for i in range(5)]
    picked = highlight._mix(sorted(info + live, key=lambda e: -e["score"]), limit=10)
    assert len(picked) == 10
    assert sum(1 for e in picked if e["kind"] in ("live", "both")) >= 3


# ---------------------------------------------------------------- 实时打分

def test_online_cold_start_is_capped():
    s = highlight.OnlineScorer(min_history=15)
    long_text = "第一名 300 元，第二名 200 元，第三名 100 元，第四名 50 元"
    ev = s.feed(_row(0.0, long_text, dur=5.0))
    assert ev is None
    assert s.buf[-1]["score"] <= 2.4            # 封顶，够不到 2.5 的推送线
    assert s.buf[-1]["kind"] == "none"


def test_online_warm_immediate_emit():
    s = highlight.OnlineScorer(window_s=900, min_history=15, th=2.5,
                               cooldown_s=0, hold_s=45, immediate=3.2)
    for i in range(15):
        assert s.feed(_row(i * 10.0, "这是正常的陈述句内容", dur=5.0)) is None
    assert s.warm
    hot = _row(150.0, "第一名 300 元，第二名 200 元，第三名 100 元，第四名 50 元",
               dur=5.0)
    ev = s.feed(hot)
    assert ev is not None
    assert ev["peak_t"] == 150.0
    assert ev["features"]["num"] >= 4
    assert ev["level"] == "P0"
    assert len(s.events) == 1


def test_poll_holds_then_releases_pending():
    s = highlight.OnlineScorer(hold_s=45, cooldown_s=0)
    s.pending = {"t0": 100.0, "best": feats(100.0, score=3.0, chars=20)}
    assert s.poll(110.0, silence_s=0.0) is None
    ev = s.poll(150.0, silence_s=0.0)
    assert ev is not None and ev["peak_t"] == 100.0
    assert s.pending is None


def test_poll_force_releases_even_within_hold_and_cooldown():
    s = highlight.OnlineScorer(hold_s=45, cooldown_s=600)
    s.pending = {"t0": 100.0, "best": feats(100.0, score=3.0, chars=20)}
    s.last_event_t = 99.0
    ev = s.poll(100.0, force=True)
    assert ev is not None and ev["peak_t"] == 100.0


def test_online_cooldown_skips_and_logs_reason():
    s = highlight.OnlineScorer(cooldown_s=600)
    s.last_event_t = 0.0
    f = feats(50.0, score=3.0, chars=20)
    assert s._emit(f) is None
    assert s.last_skip["reason"] == "cooldown"


def test_online_learned_bias_raises_the_bar():
    s = highlight.OnlineScorer(bias={"num": -0.6})
    f = feats(50.0, score=2.8, chars=20, num=1)
    assert s._emit(f) is None
    assert s.last_skip["reason"] == "learned_bias"
    # 同样的分数没有负偏置时应该能推
    s2 = highlight.OnlineScorer()
    assert s2._emit(feats(50.0, score=2.8, chars=20, num=1)) is not None


# ---------------------------------------------------------------- 音频能量

def _write_tone(path: Path, seconds: float, amp: float):
    import array
    n = int(16000 * seconds)
    data = array.array("h", [int(amp * 32767 * math.sin(2 * math.pi * 440 * i / 16000))
                             for i in range(n)])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(data.tobytes())


def test_audio_energy_features(tmp_path):
    wav = tmp_path / "tone.wav"
    _write_tone(wav, 6.0, 0.0)
    # 后半段更响
    import array
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        data = array.array("h")
        for i in range(16000 * 6):
            amp = 0.1 if i < 16000 * 3 else 0.8
            data.append(int(amp * 32767 * math.sin(2 * math.pi * 440 * i / 16000)))
        w.writeframes(data.tobytes())
    rows = [_row(0.0, "安静段发言", dur=2.0), _row(3.2, "响亮段发言", dur=2.0)]
    scored = highlight.score_rows(rows, wav)
    assert scored[0]["rms"] is not None and scored[1]["rms"] is not None
    assert scored[1]["rms"] > scored[0]["rms"]


def test_missing_audio_or_no_numpy_still_scores(tmp_path, monkeypatch):
    rows = [_row(0.0, "没有音频也能算文本分", dur=2.0)]
    assert highlight._energies(None) is None
    assert highlight._energies(tmp_path / "nope.wav") is None
    assert highlight.score_rows(rows)[0]["rms"] is None
    monkeypatch.setattr(highlight, "_np", None)
    out = highlight.score_rows(rows, tmp_path / "nope.wav")
    assert len(out) == 1 and out[0]["rms"] is None
    assert isinstance(out[0]["score"], float)
