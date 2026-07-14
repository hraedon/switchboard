"""Pure, deterministic routing core — the truth path.

This module is the routing decision engine. It imports **nothing outside the
standard library**, does **no I/O**, and reads **no clock**: the current time
and every provider state are passed in as arguments so decisions are fully
reproducible and unit-testable without a network.

Enforced by tests/test_import_boundary.py.
"""

from __future__ import annotations

import hashlib
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


class ReplayBoundary(Enum):
    """Points after which automatic retry/replay is forbidden (Plan 008 §6).

    The streaming substrate returns enough typed state to enforce this table.
    The proxy consumes these values to decide whether an alternate provider
    may be tried after a failure.
    """

    BEFORE_PERMIT = "before_permit"  # alternate provider allowed
    PERMIT_ACQUIRED = "permit_acquired"  # allowed after release
    CONNECT_FAILED = "connect_failed"  # only if zero bytes sent
    UPLOAD_STARTED = "upload_started"  # NO replay
    HEADERS_RECEIVED = "headers_received"  # NO replay
    STREAMING = "streaming"  # NO replay


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
    """

    failover_threshold_seconds: int = 10
    failover_margin: int = 5
    dwell_interval: float = 30.0
    headroom_threshold: float = 0.0


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


def route_decision(
    states: dict[str, ProviderState],
    table: RouteTable,
    route_key: str,
    config: RoutingConfig,
    *,
    now: float,
    affinity: RouteAffinity | None = None,
    servable_providers: frozenset[str] | None = None,
) -> AdmissionPlan:
    """Pure routing decision. Returns an :class:`AdmissionPlan`.

    The decision proceeds in this order (Plans 006, 008):

    1. Resolve the route key to an ordered candidate list.
    2. Filter out candidates whose capabilities don't satisfy the route's
       required capabilities (Plan 008 §4).
    3. Reject missing and closed candidates.
    4. Separate fresh candidates from unknown/stale candidates.
    5. Place candidates with immediate permits first.
    6. Apply affinity stickiness / dwell / failback logic (Plan 008 §5).
    7. Preserve primary preference among equally admissible candidates.
    8. Select at most one explicit queue candidate.
    9. Preserve the configured primary as the terminal safe-failure target
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
    * **Pure** — ``now`` and all states are arguments.  No I/O, no clock.
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
            if low_headroom:
                queue_eligible.append(name)
            elif state.availability == Availability.AVAILABLE:
                immediate.append(name)
            elif state.availability == Availability.BUSY:
                queue_eligible.append(name)
        # UNKNOWN: excluded from failover preference (fresh-only-for-failover)

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
            immediate.remove(primary)
            immediate.insert(0, primary)
        else:
            immediate.remove(affinity.provider)
            immediate.insert(0, affinity.provider)
    else:
        if primary in immediate:
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
