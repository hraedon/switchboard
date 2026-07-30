# Plan 014 — Failback hysteresis + affinity observability

Status: implemented (WI-1 … WI-6 landed; pure-core + proxy + metrics +
config + integration tests, ruff + mypy clean)

Depends on: Plan 008 §5 (route affinity / dwell / failback), Plan 013
(demote-de-prefers-only semantics for the primary).

## 1. Problem

Plan 008 §5 landed route affinity: after a failover, the routing core pins
the fallback to the front of `immediate_candidates` for `dwell_interval`
(default 30 s) so a conversation's requests stay on one provider and its
prompt cache stays warm. But **failback is eager**: on the first reconcile
tick after dwell expiry where the primary is FRESH+AVAILABLE, the core
fronts the primary again. At a *flapping* primary edge — the exact
conditions affinity exists for — this oscillates:

```
primary busy → failover (pin fallback) → 30 s dwell → primary flickers
healthy → failback → primary pressured again → failover (pin fallback) → …
```

Every swap is a cold prompt cache on both providers. The affinity record
even carries `healthy_observations` (incremented by the proxy since
Plan 012 WI-C5) — **collected, never consulted**. There is no
observability of affinity behaviour either: pins and failbacks are not
counted on any surface, so thrash is invisible to the operator.

The operator's ask: *a failed-over task should stay pinned to the fallback
"if possible", and failback should require evidence the primary is durably
healthy, not one lucky poll.*

## 2. Design stance

Time-based hysteresis, opt-in, fail-safe:

- **Default off.** `failback_delay = 0.0` reproduces today's behaviour
  byte-for-byte. This plan refines an existing opt-in mechanism (affinity
  is already automatic on failover), it does not change any default
  routing outcome.
- **Hysteresis delays failback; it never pins a bad fallback.** All
  existing guards still apply first: the affinity provider only pins when
  it is FRESH and in `immediate`. A cooled/boxed/stale fallback is
  released exactly as today. The only thing hysteresis can do is keep a
  request on a *working* fallback a little longer — a safe failure mode.
- **The core stays pure.** The continuity observation
  (`primary_healthy_since`) is shell state read in the proxy and passed in
  as an argument. The core does not track time series.

`healthy_observations` stays advisory telemetry (it counts *fallback*
successes, not primary health — the wrong input for a failback decision).
It remains collected for future use; this plan does not consume it.

## 3. Design

### 3.1 Pure core (`control.py`)

```python
@dataclass(frozen=True)
class RoutingConfig:
    ...
    failback_delay: float = 0.0
    # Seconds the primary must be continuously FRESH+AVAILABLE before an
    # affinity pin is released (failback). 0.0 = disabled (Plan 008 §5
    # behaviour: fail back on the first healthy poll after dwell).
```

`route_decision` gains a keyword argument:

```python
def route_decision(..., primary_healthy_since: float | None = None) -> AdmissionPlan:
```

In the affinity block, the post-dwell branch changes exactly one condition.
Today (affinity FRESH and in `immediate`):

```python
if (now - affinity.selected_at) < config.dwell_interval:
    pin(); reason = "affinity_dwell"
elif primary in immediate:
    failback()   # front the primary
else:
    pin()
```

The `elif primary in immediate:` branch becomes:

```python
elif primary in immediate:
    hysteresis = (
        config.failback_delay > 0
        and (
            primary_healthy_since is None
            or (now - primary_healthy_since) < config.failback_delay
        )
    )
    if hysteresis:
        pin(); reason = "affinity_hysteresis"
    else:
        failback()
```

New bounded reason code: **`affinity_hysteresis`** — failover pin held past
dwell while the primary re-proves itself. (Reason table in
`docs/routing-model.md` is updated; note that doc's §3 steps predate
affinity entirely — this plan brings §3 current.)

### 3.2 Shell (`proxy.py`)

`ProxyApp` tracks primary-health continuity:

```python
self._provider_healthy_since: dict[str, float] = {}
```

Updated each request, after `states` are snapshotted and before
`route_decision` is called, for the route's primary only:

```python
st = states.get(primary)
healthy = (
    st is not None
    and st.signal_freshness is SignalFreshness.FRESH
    and st.availability is Availability.AVAILABLE
)
if healthy:
    self._provider_healthy_since.setdefault(primary, now_mono)
else:
    self._provider_healthy_since.pop(primary, None)
```

and passed as `primary_healthy_since=self._provider_healthy_since.get(primary)`.

Notes:

- The clock *restarts* on any tick where the primary is not
  FRESH+AVAILABLE — one bad poll re-arms the hysteresis. That is the
  point.
- Tracking is per-request, not per-poll: a quiet period simply makes no
  decisions. The first request after quiet starts the clock from the
  current snapshot — hysteresis can only *delay* failback, never cause
  wrong-routing.
- Demoted-but-AVAILABLE primary (headroom / budget / 24 h signals):
  still in `queue_eligible`, not `immediate`, so the hysteresis branch is
  not in play — no interaction.

### 3.3 Metrics (`proxy.py` `RoutingMetrics` + `admin.py`)

Two bounded counters (no new labels, per WI-006.4):

- `affinity_pins_total` — incremented where the proxy records a new
  affinity entry (non-primary acquisition).
- `affinity_failbacks_total` — incremented where the proxy pops an
  affinity entry on return to primary.

Surfaced in `/status.json` (`routing` section) and `/metrics` as
`switchboard_affinity_pins_total` / `switchboard_affinity_failbacks_total`.

### 3.4 Config (`cli.py`)

```toml
[routing]
failback_delay = 120.0   # seconds; 0.0 = disabled (default)
```

Parsed like the other `[routing]` floats; logged at startup when > 0.

## 4. Work items

- **WI-1** Pure core: `failback_delay` on `RoutingConfig`,
  `primary_healthy_since` kwarg, hysteresis branch +
  `affinity_hysteresis` reason. Unit tests: disabled default unchanged;
  post-dwell + insufficient continuity → stays pinned; sufficient →
  failback; `None` continuity (never observed healthy) → stays pinned;
  affinity not FRESH/immediate → hysteresis not consulted.
- **WI-2** Shell: `_provider_healthy_since` tracking + pass-through.
- **WI-3** Metrics: two counters, `/status.json` + `/metrics` surfaces.
- **WI-4** Config: `[routing] failback_delay` parse + startup log.
- **WI-5** Integration test (`tests/test_integration_plan014.py`):
  MockTransport/NullTruthSource pattern per Plan 013 — primary flickers
  healthy before `failback_delay` elapses → fallback keeps serving; after
  delay with sustained health → failback.
- **WI-6** Docs: `docs/routing-model.md` §3 — bring the decision steps
  current (affinity/dwell from Plan 008 §5 + hysteresis from this plan) +
  reason-code table row.

## 5. Future (not this plan)

- **Consume `healthy_observations`** for adaptive dwell (extend the pin
  while the fallback is serving well).
- **Per-route hysteresis** (`[route.*] failback_delay` overrides).
- **Pin/failback rate alerts** if the counters show thrash in practice.

## 6. Non-goals

- No change to dwell semantics (the first `dwell_interval` of a pin is
  untouched).
- No change to which providers get affinity (failover and, later,
  opportunistic selections — same mechanism).
- No probabilistic or EWMA health scoring — deterministic continuity only.
