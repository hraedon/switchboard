# Plan 011 — Usage-aware failover: check fallback headroom before shunting

Status: implemented

Depends on: Plan 010 (overload breaker, model map, low-interactivity surfacing)

## 1. Problem

When umans enters low-interactivity mode or overload cooldown, switchboard
fails over **all** traffic to ollama-cloud. Currently, ollama-cloud's
availability is driven only by its local gate (in-flight permits) and the
**reactive** overload breaker (which fires *after* the provider returns
503/529). There is no proactive check of ollama-cloud's actual rate-limit
headroom — its requests-remaining, tokens-remaining, or concurrent-session
count as reported by the usage-dashboard.

If ollama-cloud is near its own limits (e.g., 90 % of its RPM budget
consumed), shunting umans's full traffic load to it will cause ollama-cloud
to hit its own rate limits → 429s → breaker → everything goes down. The
overload breaker is reactive; what's missing is a **proactive** signal:
"this fallback provider is near its limit — don't add more load."

The usage-dashboard already runs in k8s and already monitors ollama (the
local instance) via `DashboardTruthSource` (Plan 003). The infrastructure
for polling, caching, and surfacing usage exists — it just isn't wired to
ollama-cloud or consumed by the routing decision.

## 2. Design

### 2.1 The seam: `usage_headroom` on `ProviderState`

The routing core gains a new optional field:

```python
@dataclass(frozen=True)
class ProviderState:
    ...
    usage_headroom: float | None = None
    # 0.0 = at limit (no headroom); 1.0 = zero usage (full headroom)
    # None = no usage data available (behave as today — no filtering)
```

`usage_headroom` is the fraction of remaining capacity, derived from the
truth source's `LimitState`:

- Primary: `requests_remaining / requests_limit` when both are non-`None`
  and `requests_limit > 0`.
- Fallback: `1 - session_percent / 100` when only `session_percent` is
  available (the current ollama dashboard format).
- `None` when no usage data is available (dashboard unreachable, no
  matching provider entry, or reading is stale/degraded).

The field is optional with a `None` default. Existing code, tests, and
providers without a dashboard truth source are unaffected — `None` means
"no data, no filtering, behave as today."

### 2.2 Data flow: dashboard → reconcile → ProviderState → route_decision

The data already flows through the existing architecture. The plan fills
two gaps:

```
                    EXISTING (unchanged)                    NEW
                    ──────────────────────                   ────
usage-dashboard     DashboardTruthSource                     (extended to
     │               polls /readings every 30s,              extract richer
     │               caches in reconcile.last_reading        fields)
     ▼
ReconciliationLoop   stores LimitState (with                (unchanged)
                     requests_remaining, requests_limit,
                     tokens_remaining, tokens_limit,
                     service_mode, ...)
     │
     ▼
snapshot_provider_   reads reconcile.last_reading,           derives
state()              gate, is_low_interactivity() →          usage_headroom
                     ProviderState                            from reading
     │
     ▼
route_decision       consumes ProviderState                  applies
(pure core)          (availability, freshness)               headroom_threshold
                     → AdmissionPlan                          to rank candidates
```

**Caching is already handled.** The `DashboardTruthSource` polls at a
configurable interval (default 30 s) and the reconcile loop caches the
last reading. `snapshot_provider_state` reads from the cache — no
per-request dashboard poll. This is the "keeps cached" the user asked for.

### 2.3 Extending `DashboardTruthSource`

The dashboard `/readings` response is a JSON list of reading dicts.
Currently, `DashboardTruthSource._reading_to_limit_state()` only extracts
`session_percent` and `timestamp`. The extension:

1. **Also extract** `requests_remaining`, `requests_limit`,
   `tokens_remaining`, `tokens_limit`, `concurrent_sessions` from the
   reading dict, if present. These are already fields on `LimitState`.

2. **Backward-compatible derivation**: if `session_percent` is present
   but `requests_remaining` is not, derive
   `requests_remaining = round(100 - session_percent)` and
   `requests_limit = 100` (exactly what the code does today for ollama).

3. **Fail-safe change**: `_fail_safe_reading()` currently sets
   `requests_remaining=0, requests_limit=100`, which would give
   `usage_headroom=0.0` — blocking failover when the dashboard is
   unreachable. Change it to set `requests_remaining=None,
   requests_limit=None`, so `usage_headroom=None` (no data → no
   filtering → failover proceeds). The reading's `ok=False` flag already
   makes `SignalFreshness=DEGRADED`, so the provider is not a *new*
   failover target anyway — but if it's the primary, it can still serve.

   The existing `ollama` provider is not affected: when the dashboard is
   reachable and returns `session_percent`, the reading is `ok=True` and
   `requests_remaining` is populated. The fail-safe only fires when the
   dashboard is unreachable, and in that case `requests_remaining=None`
   means the AIMD controller falls back to local signals (429s, breaker)
   — which is the correct conservative behaviour.

### 2.4 Routing decision: headroom-aware failover

`RoutingConfig` gains:

```python
headroom_threshold: float = 0.0   # 0.0 = disabled (today's behaviour)
```

When `headroom_threshold > 0` and a non-primary candidate has
`usage_headroom is not None and usage_headroom < headroom_threshold`:

- The candidate is treated as **BUSY** (moved from `immediate` to
  `queue_eligible`), not `AVAILABLE`.
- This means the proxy tries the primary first (even if `BUSY`), and
  only falls back to the low-headroom provider if the primary has no
  permits.
- If the primary is `CLOSED` (low-interactivity/overload) and the only
  fallback has low headroom, the fallback becomes the `queue_candidate`
  — the proxy waits for a permit rather than immediately shunting
  traffic. The fallback's gate (managed by AIMD using the dashboard
  reading) naturally throttles the rate.

**The primary is never demoted by headroom.** If the primary is
`AVAILABLE`, it stays in `immediate`. The primary's own gate handles its
limits; headroom filtering protects the *fallback*.

When `headroom_threshold = 0.0` (default), headroom filtering is disabled
— switchboard behaves exactly as today. This makes the feature fully
opt-in.

### 2.5 Interaction with existing signals

| Signal | Source | Type | Effect on routing |
|---|---|---|---|
| Gate state (permits) | sluice reconcile | Local, reactive | AVAILABLE/BUSY/CLOSED |
| Overload breaker | switchboard | Reactive (503/529) | CLOSED during cooldown |
| Low-interactivity | sluice (from /v1/usage) | Proactive | CLOSED |
| **Usage headroom** (this plan) | **usage-dashboard** | **Proactive** | **BUSY when near limit** |

Headroom is a *supplementary* signal — it doesn't override gate state or
the overload breaker. A provider that is `CLOSED` (breaker) stays
`CLOSED` regardless of headroom. A provider that is `AVAILABLE` with low
headroom becomes `BUSY` (deferred to queue). This layered approach means:

- If ollama-cloud is locally healthy (gate open, no 503s) but near its
  external rate limit (low headroom) → `BUSY` (queue, don't shunt).
- If ollama-cloud is locally unhealthy (breaker open) → `CLOSED` (skip).
- If ollama-cloud is healthy and has headroom → `AVAILABLE` (failover OK).

### 2.6 Config

```toml
[provider."ollama-cloud"]
upstream = "https://ollama-cloud.example.com"
type = "openai"
usage_key_env = "OLLAMA_CLOUD_API_KEY"
dashboard_url = "http://usage-dashboard-server.usage-dashboard.svc.cluster.local:8080"
dashboard_token_env = "DASHBOARD_API_KEY"
dashboard_poll_interval = 30.0
dashboard_stale_ttl = 900.0

[routing]
headroom_threshold = 0.15   # don't failover to a provider below 15% headroom
```

With `headroom_threshold = 0.0` (default), or with no `dashboard_url`
configured for the fallback provider, switchboard behaves exactly as
today — no body reads, no headroom filtering, failover is unrestricted.

## 3. Work items

- **[DONE]** **WI-1** Extend `DashboardTruthSource._reading_to_limit_state()` to
  extract `requests_remaining`, `requests_limit`, `tokens_remaining`,
  `tokens_limit`, `concurrent_sessions` from the dashboard response dict
  (if present). Backward-compatible: `session_percent` still derives
  `requests_remaining`/`requests_limit` when the richer fields are absent.

- **[DONE]** **WI-2** Change `DashboardTruthSource._fail_safe_reading()` to set
  `requests_remaining=None, requests_limit=None` (not `0`/`100`), so
  `usage_headroom=None` when no data is available. Verify existing ollama
  tests still pass (the `ok=False` flag drives freshness, not the
  `requests_remaining` value).

- **[DONE]** **WI-3** Add `usage_headroom: float | None = None` to `ProviderState`
  (pure core, `control.py`). Add `headroom_threshold: float = 0.0` to
  `RoutingConfig`. Update the import-boundary test if needed (both are
  plain dataclass fields, no new imports).

- **[DONE]** **WI-4** Derive `usage_headroom` in `snapshot_provider_state` from
  `reconcile.last_reading`:
  - If reading is `None` or `ok=False` (stale) → `None`.
  - If `requests_remaining` and `requests_limit` are both non-`None` and
    `requests_limit > 0` → `requests_remaining / requests_limit`.
  - Else → `None`.

- **[DONE]** **WI-5** Extend `route_decision` to apply headroom filtering: when
  `headroom_threshold > 0`, non-primary candidates with
  `usage_headroom is not None and usage_headroom < headroom_threshold`
  are moved from `immediate` to `queue_eligible` (treated as BUSY).
  Primary is never demoted. Pure-core unit tests for all cases.

- **[DONE]** **WI-6** Config parsing: `[routing] headroom_threshold` in CLI.
  Validate: float, `>= 0.0`, `<= 1.0`.

- **[DONE]** **WI-7** Integration test (stub upstreams via `httpx.MockTransport`):
  - umans CLOSED (overload cooldown) + ollama-cloud AVAILABLE with low
    headroom → request queues on ollama-cloud (not immediate failover).
  - umans CLOSED + ollama-cloud AVAILABLE with good headroom → immediate
    failover to ollama-cloud.
  - umans AVAILABLE + ollama-cloud low headroom → routes to umans
    (primary, unaffected by headroom).
  - `headroom_threshold = 0.0` → no filtering (today's behaviour).

- **[DONE]** **WI-8** CI workflow added (3.12/3.13/3.14: ruff + mypy + pytest).

## 4. Non-goals

- **No per-request cost/token routing.** Headroom is a provider-level
  signal, not a per-request estimate. switchboard routes on provider
  availability, not request semantics.
- **No dynamic threshold learning.** The threshold is
  operator-configured. Plan 010 Feature C's threshold estimator is a
  separate concern; a future plan could feed its output into
  `headroom_threshold`.
- **No response-body inspection.** Headroom comes from the dashboard
  poll, not from in-band response headers. The `HeaderTruthSource`
  (in-band ratelimit headers) already feeds the AIMD controller for
  per-response rate-limit signals.
- **No change to the primary path.** The primary's gate handles its
  limits. Headroom filtering protects the fallback from being
  overwhelmed.
- **No token-level headroom in v1.** Request-level headroom
  (`requests_remaining / requests_limit`) is sufficient for the initial
  use case. Token-level headroom can be added as a min() of the two
  dimensions in a follow-up if needed.

## 5. Relationship to Plan 010

This plan is a natural extension of Plan 010's failover infrastructure:

- Plan 010 added the **reactive** backstop (overload breaker: 503/529 →
  cooldown → CLOSED). This plan adds the **proactive** signal
  (dashboard headroom: low remaining → BUSY).
- Plan 010 added `is_low_interactivity()` as a proactive signal for
  umans. This plan adds `usage_headroom` as a proactive signal for
  ollama-cloud. Both map to `Availability.CLOSED` or `BUSY`
  respectively.
- Plan 010's threshold estimator (Feature C) could eventually learn the
  headroom threshold automatically — but that's explicitly out of scope
  here.
- The `[model]` map from Plan 010 is unaffected — headroom filtering
  applies after model-servability filtering, so only providers that can
  serve the requested model are considered for headroom.

## 6. What this prevents

Without this plan, the failure mode is:

1. umans enters low-interactivity → `CLOSED` → all traffic shunts to
   ollama-cloud.
2. ollama-cloud receives 3× normal load. Its gate has permits (local
   concurrency is fine), so switchboard keeps forwarding.
3. ollama-cloud hits its external rate limit (RPM/TPM) → starts
   returning 429s or 503s.
4. The overload breaker fires after 3 consecutive 503s → ollama-cloud
   `CLOSED`.
5. Both providers are now `CLOSED` → all requests get 503. The system
   is down.

With this plan, step 2 is intercepted: switchboard sees ollama-cloud's
headroom dropping (from the dashboard poll) and starts treating it as
`BUSY` before it hits the wall. Traffic queues on the primary (umans,
which is in low-interactivity but still serving degraded) rather than
overwhelming the fallback. The system degrades gracefully instead of
cascading to failure.
