"""Unit tests for the per-provider speed sampler (Plan 020 Wave 3)."""

from __future__ import annotations

from switchboard.speed import SpeedSampler, _percentile


def test_summary_none_when_no_samples() -> None:
    s = SpeedSampler()
    assert s.summary("p") is None


def test_record_and_summary_shape() -> None:
    s = SpeedSampler()
    s.record("p", ttfb_ms=100.0, duration_ms=1000.0, completion_tokens=200)
    out = s.summary("p")
    assert out is not None
    assert out["samples"] == 1
    assert out["ttfb_ms"]["avg"] == 100.0
    assert out["duration_ms"]["avg"] == 1000.0
    # generation = 1000 - 100 = 900 ms = 0.9s; 200 tokens / 0.9s ≈ 222.2
    assert out["tokens_per_sec"] == 222.2


def test_tokens_per_sec_none_without_token_samples() -> None:
    s = SpeedSampler()
    s.record("p", ttfb_ms=50.0, duration_ms=500.0, completion_tokens=None)
    out = s.summary("p")
    assert out is not None
    assert out["tokens_per_sec"] is None
    assert out["ttfb_ms"]["avg"] == 50.0


def test_rolling_window_is_bounded() -> None:
    s = SpeedSampler(maxlen=3)
    for i in range(10):
        s.record("p", ttfb_ms=float(i), duration_ms=10.0, completion_tokens=None)
    out = s.summary("p")
    assert out is not None
    assert out["samples"] == 3  # only the last 3 retained
    # last three ttfbs: 7, 8, 9 → avg 8.0
    assert out["ttfb_ms"]["avg"] == 8.0


def test_providers_are_independent() -> None:
    s = SpeedSampler()
    s.record("a", ttfb_ms=10.0, duration_ms=100.0, completion_tokens=None)
    s.record("b", ttfb_ms=20.0, duration_ms=200.0, completion_tokens=None)
    assert s.summary("a")["ttfb_ms"]["avg"] == 10.0
    assert s.summary("b")["ttfb_ms"]["avg"] == 20.0


def test_percentile_basic() -> None:
    vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    # p50 nearest-rank: ceil(0.5*10)-1 = 4 → index 4 → 5.0
    assert _percentile(vals, 0.5) == 5.0
    # p95 nearest-rank: ceil(0.95*10)-1 = 9 → index 9 → 10.0
    assert _percentile(vals, 0.95) == 10.0


def test_percentile_single_value() -> None:
    assert _percentile([42.0], 0.5) == 42.0
    assert _percentile([42.0], 0.95) == 42.0


def test_tokens_per_sec_weighted_by_generation_time() -> None:
    """Aggregate tokens/sec is total tokens / total generation time (a
    generation-time-weighted mean), not a mean of per-sample rates."""
    s = SpeedSampler()
    # Sample 1: 900ms gen, 180 tokens -> 200 tps
    s.record("p", ttfb_ms=100.0, duration_ms=1000.0, completion_tokens=180)
    # Sample 2: 100ms gen, 40 tokens -> 400 tps
    s.record("p", ttfb_ms=100.0, duration_ms=200.0, completion_tokens=40)
    out = s.summary("p")
    assert out is not None
    # total tokens 220 / total gen (0.9 + 0.1 = 1.0s) = 220.0
    assert out["tokens_per_sec"] == 220.0


def test_tokens_per_sec_ignores_zero_generation_samples() -> None:
    """A same-tick sample (duration == ttfb, zero generation time) must not
    inflate tokens_per_sec: its tokens add nothing to the denominator and
    would otherwise pump the rate. It is excluded from the rate entirely."""
    s = SpeedSampler()
    # Healthy sample: 900ms gen, 180 tokens -> 200 tps.
    s.record("p", ttfb_ms=100.0, duration_ms=1000.0, completion_tokens=180)
    # Same-tick sample: 0ms gen, 999 tokens — must be dropped from the rate.
    s.record("p", ttfb_ms=500.0, duration_ms=500.0, completion_tokens=999)
    out = s.summary("p")
    assert out is not None
    assert out["tokens_per_sec"] == 200.0
