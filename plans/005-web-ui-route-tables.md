# Plan 005 — Web UI: route table CRUD dashboard

**Goal:** Admin web UI for managing the route table at runtime — add/remove
route entries, configure per-route failover thresholds, view live per-provider
state.

## Prerequisite

- Plan 004 complete (routing engine with metrics)

## Scope

- Admin routes: `GET/POST /admin/routes`, `DELETE /admin/routes/<key>`
- Dashboard HTML: route table view, per-provider status, routing metrics
- Per-route failover threshold configuration
- Optional SQLite persistence for the route table (survives restarts)
- Authentication (same session-cookie pattern as sluice)

## Deliverables

### Admin routes (`src/switchboard/admin.py`)

```
GET  /admin/routes          → list all route entries + live provider states
POST /admin/routes          → add/update a route entry
DELETE /admin/routes/<key>  → remove a route entry
GET  /admin/config          → current routing config
POST /admin/config          → update routing config (thresholds, margins)
```

### Route entry CRUD

```json
// POST /admin/routes
{
  "key": "<raw API key>",
  "providers": ["umans", "ollama"],
  "failover_threshold": 10,
  "failover_margin": 5
}
```

The server hashes the key before storing. The raw key is never persisted or
logged. The response returns the hashed key for future reference.

### Dashboard HTML (`src/switchboard/static/`)

Patina design system (same as sluice's dashboard):

- **Provider cards**: per-provider gate state, permits, in-flight, band,
  breaker, usage percent
- **Route table**: list of entries showing hashed key (truncated), provider
  list, failover thresholds
- **Routing metrics**: forward counts per provider, failover count, last
  routing decision per route key
- **Add route form**: API key input, provider multiselect, threshold sliders

### Persistence

Optional SQLite store for the route table:

```python
class RouteTableStore:
    """SQLite-backed route table persistence."""
    def load(self) -> RouteTable: ...
    def save_entry(self, entry: RouteEntry) -> None: ...
    def delete_entry(self, key: str) -> None: ...
```

When configured (`--route-table-store /data/routes.db`), route entries survive
restarts. Without it, the route table is in-memory only and re-seeded from the
provider config file on startup.

### Authentication

Same pattern as sluice:
- `--admin-token` flag
- Session cookie minted on login (`POST /login`)
- All admin routes require auth
- Route table mutations require auth
- `/status.json` and `/metrics` require auth

### Config reload

Same SIGHUP pattern as sluice: re-read the TOML config file and apply
safe-to-reload changes (routing thresholds, poll intervals) without restart.
Adding/removing providers requires a restart (same as sluice's constraint on
upstream URL changes).

## Acceptance criteria

- [ ] `GET /admin/routes` returns all route entries
- [ ] `POST /admin/routes` adds/updates a route entry
- [ ] `DELETE /admin/routes/<key>` removes a route entry
- [ ] Dashboard HTML renders per-provider state + route table
- [ ] Dashboard allows adding/removing routes via form
- [ ] Route table persists to SQLite when configured
- [ ] Admin auth (session cookie) gates all mutations
- [ ] SIGHUP reloads routing config thresholds without restart
- [ ] Tests: CRUD operations on the route table
- [ ] Tests: auth required for mutations
