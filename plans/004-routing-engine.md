# Plan 004 — Routing engine: pressure-based failover with hysteresis

**Goal:** Implement the full routing decision engine with pressure-based
failover, hysteresis (anti-flapping), and the `ProviderState` snapshot
mechanism that feeds the pure routing function.

## Prerequisite

- Plan 002 complete (multi-provider gates)
- Plan 003 complete (dashboard truth source, so ollama has real pressure data)

## Scope

- `ProviderState` snapshot: read each provider's reconcile loop and derive the
  state the routing function needs
- Full `route_decision()` implementation per `docs/routing-model.md §3`
- Hysteresis: failover margin + cooldown to prevent flapping
- Routing metrics: per-provider forward counts, failover counts, routing decisions
- Integration tests: simulated pressure on umans → failover to ollama

## Deliverables

### Provider state snapshot (`src/switchboard/control.py`)

```python
def snapshot_provider_state(
    name: str,
    reconcile: ReconciliationLoop,
    gate: PermitGate,
    *,
    now: float,
) -> ProviderState:
    """Read live state from a provider's reconcile loop and gate.

    Pure: takes the reconcile and gate as arguments, reads their current
    state, and returns a frozen ProviderState. No I/O.
    """
    gate_reason = reconcile.gate_closed_reason()
    return ProviderState(
        name=name,
        gate_closed_reason=gate_reason,
        available_permits=gate.available,
        queue_depth=gate.queue_depth,
        saturation_retry_after=(
            reconcile.saturation_retry_after()
            if gate_reason == "saturated"
            else 0
        ),
        usage_percent=_extract_usage_percent(reconcile),
        usage_stale=not reconcile.last_fetch_ok,
        ready=reconcile.ready,
    )
```

### Full routing decision

```python
def route_decision(
    states: dict[str, ProviderState],
    table: RouteTable,
    route_key: str,
    config: RoutingConfig,
    *,
    now: float,
) -> str:
    """Pure routing decision. Returns the provider name to route to."""
    candidates = table.entries.get(route_key)
    if candidates is None:
        candidates = RouteEntry(key=route_key, providers=table.default_providers)

    if not candidates.providers:
        raise ValueError("no providers configured")

    # Snapshot candidate states
    candidate_states = [
        (name, states.get(name))
        for name in candidates.providers
    ]

    # Filter out closed providers
    open_candidates = [
        (name, s) for name, s in candidate_states
        if s is not None and s.gate_closed_reason not in ("boxed", "breaker")
    ]

    # If no open candidates, route to primary (fail safe)
    if not open_candidates:
        return candidates.providers[0]

    # Not-ready providers are not preferred (but not excluded)
    ready_candidates = [
        (name, s) for name, s in open_candidates if s.ready
    ]
    if not ready_candidates:
        return candidates.providers[0]

    # Compute pressure for each ready candidate
    def pressure(state: ProviderState) -> float:
        if state.gate_closed_reason == "saturated":
            return float(state.saturation_retry_after)
        if state.usage_percent is not None and not state.usage_stale:
            return state.usage_percent
        return 0.0  # available, no pressure signal

    primary_name = candidates.providers[0]
    primary_state = next(
        ((name, s) for name, s in ready_candidates if name == primary_name),
        None,
    )

    # If primary is ready and not pressured, route to primary
    if primary_state is not None:
        primary_pressure = pressure(primary_state[1])
        if primary_pressure < config.failover_threshold_seconds:
            return primary_name

    # Find the candidate with the lowest pressure
    best_name, best_state = min(
        ready_candidates, key=lambda ns: pressure(ns[1])
    )

    # Hysteresis: only failover if the best is meaningfully less pressured
    if primary_state is not None:
        primary_pressure = pressure(primary_state[1])
        best_pressure = pressure(best_state)
        if primary_pressure - best_pressure >= config.failover_margin:
            return best_name
        return primary_name

    # Primary is not ready; route to the best ready candidate
    return best_name
```

### Hysteresis config

```python
@dataclass(frozen=True)
class RoutingConfig:
    failover_threshold_seconds: int = 10   # primary pressure >= this → consider failover
    failover_margin: int = 5              # best must be this much lower than primary
```

These are **per-route** overrides, not global. Each route entry can specify its
own thresholds:

```toml
[route.default]
providers = ["umans", "ollama"]
failover_threshold = 10    # seconds of estimated wait on umans before failing over
failover_margin = 5        # ollama must be 5 "pressure units" less pressured

[route.aggressive]
providers = ["umans", "ollama"]
failover_threshold = 5     # failover faster
failover_margin = 2
```

### Routing metrics

Per-provider counters (similar to sluice's `ClientMetrics`):

```python
@dataclass
class RoutingMetrics:
    forwarded_per_provider: dict[str, int]
    failovers: int          # times a non-primary was selected
    routing_decisions: int  # total decisions made
    last_routing: dict[str, str]  # {route_key: selected_provider}
```

Surfaced in `/status.json` and `/metrics`.

### Integration with the proxy

The proxy's per-request flow:

1. Extract route key → hash → lookup candidates
2. Snapshot `ProviderState` for each candidate (read from each reconcile/gate)
3. Call `route_decision(states, table, route_key, config, now=now)`
4. Acquire permit from the selected provider's gate
5. If acquire fails (became saturated between snapshot and acquire):
   a. Try the next candidate in the ordered list
   b. If all fail, return 503 + Retry-After
6. Forward to the selected provider

Step 5 is the **race condition guard**: the snapshot may be stale by the time
the permit is acquired. The fallback is to try the next candidate, not to
re-run the routing decision (which would be a second pure-function call over
the same possibly-stale data).

## Acceptance criteria

- [ ] `route_decision()` implemented and tested for all state combinations
- [ ] Hysteresis prevents flapping under oscillating pressure
- [ ] Primary not ready → routes to best ready fallback
- [ ] All providers closed → routes to primary (fail safe)
- [ ] Race condition guard: if selected provider's gate closes between
      snapshot and acquire, tries the next candidate
- [ ] Routing metrics surfaced in `/status.json`
- [ ] Integration test: simulate umans saturation → requests route to ollama
- [ ] Integration test: umans recovers → requests route back to umans
