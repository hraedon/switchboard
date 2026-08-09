# Plan 023 — Provider/model quarantine

Status: **WI-1 – WI-3 landed; WI-4 landed except the dashboard panel (WI-006);
WI-5 live-validated 2026-08-09.** Authored 2026-08-09.

Depends on: the model map (Plan 010 Feature B) for the (provider, model) pair,
and the config store (Plan 020 Wave 1) for persistence.

## 1. What Paul asked for

> Five failures should get the provider/model combo quarantined until the user
> takes a look and resolves the issue.

Three requirements: a **threshold** (five), a **key** (provider × model, not
provider alone), and **stickiness** — it stays quarantined until a human clears
it, because the trigger means something needs a person, not a timer.

## 2. Why the obvious version is dangerous

The request came out of watching five consecutive 403s from opencode-go. That
evidence turned out to be self-inflicted: Cloudflare error 1010, banning the
*client's* User-Agent (`Python-urllib/3.x`), which switchboard forwards verbatim
because egress is deliberately byte-identical. The same request with
`curl/8.5.0`, `opencode/1.0`, or a browser UA returned 200. opencode-go was
healthy the whole time.

A quarantine that counted those five would have:

1. taken a healthy provider out of service, then
2. sent the retry to the fallback, which would fail identically — the poisoned
   header travels with the request — and quarantined that too, until
3. every provider for the model was quarantined and the model was unservable,
4. all from one client with an unfashionable User-Agent.

**The lesson is the design constraint.** A failure only counts if it is
attributable to the provider. A failure caused by the request itself will
reproduce on every provider, so quarantining on it converts one bad client into
an estate-wide outage — the exact opposite of what a circuit breaker is for.

This is not hypothetical: it is what would have happened this morning.

## 3. Attribution

Classification is **pure** (`classify_failure` in `control.py`): status,
response headers, and the first bytes of the body in, a verdict out.

| Outcome | Verdict | Why |
|---|---|---|
| 2xx / 3xx | `NONE` | success — resets the counter |
| transport error (connect, TLS, timeout) | `PROVIDER` | we could not reach them |
| 5xx | `PROVIDER` | they broke |
| 401 / 403 with a JSON body | `PROVIDER` | **our** credential was rejected |
| 401 / 403 with CDN edge markers and a non-JSON body | `CALLER` | an edge blocked the caller's request; the next provider's edge will too |
| 400 / 404 / 422 and other 4xx | `CALLER` | malformed or unsupported request; reproduces everywhere |
| 429 | `NONE` | quota, not fault — the breaker, reroute and quota routing already own this, and a busy provider must not become a quarantined one |

The CDN discriminator reuses `_CDN_HEADERS` / `_CDN_SERVERS`, already in
`proxy.py` for classifying 429s. A Cloudflare block page carries `cf-ray` and
`server: cloudflare` and is HTML; a real API 403 is JSON. That is the difference
between "this vendor rejected our key" and "this vendor's edge disliked the
caller", and it is cheap to read from bytes already in hand.

**Deliberately conservative.** When attribution is ambiguous the answer is
`CALLER`, because a missed quarantine costs some failed requests while a false
quarantine costs a working provider.

## 4. Behaviour

- The counter is **consecutive** per (provider, model). Any success resets it
  to zero. Five scattered failures across a day are not the same event as five
  in a row, and only the latter means "this is broken now".
- At the threshold the pair is quarantined with: the count, first and last
  failure timestamps, the last status, and a short body excerpt — everything a
  human needs to decide, without going to the logs.
- A quarantined pair is dropped from the candidate set **for that model only**.
  The provider keeps serving every other model it is mapped to: a model map
  entry pointing at a model the vendor retired should not cost the vendor.
- If every provider for a model is quarantined, requests for it fail with an
  explicit error naming the pairs and how to clear them — not a bare 503 that
  looks like exhaustion.
- Quarantine **persists** to the config store. A pod restart must not silently
  un-quarantine something a human has not looked at; that would turn a standing
  alarm into a five-minute one.

## 5. Work items

### WI-1 — Pure attribution (`classify_failure`)

In `control.py`, no I/O. Fully table-testable, including the exact Cloudflare
1010 page that prompted this plan.

**Done when:** the real block page classifies as `CALLER` and a JSON API 403
classifies as `PROVIDER`.

### WI-2 — `QuarantineTracker`

Consecutive-failure counting keyed on (provider, model), threshold config
(`[routing] quarantine_threshold`, default 5, 0 disables), entry metadata,
clear operations, persistence through the config store.

**Done when:** five provider-attributable failures quarantine a pair, one
success at any point resets, and the state survives a restart.

### WI-3 — Candidate filtering + honest errors

Drop quarantined pairs at model-map filtering time. When that empties the
candidate set, return an error that names the quarantined pairs.

**Done when:** a quarantined pair takes no traffic for that model, still takes
traffic for others, and an all-quarantined model says so.

### WI-4 — Admin surface

`GET /admin/quarantine`, `DELETE /admin/quarantine/<provider>/<model>`,
`/status.json` section, dashboard panel with a Release button.

**Done when:** an operator can see what is quarantined and why, and release it,
without a restart or a shell.

**Status: API done, dashboard panel open.** The endpoints, the `/status.json`
section and the operator runbook (§12) landed. The dashboard panel with a
Release button did not — releasing still needs a `curl`, which fails the
"without a shell" half of the bar above.

### WI-4a — `quarantine_threshold` reaches every surface

The knob was validated, mutable and persisted, but missing from `/status.json`
and `GET /admin/config` (both hand-listed their fields), coerced to a float on
both runtime paths despite being declared `int`, and never pushed to the live
tracker — so a `PUT` was accepted, persisted, and inert until the next restart
replayed the overlay. One shared `routing_config_payload()` now feeds all three
reporting surfaces, `coerce_routing_value()` types values from the same bounds
table that validated them, and `update_routing_config` pushes the threshold
into the tracker.

**Done:** `test_config_surfaces` fails if a mutable knob misses a surface or
changes type, and `test_quarantine_e2e` drives the real `PUT` and then counts
real failures against the new threshold.

### WI-5 — Live validation — **DONE 2026-08-09**

Executed against the deployed pod (`978bc300`) once PR #12 gave it an admin
token. A throwaway provider pointed at the pod-local discard port
(`http://127.0.0.1:9/v1`) made every attempt a connect refusal — a real
transport error, not a simulated one — reached through a dedicated route key so
the probe could not touch the default route or real traffic.

| Step | Result |
|---|---|
| requests 1–4 | `502`, counter `wi5-probe/wi5-probe-model` → 1,2,3,4; no entry |
| request 5 | quarantined at exactly 5; `ERROR ... QUARANTINED` names the release command |
| request 6 | `503` with `reason: quarantined`, the pair listed, and `release_with` |
| `DELETE /admin/quarantine/…` | `200`; entry **and** counter cleared |
| request 7 | back to `502` — the pair takes traffic again |
| second `DELETE` | `404`, not a silent `200` |
| **pod restart while quarantined** | entry survived a full pod replacement with original timestamps; counters correctly did not (in-memory by design) |

Teardown verified against a pre-run snapshot: providers, route table, model map
and quarantine all byte-identical to baseline.

**Two findings came out of the run, neither visible from the tests.**

1. **The entry carries no evidence for a transport failure** — `last_status:
   null`, `last_detail: ""`. `_forward` catches `httpx.RequestError` itself
   (`proxy.py:2191`) and never re-raises, so `forward_failed` is False and the
   `detail = "forward failed"` branch at `proxy.py:1457` is dead for the whole
   transport-failure class. §4's "everything a human needs to decide, without
   going to the logs" is unmet for the first row of §3's own table. Attribution
   is unaffected. **WI-007.**
2. Confirmed as suspected: quarantine records only when `request_model` is
   non-None, and that is extracted **only when a model map is configured**
   (`proxy.py:1146`). An instance with no model map quarantines nothing at all.
   Not hit here (the deployed instance has a map), still undocumented.

## 6. Deliberate non-goals

- **No auto-release.** The explicit ask is that it waits for a human. A timer
  would recreate the flapping this exists to stop.
- **No half-open probing.** That is the breaker's job (Plan 008) and it works
  on a different signal — transient overload, not "someone should look at this".
- **No quarantine on 429.** Quota exhaustion is normal operation.
- **No cross-provider correlation.** Detecting "this failed on every provider,
  so it is the request" would be a stronger discriminator than the CDN
  heuristic, but it needs per-request state across attempts. Revisit if the
  heuristic proves too coarse.
