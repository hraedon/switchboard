"""Peak-window parsing and evaluation (Plan 025)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from switchboard.peak import (
    in_peak,
    next_boundary,
    parse_peak_window,
    parse_peak_windows,
)

_SGT = timezone(timedelta(hours=8))

# z.ai GLM Coding Plan peak: Mon-Fri 14:00-18:00 Singapore time.
ZAI = parse_peak_windows(["mon-fri 14:00-18:00 +08:00"])
# Qwen token plan peak: 08:00-22:00 daily UTC+8 (off-peak is the complement).
QWEN = parse_peak_windows(["daily 08:00-22:00 +08:00"])
# Cross-midnight window for wrap tests.
NIGHT = parse_peak_windows(["daily 22:00-08:00 +08:00"])


def _epoch(y: int, mo: int, d: int, h: int, mi: int, tz: timezone = _SGT) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_zai_window() -> None:
    (w,) = ZAI
    assert w.days == frozenset({0, 1, 2, 3, 4})
    assert w.start_minute == 14 * 60
    assert w.end_minute == 18 * 60
    assert w.utc_offset_minutes == 480
    assert w.spec == "mon-fri 14:00-18:00 +08:00"


def test_parse_day_forms() -> None:
    assert parse_peak_window("daily 01:00-02:00 Z").days == frozenset(range(7))
    assert parse_peak_window("wed 01:00-02:00 Z").days == frozenset({2})
    assert parse_peak_window("mon,wed,fri 01:00-02:00 Z").days == frozenset(
        {0, 2, 4}
    )
    # Wrapping day range: fri-mon = fri, sat, sun, mon.
    assert parse_peak_window("fri-mon 01:00-02:00 Z").days == frozenset(
        {4, 5, 6, 0}
    )


def test_parse_negative_offset() -> None:
    w = parse_peak_window("daily 09:00-17:00 -07:00")
    assert w.utc_offset_minutes == -420


@pytest.mark.parametrize(
    "bad",
    [
        "mon-fri 14:00-18:00",          # missing offset
        "mon-fri 14:00 +08:00",         # missing range
        "monday 14:00-18:00 +08:00",    # full day name
        "mon-fri 25:00-26:00 +08:00",   # hour out of range
        "mon-fri 14:00-14:00 +08:00",   # zero-length
        "mon-fri 14:00-18:00 +15:00",   # offset out of range
        "",                             # empty
        "peak",                         # nonsense
    ],
)
def test_parse_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_peak_window(bad)


def test_parse_windows_names_offender() -> None:
    with pytest.raises(ValueError, match="bogus"):
        parse_peak_windows(["mon-fri 14:00-18:00 +08:00", "bogus"])


# ---------------------------------------------------------------------------
# Evaluation — 2026-08-11 is a Tuesday
# ---------------------------------------------------------------------------


def test_in_peak_weekday_inside_window() -> None:
    assert in_peak(ZAI, _epoch(2026, 8, 11, 15, 0)) is True


def test_in_peak_start_inclusive_end_exclusive() -> None:
    assert in_peak(ZAI, _epoch(2026, 8, 11, 14, 0)) is True
    assert in_peak(ZAI, _epoch(2026, 8, 11, 17, 59)) is True
    assert in_peak(ZAI, _epoch(2026, 8, 11, 18, 0)) is False
    assert in_peak(ZAI, _epoch(2026, 8, 11, 13, 59)) is False


def test_in_peak_weekend_excluded() -> None:
    # 2026-08-15 is a Saturday; 15:00 SGT would be peak on a weekday.
    assert in_peak(ZAI, _epoch(2026, 8, 15, 15, 0)) is False


def test_in_peak_evaluates_in_window_timezone() -> None:
    # 07:00 UTC on a Tuesday is 15:00 SGT — inside the window even though
    # the UTC hour (07) is not.
    ts = datetime(2026, 8, 11, 7, 0, tzinfo=UTC).timestamp()
    assert in_peak(ZAI, ts) is True


def test_cross_midnight_window() -> None:
    # daily 22:00-08:00 SGT: 23:00 in, 03:00 in (started yesterday), 12:00 out.
    assert in_peak(NIGHT, _epoch(2026, 8, 11, 23, 0)) is True
    assert in_peak(NIGHT, _epoch(2026, 8, 12, 3, 0)) is True
    assert in_peak(NIGHT, _epoch(2026, 8, 11, 12, 0)) is False
    # End exclusive across the wrap.
    assert in_peak(NIGHT, _epoch(2026, 8, 12, 8, 0)) is False


def test_cross_midnight_day_constraint_applies_to_start() -> None:
    # fri 22:00-08:00: Saturday 03:00 is in (Friday's window), Sunday 03:00
    # is out (Saturday is not listed).
    w = parse_peak_windows(["fri 22:00-08:00 +08:00"])
    assert in_peak(w, _epoch(2026, 8, 15, 3, 0)) is True   # Sat 03:00
    assert in_peak(w, _epoch(2026, 8, 16, 3, 0)) is False  # Sun 03:00


def test_multiple_windows_any_match() -> None:
    both = ZAI + QWEN
    # Saturday 10:00 SGT: outside zai (weekend) but inside qwen daily peak.
    assert in_peak(both, _epoch(2026, 8, 15, 10, 0)) is True


# ---------------------------------------------------------------------------
# next_boundary (display countdown)
# ---------------------------------------------------------------------------


def test_next_boundary_none_without_windows() -> None:
    assert next_boundary((), 0.0) is None


def test_next_boundary_from_inside_window() -> None:
    now = _epoch(2026, 8, 11, 15, 0)
    boundary = next_boundary(ZAI, now)
    assert boundary == _epoch(2026, 8, 11, 18, 0)


def test_next_boundary_from_outside_window() -> None:
    now = _epoch(2026, 8, 11, 19, 0)  # Tuesday evening → Wednesday 14:00
    boundary = next_boundary(ZAI, now)
    assert boundary == _epoch(2026, 8, 12, 14, 0)


def test_next_boundary_rolls_over_weekend() -> None:
    now = _epoch(2026, 8, 14, 19, 0)  # Friday evening → Monday 14:00
    boundary = next_boundary(ZAI, now)
    assert boundary == _epoch(2026, 8, 17, 14, 0)
