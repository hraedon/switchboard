# Plan 015 — Fallback ranking by remaining usage (+ z.ai onboarding)

Status: implemented (WI-1 … WI-5 landed; pure-core + config + z.ai
config-path + integration tests, ruff + mypy clean)

Depends on: Plan 011 (`usage_headroom` signal), the
`DashboardTruthSource` provider fan-in (any `/readings` provider, not
just ollama).

## 1. Problem

When the primary is pressured and switchboard fails over, the fallback
order is **route-table order** — static config. If two fallbacks are both
FRESH+AVAILABLE, the request goes to whichever is listed first, even when
the second has a window of unused quota about to expire and the first is
nearly spent.

The signal to do better already flows end-to-end for dashboard-backed
providers: `/readings` `session_percent` → `requests_remaining`/`limit` →
`snapshot_provider_state` → `ProviderState.usage_headroom` (fraction of
the session window remaining, 0.0–1.0). It is consulted only as a
*demotion* threshold (`headroom_threshold`, Plan 011). Nothing uses it to
*order* candidates.

The operator's ask has two parts:

1. **z.ai as a routed provider.** usage-dashboard already polls z.ai
   quota (`session_percent`, `weekly_percent`, reset times). A z.ai
   upstream with an OpenAI-compatible surface can be gated by sluice's
   `openai`/`generic` (AIMD) strategy and fed quota truth from the
   dashboard — config only, no new code, worth a documented example.
2. **Headroom-ordered failover (Scope A of the opportunism feature).**
   When failing over anyway, try the fallback with the *most remaining
   window* first.

This plan does NOT displace a healthy primary — that is Plan 016.

## 2. Design stance

- **Opt-in, default off.** `headroom_ranking = false` reproduces today's
  exact ordering. The flag is an operator statement: "my fallbacks have
  comparable capability; spend the expiring windows first."
- **Ordering, never exclusion.** A provider with no headroom data is
  *not* demoted, penalised, or filtered — it keeps its eligibility and
  sorts in table order after data-bearing candidates (fail safe: unknown
  never beats measured). This composes with the existing fail-safe
  stack: FRESH/UNKNOWN filtering, `headroom_threshold` demotion, and
  queue/terminal semantics are all unchanged.
- **Primary preference is preserved by construction.** The re-sort runs
  on the built `immediate` list *before* the affinity block; the
  affinity/primary fronting step then runs exactly as today. Net effect:
  primary in `immediate` → primary first (unchanged); primary NOT in
  `immediate` → fallbacks ordered by remaining window. That is precisely
  "Scope A": the ranking only bites when already failing over.

## 3. Design

### 3.1 Pure core (`control.py`)

```python
@dataclass(frozen=True)
class RoutingConfig:
    ...
    headroom_ranking: bool = False
    # Order `immediate` candidates by usage_headroom (descending) before
    # affinity/primary fronting. Providers without headroom data sort
    # after data-bearing ones, in table order.
```

In `route_decision`, immediately after the candidate loop builds
`immediate` and before the affinity block:

```python
if config.headroom_ranking and len(immediate) > 1:
    order = {name: i for i, name in enumerate(candidates)}
    def _key(name: str) -> tuple[int, float, int]:
        st = states.get(name)
        h = st.usage_headroom if st else None
        # data-bearing first (headroom desc), then table order
        return (0 if h is not None else 1, -(h or 0.0), order[name])
    immediate.sort(key=_key)
```

Deterministic: ties break on table order; `None` never outranks measured.

### 3.2 No shell changes

`usage_headroom` already arrives via `snapshot_provider_state` for any
provider whose reconcile loop yields `requests_remaining/limit` — which
is exactly what `DashboardTruthSource._reading_to_limit_state` produces
for dashboard-backed providers (z.ai included: `session_percent` →
percent-remaining).

### 3.3 z.ai onboarding (config shape — documentation WI)

```toml
[provider."zai"]
upstream = "https://api.z.ai/api/paas/v4"     # OpenAI-compatible surface
type = "openai"                                # AIMD gate + ratelimit headers
target = 2                                     # conservative: no ground truth
dashboard_url = "http://<usage-dashboard>"     # quota truth
dashboard_token_env = "USAGE_DASHBOARD_TOKEN"
```

The `DashboardTruthSource` already fans in per provider
(`provider_name=name`; the dashboard serialises z.ai readings with
`"provider": "zai"`). No code needed — the plan's WI is a worked example
test proving the config path builds (config-parsing test), not a
deployment.

### 3.4 Anti-flap

None in v1 beyond existing dampers: quota headroom moves at
minutes-scale (dashboard cadence), and fallback ordering is evaluated
per request with table-order tiebreaks, so the ranking cannot oscillate
faster than the underlying readings change. A minimum-margin refinement
is documented as future work if poll-noise A/B flapping appears.

## 4. Work items

- **WI-1** Pure core: `headroom_ranking` flag + re-sort + unit tests:
  flag off → exact current order; flag on → headroom desc; `None` after
  data-bearing, table order preserved within groups; primary-in-immediate
  still fronts after the affinity block; single-candidate no-op.
- **WI-2** Config (`cli.py`): `[routing] headroom_ranking = true` parse
  + startup log.
- **WI-3** Integration test (`tests/test_integration_plan011…` pattern,
  new `tests/test_integration_plan015.py`): primary saturated; two
  fallbacks with different headroom → request lands on the
  higher-headroom fallback; flag off → table order wins.
- **WI-4** Config-path test: TOML with a `type = "openai"` provider +
  `dashboard_url` builds a `ProviderContext` whose truth source is
  `DashboardTruthSource` with `provider_name` = the provider key.
- **WI-5** Docs: `docs/routing-model.md` §3 ordering step + this plan
  status.

## 5. Future (not this plan)

- **Plan 016** — opportunistic displacement of a healthy primary
  (use-it-or-lose-it burn) using this headroom signal + reset-time
  awareness.
- **Ranking margin** (`headroom_ranking_margin`) to suppress
  poll-noise swaps.
- **Weekly-window headroom** as a second ranking axis.

## 6. Non-goals

- No change to `headroom_threshold` demotion semantics (they stack:
  demote first, then order the survivors).
- No displacement of a healthy primary.
- No new truth sources — z.ai quota comes from the existing dashboard
  fan-in.
