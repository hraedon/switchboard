# Plan 007 — Stable shared flow-control substrate

Status: **partially implemented** — the coupling rules shipped and are
enforced; the substrate extraction (snapshots, lease protocol, shared
streaming lifecycle) did not land as a separate package

Landed:

- §2 decision holds: switchboard composes sluice's single gate/reconcile/
  breaker implementation (`PermitGate`, `ReconciliationLoop`,
  `Provider`/`TruthSource`/`get_provider`/`make_truth_source`, breaker and
  adaptive config from `sluice.control`) rather than carrying copies, and
  sluice remains a separate repository consumed as a library
  (`pyproject.toml` `sluice` path source; CI installs
  `sluice @ git+https://github.com/hraedon/sluice.git`).
- §3 / WI-007.5 underscore-import prohibition: enforced by
  `tests/test_import_boundary.py::test_no_underscore_imports_from_sluice`
  (AST-walks every `from sluice import ...` and rejects `_`-prefixed names).
  switchboard imports only public sluice admin/session helpers
  (`send_json`, `check_admin_auth`, `mint_session`, `SESSION_COOKIE`, …) —
  never sluice's `ProxyApp`, CLI, static assets, or mutable singleton state.
- "Shared package imports neither application" holds: sluice has no
  switchboard dependency.

Not landed:

- WI-007.3 public snapshots: no `GateSnapshot`/`ReconcileSnapshot` frozen
  values. switchboard reads sluice's live public accessors
  (`ctx.gate.available`, `ctx.gate.queue_depth`,
  `ctx.reconcile.gate_closed_reason()`, `ready`, `last_fetch_ok`).
- §4 `try_acquire`/`Lease` protocol: not added. The proxy uses
  `PermitGate.acquire(timeout=0.0)` for non-blocking try-acquire.
- WI-007.4 shared streaming lifecycle: not extracted. switchboard's streaming
  core is *adapted from* sluice's `_forward()` (`src/switchboard/proxy.py`),
  not imported — the byte-forwarding state machine still lives in both
  products, so the acceptance criteria "both applications use the same
  gate/reconcile/stream substrate" and "no safety-sensitive implementation is
  independently copied" are met for gate/reconcile/breaker but **not** for the
  streaming lifecycle.
- WI-007.6 release discipline: the `sluice>=1.3.9,<2.0` pin exists, but there
  is no dedicated substrate versioning/release and no minimum-version CI
  matrix dimension.

Depends on: Plan 006 dependency declaration and correctness contract

Blocks: independent releases of switchboard and broad provider expansion

## 1. Goal

Keep sluice and switchboard on one proven implementation of admission,
reconciliation, breaker behavior, and streaming lifecycle without making
switchboard depend on sluice's private application internals.

The target is a small public substrate consumed through composition. This is
not permission to turn sluice into a framework or to merge the two products.

## 2. Decision

Do not carry forward independent copies of safety-sensitive functionality.
Extract or promote a supported package boundary and make both products depend
on it.

The likely package name is `sluice-core`; a family-neutral name is acceptable
if packaging policy prefers it. The important property is ownership and a
stable API, not the name.

## 3. Package boundary

### Shared

- Pure limit, adaptive, breaker, band, and retry functions and dataclasses.
- `PermitGate`, including FIFO, reserve, resize, cancellation, and
  `try_acquire` semantics.
- Reconciliation engine and truth-source protocol.
- Provider-neutral streaming lifecycle primitives:
  request byte iterator, disconnect race, response byte streaming, clean
  cancellation, size/idle bounds, and hop-by-hop filtering.
- Public session/admin primitives only where genuinely common and security
  reviewed.
- Stable status snapshots required to compose routing decisions.

### Product-owned

- sluice's single-upstream ASGI application and CLI.
- switchboard's provider registry, route table, routing policy, admin API, and
  dashboard.
- Product-specific configuration schemas, metrics naming, deployment files,
  and static assets.
- Provider credential and capability policy.

### Explicitly forbidden coupling

- No imports of underscore-prefixed symbols across package boundaries.
- No switchboard import of sluice's `ProxyApp`, CLI, static assets, or mutable
  application singleton state.
- No shared package import of either product.
- No callback interface that lets product code bypass permit accounting.

## 4. Public API design

Define protocols and frozen observations rather than exposing mutable internal
fields. Illustrative interfaces:

```python
class Gate(Protocol):
    async def try_acquire(self, *, admission_class: str | None = None) -> Lease | None: ...
    async def acquire(self, *, timeout: float, admission_class: str | None = None) -> Lease | None: ...
    def snapshot(self) -> GateSnapshot: ...

class Reconciler(Protocol):
    def snapshot(self, *, now_monotonic: float) -> ReconcileSnapshot: ...
    def record_response_headers(self, headers: Mapping[str, str], status: int) -> None: ...

class Lease(AsyncContextManager[None]):
    """Exactly-once release with hold-time accounting."""
```

Prefer a lease object over manually paired `acquire()`/`release()` calls if it
can be introduced without destabilizing sluice. The API should make leaked or
double-released permits difficult.

The streaming helper should accept explicit dependencies and return a typed
outcome. It must never own routing or retry policy.

## 5. Extraction method

### WI-007.1 — Inventory and classify imports

- Enumerate every switchboard import from sluice.
- Classify each as public substrate, product-specific duplication, or
  accidental private coupling.
- Record the intended owner and compatibility promise.

### WI-007.2 — Characterization tests first

Before moving code, capture sluice behavior for:

- queue FIFO and cancellation;
- resize while held or queued;
- breaker and stale-reading transitions;
- request upload backpressure;
- disconnect during every streaming phase;
- duplicate and hop-by-hop header treatment;
- upstream 429 classification;
- response-start failure and upstream exceptions;
- shutdown drain and exactly-once release.

Run the same conformance suite against both applications' composed paths.

### WI-007.3 — Promote public snapshots and protocols

- Add stable `GateSnapshot` and `ReconcileSnapshot` values.
- Remove switchboard reads of mutable/internal reconciler properties where a
  snapshot can provide a consistent observation.
- Document clock domains and freshness semantics.

### WI-007.4 — Extract streaming lifecycle

- Move the common byte-forwarding state machine once.
- Preserve zero body parsing and bounded memory.
- Return typed outcomes such as completed, downstream-disconnected,
  upstream-connect-failed, upstream-idle, and response-started-failure.
- Keep response classification hooks narrow and synchronous.

### WI-007.5 — Remove private admin imports

- Publish reviewed helpers for ASGI response creation, bounded body reading,
  session verification, CSRF, and CORS only if both products truly need them.
- Otherwise implement product-local admin surfaces using public low-level
  primitives.
- Eliminate all cross-package underscore imports.

### WI-007.6 — Package and release discipline

- Version the shared API semantically.
- Pin minimum and maximum compatible ranges in both products.
- Publish changelog and migration notes for breaking changes.
- Test minimum-supported and newest-compatible versions in CI.
- Produce reproducible wheels with provenance and dependency metadata.

## 6. Repository strategy

Choose one explicitly:

1. **Workspace/monorepo:** simplest while the APIs are moving; atomic changes
   and one compatibility test matrix.
2. **Separate repositories with released core:** acceptable once APIs stabilize;
   changes land core → release → consumers.

Do not use ambient sibling checkouts or mutable branch URLs as the production
dependency mechanism.

## 7. Compatibility and assurance

- Cross-product conformance fixtures are versioned with the core.
- Every core bug fix gets a regression test consumed by both products.
- Wire-capture tests compare direct, sluice, and switchboard request bodies and
  allowed header transformations.
- Cancellation tests run under all supported Python versions and at least two
  ASGI server/httpx combinations where practical.
- An API-surface test rejects new public exports without review.

## 8. Migration sequence

1. Inventory imports and freeze characterization tests.
2. Add public snapshots and `try_acquire`/lease semantics in sluice.
3. Switch switchboard to those APIs.
4. Extract streaming lifecycle behind the same conformance suite.
5. Remove private admin/session imports.
6. Publish the first supported core release.
7. Pin both products and delete the old duplicate implementations.

At every step, sluice remains independently releasable and switchboard remains
able to run. Avoid a flag day.

## 9. Acceptance criteria

- [ ] No safety-sensitive implementation is independently copied between the products.
- [ ] No switchboard import references an underscore-prefixed sluice symbol.
- [ ] Both applications use the same gate/reconcile/stream lifecycle substrate.
- [ ] Public snapshots replace inconsistent reads of live mutable state.
- [ ] Cross-product conformance tests cover streaming and permit lifecycle.
- [ ] Dependency metadata and compatibility ranges are complete.
- [ ] Clean isolated builds and minimum-version CI pass.
- [ ] The shared package imports neither application package.
- [ ] Product behavior and wire captures remain backward compatible except for
      explicitly documented correctness fixes.
