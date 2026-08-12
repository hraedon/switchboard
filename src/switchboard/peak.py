"""Peak-window parsing and evaluation (Plan 025).

Some subscription plans price usage by time of day: z.ai's GLM Coding Plan
burns quota at the expensive rate Mon-Fri 14:00-18:00 Singapore time; Qwen's
token plan discounts 22:00-08:00 UTC+8 daily. The routing core must be able
to *demote* a provider during its peak window without learning to read a
clock — control.py is pure and receives only a monotonic ``now`` — so this
module lives in the shell: the specs are parsed at config ingestion, and
``snapshot_provider_state`` evaluates them against the wall clock to produce
the ``in_peak`` boolean the core consumes.

Window spec syntax (one string per window)::

    "<days> <start>-<end> <utcoffset>"
    "mon-fri 14:00-18:00 +08:00"      z.ai GLM Coding Plan peak
    "daily 08:00-22:00 +08:00"        Qwen token plan peak

- ``<days>``: ``daily``, one day (``mon``), an inclusive range (``mon-fri``,
  may wrap: ``sat-sun`` or ``fri-mon``), or a comma set (``mon,wed,fri``).
  Days are evaluated in the window's own timezone, not the host's.
- ``<start>-<end>``: 24-hour ``HH:MM``; start inclusive, end exclusive.
  ``end <= start`` crosses midnight; the day constraint applies to the
  window's *start* day.
- ``<utcoffset>``: mandatory fixed offset (``+08:00``, ``-07:00``, ``Z``).
  Fixed offsets only — the vendors that matter don't observe DST, and
  zoneinfo would make evaluation depend on host tz data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

__all__ = [
    "PeakWindow",
    "in_peak",
    "next_boundary",
    "parse_peak_windows",
]

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_SPEC_RE = re.compile(
    r"^\s*(?P<days>[a-z,\-]+)\s+"
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})-(?P<eh>\d{1,2}):(?P<em>\d{2})\s+"
    r"(?P<off>Z|[+-]\d{2}:\d{2})\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PeakWindow:
    """One parsed window: weekday set + minute-of-day range + fixed offset."""

    days: frozenset[int]        # Python weekday numbers (0=Mon .. 6=Sun)
    start_minute: int           # minutes since local midnight, inclusive
    end_minute: int             # exclusive; <= start_minute wraps midnight
    utc_offset_minutes: int
    spec: str                   # the original string, for display/round-trip


def _parse_days(text: str, spec: str) -> frozenset[int]:
    text = text.lower()
    if text == "daily":
        return frozenset(range(7))
    days: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ValueError(f"peak window {spec!r}: empty day in day set")
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            if lo_s not in _DAY_NAMES or hi_s not in _DAY_NAMES:
                raise ValueError(
                    f"peak window {spec!r}: unknown day in range {part!r} "
                    f"(use {', '.join(_DAY_NAMES)} or 'daily')"
                )
            lo, hi = _DAY_NAMES.index(lo_s), _DAY_NAMES.index(hi_s)
            # Inclusive range, wrapping so "fri-mon" means fri,sat,sun,mon.
            d = lo
            while True:
                days.add(d)
                if d == hi:
                    break
                d = (d + 1) % 7
        else:
            if part not in _DAY_NAMES:
                raise ValueError(
                    f"peak window {spec!r}: unknown day {part!r} "
                    f"(use {', '.join(_DAY_NAMES)} or 'daily')"
                )
            days.add(_DAY_NAMES.index(part))
    return frozenset(days)


def parse_peak_window(spec: str) -> PeakWindow:
    """Parse one window spec; raises ValueError naming the bad spec."""
    m = _SPEC_RE.match(spec)
    if m is None:
        raise ValueError(
            f"peak window {spec!r}: expected '<days> HH:MM-HH:MM <offset>', "
            "e.g. 'mon-fri 14:00-18:00 +08:00'"
        )
    days = _parse_days(m.group("days"), spec)
    sh, sm = int(m.group("sh")), int(m.group("sm"))
    eh, em = int(m.group("eh")), int(m.group("em"))
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        raise ValueError(f"peak window {spec!r}: hour/minute out of range")
    start = sh * 60 + sm
    end = eh * 60 + em
    if start == end:
        raise ValueError(
            f"peak window {spec!r}: start equals end (zero-length window)"
        )
    off_s = m.group("off")
    if off_s.upper() == "Z":
        offset = 0
    else:
        sign = 1 if off_s[0] == "+" else -1
        oh, om = int(off_s[1:3]), int(off_s[4:6])
        # ±14:00 is the largest real UTC offset; 14 with nonzero minutes
        # would exceed it.
        if oh > 14 or om > 59 or (oh == 14 and om != 0):
            raise ValueError(f"peak window {spec!r}: UTC offset out of range")
        offset = sign * (oh * 60 + om)
    return PeakWindow(
        days=days,
        start_minute=start,
        end_minute=end,
        utc_offset_minutes=offset,
        spec=spec.strip(),
    )


def parse_peak_windows(specs: list[str]) -> tuple[PeakWindow, ...]:
    """Parse a list of window specs (the config-facing entry point)."""
    return tuple(parse_peak_window(s) for s in specs)


def _local(now_epoch: float, window: PeakWindow) -> datetime:
    tz = timezone(timedelta(minutes=window.utc_offset_minutes))
    return datetime.fromtimestamp(now_epoch, tz=UTC).astimezone(tz)


def _window_active(window: PeakWindow, local: datetime) -> bool:
    minute = local.hour * 60 + local.minute
    if window.start_minute < window.end_minute:
        return (
            local.weekday() in window.days
            and window.start_minute <= minute < window.end_minute
        )
    # Cross-midnight: active from start on a listed day through end on the
    # NEXT day. Two cases: we're past start today (today must be listed), or
    # before end and the window started yesterday (yesterday must be listed).
    if minute >= window.start_minute:
        return local.weekday() in window.days
    if minute < window.end_minute:
        return (local.weekday() - 1) % 7 in window.days
    return False


def in_peak(windows: tuple[PeakWindow, ...], now_epoch: float) -> bool:
    """True when *now* falls inside any of the windows."""
    return any(_window_active(w, _local(now_epoch, w)) for w in windows)


def next_boundary(
    windows: tuple[PeakWindow, ...], now_epoch: float
) -> float | None:
    """Epoch of the next peak state change, or None without windows.

    Display-only (the provider card's countdown), so a minute-resolution
    scan is fine: walk forward minute by minute until the in_peak value
    flips, bounded at 8 days (a weekly pattern must repeat within 7).
    """
    if not windows:
        return None
    current = in_peak(windows, now_epoch)
    # Align to the next whole minute so the countdown lands on the boundary.
    t = (int(now_epoch) // 60 + 1) * 60
    limit = now_epoch + 8 * 86400
    while t <= limit:
        if in_peak(windows, float(t)) != current:
            return float(t)
        t += 60
    return None
