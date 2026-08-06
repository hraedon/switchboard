# Plan 017 — Production deployment and OSS-provider migration

Status: proposed execution plan (authored 2026-08-06)

Depends on: nothing new. Every routing capability this plan deploys is
already implemented and tested.

## 1. Problem

switchboard works and is not used. Delegated agents still point directly at
OSS providers, so when a provider runs out of quota the agent gets an error or
retries the same exhausted upstream until something times out — the failure
this project exists to remove.

The gap is **not** algorithmic. An audit on 2026-08-06 found the routing core
complete: categorical routing (Plan 006), production contracts and capability
filtering (008), the low-interactivity signal and model remap (010),
usage-aware failover (011), token budget (012), trailing-window usage (013),
failback hysteresis (014), headroom ranking (015), opportunistic quota burn
(016), reactive usage-error reroute and per-provider credentials (this month).
`/healthz` and `/readyz` exist. Retry-After already drives a per-provider
cooldown.

What is missing is everything between "the code is right" and "the traffic
flows through it": a container, manifests, a deployment, a client migration,
and the operational evidence that it behaves under real exhaustion.

**Several plan files still say `Status: proposed` for work that shipped.**
That is a trap for anyone (human or agent) planning from this directory, and
WI-2 fixes it.

## 2. Scope

**In scope.** OSS/free providers only — opencode-go (zen), ollama-cloud, zai,
longcat. Deployment to the existing k8s cluster. Migration of opencode's
provider configuration. Operational verification.

**Out of scope, deliberately.** Claude traffic and the `openai/gpt-5.6-x`
lineage do not pass through switchboard (operator decision, 2026-08-06).
Keeping them out bounds the blast radius to exactly the traffic that hangs
today, and means every route switchboard serves is a cross-vendor one.

**Non-goal: mid-stream recovery.** Reroute is only safe before the first byte
reaches the client; once a response has started, a retry would splice two
upstream responses together. A provider that fails *during* a stream surfaces
that failure to the client. Recovering from it would require reading response
bodies in flight, which the inert-in-path rule forbids and which is not worth
the exception. Document the limit; do not engineer around it.

## 3. How to execute this plan

The work items below are written to be handed to autonomous implementers one
at a time. Waves are dependency tiers: everything inside a wave may run in
parallel, and a wave starts only when the previous one is merged.

**Every implementer, every work item, without exception:**

1. Read `AGENTS.md` first. Its hard rules — inert in-path, cache-transparency,
   streaming is sacred, fail safe, stdlib-only pure core, no work-domain
   identifiers in committed files — outrank anything written here. If a work
   item appears to require breaking one, stop and say so rather than
   proceeding; that is a finding, not an obstacle.
2. sluice must be checked out as a **sibling directory** (`../sluice`). The
   public PyPI `sluice` is an unrelated package and must never be installed.
   **Related supply-chain trap, verified the hard way:** `switchboard` ALSO
   has a public namesake (1.6.9, unrelated), which outranks our 0.1.0. Any
   install that resolves either name against an index can silently fetch a
   stranger's package — `--find-links` supplements the index, it does not
   replace it. Install first-party wheels by explicit file path and verify
   the installed version, never the build's exit code.
3. **Tell every implementer that reads outside the repo are auto-rejected,
   and that the rejection is not fatal.** This is the single largest cause of
   wasted delegations so far: three of five runs died after an agent tried to
   grep `../sluice`, hit the sandbox, and gave up — each having done real
   work first. One sentence in the prompt ("you can only read this repo; if
   you think you need sluice internals, state the assumption and continue")
   turned a failing run into a passing one. Tasks that GENUINELY need the
   sibling checkout (the container build) are not delegable under this
   sandbox and should be done directly.
4. Verify with all three, and paste the output in the handoff:
   ```
   uv run --extra dev --with pip pytest -q     # --with pip: uv venvs lack it,
   uv run --extra dev ruff check src/ tests/   #   and test_wheel_install shells
   uv run --extra dev mypy src/switchboard/    #   out to pip
   ```
5. Mock upstreams in tests must return
   `httpx.Response(status, stream=_Stream(body))` where `_Stream` subclasses
   `httpx.AsyncByteStream`. `Response(content=...)` arrives pre-consumed, so
   the proxy's `aiter_raw()` loop raises `StreamConsumed` — and because the
   status line is sent first, **status-only assertions still pass while the
   body path silently fails**. This has already cost one round of debugging.
6. One work item per branch, named `wi-017-<n>-<slug>`. Do not batch.
7. **Give reviews room.** `openai/gpt-5.6-sol` reads deeply on this codebase:
   two review runs at a 1500s ceiling died mid-analysis having produced
   nothing, which is worse than a narrow review that finishes. Allow ~2700s,
   scope each review to a single commit or work item, and tell the reviewer
   explicitly to budget its reading rather than survey the repo.
8. **Confirm the model answers before delegating to it.** On 2026-08-06 two
   work items were dispatched to `zai/glm-5.2`; both produced zero output
   tokens, and opencode retried the upstream **indefinitely** until an
   external timeout killed them — 25 minutes each, no work, no error surfaced
   to the caller. The provider was returning `Insufficient balance or no
   resource package`. Send a one-line probe to a model before handing it a
   work item, and note that a delegated run that produces nothing looks
   identical to one that is merely slow.

   The eventual diagnosis is worth the retelling, because the obvious answer
   was wrong: the API key was correct. The provider id `zai` resolves to
   z.ai's *general* endpoint, which had no balance, while the identical key
   against the *coding-plan* endpoint answered normally. Use
   `zai-coding-plan/*`. An "invalid credential" hypothesis would have sent
   someone rotating a perfectly good key.

   Two lessons worth carrying beyond this plan. First, this is precisely the
   failure switchboard exists to remove: a 402-class exhaustion response that
   a client turns into an infinite retry. Second, the usage-dashboard
   reported that same provider as entirely healthy (`throttle=none`,
   `alert=none`) throughout, because it tracks quota consumption and not
   account balance — evidence that the **reactive** signal (what the provider
   actually answered) deserves more trust than the proactive one.
9. **Redirect stdin when backgrounding a delegated run: `< /dev/null`.**
   Without it, a backgrounded `opencode run` can hang before it ever creates
   a session — no tokens, no log line, no error, indistinguishable from slow
   work. Three runs burned 25–45 minutes each this way. The identical command
   in the foreground started reviewing instantly, which is the cheapest way to
   tell a hung launch from a genuinely slow model.

   Corollary for anyone automating this: **process liveness is not progress.**
   The reliable check is the session's token count in
   `~/.local/share/opencode/opencode.db` (`SELECT title, tokens_output FROM
   session ...`). A delegated run with zero output tokens after a minute has
   not started, whatever `ps` says.
10. **Cross-lineage review before merge.** An implementation authored by
   `zai/glm-5.2` must be reviewed by a different lineage (`openai/gpt-5.6-sol`
   or `opencode-go/deepseek-v4-flash`), per the estate's review gate. Reviewers
   cannot grep outside the repo, so if a review needs sluice internals, paste
   the relevant source into the prompt.

## 4. Wave 0 — clear the review debt (blocking)

### WI-0 — Adversarially review the unreviewed reroute commits

Two commits reached `main` without a completed review: `1402ff8` (five safety
fixes) and `6f6591b` (per-provider credentials). The cycle-1 review of the
original feature found five genuine blocking issues, so the assumption that a
second cut is clean is exactly the assumption worth testing.

- Review `git show 1402ff8` and `git show 6f6591b` against `AGENTS.md`.
- Pay particular attention to: the permit release added in the
  `_acquire_with_disconnect` finally block (double-release?), the affinity
  write after a reroute (metric double-counting? races?), queue-candidate
  inclusion in `alternatives` (unbounded wait?), and credential replacement
  (can a client-supplied header ever survive to the wrong upstream?).
- Fix every blocking finding on a branch, re-verify, merge.

**Done when:** a review returns pass, or its findings are fixed and a
follow-up review passes.

## 5. Wave 1 — package and tidy (parallel)

### WI-1 — Container image

No `Dockerfile` exists. Build a multi-stage image; the interesting constraint
is the sluice sibling dependency, which is a path source and therefore not
resolvable from inside a naive build context.

- Multi-stage: build a wheel for sluice and for switchboard, install both into
  a slim runtime layer. Do not ship the build toolchain.
- Run as a non-root user. No secrets baked in — the image must be publishable.
- `ENTRYPOINT` runs `switchboard serve`; config path and listen address come
  from flags/env at runtime.
- Add `docs/deployment.md` documenting the build (including the required
  build context, since sluice lives outside the repo root).

**Done when:** `docker build` succeeds from a documented context, the
container starts against `examples/agent-delegation.toml`, and `/healthz`
answers 200 from outside the container.

### WI-2 — Reconcile plan statuses with reality

`006`, `007`, `008` and `010` say `Status: proposed` but their features are in
the code and under test. Anyone planning from this directory is misled.

- For each, verify against the code, then update the status line to
  `implemented` **with the evidence** (symbol or test names — not a bare
  assertion). Where a plan shipped only partly, say precisely which parts.
  Plan 010 in particular: Feature 0 (low-interactivity surfacing), Feature B
  (model remap) and the reactive backstop all landed; record that.
- Plan `009` is a north-star charter, not an execution plan. Leave it, but
  mark it clearly so it is never mistaken for pending work.

**Done when:** every file in `plans/` states its true status and no reader has
to consult the source to learn it.

### WI-3 — `listen` config key is ignored

Starting with a config whose `listen = "127.0.0.1:8900"` produced a server on
`127.0.0.1:8801`, the CLI default. Either the key is unwired or precedence is
inverted; a deployment that trusts the config file would bind the wrong port.

- Find the precedence bug. Correct order: explicit CLI flag > config file >
  default.
- Add a test per branch of that precedence.

**Done when:** a config-only `listen` is honoured, a CLI flag still overrides
it, and tests pin both.

### WI-3b — Validate config values as strictly as CLI flags

Follow-up from WI-3's review. Now that TOML values reach code that
previously only ever saw argparse-validated input, three gaps opened —
none blocking, all the same shape: a config typo degrades quietly instead
of failing loudly, which is the failure mode this deployment can least
afford once the config lives in a ConfigMap nobody reads.

- `log_level` from config bypasses argparse's `choices`, so `"WARN"`
  reaches uvicorn and fails with a uvicorn-flavoured error rather than a
  clean `_ConfigError`. `_validate_config_pre_build` is the natural home.
- `_resolve_float` silently substitutes the default for unparseable input,
  so `queue_timeout = "abc"` becomes 30.0 with no signal. Distinguish
  "absent" from "present but invalid".
- `float(True)` is `1.0`, so `queue_timeout = true` becomes a one-second
  timeout. Prefer explicit type checks over `try: float()`.

**Done when:** an invalid value for any serve key fails startup with a
switchboard error naming the key, under test.

### WI-4 — Make "the estate is exhausted" observable

When every eligible provider returns a usage error, switchboard surfaces the
upstream's status — correct, but operationally silent. This is the single
condition an operator most needs to see, because it is the one switchboard
cannot route around.

- Add a counter for give-up events (all candidates exhausted), exported
  alongside `switchboard_usage_reroutes_total` and surfaced in
  `/status.json`.
- Log it at WARNING with the providers tried and their statuses.
- Do not read response bodies to do this.

**Done when:** a request that exhausts every provider increments a distinct
counter and logs one actionable line, with a test.

## 6. Wave 2 — deploy (depends on WI-1)

### WI-5 — Kubernetes manifests

Model them on the `usage-dashboard` deployment already in the cluster
(namespace `usage-dashboard`, image `ghcr.io/hraedon/usage-dashboard-server:main`,
internal ingress via `traefik-internal`) — matching an established pattern
beats inventing a second one.

- Namespace, Deployment, Service, ConfigMap (the switchboard TOML), Secret
  (provider API keys, referenced by the `api_key_env` names the config
  declares), and an **internal-only** ingress. switchboard must not be
  reachable from outside the LAN: it holds every provider credential and, by
  design, will spend them on behalf of whoever asks.
- Liveness probe → `/healthz`; readiness probe → `/readyz`. Note that
  `/readyz` is 503 until every provider's first poll completes, which is the
  correct readiness semantic — do not weaken it to make rollout faster.
- `replicas: 1` initially. Affinity, route-table state and gate accounting are
  per-process; horizontal scaling needs a design pass this plan does not fund.
  State that in the manifest comments so nobody scales it casually.
- Resource requests/limits sized for a proxy that streams but does not buffer
  responses.

**Done when:** `kubectl apply` brings up a healthy pod, `/readyz` goes 200,
and a request through the Service reaches a real provider.

### WI-6 — Image build in CI

- Check whether `.github/workflows/` exists in this repo and follow its
  conventions if so.
- Build and push to `ghcr.io/hraedon/switchboard` on merge to `main`, tagged
  with both the commit SHA and `main`. The build needs the sluice sibling —
  check it out in the workflow.

**Done when:** a merge to `main` produces a pullable image and the manifests
reference it by SHA.

## 7. Wave 3 — migrate the traffic (depends on Wave 2)

### WI-7 — Deploy and prove it under real exhaustion

- Deploy to the cluster.
- Drive a real request through switchboard to an OSS provider and confirm a
  200 and a `forwarded_per_provider` increment.
- Force a genuine reroute: point one provider at a deliberately exhausted or
  invalid upstream and confirm the client still gets a 200, that
  `usage_reroutes_total` increments, and that the fallback received **its own**
  credential (this is the failure mode that silently 401s if credentials are
  misconfigured — verify it explicitly rather than assuming).

**Done when:** the reroute is demonstrated end to end against the deployed
instance, with the metric output pasted into the handoff.

### WI-8 — Migrate opencode to route through switchboard

Per-model provider entries, letting switchboard own upstream selection: the
model name becomes the routing key, which is what the `[model]` bridge already
expects.

- In `~/.config/opencode/opencode.jsonc` (mvmcc03) and the mvmcc02 equivalent,
  add a `switchboard` provider whose `options.baseURL` points at the deployed
  Service, with entries per routed model. The `macstudio` provider is the
  existing example of a custom `baseURL`.
- **Migrate one model first** (`deepseek-v4-flash`), confirm a delegated
  `/oc-review` run completes through it, then widen.
- Leave Claude and `openai/gpt-5.6-x` pointing directly at their vendors.
- Keep the direct-provider entries in place during migration so a failure is a
  one-line rollback.

**Done when:** at least one real delegated review runs end to end through
switchboard, and the rollback path is documented.

### WI-9 — Alerting

- Alert on: the exhausted-estate counter from WI-4 rising; `/readyz` failing;
  reroute rate spiking (a provider quietly dying); and pod restarts.
- Route them wherever the estate's existing alerts go — do not invent a new
  channel.

**Done when:** each alert has fired at least once in a deliberate test.

## 8. Wave 4 — harden (may run alongside Wave 3)

### WI-10 — Soak and chaos harness

A script that stands up fake upstreams and drives switchboard through the
failure modes that matter, so a regression is caught by running one command:
all-providers-exhausted, a provider that 429s then recovers, a provider that
disconnects mid-stream, a client that disconnects mid-request, and sustained
concurrent load against the gates.

Assert the properties, not the timings: no hung requests, no leaked gate
permits (`held == 0` everywhere at the end), no duplicated responses.

**Done when:** one command exercises all five and reports pass/fail.

### WI-12 — Model-map management in the admin API and dashboard

The dashboard (Plan 005) already does Providers, Route Table, Threshold
Estimator and Routing Metrics. The **model map does not appear at all** — it
is TOML-only, with no admin endpoint and no UI. That is the one piece of
configuration an operator changes most often once routing is live, because
every new model or provider needs its aliases, and it is the fiddliest to
hand-edit: a nested `[model."name"]` table per model, one key per provider,
where a typo does not error — it silently makes that provider ineligible for
that model, which surfaces later as "failover mysteriously didn't happen".

- Admin endpoints for reading and editing the map, following the route-table
  CRUD already in `admin.py` (same auth, same CSRF, same persistence shape).
  Do not invent a second pattern.
- A dashboard section listing each model with its per-provider aliases,
  showing clearly which providers can serve it — the question an operator
  actually asks is "who can serve this model", not "what is this alias".
- Validation on write: reject an alias for a provider that is not
  configured, and surface unmapped-but-referenced models rather than
  silently dropping them.
- Persist consistently with the route table (`sqlite_path` when set, config
  otherwise), so a restart does not resurrect stale mappings.

**Done when:** an operator can add a model, give it per-provider aliases,
and see which providers can serve it, without editing TOML — and a bad alias
is refused at write time with a message naming the offending provider.

### WI-12b — Follow-ups from the model-map review

Non-blocking findings from WI-12's cross-lineage review, kept here rather
than left in a task output nobody reads. The first two are the ones that
matter; the rest are noted because they are pattern-consistent with existing
warts, which is a reason to fix the pattern once, not to ignore it.

- **Partial write with no rollback** (`model_map.py`, `admin.py`): a
  persistence failure can leave the in-memory map and the stored map
  disagreeing. Since the whole point of the feature is that operators trust
  the UI over the file, a silent divergence is the worst outcome.
- **SQLite rows bypass the provider-name validation config rows get**: a
  persisted alias naming a provider that no longer exists is accepted at
  load, reintroducing the silent-ineligibility failure the feature exists to
  prevent.
- Startup validation of the `[model]` section is untested; proxy-level
  dispatch of the new endpoints is untested (note two admin paths currently
  fall through to `_proxy_request` and get proxied upstream — pre-existing
  for `/admin/routes/<key>` too, but now doubled).
- Dashboard interpolates model/provider/alias into `innerHTML` unescaped.
  Admin-authored content only, and consistent with the rest of the
  dashboard, so self-XSS at worst — but a shared escape helper is cheap.
- `_load_from_db` dies on one corrupt row, taking the server down at
  startup; a stale SQLite handle survives shutdown into the drain window.
  Both mirror `RouteTableManager` exactly, so fix the shared pattern.

**Done when:** a write that fails to persist does not leave a diverged
in-memory map, and a persisted alias naming an unconfigured provider is
rejected at load with the provider named.

### WI-11 — Operator runbook

`docs/runbook.md`: how to read `/status.json`, what each throttle state means,
what to do when the estate is exhausted, how to add or remove a provider, how
to rotate a provider credential, and how to roll back the opencode migration.
Write it for someone who did not build this.

## 9. Definition of done

- Every `plans/` status is accurate.
- switchboard runs in k8s, internal-only, with credentials in a Secret.
- At least one real delegated review routes through it, and a forced reroute
  is demonstrated against the deployed instance with metrics.
- Exhausted-estate is observable and alertable.
- The chaos harness passes.
- A runbook exists.

## 10. Deliberate non-goals

- Horizontal scaling (per-process state; needs its own design).
- Mid-stream recovery (see §2).
- Routing Claude or `gpt-5.6-x` traffic.
- Scraping opencode-go usage. The reactive reroute already handles exhaustion
  from the provider's own response, which is why it is provider-neutral.
  Scraping would add only *proactive* avoidance and depends on hard-coded
  SolidStart server-function IDs that break whenever opencode.ai redeploys.
  Revisit only if reactive proves insufficient in practice.
