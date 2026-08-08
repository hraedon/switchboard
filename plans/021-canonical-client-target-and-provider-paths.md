# Plan 021 — One canonical client target, per-provider paths

Status: Waves 1 and 3 landed (WI-7 partial, blocked on Wave 2 WI-5); Wave 2 proposed
Supersedes: the "clients must omit the version prefix" contract in
`k8s/configmap.yaml` and `docs/deployment.md`.

## 1. What Paul asked for

> We can offer one canonical target for clients and we should be able to
> configure the full path for each provider, and all of that should be
> doable in the GUI. Switchboard should ideally be able to figure it out
> from provider instructions (which often end at v1); otherwise we need
> to add validated paths as supported providers. k8s/docker env
> variables can win over gui configuration. Clients should need to make
> no accommodations for switchboard to work.

Five requirements, in priority order:

1. **Clients make no accommodations.** Point a client at switchboard the
   same way you would point it at OpenAI, and it works.
2. **Per-provider full path**, configurable.
3. **All of it GUI-manageable.**
4. **Infer the path from the provider's own documented base URL** — the
   string the vendor's quickstart gives you, which usually ends at `/v1`.
   Fall back to a curated registry of validated providers.
5. **Env vars beat GUI configuration**, so a deployment can always assert
   control.

## 2. Why this is needed (the evidence)

Today `ProxyApp._build_url` (`proxy.py:1898`) is:

```python
upstream = ctx.upstream_url.rstrip("/")
return upstream + scope["path"]
```

Pure concatenation of the client's verbatim path onto the provider base.
`k8s/configmap.yaml` states the consequence as a rule for clients:

> Each upstream is the provider's COMPLETE API root, because switchboard
> appends the client's path verbatim. Clients must therefore be pointed
> at switchboard WITHOUT a version prefix and send /chat/completions.

Measured against the live deployment 2026-08-08:

```
POST /chat/completions      -> 200   (real completion)
POST /v1/chat/completions   -> 404   path "/v1/v1/chat/completions" not found
```

That is the accommodation requirement #1 forbids, and it is the default
shape of every OpenAI-compatible client — `baseURL` conventionally ends
in `/v1`. The failure is a 404 from the *upstream*, so it reads as
"switchboard is broken" rather than "your base URL has one segment too
many".

Worth stating plainly: this is a **documentation and contract** problem,
not a defect. The current behaviour is deliberate and works. Plan 021
moves the accommodation from the client to switchboard, which is where
Paul wants it.

## 3. Design decisions

**D1 — The canonical client target is a normal OpenAI-compatible base
URL.** A client sets `baseURL = https://switchboard.<host>/v1` and emits
`/v1/chat/completions`. No switchboard-specific configuration, no
documentation caveat, no "remember to drop the /v1".

**D2 — Switchboard splits the client path, it does not concatenate it.**
The incoming path is parsed as `[<version-prefix>]/<endpoint...>`; the
version prefix is discarded and the remainder is the *endpoint*. The
upstream URL becomes `provider_base + "/" + endpoint`.

A leading segment is treated as a version prefix when it matches
`^v\d+(?:[a-z]+\d*)?$` — `v1`, `v2`, `v1beta`. **At most one** segment is
stripped, and only in leading position. Everything else is endpoint.

**Refined during implementation:** the prefix is dropped *only when the
base's own last segment is a version*. The rule reads: **the base
declares the version if it has one.**

The first draft stripped unconditionally, and that was wrong. A base with
no version (`https://api.example.com`) relying on the client to supply
`/v1` is a configuration that **works today** — unconditional stripping
would have composed `https://api.example.com/chat/completions` and broken
it. Conditioning on the base makes the compatibility claim below true for
every working configuration rather than merely for the documented one,
which is the claim that actually matters for something in the request
path.

This is what makes D4 work: because the endpoint no longer carries a
version, the provider base is free to carry whatever version *it* uses,
which is exactly what vendor documentation hands you.

Worked against the three configured providers, using their documented
bases unchanged:

| provider | documented base | client sends | upstream built |
|---|---|---|---|
| opencode-go | `https://opencode.ai/zen/go/v1` | `/v1/chat/completions` | `…/zen/go/v1/chat/completions` |
| ollama-cloud | `https://ollama.com/v1` | `/v1/chat/completions` | `…/v1/chat/completions` |
| zai | `https://api.z.ai/api/coding/paas/v4` | `/v1/chat/completions` | `…/paas/v4/chat/completions` |

All three verified 200 against the live upstreams on 2026-08-08.

**Backward compatible by construction.** A client that sends the current
bare `/chat/completions` has no version prefix to strip, so the endpoint
is `chat/completions` and the composed URL is identical. Both client
shapes work; nothing already deployed breaks. That is worth more than
elegance here — switchboard is in the live request path.

**D3 — Cache-transparency is unaffected, and the principle needs
rewording.** The README's "the request switchboard egresses must be
byte-for-byte what the client sent" has always been about the *body*;
the path was already being altered in effect by choosing a different
provider's host. D2 makes path composition explicit rather than
incidental. No body is read, parsed, or re-serialised by this plan.

**D4 — Provider config gains an explicit base, and the registry gains
validated shapes.** `switchboard.truth.Provider` (`truth.py:404`) already
carries `name`, `default_base_url`, `auth_header`, `needs_usage_key` for
four entries (umans, anthropic, openai, generic). Extend it with a
validated path shape and probe endpoint, and populate real entries for
the providers actually in use — opencode zen, ollama cloud, z.ai coding
plan — plus the obvious OpenAI-compatible fleet. Choosing a registry
provider in the GUI fills the base URL with a known-good value.

**D5 — Discovery: paste the vendor's base URL and let switchboard probe
it.** `POST /admin/providers/<name>/test` already exists (Plan 020 WI-3)
and already composes a second endpoint — it issues `GET
{upstream}/models` with the provider's own credential. Extend it into a
*discovery* mode that tries the small set of plausible compositions in
order and reports which one answers:

1. `base/models` (base already carries the version — the common case)
2. `base/v1/models` (base is a bare host)
3. `strip trailing /vN from base`, then `/models` (base has a version the
   endpoint would duplicate)

The GUI shows what was tried and what answered, and offers to save the
winning base. This is requirement #4, and it degrades honestly: when
nothing answers, say so and point at the registry rather than guessing.

`/models` is not universal, so a provider may declare a different probe
endpoint in the registry. A probe that cannot run is reported as
"unverified", never as a pass — a green tick that means "we didn't
check" is the failure mode of every config validator.

**D6 — Precedence becomes env > GUI store > TOML file.** Today the
store beats TOML wholesale (Plan 020 D1), and there is no env tier for
provider fields. Paul's requirement inverts the top: an environment
variable set in the k8s Deployment or `docker run` must beat whatever
the GUI last wrote.

The GUI must then **show env-controlled fields as locked**, with the
variable name that owns them. A form that silently discards a save is
worse than one that refuses it. `/admin/config/effective` must report a
`source` per field (`env` | `store` | `toml` | `default`) — it already
exists and is masked; this makes it answer "why is it this value", which
is the question an operator actually has.

**D7 — There must be a way to reclaim state from the store.** This is a
real gap found while assessing production readiness, not a theoretical
one. On the live deployment the store's model map excludes `opencode-go`
for `glm-5.2` while `configmap.yaml` includes it. Because
`ModelMapManager.load_from_config` uses `overwrite=False` when a store is
configured (`model_map.py:166`), config entries seed only *absent*
models — so editing the configmap and redeploying **cannot** fix it, and
the divergence survives restarts on the PVC. With no admin token set,
the API cannot fix it either. The state is unreachable by any in-band
means.

Env-wins (D6) does not solve this on its own: it covers fields that have
an env var, not store rows generally. Proposal: `POST
/admin/config/reset` with an explicit section (`providers`, `model-map`,
`routes`, `route-default`), which deletes store rows for that section so
the declared config becomes authoritative again — plus a
`SWITCHBOARD_CONFIG_RESET` env var doing the same at boot for the case
where the API is unreachable. Both log loudly.

**DECIDED by Paul 2026-08-08: env-only, plus reset.** Mounted TOML does
not beat the store — the GUI stays meaningful, its edits persist across
restarts (the point of Plan 020), and the configmap is a seed. Recovery
is D7's explicit reset rather than an implicit precedence rule, so
reclaiming state is always a deliberate, logged act rather than a
surprise on the next rollout.

## 4. Work items

**Wave 1 — the contract — DONE 2026-08-08**
- **WI-1 (done)**: Path composition per D2 — a pure function
  (`compose_upstream_path(base, client_path) -> str`) in the stdlib-only
  core, with table-driven tests covering: version stripped, no version,
  nested endpoints, query strings preserved, `/v1` appearing *inside* the
  endpoint (must not be stripped twice), trailing slashes, and the three
  live provider shapes as literal cases. Wire into `_build_url`.
- **WI-2 (done)**: Update `k8s/configmap.yaml`, `docs/deployment.md`, and the
  README to state the canonical target. Delete the "clients must omit the
  version prefix" instruction. `docs/deployment.md` is **separately
  stale** — it still documents the `--build-context sluice=../sluice`
  build removed by Plan 018 — fix that in the same pass.

**Wave 2 — provider configuration**
- **WI-3**: Registry per D4: extend `Provider`, populate validated
  entries, expose them via the admin API for the GUI's picker.
- **WI-4**: Discovery probe per D5, extending the existing `/test`
  handler. Must not spend real tokens: `/models` is a GET.
- **WI-5**: GUI provider form — registry picker, base URL field, "test
  and detect" button, and the composed URL shown as a live preview
  before saving. Seeing `…/zen/go/v1/chat/completions` render as you
  type is worth more than any validation message.

**Wave 3 — precedence and recovery — WI-6 and WI-8 done 2026-08-08**
- **WI-6 (done)**: Env tier per D6 for every provider field, with `source`
  reporting through `/admin/config/effective`.
- **WI-7 (partial, done 2026-08-08)**: the dashboard now renders a
  Configuration panel — the owning tier per provider, the fields an env
  var has taken over, disabled providers, and a warning listing env
  overrides that matched no provider (inert, and previously visible only
  in a boot log that has long scrolled away).

  **The locking half is blocked, not skipped.** Locking an input needs an
  input: the only editable control on the page is the default route, and
  the provider form is Wave 2 WI-5. When that lands it must consume
  `field_sources` to disable env-owned inputs — the data is already
  served and rendered, so it is a form-level change, not new plumbing.
- **WI-8 (done)**: `POST /admin/config/reset` + `SWITCHBOARD_CONFIG_RESET`
  per D7, with the store rows it deletes logged by name.

  Env overrides merge **per field**, deliberately unlike D1's wholesale
  store-replaces-TOML rule: a Deployment usually pins one value and has
  no business discarding the rest of a provider to do it. Providers
  whose names collide on an env stem (`opencode-go` / `opencode_go` both
  give `OPENCODE_GO`) refuse to start rather than guess which one an
  override meant. `api_key` is deliberately NOT overridable — a raw
  credential must arrive by `api_key_env` indirection so it is never a
  value this path could echo into a config dump or an error message.

  The recovery loop was proven end to end against the live divergence
  reproduced locally: boot with the bad store state and the configmap
  cannot fix it; boot with `SWITCHBOARD_CONFIG_RESET=model-map` and the
  declared config is reclaimed; boot again normally and it stays
  reclaimed.

  Still open: **WI-7**, the GUI lock indicators. Until it lands the
  `field_sources` / `env_locked` data is served by
  `/admin/config/effective` but nothing renders it, so a GUI edit to an
  env-owned field will still appear to succeed and then be ignored at
  the next boot.

## 5. What must not break

- **Streaming.** No body is read or buffered by any WI here; path
  composition happens before the request is opened.
- **The reroute loop.** It re-enters `_build_url` per attempt, so a
  rerouted request must compose against the *new* provider's base. Pin
  this with a test — it is the obvious place for a bug to hide.
- **Credential replacement.** Untouched; the header path is independent.
- **`switchboard.control` stays stdlib-only** (import-boundary test).
- **Keys never round-trip.** The discovery probe uses the provider's own
  credential and must report status and latency only — never echo the
  URL with an embedded key, and never the key.
- Existing deployments sending bare `/chat/completions` keep working
  (D2's compatibility property) — regression-test both client shapes.

## 6. Done when

A client is pointed at `https://switchboard.<host>/v1` with no
switchboard-specific configuration and serves traffic; a new provider is
added entirely through the GUI by pasting the vendor's documented base
URL and pressing test; an env var in the Deployment demonstrably
overrides a GUI-set value and the GUI says so; and the live model-map
divergence in §D7 is recoverable without editing SQLite by hand.
