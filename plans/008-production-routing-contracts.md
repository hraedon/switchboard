# Plan 008 — Production routing contracts and deployment shape

Status: **partially implemented** — capability contract, capability
filtering, stickiness/failback and the failure/replay boundary shipped;
identity-HMAC, explicit modes, versioned config, singleton lifecycle,
credential-broker protocol and admin-plane isolation did not

Landed:

- §4 capability contract (WI-008.3): `ProviderCapabilities` with
  `surfaces`/`api_family`/`streaming`/`tool_calling_profile`/`context_class`/
  `credential_domain`/`cache_domain` in `src/switchboard/control.py`.
  `route_decision` filters incompatible candidates before pressure/admission
  ranking via `_satisfies_capabilities` against
  `RouteEntry.required_capabilities` (decision reason `capability_filtered`).
  Body-free — capabilities are never inferred from request bodies. Tests
  `test_capability_filter_*` in `tests/test_control.py`.
- §5 stickiness/failback (WI-008.4): `RouteAffinity` + `dwell_interval` +
  affinity_dwell / affinity_hysteresis in `route_decision`; bounded, ephemeral
  (per-process memory, matching §9). Tests `test_affinity_*` in
  `tests/test_control.py`.
- §6 failure/replay boundary: enforced by the usage-error reroute's pure
  predicate `should_reroute` (`src/switchboard/control.py`) — never after
  `response_started`, never when `body_replayable` is false (upload started /
  streamed body consumed).
- Per-provider egress credentials (partial §2.2 direction): each upstream
  receives its own stored credential via `api_key_env` (commit 6f6591b);
  `test_each_provider_receives_its_own_credential` in
  `tests/test_usage_reroute.py`.

Not landed:

- WI-008.1 versioned config schema + `switchboard config validate` subcommand:
  only inline serve-time validation (`_validate_config`,
  `src/switchboard/cli.py`); no versioned schema, migrations, or standalone
  validator.
- §2 explicit operating modes and §2.3 mode gate: no transparent/broker mode
  selection or startup mode validation.
- §3 route identifiers as HMAC-SHA-256 with a rotatable route-index key:
  `hash_route_key` (`src/switchboard/control.py`) is plain unkeyed SHA-256.
- WI-008.6 singleton/fenced production lifecycle: no leadership lease,
  leadership-aware readiness, or safe drain.
- WI-008.7 credential-broker mode: no secret-provider protocol, rotation,
  egress header allowlists, or redaction tests.
- WI-008.8 admin-plane isolation: the admin plane runs on the same listener
  behind `--admin-token`; there is no separate admin binding or Unix socket.

Depends on: Plan 006; may proceed alongside later stages of Plan 007

Blocks: heterogeneous provider rollout, credential brokerage, and horizontal scale

## 1. Goal

Define and implement the contracts that turn a correct multi-gate proxy into a
safe production router: identity, credentials, provider capabilities,
stickiness, failure boundaries, configuration ownership, readiness, and
deployment topology.

The principal deliverable is not a larger dashboard. It is a system in which
an operator can explain, before deployment, why a request is eligible for a
provider, which credential will be used, when failover is safe, and which
component owns the relevant state.

## 2. Supported operating modes

Make modes explicit rather than allowing configuration to imply them.

### 2.1 Transparent routing mode

- Client authorization is forwarded unchanged except for documented internal
  credential stripping.
- A route may contain only providers known to accept the same credential or to
  ignore it.
- Switchboard stores no upstream API credentials.
- This mode preserves the strongest header-transparency claim.

### 2.2 Credential-broker mode

- Client identity/routing authorization is distinct from upstream provider
  authorization.
- Provider-specific credentials come from a secrets provider and are inserted
  only at egress.
- The route key and upstream credential are never the same object.
- Bodies remain byte-identical; the documentation narrows transparency to a
  precise allowlist of header transformations.
- Broker mode is opt-in and cannot be enabled accidentally by supplying a
  credential field in an otherwise transparent configuration.

### 2.3 Mode gate

Startup rejects routes whose providers cannot satisfy the selected mode. The
status API reports the active mode without exposing credentials.

## 3. Identity and route-key contract

Separate four concepts:

1. **Authentication identity:** who may use switchboard.
2. **Route identity:** which configured route applies.
3. **Workload class:** optional trusted admission/QoS metadata.
4. **Upstream authorization:** credential used by the selected provider.

### Requirements

- Define precedence among mTLS identity, bearer credential, `x-api-key`, and a
  trusted routing header.
- Honor routing headers only from configured trusted proxies or authenticated
  clients with that capability.
- Strip all internal identity and routing headers before upstream egress.
- Store route identifiers as HMAC-SHA-256 using a rotatable route-index key,
  not an unkeyed digest of potentially low-entropy identifiers.
- Support key rotation with a bounded dual-read migration window.
- Never expose full route digests in routine dashboard or metric output.

## 4. Provider capability contract

Add declarative provider metadata without inspecting request bodies:

```python
@dataclass(frozen=True)
class ProviderCapabilities:
    surfaces: frozenset[str]             # chat-completions, messages, embeddings
    api_family: str                      # exact wire contract identifier
    streaming: bool
    tool_calling_profile: str | None
    context_class: str | None
    credential_domain: str
    cache_domain: str
```

Routes declare required capabilities or reference a named capability profile.
The router filters incompatible candidates before pressure/admission ranking.

Do not parse `model` from the body. If model-specific routing becomes
necessary, require the client to select a declared route/capability profile or
place model choice in a trusted metadata surface. Body translation and
semantic inspection remain out of scope.

Startup validation rejects a route whose primary or fallbacks cannot satisfy
its declared surface and mode.

## 5. Stickiness and failback

Provider-local caches and rate windows make stateless per-request oscillation
expensive. Add bounded route state supplied explicitly to the pure core:

```python
@dataclass(frozen=True)
class RouteAffinity:
    provider: str
    selected_at: float
    failover_reason: str
    healthy_observations: int
```

Default behavior:

- Prefer the configured primary while it is immediately admissible.
- After failover, remain on the fallback for a minimum dwell interval unless
  it becomes ineligible.
- Fail back only after a configured number of fresh healthy observations.
- Bound affinity entries by configured routes or TTL/LRU for dynamic routes.
- Persist affinity only if operational evidence shows restart flapping is a
  material problem; route correctness must not depend on persistence.

Expose cache-domain changes as a metric so operators can see the cost of
failover.

## 6. Failure and replay boundary

Document and enforce the point after which automatic retry is forbidden.

| Failure point | Alternate provider allowed? | Rationale |
|---|---:|---|
| Before permit acquisition | Yes | No upstream effect |
| Permit acquired, before request started | Yes, after release | No upstream effect |
| Connect failure before body iterator is consumed | Only if transport proves zero bytes sent | Narrow safe case |
| Request body upload started | No | Replay may duplicate work; body is not buffered |
| Response headers received | No | Upstream accepted request |
| Response body streaming | No | Partial response already visible |

The streaming substrate returns enough typed state to enforce this table.
Do not add transparent body buffering merely to enable retries.

## 7. Pressure and policy model

Plan 006 removes unsafe cross-unit comparison. This plan adds a principled,
extensible policy:

- Eligibility and immediate permits remain authoritative.
- Provider-local pressure signals are normalized into named bands only when a
  provider adapter has a documented mapping.
- Unknown remains a separate state, never a numeric zero.
- Route policy chooses among `primary-first`, `lowest-wait`, and
  `fresh-capacity-first`; default is `primary-first`.
- Cost, geography, or carbon signals are not silently folded into pressure.
  Future policy inputs require explicit configuration and reason reporting.

Every decision emits a bounded reason code such as `primary_available`,
`primary_busy`, `primary_closed`, `affinity_dwell`, or `capability_filtered`.

## 8. Deployment topology

### 8.1 Supported v1 topology: singleton data plane

- One active switchboard instance per provider-account concurrency boundary.
- A singleton lease prevents accidental second active instances.
- Standby instances may be ready to acquire the lease but do not admit traffic.
- Readiness requires leadership plus at least one viable configured primary;
  degraded fallbacks do not make the whole service unready.
- Shutdown relinquishes leadership only after admission stops and in-flight
  requests drain or reach the documented deadline.

### 8.2 Future horizontal topology

Do not claim active/active support until one of these designs is implemented:

- deterministic route/provider partitioning with one active owner per
  concurrency boundary;
- a distributed linearizable permit service with lease expiry and fencing;
- provider-enforced limits strong enough that local gates are only advisory,
  accompanied by a different safety claim.

Shared SQLite, Redis counters without leases/fencing, and independent local
gates do not qualify.

## 9. Configuration and state ownership

Adopt a versioned schema with one owner for each kind of state:

| State | Source of truth | Runtime mutability |
|---|---|---|
| Provider endpoints/capabilities | Config file | Restart required initially |
| Secret references | Config file; values in secret store | Rotation without route rewrite |
| Default/file routes | Config file | Reloadable with validation |
| Operator-created routes | Route store | CRUD with audit record |
| Routing policy defaults | Config file | Safe fields reloadable |
| Affinity | Bounded memory initially | Ephemeral |
| Metrics/events | Metrics/log sink | Append/aggregate only |

Define deterministic merge precedence. Persisted runtime routes must not be
silently overwritten by a file reload. Every mutation records actor, time,
old digest, new digest, and outcome without recording raw secrets.

Add schema versioning and forward-only database migrations with backup before
migration.

## 10. Administrative and observability boundary

- Support a separate admin listener or Unix socket; production guidance makes
  it the default.
- Keep health endpoints minimal and credential-free.
- Gate detailed status, metrics, configuration, and mutations independently.
- Add bounded audit events for login, route changes, reload, leadership, and
  credential rotation.
- Escape all dashboard-rendered configuration values; avoid raw `innerHTML`
  construction for mutable strings.
- Bound body sizes, route counts, entry sizes, metric dimensions, and recent
  event buffers.
- Report degraded providers and unusable routes without exposing endpoint
  credentials or route identifiers.

## 11. Work packages

### WI-008.1 — Versioned config and route schema

Implement schema validation, migrations, merge precedence, and failure-atomic
reload. Add `switchboard config validate` for pre-deployment checks.

### WI-008.2 — Explicit transparent mode

Ship and qualify transparent mode first. Validate credential-domain and
capability compatibility for every route.

### WI-008.3 — Capability filtering

Add capability profiles, route requirements, startup validation, and decision
reason reporting without body inspection.

### WI-008.4 — Stickiness and failback

Add bounded affinity state, dwell/recovery policy, deterministic tests, and
cache-domain transition metrics.

### WI-008.5 — Failure-boundary enforcement

Consume typed streaming outcomes and prove that no request is replayed after
upload begins.

### WI-008.6 — Singleton production lifecycle

Reuse or generalize sluice's singleton/fencing behavior. Add leadership-aware
readiness, safe drain, and failover drills.

### WI-008.7 — Credential broker, separately gated

After transparent mode is stable, add secret-provider protocol, provider
credential references, egress header allowlists, rotation, redaction tests,
and an explicit security review.

### WI-008.8 — Admin-plane isolation

Add separate binding, hardened UI/API, mutation audit, and deployment examples
that do not expose the admin plane through the public ingress.

## 12. Qualification matrix

At minimum, qualify:

- both supported API surfaces;
- transparent mode with shared and ignored credentials;
- credential broker rotation without process restart;
- compatible and incompatible fallback capabilities;
- failover dwell and controlled failback;
- stale truth sources and partial provider readiness;
- singleton takeover during idle, queued, and streaming traffic;
- config reload success, validation failure, and rollback;
- database migration and restore;
- upstream failures at every replay-boundary stage;
- route-count/cardinality limits and hostile client metadata;
- direct-versus-proxied wire captures.

## 13. Acceptance criteria

- [ ] Operating mode is explicit and startup-validated.
- [ ] Authentication, route identity, workload class, and upstream credentials
      are separate concepts in code and documentation.
- [ ] Capability-incompatible providers are never admission candidates.
- [ ] Failover and failback have bounded, tested stickiness semantics.
- [ ] Automatic alternate-provider attempts stop before unsafe replay.
- [ ] Singleton production topology is enforced, not merely documented.
- [ ] Readiness represents usable routing rather than unanimity of providers.
- [ ] Configuration and persisted route precedence are deterministic.
- [ ] Admin-plane isolation and mutation audit are available.
- [ ] Credential-broker mode, if shipped, passes a dedicated threat review.
- [ ] No body inspection, translation, logging, persistence, or replay is added.
