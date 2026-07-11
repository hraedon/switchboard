from __future__ import annotations

import httpx
import pytest
from sluice.control import BreakerConfig, ControllerConfig, LimitState
from sluice.gate import PermitGate
from sluice.providers import NullTruthSource, PolledTruthSource
from sluice.reconcile import ReconciliationLoop
from sluice.usage import CachedReading

from switchboard.control import Availability, ProviderState, SignalFreshness
from switchboard.providers import (
    ProviderContext,
    build_provider_context,
    build_provider_contexts_from_config,
    snapshot_provider_state,
)


def test_build_provider_context_creates_context_with_all_components() -> None:
    ctx = build_provider_context(
        name="test-provider",
        upstream_url="https://upstream.example.com",
        provider_type="generic",
        target=3,
    )
    assert ctx.name == "test-provider"
    assert ctx.upstream_url == "https://upstream.example.com"
    assert isinstance(ctx.gate, PermitGate)
    assert isinstance(ctx.reconcile, ReconciliationLoop)
    assert ctx.truth_source is not None
    assert isinstance(ctx.http_client, httpx.AsyncClient)


def test_build_provider_context_generic_uses_null_truth_source() -> None:
    ctx = build_provider_context(
        name="generic-1",
        upstream_url="https://upstream.example.com",
        provider_type="generic",
        target=1,
    )
    assert isinstance(ctx.truth_source, NullTruthSource)


def test_build_provider_context_umans_uses_polled_truth_source() -> None:
    ctx = build_provider_context(
        name="umans-1",
        upstream_url="https://api.code.umans.ai",
        provider_type="umans",
        target=3,
        usage_key="test-key",
    )
    assert isinstance(ctx.truth_source, PolledTruthSource)


def test_build_provider_context_gate_starts_at_zero() -> None:
    ctx = build_provider_context(
        name="test",
        upstream_url="https://upstream.example.com",
        provider_type="generic",
        target=3,
    )
    assert ctx.gate.capacity == 0
    assert ctx.gate.available == 0


def test_build_provider_contexts_from_config_builds_from_toml_dict() -> None:
    import os
    config = {
        "provider": {
            "umans-1": {
                "upstream": "https://api.code.umans.ai",
                "type": "umans",
                "target": 3,
                "usage_key_env": "TEST_USAGE_KEY",
            },
            "generic-1": {
                "upstream": "https://upstream.example.com",
                "type": "generic",
                "target": 1,
            },
        }
    }
    os.environ["TEST_USAGE_KEY"] = "secret-key"
    try:
        contexts = build_provider_contexts_from_config(config)
    finally:
        del os.environ["TEST_USAGE_KEY"]
    assert "umans-1" in contexts
    assert "generic-1" in contexts
    assert isinstance(contexts["umans-1"].truth_source, PolledTruthSource)
    assert isinstance(contexts["generic-1"].truth_source, NullTruthSource)


def test_build_provider_contexts_from_config_empty_returns_empty() -> None:
    assert build_provider_contexts_from_config({}) == {}


def test_build_provider_contexts_from_config_no_provider_section() -> None:
    assert build_provider_contexts_from_config({"route": {}}) == {}


@pytest.mark.asyncio
async def test_snapshot_provider_state_not_ready_is_unknown() -> None:
    gate = PermitGate(initial_capacity=0)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        controller_config=ControllerConfig(target=3),
        breaker_config=BreakerConfig(),
    )
    ctx = ProviderContext(
        name="test",
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )
    state = snapshot_provider_state("test", ctx, now=0.0)
    assert isinstance(state, ProviderState)
    assert state.name == "test"
    assert state.availability == Availability.UNKNOWN
    assert state.signal_freshness == SignalFreshness.UNKNOWN
    assert state.available_permits == 0
    assert state.queue_depth == 0
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_snapshot_provider_state_with_capacity_available() -> None:
    """WI-006.1: positive capacity + permits available = AVAILABLE."""
    gate = PermitGate(initial_capacity=5)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        controller_config=ControllerConfig(target=3),
        breaker_config=BreakerConfig(),
    )
    reconcile._first_poll_ok = True
    reconcile._last_reading_cached = CachedReading(
        reading=LimitState(provider="generic", age_seconds=0.0),
        fetched_at_monotonic=0.0,
        ok=True,
    )
    ctx = ProviderContext(
        name="test",
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )
    state = snapshot_provider_state("test", ctx, now=0.0)
    assert state.available_permits == 5
    assert state.availability == Availability.AVAILABLE
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_snapshot_provider_state_all_permits_held_is_busy() -> None:
    """WI-006.1: all permits held from positive-capacity gate = BUSY."""
    gate = PermitGate(initial_capacity=2)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        controller_config=ControllerConfig(target=2),
        breaker_config=BreakerConfig(),
    )
    reconcile._first_poll_ok = True
    reconcile._last_reading_cached = CachedReading(
        reading=LimitState(provider="generic", age_seconds=0.0),
        fetched_at_monotonic=0.0,
        ok=True,
    )
    ctx = ProviderContext(
        name="test",
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )
    # Acquire all permits.
    await ctx.gate.acquire(timeout=0.0)
    await ctx.gate.acquire(timeout=0.0)
    state = snapshot_provider_state("test", ctx, now=0.0)
    assert state.available_permits == 0
    assert state.availability == Availability.BUSY
    await ctx.gate.release()
    await ctx.gate.release()
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_snapshot_provider_state_null_truth_is_fresh() -> None:
    gate = PermitGate(initial_capacity=2)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        controller_config=ControllerConfig(target=2),
        breaker_config=BreakerConfig(),
    )
    reconcile._first_poll_ok = True
    reconcile._last_reading_cached = CachedReading(
        reading=LimitState(provider="generic", age_seconds=0.0),
        fetched_at_monotonic=0.0,
        ok=True,
    )
    ctx = ProviderContext(
        name="generic",
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )
    state = snapshot_provider_state("generic", ctx, now=0.0)
    assert state.signal_freshness == SignalFreshness.FRESH
    await ctx.http_client.aclose()
