# switchboard

A multi-provider routing proxy for LLM APIs. switchboard sits in the live
request path and routes incoming requests to the best available upstream based
on real-time pressure signals — failing over from a saturated provider to a
healthy one and back as conditions change.

## Why it exists

sluice is a single-upstream concurrency governor: one provider, one gate, one
reconcile loop. Its design principles (inert in-path, cache-transparency, no
model routing) are load-bearing and enforced by tests. Multi-provider routing
fundamentally changes those assumptions, so switchboard is a separate project
that **imports sluice's pure core** (`sluice.control`, `sluice.gate`,
`sluice.reconcile`, the `TruthSource` protocol) and builds a routing shell on
top.

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
- **Streams** request/response bytes through untouched (same streaming +
  disconnect-cancellation + phantom-prevention logic as sluice)
- Consumes the **usage-dashboard** `/readings` API as a truth source for
  providers that have no native usage endpoint (e.g. ollama)

## What it does not do

- **No request body translation.** Both upstreams must speak the same API
  format (OpenAI-compatible `/v1/chat/completions`). The `/v1/messages`
  (Anthropic) surface is passthrough to a single Anthropic-compatible upstream
  only — no cross-format routing.
- **No response caching, prompt logging, or model routing** beyond
  provider-level failover. Those are out of scope.
- **No re-serialization of request bodies.** Cache-transparency applies within
  each upstream path — bytes are forwarded as-is.

## Design principles

- **Deterministic, stdlib-only routing core.** The routing decision is a pure
  function over provider states. No I/O, no async, no clock. Pass time and
  observations in as arguments.
- **Thin shell around sluice's core.** switchboard imports `sluice.control`,
  `sluice.gate`, `sluice.reconcile`, and the `TruthSource` protocol. It adds
  multi-provider composition and routing; it does not modify the core.
- **Fail safe.** When all providers are pressured, route to the configured
  default and let its gate handle the rejection. Never route to a provider
  whose gate is closed without checking the alternatives.
- **Streaming is sacred.** Same as sluice: never buffer a full response body;
  proxy bytes as they arrive. A change that breaks token streaming is a
  regression.
- **Cache-transparency per-provider.** The request sluice egresses to each
  upstream must be byte-for-byte what the client sent. Routing selects *which*
  upstream; it does not reshape the request.

## Scope: in / out / non-goals

**In:**
- Multi-provider route table (API-key-based routing)
- Per-provider gates, reconcile loops, and truth sources
- Pressure-based failover routing (pure function)
- usage-dashboard integration as an ollama truth source
- Admin dashboard for route table CRUD
- Metrics per-provider and per-client

**Out (for now):**
- Request/response format translation (Anthropic ↔ OpenAI)
- Per-client weighting or fair queuing beyond FIFO per provider
- Per-model routing (routing is per-provider, not per-model)

**Non-goals:**
- Replacing sluice for single-provider use cases
- Building a general-purpose API gateway
- Prompt logging, response caching, or content inspection

## Relationship to sibling tools

| Tool | Role |
|---|---|
| **sluice** | Single-upstream concurrency governor. switchboard imports its core. |
| **usage-dashboard** | Multi-provider usage monitor running in k8s. switchboard consumes its `/readings` API for ollama usage data. |
| **opencode** | A client of switchboard. Points its `baseURL` at switchboard; switchboard handles routing transparently. |
