# Plan 003 — Dashboard truth source: usage-dashboard /readings → ollama pressure

**Goal:** Implement a `TruthSource` that polls the usage-dashboard's `/readings`
API to get ollama usage data, and feeds it into the routing decision.

## Prerequisite

- Plan 002 complete (multi-provider gates, route table)

## Scope

- `DashboardTruthSource` implementing sluice's `TruthSource` protocol
- Polls `GET /readings` on the usage-dashboard service
- Extracts ollama `Reading` (session_percent, weekly_percent)
- Normalizes into `LimitState` for the routing engine
- Stale handling: when the dashboard reading is older than TTL, mark as stale
  (fail-safe: treat as "available but uncertain")
- Config: dashboard URL, bearer token, poll interval, stale TTL

## Deliverables

### `src/switchboard/dashboard.py`

```python
class DashboardTruthSource:
    """TruthSource backed by the usage-dashboard /readings API.

    Polls the dashboard for ollama usage data (session_percent,
    weekly_percent) and normalizes into a LimitState.

    The dashboard's 5-30 min cadence means the reading is coarse. The
    routing decision treats stale data fail-safe: when older than
    dashboard_stale_ttl, pressure is unknown → provider is "available
    but uncertain" (not closed, not preferred for failover target).
    """

    def __init__(
        self,
        *,
        dashboard_url: str,        # http://usage-dashboard-server.usage-dashboard.svc.cluster.local:8080
        bearer_token: str,
        provider_name: str,         # "ollama"
        poll_interval: float = 30.0,
        stale_ttl: float = 900.0,  # 15 min — dashboard cadence is 5-30 min
    ) -> None: ...

    async def fetch(self, *, now_monotonic: float) -> CachedReading:
        """Fetch /readings, extract the ollama reading, normalize."""

    @property
    def last_cached(self) -> CachedReading | None: ...

    async def close(self) -> None: ...

    def record_response_headers(
        self, headers: dict[str, str], status: int, *, now_monotonic: float
    ) -> None:
        pass  # dashboard is the truth, not in-band headers
```

### Normalization

```python
def _reading_to_limit_state(
    reading: dict,       # the dashboard's Reading.to_dict()
    local_in_flight: int,
    *,
    now_monotonic: float,
) -> LimitState:
    """Normalize dashboard reading → LimitState for routing."""
    session_pct = reading.get("session_percent")
    weekly_pct = reading.get("weekly_percent")

    # Derive a pseudo-remaining from the percentage
    if session_pct is not None:
        requests_remaining = round(100 - session_pct)
    else:
        requests_remaining = None

    return LimitState(
        concurrent_sessions=local_in_flight,
        limit=1,               # ollama is typically single-stream
        hard_cap=2,
        requests_remaining=requests_remaining,
        requests_limit=100 if requests_remaining is not None else None,
        provider="ollama",
        age_seconds=0.0,
    )
```

### Provider state derivation

The `ProviderState` for ollama in the routing decision:

```python
# When dashboard data is fresh:
ProviderState(
    name="ollama",
    gate_closed_reason="open",
    available_permits=gate.available,
    queue_depth=gate.queue_depth,
    saturation_retry_after=0,                    # no saturation estimate
    usage_percent=session_pct,                    # from dashboard
    usage_stale=False,
    ready=True,
)

# When dashboard data is stale:
ProviderState(
    name="ollama",
    gate_closed_reason="open",
    available_permits=gate.available,
    queue_depth=gate.queue_depth,
    saturation_retry_after=0,
    usage_percent=None,                           # unknown
    usage_stale=True,                             # uncertain
    ready=True,
)
```

### Integration with ProviderContext

When building the ollama `ProviderContext`:
- Use `DashboardTruthSource` as the truth source
- Use `NullTruthSource` as a fallback if the dashboard is not configured
- The reconcile loop uses the `adaptive` controller (AIMD) since there's no
  `/v1/usage`-equivalent for ollama

### Config

```toml
[provider.ollama]
upstream = "http://ollama.ollama.svc.cluster.local:11434"
type = "generic"
target = 1
dashboard_url = "http://usage-dashboard-server.usage-dashboard.svc.cluster.local:8080"
dashboard_token_env = "DASHBOARD_API_KEY"
dashboard_poll_interval = 30.0
dashboard_stale_ttl = 900.0
```

## In-cluster networking

The usage-dashboard service is at:
```
http://usage-dashboard-server.usage-dashboard.svc.cluster.local:8080
```

switchboard needs a bearer token matching the dashboard's `API_KEY` secret.
This is injected via a k8s secret in switchboard's deployment.

## Acceptance criteria

- [ ] `DashboardTruthSource` implements the `TruthSource` protocol
- [ ] Polls `/readings` and extracts the ollama reading
- [ ] Normalizes to `LimitState` with `usage_percent` populated
- [ ] Stale handling: reading older than `stale_ttl` → `usage_stale=True`
- [ ] Routing decision uses `usage_percent` as pressure for ollama
- [ ] When dashboard is unreachable, falls back to `NullTruthSource` behaviour
- [ ] Tests: mock dashboard responses, verify normalization
- [ ] Tests: stale reading → routing decision treats ollama as "uncertain"
