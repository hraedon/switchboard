# Plan 001 — MVP scaffold: pure core, import boundary, single-provider passthrough

**Goal:** Stand up the buildable skeleton with the pure routing core, import
boundary test, and a single-provider passthrough proxy that proves the sluice
integration works end-to-end.

## Scope

- `switchboard.control` — pure routing core data structures and the routing
  decision function (stubbed to "always route to the only provider" for now)
- `switchboard.proxy` — ASGI app that holds one `ProviderContext` and proxies
  requests through it (effectively a re-export of sluice's streaming logic)
- `switchboard.cli` — `switchboard serve --provider umans --upstream ...`
  that boots one provider context and runs the proxy
- Import boundary test: `switchboard.control` imports stdlib only
- Unit tests for the routing decision function

## Deliverables

### `src/switchboard/__init__.py`
- `__version__ = "0.1.0"`

### `src/switchboard/control.py`
Pure, stdlib-only. Contains:

```python
@dataclass(frozen=True)
class ProviderState:
    """Snapshot of one provider's pressure at a point in time."""
    name: str
    gate_closed_reason: str       # "open", "boxed", "breaker", "saturated"
    available_permits: int
    queue_depth: int
    saturation_retry_after: int   # 0 when available
    usage_percent: float | None   # for dashboard-sourced providers
    usage_stale: bool
    ready: bool

@dataclass(frozen=True)
class RouteEntry:
    key: str                       # hashed API key
    providers: tuple[str, ...]     # ordered: [primary, fallback_1, ...]

@dataclass(frozen=True)
class RouteTable:
    entries: dict[str, RouteEntry]
    default_providers: tuple[str, ...]

@dataclass(frozen=True)
class RoutingConfig:
    failover_threshold_seconds: int = 10
    failover_margin: int = 5        # primary pressure must exceed best by this

def route_decision(
    states: dict[str, ProviderState],
    table: RouteTable,
    route_key: str,
    config: RoutingConfig,
    *,
    now: float,
) -> str:
    """Pure routing decision. Returns the provider name to route to."""
    # (implementation per docs/routing-model.md §3)

def hash_route_key(raw_key: str) -> str:
    """SHA-256 hash of the raw API key. Pure."""
    import hashlib
    return hashlib.sha256(raw_key.encode()).hexdigest()
```

### `src/switchboard/providers.py`
```python
@dataclass
class ProviderContext:
    """One upstream provider: gate + reconcile + truth source + HTTP client."""
    name: str
    upstream_url: str
    gate: PermitGate
    reconcile: ReconciliationLoop
    truth_source: TruthSource
    http_client: httpx.AsyncClient

def build_provider_context(
    name: str,
    upstream_url: str,
    provider_type: str,
    api_key: str,
    ...
) -> ProviderContext:
    """Construct a ProviderContext using sluice's building blocks."""
```

### `src/switchboard/proxy.py`
ASGI app. For MVP:
- One `ProviderContext` (single provider, no routing yet)
- Acquire permit from the provider's gate
- Forward using the same streaming logic as sluice's `_forward()`
- Release on completion / disconnect
- Admin routes: `/healthz`, `/readyz`, `/status.json`

The streaming loop is **extracted** from sluice's `proxy.py:_forward()` into a
reusable function that takes `(client, url, gate, reconcile, scope, receive,
send)` as parameters. This extraction is the key refactoring step.

### `src/switchboard/cli.py`
```bash
switchboard serve \
    --provider umans \
    --upstream https://api.code.umans.ai \
    --usage-key-env SLUICE_USAGE_KEY \
    --listen 127.0.0.1:8801
```

### `tests/test_control.py`
- `route_decision` with single provider → always returns that provider
- `route_decision` with two providers, one available, one closed → routes to available
- `route_decision` with two providers, both available → routes to primary
- `route_decision` with primary saturated, fallback available → routes to fallback
- `route_decision` with all providers closed → routes to primary (fail safe)
- `hash_route_key` is deterministic and never returns the raw key

### `tests/test_import_boundary.py`
- `switchboard.control` imports nothing outside stdlib
- `switchboard.proxy` imports `switchboard.control` (one-way)

## Dependencies on sluice

switchboard imports sluice as a Python dependency. For MVP (same monorepo,
editable install), add `sluice` to `pyproject.toml` dependencies or install
it editable:

```bash
cd /projects/sluice && pip install -e .
cd /projects/switchboard && pip install -e ".[dev]"
```

If sluice's `_forward()` needs to be extracted into a reusable module, that
change happens in sluice first (with its own tests), then switchboard consumes
it. If the extraction is too invasive for MVP, switchboard can duplicate the
streaming loop initially and refactor later.

## Acceptance criteria

- [ ] `pytest -q` passes (all control tests + import boundary)
- [ ] `ruff check .` passes
- [ ] `mypy src` passes
- [ ] `switchboard serve --provider umans ...` boots and proxies a request
- [ ] The routing decision function is pure (no I/O, no clock)
- [ ] Import boundary test enforces `control.py` is stdlib-only
