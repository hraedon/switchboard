# Plan 002 — Multi-provider gates: multiple ProviderContexts, route table lookup

**Goal:** Extend the proxy to hold multiple `ProviderContext` instances and
select which one to forward to based on the route table.

## Prerequisite

- Plan 001 complete (skeleton, pure core, single-provider passthrough)

## Scope

- `ProviderContext` registry: `dict[str, ProviderContext]` in the proxy
- Route table: API-key-hash → ordered provider list
- Route key extraction from request headers (`Authorization` / `x-api-key`)
- Routing decision called per request, selecting the provider context
- Per-provider lifecycle: start/stop all reconcile loops
- Per-provider status in `/status.json`
- CLI flag for multi-provider config: `--provider-config <toml>`

## Deliverables

### Route table (`src/switchboard/route_table.py`)

```python
class RouteTableManager:
    """In-memory route table with CRUD + optional SQLite persistence."""
    def lookup(self, hashed_key: str) -> tuple[str, ...]:
        """Return ordered provider list for a hashed key, or default."""
    def add_entry(self, hashed_key: str, providers: list[str]) -> None:
    def remove_entry(self, hashed_key: str) -> None:
    def list_entries(self) -> list[RouteEntry]:
```

### Multi-provider proxy (`src/switchboard/proxy.py`)

The proxy's `__call__`:
1. Extract the route key from `Authorization` or `x-api-key` header
2. Hash it
3. Look up the candidate provider list
4. Snapshot each candidate's `ProviderState` from its reconcile loop
5. Call `route_decision()` → provider name
6. Acquire a permit from the selected provider's gate
7. Forward using the streaming loop to the selected provider's upstream URL
8. Release on completion / disconnect

### Provider config format (`docs/provider-config.md`)

TOML file defining multiple providers:

```toml
[provider.umans]
upstream = "https://api.code.umans.ai"
type = "umans"
usage_key_env = "SLUICE_USAGE_KEY"
target = 3

[provider.ollama]
upstream = "http://ollama.ollama.svc.cluster.local:11434"
type = "generic"
target = 1

[route.default]
providers = ["umans", "ollama"]

[route.key_abc123]
providers = ["umans", "ollama"]
# hashed key for a specific API key
```

### Lifecycle

- `LifecycleManager` fans out start/stop to all provider reconcile loops
- On startup: build all `ProviderContext` instances, start all loops
- On shutdown: stop all loops, close all HTTP clients, drain all gates
- Singleton guard is per-instance (one switchboard pod), not per-provider

### Status

`/status.json` returns per-provider state:

```json
{
  "providers": {
    "umans": {
      "gate_closed_reason": "open",
      "effective_permits": 3,
      "in_flight": 1,
      "queue_depth": 0,
      "band": "normal",
      "breaker": "closed"
    },
    "ollama": {
      "gate_closed_reason": "open",
      "effective_permits": 1,
      "in_flight": 0,
      "queue_depth": 0
    }
  },
  "route_table": {
    "default": ["umans", "ollama"]
  }
}
```

## Acceptance criteria

- [ ] Proxy holds 2+ `ProviderContext` instances, each with its own gate
- [ ] Route key extracted from request headers, hashed, looked up
- [ ] `route_decision()` called per request, selects the provider
- [ ] Request forwarded to the selected provider's upstream
- [ ] All reconcile loops start/stop correctly
- [ ] `/status.json` shows per-provider state
- [ ] Tests: two providers, primary saturated → routes to fallback
- [ ] Tests: route key not in table → uses default providers
- [ ] Tests: all providers closed → routes to primary (fail safe)
