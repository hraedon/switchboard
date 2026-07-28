# Plan 012 — Token-cap aware switching + sluice feature-parity

Status: implemented (Feature A + B + C landed; 260 tests, ruff + mypy clean)

Depends on: Plan 010 (overload breaker, model map), Plan 011 (usage headroom)

## 1. Problem

Three asks, one plan:

**A. Feature-completeness relative to sluice.** switchboard imports sluice's
core but surfaces far less of it. sluice's `status.py` exposes ~40 fields per
provider; switchboard's `_provider_status()` exposes ~10. The reconcile loops
that switchboard constructs never receive a `history_store` (no trend
analysis), never use `poll_interval_idle` (wasted polls when quiescent), and
expose no runtime overrides. `ProviderCapabilities` is defined but dead-code.
These gaps mean switchboard operators are flying blind compared to a direct
sluice deployment.

**B. Token-cap aware switching.** An operator wants to cap how many tokens
switchboard routes to a given provider per rolling window (e.g. "bleed umans
traffic to ollama-cloud before we cross 5 M tokens/hour"). Plan 011 added
*provider-side* headroom (requests_remaining from the dashboard poll), but
that is the provider's own rate-limit budget — it says nothing about
switchboard's contribution to it, and it lags by the dashboard's 5–30 min
cadence. What's missing is **switchboard's own per-request token counter**,
reconciled with the dashboard's provider-wide reading, projected forward to
switch *before* the cap is hit.

**C. Other worthwhile features.** Listed in §8 as recommendations; the plan
scopes the highest-value subset into work items.

## 2. Constraint changes (this plan amends AGENTS.md + README)

A third opt-in, gated narrow exception — same pattern as the `[model]`-map
exception to inert-request (Plan 010 §2):

- **"Response bodies are always fully inert"** → narrowed to: when a
  `[token_budget]` is configured for a provider, switchboard MAY observe the
  `usage` object (top-level `usage` in a non-streaming JSON response, or the
  final SSE chunk's `usage`) **read-only in-flight**. Bytes are forwarded
  byte-for-byte — the client receives exactly what the upstream sent. No other
  field is read, logged, or stored beyond the aggregate token count. With no
  `[token_budget]` map, response bodies remain fully inert (today's behaviour).

This is a **read-only observation**, not a rewrite. The key distinction from
the `[model]`-map exception (which *modifies* the request): token tracking
never changes a single byte the client sees. It is the streaming equivalent of
the proxy already observing the response *status code* and *headers* (for 429
classification and rate-limit-header recording) — those are not "body reads"
in the semantic sense, but they are the same "observe in-flight" pattern. The
`usage` field is the one additional structural field switchboard learns from
the response, and only when the operator opts in.

## 3. Feature A — sluice parity (observability + lifecycle)

### 3.1 Rich per-provider status surface

`_provider_status()` (admin.py) is extended to surface the full reconcile
state sluice already computes. This is display-only — the data is already on
the `ReconciliationLoop`; switchboard just wasn't reading it.

New fields added to each provider entry in `/status.json` and `/metrics`:

- `usage_age`, `stale`, `phantom_estimate`, `recent_429s`, `rate_limit_429s`,
  `total_503s`, `recent_503_count`
- `retry_after_hint` (un-jittered saturation estimate)
- `avg_wait_seconds`, `p95_wait_seconds`, `avg_hold_seconds`, `queue_timeouts`
- `requests_in_window`, `requests_limit`, `requests_remaining`,
  `requests_hard_cap`, `requests_window_seconds`
- `local_requests_in_window`, `request_window_delta`
- `throughput`, `idle`, `poll_interval_idle`
- `penalty_started_at`, `low_interactivity`, `service_mode`,
  `service_mode_resets_at`, `tokens_in`, `tokens_out`
- `priority_low`, `priority_reason`, `boxed_until`, `resets_at`
- `concurrent_sessions`, `limit`, `hard_cap`
- `overrides` (runtime target overrides, see §3.3)
- `overload_consecutive`, `overload_cooling`, `overload_cooldown_remaining`

`/metrics` gains the corresponding per-provider Prometheus gauges
(`switchboard_phantom_estimate`, `switchboard_throughput`, etc.), matching
sluice's metric names where they overlap.

### 3.2 HistoryStore per provider

sluice's `ReconciliationLoop` accepts an optional `history_store`
(`sluice.history_store.HistoryStore`, SQLite-backed) and `history` (in-memory
ring). switchboard never passes them. The fix:

- When `--route-table-store` (SQLite) is configured, each provider's
  `ReconciliationLoop` receives a `HistoryStore` sharing that SQLite
  connection (one table per provider, namespaced by provider name). This gives
  per-provider trend analysis with zero new infrastructure — the persistence
  seam already exists.
- The history is surfaced in `/status.json` as a bounded recent-trend
  summary (last N entries: band, effective_permits, stale, throughput) so the
  dashboard can render sparklines without querying SQLite directly.

### 3.3 Runtime target overrides via admin API

sluice's `apply_override` / `clear_override` on `ReconciliationLoop` lets an
operator change `target` at runtime with provider-aware validation. switchboard
gains:

- `POST /admin/providers/<name>/override` — body `{"target": <int>}`. Calls
  `ctx.reconcile.apply_override("target", value)`. Returns the warning string
  (accept-with-warning) or 200 (clean).
- `DELETE /admin/providers/<name>/override` — reverts to boot value.
- Both gated by admin-token + CSRF, same as route CRUD.

### 3.4 Idle poll backoff

`build_provider_context` passes `poll_interval_idle` to the reconcile loop
when configured per-provider in TOML (`poll_interval_idle = 300.0`). Default
unset (current behaviour). The reconcile loop already implements the
fast/idle cadence switch — switchboard just needs to pass the value through.

## 4. Feature B — token-cap aware switching

### 4.1 Architecture

```
  request ─▶ proxy._forward()
               │
               │  (streaming loop — bytes forwarded unchanged)
               │  ┌──────────────────────────────────┐
               └──│ UsageObserver (read-only parse)   │
                  │   SSE data: → usage{prompt,completion} │
                  │   JSON body: → usage{prompt,completion} │
                  └──────────────┬───────────────────┘
                                 │ record_usage(provider, tokens, now)
                                 ▼
                  TokenBudgetTracker (shell, mutable rolling window)
                                 │
                    ┌────────────┴───────────────┐
                    │ own_count (switchboard's)  │  dashboard_reading
                    │ rolling-hour deque         │  (provider-wide tokens)
                    │                            │  (already polled)
                    └────────────┬───────────────┘
                                 │ reconcile()
                                 ▼
                  projected_utilization = (own + other) / cap
                                 │
                                 ▼
                  ProviderState.token_utilization (pure core)
                                 │
                                 ▼
                  route_decision: BUSY when over soft_threshold
```

### 4.2 The seam: `token_utilization` on `ProviderState`

```python
@dataclass(frozen=True)
class ProviderState:
    ...
    token_utilization: float | None = None
    # 0.0 = zero tokens used; 1.0 = at cap; >1.0 = over cap
    # None = no token budget configured (behave as today — no filtering)
```

`RoutingConfig` gains:

```python
token_budget_threshold: float = 0.0
# 0.0 = disabled. >0 = providers whose token_utilization >= this are
# treated as BUSY (moved from immediate to queue_eligible). The primary
# is never demoted by token budget — its own gate handles its limits.
```

`route_decision` applies this exactly like `headroom_threshold`: non-primary
candidates with `token_utilization is not None and token_utilization >=
token_budget_threshold` move from `immediate` to `queue_eligible`. Primary is
never demoted.

### 4.3 Pure core: `budget.py`

A new pure module (stdlib-only, import-boundary-tested) for the budget math:

```python
@dataclass(frozen=True)
class TokenBudgetConfig:
    cap_tokens: int                 # user-defined cap per window
    window_seconds: float = 3600.0  # rolling window
    soft_threshold: float = 0.85    # bleed traffic at this fraction of cap

@dataclass(frozen=True)
class TokenSnapshot:
    """One point-in-time view of a provider's token budget."""
    own_tokens_in_window: int       # switchboard's rolling count
    provider_tokens_in_window: int | None  # dashboard's provider-wide total
    cap_tokens: int
    projected_utilization: float | None
    # = (max(own, provider) if provider known else own) / cap
    # Uses max(own, provider) because the provider-wide total is authoritative
    # and includes switchboard's contribution; own alone under-counts when
    # other clients share the provider.
```

Pure functions:
- `compute_utilization(own, provider_wide, cap) -> float | None`
- `project_utilization(current, elapsed_fraction_of_window) -> float` —
  linear projection: if we used X tokens in 40% of the window, project to
  X / 0.4 for the full window. Conservative: only projects forward, never
  backward.

### 4.4 Shell: `TokenBudgetTracker`

Mutable, per-provider, takes `now` as argument (same pattern as
`OverloadTracker`):

- `_windows: dict[str, deque[tuple[float, int]]]` — per-provider rolling
  deque of `(timestamp, token_count)` samples. Pruned to `window_seconds`
  on each access.
- `record_usage(provider, prompt_tokens, completion_tokens, *, now)` —
  appends `(now, prompt + completion)`.
- `own_tokens(provider, *, now) -> int` — sum of samples in the window.
- `utilization(provider, *, now) -> float | None` — delegates to the pure
  `compute_utilization`, combining `own_tokens` with the last dashboard
  reading (if available for this provider).
- `reconcile(provider, dashboard_tokens) -> None` — stores the provider-wide
  total for the reconciliation math. Called from the dashboard poll path
  (the `DashboardTruthSource` already fetches this).

### 4.5 Shell: `UsageObserver`

The in-flight read-only parser. Sits inside the proxy's streaming loop
alongside the existing chunk-forward logic:

```python
class UsageObserver:
    """Read-only SSE/JSON usage extractor. Never modifies bytes."""

    def feed_chunk(self, chunk: bytes) -> None:
        """Feed a response chunk. Parses SSE data: lines incrementally."""

    def feed_non_streaming(self, body: bytes) -> None:
        """Parse a complete non-streaming JSON response body for usage."""

    @property
    def usage(self) -> tuple[int, int] | None:
        """(prompt_tokens, completion_tokens) if found, else None."""
```

**SSE parsing** (streaming responses): OpenAI-compatible SSE is a sequence of
`data: <json>\n\n` lines. The `usage` object appears in the final chunk (when
the client set `stream_options.include_usage: true`) or in a trailing
`data: [DONE]`-adjacent chunk. The observer buffers the *current* `data:`
line incrementally (at most one line, ~1 KB), parses it as JSON, and extracts
`usage.prompt_tokens` / `usage.completion_tokens` if present. The line buffer
is discarded after parsing — no full-body buffering, no storage.

**Non-streaming** (when `Content-Type: application/json` and no SSE): the
response body is buffered (it's already small and non-streaming by
definition), parsed for `usage`, then forwarded. This is the same bounded
buffer pattern as the request-body buffering for the model map.

**Fail-safe**: if parsing fails or no `usage` is found, the observer silently
returns `None`. Token tracking is best-effort — the dashboard reconciliation
corrects drift, and the routing decision treats `None` utilization as "no
data, no filtering."

### 4.6 Proxy wiring

`_forward` gains a new branch when `self._budget_tracker is not None`:

1. After response headers arrive, check `Content-Type`. If
   `text/event-stream` → SSE path. If `application/json` → non-streaming
   path.
2. SSE path: each chunk from `response.aiter_raw()` is fed to the
   `UsageObserver` *in addition to* being sent to the client. Bytes sent
   to the client are unchanged.
3. Non-streaming path: buffer the response body (bounded), feed to observer,
   forward the original bytes.
4. After the stream completes, if `observer.usage is not None`, call
   `budget_tracker.record_usage(ctx.name, prompt, completion, now=now)`.

The `snapshot_provider_state` function reads
`budget_tracker.utilization(name, now=now)` → `token_utilization` on the
`ProviderState`.

### 4.7 Reconciliation with the usage-dashboard

The `DashboardTruthSource` already polls `/readings` every 30 s. For
providers whose dashboard reading includes token data (umans:
`tokens_in`/`tokens_out`), switchboard reconciles:

- The dashboard reading gives the **provider-wide** token total for the
  current window.
- switchboard's own tracker gives **switchboard's** rolling count.
- `compute_utilization` uses `max(own, provider_wide)` — the provider-wide
  total is authoritative (it includes switchboard's contribution plus any
  other clients). This means: if other clients are consuming the budget,
  switchboard sees the utilization rise even if its own count is low, and
  bleeds traffic accordingly. This is the "reconciled with stored hourly
  readings via the API" the operator asked for.

For providers without dashboard token data (ollama-cloud), switchboard relies
solely on its own count.

### 4.8 Config

```toml
[token_budget."umans"]
cap_tokens = 5000000        # 5M tokens/hour
window_seconds = 3600
soft_threshold = 0.85       # bleed at 85%

[token_budget."ollama-cloud"]
cap_tokens = 1000000        # 1M tokens/hour
window_seconds = 3600
soft_threshold = 0.80

[routing]
token_budget_threshold = 0.85   # global: apply token-budget BUSY filtering
                                # when utilization >= this. 0.0 = disabled.
```

When `token_budget_threshold = 0.0` (default), token-budget filtering is
disabled — switchboard still tracks usage (for display) but never reroutes on
it. Per-provider `soft_threshold` in `[token_budget.*]` overrides the global
for that provider.

### 4.9 Persistence

The `TokenBudgetTracker` persists its rolling window to the route-table
SQLite (same seam as the estimator and history store). On restart, the window
is loaded so the cap survives a deploy. The per-provider table:
`token_usage (provider, timestamp, tokens)` with an index on
`(provider, timestamp)` for efficient pruning.

## 5. Feature B — interaction with existing signals

| Signal | Source | Type | Effect on routing |
|---|---|---|---|
| Gate state (permits) | sluice reconcile | Local, reactive | AVAILABLE/BUSY/CLOSED |
| Overload breaker | switchboard | Reactive (503/529) | CLOSED during cooldown |
| Low-interactivity | sluice | Proactive | CLOSED |
| Usage headroom (Plan 011) | usage-dashboard | Proactive | BUSY when near provider limit |
| **Token budget** (this plan) | **own tracking + dashboard** | **Proactive** | **BUSY when near operator cap** |

Token budget is a **supplementary** signal. Layering (applied in
`route_decision`):

1. Gate state → AVAILABLE/BUSY/CLOSED
2. Overload + low-interactivity → CLOSED
3. Headroom (provider's own limit) → BUSY for non-primary
4. **Token budget (operator's cap) → BUSY for non-primary**
5. Model servability → eligibility filter
6. Affinity → dwell/failback

A provider that is `CLOSED` (breaker) stays `CLOSED` regardless of token
budget. A provider that is `AVAILABLE` but over its token budget becomes
`BUSY` (deferred to queue). The primary is never demoted by token budget.

## 6. Feature C — parity cleanup (lower-risk wiring)

Work items that close known dead-code / gap issues from the last reflection:

- **WI-C1**: Wire `ProviderCapabilities` through
  `snapshot_provider_state` — populate `capabilities` from provider config.
- **WI-C2**: Wire `required_capabilities` through TOML `[route.<key>]` and
  the admin route-add API.
- **WI-C3**: Disconnect detection during queue wait — `_admit` monitors
  `receive()` for `http.disconnect` while blocking on `queue_candidate`.
- **WI-C4**: LRU affinity eviction (currently FIFO).
- **WI-C5**: Increment `healthy_observations` on affinity (currently always 0).
- **WI-C6**: Consume `ReplayBoundary` in the proxy (retry-after-failure
  gating) or remove it as dead code.

## 7. Work items

### Feature A — sluice parity

- **WI-1** Extend `_provider_status()` + `/metrics` with the full reconcile
  field set (§3.1). Pure display — data already on the loop.
- **WI-2** Pass `HistoryStore` + `history` to each provider's reconcile loop
  when SQLite is configured (§3.2). Surface recent-trend summary in
  `/status.json`.
- **WI-3** Admin API for runtime `target` overrides (§3.3):
  `POST/DELETE /admin/providers/<name>/override`.
- **WI-4** Pass `poll_interval_idle` through provider config (§3.4).

### Feature B — token-cap switching

- **WI-5** Pure core: `budget.py` (TokenBudgetConfig, TokenSnapshot,
  compute_utilization, project_utilization). Unit tests, import-boundary.
- **WI-6** Add `token_utilization` to `ProviderState`; add
  `token_budget_threshold` to `RoutingConfig`; extend `route_decision` with
  token-budget BUSY filtering (mirrors headroom). Pure-core unit tests.
- **WI-7** Shell: `TokenBudgetTracker` (rolling-window tracker + SQLite
  persistence + dashboard reconciliation).
- **WI-8** Shell: `UsageObserver` (read-only SSE/JSON usage parser).
- **WI-9** Proxy: wire `UsageObserver` into the streaming loop (read-only,
  byte-transparent); feed `TokenBudgetTracker` on usage extraction.
- **WI-10** `snapshot_provider_state`: read tracker → `token_utilization`.
- **WI-11** Config parsing: `[token_budget.*]` + `[routing]
  token_budget_threshold`. Validation.
- **WI-12** Status surface: token budget utilization per provider in
  `/status.json` + `/metrics`.
- **WI-13** Integration test (stub upstreams via `httpx.MockTransport`):
  SSE response with `usage` → tracker records → provider over budget →
  non-primary demoted to BUSY → traffic bleeds to next provider.

### Feature C — parity cleanup

- **WI-14** Wire `ProviderCapabilities` (WI-C1) + `required_capabilities`
  (WI-C2).
- **WI-15** Disconnect detection during queue wait (WI-C3).
- **WI-16** LRU affinity + healthy_observations (WI-C4, WI-C5).
- **WI-17** `ReplayBoundary`: consume or remove (WI-C6).

### Cross-cutting

- **WI-18** AGENTS.md + README constraint-narrowing edits (§2).
- **WI-19** Run full suite (3.12/3.13/3.14: ruff + mypy + pytest); push and
  watch CI.

## 8. Other worthwhile features (recommendations, not in this plan's scope)

These are recommended for future plans, listed by value-to-effort ratio:

1. **SIGHUP config reload** — live reload of provider/route/model config
   without restart. sluice has it; switchboard doesn't. High value for ops.
2. **Per-client metrics** — track forwarded/succeeded/429s per API-key label
   (sluice has `client_metrics`). Enables client-level abuse detection.
3. **Weighted load balancing** — currently pure failover (primary → fallback).
   Adding `[route.<key>] weights = [3, 1]` would distribute load across
   healthy providers, not just fail over. Useful when both providers are
   healthy and you want to split load by capacity.
4. **usage-dashboard `/history` endpoint** — add `GET /history?provider=X
   &hours=24` to the usage-dashboard so switchboard (and operators) can query
   stored hourly readings directly, not just the latest. Cross-project
   (changes usage-dashboard, not switchboard).
   **Shipped 2026-07-28** (usage-dashboard: `GET /history?provider=<name>
   &hours=<1..168>`, bearer-authed, oldest-first over the append-only
   readings store).
5. **Active health probes** — synthetic probe requests to detect a degraded
   provider before real traffic hits it. Currently purely passive
   (reconcile-driven).
6. **Cost-aware routing** — track $/token per provider, route to cheapest.
   Explicitly out of AGENTS.md scope today, but token tracking (this plan)
   is the prerequisite.
7. **Canary / traffic splitting** — route N% of a key's traffic to a new
   provider for testing before full cutover.
8. **Structured logging / OpenTelemetry** — distributed tracing of the
   routing decision (which provider, why, how long). Currently plain logging.

## 9. Non-goals

- **No response-body modification.** The `usage` observation is read-only.
  Bytes the client receives are byte-for-byte what the upstream sent.
- **No per-request cost/$ routing.** Token tracking enables it but it's a
  future plan.
- **No change to the primary path's byte-transparency.** Token tracking
  observes bytes but does not alter them. The primary/umans path stays
  byte-identical. (Token tracking is read-only; unlike the model-map rewrite,
  no bytes change on any path.)
- **No token counting without `[token_budget]` configured.** When no budget
  is configured for any provider, the `UsageObserver` is never instantiated —
  zero overhead, zero body observation, today's behaviour exactly.
