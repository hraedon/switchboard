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
- **Thin shell around the core — and around sluice's core.** The async proxy
  (`switchboard.proxy`), the dashboard client (`switchboard.dashboard`), and the
  CLI (`switchboard.cli`) import `switchboard.control` and `sluice.control`,
  never the reverse. The shell does I/O; the core decides.
- **Inert in-path — per provider.** switchboard forwards live traffic to the
  selected upstream. It **never reads, logs, stores, or rewrites request/response
  bodies.** It routes and streams bytes through untouched. Same guarantee as
  sluice, applied per-upstream-path. *(Exceptions, both opt-in and gated:
  (1) when a `[model]` map is configured, switchboard MAY read the request
  body's top-level `model` field and MAY rewrite only that field for the
  fallback path — Plan 010. No other field is read or altered. (2) When a
  `[token_budget]` is configured for a provider, switchboard MAY observe the
  `usage` object in the response stream **read-only in-flight** (SSE
  `data:` lines or non-streaming JSON) for token accounting — Plan 012.
  Bytes forwarded to the client are never modified; no other field is read.
  Response bodies are fully inert when neither exception applies.)*
- **Cache-transparency — indistinguishable from a direct client per upstream.**
  The request switchboard egresses to each upstream must be byte-for-byte what
  the client sent. Routing selects *which* upstream; it does not reshape the
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
   limits. The ONE exception is the trailing-24h usage signal (Plan 013):
   the provider's heavy-usage penalty keys off trailing-day volume, which
   the gate cannot see coming, so `[routing] usage_24h_threshold` MAY
   demote the primary to queue-eligible. Demotion de-prefers only — the
   primary stays the queue backstop and terminal 503 fallback.
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
7. **Validate on CI early; distrust green local gates.** Push to a branch and
   watch CI (3.12 + 3.13 + 3.14) before trusting. Async/streaming behaviour is
   easy to get locally-green and actually-broken.

## Layout

```
src/switchboard/
  control.py       # PURE routing core: route table, pressure comparison, routing decision
  proxy.py         # async multi-provider reverse proxy shell (streaming, routing, both routes)
  providers.py     # provider context: gate + reconcile + truth_source per upstream
  dashboard.py     # usage-dashboard /readings client → TruthSource for ollama
  route_table.py   # route table: API-key → provider mapping, CRUD, persistence
  overload.py      # per-provider overloaded-response breaker (Plan 010 Feature A)
  threshold.py     # pure low-interactivity threshold estimator (Plan 010 Feature C)
  budget.py        # PURE token-budget math core (Plan 012 Feature B)
  token_budget.py  # shell: rolling-window token tracker (streaming-tracked counts)
  usage_observer.py     # read-only SSE/JSON usage parser (streaming-tracked counts)
  usage_history.py      # shell: 24h rolling token total + penalty event tokens (poll-based)
  estimator.py     # shell wrapper for threshold estimator
  admin.py         # admin route handlers: health, status, metrics, route table CRUD, dashboard
  cli.py           # `switchboard serve ...` entry point
  static/          # dashboard assets
tests/
  test_control.py          # pure-core unit tests, no network
  test_import_boundary.py  # control imports stdlib only; shell→core one-way
docs/routing-model.md      # the design spine — read this first
plans/                     # numbered implementation plans
```

## Relationship to sluice

switchboard imports from sluice as a library:

- `sluice.control` — `LimitState`, `ControllerConfig`, `effective_permits`,
  `BreakerConfig`, `BreakerSnapshot`, breaker state machine functions,
  `AdaptiveConfig`, `AdaptiveSnapshot`, `adaptive_effective_permits`
- `sluice.gate` — `PermitGate`
- `sluice.reconcile` — `ReconciliationLoop`
- `sluice.providers` — `TruthSource` protocol, `PolledTruthSource`,
  `HeaderTruthSource`, `NullTruthSource`, `Provider`, `get_provider`,
  `make_truth_source`

switchboard does **not** modify these. It composes multiple instances and adds
a routing layer on top. If sluice's core needs a change to support
multi-provider, the change goes into sluice (with its own tests) and
switchboard consumes the new version.

## Don't

- Don't put the routing decision in the proxy layer "to save a function call."
  The decision is the asset; keep it pure and isolated.
- Don't translate request bodies between API formats. If you need
  cross-format routing, that's a future project, not switchboard MVP.
- Don't add response caching, prompt logging, or model routing. Those are out
  of scope.
- Don't add, rename, reorder, or buffer anything on the wire to any upstream.
