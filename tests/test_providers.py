from __future__ import annotations

import time

import httpx
import pytest

from switchboard.control import Availability, ProviderState, SignalFreshness
from switchboard.dashboard import DashboardTruthSource
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig, CachedReading, LimitState
from switchboard.overload import OverloadConfig, OverloadTracker
from switchboard.providers import (
    ProviderContext,
    _safe_filename,
    build_provider_context,
    build_provider_contexts_from_config,
    snapshot_provider_state,
)
from switchboard.reconcile import ReconciliationLoop
from switchboard.truth import NullTruthSource, PolledTruthSource


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
        max_concurrency=3,
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
        max_concurrency=3,
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
        max_concurrency=2,
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
        max_concurrency=2,
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


def _make_ready_ctx(
    name: str = "test",
    capacity: int = 3,
    *,
    requests_remaining: int | None = None,
    requests_limit: int | None = None,
    bucket_reset_epoch: float | None = None,
) -> ProviderContext:
    """Build a ProviderContext with a ready reconcile loop and positive gate."""
    gate = PermitGate(initial_capacity=capacity)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        max_concurrency=capacity,
        breaker_config=BreakerConfig(),
    )
    reconcile._first_poll_ok = True
    reconcile._last_reading_cached = CachedReading(
        reading=LimitState(
            provider="generic",
            age_seconds=0.0,
            requests_remaining=requests_remaining,
            requests_limit=requests_limit,
            bucket_reset_epoch=bucket_reset_epoch,
        ),
        fetched_at_monotonic=0.0,
        ok=True,
    )
    return ProviderContext(
        name=name,
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )


@pytest.mark.asyncio
async def test_snapshot_overload_cooling_is_closed() -> None:
    """Plan 010 Feature A: overload tracker cooling → CLOSED."""
    ctx = _make_ready_ctx("umans")
    tracker = OverloadTracker(OverloadConfig(threshold=1, cooldown_default=60.0))
    tracker.record_overloaded("umans", now=100.0)
    assert tracker.is_cooling("umans", now=100.0)

    state = snapshot_provider_state(
        "umans", ctx, now=100.0, overload_tracker=tracker,
    )
    assert state.availability == Availability.CLOSED
    assert state.retry_after_seconds is not None
    assert state.retry_after_seconds > 0
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_snapshot_overload_not_cooling_is_available() -> None:
    """Tracker present but not cooling → normal AVAILABLE state."""
    ctx = _make_ready_ctx("umans")
    tracker = OverloadTracker(OverloadConfig(threshold=3))
    # Not enough consecutive overloads to open cooldown.
    tracker.record_overloaded("umans", now=0.0)

    state = snapshot_provider_state(
        "umans", ctx, now=0.0, overload_tracker=tracker,
    )
    assert state.availability == Availability.AVAILABLE
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_snapshot_overload_cooldown_lapsed_is_available() -> None:
    """Cooldown has lapsed → provider is AVAILABLE again."""
    ctx = _make_ready_ctx("umans")
    tracker = OverloadTracker(OverloadConfig(threshold=1, cooldown_default=10.0))
    tracker.record_overloaded("umans", now=0.0)
    # Cooldown was [0, 10); now is 20 → lapsed.
    state = snapshot_provider_state(
        "umans", ctx, now=20.0, overload_tracker=tracker,
    )
    assert state.availability == Availability.AVAILABLE
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_snapshot_overload_takes_precedence_over_gate_state() -> None:
    """Even with capacity available, overload cooling → CLOSED."""
    ctx = _make_ready_ctx("umans", capacity=5)
    tracker = OverloadTracker(OverloadConfig(threshold=1, cooldown_default=30.0))
    tracker.record_overloaded("umans", now=0.0)

    state = snapshot_provider_state(
        "umans", ctx, now=0.0, overload_tracker=tracker,
    )
    assert state.availability == Availability.CLOSED
    assert state.available_permits == 5  # gate still has capacity
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_snapshot_no_tracker_behaves_as_before() -> None:
    """No overload_tracker → no overload check, backward-compatible."""
    ctx = _make_ready_ctx("umans")
    state = snapshot_provider_state("umans", ctx, now=0.0)
    assert state.availability == Availability.AVAILABLE
    await ctx.http_client.aclose()


# --- Plan 011: usage_headroom derivation tests ---


@pytest.mark.asyncio
async def test_headroom_derived_from_requests() -> None:
    ctx = _make_ready_ctx("umans", requests_remaining=5, requests_limit=100)
    state = snapshot_provider_state("umans", ctx, now=0.0)
    assert state.usage_headroom is not None
    assert abs(state.usage_headroom - 0.05) < 1e-9
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_headroom_none_when_no_reading() -> None:
    ctx = _make_ready_ctx("umans")
    ctx.reconcile._last_reading_cached = None
    state = snapshot_provider_state("umans", ctx, now=0.0)
    assert state.usage_headroom is None
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_headroom_none_when_stale_reading() -> None:
    ctx = _make_ready_ctx("umans", requests_remaining=5, requests_limit=100)
    cached = ctx.reconcile._last_reading_cached
    assert cached is not None
    ctx.reconcile._last_reading_cached = CachedReading(
        reading=cached.reading,
        fetched_at_monotonic=cached.fetched_at_monotonic,
        ok=False,
    )
    state = snapshot_provider_state("umans", ctx, now=0.0)
    assert state.usage_headroom is None
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_headroom_none_when_requests_remaining_none() -> None:
    ctx = _make_ready_ctx("umans", requests_remaining=None, requests_limit=100)
    state = snapshot_provider_state("umans", ctx, now=0.0)
    assert state.usage_headroom is None
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_headroom_none_when_requests_limit_zero() -> None:
    ctx = _make_ready_ctx("umans", requests_remaining=5, requests_limit=0)
    state = snapshot_provider_state("umans", ctx, now=0.0)
    assert state.usage_headroom is None
    await ctx.http_client.aclose()


# --- Plan 015: z.ai onboarding config-path test ---


def test_build_provider_contexts_from_config_zai_dashboard_truth_source() -> None:
    """A [provider.\"zai\"] section with type=openai + dashboard_url builds a
    DashboardTruthSource with provider_name == \"zai\"."""
    config = {
        "provider": {
            "zai": {
                "upstream": "https://api.z.ai/api/paas/v4",
                "type": "openai",
                "target": 2,
                "dashboard_url": "http://usage-dashboard.example.com",
                "dashboard_token_env": "USAGE_DASHBOARD_TOKEN",
            },
        }
    }
    import os
    os.environ["USAGE_DASHBOARD_TOKEN"] = "dashboard-secret"
    try:
        contexts = build_provider_contexts_from_config(config)
    finally:
        del os.environ["USAGE_DASHBOARD_TOKEN"]

    assert "zai" in contexts
    ctx = contexts["zai"]
    assert isinstance(ctx.truth_source, DashboardTruthSource)
    assert ctx.truth_source._provider_name == "zai"


def test_build_provider_contexts_parses_peak_windows() -> None:
    """`peak_windows` specs parse at construction (Plan 025)."""
    config = {
        "provider": {
            "zai": {
                "upstream": "https://api.z.ai/api/coding/paas/v4",
                "type": "generic",
                "target": 2,
                "peak_windows": ["mon-fri 14:00-18:00 +08:00"],
            },
        }
    }
    contexts = build_provider_contexts_from_config(config)
    (w,) = contexts["zai"].peak_windows
    assert w.spec == "mon-fri 14:00-18:00 +08:00"
    assert w.utc_offset_minutes == 480


def test_build_provider_contexts_rejects_bad_peak_window() -> None:
    """A bad spec fails construction naming the provider — a window that
    silently didn't parse would mean expensive burn with no sign why."""
    config = {
        "provider": {
            "zai": {
                "upstream": "https://api.z.ai/api/coding/paas/v4",
                "type": "generic",
                "target": 2,
                "peak_windows": ["whenever it's cheap"],
            },
        }
    }
    with pytest.raises(ValueError, match="zai"):
        build_provider_contexts_from_config(config)


@pytest.mark.asyncio
async def test_snapshot_in_peak_from_windows() -> None:
    """snapshot_provider_state evaluates the context's windows against the
    wall clock; a daily 00:00-23:59 Z window is (all but one minute) always
    active, an always-empty window set never is."""
    import dataclasses

    from switchboard.peak import parse_peak_windows

    ctx = _make_ready_ctx("zai")
    state = snapshot_provider_state("zai", ctx, now=0.0)
    assert state.in_peak is False  # no windows configured

    # Two complementary cross-midnight halves cover every instant, so the
    # assertion cannot flake on when the suite runs.
    always = dataclasses.replace(
        ctx,
        peak_windows=parse_peak_windows(
            ["daily 00:00-12:00 Z", "daily 12:00-00:00 Z"]
        ),
    )
    state = snapshot_provider_state("zai", always, now=0.0)
    assert state.in_peak is True
    await ctx.http_client.aclose()


def test_build_provider_contexts_dashboard_provider_mapping() -> None:
    """`dashboard_provider` maps a switchboard provider name to the
    dashboard's provider id. The dashboard emits its own ids ("opencode",
    "ollama", "zai"); switchboard route keys ("opencode-go", ...) rarely
    match, and an unmapped lookup silently never finds a reading."""
    config = {
        "provider": {
            "opencode-go": {
                "upstream": "https://opencode.ai/zen/go/v1",
                "type": "generic",
                "target": 4,
                "dashboard_url": "http://usage-dashboard.example.com",
                "dashboard_token_env": "USAGE_DASHBOARD_TOKEN",
                "dashboard_provider": "opencode",
            },
        }
    }
    import os
    os.environ["USAGE_DASHBOARD_TOKEN"] = "dashboard-secret"
    try:
        contexts = build_provider_contexts_from_config(config)
    finally:
        del os.environ["USAGE_DASHBOARD_TOKEN"]

    ctx = contexts["opencode-go"]
    assert isinstance(ctx.truth_source, DashboardTruthSource)
    assert ctx.truth_source._provider_name == "opencode"


# --- Plan 016: quota_resets_in derivation ---


@pytest.mark.asyncio
async def test_snapshot_quota_resets_in_computed_from_bucket_reset_epoch() -> None:
    """ProviderState.quota_resets_in = bucket_reset_epoch - wall time."""
    reset_epoch = time.time() + 10800.0
    ctx = _make_ready_ctx("zai", bucket_reset_epoch=reset_epoch)
    state = snapshot_provider_state("zai", ctx, now=0.0)
    assert state.quota_resets_in is not None
    assert abs(state.quota_resets_in - 10800.0) < 2.0
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_snapshot_quota_resets_in_none_when_bucket_reset_epoch_missing() -> None:
    """No bucket_reset_epoch → quota_resets_in is None (never promotes)."""
    ctx = _make_ready_ctx("zai", bucket_reset_epoch=None)
    state = snapshot_provider_state("zai", ctx, now=0.0)
    assert state.quota_resets_in is None
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_snapshot_quota_resets_in_none_when_reset_in_past() -> None:
    """bucket_reset_epoch in the past → quota_resets_in is None (fail safe)."""
    ctx = _make_ready_ctx("zai", bucket_reset_epoch=time.time() - 60.0)
    state = snapshot_provider_state("zai", ctx, now=0.0)
    assert state.quota_resets_in is None
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_snapshot_quota_resets_in_none_when_reading_stale() -> None:
    """Stale reading → quota_resets_in is None (fresh data only)."""
    reset_epoch = time.time() + 10800.0
    ctx = _make_ready_ctx("zai", bucket_reset_epoch=reset_epoch)
    cached = ctx.reconcile._last_reading_cached
    assert cached is not None
    ctx.reconcile._last_reading_cached = CachedReading(
        reading=cached.reading,
        fetched_at_monotonic=cached.fetched_at_monotonic,
        ok=False,
    )
    state = snapshot_provider_state("zai", ctx, now=0.0)
    assert state.quota_resets_in is None
    await ctx.http_client.aclose()


def test_history_ring_warmed_from_store_on_startup(tmp_path) -> None:
    """A restart must not lose the trend surface: the ring is warmed from
    the SQLite store at construction (drop-sluice review, finding 2)."""
    from switchboard.history import HistoryEntry, SQLiteHistoryStore

    store_path = str(tmp_path / "hist")
    prior_store = SQLiteHistoryStore(
        f"{store_path}.{_safe_filename('prov_a')}.history"
    )
    for i in range(3):
        prior_store.append(
            HistoryEntry(
                timestamp=1000.0 + i,
                concurrent_sessions=0,
                local_in_flight=0,
                effective_permits=3,
                limit=4,
                hard_cap=8,
                band="normal",
                breaker="closed",
                priority_low=False,
                usage_age=0.0,
                stale=False,
                recent_429s=0,
                total_429s=0,
                queue_depth=0,
            )
        )
    prior_store.close()

    contexts = build_provider_contexts_from_config(
        {
            "provider": {
                "prov_a": {
                    "upstream": "https://a.example.com",
                    "type": "generic",
                    "target": 2,
                },
            },
        },
        history_store_path=store_path,
    )
    ring = contexts["prov_a"].reconcile.history
    assert ring is not None
    entries = ring.to_dict_list(limit=10)
    assert len(entries) == 3
    assert entries[0]["ts"] == 1000.0
    assert entries[-1]["ts"] == 1002.0


def test_history_ring_warmup_survives_corrupt_store(tmp_path) -> None:
    """A corrupt store file degrades to an empty ring — never a startup
    crash (drop-sluice review cycle 2, finding 1/5)."""
    store_path = str(tmp_path / "hist")
    corrupt = tmp_path / f"hist.{_safe_filename('prov_a')}.history"
    corrupt.write_bytes(b"this is not a sqlite database")

    contexts = build_provider_contexts_from_config(
        {
            "provider": {
                "prov_a": {
                    "upstream": "https://a.example.com",
                    "type": "generic",
                    "target": 2,
                },
            },
        },
        history_store_path=store_path,
    )
    ring = contexts["prov_a"].reconcile.history
    assert ring is not None
    assert ring.to_dict_list(limit=10) == []


def test_safe_filename_is_collision_resistant() -> None:
    """F-10: distinct provider names that normalize to the same safe string
    must map to DISTINCT files, or two providers share one history store and
    cross-contaminate their per-tick time series."""
    assert _safe_filename("opencode-go") != _safe_filename("opencode_go")
    assert _safe_filename("a.b") != _safe_filename("a_b")
    # Deterministic: the same name always maps to the same file.
    assert _safe_filename("ollama-cloud") == _safe_filename("ollama-cloud")
    # Still filesystem-safe (no path separators or null; the '.' is the
    # intentional hash separator and is safe).
    for c in ("/", "\\", "\0", " "):
        assert c not in _safe_filename(f"weird{c}name")
