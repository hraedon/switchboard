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
moment, a provider is in one of these routing states:

| state | meaning | signal source |
|---|---|---|
| **available** | gate open, permits available, not boxed/breaker | `gate_closed_reason() == "open"` and `available > 0` |
| **saturated** | permits > 0 but all held; requests are queuing | `gate_closed_reason() == "saturated"` or `queue_depth > 0` |
| **closed** | gate closed (boxed / breaker / not-ready) | `gate_closed_reason() in ("boxed", "breaker")` |
| **unknown** | truth source never reported / stale beyond TTL | `not ready` or `usage_age > stale_ttl` |

A provider's **pressure score** is a derived quantity:

```
pressure = saturation_retry_after_seconds   # 0 when available, >0 when saturated
```

For providers with no `/v1/usage` (ollama via dashboard), pressure is derived
from usage percentage and local in-flight count:

```
pressure = session_percent if session_percent is not None else 0
```

## 3. The routing decision (the pure function)

`route_decision(providers, route_key, now) -> ProviderId` — which provider to
forward to:

```
# 1. Resolve the route key to a candidate set
candidates = route_table.lookup(route_key)
# candidates is an ordered list: [primary, fallback_1, fallback_2, ...]

# 2. Filter out closed providers
open_candidates = [p for p in candidates if p.state in (available, saturated)]

# 3. If no open candidates, route to the primary (let its gate reject)
if not open_candidates:
    return candidates[0]

# 4. Among open candidates, pick the one with lowest pressure
best = min(open_candidates, key=lambda p: p.pressure)

# 5. If the best is the primary and its pressure is below the failover threshold,
#    route to primary (avoid unnecessary failover)
if best == primary and best.pressure < failover_threshold_seconds:
    return primary

# 6. If a non-primary has lower pressure than primary by a margin, failover
if best != primary and primary.pressure - best.pressure >= failover_margin:
    return best

return primary
```

Properties this guarantees:

- **Fail safe.** When all providers are closed, route to the primary and let
  its gate return 503. Never silently drop a request.
- **Sticky to primary.** Failover only happens when the primary is pressured
  *and* a fallback is meaningfully less pressured. Prevents flapping.
- **Pure.** `now` and all provider states are arguments. No I/O, no clock.
- **Deterministic.** Same inputs → same output. Testable without a network.

## 4. Route table

The route table maps routing keys to ordered provider lists:

```python
@dataclass(frozen=True)
class RouteEntry:
    key: str                    # API key hash or routing header value
    providers: tuple[str, ...]  # ordered: [primary, fallback_1, ...]
    created_at: float

@dataclass(frozen=True)
class RouteTable:
    entries: dict[str, RouteEntry]
    default_providers: tuple[str, ...]  # used when no key matches
```

The routing key is derived from the request's `Authorization` header (or
`x-api-key`). The key is **hashed** (SHA-256) before lookup — switchboard never
stores or logs the raw API key. The hash is the route table key.

## 5. Provider contexts

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

## 6. Dashboard truth source

For providers without a native usage endpoint (ollama), switchboard polls the
usage-dashboard's `/readings` API:

```
GET http://usage-dashboard-server.usage-dashboard.svc.cluster.local:8080/readings
Authorization: Bearer <token>
```

The dashboard returns normalized `Reading` objects with `session_percent` and
`weekly_percent`. switchboard normalizes these into a `LimitState`:

```python
LimitState(
    concurrent_sessions=local_in_flight,  # from the gate
    limit=1,   # ollama is typically single-stream
    hard_cap=2,
    requests_remaining=round(100 - session_percent),
    requests_limit=100,
    provider="ollama",
)
```

The dashboard's 5-30 min cadence means the ollama reading is **coarse**. The
routing decision treats stale dashboard data fail-safe: when the reading is
older than `dashboard_stale_ttl`, pressure is unknown → the provider is treated
as "available but uncertain" (not closed, not preferred for failover).

## 7. What switchboard deliberately does not model

- **Request content, tokens-per-request, or cost.** switchboard routes on
  provider pressure, not request semantics.
- **Per-model routing.** Routing is per-provider. If a client requests a model
  that provider A doesn't serve, that's a client misconfiguration, not a
  routing concern.
- **Request/response format translation.** Both upstreams must speak the same
  API format. Cross-format routing is a future project.
- **Fairness across providers.** Each provider has its own FIFO queue. There
  is no global fairness guarantee — a request routed to a saturated provider
  waits in that provider's queue.
