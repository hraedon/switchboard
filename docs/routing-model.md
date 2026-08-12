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
servable_providers=None, healthy_since=None, model_preference=())
-> AdmissionPlan`

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
| 5 | **Stickiness** | `_stage_stickiness` | affinity dwell, failback hysteresis, conversation pins; then `_stage_queue_candidate` picks at most one queue candidate |
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

The IMMEDIATE tier's sort key is
`(model_preference_rank, strategy_score…, table_order)`. The strategy is nothing
but the choice of that middle term:

| `[routing] strategy` | Key | Notes |
|---|---|---|
| `ordered` (default) | table position | the primary fronts unless a pin or a per-model preference overrides; post-dwell failback and `failback_delay` hysteresis belong to this strategy alone (§3.4) |
| `headroom` | `usage_headroom` desc | Plan 015; `headroom_ranking = true` is the same thing |
| `pace` | weekly quota surplus desc | Plan 020 D5: `remaining_fraction − burn_rate × days_until_reset`; only FRESH weekly signals are scored, unscored providers follow in table order and are never starved; `pace_flap_margin` is a deadband on the top two, not hysteresis with memory |

`QUEUE` and `BACKSTOP` keep candidate order — stale never outranks fresh, and a
queue backstop is not a preference contest.

#### 3.3.1 Per-model provider preference (Plan 026 W3)

The workload axis the estate had no way to express: *"for this model, prefer
these providers, in this order."* A model-map entry carries an optional ordered
`preference` (`["zai", "umans"]`), stored in the `model_map` table's
`preference` column and passed to the core as `model_preference` — the shell
reads config, the core ranks, exactly as `servable_providers` already works.

**It outranks the strategy, and it never adds a candidate.** The model map's
founding rule is unchanged: it filters and reorders. Concretely the preference
partitions the IMMEDIATE tier into groups by `preference_rank` — index in the
tuple, with every unnamed provider sharing the one rank *after* all named ones —
and the strategy orders each group. So:

- named providers come first, in the operator's order;
- unnamed providers keep exactly the order the strategy would have given them;
- a name that is not in the tier (filtered out, demoted to QUEUE, or not a
  candidate on this route) changes nothing. The stage only permutes the list it
  is handed, which is why "never resurrect" needs no guard.

What a preference may **not** do: resurrect a filtered candidate, lift a demoted
provider out of `QUEUE`, reorder `BACKSTOP`, or affect queue-candidate selection
(§3.5). Every named provider must hold an alias for the model, validated on
write — a preference for an alias-less provider could only ever be dead config,
so `POST /admin/model-map` refuses it by name, as it does a repeated name.

**Composition with the pace flap margin.** `pace_flap_margin` is a deadband on
the top two *scored* candidates (§3.3), and preference is lexicographically
above the score, so the two compose the only way that keeps both meaningful:
**preference groups dominate, and the deadband applies within a
same-preference-rank group.** Across groups it is silent — a near-tie between a
named and an unnamed provider is settled by the operator, not by an anti-flap
rule, and a deadband that could override a preference would make the feature
non-deterministic from the operator's point of view. Within a group the operator
expressed no order, so the anti-flap rule still governs, unchanged. (Scoring
itself is per-group too: the "scored before unscored" invariant holds inside each
group, never across one.)

TOML shape is deliberately unchanged (`[model."glm-5.2"]` still takes aliases
only): preference is a store/GUI concept first, and a TOML shape can follow if
wanted. A config overwrite carries across any part of a stored preference that
still holds an alias.

**Retired: opportunistic quota burn (Plan 016, removed by Plan 026 W2.2).** An
opt-in signal used to front a fallback that had session headroom to spare and a
quota window about to reset. `pace` supersedes it — the same use-it-or-lose-it
philosophy promoted from an exception to a primary ordering — and its
reset-window heuristic actively favoured the *expensive* provider, because a
~5 h session reset always sits inside the 6 h window it read as "spend this
now". The `opportunistic_*` fields are still parsed from TOML and from a stored
overlay, so an old config file cannot cost a boot, but they change nothing; boot
logs one warning when `opportunistic_enabled` is true, and
`PUT /admin/config/routing` refuses to write them.

### 3.4 Stickiness (stage 5)

Affinity dwell (Plan 008 §5), failback hysteresis (Plan 014) and conversation
pins (Plan 019) each front **at most one** IMMEDIATE member. One law, stated
once: *stickiness may promote a provider within its tier, never across a tier
boundary* — so a pinned provider that gets demoted (entering its peak window,
say) loses the pin's effect automatically, because it is no longer in the tier
the overlay reorders. That is what makes a pin safe to hold: a new demotion
signal revokes pins correctly without knowing pins exist.

**Failback belongs to `ordered`** (Plan 026 W2.1). Dwell holds a pin under every
strategy, but what happens when dwell expires depends on whether the strategy
has a ranking of its own:

| Strategy | Pin within dwell | Pin after dwell |
|---|---|---|
| `ordered` | held (`affinity_dwell`) | fail back to the primary, gated by `failback_delay` hysteresis (`affinity_hysteresis`) |
| `headroom`, `pace` | held (`affinity_dwell`) | **inert** — the ranking stands |

Fronting the primary is *ordered's own ranking* asserting itself after a pin
expires, not a universal law. Under a ranking strategy, failing back would
overwrite exactly the ordering stage 4 produced: before W2.1 every dwell expiry
re-fronted the table primary, so the estate leaked one primary request per dwell
interval per affinity key. That was fixed for `pace` alone on 2026-08-11 and
`headroom` carried an identical copy of the bug; `_ranks_immediate_tier` now
states the rule once, and `headroom_ranking = true` counts as `headroom`.

`pin_conversations` (Plan 019) is unchanged and outranks all of this: it holds
its pin past dwell under every strategy, for as long as the pinned provider
stays in the IMMEDIATE tier.

One asymmetry remains deliberately: with **no pin at all**, `headroom` still
fronts the primary, because Plan 015 ranks the *fallbacks* by headroom and
leaves the primary in front. So under `headroom` the ranking decides where
traffic goes once the primary is unavailable — or once a pin has expired.

**A per-model preference counts as a ranking** (Plan 026 W3.3). Pins are
untouched: a live pin still outranks the ranking, preference included. But the
two places where this stage would otherwise front the *table primary* on its own
initiative — post-dwell failback, and the no-pin default — stand down when a
preference names a member of the tier, exactly as they already do under `pace`.
Without that, a preference would be silently inert under the default `ordered`
strategy: stage 4 would order the tier and stage 5 would immediately undo it,
which is the dead-config outcome W3's write-time validation exists to prevent.
`_ranks_immediate_tier` states the rule once for strategies and preference
alike. The primary is not demoted by this — it keeps immediate eligibility, the
queue backstop, and the terminal fallback.

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
| `pace_failover` | Pace ranked a non-primary highest by weekly quota surplus (Plan 020 D5) |
| `queue_only` | No immediate candidates; queue on a candidate |
| `no_eligible_candidates` | Every candidate is closed or unknown |
| `model_unservable` | No configured provider serves the requested model |
| `capability_filtered` | No candidate satisfies the route's required capabilities |

Two consumers read the assessments:

* **`GET /admin/route-plan?model=<m>`** — the explain surface. Admin-authed,
  read-only, everything optional (no model = unfiltered, no key = the default
  route). It runs the *real* pipeline with `affinity=None` — an explanation that
  can disagree with the decision is worse than none — and touches no pin, no
  metric, and no healthy-since clock. The dashboard's Routing Explain card
  renders the response and sends no key.

  Per-model preference (§3.3.1) appears as a top-level `preference` array plus a
  `preference_rank` on each assessment entry — `null` when the provider is not
  named. A dedicated field rather than a synthetic signal, deliberately:
  `signals` are facts about provider *state* that the classify stage fired, and a
  preference is operator config the rank stage consumed. The card shows the
  order as one more stat line, and only when the model has one.

  To ask about a **specific client's** route, pass that client's raw key. The
  raw key is only hashed to a digest and is never echoed, logged, or stored by
  switchboard, but *how* you pass it matters:

  | | Access-logged? | |
  |---|---|---|
  | `x-route-plan-key: <raw-key>` | no — uvicorn does not log request headers | **preferred** |
  | `?key=<raw-key>` | yes — uvicorn's access log records query strings | kept for curl convenience |

  When both are present the header wins: a caller who set it deliberately chose
  the non-logging path. The header is also listed as a switchboard control
  header, so a request that carries it is never forwarded upstream with it.
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
- **Tier-bounded stickiness.** A pin promotes within a tier and never across
  one, and post-dwell failback applies only under `ordered` (§3.4).
- **One effective primary.** The plan's `terminal_fallback` *is* the effective
  (post-filter) primary, and the shell reads it from there rather than
  recomputing `candidates[0]` (Plan 026 W2.3). Before that, a model map or a
  quarantine that excluded the configured primary made every request for that
  model count a failover and create an affinity pin against a provider that was
  never a candidate. Consequence for metrics: `failovers` no longer counts
  model-map or quarantine filtering of the primary — only a genuine move off
  the provider that could have served.
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
