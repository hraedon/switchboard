# The routing model

This is switchboard's design spine: it defines the data the routing engine
reasons over and the exact decision it makes. Everything else (the proxy, the
dashboard client, the CLI) is plumbing around the function described here. Read
this before touching code.

## 1. The problem

sluice governs concurrency for a single upstream. When that upstream is
saturated (gate closed, breaker open, account boxed), sluice returns 503 and
the client must retry. switchboard adds a routing layer above multiple
sluice-style gates: when one provider is pressured, route to another that has
capacity.

## 2. Providers and their states

Each provider has an independent gate + reconcile loop + truth source. At any
moment, a provider's availability is categorized (Plan 006):

| Availability | Meaning | Derived from |
|---|---|---|
| **AVAILABLE** | Eligible and permits available now | `ready` and `gate_closed_reason == "open"` and `available_permits > 0` |
| **BUSY** | Eligible but no permit available now | `ready` and (`gate_closed_reason == "saturated"` or `available_permits == 0` or `queue_depth > 0`) |
| **CLOSED** | Structurally closed (boxed / breaker) | `gate_closed_reason in ("boxed", "breaker")` |
| **UNKNOWN** | Not ready — first poll not complete | `not ready` |

A provider's **signal freshness** is:

| Freshness | Meaning |
|---|---|
| **FRESH** | Ready and last fetch succeeded — may be selected or preferred normally |
| **DEGRADED** | Ready but last fetch failed — last-known-good; primary may keep serving, not a new failover target |
| **UNKNOWN** | Not ready — excluded from failover preference; unknown data never maps to zero pressure |

### 2.1 Live saturation (WI-006.1)

A provider is **BUSY** when `available_permits == 0` or `queue_depth > 0`,
even when its configured capacity is positive. This catches the case where all
permits are held by in-flight requests but the gate hasn't been resized to
zero. Structural closure (boxed/breaker) is kept separate from transient
saturation.

### 2.2 Staleness semantics (WI-006.2)

Stale or unknown data can never make a fallback more attractive. The default
policy is **fresh-only-for-failover**: only FRESH candidates are eligible for
failover. DEGRADED data may keep an already-primary route serving within a
bounded TTL, but it is not a new failover target. UNKNOWN is excluded from
failover entirely. Unknown data never maps to zero pressure.

## 3. The routing decision (the pure function)

`route_decision(states, table, route_key, config, now=now) -> AdmissionPlan`

The decision proceeds in this order (Plan 006 §3.1):

1. **Resolve** the route key to an ordered candidate list.
2. **Reject** missing and closed candidates.
3. **Separate** fresh candidates from unknown/stale candidates.
4. **Place** candidates with immediate permits (AVAILABLE) first.
5. **Preserve** primary preference among equally admissible candidates.
6. **Select** at most one explicit queue candidate.
7. **Preserve** the configured primary as the terminal safe-failure target so
   its gate can provide the canonical rejection when nothing is usable.

The function returns an `AdmissionPlan`:

```python
@dataclass(frozen=True)
class AdmissionPlan:
    immediate_candidates: tuple[str, ...]   # try these first (non-blocking)
    queue_candidate: str | None             # wait on this if immediate fails
    terminal_fallback: str                  # safe-failure target (always primary)
    reason: str                             # bounded reason code
```

### Reason codes

| Reason | Meaning |
|---|---|
| `primary_available` | Primary is in immediate candidates |
| `failover` | A non-primary is in immediate candidates |
| `queue_only` | No immediate candidates; queue on a candidate |
| `no_eligible_candidates` | All candidates are closed or unknown |

Properties this guarantees:

- **Fail safe.** When all providers are closed, the plan's `terminal_fallback`
  is the primary; the proxy forwards to it and lets its gate return 503. Never
  silently drop a request.
- **Stale data never improves preference.** Unknown/stale providers are
  excluded from failover by default.
- **Pure.** `now` and all provider states are arguments. No I/O, no clock.
- **Deterministic.** Same inputs → same plan. Testable without a network.

## 4. Admission algorithm (the async shell)

The proxy consumes the `AdmissionPlan` as follows (Plan 006 §4):

1. For each `immediate_candidate`, call `gate.acquire(timeout=0)` (non-blocking).
2. Forward through the first successful acquisition.
3. If all immediate attempts lose the snapshot race, perform one final
   non-blocking pass over the remaining eligible candidates.
4. If configured, wait only on `queue_candidate` for the remaining queue budget.
5. After queue timeout, return an honest 503 derived from the best available
   structural signal.

The default queue policy is:

```
try every fresh eligible provider now
then queue on configured primary for at most queue_timeout
then return 503
```

Failover occurs only **before** upstream forwarding begins. Once a request
starts uploading, it is never replayed to an alternate provider.

## 5. Route table

The route table maps routing keys to ordered provider lists:

```python
@dataclass(frozen=True)
class RouteEntry:
    key: str                    # API key hash or routing header value
    providers: tuple[str, ...]  # ordered: [primary, fallback_1, ...]

@dataclass(frozen=True)
class RouteTable:
    entries: dict[str, RouteEntry]
    default_providers: tuple[str, ...]  # used when no key matches
```

The routing key is derived from the request's `Authorization` header (or
`x-api-key`). The key is **hashed** (SHA-256) before lookup — switchboard never
stores or logs the raw API key. The hash is the route table key.

### Persistence (WI-006.7)

Route entries can optionally persist to SQLite (`--route-table-store`). Startup
precedence: persisted runtime entries override file seeds; file entries seed
only absent keys. Without a store, the route table is in-memory and re-seeded
from config on startup.

## 6. Provider contexts

Each provider is a self-contained sluice instance:

```python
@dataclass
class ProviderContext:
    name: str                    # "umans", "ollama", etc.
    upstream_url: str
    gate: PermitGate             # from sluice.gate
    reconcile: ReconciliationLoop # from sluice.reconcile
    truth_source: TruthSource    # from sluice.providers (or switchboard.dashboard)
    http_client: httpx.AsyncClient
```

The proxy holds `dict[str, ProviderContext]` and routes to the selected
provider's context. Each context runs its own reconcile loop independently.

## 7. Dashboard truth source

For providers without a native usage endpoint (ollama), switchboard polls the
usage-dashboard's `/readings` API. The reconcile loop owns the poll cadence
(WI-006.6); the truth source is a passive fetcher.

## 8. Bounded observability (WI-006.4)

Routing metrics use a bounded ring buffer for recent decisions (max 128
entries). Evicted decisions are counted. No Prometheus labels are created from
arbitrary client-provided values — route key hashes are truncated in display
output. Provider names in metrics come from config, not from clients.

## 9. What switchboard deliberately does not model

- **Request content, tokens-per-request, or cost.** switchboard routes on
  provider availability, not request semantics.
- **Per-model routing.** Routing is per-provider. If a client requests a model
  that provider A doesn't serve, that's a client misconfiguration, not a
  routing concern.
- **Request/response format translation.** Both upstreams must speak the same
  API format. Cross-format routing is a future project.
- **Fairness across providers.** Each provider has its own FIFO queue. There
  is no global fairness guarantee — a request routed to a saturated provider
  waits in that provider's queue.
