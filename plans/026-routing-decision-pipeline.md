# Plan 026 — The routing decision pipeline

Status: **Ratified by Paul 2026-08-11 ("draft and implement"), implementation
delegated to subagents same session.**

Depends on: Plan 025 (peak demotion, the newest classification signal), the
advisory-containment fixes (the degraded backstop tier), and the whole
accretion this plan exists to organize: Plans 008, 010–016, 020, 023, 025.

## 1. What Paul asked for

> Holistically, how do you think we should be approaching the routing design?
> It is kind of ad hoc right now.

He is right. `route_decision` is a sediment of ten plans' mechanisms that
compose *by accident*, through guards written when someone notices an
interaction. The evidence is one week's bug list: post-dwell failback
silently overwrote the pace ordering (fixed, but only for pace — headroom
has the same latent bug); the flap margin discards the whole ranking on a
top-two tie; `primary` is computed two different ways (shell vs core);
opportunistic burn would preferentially front the estate's most expensive
provider. None were dumb mistakes — they are what N mechanisms with N²
pairwise interactions produce when every pair is handled bespoke.

Second symptom: the decision cannot explain itself. `reason` is one ad hoc
string, so "why did this request go to zai?" requires re-deriving the
decision by hand from seven signals.

## 2. The target shape

`route_decision` becomes a staged pipeline with an explicit ranking
contract. Same pure core, same import boundary, same function signature
(plus one optional input in Wave 3). Every existing mechanism already
secretly belongs to one stage; the refactor makes membership definitional:

1. **Resolve** — route table → ordered candidates. Unchanged.
2. **Filter** — hard constraints, strictly subtractive: missing state,
   `CLOSED`, model servability, capability, (quarantine stays in the shell
   where it is today, expressed through the servable set). A filtered
   candidate is OUT; nothing downstream may resurrect it.
3. **Classify** — every surviving candidate is assigned exactly one tier:

   | Tier | Meaning | Today's members |
   |---|---|---|
   | `IMMEDIATE` | eligible to serve now | fresh + AVAILABLE, un-demoted |
   | `QUEUE` | serve only via the queue path | BUSY; demoted by peak / low headroom / token budget / trailing-24h; UNKNOWN-freshness primary never lands here (excluded in filter semantics as today) |
   | `BACKSTOP` | last resort, after every fresh candidate | non-primary DEGRADED |

   Each demotion signal is a named pure predicate `(state, config,
   is_primary) → bool` — adding a signal is additive, never surgical. The
   signal *names* travel with the result (see stage 6).
4. **Rank within tier** — the strategy is nothing but the choice of scoring
   key for the `IMMEDIATE` tier: `ordered` = table position, `headroom` =
   session headroom desc, `pace` = weekly surplus desc (flap margin
   unchanged in Wave 1). `QUEUE`/`BACKSTOP` keep candidate order. This
   dissolves the strategy trichotomy into one parameter.
5. **Stickiness overlay** — affinity dwell, failback/hysteresis,
   conversation pins. One law, stated once (enforced as behavior in Wave 2):
   **stickiness may promote a provider within its tier, never across a tier
   boundary.** A pinned provider that gets demoted (e.g. enters its peak
   window) loses the pin's effect automatically.
6. **Emit with explanation** — the plan carries per-candidate assessments:
   `(name, tier, signals_fired, score, final_rank)`. `reason` becomes
   derived (existing strings preserved verbatim — tests assert them), the
   decision log becomes debuggable, and a read-only explain endpoint lets
   the GUI answer "who would serve model X right now, and why".

### Invariants (each gets a named test in Wave 1)

- **Demote, never drop.** No cost/pressure/staleness signal may remove a
  provider from the plan entirely; only Filter excludes, and Filter is
  hard-constraints-only.
- **Stale never outranks fresh.** BACKSTOP sorts after every fresh tier
  member in queue-candidate selection.
- **Signals are facts; policy lives in Classify/Rank.** The shell computes
  booleans and numbers (`in_peak`, surplus, headroom, freshness); nothing in
  the shell orders candidates.

## 3. Why not the alternatives

- **Weighted-sum cost function**: untunable and opaque; lexicographic tiers
  match how the operator actually reasons ("never expensive if avoidable"
  is a tier boundary, not a coefficient).
- **General rules DSL**: the actual policies are expressible as provider
  attributes plus a fixed algebra; a DSL trades one ad hoc for another.
- **Price tables / cost-aware routing**: Plan 009 deferred it deliberately;
  no reliable price signal exists, and the operator routes by fitness.
  Peak windows + per-model preference cover the stated policies.

## 4. Waves

### Wave 1 — behavior-preserving restructure (goldens: the full suite)

The 1000+ existing tests are the contract. **Zero behavioral change.**

- W1.1 Introduce `Tier` enum and `CandidateAssessment` (frozen dataclass:
  `name`, `tier`, `signals: tuple[str, ...]`, `score: float | None`,
  `rank: int`). `AdmissionPlan` gains
  `assessments: tuple[CandidateAssessment, ...] = ()` (defaulted — existing
  constructors and tests untouched).
- W1.2 Reorganize `route_decision`'s body into private stage functions
  (`_stage_filter`, `_stage_classify`, `_stage_rank`, `_stage_stickiness`)
  with the pipeline explicit at the top level. Preserve every reason
  string, every selection rule (queue-candidate preference order: primary
  in queue tier → first queue-tier member → primary in immediate →
  first backstop), and the exact demotion predicates. The existing
  suite must pass **unmodified**.
- W1.3 Signal names: `"busy"`, `"low_headroom"`, `"over_budget"`,
  `"over_24h"`, `"in_peak"`, `"degraded"` — attached to assessments.
- W1.4 Explain surface: `GET /admin/route-plan?model=<m>&key=<raw-key>`
  (admin-auth, read-only; both params optional — no model = unfiltered, no
  key = default route). Runs `snapshot_provider_state` + `route_decision`
  and returns the assessments, selected order, queue candidate, reason.
  Never mutates affinity or metrics (pass `affinity=None`).
- W1.5 Shell decision log: `recent_decisions` entries gain a compact
  `signals` map (`{name: [signals]}` for candidates that had any). Bounded;
  no schema break for existing consumers (additive key).
- W1.6 GUI: a "Routing Explain" card — model-name input (datalist from the
  model map) + button → renders the assessment table (tier, signals, score,
  order). Same vanilla-JS + harness-test pattern as the rest of the page.
- W1.7 Invariant tests (named for the invariant, §2) + docs/routing-model.md
  rewritten around the pipeline.

### Wave 2 — deliberate behavior changes (each individually tested)

- W2.1 **Tier-bounded stickiness, generalized.** The Wave-1 stickiness
  stage currently reproduces today's behavior, including the pace-only
  post-dwell inert rule from the 08-11 fix. Generalize: post-dwell
  failback-to-primary and failback hysteresis apply **only under
  `ordered`** (fronting the primary is *ordered's own ranking*, not a
  universal law); under `headroom` and `pace` an expired pin goes inert and
  the ranking stands. This fixes headroom's latent copy of the pace bug.
  `pin_conversations` keeps its documented Plan 019 semantics (holds past
  dwell) but only within the IMMEDIATE tier — already true structurally;
  add the test that a pinned provider demoted into QUEUE loses the pin.
- W2.2 **Retire opportunistic burn (Plan 016).** Pace supersedes it (016's
  philosophy promoted to a primary ordering), it is off by default, and its
  reset-window heuristic actively favors the expensive provider (z.ai's
  ~5 h session reset always sits inside the 6 h burn window). Removal
  shape: delete the decision-path logic (`_opportunistic_target` and its
  invocation); config fields (`opportunistic_*`) remain *accepted* from
  TOML and stored overlays but inert, with a boot-time warning naming this
  plan (an old overlay row must not crash boot); `PUT
  /admin/config/routing` rejects writes to them with a clear message; the
  GUI drops the fields. Repurpose tests/test_integration_plan016.py to
  pin the inertness + warning.
- W2.3 **`primary` computed once.** The shell's `candidates[0]`
  (pre-filter) is used for metrics and affinity while the core re-derives
  an effective primary post-filter — every model-map exclusion of the
  configured primary today counts a phantom failover and creates an
  affinity pin. The plan's emitted `terminal_fallback`/assessments become
  the single source: proxy.py reads the effective primary from the plan
  instead of recomputing. Metrics semantics change (failovers stop counting
  model-map filtering); note it in the commit.

### Wave 3 — per-model preference (the missing workload axis)

- W3.1 Model map entries gain an optional **preference order**: SQLite
  `model_map` table grows a `preference TEXT` (JSON array) column via the
  Plan-025 PRAGMA/ALTER migration pattern; TOML shape
  `[model."glm-5.2"] ...` is unchanged (aliases only) — preference is a
  store/GUI concept first; a TOML shape can follow if wanted.
- W3.2 The pure `ModelMap` carries it (`preference_for(model) ->
  tuple[str, ...]`), validated on write: every named provider must hold an
  alias for that model; it reorders, **never adds** (the model map's
  founding rule).
- W3.3 Rank integration: within the IMMEDIATE tier, sort key becomes
  `(model_preference_rank, strategy_score, table_order)` — an operator's
  per-model preference outranks the strategy, the strategy orders the
  unpreferred rest. QUEUE/BACKSTOP unchanged.
- W3.4 Admin: `POST /admin/model-map` accepts optional
  `"preference": ["prov", ...]` (validated subset of the alias keys);
  GET surfaces it; the model-map GUI editor gains an optional ordered
  field (comma-separated input, pre-filled on Edit).
- W3.5 Tests: preference beats pace surplus within IMMEDIATE; preference
  never resurrects a filtered/demoted provider; store round-trip +
  migration; GUI harness round-trip.

## 5. Validation

Per wave: full pytest + ruff + mypy + all tests/gui/*.mjs green before the
wave's commit. Wave 1 additionally: the pre-existing suite passes
**without modification** (behavior-preserving proof). Live validation after
merge: explain endpoint answers for glm-5.2; a smoke completion routes
per the explanation.

## 6. Out of scope

Cost/price tables; a rules DSL; changing quarantine or reroute semantics;
k8s deployment (stays on :main until Paul reviews); per-provider pace burn
rates; time-of-day anything beyond what Plan 025 shipped.
