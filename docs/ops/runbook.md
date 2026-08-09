# switchboard operator runbook

This runbook is for the on-call operator. It assumes you did **not** build
switchboard and need to read its state, react to common conditions, and make
changes safely. For the design rationale see `docs/routing-model.md`; for the
deployment mechanics see `docs/deployment.md`.

## 1. Quick reference

**What switchboard is.** switchboard is a reverse proxy that sits in front of
multiple upstream LLM providers (opencode-go, ollama-cloud, zai, …). A client
points at switchboard instead of at a vendor; switchboard picks a provider for
each request, streams the response through untouched, and — when a provider
runs out of quota — routes the request to somebody else instead of failing.

**Where it runs.** Deployed to the existing k8s cluster, namespace
`usage-dashboard`, internal-only ingress via `traefik-internal`. It is **not**
reachable from outside the LAN: it holds every provider credential. Image
`ghcr.io/hraedon/switchboard:main`.

**How to check it is healthy.**

| Check | Endpoint | Expect |
|---|---|---|
| Process alive | `GET /healthz` | `200 {"status":"ok"}` — always, even during startup |
| Ready to serve | `GET /readyz` | `200 {"status":"ready"}` once **every** provider's first poll has completed; `503 {"status":"not ready"}` before that |
| Full state | `GET /status.json` | Per-provider state + route table + routing metrics (see §2) |
| Prometheus | `GET /metrics` | Counters and gauges in Prometheus text format |

`/healthz` backs the liveness probe; `/readyz` backs the readiness probe. A
`readyz` that is `503` for the first ~10–30 s after a rollout is **normal** —
see §9.

## 2. Reading `/status.json`

`GET /status.json` returns a JSON object. The important top-level keys:

```
{
  "providers": { "<name>": { ... }, ... },
  "route_table": { "<hashed-key>": ["primary", "fallback"], "default": [...] },
  "routing_metrics": { ... },
  "version": "0.1.0",
  "build": "<sha|null>"
}
```

### 2.1 Per-provider state (`providers.<name>`)

Each provider reports the full state of its reconcile loop and gate. The
fields an operator actually watches:

| Field | Type | Meaning |
|---|---|---|
| `ready` | bool | `true` once this provider's first truth poll succeeded. Until then the provider is `UNKNOWN` and receives no traffic. |
| `availability` | string | One of `available`, `busy`, `closed`, `unknown` (§3). |
| `signal_freshness` | string | One of `fresh`, `degraded`, `unknown` (§3). |
| `in_flight` | int | Permits currently held = requests in flight to this provider. **Must be `0` when idle.** A non-zero value that never returns to 0 after load stops is a leaked permit — a bug. |
| `capacity` | int | Current gate capacity (permits the reconcile loop has granted). |
| `available_permits` | int | `capacity - in_flight - ...` permits free right now. |
| `queue_depth` | int | Requests waiting for a permit. |
| `gate_closed_reason` | string|null | Why the gate is closed: `boxed`, `breaker`, `saturated`, or `null`. |
| `breaker` | string | Breaker state: `closed`, `open`, `half_open`. |
| `band` | string | Enforcement band: `normal`, `low`, `reject`, `boxed`, `low_interactivity`. |
| `recent_429s` | int | 429s in the breaker window. |
| `total_429s` | int | Lifetime 429s. |
| `usage_age` | float | Seconds since the last truth poll. Large = stale. |
| `stale` | bool | `true` if the last poll failed (serving last-known-good). |
| `upstream_url` | string | The provider base URL switchboard forwards to. |
| `speed.ttfb_ms.avg` | float | Mean time-to-first-byte (ms), Plan 020 Wave 3. |
| `speed.tokens_per_sec` | float | Mean completion tokens/sec. |

Diagnostic fields you will rarely need but that the code exposes:
`effective_permits`, `breaker_half_open_age_seconds`, `penalty_started_at`,
`concurrent_sessions`, `limit`, `hard_cap`, `priority_low`, `priority_reason`,
`boxed_until`, `resets_at`, `service_mode`, `low_interactivity`,
`phantom_estimate`, `requests_in_window`, `requests_remaining`,
`local_requests_in_window`, `throughput`, `idle`, `cooling_down`,
`avg_wait_seconds`, `p95_wait_seconds`, `avg_hold_seconds`,
`retry_after_hint`, `queue_timeouts`, `target`, `min_floor`,
`poll_interval`, `controller`, `provider_name`, `overrides`.

When an `[overload]` breaker is configured there are also
`overload_consecutive`, `overload_cooling`, `overload_cooldown_remaining`.
When a `[token_budget.<p>]` is configured there are `token_utilization` and
`token_budget`. When a `[usage_24h_budget.<p>]` is configured there is
`usage_history`.

### 2.2 Routing metrics (`routing_metrics`)

| Field | Meaning |
|---|---|
| `forwarded_per_provider` | Successful forwards per provider. |
| `failovers` | Times a non-primary provider was selected. |
| `routing_decisions` | Total routing decisions made. |
| `recent_decisions` | Bounded ring of recent decisions (key hash, selected, primary). |
| `evicted_decisions` | Entries dropped from the bounded ring. |
| `affinity_pins_total` | Affinity pins created (failover stickiness). |
| `affinity_failbacks_total` | Pins released on failback to primary. |
| `affinity_evictions_total` | LRU evictions of affinity entries (pin loss). |
| `usage_reroutes_total` | Requests moved off a provider that returned a usage error. |
| `usage_reroutes_from` | Same, counted by the exhausted origin provider. |
| `usage_giveups_total` | **The estate-is-exhausted counter** — requests that got a usage error with no eligible provider left (§4). |

## 3. Throttle states

Two independent axes combine to describe a provider: **availability** (can it
take work?) and **signal freshness** (do we trust the data?).

### 3.1 Availability (`availability`)

| State | Meaning | What to do |
|---|---|---|
| `available` | Eligible and a permit is free right now. | Normal. Traffic flows. |
| `busy` | Eligible but no permit is free (saturated). | Normal under load. Requests queue or fail over to the next provider. If it stays `busy` with no load, check `capacity` and `gate_closed_reason=saturated`. |
| `closed` | Cannot be selected: the provider is boxed, its breaker is open, or an overload cooldown is active. | Check `gate_closed_reason` and `breaker`. `boxed` → wait for `boxed_until`. `breaker` → wait for the cooldown; if it never recovers, the provider may be unhealthy (§9). |
| `unknown` | Not ready — the first truth poll has not completed yet, or the signal is too stale to trust. | **Normal during startup** (§9). If it persists for more than ~60 s, the truth source is unreachable. |

### 3.2 Signal freshness (`signal_freshness`)

| State | Meaning | What to do |
|---|---|---|
| `fresh` | The last truth poll succeeded. | Normal. |
| `degraded` | The last poll failed; serving last-known-good. The provider can still serve as the **primary** but is not chosen as a **new** failover target. | Check `usage_age` and the logs for the poll failure. Transient. If it persists, the truth source (dashboard or `/v1/usage`) is down. |
| `unknown` | Not ready — no successful poll yet. | Normal during startup. |

### 3.3 Combined reading

- **`available` + `fresh`** — healthy. This is the normal steady state.
- **`available` + `degraded`** — serving on stale data. Fine for the primary;
  failover will prefer a fresher provider. Investigate if it lasts more than
  a few minutes.
- **`busy` + `fresh`** — saturated. Traffic queues or fails over. Expected
  under load.
- **`closed` + `fresh`** — the provider's own signal says no (boxed/breaker).
  Respect it; do not force traffic.
- **`unknown` + `unknown`** — still starting up. Wait.

## 4. Estate exhaustion

When **every** eligible provider returns a usage error (429/402/503/529),
switchboard has nowhere to route. This is the one condition routing cannot fix.

**What happens.** The request gets a `503` (or the upstream's own status on the
terminal attempt) with a `Retry-After` header. The event is counted as a
"give-up" and logged at `WARNING` naming every provider tried and the status
each returned.

**How to identify it.**

- In `/status.json`: `routing_metrics.usage_giveups_total` is rising.
- In `/metrics`: `switchboard_usage_giveups_total` is rising.
- In the log: `WARNING switchboard.proxy: usage-error give-up: every eligible
  provider returned a usage error — providers tried: a=429, b=429`.

**What to do.**

1. Confirm it is real exhaustion and not a config error: check each provider's
   `availability` and `resets_at`/`boxed_until`. If they are `closed` with a
   future `boxed_until`, the provider is genuinely out of quota.
2. **Add a provider** (§5) to give the estate headroom, OR
3. **Wait for the quota reset** — `resets_at` (epoch seconds) tells you when
   the provider's window reopens. The `Retry-After` the client receives is
   derived from the same value.

Do **not** restart switchboard to "clear" exhaustion — the upstreams are still
out of quota. Do **not** keep the estate in a state where give-ups are the
norm; that is a capacity signal.

## 5. Adding a provider

There are three equivalent ways; the config-store write (GUI or admin API)
outranks TOML on the next restart (Plan 020 D1: store > TOML).

### 5.1 Via the dashboard (GUI)

Open the dashboard (`/`), go to **Providers**, and add a provider. The GUI
writes to the config store.

### 5.2 Via the admin API

```
POST /admin/providers
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "name": "ollama-cloud",
  "upstream": "https://ollama.com/v1",
  "type": "generic",
  "target": 4,
  "api_key_env": "SWITCHBOARD_OLLAMA_CLOUD_KEY"
}
```

The full set of accepted fields is: `name`, `upstream`, `type`, `target`,
`key_mode`, `api_key_env`, `api_key_stored`, `auth_header`, `auth_prefix`,
`dashboard_url`, `dashboard_token_env`, `usage_key_env`, `enabled`. Unknown
keys are ignored.

**Credential indirection (`api_key_env`).** In production the credential
almost always comes from the environment, not the request body: set
`api_key_env` to the name of the environment variable that holds the key (e.g.
`SWITCHBOARD_OLLAMA_CLOUD_KEY`), and inject that variable (from a k8s Secret).
switchboard reads the variable at startup. If the variable is unset or empty,
switchboard **refuses to start** rather than fall back to forwarding the
client's own credential — a missing Secret looks like an immediate crash, and
that is intentional. See `docs/deployment.md`.

To supply a key directly (throwaway local runs only), use `api_key_stored`.
The API never returns a stored key — see §7.

### 5.3 Via TOML config

Add a `[provider.<name>]` section and (re)start, or let the boot merge pick it
up:

```toml
[provider.ollama-cloud]
upstream = "https://ollama.com/v1"
type = "generic"
target = 4
api_key_env = "SWITCHBOARD_OLLAMA_CLOUD_KEY"
```

A provider is reachable by routing only once it appears in a route's
`providers` list or the default route — adding it is not enough; see
`[route.default]` in `examples/agent-delegation.toml`.

## 6. Removing a provider

### 6.1 Via the dashboard (GUI)

**Providers** → remove. The GUI writes to the config store.

### 6.2 Via the admin API

```
DELETE /admin/providers/<name>
Authorization: Bearer <admin-token>
```

**Drain behavior (Plan 020 WI-2).** Removal is a two-phase operation:

1. The provider disappears from the live map **immediately**, so no new
   request can be admitted to it.
2. In-flight requests **complete** — the gate is resized to 0 (which stops
   new permits but never revokes one already held) and switchboard waits for
   `in_flight` to reach 0 before closing the provider's HTTP client.

The drain is bounded by `drain_timeout` (default 25 s). A stream that never
ends is cut off after the timeout and the in-flight count is logged loudly.
So a removal is safe for in-flight work but not instant.

## 7. Rotating a provider credential

Because the credential lives in the environment (§5.2), rotation has two
steps:

1. **Update the environment variable** that `api_key_env` points at — in k8s,
   update the Secret and roll the deployment (or restart the pod). The new
   value is read at the next startup; there is no hot-reload of env.
2. To rotate **without a restart**, use the admin API:
   ```
   PUT /admin/providers/<name>
   Authorization: Bearer <admin-token>
   Content-Type: application/json

   {"api_key_stored": "sk-new-key-here"}
   ```

**Write-only key semantics.** The admin API treats `api_key` / `api_key_stored`
as write-only. It is never echoed back on `GET /admin/providers` or
`GET /admin/config/effective` — those surfaces report `api_key_set: true`
instead of the value. A credential is therefore something you set and then
forget you can read; if you need to verify it, test the provider against the
upstream directly, not via switchboard's API.

## 8. Rolling back the opencode migration

The migration (Plan 017 WI-8) pointed opencode at switchboard by adding a
`switchboard` provider whose `options.baseURL` points at the deployed Service.
To roll back, point opencode at the direct providers again.

On the opencode host, edit `~/.config/opencode/opencode.jsonc`:

- **Remove or comment out** the `switchboard` provider entry (the one whose
  `options.baseURL` points at the switchboard Service).
- Ensure the direct-provider entries (opencode-go, ollama-cloud, …) are still
  present and not commented out — they were left in place during migration
  precisely so this is a one-line rollback.

opencode reads its config at startup, so restart opencode after the edit. No
change to switchboard itself is required; it simply stops receiving opencode
traffic.

## 9. Common issues

### `/readyz` returns 503

**Normal for the first ~10–30 s after a (re)start.** `/readyz` is `503` until
**every** provider's first truth poll has completed. With several providers
and a 5 s poll interval this is brief but real. Do not treat a transient 503
as an outage. If it persists beyond ~60 s, a provider's truth source is
unreachable — check `status.json` for a provider stuck at `unknown` and the
logs for poll failures.

### A provider shows `unknown`

Its first poll has not completed. During startup this is expected. If it
persists, the truth source (the `upstream` URL for `generic` providers, or the
`dashboard_url` for dashboard-backed ones) is unreachable from the pod.
Verify network policy / DNS / that the upstream is up. A provider that is
`unknown` receives **no** traffic — it is excluded from failover and admission.

### A provider shows `degraded`

Its last poll failed but it has last-known-good data, so it keeps serving as
the **primary**. It is deliberately **not** chosen as a new failover target —
`degraded` data never improves preference. Check `usage_age` (seconds since
the last good poll) and the logs. Transient; if it lasts more than a few
minutes the truth source is down.

### Routes not matching

A request falls back to the default route when its API key matches no keyed
entry. Keyed routes are keyed by the **SHA-256 hash** of the raw API key (or
HMAC-SHA-256 when `SWITCHBOARD_ROUTE_KEY_SECRET` is set). A mismatch means
the stored hash was computed from a different key or a different secret. If
HMAC is enabled, the secret used to hash newly-added keys is
`SWITCHBOARD_ROUTE_KEY_SECRET`; entries hashed without a secret (or under a
previous secret outside the dual-read window) will not match. Re-add the key
via `POST /admin/routes` after confirming the secret.

### A provider is `closed` with `gate_closed_reason=breaker`

The breaker opened after too many concurrency 429s. It will transition to
`half_open` after `breaker_cooldown_seconds` and recover on the next healthy
response. If it never recovers, the provider is returning 429s on every
request — an upstream health issue, not a switchboard issue.

### `in_flight` never returns to 0

After all load stops, every provider's `in_flight` (held permits) must return
to `0`. A stuck non-zero value is a **leaked permit** — a bug, usually a
request whose upstream or client disconnected without releasing the gate.
Note the provider and the `recent_decisions`, and report it.


## 10. Routing strategies

`[routing] strategy` controls how switchboard orders the **immediate**
(capacity-available) candidates before applying affinity and primary
preference. The default preserves the behaviour switchboard has always had.

| Strategy | Behaviour |
|---|---|
| `ordered` (default) | Table order. The primary fronts unless an affinity pin or an opportunistic burn overrides it. |
| `headroom` | Order by `usage_headroom` descending (Plan 015) — the provider with the most remaining **session** headroom goes first. Equivalent to the older `headroom_ranking = true`; setting both is rejected. |
| `pace` | Order by **weekly quota surplus** descending (Plan 020 D5). A provider that will not plausibly spend its remaining weekly quota before it resets has a positive surplus and is burned first — use it or lose it. |

**What pace actually computes.**

```
surplus = weekly_remaining_fraction - pace_burn_rate_per_day * days_until_reset
```

`pace_burn_rate_per_day` (default `0.14`) is the nominal daily fraction of the
weekly quota you expect to consume — a week's quota spent evenly is 100%/7 ≈
14.3%. A positive surplus means "ahead of schedule, spend it"; a negative one
means "burning too fast, conserve".

Three properties matter when you are deciding whether to trust it:

- **Only fresh signals are scored.** A provider whose weekly reading is stale,
  missing, or whose truth source is down is *unscored*. Unscored providers rank
  after scored ones in table order — never starved, never scored on stale data.
- **The primary is never demoted.** Under pace it can lose the *front*, but it
  stays immediate-eligible, the queue backstop, and the terminal fallback.
- **An affinity pin still wins.** Pace reorders candidates; it does not break
  conversation pinning or dwell.

`pace_flap_margin` (default `0.05`) is a deadband: when the leader's surplus
advantage over the runner-up is smaller than the margin, table order is kept
instead of re-ranking. It stops two near-equal providers alternating on every
request. Note it compares the top two *candidates*, not the currently-serving
provider — it is a deadband, not true hysteresis with memory.

**Changing it.** Either surface works, and they validate identically:

```toml
[routing]
strategy = "pace"
pace_burn_rate_per_day = 0.14
pace_flap_margin = 0.05
```

or at runtime, without a restart:

```
PUT /admin/config/routing
Authorization: Bearer <admin-token>
Content-Type: application/json

{"strategy": "pace"}
```

The dashboard's **Routing Config → Edit Strategy** does the same thing. A
runtime change takes effect on the next routing decision **and is persisted to
the config store**, so it survives a restart and outranks the TOML value
(Plan 020 D1: the store wins). Only the fields you actually change are stored —
everything else keeps following the config file. The response includes
`"persisted": true`; if it says `false`, no store is configured
(`route_table_store` unset) and your change will be lost on restart.

To hand a knob back to the config file, reset the stored config
(§5) — there is no "unset one field" verb.

**When pace does nothing.** If no provider reports a weekly window, every
candidate is unscored and pace is identical to `ordered`. Check
`providers.<name>.weekly_remaining_fraction` in `/status.json`: `null` means no
weekly signal is reaching switchboard, and the strategy has nothing to rank on.
