# Plan 016 — Opportunistic quota burn (use-it-or-lose-it routing)

Status: implemented (WI-1 … WI-5 landed; pure-core + dashboard/snapshot
wiring + config + integration tests, ruff + mypy clean)

Depends on: **Plan 014** (failback hysteresis — a selected target must
stay pinned long enough for a conversation to ride the window; without
hysteresis, per-request re-evaluation flaps providers and defeats prompt
caching), **Plan 015** (`usage_headroom` ranking + z.ai onboarding).

## 1. Problem

Provider quota windows are **use-it-or-lose-it**. z.ai's session window
(5 h) refills whether or not the account used it — surplus quota at the
moment of reset is value destroyed. Today switchboard only consults
usage signals to *de-prefer* pressured providers (Plan 011 headroom,
Plan 012 budgets, Plan 013 trailing-24 h). A provider sitting on 80 %
unused quota with 2 h to reset receives **no** traffic preference —
the primary keeps serving until it is pressured, and the surplus expires.

The operator's ask:

> If I have a bunch of z.ai usage left, the system should opportunistically
> reroute to it.

This is the first **positive-selection** signal in switchboard: every
existing signal demotes; this one *promotes*. That is a routing-objective
change, and it is deliberately opt-in.

## 2. The primary-demotion question (the heart of this plan)

Hard rule 1: *"Proactive utilization signals (headroom, token budget)
never demote the primary — its own gate handles its limits."* Plan 013
carved out the one exception (trailing-24 h penalty, which the gate
cannot see coming). This plan adds a second, narrower exception, and the
honest framing is identical:

- **It de-prefers only.** The primary is never excluded: it stays in
  `immediate` (still selectable if the target loses the acquire race),
  stays `queue_candidate` backstop, stays `terminal_fallback`. The
  change is front-of-list preference — exactly the Plan 013 shape.
- **Opt-in, default off.** `opportunistic_enabled = false` reproduces
  today's behaviour byte-for-byte. The operator turns this on knowing
  they own quota-bearing fallbacks.
- **Stale/unknown data never promotes.** A candidate qualifies only on
  FRESH signal + measured headroom + measured reset time. Any `None` →
  not eligible. This composes with (never relaxes) the existing
  fresh-only-for-failover rule.
- **umans is incommensurable by design.** Code Max has no percentage
  quota, so umans never *wins* opportunistic selection — it can only be
  de-preferred in favour of a measured quota-bearing fallback. This is
  the correct reading of the operator's ask: umans is the default
  engine; burn the expiring windows when they're worth burning.

**Precondition — stickiness (Plan 014).** switchboard re-decides per
request; unpinned opportunistic selection would flip a conversation
between providers on successive polls, destroying the prompt-cache
warmth affinity exists to protect. Opportunistic selection therefore
uses the *existing* affinity mechanism: a non-primary acquisition
(including one from an `opportunistic` decision) sets the route's
affinity, dwell + hysteresis hold the pin, and re-evaluation happens on
dwell expiry — not per request.

## 3. Design

### 3.1 Signal: measured headroom + measured reset time

`usage_headroom` (session-window fraction remaining) already lands on
`ProviderState` (Plan 015 §3.2). This plan adds the reset clock:

```python
@dataclass(frozen=True)
class ProviderState:
    ...
    quota_resets_in: float | None = None
    # Seconds until the quota window this headroom refers to resets.
    # None = unknown (never promotes — fail safe).
```

Wiring: `DashboardTruthSource._reading_to_limit_state` maps the
dashboard's `session_resets_at` (ISO timestamp) →
`LimitState.bucket_reset_epoch`; `snapshot_provider_state` converts to
`quota_resets_in = bucket_reset_epoch - now` (None when absent/past).

### 3.2 Pure core (`control.py`)

```python
@dataclass(frozen=True)
class RoutingConfig:
    ...
    opportunistic_enabled: bool = False
    opportunistic_min_headroom: float = 0.5      # only when >= half the window remains
    opportunistic_reset_window: float = 21600.0  # seconds; only inside the last 6 h
    opportunistic_margin: float = 0.10           # winner must lead the runner-up by this
```

In `route_decision`, after `immediate`/`queue_eligible` are built and
after the Plan 015 ranking, but **subordinate to an active affinity pin**
(the dwell/hysteresis block runs first; only its no-pin branch is
extended):

```python
else:  # no active affinity pin
    target = _opportunistic_target(immediate, primary, states, config)
    if target is not None:
        immediate.remove(target); immediate.insert(0, target)
        reason = "opportunistic"
    elif primary in immediate:
        front(primary); reason = "primary_available"
```

`_opportunistic_target(immediate, primary, states, config) -> str | None`
returns the argmax-headroom qualifier, or None. A candidate qualifies
iff:

- not the primary, in `immediate` (hence FRESH + AVAILABLE);
- `usage_headroom is not None and usage_headroom >= opportunistic_min_headroom`;
- `quota_resets_in is not None and 0 < quota_resets_in <= opportunistic_reset_window`.

Margin rule: the best qualifier must exceed the runner-up's headroom by
`opportunistic_margin` (else None — poll-noise guard; ties/high-usage
clusters keep the primary).

New bounded reason code: **`opportunistic`**.

### 3.3 Shell (`proxy.py`)

No new mechanism: the existing post-acquisition block already records an
affinity entry for **any** non-primary acquisition with
`failover_reason=plan.reason` — `opportunistic` flows through; dwell +
failback hysteresis (Plan 014) govern release. The two affinity counters
(Plan 014 WI-3) make opportunistic pin/failback rates observable.

### 3.4 Config (`cli.py`)

```toml
[routing]
opportunistic_enabled = true
opportunistic_min_headroom = 0.5
opportunistic_reset_window = 21600.0
opportunistic_margin = 0.10
```

### 3.5 What this deliberately does not do

- **No displacement while the target's window is not expiring**
  (`reset_window` cap): arbitrating quota that has days to live is churn
  for nothing — primary-first stands until the burn zone.
- **No mid-conversation re-burn:** qualifying mechanics run on the
  no-pin branch; an established conversation affinity is never
  interrupted by a *better* score.
- **No "umans has infinite headroom" fiction:** quota-less providers
  carry `None` and are never *targets*.

## 4. Work items

- **WI-1** Pure core: `quota_resets_in` on `ProviderState`; four config
  fields; `_opportunistic_target` + branch + `opportunistic` reason.
  Unit tests: disabled default unchanged; qualifies → fronts target +
  primary still immediate/queue/terminal; below floor → None; outside
  window → None; `None` data → None; margin suppresses noise; affinity
  pin (dwell + hysteresis) beats opportunism; target demoted by
  another signal (headroom_threshold/budget/24 h) is already out of
  `immediate` and never qualifies.
- **WI-2** Shell: `session_resets_at` → `bucket_reset_epoch` mapping in
  `dashboard.py`; `quota_resets_in` in `snapshot_provider_state`
  (`providers.py`). Unit tests.
- **WI-3** Config (`cli.py`): four `[routing]` keys + validation
  (0 < margin < 1; window > 0; 0 < min_headroom ≤ 1) + startup log.
- **WI-4** Integration test (`tests/test_integration_plan016.py`):
  primary healthy, z.ai fallback at 70 % headroom with reset in 3 h →
  request routes to z.ai; reset in 20 h (outside window) → primary
  serves; headroom 0.3 → primary serves. Disabled default → primary
  serves in all three.
- **WI-5** Docs: AGENTS.md hard rule 1 — add the second exception
  paragraph (mirrors the Plan 013 amendment language); routing-model.md
  decision-order + reason table; this plan's status.

## 5. Future (not this plan)

- **Weekly-window burn** (second axis: qualifiy on weekly headroom near
  weekly reset).
- **Burn-rate targeting:** use the last window's consumption rate to
  compute the traffic *share* needed to land at zero-at-reset instead of
  a binary prefer/de-prefer.
- **Estimator-learned reset windows** if providers' reset semantics
  drift from the dashboard's reporting.

## 6. Non-goals

- No change to queue/terminal/backstop semantics — de-preference only.
- No per-request cost modelling, fairness, or weighted balancing
  (routing-model.md §9 stands).
- No consumption of quotas without measured reset times (weekly-only
  providers with unknown `resets_at` are never targets — fail safe).
