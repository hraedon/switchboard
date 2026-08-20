# Plan 027 — Model capability contract & synthesized /v1/models

Status: **All five waves implemented and validated 2026-08-20** — unit +
proxy-integration + GUI suites passing, `ruff`/`mypy src` clean, chaos
harness 5/5. Not yet exercised against a live provider (discovery is
advisory; the parity probe and the synthesis path are covered by mocked
tests). Authored 2026-08-19 (from Sol's model-contract feasibility review of
the live estate).

Depends on: the model map (Plan 010 Feature B) for the
`(logical_model, provider)` alias pairs, upstream path composition (Plan 021)
and credential normalization (the egress choke points) for discovery parity,
the config store / route-table SQLite substrate (Plan 020) for persisted
observations, and the Plan 023/024 discipline of "prompt, do not auto-assert"
for everything that changes what clients are told.

Resolves: **WI-009** (client-facing `/models` forwards to one upstream
instead of exposing the configured model set).

## 1. What was found

Six defects, from the model-contract feasibility review:

1. **High — `/models` is nondeterministic and provider-specific.**
   `GET /models` and `GET /v1/models` fall through to normal admission
   (`proxy.py`'s `__call__` → `_proxy_request`): the client sees whichever
   provider wins the moment, including that provider's raw aliases. Internal
   routing choices leak to the client and the answer changes on failover.
   Tracked as WI-009.

2. **High — the capability framework is dormant.** `ProviderCapabilities`
   and `RouteEntry.required_capabilities` exist in `control.py`, but
   `snapshot_provider_state()` never populates `capabilities`, no config
   surface sets `required_capabilities`, and route persistence stores only
   provider names. Capability-based filtering exists only in tests.

3. **Medium — discovery does not match live forwarding.** The admin probes
   build `ctx.upstream_url.rstrip("/") + "/models"` and concatenate
   `f"{auth_prefix}{api_key}"`, while live forwarding composes through
   `compose_upstream_path()` and normalizes the auth prefix (a `Bearer`
   without the trailing space becomes `Bearer KEY` at the egress choke
   point). A provider can proxy fine while discovery 404s (version segment
   unstripped on a versioned base, or missing on a bare-host base) or 401s
   (glued auth prefix).

4. **Medium — discovery discards exactly the metadata this feature needs.**
   `_parse_openai_models()` retains only IDs and fully buffers an
   unbounded upstream response before JSON parsing.

5. **Medium — GUI auto-match clears the model preference.**
   `applyAutoMatch()` re-reads the entry (aliases only) and POSTs only
   `aliases`; `POST /admin/model-map` treats an omitted `preference` as
   "clear it". A one-click alias add silently deletes the operator's
   provider order for that model.

6. **Medium — model-map parsing can leak RecursionError.**
   `_extract_model()` catches `ValueError`/`TypeError` only; a deeply
   nested (but syntactically valid) JSON body makes `json.loads` recurse
   past the limit and crashes the request path. Its sibling
   `extract_conversation_fingerprint()` already catches this.

## 2. Why the naive version is wrong

Two temptations to resist:

**Attach a few fields to the existing `/models` proxy response.** The
metadata a client needs is a property of the `(logical_model, provider)`
pair across every provider that may serve the model — not of the one
provider that answers. Reading it off whichever upstream wins admission
re-creates defect 1 with extra steps. The unit is a **model contract**:
what is guaranteed for a logical model over the *failover set*, computed
conservatively, exposed separately from what is merely *observed*.

**Infer capability from model names or advertise observed values as
guaranteed.** "gpt-4" means nothing about context on a gateway that
fronts several backends. A value seen on one provider is not a contract
for a failover target that may reject it mid-stream. Three consequences:

- Missing metadata stays **unknown**, never guessed.
- An unknown or stale participant makes the affected field
  **not-guaranteed**; the observed value is still exposed, separately, so
  the operator can see it — but it is not fed to clients as a promise.
- Reasoning controls are advertised **only when every possible failover
  target accepts the same wire encoding at the same levels**. With
  request-body translation prohibited (hard rule 5), a divergent provider
  cannot be coerced — the choices are: advertise only the common levels,
  flag the divergent provider, drop it from the model's failover set, or
  define a second logical model. The first two are what this plan
  implements; the second is an operator route-table decision.

And on the dormant framework: the provider-**global** `ProviderCapabilities`
surface is the wrong shape for this — context limits and reasoning are
model/provider-pair properties, and a per-route `required_capabilities` list
has no config surface and no data behind it. Rather than plumbing fake data
through a dormant filter, this plan adds a separate **pure
model-capabilities core** and makes *that* the capability mechanism in
production. The old surface stays (harmless, tested) and remains available
for a future route-level capability gate.

## 3. What it does

### 3.1 Wave 1 — the shared discovery path (defects 3–6)

- **`control.credential_value(prefix, key)`** — the pure normalization the
  egress choke point applies (empty prefix stays empty; a non-blank-trailing
  prefix gets exactly one separating space). `_apply_provider_credential`
  and every admin probe call it, so "what the probe sends" and "what the
  proxy sends" are the same function again.
- **Probe URL parity.** `handle_provider_test` and `handle_provider_models`
  probe `compose_upstream_path(ctx.upstream_url, "/v1/models")` — the same
  composition live forwarding applies to the canonical client shape —
  instead of raw concatenation. `handle_provider_discover` keeps its
  three-candidate exploration (it probes an *unsaved* base) but normalizes
  credentials through the same helper. A parity test asserts the probe URL
  and header equals what `_build_url` / `_apply_provider_credential`
  produce for the same context.
- **Bounded reads.** Discovery streams the upstream body and stops at a
  configured cap (4 MiB default; a `/models` listing beyond that is
  reported as `response too large`, not buffered). Non-2xx detail
  extraction is separately capped (16 KiB).
- **Retaining metadata.** The ID-only parser is replaced by a **pure
  normalizer** (`model_capabilities.parse_model_listing`) that accepts the
  parsed payload and emits one `ModelObservation` per model:
  - OpenAI base shape: IDs only (everything else stays unknown — the base
    format carries no limits).
  - vLLM: `max_model_len` → context limit.
  - LiteLLM: `max_input_tokens` / `max_output_tokens`.
  - OpenRouter: `context_length`, `top_provider.{context_length,
    max_completion_tokens}`, `architecture.{input,output}_modalities`,
    `supported_parameters` (tool calling, reasoning, and the *exact*
    reasoning wire format: `reasoning_effort` vs the `reasoning` object).
  - Fields the shape does not carry remain `None`/UNKNOWN. Nothing is
    inferred from the model name.
  The admin `/models` endpoint returns the legacy `models` id list *and*
  the richer `models_meta` (additive; the GUI's datalist is untouched).
- **Auto-match preference loss.** `applyAutoMatch()` now reads the
  re-fetched entry's `preference` (and the status.json `model_preferences`
  as the fallback snapshot) and posts it back, so the add-or-replace POST
  carries the operator's existing order instead of clearing it.
- **RecursionError.** `_extract_model()` catches `RecursionError` like its
  sibling, so a crafted 300k-deep body yields "no model field" and is
  forwarded untouched, not crashed.

### 3.2 Wave 2 — the pure core (`switchboard/model_capabilities.py`)

Stdlib-only, no I/O, no clock: time and observations are arguments.
Covered by `tests/test_import_boundary.py`'s pure-module list.

- **`TriState`** (TRUE / FALSE / UNKNOWN) and the reductions:
  - numeric limits → **minimum** (with completeness tracking);
  - modality sets and parameter sets → **intersection**;
  - booleans → three-valued AND (any known FALSE guarantees FALSE;
    all-known-TRUE guarantees TRUE; otherwise UNKNOWN);
  - reasoning levels → intersection **only when the exact wire encoding
    agrees** across all known participants; otherwise no levels are
    advertised (boolean-only reasoning support).
- **`ModelObservation`** — `(provider, alias, context_limit,
  input_limit, output_limit, input_modalities, output_modalities,
  tool_calling, reasoning, reasoning_format, reasoning_levels,
  observed_at, source, config_fingerprint)`.
- **`compute_contract(model, participants, observations, caps, now,
  max_age) -> ModelContract`** — the LCD over the participants. A
  participant **counts** into the contract even while its gate is closed:
  a temporarily-down provider returns, and the compatibility contract
  must already cover it. Freshness (not health) is the only reason a
  participant cannot certify a field.
  Each field carries its own **source** in the result: `verified`
  (fresh observation at every participant), `capped` (operator cap below
  observed), `declared` (operator cap filling a gap the providers never
  reported), or `observed` (best-effort value, not guaranteed). A cap set
  **above** an observed limit is clamped to the observed value and
  reported as a **deviation** — a declared value above reality is a lie
  the operator will hit on the wire.
- **`render_client_models(contracts) -> dict`** — the OpenAI-compatible
  `{"object": "list", "data": [...]}` body for `/v1/models`: canonical
  model IDs, deterministic order, no provider names anywhere, and an
  `x-switchboard` block per model carrying **guaranteed-only** metadata.
- **`render_opencode_config(contracts, base_url, ...)`** — the adapter to
  OpenCode's custom-provider model schema: `limit.{context,input,output}`
  from verified limits only, `reasoning` only when verified,
  `attachment`/`tool_call` only when verified, input/output modality lists
  only when verified, and **named variants only for verified reasoning
  levels in the verified wire format** (`reasoningEffort` vs
  `reasoning.effort`). With levels unverified but support verified, the
  generated config sets `reasoning: true` and **no** variants is avoided —
  instead `reasoning` stays `false` unless the operator's caps declare
  support (marked declared), because a bare `reasoning: true` makes
  OpenCode fabricate generic variants for a control some failover target
  may ignore.

### 3.3 Wave 3 — persisted observations + admin reporting

- **`capability_store.py`** (shell) — one row per `(provider, alias)` in
  the shared route-table SQLite file: freshest observation wins, with
  `observed_at` and the provider-config **fingerprint** (hash of the
  provider's effective config) recorded so a config change is visible as
  staleness of the evidence. Generation-safe: DB-first writes like the
  model map, boot distrusts corrupt rows (warn + skip, never crash).
- **`model_discovery.py`** (shell) — `refresh(providers)`: for each live
  provider, GET the parity URL with the parity credential, bounded read,
  `parse_model_listing`, `store.upsert`. Returns a per-provider summary
  (`ok`, count, detail). This is the single probe implementation — the
  GUI's "show models" / "auto-match" handlers call the same one, so the
  probe and the store can never diverge again.
- **Background loop** (lifespan, like the usage-history loop): a fast
  tick, internally throttled to `[capabilities.discovery] interval`
  (default 6 h; `0` = manual only). Discovery is advisory: it feeds
  metadata surfaces, never routing.
- **`GET /admin/model-capabilities`** — the full estate matrix: per model,
  per participant: freshness (fresh/stale/missing), fingerprint match,
  observed values; the computed contract with per-field sources,
  deviations, and the reasoning-advertisement decision.
- **`POST /admin/model-capabilities/refresh`** — operator-triggered
  immediate discovery (auth + CSRF gated, like the other probe endpoints).
- **Config** (`serve` keys + `[capabilities]` section, env-overridable
  like the rest): `capabilities_discovery_interval` (s, default 21600,
  0 = off), `capabilities_max_age` (s, default 86400), and per-model
  operator caps under `[capabilities.models."<name>"]`
  (`context_limit`, `input_limit`, `output_limit`, `tool_calling`,
  `reasoning`, `input_modalities`, `output_modalities`). Cap values are
  clamped and deviation-reported per §3.2.

### 3.4 Wave 4 — the canonical `/v1/models` (resolves WI-009)

`__call__` short-circuits `GET` on `/models` or `/v1/models` **only when
the model map is non-empty** (empty map = feature off, identical fall-
through to today, per ModelMap semantics):

1. Resolve the caller's route key → candidate list (a keyed route sees
   its own candidate set; an unkeyed caller sees the default route's —
   "an API key scoped to one backend does not see the whole estate").
2. For each mapped model, participants = candidates ∩
   `model_map.providers_for(model)` ∩ live providers. Models with no
   participant are omitted (that route cannot serve them).
3. Compute the contract per model (§3.2) from the freshest stored
   observations + operator caps. Missing/stale data ⇒ guaranteed fields
   absent; the model ID itself is always listed.
4. `render_client_models` → `send_json` with
   `Cache-Control: private, no-store`. No provider names, no credentials,
   no forwarding. Reading no request body and sending a synthesized body,
   it is a **switchboard-owned control-plane exception**, documented as
   exception (4) in AGENTS.md's inert-in-path list. Generation responses
   remain byte-inert.

Deterministic for a given (model map, route, stored observations): the
answer no longer depends on who won admission.

### 3.5 Wave 5 — OpenCode config generation

- **`GET /admin/client-config/opencode`** (admin-gated) —
  `render_opencode_config` over the estate-wide contracts (every live
  provider holding an alias, same LCD/participant rules), emitting a paste-
  ready OpenCode provider block. `baseURL` is derived from the request's
  Host (+ trusted forwarded-proto); `apiKey` is an explicit placeholder
  the operator fills — switchboard does not mint route keys into a
  document that may be pasted somewhere.
- **`switchboard opencode-config`** CLI subcommand — fetches the endpoint
  from a local/admin URL (token via `SWITCHBOARD_ADMIN_TOKEN`, never a
  flag) and prints the JSON to stdout: the "local sync" surface, so a
  machine can regenerate its config after any discovery refresh.

## 4. What it deliberately does not do

- **No routing enforcement.** Discovery is advisory in this plan: the
  contract shapes what clients are *told*, not which providers are
  filtered. Enforcing contracts at admission (filtering providers whose
  verified capabilities fall short of what the contract advertises) is a
  separate opt-in plan on top of the same core.
- **No request-body translation, ever.** A reasoning format divergence is
  answered with "advertise the intersection", never with a rewrite.
- **No admin write surface for operator caps** (GUI/TOML parity) in this
  plan — caps are TOML-only until the GUI catches up.
- **No provider-global capability plumbing.** `ProviderCapabilities` /
  `required_capabilities` stay as-is (tested, available for a future
  route-level gate); the model contract is the production capability
  mechanism.

## 5. Work items

| WI | Wave | Summary |
|---|---|---|
| 027-W1 | Wave 1 | discovery URL/credential parity, bounded reads, RecursionError, auto-match preference |
| 027-W2 | Wave 2 | pure model_capabilities core (normalizer, LCD, caps, renderers) |
| 027-W3 | Wave 3 | capability store, discovery runner, admin matrix + refresh, config |
| 027-W4 | Wave 4 | synthesized canonical /v1/models (resolves WI-009) |
| 027-W5 | Wave 5 | OpenCode config generation (endpoint + CLI) |
