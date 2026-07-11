"""Unit tests for the pure routing core (Plans 006, 008)."""

from __future__ import annotations

import pytest

from switchboard.control import (
    AdmissionPlan,
    Availability,
    ProviderCapabilities,
    ProviderState,
    ReplayBoundary,
    RouteAffinity,
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
    capabilities: ProviderCapabilities | None = None,
) -> ProviderState:
    return ProviderState(
        name=name,
        availability=availability,
        available_permits=available_permits,
        queue_depth=queue_depth,
        retry_after_seconds=retry_after_seconds,
        signal_freshness=signal_freshness,
        capabilities=capabilities,
    )


def _caps(
    *,
    surfaces: frozenset[str] = frozenset(),
    api_family: str = "",
    streaming: bool = True,
    tool_calling_profile: str | None = None,
    context_class: str | None = None,
    credential_domain: str = "",
    cache_domain: str = "",
) -> ProviderCapabilities:
    return ProviderCapabilities(
        surfaces=surfaces,
        api_family=api_family,
        streaming=streaming,
        tool_calling_profile=tool_calling_profile,
        context_class=context_class,
        credential_domain=credential_domain,
        cache_domain=cache_domain,
    )


def _affinity(
    provider: str,
    *,
    selected_at: float = 0.0,
    failover_reason: str = "",
    healthy_observations: int = 0,
) -> RouteAffinity:
    return RouteAffinity(
        provider=provider,
        selected_at=selected_at,
        failover_reason=failover_reason,
        healthy_observations=healthy_observations,
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


# --- Capability filtering tests (Plan 008 §4 / WI-008.3) ---


def test_capability_filter_passes_matching_provider() -> None:
    """Provider with required surfaces is not filtered out."""
    caps = _caps(surfaces=frozenset({"chat-completions", "messages"}), api_family="openai")
    entry = RouteEntry(
        key="k",
        providers=("umans", "ollama"),
        required_capabilities=frozenset({"chat-completions"}),
    )
    table = RouteTable(entries={"k": entry}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans", capabilities=caps),
        "ollama": _state("ollama", capabilities=caps),
    }
    plan = route_decision(states, table, "k", CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama" in plan.immediate_candidates
    assert plan.reason != "capability_filtered"


def test_capability_filter_removes_non_matching_provider() -> None:
    """Provider without required surfaces is filtered out."""
    chat_caps = _caps(surfaces=frozenset({"chat-completions"}), api_family="openai")
    embed_caps = _caps(surfaces=frozenset({"embeddings"}), api_family="openai")
    entry = RouteEntry(
        key="k",
        providers=("umans", "ollama"),
        required_capabilities=frozenset({"embeddings"}),
    )
    table = RouteTable(entries={"k": entry}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans", capabilities=chat_caps),
        "ollama": _state("ollama", capabilities=embed_caps),
    }
    plan = route_decision(states, table, "k", CONFIG, now=0.0)
    assert "umans" not in plan.immediate_candidates
    assert "ollama" in plan.immediate_candidates
    assert plan.reason == "primary_available"
    assert plan.terminal_fallback == "ollama"


def test_capability_filter_backward_compat_no_metadata() -> None:
    """Provider without capabilities metadata is NOT filtered (backward compat)."""
    entry = RouteEntry(
        key="k",
        providers=("umans", "ollama"),
        required_capabilities=frozenset({"chat-completions"}),
    )
    table = RouteTable(entries={"k": entry}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    plan = route_decision(states, table, "k", CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama" in plan.immediate_candidates
    assert plan.reason != "capability_filtered"


def test_capability_all_filtered_returns_capability_filtered() -> None:
    """All candidates filtered by capabilities → capability_filtered reason."""
    chat_caps = _caps(surfaces=frozenset({"chat-completions"}), api_family="openai")
    entry = RouteEntry(
        key="k",
        providers=("umans", "ollama"),
        required_capabilities=frozenset({"embeddings"}),
    )
    table = RouteTable(entries={"k": entry}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans", capabilities=chat_caps),
        "ollama": _state("ollama", capabilities=chat_caps),
    }
    plan = route_decision(states, table, "k", CONFIG, now=0.0)
    assert plan.immediate_candidates == ()
    assert plan.queue_candidate is None
    assert plan.reason == "capability_filtered"
    assert plan.terminal_fallback == "umans"


def test_capability_filter_partial_primary_filtered() -> None:
    """Primary filtered by capabilities but fallback passes → route to fallback."""
    chat_caps = _caps(surfaces=frozenset({"chat-completions"}), api_family="openai")
    embed_caps = _caps(surfaces=frozenset({"embeddings"}), api_family="openai")
    entry = RouteEntry(
        key="k",
        providers=("umans", "ollama"),
        required_capabilities=frozenset({"embeddings"}),
    )
    table = RouteTable(entries={"k": entry}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans", capabilities=chat_caps),
        "ollama": _state("ollama", capabilities=embed_caps),
    }
    plan = route_decision(states, table, "k", CONFIG, now=0.0)
    assert plan.immediate_candidates == ("ollama",)
    assert plan.terminal_fallback == "ollama"
    assert plan.reason == "primary_available"


def test_capability_no_required_capabilities_skips_filter() -> None:
    """RouteEntry with empty required_capabilities does no filtering."""
    entry = RouteEntry(
        key="k",
        providers=("umans", "ollama"),
    )
    table = RouteTable(entries={"k": entry}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans", capabilities=_caps(surfaces=frozenset({"chat-completions"}))),
        "ollama": _state("ollama", capabilities=_caps(surfaces=frozenset({"embeddings"}))),
    }
    plan = route_decision(states, table, "k", CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama" in plan.immediate_candidates


# --- Affinity stickiness / failback tests (Plan 008 §5 / WI-008.4) ---


def test_affinity_provider_preferred_within_dwell() -> None:
    """Affinity provider preferred when available and fresh (within dwell)."""
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(states, TABLE, "k", CONFIG, now=10.0, affinity=affinity)
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "affinity_dwell"


def test_primary_preferred_after_dwell_passes() -> None:
    """After dwell interval, failback to primary if healthy."""
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(states, TABLE, "k", CONFIG, now=40.0, affinity=affinity)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_dwell_interval_prevents_failback() -> None:
    """Within dwell interval, primary is NOT preferred even if available."""
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=100.0)
    plan = route_decision(states, TABLE, "k", CONFIG, now=110.0, affinity=affinity)
    # 10 seconds elapsed, dwell_interval is 30 → stay on affinity
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "affinity_dwell"


def test_dwell_passed_primary_unavailable_stay_on_affinity() -> None:
    """Dwell passed but primary unavailable → stay on affinity provider."""
    states = {
        "umans": _state("umans", availability=Availability.CLOSED),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(states, TABLE, "k", CONFIG, now=40.0, affinity=affinity)
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "failover"


def test_affinity_to_primary_normal_preference() -> None:
    """Affinity to primary → normal primary preference."""
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("umans", selected_at=0.0)
    plan = route_decision(states, TABLE, "k", CONFIG, now=10.0, affinity=affinity)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_affinity_provider_not_available_normal_preference() -> None:
    """Affinity provider not available → normal primary preference."""
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama", availability=Availability.CLOSED),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(states, TABLE, "k", CONFIG, now=10.0, affinity=affinity)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_affinity_provider_busy_not_preferred() -> None:
    """Affinity provider BUSY (not in immediate) → normal primary preference."""
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama", availability=Availability.BUSY),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(states, TABLE, "k", CONFIG, now=10.0, affinity=affinity)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_custom_dwell_interval_controls_failback() -> None:
    """Custom dwell_interval controls when failback is considered."""
    config = RoutingConfig(dwell_interval=10.0)
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=0.0)

    # Within 10s dwell → stay on affinity
    plan = route_decision(states, TABLE, "k", config, now=5.0, affinity=affinity)
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "affinity_dwell"

    # Past 10s dwell → failback to primary
    plan = route_decision(states, TABLE, "k", config, now=15.0, affinity=affinity)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_no_affinity_normal_primary_preference() -> None:
    """No affinity → normal primary preference (backward compat)."""
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    plan = route_decision(states, TABLE, "k", CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_affinity_same_inputs_same_plan() -> None:
    """Affinity decisions are deterministic."""
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan1 = route_decision(states, TABLE, "k", CONFIG, now=10.0, affinity=affinity)
    plan2 = route_decision(states, TABLE, "k", CONFIG, now=10.0, affinity=affinity)
    assert plan1 == plan2


# --- ReplayBoundary tests (Plan 008 §6 / WI-008.5) ---


def test_replay_boundary_values() -> None:
    """ReplayBoundary enum has the expected members and values."""
    assert ReplayBoundary.BEFORE_PERMIT.value == "before_permit"
    assert ReplayBoundary.PERMIT_ACQUIRED.value == "permit_acquired"
    assert ReplayBoundary.CONNECT_FAILED.value == "connect_failed"
    assert ReplayBoundary.UPLOAD_STARTED.value == "upload_started"
    assert ReplayBoundary.HEADERS_RECEIVED.value == "headers_received"
    assert ReplayBoundary.STREAMING.value == "streaming"


def test_replay_boundary_member_count() -> None:
    """ReplayBoundary has exactly six members."""
    assert len(list(ReplayBoundary)) == 6
