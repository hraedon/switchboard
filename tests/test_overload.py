"""Unit tests for the overloaded-response breaker (Plan 010 Feature A). Pure."""

from __future__ import annotations

import pytest

from switchboard.overload import OverloadConfig, OverloadTracker


def _tracker(**kw: float | int) -> OverloadTracker:
    return OverloadTracker(OverloadConfig(**kw))  # type: ignore[arg-type]


def test_below_threshold_does_not_open() -> None:
    t = _tracker(threshold=3)
    t.record_overloaded("umans", now=0.0)
    t.record_overloaded("umans", now=1.0)
    assert not t.is_cooling("umans", now=2.0)
    assert t.consecutive("umans") == 2


def test_threshold_opens_cooldown() -> None:
    t = _tracker(threshold=3, cooldown_default=30.0)
    for i in range(3):
        t.record_overloaded("umans", now=float(i))
    assert t.is_cooling("umans", now=2.0)
    assert t.cooldown_remaining("umans", now=2.0) == 30  # 2.0 + 30 - 2.0


def test_cooldown_lapses() -> None:
    t = _tracker(threshold=1, cooldown_default=10.0)
    t.record_overloaded("umans", now=100.0)
    assert t.is_cooling("umans", now=105.0)
    assert not t.is_cooling("umans", now=110.0)
    assert t.cooldown_remaining("umans", now=110.0) == 0


def test_ok_resets_counter_and_clears_cooldown() -> None:
    t = _tracker(threshold=2, cooldown_default=30.0)
    t.record_overloaded("umans", now=0.0)
    t.record_overloaded("umans", now=1.0)
    assert t.is_cooling("umans", now=2.0)
    t.record_ok("umans")
    assert not t.is_cooling("umans", now=2.0)
    assert t.consecutive("umans") == 0


def test_retry_after_drives_cooldown_within_bounds() -> None:
    t = _tracker(threshold=1, cooldown_default=30.0, cooldown_min=5.0, cooldown_max=300.0)
    t.record_overloaded("umans", now=0.0, retry_after=120.0)
    assert t.cooldown_remaining("umans", now=0.0) == 120


def test_retry_after_clamped_to_max() -> None:
    t = _tracker(threshold=1, cooldown_max=300.0)
    t.record_overloaded("umans", now=0.0, retry_after=9999.0)
    assert t.cooldown_remaining("umans", now=0.0) == 300


def test_retry_after_clamped_to_min() -> None:
    t = _tracker(threshold=1, cooldown_min=5.0)
    t.record_overloaded("umans", now=0.0, retry_after=1.0)
    assert t.cooldown_remaining("umans", now=0.0) == 5


def test_nonpositive_retry_after_uses_default() -> None:
    t = _tracker(threshold=1, cooldown_default=30.0)
    t.record_overloaded("umans", now=0.0, retry_after=0.0)
    assert t.cooldown_remaining("umans", now=0.0) == 30


def test_providers_are_independent() -> None:
    t = _tracker(threshold=2)
    t.record_overloaded("umans", now=0.0)
    t.record_overloaded("umans", now=1.0)
    t.record_overloaded("ollama-cloud", now=1.0)
    assert t.is_cooling("umans", now=2.0)
    assert not t.is_cooling("ollama-cloud", now=2.0)


def test_unknown_provider_is_not_cooling() -> None:
    t = _tracker()
    assert not t.is_cooling("nobody", now=0.0)
    assert t.cooldown_remaining("nobody", now=0.0) == 0
    assert t.consecutive("nobody") == 0


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        OverloadConfig(threshold=0)
    with pytest.raises(ValueError):
        OverloadConfig(cooldown_min=10.0, cooldown_default=5.0, cooldown_max=300.0)
