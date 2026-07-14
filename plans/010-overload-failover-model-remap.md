# Plan 010 — Overloaded-response failover with model remapping

Status: proposed execution plan

Depends on: Plan 006 (categorical routing), Plan 008 (production contracts)

Blocks: usable umans → ollama-cloud failover for the current stuck-retry problem

## 1. Problem

A client (opencode / a subagent) points at umans and, when umans is in
**low-interactivity mode**, gets a stream of "The service is temporarily
overloaded. Please retry." responses. The client's own retry loop hammers the
same overloaded upstream forever (`retrying in 2s — attempt #380`). switchboard
is meant to be the place that shunts that traffic to a healthy provider, but two
gaps stop it:

1. **Overloaded responses are invisible to routing.** `proxy.py` only feeds the
   breaker on `status_code == 429`. A `503`/`529` "overloaded" passes straight
   through, the provider stays `AVAILABLE`, and switchboard keeps selecting it.
2. **No model bridge.** The only viable fallback (ollama-cloud) labels models
   differently from umans — `umans-kimi-k2.7` vs `kimi-k2.7-code`. A plain
   passthrough failover would forward the umans model name to ollama-cloud and
   get rejected. Failover is useless without a per-provider model rewrite.

Decisions taken with the operator (2026-07-14):
- **Layering:** sluice *surfaces* the low-interactivity box (parses the
  `/v1/usage` signal and exposes it as a gate-closed reason); it does **not**
  reroute — it can't, it is single-provider. **switchboard** consumes that
  surfaced state and reroutes. This is the family's standard split (sluice core
  surfaces state, switchboard composes routing).
- **Two signals, one outcome (`CLOSED` → failover):**
  - *Proactive:* sluice's surfaced `low_interactivity` reason (from the usage
    field). Routes away **before** the client eats an overloaded response.
  - *Reactive backstop:* switchboard counts consecutive overloaded *responses*
    per provider and trips its own breaker — covers the window where the usage
    signal lags or is absent, and is provider-neutral.
- Fallback is **ollama-cloud**, authenticated with a **separate stored API key**.
- Model names are **structurally different** across providers, so switchboard
  **must read and rewrite the `model` field** on the fallback path.

> Note: this supersedes the earlier "overloaded-count only" scoping — the
> operator subsequently asked that sluice also surface the low-interactivity box.
> Overloaded-count remains, now as the reactive backstop rather than the sole
> mechanism.

## 2. Constraint changes (this plan amends AGENTS.md + README)

This plan deliberately narrows two founding rules. The narrowing is bounded and
applies **only** to the fallback (rewrite) path:

- **"Inert in-path — never reads/rewrites bodies"** → narrowed to: switchboard
  MAY read the request body's top-level `model` field, and MAY rewrite **only**
  that field, and only when a `[model]` map is configured. No other field is
  read, logged, stored, or altered. Response bodies remain fully inert.
- **"Cache-transparency — byte-identical egress"** → narrowed to: byte-identical
  egress is guaranteed **when no model rewrite occurs** (the primary/umans path,
  which is the caching-sensitive one). When switchboard rewrites `model` for a
  fallback provider, it re-serialises the request JSON; that provider's path is
  explicitly *not* byte-transparent. umans prompt-caching is unaffected because
  the umans path never rewrites.

Both narrowings are opt-in: with no `[model]` map configured, switchboard behaves
exactly as today (no body reads, full byte-transparency everywhere).

## 3. Feature 0 — sluice surfaces the low-interactivity box

sluice-side, additive and fail-safe. sluice does **not** reroute; it makes the
state visible so switchboard (and the dashboard) can act on it.

- `usage.py::parse_usage`: read `usage.service_mode` (**confirmed shape**, see
  `samples/service-mode-capture-2026-07-14.md`): `{current: str, resets_at: ISO}`.
  Also capture `usage.tokens_in`/`tokens_out` (for Feature C). Absent
  `service_mode` → not-low-interactivity (fail safe; overload breaker + priority/
  boxed are the backstops). NOTE: low-interactivity is under `usage.service_mode`,
  **distinct** from `usage.priority` (low/boxed) — surface both independently.
- `control.py`: extend `UsageReading` with `service_mode` (str | None) +
  `service_mode_resets_at_epoch` + `tokens_in`/`tokens_out`; add a pure predicate
  `is_low_interactivity(reading, *, now)` = `service_mode == "low_interactivity"
  and resets_at_epoch is not None and resets_at_epoch > now` — unexpired-only, so
  a stale past timestamp never latches (same lesson as `boxed_until`).
- `reconcile.py::gate_closed_reason`: return `"low_interactivity"` when the
  predicate holds (checked alongside `boxed`). `retry_after_seconds` derives
  from `interactive_at` (deadline, floored/capped like the boxed branch).
- Surfaced in sluice's `/status.json` so the existing dashboard shows it.

sluice's own gate closing on low-interactivity means a *direct* sluice user gets
an honest 503 + Retry-After instead of hammering — a strict improvement even
without switchboard. Ships as its own sluice release (its CI + release.yml).

## 4. Feature A — Overloaded-response breaker (switchboard-side, reactive backstop)

Kept **in switchboard**, not sluice: the desired behaviour is *route away to
another provider*, which is inherently multi-provider. sluice's 429 breaker is
untouched (it stays deployed and load-bearing).

New module `switchboard/overload.py`:

```python
@dataclass
class OverloadState:
    consecutive: int = 0
    cooldown_until: float = 0.0   # monotonic; provider CLOSED while now < this

class OverloadTracker:
    # per-provider OverloadState; all methods take `now` (pure-time, testable)
    def record_overloaded(self, provider, *, now, retry_after) -> None: ...
    def record_ok(self, provider) -> None: ...          # resets consecutive
    def is_cooling(self, provider, *, now) -> bool: ...
    def cooldown_remaining(self, provider, *, now) -> int: ...
```

- **Overloaded** = upstream response whose status is in `overload_statuses`
  (default `{503, 529}`; final set confirmed from a live capture).
- After `overload_threshold` consecutive overloaded responses (default **3**),
  set `cooldown_until = now + cooldown`, where `cooldown` = the response's
  `Retry-After` if present and sane, else `overload_cooldown_default` (default
  **30 s**), clamped to `[5, 300]`.
- Any non-overloaded (2xx/3xx, or a *different* error) response calls
  `record_ok` and resets the counter — a provider that recovers is immediately
  eligible again after the cooldown lapses.
- `snapshot_provider_state` (providers.py) maps **both** closure signals to
  `CLOSED`: sluice's surfaced `gate_closed_reason() == "low_interactivity"`
  (proactive, Feature 0) *and* the overload tracker's `is_cooling` (reactive).
  Reason surfaced as `"low_interactivity"` or `"overloaded"` respectively;
  `retry_after_seconds` from `interactive_at` or `cooldown_remaining`.
- `route_decision` already fails over on `CLOSED`. No change to the pure router
  for this feature beyond threading the reason through for display.

`proxy._forward` gains: after it has the upstream `response`, classify the status
and call `tracker.record_overloaded(...)` / `record_ok(...)` for `ctx.name`.
This sits right next to the existing 429 classification block.

## 5. Feature B — Model-aware failover with rewrite

### 5.1 Config (`[model]` section, parsed in providers/route config)

```toml
[model."umans-kimi-k2.7"]
umans        = "umans-kimi-k2.7"
ollama-cloud = "kimi-k2.7-code"

[model."umans-glm-4.7"]
umans        = "umans-glm-4.7"
ollama-cloud = "glm-4.7-code"
```

The **incoming** model string (what the client sends) is the map key. Each entry
maps provider-name → the exact model string that provider expects.

Pure types + helpers live in `switchboard/control.py` (keeps the core the asset):

```python
@dataclass(frozen=True)
class ModelMap:
    routes: dict[str, dict[str, str]]      # incoming_model -> {provider: alias}
    def providers_for(self, model) -> frozenset[str]   # who can serve it
    def alias_for(self, model, provider) -> str | None # target name, or None
```

### 5.2 Proxy flow changes

Chat/completions request bodies are sent up-front (not streamed), so buffering
the *request* body is cheap and does not touch response streaming.

1. **Buffer** the request body (bounded by existing `max_request_body_bytes`),
   parse JSON, read top-level `model`. Non-JSON / missing `model` / no `[model]`
   map → behave exactly as today (no filtering, forward original bytes).
2. **Filter candidates** to `model_map.providers_for(model)` before
   `route_decision` — a provider that can't serve the requested model is not a
   valid failover target for this request. (Reuses the existing capability-filter
   shape in `route_decision`; model becomes another eligibility input.)
3. **Route + admit** unchanged.
4. **Egress:** if `alias_for(model, chosen) == incoming model` → forward the
   **original buffered bytes** (byte-transparent). Else rewrite the `model` field,
   re-serialise, forward the new bytes (fallback path, transparency waived).

Buffering-then-forwarding original bytes is byte-identical to today for the
primary path; only the rewrite branch changes bytes.

## 5b. Feature C — empirical low-interactivity threshold estimator

Learn, over time, the usage level at which low-interactivity engages — while
being honest that the request/token-threshold hypothesis may be **wrong**. The
estimator is built to test it, not assume it.

**Inputs.** Every `/v1/usage` poll already carries `requests_in_window` and
`tokens_in/out`. Once Feature 0 labels each reading with `low_interactivity`,
each poll is a sample `(requests, tokens, concurrent_sessions, low_interactivity,
window_id)` where `window_id` is derived from `resets_at` (counters reset per
window). Requires: Feature 0's `parse_usage` to also capture `tokens_in/out`
(currently unparsed), and a small **sluice accessor** exposing the last
`UsageReading` counters to switchboard (`reconcile.last_reading()` or similar).

**Estimate (pure core, `switchboard/threshold.py`).** Detect OFF→ON transitions
per window; the trigger is bracketed by poll cadence:

- upper bound = smallest usage seen ON-at-transition;
- lower bound = largest usage seen while still OFF;
- best guess = midpoint of the tightest *consistent* bracket — report the
  bracket + sample count, never a bare point;
- computed independently for **requests** and **tokens**, with
  `concurrent_sessions` as a control. The dimension whose bracket stays tight as
  samples accumulate is the likely binding constraint; a `lower > upper`
  contradiction across windows means it is **not** a simple usage threshold —
  surface that as the finding rather than emitting a false number.

Pure update: `update_estimate(prior, sample) -> estimate`. The shell polls,
detects transitions, and persists.

**Persistence.** Samples/estimate persist to SQLite (reuse the route-table store
seam) so learning survives restarts.

**Surfaced, not acted on (v1).** `status.json` + dashboard show the estimate and
"% of estimate" position. **Proactive routing** on the estimate (bleeding traffic
to ollama-cloud *before* the box hits) is an explicit **opt-in follow-up**, gated
until the estimate proves stable — v1 never reroutes on a guess.

## 6. Config — the ollama-cloud provider

`[provider."ollama-cloud"]` with `type` (generic/openai), its `upstream` URL,
and `usage_key_env` for the separate stored key. It has no umans-style
`/v1/usage`, so it uses the header/AIMD truth source (or Null) — availability is
driven by its gate + the overload breaker, which is exactly what we want.

## 7. Work items

**sluice (Feature 0 — branch `feat/surface-low-interactivity`, its own release):**
- **[DONE]** **WI-0a** `usage.py` parses `usage.service_mode` + `tokens_in/out`
  (confirmed shape); `control.py` gains the fields + `is_low_interactivity`
  predicate; unit tests off the captured payload. Commit `5b5670d`.
- **[PARTIAL]** **WI-0b** `ReconciliationLoop.is_low_interactivity()` accessor +
  status.json surfacing done (`5b5670d`). Deliberately did **NOT** touch
  `gate_closed_reason` (low-interactivity still serves degraded — a direct sluice
  user keeps getting service; switchboard does the routing). REMAINING: add
  `low_interactivity_retry_after()` accessor (deadline from
  `service_mode_resets_at_epoch`), then tag + release as **v1.4.0**; bump
  switchboard's `sluice>=1.4.0` pin.

**switchboard (this repo, on `main`):**
- **[DONE]** **WI-1** `overload.py` + unit tests. Commit `7b15c6b`.
- **[TODO]** **WI-2** proxy: overloaded classification (503/529) → tracker
  (easy, next to the 429 block); `snapshot_provider_state` consults tracker +
  `is_low_interactivity()` → `CLOSED`.
- **[DONE]** **WI-3** `ModelMap` + `servable_providers` filter in `route_decision`
  + core tests. Commit `7b15c6b`.
- **[TODO]** **WI-4** proxy: request-body buffering, `model` extraction, servable
  filtering, fallback rewrite; guarded no-op when no `[model]` map. *(Riskiest —
  alters the request path; primary/umans path must stay byte-transparent.)*
- **[TODO]** **WI-5** config parsing for `[model]` + `[provider."ollama-cloud"]`.
- **[TODO]** **WI-6** AGENTS.md + README constraint-narrowing edits (§2 above).
- **[TODO]** **WI-7** integration test (stub upstreams via `httpx.MockTransport`):
  umans 3×503 → failover to stub ollama-cloud with model rewritten; subsequent
  200 clears cooldown; `low_interactivity` reading → immediate failover.
- **[TODO]** **WI-8** watch CI (3.12/3.13/3.14) per hard rule 7.

**switchboard (Feature C — sequence after A/B land):**
- **[DONE]** **WI-9** `threshold.py` pure estimator + unit tests. Commit `7b15c6b`.
- **[TODO]** **WI-10** shell: poll→sample→persist wiring (SQLite), transition
  detection from `last_reading()`; estimate on `status.json` + dashboard.

## 8. Non-goals

- No response-body inspection or rewrite — responses stay fully inert.
- No cross-format translation (OpenAI ↔ Anthropic); both providers speak the
  same wire format. Model remap is a field rename, not a format bridge.
- No per-request cost/token routing.
- sluice does not reroute — it only surfaces state (Feature 0).
