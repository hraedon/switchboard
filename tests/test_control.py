"""Unit tests for the pure routing core (Plans 006, 008)."""

from __future__ import annotations

import pytest

from switchboard.control import (
    AdmissionPlan,
    Availability,
    ProviderCapabilities,
    ProviderState,
    RouteAffinity,
    RouteEntry,
    RouteTable,
    RoutingConfig,
    RoutingStrategy,
    SignalFreshness,
    hash_route_key,
    pace_surplus,
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
    usage_headroom: float | None = None,
    quota_resets_in: float | None = None,
    token_utilization: float | None = None,
    token_soft_threshold: float | None = None,
    usage_24h_utilization: float | None = None,
    weekly_remaining_fraction: float | None = None,
    weekly_reset_in: float | None = None,
) -> ProviderState:
    return ProviderState(
        name=name,
        availability=availability,
        available_permits=available_permits,
        queue_depth=queue_depth,
        retry_after_seconds=retry_after_seconds,
        signal_freshness=signal_freshness,
        capabilities=capabilities,
        usage_headroom=usage_headroom,
        quota_resets_in=quota_resets_in,
        token_utilization=token_utilization,
        token_soft_threshold=token_soft_threshold,
        usage_24h_utilization=usage_24h_utilization,
        weekly_remaining_fraction=weekly_remaining_fraction,
        weekly_reset_in=weekly_reset_in,
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


def test_hash_route_key_no_secret_is_plain_sha256() -> None:
    """No secret = plain SHA-256 (Plan 008 §3 backward compatibility). An
    unconfigured deployment and a nil secret must produce byte-identical
    digests so existing route-table entries keep matching."""
    import hashlib

    raw = "sk-legacy"
    assert hash_route_key(raw) == hash_route_key(raw, None)
    assert hash_route_key(raw) == hash_route_key(raw, "")
    assert hash_route_key(raw) == hashlib.sha256(raw.encode()).hexdigest()


def test_hash_route_key_hmac_differs_from_plain() -> None:
    """A keyed HMAC must not equal the unkeyed digest — otherwise the secret
    adds no defense against rainbow-table matching of a leaked store."""
    raw = "sk-secret"
    assert hash_route_key(raw, "route-hmac-key") != hash_route_key(raw)


def test_hash_route_key_hmac_secret_dependent() -> None:
    """Different secrets yield different digests for the same key (rotation
    changes the stored identity), and the same secret is stable."""
    raw = "sk-secret"
    a = hash_route_key(raw, "secret-a")
    b = hash_route_key(raw, "secret-b")
    assert a != b
    assert a == hash_route_key(raw, "secret-a")
    assert len(a) == 64


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


# --- Failback hysteresis tests (Plan 014) ---


def test_failback_delay_default_unchanged() -> None:
    """failback_delay=0.0 (default) reproduces Plan 008 §5 failback."""
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(states, TABLE, "k", CONFIG, now=40.0, affinity=affinity)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_failback_delay_holds_pin_within_delay() -> None:
    """Post-dwell, insufficient primary continuity → stay on affinity."""
    config = RoutingConfig(failback_delay=60.0)
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(
        states, TABLE, "k", config, now=40.0, affinity=affinity,
        healthy_since={"umans": 5.0},
    )
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "affinity_hysteresis"


def test_failback_delay_allows_failback_after_delay() -> None:
    """Post-dwell, sufficient primary continuity → failback."""
    config = RoutingConfig(failback_delay=60.0)
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(
        states, TABLE, "k", config, now=100.0, affinity=affinity,
        healthy_since={"umans": 20.0},
    )
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_failback_delay_none_never_healthy() -> None:
    """healthy_since=None (never observed healthy) → stay pinned."""
    config = RoutingConfig(failback_delay=60.0)
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(
        states, TABLE, "k", config, now=100.0, affinity=affinity,
        healthy_since=None,
    )
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "affinity_hysteresis"


def test_failback_delay_not_consulted_when_affinity_not_eligible() -> None:
    """Affinity provider not FRESH/immediate → existing rules govern."""
    config = RoutingConfig(failback_delay=60.0)
    states = {
        "umans": _state("umans"),
        # Affinity provider is BUSY, so it is not in immediate.
        "ollama": _state("ollama", availability=Availability.BUSY),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(
        states, TABLE, "k", config, now=40.0, affinity=affinity,
        healthy_since={"umans": 5.0},
    )
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_failback_delay_zero_ignores_healthy_since() -> None:
    """failback_delay=0 disables hysteresis even with a healthy_since value."""
    config = RoutingConfig(failback_delay=0.0)
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama"),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(
        states, TABLE, "k", config, now=40.0, affinity=affinity,
        healthy_since={"umans": 5.0},
    )
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


# --- ModelMap + servable filtering (Plan 010 Feature B) --------------------

from switchboard.control import ModelMap  # noqa: E402

_MODELS = ModelMap(routes={
    "umans-kimi-k2.7": {"umans": "umans-kimi-k2.7", "ollama-cloud": "kimi-k2.7-code"},
    "umans-glm-4.7": {"umans": "umans-glm-4.7"},  # umans-only
})


def test_modelmap_providers_for() -> None:
    assert _MODELS.providers_for("umans-kimi-k2.7") == frozenset({"umans", "ollama-cloud"})
    assert _MODELS.providers_for("umans-glm-4.7") == frozenset({"umans"})
    assert _MODELS.providers_for("unknown") == frozenset()


def test_modelmap_alias_for() -> None:
    assert _MODELS.alias_for("umans-kimi-k2.7", "ollama-cloud") == "kimi-k2.7-code"
    assert _MODELS.alias_for("umans-kimi-k2.7", "umans") == "umans-kimi-k2.7"
    assert _MODELS.alias_for("umans-kimi-k2.7", "nobody") is None
    assert _MODELS.alias_for("unknown", "umans") is None


def test_modelmap_contains() -> None:
    assert "umans-kimi-k2.7" in _MODELS
    assert "unknown" not in _MODELS


def test_servable_none_does_not_filter() -> None:
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {
        "umans": _state("umans", availability=Availability.CLOSED),
        "ollama-cloud": _state("ollama-cloud"),
    }
    plan = route_decision(states, table, "k", CONFIG, now=0.0, servable_providers=None)
    assert plan.immediate_candidates == ("ollama-cloud",)  # normal failover


def test_servable_filters_to_capable_provider() -> None:
    # umans is CLOSED (low-interactivity); a kimi request is servable by both, so
    # it fails over to ollama-cloud.
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {
        "umans": _state("umans", availability=Availability.CLOSED),
        "ollama-cloud": _state("ollama-cloud"),
    }
    plan = route_decision(
        states, table, "k", CONFIG, now=0.0,
        servable_providers=frozenset({"umans", "ollama-cloud"}),
    )
    assert plan.immediate_candidates == ("ollama-cloud",)
    assert plan.reason == "failover"


def test_servable_excludes_provider_without_model() -> None:
    # A glm request umans-only: ollama-cloud is not servable, so with umans
    # CLOSED there is no failover target — terminal_fallback stays umans.
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {
        "umans": _state("umans", availability=Availability.CLOSED),
        "ollama-cloud": _state("ollama-cloud"),
    }
    plan = route_decision(
        states, table, "k", CONFIG, now=0.0,
        servable_providers=frozenset({"umans"}),
    )
    assert plan.immediate_candidates == ()
    assert plan.terminal_fallback == "umans"


def test_servable_empty_is_model_unservable() -> None:
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {"umans": _state("umans"), "ollama-cloud": _state("ollama-cloud")}
    plan = route_decision(
        states, table, "k", CONFIG, now=0.0,
        servable_providers=frozenset({"some-other-provider"}),
    )
    assert plan.reason == "model_unservable"
    assert plan.immediate_candidates == ()
    assert plan.terminal_fallback == "umans"


# --- Plan 011: headroom filtering tests ---

_HEADROOM_CONFIG = RoutingConfig(headroom_threshold=0.15)


def test_headroom_demotes_non_primary_with_low_headroom() -> None:
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state("ollama-cloud", usage_headroom=0.05),
    }
    plan = route_decision(states, table, "k", _HEADROOM_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama-cloud" not in plan.immediate_candidates
    assert plan.queue_candidate == "ollama-cloud"


def test_headroom_does_not_demote_when_equal_to_threshold() -> None:
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state("ollama-cloud", usage_headroom=0.15),
    }
    plan = route_decision(states, table, "k", _HEADROOM_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama-cloud" in plan.immediate_candidates


def test_headroom_does_not_demote_when_headroom_above_threshold() -> None:
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state("ollama-cloud", usage_headroom=0.50),
    }
    plan = route_decision(states, table, "k", _HEADROOM_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama-cloud" in plan.immediate_candidates


def test_headroom_none_not_filtered() -> None:
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state("ollama-cloud", usage_headroom=None),
    }
    plan = route_decision(states, table, "k", _HEADROOM_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama-cloud" in plan.immediate_candidates


def test_headroom_primary_never_demoted() -> None:
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {
        "umans": _state("umans", usage_headroom=0.05),
        "ollama-cloud": _state("ollama-cloud", usage_headroom=0.50),
    }
    plan = route_decision(states, table, "k", _HEADROOM_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert plan.immediate_candidates[0] == "umans"


def test_headroom_threshold_zero_is_noop() -> None:
    config = RoutingConfig(headroom_threshold=0.0)
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {
        "umans": _state("umans", availability=Availability.CLOSED),
        "ollama-cloud": _state("ollama-cloud", usage_headroom=0.01),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert "ollama-cloud" in plan.immediate_candidates


# --- Plan 012: token-budget filtering tests ---

_BUDGET_CONFIG = RoutingConfig(token_budget_threshold=0.85)


def test_token_budget_demotes_non_primary_over_budget() -> None:
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state(
            "ollama-cloud", token_utilization=0.90
        ),
    }
    plan = route_decision(states, table, "k", _BUDGET_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama-cloud" not in plan.immediate_candidates
    assert plan.queue_candidate == "ollama-cloud"


def test_token_budget_does_not_demote_when_below_threshold() -> None:
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state(
            "ollama-cloud", token_utilization=0.50
        ),
    }
    plan = route_decision(states, table, "k", _BUDGET_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama-cloud" in plan.immediate_candidates


def test_token_budget_demotes_at_threshold() -> None:
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state(
            "ollama-cloud", token_utilization=0.85
        ),
    }
    plan = route_decision(states, table, "k", _BUDGET_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama-cloud" not in plan.immediate_candidates


def test_token_budget_none_not_filtered() -> None:
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state(
            "ollama-cloud", token_utilization=None
        ),
    }
    plan = route_decision(states, table, "k", _BUDGET_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama-cloud" in plan.immediate_candidates


def test_token_budget_primary_never_demoted() -> None:
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans", token_utilization=0.99),
        "ollama-cloud": _state("ollama-cloud", token_utilization=0.10),
    }
    plan = route_decision(states, table, "k", _BUDGET_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert plan.immediate_candidates[0] == "umans"


def test_token_budget_threshold_zero_is_noop() -> None:
    config = RoutingConfig(token_budget_threshold=0.0)
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans", availability=Availability.CLOSED),
        "ollama-cloud": _state(
            "ollama-cloud", token_utilization=0.99
        ),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert "ollama-cloud" in plan.immediate_candidates


def test_token_budget_and_headroom_both_demote() -> None:
    config = RoutingConfig(
        headroom_threshold=0.15, token_budget_threshold=0.85
    )
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state(
            "ollama-cloud",
            usage_headroom=0.05,
            token_utilization=0.50,
        ),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama-cloud" not in plan.immediate_candidates


# --- Per-provider soft_threshold override (Plan 012 fix) ---


def test_token_soft_threshold_overrides_global() -> None:
    """A provider's own soft_threshold demotes it even when the global
    token_budget_threshold is disabled (0.0).  This was dead config before the
    wiring: parsed/validated/displayed but never consumed."""
    config = RoutingConfig(token_budget_threshold=0.0)  # global off
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state(
            "ollama-cloud",
            token_utilization=0.90,
            token_soft_threshold=0.85,
        ),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert "ollama-cloud" not in plan.immediate_candidates
    assert plan.queue_candidate == "ollama-cloud"


def test_token_soft_threshold_below_not_demoted() -> None:
    """Below the per-provider soft_threshold, no demotion."""
    config = RoutingConfig(token_budget_threshold=0.0)
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state(
            "ollama-cloud",
            token_utilization=0.50,
            token_soft_threshold=0.85,
        ),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert "ollama-cloud" in plan.immediate_candidates


def test_token_soft_threshold_takes_precedence_over_global() -> None:
    """When both are set, the per-provider threshold wins for that provider."""
    config = RoutingConfig(token_budget_threshold=0.99)  # global very high
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state(
            "ollama-cloud",
            token_utilization=0.90,
            token_soft_threshold=0.85,  # lower → demotes
        ),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert "ollama-cloud" not in plan.immediate_candidates


def test_token_soft_threshold_none_falls_back_to_global() -> None:
    """Without a per-provider threshold, the global one still governs."""
    config = RoutingConfig(token_budget_threshold=0.85)
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state(
            "ollama-cloud",
            token_utilization=0.90,
            token_soft_threshold=None,
        ),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert "ollama-cloud" not in plan.immediate_candidates


def test_token_soft_threshold_never_demotes_primary() -> None:
    """Safety pin: the per-provider threshold demotes non-primary providers
    only — the primary stays immediate even at 0.99 utilization with its own
    soft_threshold set.  The `not is_primary` guard must hold against the
    per-provider threshold, not just the global one."""
    config = RoutingConfig(token_budget_threshold=0.0)  # global off
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state(
            "umans",
            token_utilization=0.99,
            token_soft_threshold=0.85,
        ),
        "ollama-cloud": _state("ollama-cloud", token_utilization=0.10),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert plan.immediate_candidates[0] == "umans"


# --- healthy_since uses the effective primary after model filtering (M1) ---


def test_healthy_since_uses_effective_primary_after_model_filter() -> None:
    """When a model map excludes the configured primary, the failback
    hysteresis check must read the *effective* primary's clock from the
    per-provider map, not the original primary's single value.

    Setup: umans is the configured primary but does not serve the requested
    model, so the model filter re-derives the effective primary to
    ollama-cloud.  An affinity pin sits on zai.  Past ``dwell_interval`` the
    hysteresis check consults the effective primary (ollama-cloud)'s clock:

    * enough continuity → failback to ollama-cloud;
    * insufficient continuity → hold the pin on zai.
    """
    config = RoutingConfig(failback_delay=60.0, dwell_interval=10.0)
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud", "zai")
    )
    states = {
        "umans": _state("umans"),
        "ollama-cloud": _state("ollama-cloud"),
        "zai": _state("zai"),
    }
    affinity = _affinity("zai", selected_at=0.0)
    # umans does not serve the model → effective primary is ollama-cloud.
    servable = frozenset({"ollama-cloud", "zai"})

    # ollama-cloud healthy for 90s (>= 60s delay) → failback releases the pin.
    plan = route_decision(
        states, table, "k", config, now=100.0, affinity=affinity,
        servable_providers=servable,
        healthy_since={"ollama-cloud": 10.0},
    )
    assert plan.immediate_candidates[0] == "ollama-cloud"
    assert plan.reason == "primary_available"

    # ollama-cloud only healthy for 50s (< 60s delay) → pin holds on zai.
    plan = route_decision(
        states, table, "k", config, now=100.0, affinity=affinity,
        servable_providers=servable,
        healthy_since={"ollama-cloud": 50.0},
    )
    assert plan.immediate_candidates[0] == "zai"
    assert plan.reason == "affinity_hysteresis"


# --- Conversation pinning (Plan 019 §6) -------------------------------------


def test_pin_conversations_suppresses_failback() -> None:
    """With pin_conversations on, an active affinity pin on a FRESH fallback
    stays front past dwell — the failback-to-primary branch is suppressed.
    Without pinning, the same inputs fail back to the primary."""
    base = dict(failback_delay=0.0, dwell_interval=10.0)
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama")
    )
    states = {"umans": _state("umans"), "ollama": _state("ollama")}
    affinity = _affinity("ollama", selected_at=0.0)

    # Without pinning: post-dwell, primary available → failback to umans.
    plan = route_decision(
        states, table, "k", RoutingConfig(**base), now=100.0, affinity=affinity,
    )
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"

    # With pinning: the pin holds — ollama stays front, no failback.
    pinned = RoutingConfig(**base, pin_conversations=True)
    plan = route_decision(
        states, table, "k", pinned, now=100.0, affinity=affinity,
    )
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "affinity_pinned"


def test_pin_conversations_releases_when_pinned_provider_drops() -> None:
    """When the pinned provider drops out of immediate (BUSY), normal
    failover selects the next best — pinning does not strand a request on
    an unavailable provider."""
    config = RoutingConfig(
        pin_conversations=True, dwell_interval=10.0, failback_delay=0.0
    )
    table = RouteTable(entries={}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans"),
        "ollama": _state("ollama", availability=Availability.BUSY),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(
        states, table, "k", config, now=100.0, affinity=affinity,
    )
    # ollama is BUSY → not in immediate → pin cannot hold → umans serves.
    assert plan.immediate_candidates[0] == "umans"


def test_pin_conversations_on_primary_resists_opportunistic_diversion() -> None:
    """M1 fix: a conversation pinned to the PRIMARY (pin-on-first-request)
    must NOT be diverted by opportunistic quota-burn.  Without this guard the
    pinned primary falls through to the else branch, opportunism fronts a
    fallback, and the conversation permanently migrates — breaking the
    pin-stays-until-drop promise."""
    config = RoutingConfig(
        pin_conversations=True,
        opportunistic_enabled=True,
        opportunistic_min_headroom=0.5,
        opportunistic_reset_window=21600.0,
        dwell_interval=10.0,
        failback_delay=0.0,
    )
    table = RouteTable(entries={}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state("umans"),
        "ollama": _state(
            "ollama", usage_headroom=0.9, quota_resets_in=3600.0,
        ),
    }
    # Pin is on the primary (umans) — the if-block's `!= primary` guard
    # routes to the else branch, where opportunism would otherwise fire.
    affinity = _affinity("umans", selected_at=0.0)
    plan = route_decision(
        states, table, "k", config, now=100.0, affinity=affinity,
    )
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "affinity_pinned"


def test_pin_conversations_dwell_still_applies() -> None:
    """Within dwell_interval, the pin holds with reason affinity_dwell
    (the dwell mechanism is independent of the failback suppression)."""
    config = RoutingConfig(
        pin_conversations=True, dwell_interval=30.0, failback_delay=0.0
    )
    table = RouteTable(entries={}, default_providers=("umans", "ollama"))
    states = {"umans": _state("umans"), "ollama": _state("ollama")}
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(
        states, table, "k", config, now=10.0, affinity=affinity,
    )
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "affinity_dwell"


# --- Conversation fingerprint extraction (Plan 019 §6.2) --------------------


from switchboard.control import extract_conversation_fingerprint  # noqa: E402


def test_fingerprint_from_first_user_message() -> None:
    body = b'{"messages":[{"role":"system","content":"x"},{"role":"user","content":"hello"}]}'
    fp = extract_conversation_fingerprint(body)
    assert fp is not None
    assert len(fp) == 64  # SHA-256 hex


def test_fingerprint_stable_for_same_content() -> None:
    body = b'{"messages":[{"role":"user","content":"same question"}]}'
    assert extract_conversation_fingerprint(body) == extract_conversation_fingerprint(body)


def test_fingerprint_differs_for_different_content() -> None:
    a = b'{"messages":[{"role":"user","content":"question A"}]}'
    b = b'{"messages":[{"role":"user","content":"question B"}]}'
    assert extract_conversation_fingerprint(a) != extract_conversation_fingerprint(b)


def test_fingerprint_multimodal_first_text_part() -> None:
    body = (
        b'{"messages":[{"role":"user","content":'
        b'[{"type":"image_url","image_url":"x"},'
        b'{"type":"text","text":"describe this"}]}]}'
    )
    fp = extract_conversation_fingerprint(body)
    assert fp is not None


def test_fingerprint_none_when_no_user_message() -> None:
    body = b'{"messages":[{"role":"system","content":"x"}]}'
    assert extract_conversation_fingerprint(body) is None


def test_fingerprint_none_on_invalid_json() -> None:
    assert extract_conversation_fingerprint(b"not json") is None
    assert extract_conversation_fingerprint(b"") is None


def test_fingerprint_none_when_messages_not_list() -> None:
    assert extract_conversation_fingerprint(b'{"messages":"oops"}') is None


def test_fingerprint_none_on_deeply_nested_json() -> None:
    """H1: deeply-nested JSON (valid syntax that overflows the parser) must
    return None, not raise RecursionError out of the request path (a crafted
    body that crashes the proxy is a DoS vector)."""
    body = b"[" * 30000 + b"]" * 30000
    assert extract_conversation_fingerprint(body) is None


# --- Plan 013: trailing-24h usage filtering tests ---

_24H_CONFIG = RoutingConfig(usage_24h_threshold=0.85)


def test_usage_24h_demotes_primary_over_threshold() -> None:
    """Plan 013 §2: the ONE proactive signal that may demote the primary.

    umans (primary) over its trailing-24h cap → traffic prefers the
    fallback; umans stays queue-candidate backstop (de-prefer, never
    exclude).
    """
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans", usage_24h_utilization=0.90),
        "ollama-cloud": _state("ollama-cloud", usage_24h_utilization=0.10),
    }
    plan = route_decision(states, table, "k", _24H_CONFIG, now=0.0)
    assert "umans" not in plan.immediate_candidates
    assert "ollama-cloud" in plan.immediate_candidates
    assert plan.immediate_candidates[0] == "ollama-cloud"
    # Backstop semantics: primary remains the queue candidate.
    assert plan.queue_candidate == "umans"
    assert plan.terminal_fallback == "umans"
    assert plan.reason == "failover"


def test_usage_24h_demotes_non_primary_over_threshold() -> None:
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans", usage_24h_utilization=0.10),
        "ollama-cloud": _state("ollama-cloud", usage_24h_utilization=0.90),
    }
    plan = route_decision(states, table, "k", _24H_CONFIG, now=0.0)
    assert "umans" in plan.immediate_candidates
    assert "ollama-cloud" not in plan.immediate_candidates
    assert plan.queue_candidate == "ollama-cloud"


def test_usage_24h_does_not_demote_below_threshold() -> None:
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans", usage_24h_utilization=0.84),
        "ollama-cloud": _state("ollama-cloud", usage_24h_utilization=0.10),
    }
    plan = route_decision(states, table, "k", _24H_CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"


def test_usage_24h_demotes_at_threshold() -> None:
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans", usage_24h_utilization=0.85),
        "ollama-cloud": _state("ollama-cloud", usage_24h_utilization=0.10),
    }
    plan = route_decision(states, table, "k", _24H_CONFIG, now=0.0)
    assert "umans" not in plan.immediate_candidates
    assert "ollama-cloud" in plan.immediate_candidates


def test_usage_24h_none_not_filtered() -> None:
    """Fail safe: no data (None) → no filtering, even over threshold."""
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans", usage_24h_utilization=None),
        "ollama-cloud": _state("ollama-cloud", usage_24h_utilization=None),
    }
    plan = route_decision(states, table, "k", _24H_CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert "ollama-cloud" in plan.immediate_candidates


def test_usage_24h_threshold_zero_is_noop() -> None:
    """Default config (0.0) → feature fully off, today's behavior."""
    config = RoutingConfig(usage_24h_threshold=0.0)
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans", usage_24h_utilization=0.99),
        "ollama-cloud": _state("ollama-cloud", usage_24h_utilization=0.99),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert plan.immediate_candidates[0] == "umans"


def test_usage_24h_all_over_threshold_primary_still_backstop() -> None:
    """Every provider over threshold → nothing immediate; the primary is
    still the queue candidate (fail safe, never strand traffic)."""
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans", usage_24h_utilization=0.95),
        "ollama-cloud": _state("ollama-cloud", usage_24h_utilization=0.90),
    }
    plan = route_decision(states, table, "k", _24H_CONFIG, now=0.0)
    assert plan.immediate_candidates == ()
    assert plan.queue_candidate == "umans"
    assert plan.reason == "queue_only"


def test_usage_24h_composes_with_token_budget() -> None:
    """Both signals fire independently: primary demoted by 24h, fallback
    demoted by token budget → nothing immediate, primary queues."""
    config = RoutingConfig(
        token_budget_threshold=0.85, usage_24h_threshold=0.85
    )
    table = RouteTable(
        entries={}, default_providers=("umans", "ollama-cloud")
    )
    states = {
        "umans": _state("umans", usage_24h_utilization=0.95),
        "ollama-cloud": _state("ollama-cloud", token_utilization=0.90),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert plan.immediate_candidates == ()
    assert plan.queue_candidate == "umans"


# --- Plan 015: headroom-ordered fallback ranking tests ---

_RANKING_OFF_CONFIG = RoutingConfig(headroom_ranking=False)
_RANKING_ON_CONFIG = RoutingConfig(headroom_ranking=True)


def test_headroom_ranking_off_preserves_table_order() -> None:
    """Flag off: fallbacks stay in table order even with headroom data."""
    table = RouteTable(
        entries={}, default_providers=("umans", "fallback-a", "fallback-b")
    )
    states = {
        "umans": _state("umans", availability=Availability.BUSY),
        "fallback-a": _state("fallback-a", usage_headroom=0.10),
        "fallback-b": _state("fallback-b", usage_headroom=0.90),
    }
    plan = route_decision(states, table, "k", _RANKING_OFF_CONFIG, now=0.0)
    assert plan.immediate_candidates == ("fallback-a", "fallback-b")


def test_headroom_ranking_on_prefers_higher_headroom_fallback() -> None:
    """Flag on, primary not immediate: higher-headroom fallback ranks first."""
    table = RouteTable(
        entries={}, default_providers=("umans", "fallback-a", "fallback-b")
    )
    states = {
        "umans": _state("umans", availability=Availability.BUSY),
        "fallback-a": _state("fallback-a", usage_headroom=0.10),
        "fallback-b": _state("fallback-b", usage_headroom=0.90),
    }
    plan = route_decision(states, table, "k", _RANKING_ON_CONFIG, now=0.0)
    assert plan.immediate_candidates == ("fallback-b", "fallback-a")
    assert plan.reason == "failover"


def test_headroom_ranking_primary_still_fronts() -> None:
    """Flag on, primary immediate: ranking runs, then primary fronts."""
    table = RouteTable(
        entries={}, default_providers=("umans", "fallback-a", "fallback-b")
    )
    states = {
        "umans": _state("umans", usage_headroom=0.10),
        "fallback-a": _state("fallback-a", usage_headroom=0.90),
        "fallback-b": _state("fallback-b", usage_headroom=0.50),
    }
    plan = route_decision(states, table, "k", _RANKING_ON_CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.immediate_candidates == ("umans", "fallback-a", "fallback-b")
    assert plan.reason == "primary_available"


def test_headroom_ranking_none_sorts_after_data_bearing() -> None:
    """Providers without headroom data sort after measured ones, in table order."""
    table = RouteTable(
        entries={},
        default_providers=("umans", "fallback-a", "fallback-b", "fallback-c"),
    )
    states = {
        "umans": _state("umans", availability=Availability.BUSY),
        "fallback-a": _state("fallback-a", usage_headroom=None),
        "fallback-b": _state("fallback-b", usage_headroom=0.50),
        "fallback-c": _state("fallback-c", usage_headroom=None),
    }
    plan = route_decision(states, table, "k", _RANKING_ON_CONFIG, now=0.0)
    assert plan.immediate_candidates == ("fallback-b", "fallback-a", "fallback-c")


def test_headroom_ranking_single_candidate_no_op() -> None:
    """One immediate candidate: sorting is a no-op."""
    table = RouteTable(entries={}, default_providers=("umans", "ollama-cloud"))
    states = {
        "umans": _state("umans", availability=Availability.BUSY),
        "ollama-cloud": _state("ollama-cloud", usage_headroom=0.10),
    }
    plan = route_decision(states, table, "k", _RANKING_ON_CONFIG, now=0.0)
    assert plan.immediate_candidates == ("ollama-cloud",)


def test_headroom_ranking_ties_break_on_table_order() -> None:
    """Equal headroom values keep table order (deterministic)."""
    table = RouteTable(
        entries={}, default_providers=("umans", "fallback-a", "fallback-b")
    )
    states = {
        "umans": _state("umans", availability=Availability.BUSY),
        "fallback-a": _state("fallback-a", usage_headroom=0.50),
        "fallback-b": _state("fallback-b", usage_headroom=0.50),
    }
    plan = route_decision(states, table, "k", _RANKING_ON_CONFIG, now=0.0)
    assert plan.immediate_candidates == ("fallback-a", "fallback-b")


# --- Plan 016: opportunistic quota-burn tests ---

_OPPORTUNISTIC_CONFIG = RoutingConfig(
    opportunistic_enabled=True,
    opportunistic_min_headroom=0.5,
    opportunistic_reset_window=21600.0,
    opportunistic_margin=0.10,
)


def test_opportunistic_default_disabled_unchanged() -> None:
    """Default config (opportunistic_enabled=False) keeps primary preference."""
    table = RouteTable(
        entries={}, default_providers=("umans", "zai")
    )
    states = {
        "umans": _state("umans"),
        "zai": _state(
            "zai",
            usage_headroom=0.70,
            quota_resets_in=10800.0,
        ),
    }
    plan = route_decision(states, table, "k", RoutingConfig(), now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_opportunistic_qualifier_fronts_target_keeps_primary() -> None:
    """Healthy primary + qualifying fallback → fallback fronts; primary stays."""
    table = RouteTable(entries={}, default_providers=("umans", "zai"))
    states = {
        "umans": _state("umans"),
        "zai": _state(
            "zai",
            usage_headroom=0.70,
            quota_resets_in=10800.0,
        ),
    }
    plan = route_decision(states, table, "k", _OPPORTUNISTIC_CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "zai"
    assert "umans" in plan.immediate_candidates
    assert plan.terminal_fallback == "umans"
    assert plan.reason == "opportunistic"


def test_opportunistic_below_min_headroom_returns_none() -> None:
    """Fallback headroom below floor → primary serves."""
    table = RouteTable(entries={}, default_providers=("umans", "zai"))
    states = {
        "umans": _state("umans"),
        "zai": _state(
            "zai",
            usage_headroom=0.30,
            quota_resets_in=10800.0,
        ),
    }
    plan = route_decision(states, table, "k", _OPPORTUNISTIC_CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_opportunistic_outside_reset_window_returns_none() -> None:
    """Reset too far in the future → opportunism does not fire."""
    table = RouteTable(entries={}, default_providers=("umans", "zai"))
    states = {
        "umans": _state("umans"),
        "zai": _state(
            "zai",
            usage_headroom=0.70,
            quota_resets_in=72000.0,
        ),
    }
    plan = route_decision(states, table, "k", _OPPORTUNISTIC_CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_opportunistic_quota_resets_in_none_returns_none() -> None:
    """Unknown reset time (None) never promotes."""
    table = RouteTable(entries={}, default_providers=("umans", "zai"))
    states = {
        "umans": _state("umans"),
        "zai": _state(
            "zai",
            usage_headroom=0.70,
            quota_resets_in=None,
        ),
    }
    plan = route_decision(states, table, "k", _OPPORTUNISTIC_CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_opportunistic_headroom_none_returns_none() -> None:
    """Unknown headroom (None) never promotes."""
    table = RouteTable(entries={}, default_providers=("umans", "zai"))
    states = {
        "umans": _state("umans"),
        "zai": _state(
            "zai",
            usage_headroom=None,
            quota_resets_in=10800.0,
        ),
    }
    plan = route_decision(states, table, "k", _OPPORTUNISTIC_CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_opportunistic_margin_suppresses_close_qualifiers() -> None:
    """Best and runner-up within margin → keep primary."""
    table = RouteTable(
        entries={}, default_providers=("umans", "zai", "fallback-b")
    )
    states = {
        "umans": _state("umans"),
        "zai": _state(
            "zai",
            usage_headroom=0.70,
            quota_resets_in=10800.0,
        ),
        "fallback-b": _state(
            "fallback-b",
            usage_headroom=0.65,
            quota_resets_in=10800.0,
        ),
    }
    plan = route_decision(states, table, "k", _OPPORTUNISTIC_CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"


def test_opportunistic_single_qualifier_no_margin_needed() -> None:
    """Only one qualifier: margin rule is moot; it wins."""
    table = RouteTable(entries={}, default_providers=("umans", "zai"))
    states = {
        "umans": _state("umans"),
        "zai": _state(
            "zai",
            usage_headroom=0.51,
            quota_resets_in=10800.0,
        ),
    }
    plan = route_decision(states, table, "k", _OPPORTUNISTIC_CONFIG, now=0.0)
    assert plan.immediate_candidates[0] == "zai"
    assert plan.reason == "opportunistic"


def test_opportunistic_affinity_pin_beats_opportunism() -> None:
    """An active non-primary affinity pin (within dwell) overrides opportunistic selection."""
    table = RouteTable(
        entries={}, default_providers=("umans", "fallback-b", "zai")
    )
    states = {
        "umans": _state("umans"),
        "fallback-b": _state("fallback-b"),
        "zai": _state(
            "zai",
            usage_headroom=0.70,
            quota_resets_in=10800.0,
        ),
    }
    affinity = _affinity("fallback-b", selected_at=0.0)
    plan = route_decision(
        states, table, "k", _OPPORTUNISTIC_CONFIG, now=10.0, affinity=affinity
    )
    assert plan.immediate_candidates[0] == "fallback-b"
    assert plan.reason == "affinity_dwell"


def test_opportunistic_target_demoted_by_other_signal_never_qualifies() -> None:
    """A fallback demoted by headroom_threshold is not in immediate → no opportunism."""
    config = RoutingConfig(
        headroom_threshold=0.15,
        opportunistic_enabled=True,
        opportunistic_min_headroom=0.5,
        opportunistic_reset_window=21600.0,
        opportunistic_margin=0.10,
    )
    table = RouteTable(entries={}, default_providers=("umans", "zai"))
    states = {
        "umans": _state("umans"),
        "zai": _state(
            "zai",
            usage_headroom=0.10,
            quota_resets_in=10800.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=0.0)
    assert plan.immediate_candidates[0] == "umans"
    assert "zai" not in plan.immediate_candidates
    assert plan.queue_candidate == "zai"


def test_opportunistic_after_non_primary_affinity_failback() -> None:
    """After a non-primary affinity pin is released on failback, the next
    no-pin re-evaluation can select opportunistically."""
    config = RoutingConfig(
        dwell_interval=30.0,
        failback_delay=60.0,
        opportunistic_enabled=True,
        opportunistic_min_headroom=0.5,
        opportunistic_reset_window=21600.0,
        opportunistic_margin=0.10,
    )
    table = RouteTable(
        entries={}, default_providers=("umans", "fallback-b", "zai")
    )
    states = {
        "umans": _state("umans"),
        "fallback-b": _state("fallback-b"),
        "zai": _state(
            "zai",
            usage_headroom=0.70,
            quota_resets_in=10800.0,
        ),
    }
    # Within dwell: non-primary affinity pins front.
    affinity = _affinity("fallback-b", selected_at=0.0)
    plan = route_decision(states, table, "k", config, now=10.0, affinity=affinity)
    assert plan.immediate_candidates[0] == "fallback-b"
    assert plan.reason == "affinity_dwell"

    # After dwell + failback_delay: primary is fronted.
    plan = route_decision(
        states, table, "k", config, now=100.0, affinity=affinity,
        healthy_since={"umans": 20.0},
    )
    assert plan.immediate_candidates[0] == "umans"
    assert plan.reason == "primary_available"

    # No pin: opportunism can fire.
    plan = route_decision(states, table, "k", config, now=100.0, affinity=None)
    assert plan.immediate_candidates[0] == "zai"
    assert plan.reason == "opportunistic"


# --- Plan 020 Wave 4: pace routing tests (WI-13) ----------------------------

_PACE_CONFIG = RoutingConfig(strategy=RoutingStrategy.PACE)


def test_pace_surplus_none_without_weekly_data() -> None:
    """A provider with no weekly signal is unscored (fail safe)."""
    st = _state("zai")
    assert pace_surplus(st, 0.14) is None


def test_pace_surplus_none_when_stale() -> None:
    """A non-FRESH provider is unscored — stale data never promotes."""
    st = _state(
        "zai",
        signal_freshness=SignalFreshness.DEGRADED,
        weekly_remaining_fraction=0.8,
        weekly_reset_in=86400.0,
    )
    assert pace_surplus(st, 0.14) is None


def test_pace_surplus_worked_example_a() -> None:
    """Plan 020 D5 worked example A: 80% remaining, resets in 5 days.

    expected_burn = 0.14 * 5 = 0.70 → surplus = 0.80 - 0.70 = +0.10
    """
    st = _state(
        "zai",
        weekly_remaining_fraction=0.80,
        weekly_reset_in=5 * 86400.0,
    )
    assert pace_surplus(st, 0.14) == pytest.approx(0.10)


def test_pace_surplus_worked_example_b() -> None:
    """Plan 020 D5 worked example B: 40% remaining, resets in 1 day.

    expected_burn = 0.14 * 1 = 0.14 → surplus = 0.40 - 0.14 = +0.26
    B wins (higher surplus).
    """
    st = _state(
        "ollama",
        weekly_remaining_fraction=0.40,
        weekly_reset_in=86400.0,
    )
    assert pace_surplus(st, 0.14) == pytest.approx(0.26)


def test_pace_surplus_negative_when_burning_fast() -> None:
    """90% remaining but resets in 8 days → surplus = 0.90 - 1.12 = -0.22."""
    st = _state(
        "zai",
        weekly_remaining_fraction=0.90,
        weekly_reset_in=8 * 86400.0,
    )
    assert pace_surplus(st, 0.14) == pytest.approx(-0.22)


def test_pace_surplus_zero_at_nominal_burn() -> None:
    """50% remaining, resets in ~3.57 days → surplus ≈ 0."""
    st = _state(
        "zai",
        weekly_remaining_fraction=0.50,
        weekly_reset_in=0.50 / 0.14 * 86400.0,
    )
    assert pace_surplus(st, 0.14) == pytest.approx(0.0, abs=1e-6)


def test_pace_ranks_by_surplus_descending() -> None:
    """B (surplus +0.26) outranks A (surplus +0.10) → B fronts."""
    config = RoutingConfig(strategy=RoutingStrategy.PACE)
    table = RouteTable(entries={}, default_providers=("zai", "ollama"))
    states = {
        "zai": _state(
            "zai", weekly_remaining_fraction=0.80, weekly_reset_in=5 * 86400.0,
        ),
        "ollama": _state(
            "ollama", weekly_remaining_fraction=0.40, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "pace_failover"


def test_pace_unscored_ranks_after_scored() -> None:
    """An unscored provider (no weekly data) is never starved — it ranks
    after scored providers in table order but stays immediate-eligible."""
    config = RoutingConfig(strategy=RoutingStrategy.PACE)
    table = RouteTable(entries={}, default_providers=("umans", "zai"))
    states = {
        "umans": _state("umans"),  # no weekly data → unscored
        "zai": _state(
            "zai", weekly_remaining_fraction=0.40, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    # zai has a positive surplus, umans is unscored → zai fronts.
    assert plan.immediate_candidates[0] == "zai"
    # Both are immediate candidates (unscored is not excluded).
    assert "umans" in plan.immediate_candidates


def test_pace_flap_margin_preserves_table_order() -> None:
    """When the leader's surplus advantage < pace_flap_margin, table order is
    preserved to avoid per-request flapping between near-equal providers.

    Discriminating case: the runner-up (ollama) has the HIGHER surplus. If
    hysteresis works, table-order-first zai fronts; if hysteresis is removed,
    ollama would front (it has the higher surplus)."""
    config = RoutingConfig(
        strategy=RoutingStrategy.PACE, pace_flap_margin=0.10,
    )
    table = RouteTable(entries={}, default_providers=("zai", "ollama"))
    # zai surplus = 0.06; ollama surplus = 0.14; advantage = 0.08 < 0.10.
    # ollama has higher surplus, so without hysteresis it would front.
    states = {
        "zai": _state(
            "zai", weekly_remaining_fraction=0.20, weekly_reset_in=86400.0,
        ),
        "ollama": _state(
            "ollama", weekly_remaining_fraction=0.28, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    # Hysteresis keeps table order → zai fronts despite lower surplus.
    assert plan.immediate_candidates[0] == "zai"

    # With margin=0.0, hysteresis fires only on exact ties (0 < 0 is false),
    # so the re-rank should produce ollama first.
    config_no_margin = RoutingConfig(
        strategy=RoutingStrategy.PACE, pace_flap_margin=0.0,
    )
    plan2 = route_decision(states, table, "k", config_no_margin, now=100.0)
    assert plan2.immediate_candidates[0] == "ollama"


def test_pace_flap_margin_allows_rerank() -> None:
    """When the leader's surplus advantage >= pace_flap_margin, re-rank fires."""
    config = RoutingConfig(
        strategy=RoutingStrategy.PACE, pace_flap_margin=0.05,
    )
    table = RouteTable(entries={}, default_providers=("zai", "ollama"))
    # zai surplus = 0.10; ollama surplus = 0.26; advantage = 0.16 > 0.05.
    states = {
        "zai": _state(
            "zai", weekly_remaining_fraction=0.80, weekly_reset_in=5 * 86400.0,
        ),
        "ollama": _state(
            "ollama", weekly_remaining_fraction=0.40, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    assert plan.immediate_candidates[0] == "ollama"


def test_pace_does_not_demote_primary() -> None:
    """Pace is a ranking signal, not a demotion: the primary stays
    immediate-eligible, queue backstop, and terminal fallback even when it has
    the worst surplus. It may lose the *front* but it is not demoted."""
    config = RoutingConfig(strategy=RoutingStrategy.PACE)
    table = RouteTable(entries={}, default_providers=("umans", "ollama"))
    states = {
        "umans": _state(
            "umans", weekly_remaining_fraction=0.10, weekly_reset_in=86400.0,
        ),
        "ollama": _state(
            "ollama", weekly_remaining_fraction=0.90, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    # ollama has higher surplus → fronts. umans is still a candidate.
    assert plan.immediate_candidates[0] == "ollama"
    assert "umans" in plan.immediate_candidates
    # terminal_fallback is still the primary (fail safe).
    assert plan.terminal_fallback == "umans"


def test_pace_with_affinity_still_pins() -> None:
    """Pace ranking is subordinate to affinity: an active dwell pin is not
    overridden by the pace ranking."""
    config = RoutingConfig(
        strategy=RoutingStrategy.PACE, dwell_interval=30.0,
    )
    table = RouteTable(entries={}, default_providers=("zai", "ollama"))
    states = {
        "zai": _state(
            "zai", weekly_remaining_fraction=0.90, weekly_reset_in=86400.0,
        ),
        "ollama": _state(
            "ollama", weekly_remaining_fraction=0.10, weekly_reset_in=86400.0,
        ),
    }
    affinity = _affinity("ollama", selected_at=0.0)
    plan = route_decision(states, table, "k", config, now=10.0, affinity=affinity)
    # ollama has worse surplus, but the dwell pin holds it at front.
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "affinity_dwell"


def test_pace_strategy_default_is_ordered() -> None:
    """Default strategy is ORDERED — pace does not fire unless opted in."""
    config = RoutingConfig()  # default strategy
    table = RouteTable(entries={}, default_providers=("zai", "ollama"))
    states = {
        "zai": _state(
            "zai", weekly_remaining_fraction=0.10, weekly_reset_in=86400.0,
        ),
        "ollama": _state(
            "ollama", weekly_remaining_fraction=0.90, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    # Without pace, table order → zai (primary) fronts.
    assert plan.immediate_candidates[0] == "zai"
    assert plan.reason == "primary_available"


def test_pace_custom_burn_rate() -> None:
    """A custom burn_rate_per_day changes the surplus and thus the ranking."""
    config = RoutingConfig(
        strategy=RoutingStrategy.PACE, pace_burn_rate_per_day=0.30,
    )
    table = RouteTable(entries={}, default_providers=("zai", "ollama"))
    # zai: 0.80 - 0.30*5 = 0.80 - 1.50 = -0.70
    # ollama: 0.40 - 0.30*1 = 0.10
    # ollama wins.
    states = {
        "zai": _state(
            "zai", weekly_remaining_fraction=0.80, weekly_reset_in=5 * 86400.0,
        ),
        "ollama": _state(
            "ollama", weekly_remaining_fraction=0.40, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    assert plan.immediate_candidates[0] == "ollama"


def test_pace_all_unscored_preserves_table_order() -> None:
    """When no provider has weekly data, pace is a no-op (table order)."""
    config = RoutingConfig(strategy=RoutingStrategy.PACE)
    table = RouteTable(entries={}, default_providers=("zai", "ollama"))
    states = {"zai": _state("zai"), "ollama": _state("ollama")}
    plan = route_decision(states, table, "k", config, now=100.0)
    assert plan.immediate_candidates[0] == "zai"
    assert plan.reason == "primary_available"


def test_pace_hysteresis_still_enforces_scored_first() -> None:
    """Under hysteresis (margin too small), scored providers still rank before
    unscored ones — the plan's "unscored ranks after scored" guardrail holds
    even when surplus re-ranking is suppressed."""
    config = RoutingConfig(
        strategy=RoutingStrategy.PACE, pace_flap_margin=0.50,
    )
    table = RouteTable(entries={}, default_providers=("umans", "zai", "ollama"))
    # umans is table-first but unscored; zai and ollama are scored with
    # near-equal surplus (advantage < 0.50). Without the scored-first guard,
    # umans would front (table order). With it, zai/ollama front.
    states = {
        "umans": _state("umans"),  # unscored, table-first
        "zai": _state(
            "zai", weekly_remaining_fraction=0.50, weekly_reset_in=86400.0,
        ),
        "ollama": _state(
            "ollama", weekly_remaining_fraction=0.55, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    # umans (unscored) must NOT front — scored providers rank first.
    assert plan.immediate_candidates[0] != "umans"
    assert "umans" in plan.immediate_candidates  # still immediate-eligible


def test_pace_does_not_fire_opportunism() -> None:
    """PACE subsumes Plan 016 opportunism: when strategy=pace, the
    session-window opportunistic target does not fire (pace uses the weekly
    window instead)."""
    config = RoutingConfig(
        strategy=RoutingStrategy.PACE,
        opportunistic_enabled=True,
        opportunistic_min_headroom=0.5,
        opportunistic_reset_window=21600.0,
        opportunistic_margin=0.10,
    )
    table = RouteTable(entries={}, default_providers=("zai", "ollama"))
    # zai has weekly data (pace will rank it). ollama has session headroom
    # that would trigger opportunism (headroom=0.9, reset_in=3600 < 21600).
    states = {
        "zai": _state(
            "zai", weekly_remaining_fraction=0.50, weekly_reset_in=86400.0,
            usage_headroom=0.2,  # low session headroom
        ),
        "ollama": _state(
            "ollama",
            usage_headroom=0.9,  # high session headroom → opportunistic candidate
            quota_resets_in=3600.0,  # within opportunistic_reset_window
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    # Under PACE, opportunism does NOT fire (would be "opportunistic" reason).
    # zai is the only scored provider → fronts.
    assert plan.immediate_candidates[0] == "zai"
    assert plan.reason != "opportunistic"


def test_pace_reason_not_set_when_primary_absent_for_other_reasons() -> None:
    """When the primary is BUSY (not pace-ranked out), the reason should be
    'failover' not 'pace_failover' — pace didn't cause the failover."""
    config = RoutingConfig(strategy=RoutingStrategy.PACE)
    table = RouteTable(entries={}, default_providers=("umans", "ollama"))
    # umans is BUSY -> not in immediate -> ollama fronts. Neither has weekly
    # data, so pace didn't re-rank anything.
    states = {
        "umans": _state("umans", availability=Availability.BUSY),
        "ollama": _state("ollama"),  # no weekly data
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "failover"  # not pace_failover


def test_pace_single_scored_preserves_no_margin_check() -> None:
    """A single scored provider needs no margin check - it always fronts,
    even when it is not table-order-first."""
    config = RoutingConfig(
        strategy=RoutingStrategy.PACE, pace_flap_margin=0.50,
    )
    table = RouteTable(entries={}, default_providers=("ollama", "zai"))
    # ollama is table-first but unscored; zai is scored.
    # If a margin check were wrongly applied to a single scored provider,
    # ollama would front. zai must front.
    states = {
        "ollama": _state("ollama"),  # unscored
        "zai": _state(
            "zai", weekly_remaining_fraction=0.50, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    # zai is the only scored provider -> fronts despite high flap_margin
    # and despite not being table-order-first.
    assert plan.immediate_candidates[0] == "zai"


def test_pace_failover_reason_when_pace_reranks_primary() -> None:
    """pace_failover fires only when pace re-ranked the primary off the front
    AND the primary is still in immediate (pace moved it, not gate/24h)."""
    config = RoutingConfig(strategy=RoutingStrategy.PACE)
    table = RouteTable(entries={}, default_providers=("zai", "ollama"))
    # zai (primary) has lower surplus; ollama has higher surplus.
    # pace re-ranks -> ollama fronts -> pace_failover.
    states = {
        "zai": _state(
            "zai", weekly_remaining_fraction=0.10, weekly_reset_in=86400.0,
        ),
        "ollama": _state(
            "ollama", weekly_remaining_fraction=0.90, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    assert plan.immediate_candidates[0] == "ollama"
    assert plan.reason == "pace_failover"


def test_pace_failover_reason_not_set_when_primary_busy() -> None:
    """When the primary is BUSY and pace re-ranked the fallbacks, the reason
    should be 'failover' not 'pace_failover' — pace didn't move the primary."""
    config = RoutingConfig(strategy=RoutingStrategy.PACE)
    table = RouteTable(entries={}, default_providers=("umans", "zai", "ollama"))
    # umans (primary) is BUSY -> not in immediate. zai and ollama are scored
    # with ollama having higher surplus -> pace re-ranks them.
    states = {
        "umans": _state("umans", availability=Availability.BUSY),
        "zai": _state(
            "zai", weekly_remaining_fraction=0.10, weekly_reset_in=86400.0,
        ),
        "ollama": _state(
            "ollama", weekly_remaining_fraction=0.90, weekly_reset_in=86400.0,
        ),
    }
    plan = route_decision(states, table, "k", config, now=100.0)
    assert plan.immediate_candidates[0] == "ollama"
    # Primary is not in immediate -> not pace_failover.
    assert plan.reason == "failover"


def test_headroom_strategy_equivalent_to_headroom_ranking() -> None:
    """RoutingStrategy.HEADROOM produces the same ordering as
    headroom_ranking=True."""
    table = RouteTable(entries={}, default_providers=("zai", "ollama"))
    states = {
        "zai": _state("zai", usage_headroom=0.2),
        "ollama": _state("ollama", usage_headroom=0.9),
    }
    config_enum = RoutingConfig(strategy=RoutingStrategy.HEADROOM)
    config_flag = RoutingConfig(headroom_ranking=True)
    plan_enum = route_decision(states, table, "k", config_enum, now=100.0)
    plan_flag = route_decision(states, table, "k", config_flag, now=100.0)
    assert plan_enum.immediate_candidates == plan_flag.immediate_candidates
