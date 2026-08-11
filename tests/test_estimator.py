"""Tests for the threshold estimator shell wiring (Plan 010 Feature C — WI-10)."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from typing import Any

import httpx

from switchboard.estimator import ThresholdEstimator
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig, CachedReading, LimitState
from switchboard.providers import ProviderContext
from switchboard.proxy import RoutingMetrics
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource

_WALL_NOW = 1_000_000.0
_FUTURE_RESET = _WALL_NOW + 3600.0


def _close_ctxs(*ctxs: ProviderContext) -> None:
    for ctx in ctxs:
        asyncio.run(ctx.http_client.aclose())


def _make_ctx_with_reading(
    *,
    requests_in_window: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    concurrent_sessions: int = 1,
    service_mode: str | None = None,
    service_mode_resets_at_epoch: float | None = None,
    ok: bool = True,
    wall_clock: Callable[[], float] | None = None,
) -> ProviderContext:
    """Build a ProviderContext with a specific cached reading."""
    gate = PermitGate(initial_capacity=3)
    truth = NullTruthSource(provider="umans")
    kwargs: dict[str, Any] = dict(
        truth_source=truth,
        gate=gate,
        max_concurrency=3,
        breaker_config=BreakerConfig(),
    )
    if wall_clock is not None:
        kwargs["wall_clock"] = wall_clock
    reconcile = ReconciliationLoop(**kwargs)
    reconcile._first_poll_ok = True
    reading = LimitState(
        provider="umans",
        age_seconds=0.0,
        requests_in_window=requests_in_window,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        concurrent_sessions=concurrent_sessions,
        service_mode=service_mode,
        service_mode_resets_at_epoch=service_mode_resets_at_epoch,
    )
    reconcile._last_reading_cached = CachedReading(
        reading=reading,
        fetched_at_monotonic=0.0,
        ok=ok,
    )
    client = httpx.AsyncClient()
    return ProviderContext(
        name="umans",
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=client,
    )


def _make_ctx_no_reading() -> ProviderContext:
    """Build a ProviderContext with no cached reading."""
    gate = PermitGate(initial_capacity=3)
    truth = NullTruthSource(provider="umans")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        max_concurrency=3,
        breaker_config=BreakerConfig(),
    )
    reconcile._first_poll_ok = True
    client = httpx.AsyncClient()
    return ProviderContext(
        name="umans",
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=client,
    )


def test_basic_update_off_then_on() -> None:
    """Feed OFF then ON samples within one window. Estimate should tighten."""
    db = sqlite3.connect(":memory:")
    est = ThresholdEstimator(provider_name="umans", db=db)

    ctx_off = _make_ctx_with_reading(
        requests_in_window=100,
        tokens_in=1000,
        tokens_out=500,
        concurrent_sessions=2,
        service_mode=None,
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    result = est.maybe_sample(ctx_off)
    assert result is not None
    assert result.estimate.edges == 0

    ctx_on = _make_ctx_with_reading(
        requests_in_window=120,
        tokens_in=1300,
        tokens_out=600,
        concurrent_sessions=4,
        service_mode="low_interactivity",
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    result = est.maybe_sample(ctx_on)
    assert result is not None
    assert result.estimate.edges == 1
    assert result.estimate.requests.lower == 100
    assert result.estimate.requests.upper == 120
    assert result.estimate.requests.best_guess == 110
    assert result.estimate.tokens.lower == 1500
    assert result.estimate.tokens.upper == 1900
    assert result.estimate.last_edge_concurrent_sessions == 4

    _close_ctxs(ctx_off, ctx_on)
    db.close()


def test_persistence_round_trip() -> None:
    """State saved to SQLite is restored by a new estimator with load()."""
    db = sqlite3.connect(":memory:")
    est1 = ThresholdEstimator(provider_name="umans", db=db)

    ctx_off = _make_ctx_with_reading(
        requests_in_window=50,
        tokens_in=500,
        tokens_out=200,
        concurrent_sessions=1,
        service_mode=None,
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    est1.maybe_sample(ctx_off)

    ctx_on = _make_ctx_with_reading(
        requests_in_window=80,
        tokens_in=800,
        tokens_out=300,
        concurrent_sessions=3,
        service_mode="low_interactivity",
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    est1.maybe_sample(ctx_on)

    state1 = est1.state()
    assert state1.estimate.edges == 1

    est2 = ThresholdEstimator(provider_name="umans", db=db)
    est2.load()
    state2 = est2.state()

    assert state2.estimate.edges == state1.estimate.edges
    assert state2.estimate.requests.lower == state1.estimate.requests.lower
    assert state2.estimate.requests.upper == state1.estimate.requests.upper
    assert state2.estimate.tokens.lower == state1.estimate.tokens.lower
    assert state2.estimate.tokens.upper == state1.estimate.tokens.upper
    assert (
        state2.estimate.last_edge_concurrent_sessions
        == state1.estimate.last_edge_concurrent_sessions
    )
    assert state2.window is not None
    assert state2.window.window_id == state1.window.window_id

    _close_ctxs(ctx_off, ctx_on)
    db.close()


def test_window_transition() -> None:
    """Samples from two different windows should produce two edges."""
    db = sqlite3.connect(":memory:")
    est = ThresholdEstimator(provider_name="umans", db=db)

    reset1 = _WALL_NOW + 1800.0
    reset2 = _WALL_NOW + 5400.0

    ctx_off_w1 = _make_ctx_with_reading(
        requests_in_window=90,
        tokens_in=900,
        tokens_out=100,
        service_mode=None,
        service_mode_resets_at_epoch=reset1,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_off_w1)

    ctx_on_w1 = _make_ctx_with_reading(
        requests_in_window=150,
        tokens_in=1500,
        tokens_out=200,
        concurrent_sessions=2,
        service_mode="low_interactivity",
        service_mode_resets_at_epoch=reset1,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_on_w1)

    ctx_off_w2 = _make_ctx_with_reading(
        requests_in_window=105,
        tokens_in=1050,
        tokens_out=120,
        service_mode=None,
        service_mode_resets_at_epoch=reset2,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_off_w2)

    ctx_on_w2 = _make_ctx_with_reading(
        requests_in_window=130,
        tokens_in=1300,
        tokens_out=150,
        concurrent_sessions=3,
        service_mode="low_interactivity",
        service_mode_resets_at_epoch=reset2,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_on_w2)

    state = est.state()
    assert state.estimate.edges == 2
    assert state.estimate.requests.lower == 105
    assert state.estimate.requests.upper == 130
    assert state.estimate.requests.best_guess == 117
    assert state.estimate.tokens.lower == 1170
    assert state.estimate.tokens.upper == 1450

    _close_ctxs(ctx_off_w1, ctx_on_w1, ctx_off_w2, ctx_on_w2)
    db.close()


def test_no_reading_returns_none() -> None:
    """maybe_sample with no reading should return None and leave state unchanged."""
    est = ThresholdEstimator(provider_name="umans")

    ctx = _make_ctx_no_reading()
    before = est.state()
    result = est.maybe_sample(ctx)
    assert result is None
    after = est.state()
    assert after is before

    _close_ctxs(ctx)


def test_stale_reading_returns_none() -> None:
    """maybe_sample with ok=False should return None and leave state unchanged."""
    est = ThresholdEstimator(provider_name="umans")

    ctx = _make_ctx_with_reading(
        requests_in_window=100,
        service_mode="low_interactivity",
        service_mode_resets_at_epoch=_FUTURE_RESET,
        ok=False,
        wall_clock=lambda: _WALL_NOW,
    )
    before = est.state()
    result = est.maybe_sample(ctx)
    assert result is None
    after = est.state()
    assert after is before

    _close_ctxs(ctx)


def test_no_resets_at_epoch_returns_none() -> None:
    """maybe_sample with resets_at_epoch=None should return None."""
    est = ThresholdEstimator(provider_name="umans")

    ctx = _make_ctx_with_reading(
        requests_in_window=100,
        service_mode=None,
        service_mode_resets_at_epoch=None,
        wall_clock=lambda: _WALL_NOW,
    )
    result = est.maybe_sample(ctx)
    assert result is None

    _close_ctxs(ctx)


def test_zero_resets_at_epoch_returns_none() -> None:
    """maybe_sample with resets_at_epoch=0.0 should return None."""
    est = ThresholdEstimator(provider_name="umans")

    ctx = _make_ctx_with_reading(
        requests_in_window=100,
        service_mode=None,
        service_mode_resets_at_epoch=0.0,
        wall_clock=lambda: _WALL_NOW,
    )
    result = est.maybe_sample(ctx)
    assert result is None

    _close_ctxs(ctx)


def test_wrong_provider_returns_none() -> None:
    """maybe_sample with a context for a different provider should return None."""
    est = ThresholdEstimator(provider_name="umans")

    ctx = _make_ctx_with_reading(
        requests_in_window=100,
        service_mode=None,
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    ctx.name = "ollama"
    result = est.maybe_sample(ctx)
    assert result is None

    _close_ctxs(ctx)


def test_status_payload_includes_estimator() -> None:
    """_build_status_payload includes estimator section when estimator is provided."""
    db = sqlite3.connect(":memory:")
    est = ThresholdEstimator(provider_name="umans", db=db)

    ctx_off = _make_ctx_with_reading(
        requests_in_window=100,
        tokens_in=1000,
        tokens_out=500,
        concurrent_sessions=2,
        service_mode=None,
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_off)

    ctx_on = _make_ctx_with_reading(
        requests_in_window=120,
        tokens_in=1300,
        tokens_out=600,
        concurrent_sessions=4,
        service_mode="low_interactivity",
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_on)

    from switchboard.admin import _build_status_payload as _bsp

    no_reading_ctx = _make_ctx_no_reading()
    providers = {"umans": no_reading_ctx}
    mgr = RouteTableManager(default_providers=("umans",))
    metrics = RoutingMetrics()
    payload = _bsp(providers, mgr, metrics, estimator=est)

    assert "estimator" in payload
    est_data = payload["estimator"]
    assert est_data["edges"] == 1
    assert est_data["requests"]["lower"] == 100
    assert est_data["requests"]["upper"] == 120
    assert est_data["requests"]["best_guess"] == 110
    assert est_data["requests"]["contradicted"] is False
    assert est_data["tokens"]["lower"] == 1500
    assert est_data["tokens"]["upper"] == 1900
    assert est_data["last_edge_concurrent_sessions"] == 4

    _close_ctxs(ctx_off, ctx_on, no_reading_ctx)
    db.close()


def test_status_payload_without_estimator() -> None:
    """_build_status_payload omits estimator section when estimator is None."""
    from switchboard.admin import _build_status_payload as _bsp

    no_reading_ctx = _make_ctx_no_reading()
    providers = {"umans": no_reading_ctx}
    mgr = RouteTableManager(default_providers=("umans",))
    metrics = RoutingMetrics()
    payload = _bsp(providers, mgr, metrics, estimator=None)

    assert "estimator" not in payload

    _close_ctxs(no_reading_ctx)


def test_event_persistence() -> None:
    """Threshold events are persisted to SQLite and queryable."""
    db = sqlite3.connect(":memory:")
    est = ThresholdEstimator(provider_name="umans", db=db)

    ctx_off = _make_ctx_with_reading(
        requests_in_window=100,
        tokens_in=1000,
        tokens_out=500,
        concurrent_sessions=2,
        service_mode=None,
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_off)

    ctx_on = _make_ctx_with_reading(
        requests_in_window=120,
        tokens_in=1300,
        tokens_out=600,
        concurrent_sessions=4,
        service_mode="low_interactivity",
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_on)

    events = est.recent_events(limit=10)
    assert len(events) == 1
    assert events[0]["triggered"] is True
    assert events[0]["requests"] == 120
    assert events[0]["tokens"] == 1900
    assert events[0]["concurrent_sessions"] == 4

    summary = est.event_summary()
    assert summary["trigger_count"] == 1
    assert summary["non_trigger_count"] == 0
    assert summary["total_events"] == 1

    _close_ctxs(ctx_off, ctx_on)
    db.close()


def test_non_trigger_event_persistence() -> None:
    """Non-trigger events are persisted when a window ends without low-interactivity."""
    db = sqlite3.connect(":memory:")
    est = ThresholdEstimator(provider_name="umans", db=db)

    reset1 = _WALL_NOW + 1800.0
    reset2 = _WALL_NOW + 5400.0

    ctx_off_w1 = _make_ctx_with_reading(
        requests_in_window=90,
        tokens_in=900,
        tokens_out=100,
        service_mode=None,
        service_mode_resets_at_epoch=reset1,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_off_w1)

    # New window — w1 had OFF but no edge → non-trigger event
    ctx_off_w2 = _make_ctx_with_reading(
        requests_in_window=50,
        tokens_in=500,
        tokens_out=100,
        service_mode=None,
        service_mode_resets_at_epoch=reset2,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_off_w2)

    events = est.recent_events(limit=10)
    assert len(events) == 1
    assert events[0]["triggered"] is False
    assert events[0]["requests"] == 90
    assert events[0]["tokens"] == 1000

    summary = est.event_summary()
    assert summary["trigger_count"] == 0
    assert summary["non_trigger_count"] == 1

    _close_ctxs(ctx_off_w1, ctx_off_w2)
    db.close()


def test_event_pruning() -> None:
    """Old events are pruned by prune_events()."""
    db = sqlite3.connect(":memory:")
    est = ThresholdEstimator(provider_name="umans", db=db)

    ctx_off = _make_ctx_with_reading(
        requests_in_window=100,
        tokens_in=1000,
        tokens_out=500,
        service_mode=None,
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_off)

    ctx_on = _make_ctx_with_reading(
        requests_in_window=120,
        tokens_in=1300,
        tokens_out=600,
        concurrent_sessions=4,
        service_mode="low_interactivity",
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_on)

    assert len(est.recent_events()) == 1

    # Prune everything older than 0 seconds (i.e. all events)
    est.prune_events(max_age_seconds=0.0)

    assert len(est.recent_events()) == 0

    _close_ctxs(ctx_off, ctx_on)
    db.close()


def test_status_payload_includes_events() -> None:
    """_build_status_payload includes threshold events in the estimator section."""
    db = sqlite3.connect(":memory:")
    est = ThresholdEstimator(provider_name="umans", db=db)

    ctx_off = _make_ctx_with_reading(
        requests_in_window=100,
        tokens_in=1000,
        tokens_out=500,
        concurrent_sessions=2,
        service_mode=None,
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_off)

    ctx_on = _make_ctx_with_reading(
        requests_in_window=120,
        tokens_in=1300,
        tokens_out=600,
        concurrent_sessions=4,
        service_mode="low_interactivity",
        service_mode_resets_at_epoch=_FUTURE_RESET,
        wall_clock=lambda: _WALL_NOW,
    )
    est.maybe_sample(ctx_on)

    from switchboard.admin import _build_status_payload as _bsp

    no_reading_ctx = _make_ctx_no_reading()
    providers = {"umans": no_reading_ctx}
    mgr = RouteTableManager(default_providers=("umans",))
    metrics = RoutingMetrics()
    payload = _bsp(providers, mgr, metrics, estimator=est)

    assert "estimator" in payload
    assert "events" in payload["estimator"]
    assert payload["estimator"]["events"]["trigger_count"] == 1
    assert payload["estimator"]["events"]["total_events"] == 1
    assert len(payload["estimator"]["events"]["events"]) == 1

    _close_ctxs(ctx_off, ctx_on, no_reading_ctx)
    db.close()


def test_dimension_estimate_adjacent_bounds_return_trigger_value() -> None:
    """Adjacent integer bounds (lower=4, upper=5) must return upper — the
    proven-trigger value — not floor division which returns the proven-
    non-trigger lower bound."""
    from switchboard.threshold import DimensionEstimate

    adj = DimensionEstimate(lower=4, upper=5, edges=2)
    assert adj.best_guess == 5

    wide = DimensionEstimate(lower=100, upper=120, edges=2)
    assert wide.best_guess == 110
