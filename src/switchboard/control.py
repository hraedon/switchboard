"""Pure, deterministic routing core — the truth path.

This module is the routing decision engine. It imports **nothing outside the
standard library**, does **no I/O**, and reads **no clock**: the current time
and every provider state are passed in as arguments so decisions are fully
reproducible and unit-testable without a network.

Enforced by tests/test_import_boundary.py.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum


class Availability(Enum):
    """Provider availability state for routing decisions.

    Categorical eligibility replaces the scalar pressure comparison from
    Plans 001-005.  The decision proceeds: filter out CLOSED, separate by
    signal freshness, then place AVAILABLE candidates ahead of BUSY ones.
    """

    AVAILABLE = "available"  # eligible and permit available now
    BUSY = "busy"  # eligible but no permit available now
    CLOSED = "closed"  # boxed, breaker-open, administratively closed
    UNKNOWN = "unknown"  # not ready or signal too stale to trust


class SignalFreshness(Enum):
    """How fresh the provider's truth signal is.

    Staleness semantics (Plan 006 §3.2):

    * ``FRESH`` — may be selected or preferred normally.
    * ``DEGRADED`` — last-known-good may keep an already-primary route serving
      within a bounded TTL, but it is not a new failover target.
    * ``UNKNOWN`` — excluded from failover preference; admitted only under an
      explicit route policy.  Unknown data never maps to zero pressure.
    """

    FRESH = "fresh"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declarative provider capability metadata (Plan 008 §4).

    Routes declare required capability surfaces; the router filters
    incompatible candidates before pressure/admission ranking.  No request
    body inspection is performed.
    """

    surfaces: frozenset[str]  # e.g. {"chat-completions", "messages"}
    api_family: str  # exact wire contract identifier
    streaming: bool = True
    tool_calling_profile: str | None = None
    context_class: str | None = None
    credential_domain: str = ""
    cache_domain: str = ""


@dataclass(frozen=True)
class RouteAffinity:
    """Bounded route affinity state for stickiness/failback (Plan 008 §5).

    Supplied explicitly to the pure core by the proxy.  The pure function
    uses this as input only — it does not update affinity state.  The caller
    (proxy) updates affinity after failover or failback.
    """

    provider: str
    selected_at: float
    failover_reason: str = ""
    healthy_observations: int = 0


@dataclass(frozen=True)
class ProviderState:
    """Snapshot of one provider's state at a point in time.

    Assembled by the shell from each provider's reconcile loop and gate.
    Pure data — no I/O, no clock.
    """

    name: str
    availability: Availability
    available_permits: int
    queue_depth: int
    retry_after_seconds: int | None
    signal_freshness: SignalFreshness
    capabilities: ProviderCapabilities | None = None
    usage_headroom: float | None = None
    quota_resets_in: float | None = None
    # Seconds until the quota window this headroom refers to resets.
    # None = unknown (never promotes -- fail safe).
    token_utilization: float | None = None
    usage_24h_utilization: float | None = None
    # tokens_24h / cap_tokens (Plan 013). 0.0 = none used; 1.0 = at cap.
    # None = no 24h budget configured or no data (no filtering).


@dataclass(frozen=True)
class RouteEntry:
    """A route table entry mapping a hashed key to an ordered provider list."""

    key: str  # SHA-256 hash of the raw API key
    providers: tuple[str, ...]  # ordered: [primary, fallback_1, ...]
    required_capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RouteTable:
    """The full route table. Entries + a default provider list."""

    entries: dict[str, RouteEntry] = field(default_factory=dict)
    default_providers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelMap:
    """Per-provider model-name aliases (Plan 010 Feature B).

    Providers label the same model differently (umans ``umans-kimi-k2.7`` vs
    ollama-cloud ``kimi-k2.7-code``).  ``routes`` maps the **incoming** model
    string (what the client sends) to ``{provider_name: that provider's model
    string}``.  Used for two things:

    * **Candidate filtering** — only providers with an alias for the requested
      model can serve it, so failover never routes a model to a provider that
      doesn't offer it.
    * **Egress rewrite** — the ``model`` field is rewritten to the chosen
      provider's alias (only when it differs, so the primary path stays
      byte-identical).

    A model absent from ``routes`` is not filtered or rewritten — switchboard
    behaves exactly as today (forward original bytes).  Empty ``routes`` = the
    whole feature is off.
    """

    routes: dict[str, dict[str, str]] = field(default_factory=dict)

    def __contains__(self, model: str) -> bool:
        return model in self.routes

    def providers_for(self, model: str) -> frozenset[str]:
        """Providers that declare an alias for ``model`` (empty if unmapped)."""
        entry = self.routes.get(model)
        return frozenset(entry.keys()) if entry else frozenset()

    def alias_for(self, model: str, provider: str) -> str | None:
        """The model string ``provider`` expects for ``model``, or None."""
        entry = self.routes.get(model)
        return entry.get(provider) if entry else None


@dataclass(frozen=True)
class RoutingConfig:
    """Routing engine parameters.

    ``failover_threshold_seconds`` and ``failover_margin`` are retained for
    display and potential future tie-breaking but are no longer the primary
    decision mechanism (Plan 006 replaced scalar pressure comparison with
    categorical eligibility).

    ``dwell_interval`` (Plan 008 §5) is the minimum time in seconds to stay
    on a fallback before failing back to the primary.

    ``failback_delay`` (Plan 014) is the minimum continuous time in seconds
    the primary must be FRESH+AVAILABLE before an affinity pin is released.
    0.0 = disabled (Plan 008 §5 behaviour: fail back on the first healthy
    poll after ``dwell_interval``).
    """

    failover_threshold_seconds: int = 10
    failover_margin: int = 5
    dwell_interval: float = 30.0
    failback_delay: float = 0.0
    headroom_threshold: float = 0.0
    headroom_ranking: bool = False
    # Order `immediate` candidates by usage_headroom (descending) before
    # affinity/primary fronting. Providers without headroom data sort after
    # data-bearing ones, in table order.
    token_budget_threshold: float = 0.0
    usage_24h_threshold: float = 0.0
    # 0.0 = disabled. >0 = providers whose usage_24h_utilization >= this are
    # demoted from immediate to queue_eligible — INCLUDING the primary
    # (Plan 013 §2: the trailing-24h penalty is what the primary's gate
    # cannot see coming, so the usual no-primary-demotion rule does not
    # apply to this signal).
    opportunistic_enabled: bool = False
    opportunistic_min_headroom: float = 0.5
    # only when >= half the window remains
    opportunistic_reset_window: float = 21600.0
    # seconds; only inside the last 6 h
    opportunistic_margin: float = 0.10
    # winner must lead the runner-up by this


@dataclass(frozen=True)
class AdmissionPlan:
    """Ordered admission plan produced by the routing decision.

    The proxy consumes this plan as follows:

    1. Try each ``immediate_candidate`` with a non-blocking gate acquire
       (``timeout=0``).
    2. Forward through the first successful acquisition.
    3. If all immediate attempts lose the snapshot race, perform one final
       non-blocking pass over the remaining eligible candidates.
    4. If configured, wait only on ``queue_candidate`` for the remaining
       queue budget.
    5. After queue timeout, return an honest 503 derived from
       ``terminal_fallback``'s structural signal.
    """

    immediate_candidates: tuple[str, ...]
    queue_candidate: str | None
    terminal_fallback: str
    reason: str


def hash_route_key(raw_key: str) -> str:
    """SHA-256 hash of the raw API key. Pure, deterministic."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


#: A path segment that names an API version: v1, v2, v1beta, v1alpha2.
_VERSION_SEGMENT = re.compile(r"^v\d+(?:[a-z]+\d*)?$")


def compose_upstream_path(base: str, client_path: str) -> str:
    """Compose an upstream URL from a provider base and the client's path.

    Plan 021 D2. Clients must not have to accommodate switchboard: pointing
    one at ``https://switchboard.<host>/v1`` — the shape every
    OpenAI-compatible ``baseURL`` conventionally takes — has to work. But the
    provider base is most useful when it can be pasted verbatim from the
    vendor's own quickstart, and those usually already end in a version
    (``https://ollama.com/v1``, ``.../zen/go/v1``, ``.../paas/v4``). Naive
    concatenation doubles the version and 404s.

    **The base declares the version if it has one.** When the base's last
    segment looks like a version, a leading version segment on the client
    path is redundant and is dropped. When the base carries no version, the
    client's is preserved — that is what keeps a bare-host base (the natural
    OpenAI-style setup, working today) working unchanged.

    At most one segment is ever dropped, and only in leading position, so a
    ``v1`` appearing later in an endpoint is left alone.

        >>> compose_upstream_path("https://ollama.com/v1", "/v1/chat/completions")
        'https://ollama.com/v1/chat/completions'
        >>> compose_upstream_path("https://api.example.com", "/v1/chat/completions")
        'https://api.example.com/v1/chat/completions'

    Pure and deterministic: no I/O, no network, no clock.
    """
    base = base.rstrip("/")
    path, sep, query = client_path.partition("?")

    segments = [s for s in path.split("/") if s]

    base_tail = base.rsplit("/", 1)[-1]
    if (
        segments
        and _VERSION_SEGMENT.match(segments[0])
        and _VERSION_SEGMENT.match(base_tail)
    ):
        segments = segments[1:]

    composed = base + ("/" + "/".join(segments) if segments else "")
    return composed + (sep + query if sep else "")


def _satisfies_capabilities(
    state: ProviderState,
    required: frozenset[str],
) -> bool:
    """Check whether a provider satisfies required capability surfaces.

    Providers without capabilities metadata are NOT filtered (backward
    compat).  A provider satisfies requirements if all required surfaces are
    in the provider's capabilities surfaces.
    """
    if not required:
        return True
    caps = state.capabilities
    if caps is None:
        return True
    return required <= caps.surfaces


def _opportunistic_target(
    immediate: list[str],
    primary: str,
    states: dict[str, ProviderState],
    config: RoutingConfig,
) -> str | None:
    """Select an opportunistic quota-burn target, or None.

    A candidate qualifies when it is not the primary, is in ``immediate``
    (therefore FRESH and AVAILABLE), reports measured headroom above the
    configured floor, and reports a quota reset within the burn window.
    The best qualifier wins only if it leads the runner-up by the configured
    margin (a single qualifier needs no margin).  Ties break on ``immediate``
    order (table order after ranking).
    """
    if not config.opportunistic_enabled:
        return None

    qualifiers: list[tuple[str, float]] = []
    for name in immediate:
        if name == primary:
            continue
        state = states.get(name)
        if state is None:
            continue
        headroom = state.usage_headroom
        if headroom is None or headroom < config.opportunistic_min_headroom:
            continue
        resets_in = state.quota_resets_in
        if resets_in is None or not (0.0 < resets_in <= config.opportunistic_reset_window):
            continue
        qualifiers.append((name, headroom))

    if not qualifiers:
        return None

    # argmax headroom; deterministic tiebreak: earlier in immediate order.
    order = {name: idx for idx, name in enumerate(immediate)}

    def _sort_key(item: tuple[str, float]) -> tuple[float, int]:
        return (-item[1], order[item[0]])

    qualifiers.sort(key=_sort_key)
    best_name, best_headroom = qualifiers[0]
    if len(qualifiers) == 1:
        return best_name
    runnerup_headroom = qualifiers[1][1]
    if best_headroom - runnerup_headroom >= config.opportunistic_margin:
        return best_name
    return None


def route_decision(
    states: dict[str, ProviderState],
    table: RouteTable,
    route_key: str,
    config: RoutingConfig,
    *,
    now: float,
    affinity: RouteAffinity | None = None,
    servable_providers: frozenset[str] | None = None,
    primary_healthy_since: float | None = None,
) -> AdmissionPlan:
    """Pure routing decision. Returns an :class:`AdmissionPlan`.

    The decision proceeds in this order (Plans 006, 008, 014, 015, 016):

    1. Resolve the route key to an ordered candidate list.
    2. Filter out candidates whose capabilities don't satisfy the route's
       required capabilities (Plan 008 §4).
    3. Reject missing and closed candidates.
    4. Separate fresh candidates from unknown/stale candidates.
    5. Place candidates with immediate permits first.
    6. Order immediate candidates by ``usage_headroom`` descending when
       ``headroom_ranking`` is enabled (Plan 015); data-bearing candidates
       precede ones without headroom data; ties break on table order.
    7. Apply affinity stickiness / dwell / failback logic (Plan 008 §5).
       When ``failback_delay`` is configured and the primary has not been
       continuously FRESH+AVAILABLE for that duration, the affinity pin is
       held past ``dwell_interval`` (Plan 014).  Subordinate to an active
       affinity pin, opportunistically front a qualifying quota-burn
       fallback (Plan 016); the primary stays immediate-eligible and the
       terminal fallback.
    8. Preserve primary preference among equally admissible candidates when
       neither an affinity pin nor an opportunistic target pinned the front.
    9. Select at most one explicit queue candidate.
    10. Preserve the configured primary as the terminal safe-failure target
        so its gate can provide the canonical rejection when nothing is usable.

    Guarantees:

    * **Fail safe** — when all providers are closed, the plan's
      ``terminal_fallback`` is the primary; the proxy forwards to it and lets
      its gate return 503.  Never silently drop a request.
    * **Stale data never improves preference** — unknown/stale providers are
      excluded from failover by default (``fresh-only-for-failover`` policy).
    * **Capability filtering** — providers whose declared surfaces don't
      include all required surfaces are excluded before admission ranking.
    * **Bounded stickiness** — after failover, the routing core prefers the
      affinity provider for at least ``dwell_interval`` seconds before
      considering failback to the primary.
    * **Failback hysteresis** — when ``failback_delay > 0``, failback to the
      primary requires the primary to have been continuously FRESH+AVAILABLE
      for at least ``failback_delay`` seconds.  A single unhealthy poll resets
      the continuity clock.
    * **Opt-in headroom ranking** — when ``headroom_ranking`` is enabled,
      immediate candidates are ordered by ``usage_headroom`` descending before
      affinity/primary fronting; absence of data never outranks a measured
      provider.
    * **Opportunistic quota burn (Plan 016)** — opt-in; subordinate to an
      active affinity pin; de-preference only: the primary remains
      immediate-eligible, queue backstop, and terminal fallback.  Stale or
      unmeasured data never promotes.
    * **Pure** — ``now``, ``primary_healthy_since``, and all states are
      arguments.  No I/O, no clock.
    * **Deterministic** — same inputs produce the same plan.
    """
    entry = table.entries.get(route_key)
    candidates = entry.providers if entry is not None else table.default_providers

    if not candidates:
        raise ValueError("no providers configured")

    primary = candidates[0]

    # --- Model-servability filtering (Plan 010 Feature B) ---
    # When the request's model is mapped, only providers that declare an alias
    # for it are eligible — failover never routes a model to a provider that
    # doesn't serve it.  ``None`` means unmapped/no-map: no filtering (today's
    # behaviour).  The configured primary is preserved as terminal_fallback so a
    # fully-unservable request still gets a canonical rejection from its gate.
    if servable_providers is not None:
        servable = tuple(n for n in candidates if n in servable_providers)
        if not servable:
            return AdmissionPlan(
                immediate_candidates=(),
                queue_candidate=None,
                terminal_fallback=primary,
                reason="model_unservable",
            )
        candidates = servable
        primary = candidates[0]

    # --- Capability filtering (Plan 008 §4) ---
    required_caps = entry.required_capabilities if entry is not None else frozenset()
    if required_caps:
        filtered: list[str] = []
        for name in candidates:
            state = states.get(name)
            if state is None:
                filtered.append(name)
                continue
            if _satisfies_capabilities(state, required_caps):
                filtered.append(name)
        if not filtered:
            return AdmissionPlan(
                immediate_candidates=(),
                queue_candidate=None,
                terminal_fallback=primary,
                reason="capability_filtered",
            )
        candidates = tuple(filtered)
        primary = candidates[0]

    immediate: list[str] = []
    queue_eligible: list[str] = []

    for name in candidates:
        state = states.get(name)
        if state is None:
            continue
        if state.availability == Availability.CLOSED:
            continue

        is_primary = name == primary

        if (
            state.signal_freshness == SignalFreshness.FRESH
            or (
                state.signal_freshness == SignalFreshness.DEGRADED
                and is_primary
            )
        ):
            low_headroom = (
                not is_primary
                and config.headroom_threshold > 0
                and state.usage_headroom is not None
                and state.usage_headroom < config.headroom_threshold
            )
            over_budget = (
                not is_primary
                and config.token_budget_threshold > 0
                and state.token_utilization is not None
                and state.token_utilization
                >= config.token_budget_threshold
            )
            # Plan 013: trailing-24h usage — the ONE proactive signal that
            # may demote the primary (no `not is_primary` guard).  Demotion
            # de-prefers only: the primary stays queue-eligible backstop.
            over_24h = (
                config.usage_24h_threshold > 0
                and state.usage_24h_utilization is not None
                and state.usage_24h_utilization >= config.usage_24h_threshold
            )
            if low_headroom or over_budget or over_24h:
                queue_eligible.append(name)
            elif state.availability == Availability.AVAILABLE:
                immediate.append(name)
            elif state.availability == Availability.BUSY:
                queue_eligible.append(name)
        # UNKNOWN: excluded from failover preference (fresh-only-for-failover)

    # --- Headroom-ordered fallback ranking (Plan 015) ---
    if config.headroom_ranking and len(immediate) > 1:
        order = {name: i for i, name in enumerate(candidates)}

        def _rank_key(name: str) -> tuple[int, float, int]:
            st = states.get(name)
            h = st.usage_headroom if st else None
            # data-bearing first (headroom desc), then table order
            return (0 if h is not None else 1, -(h or 0.0), order[name])

        immediate.sort(key=_rank_key)

    # --- Affinity stickiness / dwell / failback (Plan 008 §5) ---
    affinity_reason = ""
    affinity_state = (
        states.get(affinity.provider) if affinity is not None else None
    )
    affinity_fresh = (
        affinity_state is not None
        and affinity_state.signal_freshness == SignalFreshness.FRESH
    )
    if (
        affinity is not None
        and affinity.provider != primary
        and affinity.provider in immediate
        and affinity_fresh
    ):
        if (now - affinity.selected_at) < config.dwell_interval:
            immediate.remove(affinity.provider)
            immediate.insert(0, affinity.provider)
            affinity_reason = "affinity_dwell"
        elif primary in immediate:
            hysteresis = (
                config.failback_delay > 0
                and (
                    primary_healthy_since is None
                    or (now - primary_healthy_since) < config.failback_delay
                )
            )
            if hysteresis:
                immediate.remove(affinity.provider)
                immediate.insert(0, affinity.provider)
                affinity_reason = "affinity_hysteresis"
            else:
                immediate.remove(primary)
                immediate.insert(0, primary)
        else:
            immediate.remove(affinity.provider)
            immediate.insert(0, affinity.provider)
    else:
        target = _opportunistic_target(immediate, primary, states, config)
        if target is not None:
            immediate.remove(target)
            immediate.insert(0, target)
            affinity_reason = "opportunistic"
        elif primary in immediate:
            immediate.remove(primary)
            immediate.insert(0, primary)

    # Select at most one queue candidate: prefer primary if eligible.
    queue_candidate: str | None = None
    if primary in queue_eligible:
        queue_candidate = primary
    elif queue_eligible:
        queue_candidate = queue_eligible[0]

    if not immediate and queue_candidate is None:
        return AdmissionPlan(
            immediate_candidates=(),
            queue_candidate=None,
            terminal_fallback=primary,
            reason="no_eligible_candidates",
        )

    if immediate:
        if affinity_reason:
            reason = affinity_reason
        elif immediate[0] == primary:
            reason = "primary_available"
        else:
            reason = "failover"
    else:
        reason = "queue_only"

    return AdmissionPlan(
        immediate_candidates=tuple(immediate),
        queue_candidate=queue_candidate,
        terminal_fallback=primary,
        reason=reason,
    )


# ── Usage-error reroute (Plan 010, reactive half) ─────────────────────────


#: Upstream statuses that mean "this provider cannot serve you right now"
#: rather than "your request is wrong". Every provider in the estate signals
#: exhaustion with one of these: 429 (rate/quota), 402 (billing/credit
#: exhausted), 503/529 (overloaded / temporarily unavailable). 500 and 502 are
#: deliberately absent — a genuine upstream bug or bad gateway is not a usage
#: signal, and rerouting it would silently spray a broken request across every
#: provider in turn.
DEFAULT_REROUTE_STATUSES: frozenset[int] = frozenset({402, 429, 503, 529})


def should_reroute(
    *,
    status: int,
    reroute_statuses: frozenset[int],
    reroutes_done: int,
    max_attempts: int,
    body_replayable: bool,
    response_started: bool,
    alternatives_remain: bool,
) -> bool:
    """Decide whether a usage-error response should be retried elsewhere.

    Pure predicate — the proxy owns the I/O, this owns the rule. Every clause
    is a safety property, not a preference:

    * ``response_started`` — once a byte has reached the client the request is
      committed to that upstream; a "retry" would concatenate two responses.
      This is the invariant that makes rerouting safe at all.
    * ``body_replayable`` — a streamed (unbuffered) body has already been
      consumed by the first attempt and cannot be sent again.
    * ``alternatives_remain`` — retrying the same pressured provider is just a
      slower failure, and is what the client's own retry loop already does.
    * ``reroutes_done``/``max_attempts`` — ``max_attempts`` counts RETRIES, not
      total tries, so 1 means "try the primary, then at most one other".
      Bounded so a fully-exhausted estate degrades to a single error rather
      than a fan-out across every provider in turn.
    """
    if response_started:
        return False
    if not body_replayable:
        return False
    if not alternatives_remain:
        return False
    if reroutes_done >= max_attempts:
        return False
    return status in reroute_statuses
