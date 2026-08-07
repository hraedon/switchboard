# Plan 020 — GUI-managed configuration, per-provider statistics, pace-based routing

Status: proposed (authored 2026-08-06, for Paul's review)

Depends on: Plan 018 (landed — switchboard is self-contained). Interacts
with: Plan 019 (multi-account + conversation pinning, still draft — §9),
and Paul's in-flight usage-dashboard work adding opencode-go monitoring
(§7, the signal source for pace routing).

## 1. What Paul asked for

1. Configure switchboard from its own GUI — providers, models, API keys,
   routing decisions — with at least sluice-grade API-key auth.
2. Per-provider usage and speed statistics, visible in the GUI.
3. "Smart" routing: send each request to the provider with the most usage
   remaining relative to its reset time, assuming a nominal burn-down of
   14%/day.
4. Add new models via the GUI.

## 2. What already exists (do not rebuild)

Grounded against main @ `715f03e`:

- **Auth (covers the #1 requirement as stated).** Admin mutations already
  require the admin token (Bearer or `x-api-key`-style via
  `is_admin_auth_value`) or a logged-in session (`switchboard_session`
  cookie, `LoginThrottle`, CSRF check). This *is* sluice's API-key auth,
  vendored. Nothing new needed; every new endpoint reuses
  `check_admin_auth`/`check_csrf`.
- **Model map**: full CRUD API (`/admin/model-map` GET/POST/DELETE with
  provider-name validation) and a "Model Map" GUI panel. #4 is an
  extension, not a build. Known debt: WI-12b (partial-write rollback;
  SQLite-loaded aliases skip provider validation).
- **Route table**: CRUD API + GUI panel.
- **Routing core hooks**: `ProviderState.usage_headroom` +
  `quota_resets_in` exist; Plan 015 headroom ranking and Plan 016
  opportunistic burn are implemented behind config flags. Pace routing
  (#3) is a third strategy on the same rails, not a new engine.
- **Signal plumbing**: `DashboardTruthSource` polls usage-dashboard
  `/readings` per provider (`dashboard_url`/`dashboard_token_env`),
  maps `session_percent` and `session_resets_at` today.
- **History substrate**: `switchboard.history` ring + SQLite store,
  warmed on restart — the trend surface for #2's GUI panels.

What does **not** exist: runtime provider lifecycle (providers are
boot-time TOML only), GUI-managed API keys, any latency/TTFB/token-rate
measurement, weekly-window mapping from readings, and the pace strategy.

## 3. Design decisions (settle before implementing)

**D1 — Config store and precedence.** New SQLite-backed
`ConfigStoreManager` (same pattern as route table / model map) owning
GUI-created and GUI-modified providers. Boot: TOML is loaded first, then
the store is overlaid — a store row with the same provider name wins
wholesale (no field merging; merging is where silent config bugs live).
Every GUI mutation writes the store first, then applies live; a restart
therefore converges to the same state. A GET `/admin/config/effective`
returns the merged view (keys masked) so the operator can always see
what is actually running and diff it against the TOML.

**D2 — API keys at rest.** GUI-entered keys are stored in the config
store's SQLite file, `0600`, on a volume that is not the repo. Rules
that are not negotiable: keys are **write-only through the API** (update
= overwrite; read returns `"api_key_set": true` and a last-4 hint at
most); keys never appear in `/admin/config/effective` output, status
payloads, logs, or Prometheus; `api_key_env` indirection remains the
recommended mode and the GUI shows which mode each provider uses.
Encryption-at-rest via a `SWITCHBOARD_MASTER_KEY` env var is designed
into the schema (a `key_ciphertext` column and a `key_mode` discriminator)
but implemented as a follow-on WI — on this estate the file-permission
model matches how `secrets.env` is handled today, and pretending Fernet
adds real security while the master key sits in the same environment
would be theater. Flag it honestly in the GUI ("stored on the server").

**D3 — Runtime provider lifecycle.** A `ProviderManager` that can, while
serving: **add** (build `ProviderContext`, start its reconcile task,
register in the routing map), **remove** (deregister first so no new
admissions, stop the reconcile loop, wait for held permits to drain with
a bounded timeout, then close the httpx client and history store), and
**replace** for immutable-field changes (upstream/key/type: add the new
context under the same name atomically with the swap, drain the old one
in the background). `target` changes reuse the existing override
machinery. The drain discipline matters: closing an httpx client with
streams in flight is how phantom-cancellation bugs happen — the permit
count reaching zero is the signal that the context is quiet.

**D4 — Speed statistics without touching the streaming contract.** The
proxy already brackets `_forward`; add per-request samples recorded at
points that already exist: TTFB (admission → first upstream body byte —
the probe boundary already observes this), total duration (the existing
`hold_seconds` measurement), completion token counts from the existing
`UsageObserver` (SSE + JSON paths) giving tokens/sec. Samples go into a
bounded per-provider ring (deque, ~512 samples ≈ an hour of moderate
traffic) with p50/p95 computed on read. Exposure: `/status.json`
(`providers.<name>.speed`), Prometheus (`switchboard_ttfb_seconds` /
`switchboard_request_seconds` / `switchboard_tokens_per_second`
summaries), and GUI cards. **No body buffering, no new reads of body
content** — token counts come only from the observer that already parses
usage frames. Persisting speed aggregates into `HistoryEntry` requires
the schema-migration machinery the vendoring deliberately dropped; that
is a separate opt-in WI, not a prerequisite.

**D5 — Pace scoring (the 14%/day rule).** Pure function in
`switchboard.control`:

```
expected_burn(t_reset)  = burn_rate_per_day × t_reset/86400
surplus(p)              = remaining_fraction(p) − expected_burn(t_reset(p))
```

with `burn_rate_per_day` defaulting to **0.14** (Paul's number; ~a
weekly quota consumed evenly, 100%/7d ≈ 14.3%). Rank eligible providers
by `surplus` descending: a provider that will not plausibly spend its
remaining quota before reset is use-it-or-lose-it and should be burned
first — this is Plan 016's philosophy promoted from "opportunistic
override" to the primary ordering. Worked example: A has 80% remaining,
resets in 5 days → surplus 0.80 − 0.70 = +0.10. B has 40% remaining,
resets in 1 day → 0.40 − 0.14 = +0.26. **B wins**: its quota dies
sooner. Guardrails:

- Only providers with a FRESH usage signal are scored; unscored
  providers rank after scored ones in config order (they are never
  starved — availability/reroute semantics are untouched).
- Scoring uses the **weekly** window (that is what 14%/day means).
  Session-window pressure stays what it is today: an availability/
  boxing signal, not a ranking input.
- Hysteresis: reuse Plan 014's affinity/failback machinery unchanged;
  additionally, re-rank only when the leader's surplus advantage
  exceeds a configurable margin (`pace_flap_margin`, default 0.05) so
  two near-equal providers don't alternate per-request.
- Strategy is a config enum: `routing.strategy = "ordered" | "headroom"
  | "pace"`, default `ordered` (current behavior). Pace is opt-in.

**D6 — Weekly-window plumbing.** `LimitState` gains explicit
`weekly_remaining_fraction: float | None` and `weekly_reset_epoch:
float | None` (in-memory only — no history schema change).
`_reading_to_limit_state` maps `weekly_percent`/`weekly_resets_at` from
readings; `snapshot_provider_state` carries them into `ProviderState`.
The existing `usage_headroom`/`quota_resets_in` mappings stay as they
are so Plans 015/016 behavior is unchanged.

## 4. Work items

**Wave 0 — debt that #4 builds on**
- **WI-0**: Close WI-12b — model-map partial-write rollback, and
  SQLite-loaded aliases validated against configured providers at load
  (log-and-skip invalid rows, never crash boot).

**Wave 1 — config store + lifecycle (#1 core)**
- **WI-1**: `ConfigStoreManager` — SQLite schema for providers
  (name, upstream, type, target, auth fields, key columns per D2,
  `account` column defaulted for Plan 019 forward-compat), load/merge
  per D1, unit tests incl. corrupt-store fail-safe.
- **WI-2**: `ProviderManager` runtime lifecycle per D3 — the hardest WI;
  needs drain tests (in-flight stream survives its provider's removal;
  no new admissions after deregister; double-remove is a no-op).
- **WI-3**: Admin API — `GET/POST /admin/providers`,
  `PUT/DELETE /admin/providers/<name>`, plus
  `POST /admin/providers/<name>/test` (cheap upstream reachability check
  with the configured key; returns status + latency, never the key).
  Masked serialization per D2. Auth/CSRF identical to route handlers.
- **WI-4**: `GET /admin/config/effective` (masked) + boot-merge wiring +
  docs for the precedence rule.
- **WI-5**: k8s persistence — the store needs a PVC (today nothing the
  pod writes survives a restart); single-replica assumption documented
  (leadership lease remains Plan 008's missing piece, out of scope).

**Wave 2 — GUI (#1 + #4 surface)**
- **WI-6**: Providers panel: list with live state (from status.json),
  add/edit/remove forms, key-mode indicator, test-connection button.
- **WI-7**: Models panel: create/edit a model with per-provider aliases
  (the API exists; the panel gains an add flow + inline validation
  errors from the 400 responses).
- **WI-8**: Routing panel: route-table CRUD (exists) + strategy selector
  and pace knobs (burn rate, flap margin) once Wave 4 lands the config.
  MUST include runtime editing of the DEFAULT route: the Wave 1 live
  smoke proved a GUI-created provider serves only once a route names it
  (the model map filters a route's candidates, it never adds to them),
  and today the default route is boot-only — so "add provider" in the
  GUI is a dead end until this lands. Persist the default in the store
  alongside keyed routes.

  **WI-8a (default route) DONE 2026-08-07.** `route_default` table in the
  route-table store, `PUT /admin/routes/default`, and a dashboard editor.
  Precedence per D1: a persisted default outranks TOML; `load_from_config`
  now respects a store-sourced default the same way it already respected
  persisted keyed entries. Boot-derived values (the all-providers fallback,
  the unknown/tombstone filters) are applied with `persist=False` so a
  transient condition never gets frozen on disk.

  Asymmetry worth keeping in mind for the rest of Wave 2: **an unknown
  provider in a TOML default is fatal, in a stored default it is a warning.**
  A GUI edit must never be able to make the process unbootable, because the
  GUI is then unreachable — the same rule should govern WI-6's provider forms
  and WI-7's model aliases.

  **Not done, still WI-8:** the strategy selector and pace knobs (correctly
  gated on Wave 4), and keyed-route CRUD in the GUI — the API exists but the
  panel does not.

  Left behind for WI-6/7: `tests/gui/default_route.mjs`, a DOM-shim harness
  that drives dashboard JS under node with no browser dependency, wrapped by
  `tests/test_gui_default_route.py` (skips when node is absent). It pins the
  behaviours a GUI regression actually loses — a poll landing mid-edit
  discarding typed input, a 400 not surfacing the rejected name, an
  unescaped provider name. Extend it rather than starting a browser stack.

**Wave 3 — statistics (#2)**
- **WI-9**: Speed sampling per D4 (TTFB, duration, tokens/sec) into
  per-provider rings; unit tests with the mock-stream harness (mock
  upstreams MUST use `httpx.AsyncByteStream` per the test gotcha).
- **WI-10**: Exposure: status.json block, Prometheus summaries, GUI
  per-provider cards (usage remaining + reset countdown + p50/p95 TTFB
  + TPS + reroute/error counters) and sparklines from the history ring.
- **WI-11 (optional, gated on need)**: persist speed aggregates into
  history — requires reintroducing column-migration machinery first
  (cycle-2 review finding: the vendored store deliberately has none).

**Wave 4 — pace routing (#3)**
- **WI-12**: Weekly plumbing per D6 + estate config: opencode-go (and
  peers) get `dashboard_url` pointing at usage-dashboard once Paul's
  ocg monitoring lands there. Contract test against the recorded
  readings fixture (extend it with the ocg reading shape when available).
- **WI-13**: `pace` strategy in `switchboard.control` per D5 — pure,
  stdlib-only, property-style tests (surplus ordering, stale exclusion,
  margin hysteresis, worked examples from this plan as literal cases).
- **WI-14**: Strategy config + GUI toggle + decision transparency: the
  recent-decisions panel shows each candidate's surplus at decision
  time, so "why did it go there" is answerable from the GUI.
- **WI-15**: Live validation on mvmcc03: two providers with skewed
  remaining/reset → traffic follows the surplus; kill the dashboard →
  scores go stale → ordering falls back to config order with no errors;
  flap test proves the margin works.

## 4b. Deferred review findings (wave 0+1, deepseek cycle 1)

Carried forward deliberately, not forgotten:

- **Top-level JSON-500 wrapper in `ProxyApp.__call__`** (finding 4): an
  exception escaping a handler aborts the connection with no JSON error
  body. Needs care around already-started streaming responses; take it
  in Wave 2 alongside the GUI error handling.
- **`check_csrf` passes when neither `Authorization` nor
  `Sec-Fetch-Site` is present** (finding 10, pre-existing): inherited by
  every mutating endpoint including the upstream-spending `/test` probe.
  Tighten estate-wide in Wave 2 (all modern browsers send
  `Sec-Fetch-Site`; the permissive branch exists for curl workflows —
  decide whether to keep it for token-authed requests only).

  Still open, and now inherited by `PUT /admin/routes/default` as well
  (WI-8a review, GLM). Deliberately NOT fixed there: it is pre-existing
  and estate-wide, so fixing it inside one WI would change the auth
  posture of every mutating endpoint under cover of a routing change.
  The count of endpoints riding on it grows with each Wave 2 panel —
  the longer this waits, the larger the blast radius of the eventual
  fix, which argues for taking it as its own WI early in Wave 2 rather
  than as a rider on the last one.
- **First PUT of a TOML-only provider** (finding 3, residual): the list
  now exposes `key_mode`/`api_key_set` so the GUI can pre-fill, but the
  API itself still accepts a full-row PUT that changes key_mode without
  ceremony. Wave 2's form flow should require an explicit key decision.

## 5. What must not break (verify per wave)

- Streaming: never buffer bodies; no new body reads outside the
  existing observer. The reroute loop, credential stripping/replacement,
  and 4xx-never-rerouted semantics are untouched by every WI here.
- `switchboard.control` stays stdlib-only (import-boundary test).
- Keys never round-trip (grep the test suite for a masked-serialization
  test on every surface: config/effective, providers GET, status.json,
  metrics, logs).
- Trio green per WI; cross-lineage review per wave (cycle-2-on-fixes
  discipline — unreviewed fix commits have bitten twice).

## 6. Sequencing and estimates

Wave 1 is the long pole (WI-2 especially). Waves 2 and 3 are independent
of each other once Wave 1 lands; Wave 4 only needs WI-12's plumbing plus
whatever usage-dashboard ships. Rough shape: Wave 0 small; Wave 1 ~3 WIs
of a day-scale each for a delegated implementer with review; Waves 2–4
each a solid session. Nothing here blocks Plan 019, but see §9.

## 7. External dependency: usage-dashboard ocg monitoring

Pace routing is only as good as its signal. Needed from the dashboard's
opencode-go reading (same `Reading.to_dict()` schema): `weekly_percent`
(or the ocg equivalent normalized to it), `weekly_resets_at`, honest
`stale`/`fetched_at`. If ocg only exposes a differently-shaped window,
normalize it dashboard-side so switchboard keeps one contract.
Switchboard-side integration is deliberately thin: one more provider
with a `dashboard_url`.

## 8. Known constraint: WI-002

opencode.ai blocks the k3s cluster egress (Cloudflare 1010), so on the
k8s instance a pace score for opencode-go is academic until that is
resolved — the reactive reroute will bounce those requests regardless.
Pace routing must tolerate "scored but persistently failing" providers;
the existing overload breaker (Plan 010) already provides the demotion,
and WI-15 should include this case in the live validation.

## 9. Relationship to Plan 019 (multi-account)

WI-1's schema carries an `account` discriminator from day one so 019
does not force a migration. Conversation pinning (019) outranks pace
ordering by design — a pinned conversation stays put until failback
hysteresis moves it; pace decides only for unpinned/new work. Land 020
Wave 1 before starting 019's account work so accounts are rows in one
store, not a second store.

## 10. Done when

- A provider can be added, keyed, tested, routed to, statted, and
  removed entirely from the GUI, surviving a restart.
- A new model with per-provider aliases can be created from the GUI and
  served immediately.
- The GUI shows, per provider: usage remaining, reset countdown, TTFB
  p50/p95, request duration, tokens/sec, forwarded/reroute/error counts.
- With `strategy = "pace"`, live traffic demonstrably follows the
  surplus ranking, degrades to config order on stale signal, and every
  §5 invariant still holds.
