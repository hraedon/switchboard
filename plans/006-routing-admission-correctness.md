# Plan 006 — Routing and admission correctness recovery

Status: **implemented** (landed in commit b8d5178 "feat: implement plans
001-006" plus later refinements; all eight work items shipped)

Evidence (do not re-plan; this work is in the code and under test):

- Categorical eligibility and ordered admission: `Availability`,
  `SignalFreshness`, `AdmissionPlan` and `route_decision`
  (`src/switchboard/control.py`); the proxy consumes the plan in `_admit`
  (`src/switchboard/proxy.py`) — try every immediate candidate non-blocking,
  a second non-blocking pass on snapshot-race loss, queue only on
  `queue_candidate`.
- WI-006.1 live saturation: `snapshot_provider_state`
  (`src/switchboard/providers.py`) derives `BUSY` from `gate.available == 0`
  or `queue_depth > 0`; test `test_snapshot_provider_state_all_permits_held_is_busy`.
- WI-006.2 stale-fails-safe: `SignalFreshness` FRESH/DEGRADED/UNKNOWN with
  fresh-only-for-failover; tests `test_primary_unknown_excluded_from_failover`,
  `test_stale_fallback_not_preferred_over_busy_primary`,
  `test_degraded_primary_can_stay`, `test_degraded_fallback_not_failover_target`.
- WI-006.3 ordered immediate admission: `_admit` (see above); tests
  `test_admit_immediate_failover_to_idle_fallback` (no queue wait),
  `test_admit_race_fills_primary_tries_next`,
  `test_admit_queue_wait_uses_remaining_budget`.
- WI-006.4 bounded observability: `RoutingMetrics.recent_decisions` bounded
  ring (`_RECENT_DECISIONS_MAX = 128`) with `evicted_decisions` counter
  (`src/switchboard/proxy.py`); tests
  `test_routing_metrics_bounded_recent_decisions`,
  `test_routing_metrics_bounded_with_many_unique_keys`.
- WI-006.5 sluice dependency: `pyproject.toml` pins `sluice>=1.3.9,<2.0` with
  an explicit path source; clean wheel install proven by
  `tests/test_wheel_install.py::test_clean_wheel_install`.
- WI-006.6 dashboard cadence: `dashboard_poll_interval` is passed into the
  `ReconciliationLoop` (`src/switchboard/providers.py`).
- WI-006.7 persistence: SQLite route-table store behind `--route-table-store`
  (`src/switchboard/route_table.py`, `src/switchboard/cli.py`); restart
  survival proven by `test_sqlite_persistence_add_entry_survives_new_manager`,
  `test_sqlite_persistence_remove_entry_gone_in_new_manager`.
- WI-006.8 config validation: `_validate_config` / `_validate_provider_config`
  (`src/switchboard/cli.py`) reject empty/invalid/unknown configuration
  references at startup.
- Routing-invariant property tests: `test_closed_never_in_immediate`,
  `test_unknown_never_outranks_fresh`,
  `test_plan_contains_only_configured_providers`, `test_same_inputs_same_plan`,
  `test_no_provider_appears_twice_in_immediate`,
  `test_terminal_fallback_always_primary`.

Priority: release-blocking

Depends on: Plans 001–005 implementation as currently present

Blocks: Plans 007–008 execution, any Plan 009-derived work, and any production deployment

## 1. Goal

Make switchboard's central promise true under live concurrency: select only
eligible providers, attempt available capacity without delay, queue according
to an explicit policy, and fail safe when information is missing or stale.

This plan also closes the concrete findings from the first architectural
review. It deliberately avoids expanding the product surface while the data
plane contract is being repaired.

## 2. Non-negotiable outcomes

1. A primary with no immediately available permit is represented as
   saturated even when its configured capacity is positive.
2. Stale or unknown provider data can never make a fallback more attractive.
3. Routing produces an ordered admission plan, not an optimistic single
   provider name.
4. Every eligible candidate gets an immediate non-blocking acquisition attempt
   before any request waits in one provider's queue.
5. Queueing is a named policy with bounded time and honest Retry-After.
6. The provider actually used—not merely the provider initially preferred—is
   recorded in routing metrics.
7. Caller-controlled observability state is bounded.
8. A clean installation brings a compatible sluice dependency with it.

## 3. Core model revision

Replace the scalar-only choice with categorical eligibility plus an ordered
plan. The exact names may change, but the pure model should express at least:

```python
class Availability(Enum):
    AVAILABLE = "available"      # eligible and permit available now
    BUSY = "busy"                # eligible but no permit available now
    CLOSED = "closed"            # boxed, breaker-open, administratively closed
    UNKNOWN = "unknown"          # not ready or signal too stale to trust

@dataclass(frozen=True)
class ProviderState:
    name: str
    availability: Availability
    available_permits: int
    queue_depth: int
    retry_after_seconds: int | None
    signal_freshness: SignalFreshness
    preference_rank: int

@dataclass(frozen=True)
class AdmissionPlan:
    immediate_candidates: tuple[str, ...]
    queue_candidate: str | None
    terminal_fallback: str
    reason: str
```

The core remains stdlib-only and pure. `now`, freshness deadlines, prior route
state, and all observations remain arguments.

### 3.1 Eligibility before ranking

The decision proceeds in this order:

1. Resolve the route to an ordered candidate list.
2. Reject missing, closed, and capability-ineligible candidates.
3. Separate fresh candidates from unknown/stale candidates.
4. Place candidates with immediate permits first.
5. Preserve primary preference among equally admissible candidates.
6. Select at most one explicit queue candidate.
7. Preserve the configured primary as the terminal safe-failure target so its
   gate can provide the canonical rejection when nothing is usable.

Do not compare percentages directly with seconds. Plan 006 may retain
provider-local pressure only as a tie-breaker among states with the same signal
kind. A normalized policy belongs in Plan 008.

### 3.2 Staleness semantics

Use three states rather than overloading a numeric score:

- `fresh`: may be selected or preferred normally;
- `degraded`: last-known-good may keep an already-primary route serving within
  a bounded TTL, but it is not a new failover target;
- `unknown`: excluded from failover preference and admitted only under an
  explicit route policy.

The default policy is `fresh-only-for-failover`. Unknown data never maps to
zero pressure.

## 4. Admission algorithm

The async shell consumes `AdmissionPlan` as follows:

1. For each `immediate_candidate`, call a true non-blocking gate operation.
2. Forward through the first successful acquisition.
3. If all immediate attempts lose the snapshot race, perform one final
   non-blocking pass over the remaining eligible candidates.
4. If configured, wait only on `queue_candidate` for the remaining queue
   budget.
5. After queue timeout, return an honest 503 derived from the best available
   structural signal.

If `PermitGate.acquire(timeout=0)` does not provide reliable try-acquire
semantics, add a public `try_acquire()` primitive in sluice with its own FIFO
and cancellation tests. Do not simulate it with unlocked reads of
`gate.available`.

The default queue policy is:

```text
try every fresh eligible provider now
then queue on configured primary for at most queue_timeout
then return 503
```

Alternative policies may be added later, but must be named route configuration
rather than hidden control flow.

## 5. Concrete review-finding closure

### WI-006.1 — Correct live saturation snapshots

- Derive `BUSY` from `available_permits == 0` or `queue_depth > 0` when the
  gate otherwise has positive effective capacity.
- Keep structural closure separate from transient saturation.
- Add a test that acquires every permit from a positive-capacity gate and then
  snapshots it.

### WI-006.2 — Make stale data fail safe

- Remove the `usage_stale -> pressure 0` behavior.
- Test stale primary, stale fallback, never-ready fallback, stale
  last-known-good, and all-unknown combinations.
- Reconcile `docs/routing-model.md` and Plan 003 with the implemented policy.

### WI-006.3 — Implement ordered immediate admission

- Replace `_try_acquire(candidates, selected)` with plan-driven admission.
- Prove that a full primary and idle fallback do not wait for `queue_timeout`.
- Prove that a race filling the preferred gate still tries the next candidate.
- Preserve permit release and upstream cancellation on every branch.

### WI-006.4 — Bound routing observability

- Replace `last_routing: dict[route_key, provider]` with a bounded ring of
  recent decisions or a fixed-cardinality per-configured-route map.
- Never create Prometheus labels from arbitrary client-provided values.
- Expose dropped/evicted observation counts.
- Load-test unique bearer tokens and assert bounded memory/cardinality.

### WI-006.5 — Declare the sluice dependency

- Add a compatible, deliberately pinned sluice dependency.
- If sluice is not yet published, use the workspace's supported internal
  package mechanism rather than relying on sibling `PYTHONPATH` injection.
- Add a clean-environment wheel installation test that imports the CLI and
  boots a minimal application.
- Document the supported sluice version range pending Plan 007.

### WI-006.6 — Wire dashboard polling correctly

- Make the reconciliation loop, not the truth source, own poll cadence.
- Pass `dashboard_poll_interval` into that loop.
- Remove unused cadence state from `DashboardTruthSource`.
- Test call counts with a controlled clock/event loop.

### WI-006.7 — Make persistence reachable

- Add `--route-table-store`, environment, and TOML configuration.
- Define startup precedence: persisted runtime entries override file seeds;
  file entries seed only absent keys.
- Validate and fail startup on unreadable/corrupt stores; do not silently
  discard runtime routes.
- Close the database on failed startup as well as normal shutdown.

### WI-006.8 — Validate all configuration references

- Reject empty upstream URLs, invalid targets and timeouts, duplicate provider
  names, unknown provider types, and unknown providers in every route entry.
- Validate hashed-key format for file-defined routes.
- Convert all configuration failures to concise CLI errors and close partially
  created provider clients.

## 6. Integration and adversarial tests

Required tests use real `PermitGate` and `ReconciliationLoop` instances where
possible; mocks alone do not qualify the concurrency path.

- Positive-capacity primary fully held; fallback idle; immediate failover.
- Primary becomes full between snapshot and acquisition.
- Both providers full; one permit releases during the queue window.
- Downstream disconnect while queued, uploading, awaiting headers, and
  streaming a response.
- Stale dashboard fallback is not selected over a known primary.
- All signals stale or unavailable return through the configured safe-failure
  path.
- Actual acquired provider drives failover and forwarded metrics.
- Ten thousand unique route credentials leave bounded observation state.
- Restart restores persisted route mutations.
- Clean wheel install works without sibling source directories.

Add a small deterministic state-machine/property test for routing invariants:

- closed candidates never appear in immediate candidates;
- unknown candidates never outrank fresh candidates by default;
- the plan contains only configured providers;
- same inputs produce the same plan;
- no provider appears twice;
- the terminal fallback is always defined when a route has a primary.

## 7. Documentation changes

- Rewrite `docs/routing-model.md` around eligibility, admission planning, and
  queue policy.
- Add a sequence diagram for snapshot → plan → try-acquire → queue → forward.
- State clearly that failover occurs only before upstream forwarding begins.
- Record current singleton deployment assumptions.
- Correct README claims that are not yet implemented.

## 8. Rollout and evidence

1. Land sluice `try_acquire()` first if required.
2. Land the pure model and exhaustive unit tests.
3. Land plan-driven admission and integration tests.
4. Land dependency, cadence, persistence, and metrics closure.
5. Run CI on every supported Python version.
6. Run a two-provider soak with forced saturation and disconnect injection.
7. Capture latency histograms proving saturated-primary failover does not wait
   for queue timeout.

## 9. Acceptance criteria

- [ ] All eight work items are complete.
- [ ] A live full primary fails over to an idle fallback without queue delay.
- [ ] Stale or unknown state never improves provider preference.
- [ ] Routing returns an `AdmissionPlan` or equivalent ordered pure result.
- [ ] Admission tests cover snapshot races and cancellation.
- [ ] Caller-controlled routing observations are bounded.
- [ ] Dashboard cadence is honored.
- [ ] SQLite persistence is reachable and restart-tested.
- [ ] A clean package installation includes a compatible sluice.
- [ ] Tests, Ruff, strict mypy, identifier gate, and all Python-version CI pass.
- [ ] No request or response body is parsed, logged, stored, or replayed.
