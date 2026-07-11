# Plan 009 — Switchboard maximal-ambition routing platform

Status: north-star program charter — **not an execution plan**

Scope: long-horizon product and engineering horizon

Depends on: the architectural contracts in Plans 006–008

> **How to read this document.** This is the maximal coherent vision, retained
> so near-term choices compound toward a useful end state. It is a menu with
> architecture, not a backlog and not authorization to implement every item.
> Work begins only through a later numbered execution plan that identifies an
> observed need, owning component, safety contract, operational cost, and
> independently valuable delivery slice. Plans 006–008 remain the actual next
> work. If a later execution plan conflicts with this charter, the execution
> plan wins and this document is amended.

## 1. North star

Build the definitive content-blind, self-hosted traffic control plane for
interactive model APIs:

- as transparent as a direct connection when operating in transparent mode;
- as conservative about concurrency and quota as sluice;
- able to use multiple compatible providers without creating duplicate work;
- explicit about identity, credentials, capabilities, cost, locality, and
  failure policy;
- useful from one workstation and two upstreams through a multi-region fleet;
- observable and auditable without retaining prompts or responses;
- deterministic in every admission, authorization, and routing decision;
- extensible through narrow provider adapters rather than gateway sprawl;
- independently verifiable through state-machine, wire-capture, and chaos
  evidence.

The maximal result is not a general-purpose API gateway and not a semantic
model router. It is a reliability and policy plane that decides **whether and
where a request may begin**, then gets out of the byte path except to stream it
faithfully and cancel it safely.

## 2. Measurable end state

At maturity, an authorized operator can:

1. Register many provider accounts and self-hosted inference pools with typed
   capability, credential, quota, cache, cost, and failure-domain metadata.
2. Define versioned routes and admission policies as code, validate them
   offline, simulate them from redacted metadata, review diffs, and promote the
   same signed artifact through environments.
3. Route compatible requests using fresh capacity while preserving primary
   preference, cache locality, affinity, and deliberate failback behavior.
4. Prove that closed, stale, unauthorized, incompatible, or out-of-budget
   providers cannot be selected.
5. Allocate concurrency fairly across workloads with reservations, borrowing,
   starvation bounds, and emergency policy—all without reading content.
6. Distinguish client identity, route identity, workload class, and upstream
   credentials, rotating each independently.
7. Operate in transparent or credential-broker mode with exact, testable wire
   transformation contracts.
8. Scale from enforced singleton operation to fenced, active/active admission
   across replicas and regions without multiplying provider concurrency.
9. Survive process, node, network, truth-source, database, and region failure
   without leaking permits or replaying an ambiguous request.
10. Explain every decision with a bounded reason code and the exact signed
    policy version that produced it.
11. Observe saturation, queue time, failover, cache-domain movement, quota,
    cost envelopes, breaker behavior, and degraded truth without emitting
    prompt, response, credential, or high-cardinality identity data.
12. Reconstruct configuration and control-plane history from an append-only
    audit trail while acknowledging that request bodies were never retained.
13. Qualify provider adapters against shared conformance suites and wire
    captures before enabling them in production.
14. Give application teams a stable CLI, API, SDK, and local test harness that
    predicts production routing decisions.
15. Export an assurance bundle proving configuration, dependency versions,
    tests, wire invariants, chaos results, and deployment posture.

## 3. Product modes are one codebase, not forks

| Mode | Purpose | Coordination | Credential behavior |
|---|---|---|---|
| Local transparent | Developer/workstation failover | Single process | Client credential passes through |
| Managed singleton | Production account governor | Fenced active/standby | Transparent or brokered |
| Team routing plane | Many routes and workload classes | One active owner per boundary | Secret-provider integration |
| Fleet control plane | Many clusters/accounts | Partitioned or distributed leases | Federated secret references |
| Multi-region edge | Regional admission and failover | Globally fenced budgets plus local leases | Regional egress identities |

Increasing mode changes operational machinery, not routing semantics. A policy
artifact has the same canonical meaning in local validation and production.

## 4. Non-negotiable principles

### 4.1 Admission before effects

Alternate-provider selection is safe only before upstream effects begin. Once
request upload starts, switchboard never guesses that replay is safe.

### 4.2 Content blindness is structural

The data plane never parses, logs, indexes, persists, classifies, embeds, or
translates request or response bodies. Features that require content belong in
a different, explicitly consented product.

### 4.3 Deterministic policy path

Eligibility, authorization, capability matching, ranking, fairness, budget
enforcement, and failback are pure functions over explicit observations and
versioned policy. No model, randomness, network call, or hidden clock appears
in the decision core.

### 4.4 Unknown is not healthy

Missing, stale, contradictory, or unauthenticated observations lower claims
and narrow choices. They never become zero pressure or spare capacity.

### 4.5 Provider limits are not discovered by injury

Use documented endpoints, headers, sanctioned telemetry, and configured
limits. Never probe limits by deliberate oversubscription.

### 4.6 One effect has one admission lease

Every forwarded request has one fenced admission lease, owned through request
completion or cancellation and released exactly once. Process or network
failure cannot create an unbounded local phantom.

### 4.7 Policy explains every route

Every decision identifies policy version, route, selected provider, bounded
reason, freshness class, and admission outcome. Explanation does not require
capturing caller secrets or content.

### 4.8 Modes make tradeoffs explicit

Credential rewriting, distributed coordination, cost-aware ranking, and
emergency overrides are separately enabled capabilities with narrower claims.
They are never accidental consequences of configuration.

### 4.9 Cardinality is a resource limit

Routes, identities, labels, audit events, queues, affinity, metrics, and status
payloads all have configured bounds and overload behavior.

### 4.10 The admin plane is not the data plane

Administrative identity, networking, persistence, and availability are
separable from request forwarding. Loss of the UI does not silently alter
admission policy.

## 5. Permanent exclusions

Even at maximal ambition, switchboard does not become:

- a prompt logger, response archive, conversation store, or observability tap;
- a request/response translator between API families;
- a semantic, topic, safety, or content-based model router;
- a response or prompt cache;
- an autonomous request replay engine after upstream effects may have begun;
- a provider-limit probing or benchmarking system that intentionally exceeds
  sanctioned limits;
- a model training/evaluation platform, agent runtime, workflow engine, or
  general service mesh;
- a billing ledger or substitute for provider invoices;
- a secrets manager, identity provider, or public multi-tenant SaaS control
  plane;
- a claim of identical behavior between providers merely because their JSON
  surface looks similar.

Integrate with mature systems in those categories; do not absorb them.

## 6. Target architecture

```text
                  policy authoring · CLI · UI · GitOps
                                 │
                    ┌────────────▼────────────┐
                    │ Unprivileged control plane
                    │ routes · policy · audit │
                    │ adapter registry · sim  │
                    └──────┬─────────┬────────┘
                           │ signed  │ observations
                           │ policy  │ and evidence
            ┌──────────────▼───┐   ┌─▼────────────────┐
            │ Admission plane  │   │ Telemetry plane  │
            │ leases · budgets │   │ bounded metrics  │
            │ fairness · fence │   │ events · history │
            └──────────┬───────┘   └──────────────────┘
                       │ admission plan / fenced lease
              ┌────────▼──────────────────────────────┐
 clients ────►│ Content-blind regional data plane     │
              │ identify · authorize · stream · cancel│
              └───────┬──────────┬──────────┬─────────┘
                      │          │          │
                ┌─────▼───┐ ┌────▼────┐ ┌──▼──────────┐
                │ provider│ │provider │ │ self-hosted │
                │ account │ │account  │ │ inference   │
                └─────────┘ └─────────┘ └─────────────┘
                      ▲          ▲          ▲
                      └──── sanctioned truth adapters ────┘
```

The control plane may be highly available without sitting in the request body
path. Data planes continue using the last valid signed policy for a bounded
interval and then degrade according to explicit fail-safe policy.

## 7. Program workstreams

### WS-A — Canonical policy and decision model

- Versioned provider, route, identity, capability, fairness, affinity, budget,
  queue, retry-boundary, and emergency-policy schemas.
- Canonical serialization and signed policy artifacts.
- Pure admission-plan engine with stable reason taxonomy.
- Schema and semantic migrations with cross-version fixtures.
- Offline validator, diff, formatter, and deterministic simulator.

### WS-B — Shared flow-control and streaming substrate

- Stable gate, lease, reconciliation, breaker, truth, and streaming APIs shared
  with sluice.
- Exactly-once release and cancellation state machine.
- Backpressure, body-size, idle, disconnect, and shutdown guarantees.
- Direct/sluice/switchboard wire-equivalence fixtures.
- Compatibility matrix across Python, ASGI server, and HTTP client versions.

### WS-C — Provider adapter and capability ecosystem

- Narrow adapter SDK for sanctioned usage truth, header truth, capabilities,
  credential shape, retry hints, and error classification.
- Adapter conformance suite and signed compatibility manifests.
- Provider-account instances separated from provider-type definitions.
- Explicit experimental/quarantined/supported lifecycle.
- No adapter receives body access.

### WS-D — Identity, credentials, and secrets

- mTLS, workload identity, bearer, and trusted-proxy identity adapters.
- Route identity and HMAC index-key rotation.
- Credential broker with secret-reference protocol and short-lived credential
  support where providers allow it.
- Egress credential allowlists, redaction, rotation, and compromise response.
- Separation between data-plane, admin, and provider credentials.

### WS-E — Fairness, reservations, and workload policy

- Hierarchical admission classes with guaranteed reserves and bounded borrowing.
- Weighted fair queueing or deficit scheduling proven under cancellation and
  resize.
- Per-route and per-workload queue budgets and starvation bounds.
- Emergency classes with explicit authorization, expiry, and audit.
- No client-controlled priority without authenticated policy.

### WS-F — Affinity, cache locality, and failover policy

- Route/session affinity without retaining conversation content.
- Cache-domain-aware dwell and controlled failback.
- Failure-domain diversity across account, host, zone, and region.
- Policy-selectable primary-first, capacity-first, and bounded cost-aware
  strategies.
- Honest accounting of cache-domain moves and failover costs.

### WS-G — Quota, budget, and cost envelopes

- Sanctioned request/token/rate/quota signals from provider telemetry and
  response headers only.
- Operator-supplied price tables as versioned estimates, never invoices.
- Hard and soft budget envelopes with freshness-aware enforcement.
- Forecasting from aggregate metadata, with uncertainty labels.
- Deliberate policy ordering among availability, quota, cost, and locality.

### WS-H — Distributed admission and fleet topology

- Fenced singleton and active/standby baseline.
- Partitioned ownership by provider-account concurrency boundary.
- Linearizable distributed leases where active/active is warranted.
- Regional lease delegation with bounded overshoot and reclaim semantics.
- Split-brain, clock-skew, lease-loss, and disaster-recovery drills.
- No active/active claim without evidence that total permits remain bounded.

### WS-I — Configuration, workflow, and audit

- GitOps and API-managed routes with explicit ownership and merge rules.
- Proposal/review/approval/promotion for high-impact policy changes.
- Signed artifacts, compare-and-swap activation, rollback, and expiry.
- Append-only mutation and emergency-action audit with external export.
- Route-store migrations, backup, restore, and reconciliation.

### WS-J — Observability and explanation

- Fixed-cardinality metrics, bounded recent events, traces without bodies, and
  stable reason codes.
- Views for provider health, admission latency, queueing, fairness, failover,
  affinity, quota freshness, and lease topology.
- SLOs for admission latency, false rejection, permit leakage, stale truth,
  and policy convergence.
- Evidence bundles that connect running build and policy digests to tests and
  deployment posture.

### WS-K — Operator, developer, and client experience

- Accessible admin UI with separate operational and policy-authoring views.
- `switchboard config validate`, simulate, diff, explain, doctor, and capture
  commands.
- Local two-provider harness and fault injector.
- Client SDK helpers for trusted route/workload metadata and reason handling.
- Migration guides from direct providers and sluice.

### WS-L — Security, reliability, and assurance

- Threat models for transparent, brokered, distributed, and multi-region modes.
- Fuzzing of ASGI events, headers, config, adapter responses, and persistence.
- Property/model checking of admission leases and routing invariants.
- Chaos program for disconnect, process death, partition, stale truth, database
  failure, and split brain.
- Dependency provenance, reproducible builds, SBOM, signing, and coordinated
  vulnerability response.

## 8. Delivery horizons

### Horizon 0 — Correct single-process router

Plans 006 and 007: truthful state, ordered admission, bounded metrics, stable
shared substrate, clean packaging, and two-provider soak evidence.

### Horizon 1 — Production singleton

Plan 008: explicit modes, capabilities, identity separation, stickiness,
failure boundary, durable routes, hardened admin plane, and fenced lifecycle.

### Horizon 2 — Policy and adapter platform

Canonical signed policies, offline simulation, adapter SDK/conformance,
configuration promotion, secret-provider integration, and richer workload
classes. Remain singleton per provider-account boundary.

### Horizon 3 — Team fairness and governance

Hierarchical reservations, starvation bounds, reviewed mutations, budget
envelopes, audit export, and mature operator/developer tooling.

### Horizon 4 — Partitioned fleet

Many provider accounts and clusters with deterministic ownership, active
standby, policy distribution, fleet status, and disaster recovery. Prefer
partitioning over distributed hot-path consensus.

### Horizon 5 — Distributed and multi-region admission

Only when demonstrated demand warrants the complexity: fenced distributed
leases, regional delegation, bounded failure behavior, and formal/chaos
evidence. This horizon must not weaken the singleton safety claim.

### Horizon 6 — Ecosystem maturity

Stable adapter certification, long-term compatibility, signed policy exchange,
external assurance integrations, and a maintained reference deployment from a
single workstation through a multi-region fleet.

## 9. Dependency spine

```text
truthful snapshots
  → ordered admission and safe failure boundary
    → shared lease/stream substrate
      → explicit identity, credential, and capability contracts
        → signed policy and bounded affinity
          → fenced singleton production
            → fairness, budgets, and governance
              → partitioned fleet
                → distributed/multi-region leases
```

UI breadth, adapter count, and active/active scale never jump ahead of this
spine.

## 10. Testing and evidence program

### 10.1 Test layers

- Pure unit and property tests for every policy invariant.
- Deterministic state-machine tests for leases, cancellation, breaker, affinity,
  and failback.
- Cross-product sluice/switchboard conformance tests.
- ASGI integration tests with real gates and controlled transports.
- Wire captures proving body identity and allowed header transformations.
- Provider-adapter contract fixtures with malformed and stale observations.
- Persistence migration, backup, restore, and corruption tests.
- Soak, overload, fairness, and cardinality tests.
- Chaos tests for every supported topology.
- Security tests for identity confusion, header spoofing, credential leakage,
  SSRF, CSRF, path traversal, and admin-plane exposure.

### 10.2 Reference environments

Maintain reproducible fixtures for:

- two compatible local upstreams;
- one polled-truth and one header-truth provider;
- transparent and brokered credentials;
- boxed, breaker-open, stale, busy, slow, disconnecting, and malformed
  providers;
- singleton takeover and partitioned ownership;
- high-cardinality hostile clients;
- cache-domain and capability mismatch.

### 10.3 Release evidence

Every release produces machine-readable evidence containing source and policy
digests, dependency lock, test results, wire-capture results, migration checks,
known limitations, supported modes, and deployment assumptions.

## 11. Principal risks

| Risk | Consequence | Program response |
|---|---|---|
| Pressure abstraction hides incomparable signals | Unsafe or surprising routes | Eligibility first; typed signal bands; explicit policy |
| Multi-provider credentials weaken transparency | Secret leakage or broken auth | Explicit modes, egress allowlists, threat review |
| Horizontal replicas multiply permits | Provider boxing | Fencing/partitioning before active/active claims |
| Failover duplicates ambiguous work | User-visible duplicate effects | Hard replay boundary; no body buffering |
| Provider compatibility is overstated | Valid request fails after routing | Capability profiles and conformance evidence |
| Route affinity grows without bound | Memory exhaustion | Configured-route cardinality or TTL/LRU |
| Rich metrics leak identity/content | Privacy and cardinality failure | Bounded reason codes and fixed dimensions |
| Shared core becomes a framework monolith | Coupling stalls both products | Small public substrate; product-owned composition |
| Maximal charter becomes an accidental backlog | Premature complexity | Promotion gate and independently valuable plans |

## 12. Product and engineering success measures

### Correctness

- Zero known cases where a closed, stale-preferred, incompatible, or
  unauthorized provider is selected.
- Zero automatic replays after request upload begins.
- Model/property tests cover all admission state transitions.

### Reliability

- No leaked permits across qualified disconnect and failure scenarios.
- Saturated-primary failover stays within the admission latency SLO.
- Singleton/fleet topology never exceeds its declared aggregate permit bound.

### Privacy and security

- No request or response bodies in logs, stores, traces, metrics, or audit.
- No upstream or route credentials in routine status and evidence artifacts.
- Every privileged mode has a current threat model and rotation drill.

### Operations

- Every running instance reports build, policy, schema, adapter, and topology
  versions.
- Configuration can be validated and simulated before activation.
- Backup, restore, takeover, and rollback are exercised, not merely documented.

### User outcomes

- Applications gain provider resilience without application-level retry trees.
- Operators can explain a routing decision from bounded metadata alone.
- Teams can add a qualified compatible provider without modifying the core.

## 13. Definition of done

A feature is not done when it has only a UI, a happy-path unit test, or a
provider demo. It is done when:

- policy and degradation behavior are documented;
- identity, credential, privacy, and cardinality effects are bounded;
- the pure decision and async lifecycle are separately tested;
- cancellation, shutdown, stale data, and hostile input are covered;
- metrics and reason codes explain success and rejection;
- deployment and rollback are documented;
- direct-versus-proxied wire guarantees are requalified;
- supported modes and non-goals remain honest.

The maximal charter is complete only when the end state in §2 is demonstrably
available across the claimed modes, every permanent exclusion still holds,
and an independent operator can verify the release evidence without trusting a
narrative description.

## 14. Promotion gate for future execution plans

A capability from this charter becomes an execution plan only after answering:

1. What observed problem and user owns the need?
2. Why is switchboard the correct component rather than an integration?
3. Which permanent principles and operating modes are affected?
4. What new state, identity, credential, or cardinality is introduced?
5. How does it degrade under stale data, partition, overload, and shutdown?
6. How is the behavior proven without body capture?
7. What is the smallest independently valuable slice?
8. What will be removed or deferred to pay its operational cost?

Plans that cannot answer all eight remain ideas, not scheduled work.

## 15. Immediate next tranche

Execute Plan 006 only. In parallel, prepare the sluice API inventory and
characterization tests from Plan 007. Do not add providers, active/active
coordination, credential brokerage, cost routing, or broader UI until the
ordered-admission path and clean dependency installation are qualified.

That tranche is deliberately narrow because every maximal capability depends
on truthful state, exactly-once leases, and an honest replay boundary; none
requires weakening them.
