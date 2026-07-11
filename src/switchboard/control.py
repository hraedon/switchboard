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


@dataclass(frozen=True)
class ProviderState:
    """Snapshot of one provider's pressure at a point in time.

    Assembled by the shell from each provider's reconcile loop and gate.
    Pure data — no I/O, no clock.
    """

    name: str
    gate_closed_reason: str  # "open", "boxed", "breaker", "saturated"
    available_permits: int
    queue_depth: int
    saturation_retry_after: int  # seconds, 0 when available
    usage_percent: float | None  # for dashboard-sourced providers (ollama)
    usage_stale: bool
    ready: bool


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
    """Routing engine parameters. Defaults bias toward sticky-to-primary."""

    failover_threshold_seconds: int = 10
    failover_margin: int = 5


def hash_route_key(raw_key: str) -> str:
    """SHA-256 hash of the raw API key. Pure, deterministic."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _provider_pressure(state: ProviderState) -> float:
    """Compute a scalar pressure score for a provider.

    Lower is better (0 = no pressure). Pure.
    """
    if state.gate_closed_reason in ("boxed", "breaker"):
        return float("inf")
    if state.gate_closed_reason == "saturated":
        return float(state.saturation_retry_after)
    if state.usage_percent is not None and not state.usage_stale:
        return state.usage_percent
    return 0.0


def route_decision(
    states: dict[str, ProviderState],
    table: RouteTable,
    route_key: str,
    config: RoutingConfig,
    *,
    now: float,
) -> str:
    """Pure routing decision. Returns the provider name to route to.

    Guarantees (see docs/routing-model.md §3):

    * Fail safe — when all providers are closed, route to the primary and let
      its gate return 503. Never silently drop a request.
    * Sticky to primary — failover only when the primary is pressured AND a
      fallback is meaningfully less pressured.
    * Pure — ``now`` and all states are arguments. No I/O, no clock.
    """
    entry = table.entries.get(route_key)
    candidates = entry.providers if entry is not None else table.default_providers

    if not candidates:
        raise ValueError("no providers configured")

    if len(candidates) == 1:
        return candidates[0]

    primary = candidates[0]

    candidate_states: list[tuple[str, ProviderState | None]] = [
        (name, states.get(name)) for name in candidates
    ]

    open_candidates: list[tuple[str, ProviderState]] = [
        (name, s)
        for name, s in candidate_states
        if s is not None and s.gate_closed_reason not in ("boxed", "breaker")
    ]

    if not open_candidates:
        return primary

    ready_candidates = [
        (name, s) for name, s in open_candidates if s.ready
    ]
    if not ready_candidates:
        return primary

    primary_state = next(
        ((name, s) for name, s in ready_candidates if name == primary),
        None,
    )

    if primary_state is not None:
        primary_pressure = _provider_pressure(primary_state[1])
        if primary_pressure < config.failover_threshold_seconds:
            return primary

    best_name, best_state = min(
        ready_candidates, key=lambda ns: _provider_pressure(ns[1])
    )

    if primary_state is not None:
        primary_pressure = _provider_pressure(primary_state[1])
        best_pressure = _provider_pressure(best_state)
        if primary_pressure - best_pressure >= config.failover_margin:
            return best_name
        return primary

    return best_name
