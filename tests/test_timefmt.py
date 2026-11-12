# -*- coding: utf-8 -*-
"""时间换算：录制当时时区优先、跨零点日期不省、录音内偏移格式。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import timefmt


YEAR = datetime.now(timefmt.CN_TZ).year


def _session(**kw):
    return kw


# ---------------------------------------------------------------- 起点

def test_started_utc_wins_over_everything():
    s = _session(started_utc="2026-11-05T09:04:38Z",
                 started_at="2020-01-01T00:00:00", tz_offset_min=0)
    assert timefmt.start_utc(s) == datetime(2026, 11, 5, 9, 4, 38, tzinfo=timezone.utc)


def test_naive_started_utc_is_treated_as_utc():
    s = _session(started_utc="2026-11-05T09:04:38")
    assert timefmt.start_utc(s).utcoffset() == timedelta(0)


def test_started_at_uses_stored_offset_not_machine_tz():
    """录制时存下的 +480 才是准绳：机器搬到 UTC 机房也不能变。"""
    s = _session(started_at="2026-11-05T17:04:38", tz_offset_min=480)
    assert timefmt.start_utc(s) == datetime(2026, 11, 5, 9, 4, 38, tzinfo=timezone.utc)
    s = _session(started_at="2026-11-05T17:04:38", tz_offset_min=-300)
    assert timefmt.start_utc(s) == datetime(2026, 11, 5, 22, 4, 38, tzinfo=timezone.utc)


def test_explicit_tz_in_started_at_wins_over_offset_column():
    s = _session(started_at="2026-11-05T17:04:38+08:00", tz_offset_min=0)
    assert timefmt.start_utc(s) == datetime(2026, 11, 5, 9, 4, 38, tzinfo=timezone.utc)


def test_bad_stored_offset_falls_back_to_local_interpreting():
    raw = "2026-11-05T17:04:38"
    s = _session(started_at=raw, tz_offset_min="east")
    want = datetime.fromisoformat(raw).astimezone(timezone.utc)
    assert timefmt.start_utc(s) == want


def test_missing_or_broken_session_never_raises():
    for s in (None, {}, _session(started_at="not a date"),
              _session(started_utc="???", started_at="???")):
        got = timefmt.start_utc(s)
        assert got.tzinfo is not None
        assert abs((datetime.now(timezone.utc) - got).total_seconds()) < 60


def test_wall_clamps_negative_and_invalid_offsets():
    s = _session(started_utc="2026-11-05T09:04:38Z")
    assert timefmt.wall(s, -30)["cn"].hour == 17
    assert timefmt.wall(s, "x")["utc"].hour == 9
    w = timefmt.wall(s, 3600)
    assert w["cn"].strftime("%H:%M:%S") == "18:04:38"
    assert w["utc"].strftime("%H:%M:%S") == "10:04:38"


# ---------------------------------------------------------------- 文案

def test_clock_line_same_day_only_prints_utc_time():
    s = _session(started_utc=f"{YEAR}-11-05T09:04:38Z")
    assert timefmt.clock_line(s, 0) == "11-05 17:04:38 北京 / 09:04:38 UTC"


def test_clock_line_cross_midnight_keeps_both_dates():
    s = _session(started_utc=f"{YEAR}-11-05T15:30:00Z")   # 北京 23:30
    line = timefmt.clock_line(s, 3000)                     # 北京次日 00:20
    assert line == "11-06 00:20:00 北京 / 11-05 16:20:00 UTC"


def test_clock_pair_returns_full_strings():
    s = _session(started_utc=f"{YEAR}-11-05T09:04:38Z")
    cn, utc = timefmt.clock_pair(s, 0)
    assert cn == "11-05 17:04:38" and utc == "11-05 09:04:38"


def test_offset_format_switches_to_hours():
    assert timefmt.offset(0) == "0:00"
    assert timefmt.offset(69) == "1:09"
    assert timefmt.offset("12.7") == "0:12"
    assert timefmt.offset(3599) == "59:59"
    assert timefmt.offset(3600) == "1:00:00"
    assert timefmt.offset(7503) == "2:05:03"
    assert timefmt.offset(-5) == "0:00"
    assert timefmt.offset(None) == "0:00"
    assert timefmt.offset("oops") == "0:00"


# ---------------------------------------------------------------- 简报用的北京时间

def test_hm_span_uses_beijing_and_collapses_same_day():
    s = _session(started_utc=f"{YEAR}-11-05T09:04:38Z")   # 北京 17:04:38
    assert timefmt.hm_span(s, 0, 600) == ("17:04", "17:14")


def test_hm_span_marks_next_day_instead_of_ambiguous_hhmm():
    s = _session(started_utc=f"{YEAR}-11-05T15:30:00Z")   # 北京 23:30
    assert timefmt.hm_span(s, 1800, 2400) == ("次日 00:00", "次日 00:10")
    assert timefmt.hm_span(s, 90000, 90060) == ("11-07 00:30", "11-07 00:31")


def test_start_hm_is_beijing_minute_with_date():
    s = _session(started_utc=f"{YEAR}-11-05T09:04:38Z")
    assert timefmt.start_hm(s) == "11-05 17:04"


def test_local_offset_min_is_aligned_to_minutes():
    off = timefmt.local_offset_min()
    assert isinstance(off, int)
    assert abs(off) <= 14 * 60
