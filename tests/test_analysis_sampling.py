# -*- coding: utf-8 -*-
"""长直播总结：送进模型的句子要覆盖全场，而不是只取开头 350 句。"""
from __future__ import annotations

from app import analysis


def _rows(n: int, step: float = 10.0) -> list[dict]:
    return [{"id": i, "t_start": i * step, "t_end": i * step + 5,
             "text": "第%d句测试发言，内容足够长。" % i} for i in range(n)]


def test_short_session_keeps_every_row():
    rows = _rows(100)
    assert analysis.sample_rows(rows, 800) == rows


def test_long_session_covers_head_middle_and_tail():
    rows = _rows(3000)
    picked = analysis.sample_rows(rows, 300)
    ids = {r["id"] for r in picked}
    assert len(picked) <= 300
    assert 0 in ids                      # 开头
    assert rows[-1]["id"] in ids         # 结尾（最新一句必须在）
    assert any(1400 <= i <= 1600 for i in ids)      # 中段
    assert len({i // 500 for i in ids}) >= 5        # 不止开头一小段


def test_sample_rows_stays_time_sorted():
    picked = analysis.sample_rows(_rows(2000), 200)
    assert [r["t_start"] for r in picked] == sorted(r["t_start"] for r in picked)
