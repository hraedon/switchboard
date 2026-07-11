"""Unit tests for the pure routing core."""

from __future__ import annotations

from switchboard.control import (
    ProviderState,
    RouteEntry,
    RouteTable,
    RoutingConfig,
    hash_route_key,
    route_decision,
)


def _state(
    name: str,
    *,
    gate_closed_reason: str = "open",
    available_permits: int = 3,
    queue_depth: int = 0,
    saturation_retry_after: int = 0,
    usage_percent: float | None = None,
    usage_stale: bool = False,
    ready: bool = True,
) -> ProviderState:
    return ProviderState(
        name=name,
        gate_closed_reason=gate_closed_reason,
        available_permits=available_permits,
        queue_depth=queue_depth,
        saturation_retry_after=saturation_retry_after,
        usage_percent=usage_percent,
        usage_stale=usage_stale,
        ready=ready,
    )


CONFIG = RoutingConfig(failover_threshold_seconds=10, failover_margin=5)
TABLE = RouteTable(
    entries={},
    default_providers=("umans", "ollama"),
)


def test_single_provider_always_selected() -> None:
    table = RouteTable(entries={}, default_providers=("umans",))
    states = {"umans": _state("umans")}
    assert route_decision(states, table, "any_key", CONFIG, now=0.0) == "umans"


def test_both_available_routes_to_primary() -> None:
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    assert route_decision(states, TABLE, "any_key", CONFIG, now=0.0) == "umans"


def test_primary_closed_routes_to_fallback() -> None:
    states = {
        "umans": _state("umans", gate_closed_reason="boxed"),
        "ollama": _state("ollama"),
    }
    assert route_decision(states, TABLE, "any_key", CONFIG, now=0.0) == "ollama"


def test_primary_breaker_routes_to_fallback() -> None:
    states = {
        "umans": _state("umans", gate_closed_reason="breaker"),
        "ollama": _state("ollama"),
    }
    assert route_decision(states, TABLE, "any_key", CONFIG, now=0.0) == "ollama"


def test_primary_saturated_fallback_available_routes_to_fallback() -> None:
    states = {
        "umans": _state("umans", gate_closed_reason="saturated", saturation_retry_after=20),
        "ollama": _state("ollama"),
    }
    assert route_decision(states, TABLE, "any_key", CONFIG, now=0.0) == "ollama"


def test_primary_saturated_below_threshold_stays_on_primary() -> None:
    states = {
        "umans": _state("umans", gate_closed_reason="saturated", saturation_retry_after=7),
        "ollama": _state("ollama"),
    }
    assert route_decision(states, TABLE, "any_key", CONFIG, now=0.0) == "umans"


def test_primary_saturated_but_fallback_not_better_margin_stays() -> None:
    states = {
        "umans": _state("umans", gate_closed_reason="saturated", saturation_retry_after=12),
        "ollama": _state("ollama", usage_percent=9),
    }
    assert route_decision(states, TABLE, "any_key", CONFIG, now=0.0) == "umans"


def test_all_closed_routes_to_primary_fail_safe() -> None:
    states = {
        "umans": _state("umans", gate_closed_reason="boxed"),
        "ollama": _state("ollama", gate_closed_reason="breaker"),
    }
    assert route_decision(states, TABLE, "any_key", CONFIG, now=0.0) == "umans"


def test_primary_not_ready_routes_to_ready_fallback() -> None:
    states = {
        "umans": _state("umans", ready=False),
        "ollama": _state("ollama", ready=True),
    }
    assert route_decision(states, TABLE, "any_key", CONFIG, now=0.0) == "ollama"


def test_hash_route_key_deterministic() -> None:
    assert hash_route_key("sk-test-123") == hash_route_key("sk-test-123")


def test_hash_route_key_never_returns_raw() -> None:
    raw = "sk-super-secret-key"
    hashed = hash_route_key(raw)
    assert hashed != raw
    assert len(hashed) == 64
    assert raw not in hashed


def test_route_table_lookup_uses_entry() -> None:
    entry = RouteEntry(key="abc123", providers=("ollama", "umans"))
    table = RouteTable(entries={"abc123": entry}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    assert route_decision(states, table, "abc123", CONFIG, now=0.0) == "ollama"


def test_route_table_missing_key_uses_default() -> None:
    table = RouteTable(entries={}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    assert route_decision(states, table, "unknown_key", CONFIG, now=0.0) == "umans"


def test_usage_stale_treated_as_zero_pressure() -> None:
    states = {
        "umans": _state("umans", gate_closed_reason="saturated", saturation_retry_after=15),
        "ollama": _state("ollama", usage_percent=90, usage_stale=True),
    }
    assert route_decision(states, TABLE, "any_key", CONFIG, now=0.0) == "ollama"


def test_no_providers_raises() -> None:
    table = RouteTable(entries={}, default_providers=())
    import pytest
    with pytest.raises(ValueError):
        route_decision({}, table, "any_key", CONFIG, now=0.0)
