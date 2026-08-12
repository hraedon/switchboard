"""The routing pipeline's invariants, each named for the law it pins (Plan 026).

Plan 026 §2 states three invariants that the pre-pipeline decision only held by
accident — they were properties of where guards happened to sit, so every new
mechanism was a chance to break one silently. These tests state them directly,
so a future signal that violates one fails here by name rather than surfacing
as "why did this request 503".

The rest of the file pins the *explanation* (W1.1/W1.3): the tier, signals,
score and rank a candidate is assessed with. Those are a public contract now —
the explain endpoint, the decision log and the dashboard all render them.
"""

from __future__ import annotations

from switchboard.control import (
    AdmissionPlan,
    Availability,
    CandidateAssessment,
    ProviderCapabilities,
    ProviderState,
    RouteAffinity,
    RouteEntry,
    RouteTable,
    RoutingConfig,
    RoutingStrategy,
    SignalFreshness,
    Tier,
    _stage_classify,
    route_decision,
)


def _state(
    name: str,
    *,
    availability: Availability = Availability.AVAILABLE,
    signal_freshness: SignalFreshness = SignalFreshness.FRESH,
    usage_headroom: float | None = None,
    token_utilization: float | None = None,
    token_soft_threshold: float | None = None,
    usage_24h_utilization: float | None = None,
    weekly_remaining_fraction: float | None = None,
    weekly_reset_in: float | None = None,
    in_peak: bool = False,
) -> ProviderState:
    return ProviderState(
        name=name,
        availability=availability,
        available_permits=3,
        queue_depth=0,
        retry_after_seconds=None,
        signal_freshness=signal_freshness,
        usage_headroom=usage_headroom,
        token_utilization=token_utilization,
        token_soft_threshold=token_soft_threshold,
        usage_24h_utilization=usage_24h_utilization,
        weekly_remaining_fraction=weekly_remaining_fraction,
        weekly_reset_in=weekly_reset_in,
        in_peak=in_peak,
    )


def _table(*providers: str) -> RouteTable:
    return RouteTable(entries={}, default_providers=providers)


def _by_name(plan: AdmissionPlan) -> dict[str, CandidateAssessment]:
    return {a.name: a for a in plan.assessments}


def _everywhere(plan: AdmissionPlan, name: str) -> bool:
    """Is ``name`` still reachable by the plan at all?"""
    return (
        name in plan.immediate_candidates
        or plan.queue_candidate == name
        or plan.terminal_fallback == name
        or name in {a.name for a in plan.assessments}
    )


# ── Invariant: demote, never drop ─────────────────────────────────────────
#
# No cost/pressure/staleness signal may remove a provider from the plan
# entirely. Only Filter excludes, and Filter is hard-constraints-only.


def test_invariant_demote_never_drop_low_headroom() -> None:
    plan = route_decision(
        {
            "primary": _state("primary"),
            "fallback": _state("fallback", usage_headroom=0.01),
        },
        _table("primary", "fallback"),
        "k",
        RoutingConfig(headroom_threshold=0.2),
        now=100.0,
    )
    assert _everywhere(plan, "fallback")
    assert _by_name(plan)["fallback"].tier is Tier.QUEUE
    assert "low_headroom" in _by_name(plan)["fallback"].signals


def test_invariant_demote_never_drop_token_budget() -> None:
    plan = route_decision(
        {
            "primary": _state("primary"),
            "fallback": _state("fallback", token_utilization=0.99),
        },
        _table("primary", "fallback"),
        "k",
        RoutingConfig(token_budget_threshold=0.8),
        now=100.0,
    )
    assert _everywhere(plan, "fallback")
    assert _by_name(plan)["fallback"].tier is Tier.QUEUE
    assert "over_budget" in _by_name(plan)["fallback"].signals


def test_invariant_demote_never_drop_trailing_24h_even_for_primary() -> None:
    """The one signal allowed to demote the primary must still not drop it."""
    plan = route_decision(
        {"primary": _state("primary", usage_24h_utilization=0.95)},
        _table("primary"),
        "k",
        RoutingConfig(usage_24h_threshold=0.9),
        now=100.0,
    )
    assert plan.queue_candidate == "primary"
    assert plan.terminal_fallback == "primary"
    assert _by_name(plan)["primary"].tier is Tier.QUEUE
    assert "over_24h" in _by_name(plan)["primary"].signals


def test_invariant_demote_never_drop_peak_window() -> None:
    """Plan 025's peak demotion is expensive-not-broken: it must not exclude."""
    plan = route_decision(
        {
            "primary": _state("primary", in_peak=True),
            "fallback": _state("fallback"),
        },
        _table("primary", "fallback"),
        "k",
        RoutingConfig(),
        now=100.0,
    )
    assert _everywhere(plan, "primary")
    assert plan.queue_candidate == "primary"
    assert plan.immediate_candidates == ("fallback",)
    assert _by_name(plan)["primary"].tier is Tier.QUEUE
    assert "in_peak" in _by_name(plan)["primary"].signals


def test_invariant_demote_never_drop_every_signal_at_once() -> None:
    """Signals compose additively; a pile-up still demotes rather than drops."""
    plan = route_decision(
        {
            "primary": _state("primary"),
            "fallback": _state(
                "fallback",
                availability=Availability.BUSY,
                usage_headroom=0.01,
                token_utilization=0.99,
                usage_24h_utilization=0.95,
                in_peak=True,
            ),
        },
        _table("primary", "fallback"),
        "k",
        RoutingConfig(
            headroom_threshold=0.2,
            token_budget_threshold=0.8,
            usage_24h_threshold=0.9,
        ),
        now=100.0,
    )
    assessment = _by_name(plan)["fallback"]
    assert assessment.tier is Tier.QUEUE
    assert assessment.signals == (
        "busy", "low_headroom", "over_budget", "over_24h", "in_peak",
    )


def test_invariant_only_filter_excludes_and_it_is_hard_constraints_only() -> None:
    """A model the provider does not serve is a hard constraint: it excludes.

    The contrast with the tests above is the whole point — a filtered
    candidate holds no tier and gets no assessment, and a *demoted* one always
    does.
    """
    plan = route_decision(
        {"primary": _state("primary"), "fallback": _state("fallback")},
        _table("primary", "fallback"),
        "k",
        RoutingConfig(),
        now=100.0,
        servable_providers=frozenset({"primary"}),
    )
    assert plan.immediate_candidates == ("primary",)
    assert "fallback" not in {a.name for a in plan.assessments}


# ── Invariant: stale never outranks fresh ─────────────────────────────────
#
# BACKSTOP sorts after every fresh tier member in queue-candidate selection.


def test_invariant_stale_never_outranks_fresh_in_queue_selection() -> None:
    """A BUSY fresh fallback beats a DEGRADED one for the queue slot."""
    plan = route_decision(
        {
            "primary": _state("primary", availability=Availability.CLOSED),
            "fresh-busy": _state("fresh-busy", availability=Availability.BUSY),
            "stale": _state(
                "stale", signal_freshness=SignalFreshness.DEGRADED
            ),
        },
        _table("primary", "fresh-busy", "stale"),
        "k",
        RoutingConfig(),
        now=100.0,
    )
    assert plan.queue_candidate == "fresh-busy"
    assessments = _by_name(plan)
    assert assessments["fresh-busy"].tier is Tier.QUEUE
    assert assessments["stale"].tier is Tier.BACKSTOP
    # Tuple position is the decision's own preference order.
    names = [a.name for a in plan.assessments]
    assert names.index("fresh-busy") < names.index("stale")


def test_invariant_stale_never_outranks_fresh_but_beats_a_503() -> None:
    """Last resort is still a resort: a DEGRADED fallback takes the queue slot
    when every fresh candidate is gone (Plan 022 containment)."""
    plan = route_decision(
        {
            "primary": _state("primary", availability=Availability.CLOSED),
            "stale": _state(
                "stale", signal_freshness=SignalFreshness.DEGRADED
            ),
        },
        _table("primary", "stale"),
        "k",
        RoutingConfig(),
        now=100.0,
    )
    assert plan.queue_candidate == "stale"
    assert plan.reason == "queue_only"
    assert _by_name(plan)["stale"].tier is Tier.BACKSTOP


def test_invariant_stale_never_outranks_fresh_is_never_immediate() -> None:
    """A DEGRADED fallback is not a new immediate failover target, ever."""
    plan = route_decision(
        {
            "primary": _state("primary", availability=Availability.CLOSED),
            "stale": _state(
                "stale", signal_freshness=SignalFreshness.DEGRADED
            ),
        },
        _table("primary", "stale"),
        "k",
        RoutingConfig(),
        now=100.0,
    )
    assert plan.immediate_candidates == ()


def test_invariant_unknown_freshness_is_excluded_not_backstopped() -> None:
    """UNKNOWN is not stale-but-usable: unknown data never maps to zero
    pressure, so it holds no tier at all."""
    plan = route_decision(
        {
            "primary": _state("primary"),
            "never-polled": _state(
                "never-polled",
                availability=Availability.UNKNOWN,
                signal_freshness=SignalFreshness.UNKNOWN,
            ),
        },
        _table("primary", "never-polled"),
        "k",
        RoutingConfig(),
        now=100.0,
    )
    assert "never-polled" not in {a.name for a in plan.assessments}


# ── Invariant: signals are facts; policy lives in Classify/Rank ───────────


def test_invariant_signals_are_facts_classify_owns_the_policy() -> None:
    """``in_peak`` is a boolean the shell computes; the tier is Classify's.

    The shell reads the wall clock and hands over a fact. Nothing in the shell
    orders candidates, so the same fact must produce the tier here.
    """
    classified = _stage_classify(
        ("primary", "fallback"),
        {
            "primary": _state("primary", in_peak=True),
            "fallback": _state("fallback"),
        },
        RoutingConfig(),
        "primary",
    )
    assert classified.immediate == ["fallback"]
    assert classified.queue == ["primary"]
    assert classified.signals["primary"] == ("in_peak",)
    assert classified.signals["fallback"] == ()


def test_invariant_proactive_signals_never_demote_the_primary() -> None:
    """Headroom and token budget are the primary's own gate's business.

    Reported as *not fired* for the primary rather than fired-and-ignored:
    the predicate carries the policy, so the explanation says what the
    decision actually used.
    """
    plan = route_decision(
        {"primary": _state("primary", usage_headroom=0.01, token_utilization=0.99)},
        _table("primary"),
        "k",
        RoutingConfig(headroom_threshold=0.2, token_budget_threshold=0.8),
        now=100.0,
    )
    assert plan.immediate_candidates == ("primary",)
    assert _by_name(plan)["primary"].signals == ()


# ── The assessments themselves (W1.1, W1.3) ───────────────────────────────


def test_admission_plan_assessments_default_to_empty() -> None:
    """W1.1's back-compat clause: every pre-pipeline constructor still works."""
    plan = AdmissionPlan(
        immediate_candidates=("a",),
        queue_candidate=None,
        terminal_fallback="a",
        reason="primary_available",
    )
    assert plan.assessments == ()


def test_assessment_of_a_clean_immediate_candidate_has_no_signals() -> None:
    plan = route_decision(
        {"primary": _state("primary")},
        _table("primary"),
        "k",
        RoutingConfig(),
        now=100.0,
    )
    (assessment,) = plan.assessments
    assert assessment == CandidateAssessment(
        name="primary", tier=Tier.IMMEDIATE, signals=(), score=None, rank=0
    )


def test_assessment_busy_is_a_signal_not_a_demotion() -> None:
    plan = route_decision(
        {"primary": _state("primary", availability=Availability.BUSY)},
        _table("primary"),
        "k",
        RoutingConfig(),
        now=100.0,
    )
    assert _by_name(plan)["primary"] == CandidateAssessment(
        name="primary", tier=Tier.QUEUE, signals=("busy",), score=None, rank=0
    )


def test_assessment_degraded_primary_keeps_serving_and_says_so() -> None:
    """A DEGRADED *primary* stays immediate on last-known-good — and the
    ``degraded`` signal travels with it so the operator knows why the numbers
    on screen may be stale."""
    plan = route_decision(
        {"primary": _state("primary", signal_freshness=SignalFreshness.DEGRADED)},
        _table("primary"),
        "k",
        RoutingConfig(),
        now=100.0,
    )
    assessment = _by_name(plan)["primary"]
    assert assessment.tier is Tier.IMMEDIATE
    assert assessment.signals == ("degraded",)


def test_assessment_per_provider_soft_threshold_fires_over_budget() -> None:
    """A per-provider ``soft_threshold`` overrides the global one (Plan 012)."""
    plan = route_decision(
        {
            "primary": _state("primary"),
            "fallback": _state(
                "fallback", token_utilization=0.5, token_soft_threshold=0.4
            ),
        },
        _table("primary", "fallback"),
        "k",
        RoutingConfig(token_budget_threshold=0.0),
        now=100.0,
    )
    assert _by_name(plan)["fallback"].signals == ("over_budget",)


def test_assessment_ranks_are_per_tier_and_zero_based() -> None:
    plan = route_decision(
        {
            "primary": _state("primary"),
            "second": _state("second"),
            "busy": _state("busy", availability=Availability.BUSY),
            "stale": _state("stale", signal_freshness=SignalFreshness.DEGRADED),
        },
        _table("primary", "second", "busy", "stale"),
        "k",
        RoutingConfig(),
        now=100.0,
    )
    assert [(a.name, a.tier.value, a.rank) for a in plan.assessments] == [
        ("primary", "immediate", 0),
        ("second", "immediate", 1),
        ("busy", "queue", 0),
        ("stale", "backstop", 0),
    ]


def test_assessment_score_is_none_under_the_ordered_strategy() -> None:
    plan = route_decision(
        {
            "primary": _state("primary", usage_headroom=0.2),
            "fallback": _state("fallback", usage_headroom=0.9),
        },
        _table("primary", "fallback"),
        "k",
        RoutingConfig(strategy=RoutingStrategy.ORDERED),
        now=100.0,
    )
    assert all(a.score is None for a in plan.assessments)


def test_assessment_score_is_headroom_under_the_headroom_strategy() -> None:
    plan = route_decision(
        {
            "primary": _state("primary", usage_headroom=0.2),
            "fallback": _state("fallback", usage_headroom=0.9),
        },
        _table("primary", "fallback"),
        "k",
        RoutingConfig(strategy=RoutingStrategy.HEADROOM),
        now=100.0,
    )
    scores = {a.name: a.score for a in plan.assessments}
    assert scores == {"primary": 0.2, "fallback": 0.9}
    # Today the stickiness overlay re-fronts the primary afterwards, so the
    # better-scoring fallback does NOT lead. That is deliberate Wave-1
    # behaviour preservation, and exactly what Plan 026 W2.1 changes; pinning
    # it here means the Wave-2 flip has to be a deliberate edit to this line.
    assert plan.immediate_candidates == ("primary", "fallback")
    assert _by_name(plan)["primary"].rank == 0


def test_assessment_headroom_score_agrees_with_the_order_it_produced() -> None:
    """With the primary out of the running, the ranking stands on its own."""
    plan = route_decision(
        {
            "primary": _state("primary", availability=Availability.CLOSED),
            "low": _state("low", usage_headroom=0.2),
            "high": _state("high", usage_headroom=0.9),
        },
        _table("primary", "low", "high"),
        "k",
        RoutingConfig(strategy=RoutingStrategy.HEADROOM),
        now=100.0,
    )
    assert plan.immediate_candidates == ("high", "low")
    assert _by_name(plan)["high"].rank == 0
    assert _by_name(plan)["high"].score == 0.9


def test_assessment_score_is_surplus_under_the_pace_strategy() -> None:
    plan = route_decision(
        {
            "primary": _state(
                "primary", weekly_remaining_fraction=0.2, weekly_reset_in=86400.0
            ),
            "rich": _state(
                "rich", weekly_remaining_fraction=0.9, weekly_reset_in=86400.0
            ),
        },
        _table("primary", "rich"),
        "k",
        RoutingConfig(strategy=RoutingStrategy.PACE, pace_burn_rate_per_day=0.1),
        now=100.0,
    )
    scores = {a.name: a.score for a in plan.assessments}
    assert scores["rich"] is not None and scores["primary"] is not None
    assert abs(scores["rich"] - 0.8) < 1e-9
    assert abs(scores["primary"] - 0.1) < 1e-9
    assert plan.immediate_candidates[0] == "rich"


def test_assessment_score_is_none_for_an_unscored_pace_candidate() -> None:
    """Unscored is not zero: a provider with no fresh weekly signal ranks in
    table order behind the scored ones and reports no score."""
    plan = route_decision(
        {
            "primary": _state(
                "primary", weekly_remaining_fraction=0.9, weekly_reset_in=86400.0
            ),
            "silent": _state("silent"),
        },
        _table("primary", "silent"),
        "k",
        RoutingConfig(strategy=RoutingStrategy.PACE),
        now=100.0,
    )
    assert _by_name(plan)["silent"].score is None


def test_assessment_score_is_none_outside_the_immediate_tier() -> None:
    """Only IMMEDIATE is ranked by the strategy, so only it carries a score."""
    plan = route_decision(
        {
            "primary": _state("primary", usage_headroom=0.5),
            "busy": _state(
                "busy", availability=Availability.BUSY, usage_headroom=0.9
            ),
        },
        _table("primary", "busy"),
        "k",
        RoutingConfig(strategy=RoutingStrategy.HEADROOM),
        now=100.0,
    )
    assert _by_name(plan)["busy"].score is None


def test_assessment_reflects_the_stickiness_overlay() -> None:
    """Rank 0 is what will actually be tried first, pin included."""
    plan = route_decision(
        {"primary": _state("primary"), "fallback": _state("fallback")},
        _table("primary", "fallback"),
        "k",
        RoutingConfig(dwell_interval=30.0),
        now=100.0,
        affinity=RouteAffinity(provider="fallback", selected_at=90.0),
    )
    assert plan.reason == "affinity_dwell"
    assert plan.immediate_candidates == ("fallback", "primary")
    assert _by_name(plan)["fallback"].rank == 0
    assert _by_name(plan)["primary"].rank == 1


def test_a_pinned_provider_that_gets_demoted_holds_no_immediate_rank() -> None:
    """Stickiness may promote within a tier, never across one — structurally:
    a demoted provider is not in the IMMEDIATE tier for the pin to reorder."""
    plan = route_decision(
        {
            "primary": _state("primary"),
            "fallback": _state("fallback", in_peak=True),
        },
        _table("primary", "fallback"),
        "k",
        RoutingConfig(dwell_interval=30.0, pin_conversations=True),
        now=100.0,
        affinity=RouteAffinity(provider="fallback", selected_at=90.0),
    )
    assert plan.immediate_candidates == ("primary",)
    assert _by_name(plan)["fallback"].tier is Tier.QUEUE
    assert _by_name(plan)["primary"].tier is Tier.IMMEDIATE


def test_no_assessments_when_a_hard_filter_empties_the_candidate_set() -> None:
    """Nothing survived to classify, so there is nothing to explain — but the
    terminal fallback still names a gate that can answer honestly."""
    plan = route_decision(
        {"primary": _state("primary")},
        _table("primary"),
        "k",
        RoutingConfig(),
        now=100.0,
        servable_providers=frozenset({"nobody"}),
    )
    assert plan.reason == "model_unservable"
    assert plan.assessments == ()
    assert plan.terminal_fallback == "primary"


def test_capability_filtering_also_yields_no_assessments() -> None:
    caps = ProviderCapabilities(
        surfaces=frozenset({"chat-completions"}), api_family="openai"
    )
    table = RouteTable(
        entries={
            "k": RouteEntry(
                key="k",
                providers=("primary",),
                required_capabilities=frozenset({"messages"}),
            )
        },
        default_providers=("primary",),
    )
    plan = route_decision(
        {
            "primary": ProviderState(
                name="primary",
                availability=Availability.AVAILABLE,
                available_permits=3,
                queue_depth=0,
                retry_after_seconds=None,
                signal_freshness=SignalFreshness.FRESH,
                capabilities=caps,
            )
        },
        table,
        "k",
        RoutingConfig(),
        now=100.0,
    )
    assert plan.reason == "capability_filtered"
    assert plan.assessments == ()
