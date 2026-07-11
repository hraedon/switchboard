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
    preference_rank: int  # position in the route's ordered candidate list


@dataclass(frozen=True)
class RouteEntry:
    """A route table entry mapping a hashed key to an ordered provider list."""

    key: str  # SHA-256 hash of the raw API key
    providers: tuple[str, ...]  # ordered: [primary, fallback_1, ...]


@dataclass(frozen=True)
class RouteTable:
    """The full route table. Entries + a default provider list."""

    entries: dict[str, RouteEntry] = field(default_factory=dict)
    default_providers: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutingConfig:
    """Routing engine parameters.

    ``failover_threshold_seconds`` and ``failover_margin`` are retained for
    display and potential future tie-breaking but are no longer the primary
    decision mechanism (Plan 006 replaced scalar pressure comparison with
    categorical eligibility).
    """

    failover_threshold_seconds: int = 10
    failover_margin: int = 5


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


def route_decision(
    states: dict[str, ProviderState],
    table: RouteTable,
    route_key: str,
    config: RoutingConfig,
    *,
    now: float,
) -> AdmissionPlan:
    """Pure routing decision. Returns an :class:`AdmissionPlan`.

    The decision proceeds in this order (Plan 006 §3.1):

    1. Resolve the route key to an ordered candidate list.
    2. Reject missing and closed candidates.
    3. Separate fresh candidates from unknown/stale candidates.
    4. Place candidates with immediate permits first.
    5. Preserve primary preference among equally admissible candidates.
    6. Select at most one explicit queue candidate.
    7. Preserve the configured primary as the terminal safe-failure target
       so its gate can provide the canonical rejection when nothing is usable.

    Guarantees:

    * **Fail safe** — when all providers are closed, the plan's
      ``terminal_fallback`` is the primary; the proxy forwards to it and lets
      its gate return 503.  Never silently drop a request.
    * **Stale data never improves preference** — unknown/stale providers are
      excluded from failover by default (``fresh-only-for-failover`` policy).
    * **Pure** — ``now`` and all states are arguments.  No I/O, no clock.
    * **Deterministic** — same inputs produce the same plan.
    """
    entry = table.entries.get(route_key)
    candidates = entry.providers if entry is not None else table.default_providers

    if not candidates:
        raise ValueError("no providers configured")

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
            if state.availability == Availability.AVAILABLE:
                immediate.append(name)
            elif state.availability == Availability.BUSY:
                queue_eligible.append(name)
        # UNKNOWN: excluded from failover preference (fresh-only-for-failover)

    # Preserve primary preference in immediate candidates.
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
        reason = "primary_available" if immediate[0] == primary else "failover"
    else:
        reason = "queue_only"

    return AdmissionPlan(
        immediate_candidates=tuple(immediate),
        queue_candidate=queue_candidate,
        terminal_fallback=primary,
        reason=reason,
    )
