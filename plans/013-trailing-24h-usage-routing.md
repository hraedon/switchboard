# Plan 013 — Trailing-24h usage routing (penalty-avoidance failover)

Status: implemented (WI-1 … WI-6 landed; pure-core + tracker + integration
+ config tests, ruff + mypy clean)

Depends on: Plan 012 (token-budget seam, utilization filtering), the
usage-history tracker + threshold events work (uncommitted as of this
writing — poll-based trailing-24h totals via umans `/v1/usage/history`,
surfaced in `/status.json` + `/metrics`).

## 1. Problem

umans' heavy-usage penalty (low-interactivity mode) keys off an
**undisclosed trailing-day token volume** — not the current request
window, not concurrency. When it trips, every request queues behind
interactive sessions for the rest of the penalty window (hours). The
operator's ask:

> Automated re-routing of requests when umans 24h usage approaches
> configurable levels — bleed traffic to the fallback *before* the
> penalty trips, instead of failing over *after*.

What exists today:

- **Measurement** — `UsageHistoryTracker` polls `/v1/usage/history` and
  maintains an authoritative, provider-wide trailing-24h token total
  (`tokens_24h`). Display-only: surfaced in `/status.json` and
  `/metrics`, never consulted by routing.
- **The seam** — `route_decision` already filters candidates on two
  proactive utilization signals (`usage_headroom`, `token_utilization`),
  demoting over-threshold providers from `immediate` to `queue_eligible`.
- **The trip point** — `ThresholdEstimator` learns the actual penalty
  threshold from observed OFF→ON edges (advisory).

What's missing is the wire: `tokens_24h` → utilization → routing
decision.

## 2. The primary-demotion question (the heart of this plan)

Both existing proactive signals carry a `not is_primary` guard in
`route_decision`: *"the primary is never demoted — its own gate handles
its limits."* That rule is correct for signals the gate can see
(concurrency saturation, operator token caps on the request path). It is
**wrong for this signal**: the trailing-24h penalty is exactly what the
primary's gate cannot see coming, and umans **is** the primary. A
penalty-avoidance signal that cannot demote the primary is pointless.

This plan therefore makes `usage_24h_utilization` the **first proactive
signal that may demote the primary**. The semantics stay safe:

- Demotion moves the primary from `immediate` to `queue_eligible` —
  traffic *prefers* the fallback, but the primary remains the queue
  candidate of last resort (`queue_candidate` prefers the primary) and
  the `terminal_fallback` for the honest 503. The primary is never
  *excluded*, only *de-preferred*.
- Stale or absent data → `usage_24h_utilization is None` → no filtering
  (fail safe: never route on bad information).
- Default `usage_24h_threshold = 0.0` → feature fully off, today's
  behavior byte-for-byte.

## 3. Design

### 3.1 Pure core (`control.py`)

```python
@dataclass(frozen=True)
class ProviderState:
    ...
    usage_24h_utilization: float | None = None
    # tokens_24h / cap_tokens. 0.0 = none used; 1.0 = at cap.
    # None = no 24h budget configured or no data (no filtering).

@dataclass(frozen=True)
class RoutingConfig:
    ...
    usage_24h_threshold: float = 0.0
    # 0.0 = disabled. >0 = providers whose usage_24h_utilization >= this
    # are demoted from immediate to queue_eligible — INCLUDING the
    # primary (§2).
```

`route_decision` applies it alongside headroom/token-budget, minus the
`not is_primary` guard:

```python
over_24h = (
    config.usage_24h_threshold > 0
    and state.usage_24h_utilization is not None
    and state.usage_24h_utilization >= config.usage_24h_threshold
)
if low_headroom or over_budget or over_24h:
    queue_eligible.append(name)
```

Pure, deterministic, stdlib-only — same test discipline as the existing
filters.

### 3.2 Shell (`usage_history.py`)

`UsageHistoryTracker.register()` gains an optional `cap_tokens`
(0/None = tracking only, no utilization). New method:

```python
def utilization(self, provider: str) -> float | None:
    """tokens_24h / cap_tokens, or None when no cap is configured or no
    successful fetch has landed yet (fail safe: no data → no filtering)."""
```

### 3.3 Wiring (`providers.py`, `proxy.py`)

`snapshot_provider_state` gains a `usage_history_tracker` parameter
(mirrors `budget_tracker`) and fills `usage_24h_utilization`. The proxy
passes its tracker through.

### 3.4 Config (`cli.py`)

```toml
[usage_24h_budget."umans"]
cap_tokens = 300_000_000     # trailing-24h token cap for routing

[routing]
usage_24h_threshold = 0.85   # demote at 85% of cap; 0.0 = disabled
```

Validation mirrors `[token_budget.*]`: `cap_tokens` int > 0, provider
must exist, threshold in [0.0, 1.0]. The tracker still registers for
display-only providers (no `cap_tokens`) exactly as today.

### 3.5 Anti-flap

No hysteresis in v1. Three existing dampers suffice: hourly buckets move
the trailing total slowly, the tracker caches successful fetches for
5 min, and affinity dwell (default 30 s) prevents per-request
oscillation after a failover. A bleed-at/resume-at band is a documented
refinement if oscillation appears in practice.

## 4. Work items

- **WI-1** Pure core: `usage_24h_utilization` on `ProviderState`,
  `usage_24h_threshold` on `RoutingConfig`, demotion in `route_decision`
  (primary included). Unit tests: disabled-by-default no-op, primary
  demoted over threshold, non-primary demoted, None no-op, queue/backstop
  semantics preserved.
- **WI-2** Shell: `UsageHistoryTracker.utilization()` + `cap_tokens` in
  `register()`. Unit tests.
- **WI-3** Wiring: `snapshot_provider_state` + proxy call site.
- **WI-4** Config: `[usage_24h_budget.*]` + `[routing]
  usage_24h_threshold` parsing + validation + tracker registration with
  caps.
- **WI-5** Integration test: provider over 24h threshold → route
  decision prefers fallback, primary retained as queue candidate;
  threshold 0.0 → unchanged decision.
- **WI-6** Docs: AGENTS.md constraint note (first primary-demoting
  proactive signal) + this plan's status.

## 5. Future (not this plan)

- **Estimator-learned cap** (`cap = "auto"`): use
  `ThresholdEstimator.state().estimate.tokens.best_guess` as the cap so
  demotion tracks the *empirical* penalty point. Unset/contradicted
  estimate → None → no filtering (fail safe). Defer until the estimator
  has converged in production long enough to trust.
- **Hysteresis** (bleed at 85 %, resume at 75 %).
- **Requests-based 24h signal** (`tokens_24h_requests / cap`) if the
  penalty proves request-keyed rather than token-keyed (the estimator
  brackets both).

## 6. Non-goals

- No change to the queue/backstop semantics: demotion de-prefers, never
  excludes.
- No response-body or request-body observation: the signal is purely the
  poll-based tracker; inert-path guarantees unchanged.
- No weighted load balancing (remains Plan 012 §8 item 3).
