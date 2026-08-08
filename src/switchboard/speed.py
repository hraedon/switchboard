"""Per-provider rolling speed statistics (Plan 020 Wave 3).

Time-to-first-byte, total duration, and tokens-per-second, sampled per
successful upstream response and held in a bounded rolling window per
provider.  Display-only state: it feeds ``/status.json``, ``/metrics``, and
the dashboard — it never influences a routing decision (that is the
pace-routing work in Wave 4, which would consume these aggregates via the
pure core).

Like :class:`~switchboard.overload.OverloadTracker`: mutable state keyed by
provider name (bounded by config, not by client input).  Timing is captured
in the proxy's streaming path as monotonic deltas — no response-body content
is read for this (token counts arrive from the existing, opt-in usage
observer, reused rather than duplicated).
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any


class SpeedSampler:
    """Bounded rolling window of per-provider response-speed samples.

    Each sample is ``(ttfb_ms, duration_ms, completion_tokens_or_None)``.
    Aggregates are computed on demand from the window; with a default of 128
    samples the sort is negligible.
    """

    def __init__(self, maxlen: int = 128) -> None:
        self._samples: dict[str, deque[tuple[float, float, float | None]]] = {}
        self._maxlen = maxlen

    def record(
        self,
        name: str,
        *,
        ttfb_ms: float,
        duration_ms: float,
        completion_tokens: int | None,
    ) -> None:
        """Record one successful-response sample for a provider."""
        w = self._samples.get(name)
        if w is None:
            w = deque(maxlen=self._maxlen)
            self._samples[name] = w
        w.append((ttfb_ms, duration_ms, completion_tokens))

    def summary(self, name: str) -> dict[str, Any] | None:
        """Aggregate speed stats for ``/status.json``, or None if no samples.

        * ``samples`` — how many observations the window holds.
        * ``ttfb_ms`` — avg / p50 / p95 of time-to-first-byte (request open →
          response headers received), in milliseconds.
        * ``duration_ms`` — mean total request duration.
        * ``tokens_per_sec`` — total completion tokens divided by total
          generation time (duration - ttfb), over samples that carried a token
          count.  ``None`` when no token-bearing samples exist.
        """
        w = self._samples.get(name)
        if not w:
            return None
        ttfbs = [s[0] for s in w]
        durations = [s[1] for s in w]
        # Only token-bearing samples with positive generation time feed the
        # rate — a zero-gen sample (same-tick timing) would add tokens to the
        # numerator and nothing to the denominator, inflating tokens_per_sec.
        tokened = [
            (s[1] - s[0], s[2])
            for s in w
            if s[2] is not None and s[2] > 0 and (s[1] - s[0]) > 0
        ]
        gen_seconds = sum(d / 1000.0 for d, _ in tokened)
        total_tokens = sum(t for _, t in tokened)
        tps = (total_tokens / gen_seconds) if gen_seconds > 0 else None
        return {
            "samples": len(w),
            "ttfb_ms": {
                "avg": round(sum(ttfbs) / len(ttfbs), 1),
                "p50": round(_percentile(ttfbs, 0.5), 1),
                "p95": round(_percentile(ttfbs, 0.95), 1),
            },
            "duration_ms": {
                "avg": round(sum(durations) / len(durations), 1),
            },
            "tokens_per_sec": round(tps, 1) if tps is not None else None,
        }


def _percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile of a non-empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # Nearest-rank: index = ceil(p * n) - 1, clamped.
    idx = max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))
    return ordered[idx]
