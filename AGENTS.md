# AGENTS.md — switchboard

Conventions and hard rules for working in this repo. switchboard is part of the
cert-watch / gpo-lens / adcs-lens / sluice tool family. It is the family's
**routing layer** — it sits in the live request path above multiple sluice-style
gates and decides which upstream provider receives each request.

## Family conventions (adapted for a routing tool)

- **Deterministic, stdlib-only routing core.** The routing decision logic lives
  in `switchboard.control` as pure functions over plain data: provider states,
  pressure estimates, route table entries. No I/O, no async, no httpx, no model
  calls. It must be unit-testable with no network. An import-boundary test
  enforces that `switchboard.control` imports nothing outside the stdlib.
- **Thin shell around the cores.** The async proxy (`switchboard.proxy`),
  the dashboard client (`switchboard.dashboard`), and the CLI
  (`switchboard.cli`) import the pure cores (`switchboard.control`,
  `switchboard.limit`), never the reverse. The shell does I/O; the core
  decides.
- **Inert in-path — per provider.** switchboard forwards live traffic to the
  selected upstream. It **never reads, logs, stores, or rewrites request/response
  bodies.** It routes and streams bytes through untouched. Same guarantee as
  sluice, applied per-upstream-path. *(Exceptions, all opt-in and gated:
  (1) when a `[model]` map is configured, switchboard MAY read the request
  body's top-level `model` field and MAY rewrite only that field for the
  fallback path — Plan 010. No other field is read or altered. (2) When a
  `[token_budget]` is configured for a provider, switchboard MAY observe the
  `usage` object in the response stream **read-only in-flight** (SSE
  `data:` lines or non-streaming JSON) for token accounting — Plan 012.
  Bytes forwarded to the client are never modified; no other field is read.
  (3) When `[routing] pin_conversations` is configured, switchboard MAY read
  the request body's `messages` array to extract a conversation fingerprint
  (hash of the first user message's text content) for per-conversation
  affinity — Plan 019. No other field is read; body bytes forwarded are
  unchanged; a finite `max_request_body_bytes` is required. This exception
  composes with (1) — each reads only its own field.
  Response bodies are fully inert when none of the exceptions apply.)*
- **Cache-transparency — indistinguishable from a direct client per upstream.**
  The request switchboard egresses to each upstream must be byte-for-byte what
  the client sent, with two bounded exceptions (the `model` rewrite below, and
  provider-credential replacement: when a provider configures `api_key`,
  switchboard strips every inbound credential header and presents that
  provider's own key. Without it, cross-vendor failover would hand one
  vendor's key to another and be rejected — a leak as well as a failure. With
  no `api_key` configured the client's headers pass through untouched).
  Routing selects *which* upstream; it does not reshape the
  request. Any switchboard control header and the admin credential are consumed
  and stripped before forwarding, never sent upstream. *(Exception: when
  switchboard rewrites `model` for a fallback provider, it re-serialises the
  request JSON; that provider's path is explicitly not byte-transparent.
  Byte-identical egress is guaranteed when no model rewrite occurs — the
  primary/umans path, which is the caching-sensitive one. With no `[model]`
  map, this exception does not apply.)*
- **Fail safe.** Any uncertainty — stale usage data, unreachable truth source,
  breaker open on all providers — must route to the configured default provider
  and let its gate handle the rejection (503 + Retry-After), never silently
  route to a provider that is known to be unavailable.
- **Streaming is sacred.** Both routes stream Server-Sent Events. Never buffer a
  full response body; proxy bytes as they arrive. Same streaming +
  disconnect-cancellation + phantom-prevention logic as sluice.
- **No work-domain identifiers in committed files.** Placeholders only. API
  keys, host names, and any work-domain identifiers are not. `samples/` is
  gitignored and never committed. Enforced by the identifier gate
  (pre-commit hook + CI job).

## Hard rules

1. **Fail safe.** When all providers are pressured, route to the configured
   default and let its gate reject the request. Never route to a provider whose
   gate is closed without checking alternatives. Never widen the routing
   decision on bad information. Proactive utilization signals (headroom,
   token budget) never demote the **primary** — its own gate handles its
   limits. Two exceptions carve out de-preference-only behaviour. (1) The
   trailing-24h usage signal (Plan 013): the provider's heavy-usage penalty
   keys off trailing-day volume, which the gate cannot see coming, so
   `[routing] usage_24h_threshold` MAY demote the primary to queue-eligible.
   (2) The peak-window signal (Plan 025): a provider inside a configured
   peak-pricing window is expensive rather than broken, and price is not
   something its gate can see, so `[routing] peak_windows` MAY demote the
   primary to queue-eligible. Both are de-preference only: the primary stays
   queue backstop and terminal 503 fallback.
   Separately, a **ranking strategy** (`[routing] strategy = "pace"`) MAY cost
   the primary front-of-immediate on score alone. That is not a demotion — the
   primary stays immediate-eligible, queue backstop, and terminal fallback —
   and stale or unmeasured quota data is never scored, so it never promotes.
   *(Plan 016's `opportunistic_enabled` used to be exception (2) here. Plan 026
   W2.2 retired it: pace supersedes it, and its session-window reset heuristic
   systematically favoured the most expensive provider in this estate. The
   fields still parse and do nothing.)*
2. **The routing core is pure.** No clock, no randomness, no I/O inside
   `switchboard.control`. Pass time and observations in as arguments so
   decisions are reproducible and testable.
3. **Streaming is sacred.** Same as sluice. Never buffer a full response body.
4. **Release on disconnect, and cancel upstream.** Same as sluice: a permit is
   held for the life of the upstream request and released when it completes or
   when the downstream client disconnects. On disconnect, the upstream request
   is cancelled.
5. **No request body translation.** Both upstreams must speak the same API
   format. switchboard routes between same-format providers. Cross-format
   translation (Anthropic ↔ OpenAI) is explicitly out of scope for MVP.
6. **Cache-transparency per upstream.** The request egressed to each upstream
   must be byte-for-byte what the client sent — same body bytes, same headers
   (minus hop-by-hop and switchboard-internal), plus nothing switchboard-internal.
   Two bounded exceptions, both opt-in: the `model` rewrite on the fallback
   path when a `[model]` map is configured, and credential replacement for a
   provider that configures `api_key` (every inbound credential header is
   stripped and that provider's key applied — required for cross-vendor
   failover, and inert for providers with no key configured).
7. **Validate on CI early; distrust green local gates.** Push to a branch and
   watch CI (3.12 + 3.13 + 3.14) before trusting. Async/streaming behaviour is
   easy to get locally-green and actually-broken.

## Layout

```
src/switchboard/
  control.py       # PURE routing core: route table, pressure comparison, routing decision
  limit.py         # PURE flow-control core: LimitState, breaker state machine (vendored, Plan 018)
  proxy.py         # async multi-provider reverse proxy shell (streaming, routing, both routes)
  providers.py     # provider context: gate + reconcile + truth_source per upstream
  gate.py          # resizeable async permit gate with hold sampling (vendored, Plan 018)
  reconcile.py     # per-provider reconciliation loop: truth → permits (vendored, Plan 018)
  truth.py         # TruthSource protocol + polled/header/null sources (vendored, Plan 018)
  history.py       # per-tick HistoryEntry ring + SQLite store (vendored, Plan 018)
  session.py       # admin session cookies + login throttle (vendored, Plan 018)
  utils.py         # ASGI helpers: send_json/send_text, auth, CORS (vendored, Plan 018)
  dashboard.py     # usage-dashboard /readings client → TruthSource for ollama
  route_table.py   # route table: API-key → provider mapping, CRUD, persistence
  overload.py      # per-provider overloaded-response breaker (Plan 010 Feature A)
  threshold.py     # pure low-interactivity threshold estimator (Plan 010 Feature C)
  budget.py        # PURE token-budget math core (Plan 012 Feature B)
  token_budget.py  # shell: rolling-window token tracker (streaming-tracked counts)
  usage_observer.py     # read-only SSE/JSON usage parser (streaming-tracked counts)
  usage_history.py      # shell: 24h rolling token total + penalty event tokens (poll-based)
  estimator.py     # shell wrapper for threshold estimator
  speed.py         # per-provider speed statistics: TTFB / duration / tokens-per-sec (Plan 020 Wave 3)
  model_map.py     # model-name alias map: candidate filtering + egress rewrite (Plan 010 Feature B)
  config_store.py  # SQLite-backed provider-config store; store > TOML precedence (Plan 020 WI-1)
  provider_manager.py   # runtime provider lifecycle: add/remove/replace with draining (Plan 020 WI-2)
  env_config.py    # env-tier provider-field overrides: env > store > TOML (Plan 021 WI-6)
  config_reset.py  # reclaim store state: POST /admin/config/reset + SWITCHBOARD_CONFIG_RESET (Plan 021 WI-8)
  admin.py         # admin route handlers: health, status, metrics, route table CRUD, dashboard
  cli.py           # `switchboard serve ...` entry point
  static/          # dashboard assets
tests/
  test_control.py          # pure-core unit tests, no network
  test_import_boundary.py  # control imports stdlib only; shell→core one-way
docs/routing-model.md      # the design spine — read this first
plans/                     # numbered implementation plans
```

## Relationship to sluice (historical)

switchboard no longer depends on sluice — Plan 018 vendored the flow-control
core it actually used into switchboard-owned modules and deleted the
dependency:

- `switchboard.limit` — `LimitState`, `CachedReading`, `BreakerConfig`,
  breaker state machine, `RETRY_AFTER_SHORT`
- `switchboard.gate` — `PermitGate` (simplified: no reserve/cooldown)
- `switchboard.reconcile` — `ReconciliationLoop` (static
  `max_concurrency` + header tightening; sluice's adaptive controller was
  umans-specific and was not ported)
- `switchboard.truth` — `TruthSource` protocol, `PolledTruthSource`,
  `HeaderTruthSource`, `NullTruthSource`, `Provider`, `get_provider`,
  `make_truth_source`
- `switchboard.history` — per-tick `HistoryEntry` time series: in-memory
  ring + per-provider SQLite store (the substrate for usage-based triage)
- `switchboard.session` / `switchboard.utils` — admin session + ASGI helpers

Do NOT reintroduce a sluice import: the public PyPI package of that name is
an unrelated project, so a revived import would resolve to a stranger's
code. `tests/test_import_boundary.py` fails on any `sluice` import in src or
tests, and CI asserts the package is absent from the published image.

## Don't

- Don't put the routing decision in the proxy layer "to save a function call."
  The decision is the asset; keep it pure and isolated.
- Don't translate request bodies between API formats. If you need
  cross-format routing, that's a future project, not switchboard MVP.
- Don't add response caching, prompt logging, or model routing. Those are out
  of scope.
- Don't add, rename, reorder, or buffer anything on the wire to any upstream.
