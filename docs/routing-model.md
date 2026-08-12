# The routing model

This is switchboard's design spine: it defines the data the routing engine
reasons over and the exact decision it makes. Everything else (the proxy, the
dashboard client, the CLI) is plumbing around the function described here. Read
this before touching code.

## 1. The problem

A single upstream's concurrency is governed by its own gate + reconcile loop
(now vendored into switchboard — Plan 018 dropped the external dependency).
When that upstream is saturated (gate closed, breaker open, account boxed),
the gate returns 503 and the client must retry. switchboard adds a routing
layer above multiple gates: when one provider is pressured, route to another
that has capacity.

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
| **DEGRADED** | Ready but last fetch failed — last-known-good; primary may keep serving; a fallback is demoted to a last-resort queue backstop, never an immediate failover target |
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
immediate failover. DEGRADED data may keep an already-primary route serving,
but it is not a new immediate failover target; a DEGRADED fallback is demoted
to a *last-resort queue backstop*, selected only when every fresh candidate
and the primary are ineligible — its signal failed, not necessarily the
provider, and availability must not hinge on an advisory signal (Plan 022
containment). UNKNOWN is excluded from failover entirely. Unknown data never
maps to zero pressure.

## 3. The routing decision (the pure function)

`route_decision(states, table, route_key, config, now=now, affinity=None,
servable_providers=None, healthy_since=None) -> AdmissionPlan`

The decision is a **staged pipeline with an explicit ranking contract**
(Plan 026). Before that plan it was a sediment of ten plans' mechanisms
composing by accident, through guards written when someone noticed an
interaction — N mechanisms with N² pairwise interactions, each pair handled
bespoke. The pipeline makes stage membership definitional: every mechanism
belongs to exactly one stage, and adding a mechanism means adding to a stage
rather than threading a new branch through the decision.

| # | Stage | Function | What lives here |
|---|---|---|---|
| 1 | **Resolve** | `_stage_resolve` | route table → ordered candidates; position 0 is the configured primary |
| 2 | **Filter** | `_stage_filter` | hard constraints only, strictly subtractive: model servability (Plan 010), capability surfaces (Plan 008 §4). Quarantine (Plan 023) enters here too, expressed by the shell through `servable_providers` |
| 3 | **Classify** | `_stage_classify` | every survivor gets exactly one tier; structurally unusable states (missing, `CLOSED`, UNKNOWN freshness) drop out; the signals that fired travel with the candidate |
| 4 | **Rank** | `_stage_rank` | order the IMMEDIATE tier by the strategy's scoring key |
| 5 | **Stickiness** | `_stage_stickiness` | affinity dwell, failback hysteresis, conversation pins, opportunistic burn; then `_stage_queue_candidate` picks at most one queue candidate |
| 6 | **Emit** | `_stage_assess` | the plan, plus a per-candidate explanation |

### 3.1 Tiers (stage 3)

Lexicographic tiers, not a weighted cost function: a tier boundary is how the
operator actually reasons ("never expensive if avoidable" is a boundary, not a
coefficient).

| Tier | Meaning | Members today |
|---|---|---|
| `IMMEDIATE` | eligible to serve now | FRESH + `AVAILABLE`, no demotion signal (and the primary on DEGRADED last-known-good) |
| `QUEUE` | serve only via the queue path | `BUSY`; or demoted by peak window / low headroom / token budget / trailing-24h |
| `BACKSTOP` | last resort, after every fresh candidate | non-primary DEGRADED |

A candidate the filter stage removed holds **no tier at all** and appears in no
assessment: it is out of the plan, and no later stage may resurrect it.

### 3.2 Signals (stage 3)

Each demotion signal is a named pure predicate `(state, config, is_primary) ->
bool`, so a new pressure or cost signal is one row in `_DEMOTION_SIGNALS` plus
one name in `SIGNAL_NAMES` — additive, never surgical. The names travel with
the decision:

| Signal | Fires when | May demote the primary? |
|---|---|---|
| `busy` | `availability == BUSY` (a fact, not a demotion) | n/a |
| `low_headroom` | session `usage_headroom < headroom_threshold` (Plan 015) | no |
| `over_budget` | `token_utilization >=` this provider's `soft_threshold`, else the global `token_budget_threshold` (Plan 012 §4) | no |
| `over_24h` | `usage_24h_utilization >= usage_24h_threshold` (Plan 013) | **yes** |
| `in_peak` | inside a configured peak-pricing window (Plan 025) | **yes** |
| `degraded` | `signal_freshness == DEGRADED` (a fact) | n/a |

`low_headroom` and `over_budget` never demote the primary — a proactive
utilization signal must not de-prefer the provider whose own gate already
handles its limits. The two that may are the ones the gate cannot see coming
(trailing-day volume) or that are not about capacity at all (price).

### 3.3 Ranking (stage 4)

The strategy is nothing but the choice of scoring key for the IMMEDIATE tier:

| `[routing] strategy` | Key | Notes |
|---|---|---|
| `ordered` (default) | table position | the primary fronts unless a pin or an opportunistic burn overrides |
| `headroom` | `usage_headroom` desc | Plan 015; `headroom_ranking = true` is the same thing |
| `pace` | weekly quota surplus desc | Plan 020 D5: `remaining_fraction − burn_rate × days_until_reset`; only FRESH weekly signals are scored, unscored providers follow in table order and are never starved; `pace_flap_margin` is a deadband on the top two, not hysteresis with memory |

`QUEUE` and `BACKSTOP` keep candidate order — stale never outranks fresh, and a
queue backstop is not a preference contest.

### 3.4 Stickiness (stage 5)

Affinity dwell (Plan 008 §5), failback hysteresis (Plan 014), conversation pins
(Plan 019) and opportunistic burn (Plan 016) all front **at most one** IMMEDIATE
member. One law, stated once: *stickiness may promote a provider within its
tier, never across a tier boundary* — so a pinned provider that gets demoted
(entering its peak window, say) loses the pin's effect automatically, because it
is no longer in the tier the overlay reorders.

Under `pace`, an expired pin goes inert rather than re-fronting the primary:
failing back would overwrite exactly the surplus ordering stage 4 produced.
(`headroom` still re-fronts the primary today; Plan 026 W2.1 generalizes the
rule.)

### 3.5 Queue-candidate selection (stage 5)

A ranking contract, not a series of guards. In order:

1. the primary, if it is queue-eligible;
2. the first other `QUEUE`-tier member;
3. the primary from `IMMEDIATE` — with nothing BUSY or demoted, a request whose
   immediate acquisitions all lose the snapshot race should still wait on the
   documented backstop rather than fail at once (§4 step 4);
4. the first `BACKSTOP` — every fresh candidate is gone and the primary is
   ineligible, but queueing on a DEGRADED fallback beats a 503: its signal
   failed, not necessarily the provider (Plan 022 containment).

Step 4 sitting last is "stale never outranks fresh" in its load-bearing
position.

### 3.6 The plan, and the explanation (stage 6)

```python
@dataclass(frozen=True)
class AdmissionPlan:
    immediate_candidates: tuple[str, ...]   # try these first (non-blocking)
    queue_candidate: str | None             # wait on this if immediate fails
    terminal_fallback: str                  # safe-failure target
    reason: str                             # bounded reason code
    assessments: tuple[CandidateAssessment, ...] = ()

@dataclass(frozen=True)
class CandidateAssessment:
    name: str
    tier: Tier                    # IMMEDIATE | QUEUE | BACKSTOP
    signals: tuple[str, ...] = () # the names from SIGNAL_NAMES that fired
    score: float | None = None    # the strategy's ranking key, IMMEDIATE only
    rank: int = 0                 # 0-based position within its own tier
```

`assessments` carries one entry per **surviving** candidate, ordered
IMMEDIATE → QUEUE → BACKSTOP with each tier in its final order, so position in
the tuple is the decision's own preference order. `tier == IMMEDIATE and
rank == 0` is what will be tried first.

`reason` is derived from the outcome and its strings are unchanged from before
the pipeline existed:

| Reason | Meaning |
|---|---|
| `primary_available` | Primary fronts the immediate candidates |
| `failover` | A non-primary fronts the immediate candidates |
| `affinity_dwell` | Affinity provider is pinned within `dwell_interval` |
| `affinity_hysteresis` | Affinity pin held past dwell while the primary reproves itself (Plan 014) |
| `affinity_pinned` | Conversation pinning (Plan 019) holds the pin past dwell while the pinned provider stays FRESH + AVAILABLE |
| `opportunistic` | No affinity pin; a quota-bearing fallback with expiring headroom took the front (Plan 016) |
| `pace_failover` | Pace ranked a non-primary highest by weekly quota surplus (Plan 020 D5) |
| `queue_only` | No immediate candidates; queue on a candidate |
| `no_eligible_candidates` | Every candidate is closed or unknown |
| `model_unservable` | No configured provider serves the requested model |
| `capability_filtered` | No candidate satisfies the route's required capabilities |

Two consumers read the assessments:

* **`GET /admin/route-plan?model=<m>&key=<raw-key>`** — the explain surface.
  Admin-authed, read-only, both params optional (no model = unfiltered, no key
  = the default route). It runs the *real* pipeline with `affinity=None` — an
  explanation that can disagree with the decision is worse than none — and
  touches no pin, no metric, and no healthy-since clock. The raw key is used to
  compute a digest and is never echoed, logged, or stored by switchboard —
  though it does travel in the query string, and uvicorn's access log records
  those, so omit `key=` unless you are asking about a keyed route specifically.
  The dashboard's Routing Explain card renders the response and never sends a
  key.
* **the decision log** — `recent_decisions` entries gain an additive `signals`
  map (`{provider: [names]}`) for the candidates that had any, so "why did
  traffic move off alpha at 14:00" is answerable from `/status.json` rather
  than by reconstructing a state that has since moved on.

### 3.7 Invariants

Each has a named test in `tests/test_pipeline_invariants.py`.

- **Demote, never drop.** No cost, pressure, or staleness signal may remove a
  provider from the plan entirely. Only stage 2 excludes, and it excludes on
  hard constraints alone.
- **Stale never outranks fresh.** UNKNOWN is excluded from failover; a DEGRADED
  fallback sorts after every fresh tier member in queue-candidate selection and
  is never an immediate target.
- **Signals are facts; policy lives in Classify/Rank.** The shell computes
  booleans and numbers (`in_peak`, surplus, headroom, freshness). Nothing in
  the shell orders candidates.

Properties this guarantees:

- **Fail safe.** When all providers are closed, the plan's `terminal_fallback`
  is the primary; the proxy forwards to it and lets its gate return 503. Never
  silently drop a request. When model or capability filtering removes the
  configured primary, the terminal fallback is the first *surviving* candidate,
  so the canonical rejection comes from a provider that could actually serve
  the request.
- **Bounded stickiness.** After failover, the core prefers the affinity
  provider for at least `dwell_interval` seconds before considering failback.
- **Failback hysteresis.** When `failback_delay > 0`, failback to the primary
  requires the primary to have been continuously FRESH+AVAILABLE for at least
  `failback_delay` seconds. A single unhealthy poll resets the continuity
  clock; the clock is read for the *effective* (post-filter) primary.
- **Opportunistic quota burn (Plan 016).** Opt-in; subordinate to an active
  affinity pin; de-preference only — the primary stays immediate-eligible,
  queue backstop, and terminal fallback. Stale or unmeasured data never
  promotes.
- **Pure.** `now`, `healthy_since`, and all provider states are arguments. No
  I/O, no clock, stdlib only (enforced by `tests/test_import_boundary.py`).
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
`x-api-key`). The key is **hashed** before lookup — switchboard never stores
or logs the raw API key. The hash is the route table key. With no
`SWITCHBOARD_ROUTE_KEY_SECRET` configured the hash is plain SHA-256; with a
secret it is HMAC-SHA-256 (Plan 008 §3), which defeats rainbow-table matching
of stored digests should the route-table store leak. Rotation is supported via
`SWITCHBOARD_ROUTE_KEY_SECRET_PREV`: the proxy tries the current secret's
HMAC, then the previous secret's, before falling back to the default route.

### Persistence (WI-006.7)

Route entries can optionally persist to SQLite (`--route-table-store`). Startup
precedence: persisted runtime entries override file seeds; file entries seed
only absent keys. Without a store, the route table is in-memory and re-seeded
from config on startup.

## 6. Provider contexts

Each provider is a self-contained gate + reconcile loop + truth source:

```python
@dataclass
class ProviderContext:
    name: str                    # "umans", "ollama", etc.
    upstream_url: str
    gate: PermitGate             # from switchboard.gate
    reconcile: ReconciliationLoop # from switchboard.reconcile
    truth_source: TruthSource    # from switchboard.truth (or switchboard.dashboard)
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

Each entry carries `route_key_hash`, `selected` and `primary`, plus — when any
candidate had a signal — the additive `signals` map from §3.6. The map is
bounded by the candidate count inside a ring already bounded at 128, and is
omitted entirely on an unremarkable decision, so existing consumers see exactly
the three keys they always did.

## 9. What switchboard deliberately does not model

- **Cost.** switchboard routes on provider availability, not monetary cost.
- **Request/response format translation.** Both upstreams must speak the same
  API format. Cross-format routing (Anthropic ↔ OpenAI) is a future project.
  *(switchboard does filter candidates by which providers serve a requested
  model — Plan 010's `[model]` map — and may rewrite that one field on the
  fallback path. That is model-name compatibility, not format translation.)*
- **Fairness across providers.** Each provider has its own FIFO queue. There
  is no global fairness guarantee — a request routed to a saturated provider
  waits in that provider's queue.
