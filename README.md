# switchboard

A multi-provider routing proxy for LLM APIs. switchboard sits in the live
request path and routes incoming requests to the best available upstream based
on real-time pressure signals — failing over from a saturated provider to a
healthy one and back as conditions change.

## Why it exists

switchboard began as a routing shell over sluice's single-upstream
concurrency core. It is now **standalone**: Plan 018 dropped the dependency
and absorbed the pure core it needed, so the only runtime dependencies are
`httpx` and `uvicorn`. `switchboard.control`, `.gate`, and `.reconcile` are
switchboard's own, and `switchboard.overload` deliberately diverges from
sluice's breaker — routing *away* to another provider is inherently
multi-provider, which a single-upstream breaker cannot express.

## What it does

- Holds a **route table** mapping API keys (or routing headers) to upstream
  providers
- Runs **independent gates + reconcile loops** per provider, each with its own
  truth source
- Makes a **routing decision** per request: which provider to forward to,
  based on each provider's current pressure (gate state, saturation estimate,
  usage percentage)
- **Fails over** when the primary provider is saturated, boxed, or breaker-open
  — and falls back when it recovers
- **Reroutes on usage errors**: when an upstream answers `429`/`402`/`503`/`529`
  — quota exhausted, out of credit, overloaded — switchboard retries the same
  request on another eligible provider *before any byte reaches the client*,
  so a delegated agent gets an answer instead of an error or an endless
  client-side retry loop. Opt-in via `[reroute]`; see below
- **Streams** request/response bytes through untouched, with
  disconnect-cancellation and phantom-prevention
- Consumes the **usage-dashboard** `/readings` API as a truth source for
  providers that have no native usage endpoint (e.g. ollama)

## What it does not do

- **No request body translation.** Both upstreams must speak the same API
  format (OpenAI-compatible `/v1/chat/completions`). The `/v1/messages`
  (Anthropic) surface is passthrough to a single Anthropic-compatible upstream
  only — no cross-format routing.
- **No response caching, prompt logging, or model routing** beyond
  provider-level failover. Those are out of scope.
- **No re-serialization of request bodies** on the primary path.
  Cache-transparency applies to the primary/umans path — bytes are forwarded
  as-is. *(Exceptions: (1) when a `[model]` map is configured, the fallback
  path MAY rewrite the `model` field and re-serialize the request JSON for a
  provider that expects a different model name. (2) When a `[token_budget]`
  is configured, switchboard MAY observe the `usage` object in response bytes
  read-only in-flight for token accounting — bytes forwarded to the client are
  never modified. Neither exception applies without explicit opt-in
  configuration.)*

## Per-provider credentials

```toml
[provider.ollama-cloud]
upstream = "https://ollama.com/v1"
api_key_env = "SWITCHBOARD_OLLAMA_CLOUD_KEY"   # inline `api_key` also accepted
auth_header = "authorization"                   # or e.g. "x-api-key"
auth_prefix = "Bearer "                         # defaults by header
```

Each provider presents its own credential to its upstream, replacing whatever
the client sent. This is a prerequisite for cross-vendor failover, not a
convenience: providers issue their own keys, so a rerouted request carrying the
original vendor's key would be rejected — converting "out of quota" into "401",
which is worse than the failure rerouting exists to prevent.

This narrows byte-identical egress by exactly one header, and only for providers
that configure a key. With none configured the client's headers pass through
untouched and single-vendor cache-transparency is unchanged.

## Usage-error reroute

```toml
[reroute]
enabled = true
max_attempts = 1          # retries, not total tries: primary + 1 other
statuses = [402, 429, 503, 529]   # optional; this is the default
```

Off unless configured, because enabling it requires buffering the request body
so a retry can replay it — that changes request-streaming semantics and costs
memory, and no deployment should acquire either by upgrading.

The safety properties, all enforced in `switchboard.control.should_reroute`:

- **Never after the first byte.** The client's response has not started when
  the probe fires, so a retry can never splice two upstream responses together.
- **Never without a replayable body.** A streamed body is gone once consumed.
- **Never for the client's own faults.** `400`/`401`/`404` and genuine upstream
  breakage (`500`/`502`) are passed through — rerouting those would spray a
  broken request across the estate.
- **Bounded.** `max_attempts` caps the fan-out; a fully-exhausted estate
  returns one error, preserving the upstream status and `Retry-After` so the
  client's own backoff still sees the truth.

Response bodies stay inert — the exhausted upstream's response is closed unread
rather than inspected. When the retry budget is spent or no alternative exists,
the probe is never armed and the upstream's own response (status, headers and
body) passes through untouched, exactly as without the feature. switchboard
synthesises a body only in the narrow case where it had already closed an
exhausted response and then could not admit anywhere else; that reply still
carries the upstream's status and `Retry-After`. Reroutes are counted in `/status.json` and exported as
`switchboard_usage_reroutes_total` plus a per-origin
`switchboard_usage_reroutes_from_total{provider=...}` — the operational
question being "who is running out".

## The default route

The default route is where a request goes when no route key matches it — in
practice, most traffic. It is declared in TOML:

```toml
[route.default]
providers = ["umans", "ollama"]   # preference order
```

and can be changed at runtime, from the dashboard or the API:

```
PUT /admin/routes/default    {"providers": ["ollama", "umans"]}
```

This matters more than it looks. The model map only *filters* a route's
candidate providers — it never adds to them — so **a provider that no route
names is never selected**, however it was configured. Before the default route
became editable, adding a provider through the admin API had no effect on
traffic until someone edited the TOML and restarted.

Precedence follows the config store's rule: a default set through the API is
persisted and **outranks the TOML default** on the next boot, so a change made
in the GUI is not silently undone by a restart. `GET /admin/routes` reports
the default in effect, and the boot log prints it as `default_route:`.

Two deliberate asymmetries in how bad input is handled:

- **A TOML default naming an unknown provider is a fatal config error.** The
  operator has an editor and a loud failure is the fastest way to a typo.
- **A stored default naming a provider the config no longer defines is a
  warning**, and the unknown names are dropped. Being fatal here would let a
  GUI edit make the process unbootable — and an unbootable process cannot be
  fixed from the GUI, leaving hand-editing SQLite as the only way back in.

If every provider in the default is disabled, startup fails rather than
running with a default that routes nowhere.

## Design principles

- **Deterministic, stdlib-only routing core.** The routing decision is a pure
  function over provider states: no I/O, no async, no clock — time and
  observations are passed in as arguments. `switchboard.control` is
  import-boundary tested to keep it that way; composition lives above it.
- **Fail safe.** When all providers are pressured, route to the configured
  default and let its gate handle the rejection. Never route to a provider
  whose gate is closed without checking the alternatives.
- **Streaming is sacred.** Never buffer a full response body; proxy bytes as
  they arrive. A change that breaks token streaming is a regression.
- **Cache-transparency per-provider.** The request switchboard egresses to
  each upstream must be byte-for-byte what the client sent. Routing selects *which*
  upstream; it does not reshape the request. *(Exception: when a `[model]` map
  is configured, the fallback path MAY rewrite the `model` field for a provider
  that expects a different name — that path is explicitly not byte-transparent.
  The primary path stays byte-identical.)*

## Scope: in / out / non-goals

**In:**
- Multi-provider route table (API-key-based routing)
- Per-provider gates, reconcile loops, and truth sources
- Pressure-based failover routing (pure function)
- Overloaded-response breaker (503/529 → cooldown → failover)
- Model-aware failover with per-provider model rewriting (`[model]` map)
- usage-dashboard integration as an ollama truth source
- Admin dashboard for route table CRUD, including runtime editing of the
  default route
- Metrics per-provider and per-client

**Out (for now):**
- Request/response format translation (Anthropic ↔ OpenAI)
- Per-client weighting or fair queuing beyond FIFO per provider

**Non-goals:**
- Building a general-purpose API gateway
- Prompt logging, response caching, or content inspection

## Relationship to sibling tools

| Tool | Role |
|---|---|
| **usage-dashboard** | Multi-provider usage monitor running in k8s. switchboard consumes its `/readings` API as a truth source for providers with no native usage endpoint. |
| **opencode** | A client of switchboard. Points its `baseURL` at switchboard; switchboard handles routing transparently. |
