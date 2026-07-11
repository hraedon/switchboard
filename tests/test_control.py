"""Unit tests for the pure routing core (Plan 006 model)."""

from __future__ import annotations

import pytest

from switchboard.control import (
    AdmissionPlan,
    Availability,
    ProviderState,
    RouteEntry,
    RouteTable,
    RoutingConfig,
    SignalFreshness,
    hash_route_key,
    route_decision,
)


def _state(
    name: str,
    *,
    availability: Availability = Availability.AVAILABLE,
    available_permits: int = 3,
    queue_depth: int = 0,
    retry_after_seconds: int | None = None,
    signal_freshness: SignalFreshness = SignalFreshness.FRESH,
    preference_rank: int = 0,
) -> ProviderState:
    return ProviderState(
        name=name,
        availability=availability,
        available_permits=available_permits,
        queue_depth=queue_depth,
        retry_after_seconds=retry_after_seconds,
        signal_freshness=signal_freshness,
        preference_rank=preference_rank,
    )


CONFIG = RoutingConfig(failover_threshold_seconds=10, failover_margin=5)
TABLE = RouteTable(
    entries={},
    default_providers=("umans", "ollama"),
)


def test_single_provider_always_selected() -> None:
    table = RouteTable(entries={}, default_providers=("umans",))
    states = {"umans": _state("umans")}
    plan = route_decision(states, table, "any_key", CONFIG, now=0.0)
    assert plan.immediate_candidates == ("umans",)
    assert plan.terminal_fallback == "umans"


def test_both_available_routes_to_primary() -> None:
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_primary_closed_routes_to_fallback() -> None:
    states = {
        "umans": _state("umans", availability=Availability.CLOSED),
        "ollama": _state("ollama"),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    assert plan.immediate_candidates == ("ollama",)
    assert plan.terminal_fallback == "umans"
    assert plan.reason == "failover"


def test_primary_busy_fallback_available_immediate_failover() -> None:
    """WI-006.3: primary BUSY + fallback AVAILABLE → immediate failover, no queue."""
    states = {
        "umans": _state("umans", availability=Availability.BUSY),
        "ollama": _state("ollama"),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    assert "ollama" in plan.immediate_candidates
    assert "umans" not in plan.immediate_candidates
    assert plan.queue_candidate == "umans"


def test_all_closed_terminal_fallback_is_primary() -> None:
    states = {
        "umans": _state("umans", availability=Availability.CLOSED),
        "ollama": _state("ollama", availability=Availability.CLOSED),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    assert plan.immediate_candidates == ()
    assert plan.queue_candidate is None
    assert plan.terminal_fallback == "umans"
    assert plan.reason == "no_eligible_candidates"


def test_primary_unknown_excluded_from_failover() -> None:
    """WI-006.2: stale/unknown primary is not preferred."""
    states = {
        "umans": _state("umans", signal_freshness=SignalFreshness.UNKNOWN),
        "ollama": _state("ollama"),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    assert "umans" not in plan.immediate_candidates
    assert plan.terminal_fallback == "umans"


def test_stale_fallback_not_preferred_over_busy_primary() -> None:
    """WI-006.2: stale data never improves fallback preference."""
    states = {
        "umans": _state(
            "umans",
            availability=Availability.BUSY,
            signal_freshness=SignalFreshness.FRESH,
        ),
        "ollama": _state(
            "ollama",
            availability=Availability.AVAILABLE,
            signal_freshness=SignalFreshness.UNKNOWN,
        ),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    assert "ollama" not in plan.immediate_candidates
    assert plan.queue_candidate == "umans"


def test_degraded_primary_can_stay() -> None:
    """DEGRADED primary may keep serving (last-known-good)."""
    states = {
        "umans": _state(
            "umans",
            availability=Availability.AVAILABLE,
            signal_freshness=SignalFreshness.DEGRADED,
        ),
        "ollama": _state("ollama"),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert plan.immediate_candidates[0] == "umans"


def test_degraded_fallback_not_failover_target() -> None:
    """DEGRADED fallback is NOT a failover target."""
    states = {
        "umans": _state(
            "umans",
            availability=Availability.BUSY,
            signal_freshness=SignalFreshness.FRESH,
        ),
        "ollama": _state(
            "ollama",
            availability=Availability.AVAILABLE,
            signal_freshness=SignalFreshness.DEGRADED,
        ),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    assert "ollama" not in plan.immediate_candidates
    assert plan.queue_candidate == "umans"


def test_both_busy_queue_on_primary() -> None:
    states = {
        "umans": _state("umans", availability=Availability.BUSY),
        "ollama": _state("ollama", availability=Availability.BUSY),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    assert plan.immediate_candidates == ()
    assert plan.queue_candidate == "umans"
    assert plan.reason == "queue_only"


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
    plan = route_decision(states, table, "abc123", CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "ollama"


def test_route_table_missing_key_uses_default() -> None:
    table = RouteTable(entries={}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    plan = route_decision(states, table, "unknown_key", CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"


def test_no_providers_raises() -> None:
    table = RouteTable(entries={}, default_providers=())
    with pytest.raises(ValueError):
        route_decision({}, table, "any_key", CONFIG, now=0.0)


# --- Property/invariant tests (Plan 006 §6) ---


def test_closed_never_in_immediate() -> None:
    states = {
        "umans": _state("umans", availability=Availability.CLOSED),
        "ollama": _state("ollama", availability=Availability.CLOSED),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    for name in plan.immediate_candidates:
        assert states[name].availability != Availability.CLOSED


def test_unknown_never_outranks_fresh() -> None:
    states = {
        "umans": _state("umans", signal_freshness=SignalFreshness.UNKNOWN),
        "ollama": _state("ollama", signal_freshness=SignalFreshness.FRESH),
    }
    plan = route_decision(states, TABLE, "any_key", CONFIG, now=0.0)
    assert "umans" not in plan.immediate_candidates
    assert "ollama" in plan.immediate_candidates


def test_plan_contains_only_configured_providers() -> None:
    entry = RouteEntry(key="k", providers=("umans", "ollama"))
    table = RouteTable(entries={"k": entry}, default_providers=("umans", "ollama"))
    states = {"umans": _state("umans"), "ollama": _state("ollama")}
    plan = route_decision(states, table, "k", CONFIG, now=0.0)
    all_names = set(plan.immediate_candidates)
    if plan.queue_candidate:
        all_names.add(plan.queue_candidate)
    all_names.add(plan.terminal_fallback)
    assert all_names <= {"umans", "ollama"}


def test_same_inputs_same_plan() -> None:
    states = {
        "umans": _state("umans", availability=Availability.BUSY),
        "ollama": _state("ollama"),
    }
    plan1 = route_decision(states, TABLE, "k", CONFIG, now=0.0)
    plan2 = route_decision(states, TABLE, "k", CONFIG, now=0.0)
    assert plan1 == plan2


def test_no_provider_appears_twice_in_immediate() -> None:
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    plan = route_decision(states, TABLE, "k", CONFIG, now=0.0)
    assert len(plan.immediate_candidates) == len(set(plan.immediate_candidates))


def test_terminal_fallback_always_primary() -> None:
    states = {
        "umans": _state("umans", availability=Availability.CLOSED),
        "ollama": _state("ollama", availability=Availability.CLOSED),
    }
    plan = route_decision(states, TABLE, "k", CONFIG, now=0.0)
    assert plan.terminal_fallback == "umans"


def test_missing_state_excluded() -> None:
    states = {"umans": _state("umans")}
    plan = route_decision(states, TABLE, "k", CONFIG, now=0.0)
    assert "ollama" not in plan.immediate_candidates


def test_admission_plan_is_frozen() -> None:
    plan = AdmissionPlan(
        immediate_candidates=("umans",),
        queue_candidate=None,
        terminal_fallback="umans",
        reason="ok",
    )
    with pytest.raises(AttributeError):
        plan.reason = "changed"  # type: ignore[misc]
