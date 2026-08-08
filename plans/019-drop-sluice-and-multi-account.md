# Plan 019 — Drop sluice dependency, add multi-account support + conversation pinning

> Renumbered from 017 on 2026-08-06: plan 017 was taken by the production
> deployment work that landed on main while this branch sat uncommitted.
> Content is otherwise unchanged from the original author's draft.

Status: **Phase 1 + 2 done via Plan 018/020; Phase 3 (conversation pinning) implemented.**
Phase 1 (drop sluice) and Phase 2 (per-provider credential injection) shipped
under Plans 018 and 020. Phase 3 — `pin_conversations` on `RoutingConfig`,
`extract_conversation_fingerprint`, separated `route_key`/`affinity_key`,
pin-on-first-request, configurable `affinity_max_entries` with an eviction
counter, and the `max_request_body_bytes` finite requirement — is implemented
and under test. The AGENTS.md inert-in-path exception (3) documents the body
read.

Depends on: nothing (this is a foundational refactor). All prior plans'
routing intelligence (006, 008, 010, 012, 013, 014, 015, 016) is preserved
in the pure core. Plan 017 adds `pin_conversations` to `RoutingConfig`
(a tested affinity-policy extension) and absorbs sluice's primitives into
simplified switchboard-owned modules.

## Adversarial review amendments (qwen + sol)

The following findings from the adversarial review are incorporated into
the plan below:

1. **admin.py property surface** (CRITICAL): admin.py's `_provider_status()`
   and `send_prometheus()` consume ~30 reconcile/gate properties. The
   simplified reconcile must either expose stubs for dropped properties
   or admin.py must be updated. → Added WI-8a.
2. **Keep hold sampling** (MAJOR): `saturation_retry_after` depends on
   `gate.avg_hold_seconds`. The simplified gate retains hold sampling
   (~10 lines, no AIMD dependency). → Updated §4.2.
3. **utils.py has 8 functions, not 5** (MAJOR): `build_set_cookie`,
   `check_csrf`, `read_body` are also imported. `send_text` needs
   `content_type` param. → Updated §4.6.
4. **Conversation fingerprint limitations** (MAJOR): identical first
   messages collide; multi-modal/format gaps fall back to API-key hash.
   Documented; "fingerprint miss" metric added. → Updated §6.2, §6.6.
5. **Affinity table sizing** (MAJOR): 1024 is too small for per-
   conversation pinning. `affinity_max` is now configurable. → Updated §6.6.
6. **Breaker 429-class distinction** (MAJOR): only `record_429()`
   (concurrency) feeds the breaker. `record_rate_limit_429()` and
   `record_gateway_429()` are observability-only. → Updated §4.1, §4.4.
7. **Override endpoint hardcodes "target"** (MAJOR): admin override API
   and dashboard must be updated. → Added to WI-8a.
8. **`forward_key_env` missing → fail-closed** (CRITICAL): startup fails
   if `forward_key_env` is configured but the env var is missing/empty.
   Never silently passthrough. → Updated §5.3, WI-12.
9. **Permit calculation double-counts local sessions** (CRITICAL): use
   `external = max(0, observed - local_held)` and
   `capacity = min(max_concurrency, limit - external)`. → Updated §4.4.
10. **Retain header-driven tightening for non-umans providers** (CRITICAL):
    AIMD removal is too aggressive for Anthropic/OpenAI/generic providers
    that rely on in-band rate-limit headers. The simplified reconcile
    retains a static tightening policy: close on stale headers, zero
    headroom, or positive Retry-After. → Updated §4.4.
11. **Body buffering unbounded by default** (MAJOR): require finite
    `max_request_body_bytes` when pinning or model_map is enabled.
    → Added to WI-12, WI-16.
12. **Separate route_key from affinity_key** (MAJOR): `route_key`
    (API-key hash) for table lookup; `affinity_key` (fingerprint or
    API-key hash) for pinning. Never mix. → Updated §6.4.
13. **Stale truth fetch handling** (MAJOR): specify LKG/fail-safe policy.
    Stale data never widens the gate. → Updated §4.4.
14. **Half-open transition is time-based** (MINOR/MAJOR): half-open →
    closed on successful poll (not inference probe), since multi-provider
    routing won't route inference traffic to a CLOSED provider. → Updated
    §4.1.
15. **`limit.py` in import-boundary test** (MINOR): add to pure-modules
    list. → Updated WI-10.
16. **AGENTS.md needs hard rule 6 amendment** (MAJOR): credential
    injection contradicts hard rule 6 (cache-transparency). Amend both
    family conventions AND hard rule 6. → Updated §5.4, §6.5.
17. **Full config migration table** (MAJOR): cover TOML, admin JSON,
    status schema, Prometheus metrics, persistence. → Updated §10.

## 1. Problem

switchboard was built atop sluice as a library dependency. The original
reason — sluice was the single-provider gate and switchboard composed
multiple instances — no longer applies. sluice's AIMD adaptive controller,
history store, singleton guard, and Windows service scaffolding are dead
weight for switchboard's use case. The operator wants:

1. **Multiple opencode go (umans) accounts** — pool several API keys,
   route intelligently across them, inject the selected account's
   credential into forwarded requests.
2. **Conversation pinning** — pin a conversation to the provider it
   started on, only failing over when that provider hits limits.
3. **No sluice dependency** — remove `sluice>=1.3.9,<2.0` from
   pyproject.toml; absorb only the primitives switchboard actually uses.

## 2. Design stance

- **The pure core is untouched.** `control.py` (551 lines, stdlib-only)
  already implements every routing signal: categorical failover, affinity
  dwell/hysteresis, headroom ranking, opportunistic quota burn, token
  budget, 24h usage penalty, model servability, capability filtering. Zero
  changes needed.
- **Replace, don't absorb.** sluice's ~7,200 lines collapse to ~500 lines
  of simplified primitives. Static concurrency limits replace AIMD. A
  simple breaker replaces the full state machine. No history store, no
  adaptive controller, no singleton guard.
- **Opt-in credential injection.** When `forward_key_env` is configured on
  a provider, switchboard strips the client's auth header and injects the
  provider's credential. Mirrors the model-rewrite exception language in
  AGENTS.md.
- **Conversation pinning via first-message fingerprint.** The stable
  identifier across a multi-turn conversation is the first user message
  in the `messages` array. Extract it as a fingerprint, use it as the
  affinity key instead of (or alongside) the API-key hash.

## 3. What switchboard imports from sluice (the absorption surface)

| Sluice module | Lines | What switchboard uses | Replacement |
|---|---|---|---|
| `sluice.gate` | 247 | `PermitGate` — async semaphore with non-blocking + timed acquire, release cooldown, queue depth | `switchboard.gate` (~70 lines): simplified `PermitGate` — same public interface, no reserve, no wait sampling, no p95 |
| `sluice.reconcile` | 1071 | `ReconciliationLoop`, `RETRY_AFTER_SHORT` — poll loop, 429 recording, breaker state, box detection, saturation retry, low-interactivity | `switchboard.reconcile` (~250 lines): simplified loop — poll truth source, resize gate, track breaker/box, record events. No AIMD, no adaptive, no history store, no phantom estimate |
| `sluice.control` | 629 | `LimitState`, `ControllerConfig`, `BreakerConfig`, `AdaptiveConfig`, `BreakerSnapshot`, `adaptive_effective_permits`, breaker functions, `is_low_interactivity`, `is_hard_boxed`, `classify_band` | `switchboard.limit` (~100 lines): `LimitState` (absorbed as-is), `CachedReading`, `RETRY_AFTER_SHORT`. `BreakerConfig`/`BreakerState` simplified. Drop `AdaptiveConfig`, `adaptive_effective_permits`, `ControllerConfig`, `ControllerState`, `effective_permits`, `classify_band`, `phantom_estimate` |
| `sluice.providers` | 366 | `TruthSource` protocol, `PolledTruthSource`, `HeaderTruthSource`, `NullTruthSource`, `Provider`, `get_provider`, `make_truth_source`, `parse_ratelimit_headers` | `switchboard.truth` (~180 lines): same protocol + 3 impls + registry + header parser. `Provider` dataclass simplified (no `controller` field — only one strategy now) |
| `sluice.usage` | 270 | `CachedReading`, `UsageClient`, `parse_usage` | Absorbed into `switchboard.truth` (~80 lines): `UsageClient` → `PolledTruthSource` internals |
| `sluice.admin` | 817 | `check_admin_auth`, `send_json`, `send_text`, `cors_extra_headers`, `is_admin_auth_value` | `switchboard.utils` (~70 lines): 5 utility functions. The rest of sluice.admin (route handlers) are already reimplemented in `switchboard.admin` |
| `sluice.session` | 130 | `SESSION_COOKIE`, `LoginThrottle`, `mint_session`, `verify_session` | `switchboard.session` (~130 lines): copy as-is, rename cookie to `switchboard_session` |
| `sluice.history` | 173 | `History` ring buffer | Drop — only used for trend analysis via `history_store_path`. Without AIMD, there's no consumer |
| `sluice.history_store` | 304 | `SQLiteHistoryStore` | Drop — same reason |

**Net: ~7,200 lines of sluice → ~600 lines of new switchboard modules.**

## 4. New modules

### 4.1 `switchboard/limit.py` (~120 lines)

Pure module (stdlib-only, enforced by import-boundary test).

```python
@dataclass(frozen=True)
class LimitState:
    """Same fields as sluice.control.LimitState — all provider signals unioned."""
    # (copied as-is from sluice/control.py:39-94)

@dataclass
class CachedReading:
    """A usage reading paired with its fetch timestamp and success flag."""
    reading: LimitState
    fetched_at_monotonic: float
    ok: bool

RETRY_AFTER_SHORT = 5

@dataclass(frozen=True)
class BreakerConfig:
    """Simplified breaker: consecutive concurrency-429s → open → cooldown.

    Only record_429() (concurrency-classified) feeds the failure counter.
    record_rate_limit_429() and record_gateway_429() are observability-only
    and do NOT trip the breaker (same distinction as sluice.reconcile).
    """
    failure_threshold: int = 5
    cooldown_seconds: float = 30.0

class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class BreakerSnapshot:
    state: BreakerState
    consecutive_failures: int
    opened_at: float | None
    half_opened_at: float | None = None  # when HALF_OPEN transition occurred
```

**Half-open transition** (review finding 14): half-open → closed on
successful truth-source poll (not inference probe). In multi-provider
routing, the routing core marks a breaker-open provider as CLOSED and
won't route inference traffic to it. The reconcile loop's periodic poll
is the probe: if the poll succeeds (ok=True), the breaker closes. If the
poll fails, the breaker re-opens. This is time-based via
`cooldown_seconds`: after cooldown expires, the next poll acts as the
probe.

### 4.2 `switchboard/gate.py` (~80 lines)

```python
class PermitGate:
    """Simplified async semaphore — non-blocking + timed acquire, queue depth.

    Retains hold sampling (review finding 2): saturation_retry_after
    depends on avg_hold_seconds. ~10 lines, no AIMD dependency.
    """
    def __init__(self, initial_capacity: int, *, wait_window: int = 64) -> None: ...
    async def acquire(self, *, timeout: float) -> bool: ...
    async def release(self, *, hold_seconds: float | None = None) -> None: ...
    async def resize(self, new_capacity: int) -> None: ...
    @property
    def held(self) -> int: ...
    @property
    def capacity(self) -> int: ...
    @property
    def queue_depth(self) -> int: ...
    @property
    def available(self) -> int: ...
    @property
    def avg_hold_seconds(self) -> float: ...  # retained for saturation_retry_after
```

Dropped from sluice.gate: reserve, release cooldown, wait sampling
(was for AIMD), p95, timeouts counter, cooling_down. Retained: hold
sampling (needed by reconcile.saturation_retry_after).

### 4.3 `switchboard/truth.py` (~180 lines)

```python
class TruthSource(Protocol):
    async def fetch(self, *, now_monotonic: float) -> CachedReading: ...
    @property
    def last_cached(self) -> CachedReading | None: ...
    async def close(self) -> None: ...
    def record_response_headers(self, headers, status, *, now_monotonic) -> None: ...

class PolledTruthSource:
    """umans /v1/usage polling — same as sluice.providers.PolledTruthSource."""
    ...

class HeaderTruthSource:
    """In-band ratelimit headers — same as sluice.providers.HeaderTruthSource."""
    ...

class NullTruthSource:
    """No external truth — same as sluice.providers.NullTruthSource."""
    ...

@dataclass(frozen=True)
class Provider:
    """Simplified: no controller field (only one strategy)."""
    name: str
    default_base_url: str
    auth_header: str
    needs_usage_key: bool = False

def get_provider(name: str) -> Provider: ...
def make_truth_source(provider, *, base_url, api_key, auth_header, fresh_ttl) -> TruthSource: ...
def parse_ratelimit_headers(headers, *, provider) -> LimitState: ...
```

### 4.4 `switchboard/reconcile.py` (~300 lines)

```python
class ReconciliationLoop:
    """Simplified poll loop: fetch truth → resize gate → update breaker/box.

    Controller strategy (review finding 9, 10):
    - For polled providers (umans): capacity = min(max_concurrency,
      provider_limit - external_sessions), where external_sessions =
      max(0, observed_concurrent_sessions - local_held). This avoids
      double-counting locally-held requests.
    - For header-driven providers (Anthropic/OpenAI/generic): static
      capacity = max_concurrency, but tighten on: (a) stale headers
      (signal_freshness != FRESH), (b) zero requests_remaining or
      zero tokens_remaining, (c) positive Retry-After on 429. Tightening
      sets capacity to 0 until the next successful poll. This replaces
      AIMD with a fail-safe static policy.
    - Stale truth fetch (review finding 13): when CachedReading.ok is
      False, serve LKG but do NOT widen the gate. If no LKG, resize to 0
      (fail-closed). Never invent zero concurrency.
    """
    def __init__(self, *, truth_source, gate, max_concurrency, poll_interval,
                 breaker_config, provider_type="umans") -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def abort(self) -> None: ...  # public replacement for _stopped (review finding 15)
    def record_429(self) -> None: ...           # feeds breaker
    def record_rate_limit_429(self) -> None: ... # observability only
    def record_gateway_429(self) -> None: ...    # observability only
    def record_success(self) -> None: ...
    def record_response_headers(self, headers, status, *, now_monotonic) -> None: ...
    def record_request_forwarded(self) -> None: ...
    @property
    def ready(self) -> bool: ...
    @property
    def last_fetch_ok(self) -> bool: ...
    @property
    def last_reading(self) -> CachedReading | None: ...
    @property
    def penalty_started_at(self) -> float | None: ...
    def is_low_interactivity(self) -> bool: ...
    def gate_closed_reason(self) -> str: ...  # "open", "boxed", "breaker", "saturated"
    def retry_after_seconds(self) -> int | None: ...
    def saturation_retry_after(self) -> int: ...  # uses gate.avg_hold_seconds
    def apply_override(self, field: str, value: int) -> str | None: ...
    def clear_override(self, field: str) -> None: ...
    # Properties consumed by admin.py status/metrics (review finding 1):
    # Dropped features return stub values (0, None, False, "").
    @property
    def breaker_state(self) -> BreakerState: ...
    @property
    def target(self) -> int: ...  # returns max_concurrency for compat
    @property
    def controller_name(self) -> str: ...  # returns "static"
    @property
    def total_requests_forwarded(self) -> int: ...
```

### 4.5 `switchboard/session.py` (~130 lines)

Copy of `sluice/session.py` with `SESSION_COOKIE = "switchboard_session"`
and `_SESSION_CONTEXT = b"switchboard-session-v1"`.

### 4.6 `switchboard/utils.py` (~100 lines)

```python
def check_admin_auth(scope, admin_token) -> bool: ...
def is_admin_auth_value(value: bytes, admin_token: str | None) -> bool: ...
async def send_json(send, status, data, *, retry_after=None, extra_headers=None) -> None: ...
async def send_text(send, status, text, *, content_type="text/plain", extra_headers=None) -> None: ...
def cors_extra_headers(origin, method) -> dict[str, str]: ...
def build_set_cookie(name: str, value: str, max_age: int) -> str: ...  # review finding 3
def check_csrf(scope, admin_token) -> bool: ...                        # review finding 3
async def read_body(receive) -> bytes: ...                             # review finding 3
```

## 5. Credential injection

### 5.1 Config

```toml
[provider.umans_a]
upstream = "https://api.code.umans.ai"
type = "umans"
usage_key_env = "UMANS_KEY_A"
forward_key_env = "UMANS_KEY_A"    # inject into forwarded requests

[provider.umans_b]
upstream = "https://api.code.umans.ai"
type = "umans"
usage_key_env = "UMANS_KEY_B"
forward_key_env = "UMANS_KEY_B"
```

### 5.2 `ProviderContext` change

```python
@dataclass
class ProviderContext:
    ...
    forward_key: str = ""           # credential to inject, or "" for passthrough
    forward_auth_header: str = ""   # "authorization" or "x-api-key"
```

### 5.3 `ProxyApp._filter_request_headers` / `_forward` change

When the selected provider has `forward_key`:
1. Strip the client's `Authorization` and `x-api-key` headers.
2. Inject the provider's credential: `Authorization: Bearer {forward_key}` or `x-api-key: {forward_key}`.

When `forward_key` is absent, today's byte-transparent behavior is unchanged.

**Fail-closed validation** (review finding 8): if `forward_key_env` is
configured but the env var is missing or empty, startup fails with a
`_ConfigError`. Never silently degrade to passthrough — that would leak
the client's credential to the selected account's domain. If
`forward_key_env` is set but `usage_key_env` is not, warn that usage-
history tracking will be unavailable for this provider (review finding 6).

**Content-Length** (review finding 11): credential injection is header-
only; Content-Length is unaffected. When model_map also rewrites the body,
the existing Content-Length strip in `_forward` handles it independently.

### 5.4 AGENTS.md amendment

Amend both the family-conventions cache-transparency paragraph AND hard
rule 6 (review finding 16). Credential injection contradicts hard rule 6
("plus nothing switchboard-internal"). The amendment:

**Family conventions** — add to the cache-transparency exceptions:

> *(3) When a `[provider]` has `forward_key_env` configured, switchboard
> MAY replace the request's `Authorization` or `x-api-key` header with the
> selected account's credential. This provider's path is explicitly not
> byte-transparent. Byte-identical egress is guaranteed when no
> `forward_key_env` is configured.)*

**Hard rule 6** — amend:

> The request egressed to each upstream must be byte-for-byte what the
> client sent — same body bytes, same headers (minus hop-by-hop and
> switchboard-internal), plus nothing switchboard-internal. *(Exception:
> when `forward_key_env` is configured, switchboard replaces the
> `Authorization` or `x-api-key` header with the selected account's
> credential. When `model` rewrite occurs, the request JSON is
> re-serialised. Both exceptions are per-provider opt-in.)*

## 6. Conversation pinning

### 6.1 Problem

All opencode sessions from one instance share the same API key. The
API-key hash (`hashed_key`) groups all sessions to one affinity entry —
no per-session distribution. The stable identifier across a multi-turn
conversation is the **first user message** in the `messages` array.

### 6.2 Conversation fingerprint

```python
def _extract_conversation_fingerprint(body: bytes) -> str | None:
    """Extract a stable fingerprint from the first user message."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    messages = data.get("messages")
    if not isinstance(messages, list):
        return None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return hashlib.sha256(content.encode("utf-8")).hexdigest()
            if isinstance(content, list):
                # Multi-modal: hash the first text part
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if isinstance(text, str):
                            return hashlib.sha256(text.encode("utf-8")).hexdigest()
    return None
```

This requires reading the request body — same opt-in gating as the
model_map feature. When `pin_conversations = true`, the body is buffered
(the same buffer already used for model_map), the fingerprint is
extracted, and it becomes the affinity key instead of `hashed_key`.

### 6.3 Pure core change (small)

```python
@dataclass(frozen=True)
class RoutingConfig:
    ...
    pin_conversations: bool = False
    # When True, the proxy pins every first request to the selected
    # provider and does not failback unless the pinned provider drops
    # out of immediate (hits limits). The affinity key is the
    # conversation fingerprint, not the API-key hash.
```

In `route_decision`, when `pin_conversations=True` and an affinity pin is
active:
- The pinned provider stays front of `immediate` as long as it's FRESH
  and in `immediate` (AVAILABLE).
- No failback to primary — the primary concept is table order only.
- When the pinned provider drops to BUSY/CLOSED/UNKNOWN, normal failover
  selects the next best and re-pins.

This is a minimal change to the existing affinity block: the
`pin_conversations` flag suppresses the `elif primary in immediate:
failback()` branch.

### 6.4 Shell change

In `ProxyApp._proxy_request`:
- **Separate `route_key` from `affinity_key`** (review finding 12):
  ```python
  route_key = hash_route_key(raw_key)           # for table lookup + metrics
  affinity_key = conversation_fingerprint or route_key  # for pinning
  ```
  `route_key` goes to `RouteTableManager.lookup()` and
  `metrics.record_decision()`. `affinity_key` goes to
  `self._affinity[affinity_key]`. Never mix.
- When `pin_conversations=True`, buffer the body (reuse the existing
  model_map buffering path).
- Extract the conversation fingerprint.
- Use `affinity_key` for the affinity table; set affinity on **every**
  first request (when no existing affinity entry exists for this
  `affinity_key`), not just on failover.
- On subsequent requests, the existing affinity lookup finds the pin.

**Body limit requirement** (review finding 11): when `pin_conversations`
or `model_map` is enabled, `max_request_body_bytes` must be set to a
finite value. If it's `None` (unbounded), startup validation fails. This
prevents memory exhaustion from unbounded body buffering.

**Affinity table sizing** (review finding 5): `affinity_max` is
configurable via `[routing] affinity_max_entries` (default 1024 for
API-key-hash mode, recommended 8192 for conversation pinning). An
eviction counter is surfaced in `/status.json` so operators can detect
pin loss.

### 6.5 AGENTS.md amendment

Add to the inert-in-path exceptions:

> *(3) When `[routing] pin_conversations` is configured, switchboard MAY
> read the request body's `messages` array to extract a conversation
> fingerprint (hash of the first user message's text content). No other
> field is read or altered. Request body bytes forwarded to the upstream
> are unchanged. This exception composes with exception (1) — when both
> `pin_conversations` and `[model]` are configured, each reads only its
> designated field. This exception applies only when
> `pin_conversations = true`; without it, switchboard does not read the
> request body for pinning purposes.)*

### 6.6 Limitations and metrics

**Documented limitations** (review finding 4):
- Pinning works only for chat-completions format (`messages` array with
  `role: "user"` entries). Completions (`prompt`), Gemini (`contents`),
  and other formats fall back to API-key-hash affinity.
- Conversations with identical first user messages (e.g., "Hello",
  "Review this") share a pin. This is acceptable — both conversations
  are routed to the same provider, which is still a valid routing
  decision.
- Image-only first messages (no text part) fall back to API-key-hash.
- If a client rewrites the first user message (context-window trimming,
  summarization), the conversation gets a new pin. This is the correct
  behavior: a rewritten conversation has different cache state anyway.
- Restart discards all pins (affinity is in-memory). Conversations
  resume on whichever provider the routing core selects, then re-pin.

**New metrics:**
- `fingerprint_misses_total` — incremented when fingerprint extraction
  fails (body not JSON, no messages array, no user message, no text).
  Surfed in `/status.json` and `/metrics`.
- `affinity_evictions` — incremented when an affinity entry is LRU-
  evicted. Already tracked as `evicted_decisions` pattern; add explicit
  counter for affinity table.

## 7. Config

```toml
[provider.umans_a]
upstream = "https://api.code.umans.ai"
type = "umans"
max_concurrency = 3
usage_key_env = "UMANS_KEY_A"
forward_key_env = "UMANS_KEY_A"

[provider.umans_b]
upstream = "https://api.code.umans.ai"
type = "umans"
max_concurrency = 3
usage_key_env = "UMANS_KEY_B"
forward_key_env = "UMANS_KEY_B"

[provider.ollama]
upstream = "http://localhost:11434"
type = "generic"
max_concurrency = 2

[route.default]
providers = ["umans_a", "umans_b", "ollama"]

[routing]
pin_conversations = true
dwell_interval = 30.0
failback_delay = 0.0    # unused when pin_conversations=true
```

## 8. Work items

### Phase 1: Absorb sluice primitives (no behavior change)

- **WI-1** `switchboard/limit.py`: `LimitState` (copy from sluice),
  `CachedReading`, `BreakerConfig`, `BreakerState`, `BreakerSnapshot`,
  `RETRY_AFTER_SHORT`. Add to import-boundary pure-modules test. Unit
  test: frozen dataclass, defaults, breaker state transitions.
- **WI-2** `switchboard/gate.py`: `PermitGate` (simplified — no reserve,
  no cooldown, no wait sampling; **retain hold sampling** for
  `saturation_retry_after`). Unit test: acquire/release/resize, non-
  blocking timeout, queue_depth, avg_hold_seconds.
- **WI-3** `switchboard/truth.py`: `TruthSource` protocol, `PolledTruthSource`,
  `HeaderTruthSource`, `NullTruthSource`, `Provider`, `get_provider`,
  `make_truth_source`, `parse_ratelimit_headers`. Unit tests: each truth
  source fetch/record, header parser, LKG/fail-safe behavior.
- **WI-4** `switchboard/reconcile.py`: `ReconciliationLoop` (simplified —
  static max_concurrency with external-session subtraction for polled
  providers, header-driven tightening for non-umans, simple breaker with
  429-class distinction, no AIMD/history). Unit test: poll loop resizes
  gate, permit formula (external = observed - local_held), breaker opens
  only on concurrency 429s, box detection, saturation_retry_after,
  low_interactivity, ready/last_fetch_ok, stale-data-never-widens,
  abort() method.
- **WI-5** `switchboard/session.py`: copy from sluice, rename cookie.
- **WI-6** `switchboard/utils.py`: 8 admin utility functions (including
  `build_set_cookie`, `check_csrf`, `read_body`; `send_text` with
  `content_type` param).
- **WI-7** Update `providers.py`: import from new modules instead of sluice.
  Add `forward_key` / `forward_auth_header` to `ProviderContext`. Resolve
  `forward_key_env` from config. Replace `target` with `max_concurrency`
  (accept `target` as deprecated alias).
- **WI-8** Update `proxy.py`, `dashboard.py`, `estimator.py`, `cli.py`:
  replace all `from sluice.*` imports with new module imports.
- **WI-8a** **Audit and update `admin.py`** (review finding 1): map every
  `ctx.reconcile.*` and `ctx.gate.*` property consumed by
  `_provider_status()` and `send_prometheus()`. For dropped features
  (AIMD, history, phantom, wait sampling), either expose stubs
  (returning 0/None/False) or remove the status/metrics fields and
  update the dashboard HTML. Update override endpoint: `"target"` →
  `"max_concurrency"` (accept both during deprecation).
- **WI-9** Update `pyproject.toml`: remove `sluice` dependency.
- **WI-10** Update `tests/test_import_boundary.py`: remove sluice-specific
  tests, add `switchboard.limit` to pure-modules list, add boundary tests
  for new modules (gate/reconcile = shell w/ asyncio, no httpx; truth =
  shell w/ httpx; session = stdlib-only).

### Phase 2: Credential injection

- **WI-11** `ProxyApp._filter_request_headers` / `_forward`: when
  `forward_key` is set, strip client auth (both `Authorization` AND
  `x-api-key`) and inject provider credential. Integration test:
  forward_key set → upstream receives injected credential; forward_key
  absent → upstream receives client credential (unchanged).
- **WI-12** Config validation: `forward_key_env` must resolve to non-empty
  (fail startup if missing — review finding 8). Warn if `forward_key_env`
  set but `usage_key_env` not set (usage-history unavailable). Require
  finite `max_request_body_bytes` when pinning or model_map enabled
  (review finding 11). Startup log: show which providers have forward_key.
- **WI-13** AGENTS.md amendment for credential injection (both family
  conventions AND hard rule 6).

### Phase 3: Conversation pinning

- **WI-14** `switchboard/control.py`: `pin_conversations` on
  `RoutingConfig`; suppress failback branch when True and affinity is
  active. Unit tests: pin_conversations=False → today's behavior; True
  with active pin → no failback; True + pinned provider drops → failover
  + re-pin; True + no pin → set pin on first request.
- **WI-15** `ProxyApp._proxy_request`: buffer body when
  `pin_conversations=True`; extract fingerprint; use `affinity_key`
  (fingerprint or route_key) for pinning — keep `route_key` separate for
  table lookup and metrics (review finding 12). Set affinity on every
  first request. Add `fingerprint_misses_total` and `affinity_evictions`
  metrics. Integration test: two conversations with different first
  messages → pinned to different providers; multi-turn → same provider.
- **WI-16** Config: `[routing] pin_conversations` and
  `[routing] affinity_max_entries` parse + startup log. Require finite
  `max_request_body_bytes` when pinning enabled.
- **WI-17** AGENTS.md amendment for conversation fingerprint exception
  (compose with model_map exception language).

### Phase 4: Cleanup + docs

- **WI-18** `docs/routing-model.md`: update §6 (provider contexts), §9
  (add conversation pinning to the model). Add §10 for multi-account
  configuration.
- **WI-19** Full test suite: `pytest -q`, `ruff check .`, `mypy src`.
  Verify import boundary: `control.py` still stdlib-only.
- **WI-20** Remove `sluice` from `pyproject.toml` dependencies. Verify
  `pip install -e ".[dev]"` works without sluice installed.

## 9. What this plan deliberately does not do

- **No AIMD / adaptive controller.** Static `max_concurrency` per provider.
  If a provider's concurrency limit changes, the operator updates config
  and restarts. The truth source still polls `/v1/usage` for box/breaker/
  low-interactivity detection — just doesn't resize the gate adaptively.
- **No history store / trend analysis.** `history_store_path` and the
  `SQLiteHistoryStore` are dropped. `History` ring buffer is dropped. The
  threshold estimator (`switchboard/threshold.py`) and usage history
  tracker (`switchboard/usage_history.py`) remain — they fetch from the
  provider's API, not from sluice's history store.
- **No reserve / release cooldown.** The simplified gate is a plain
  semaphore. These were AIMD-era optimizations.
- **No singleton guard.** sluice's `SingletonGuard` (HA leader election)
  is not used by switchboard.
- **No Windows service scaffolding.** sluice's `win_service.py` is not
  used by switchboard.
- **No cross-format translation.** Same as AGENTS.md hard rule 5.
- **No per-session conversation ID header.** The fingerprint is extracted
  from the request body, not from a client-supplied header. This avoids
  requiring opencode cooperation.

## 10. Migration path

Full migration table (review finding 17):

| Surface | Old | New | Transition |
|---|---|---|---|
| TOML `[provider.*]` | `target = 3` | `max_concurrency = 3` | Accept `target` as deprecated alias for one release; warn on startup |
| Admin override API | `{"target": 3}` | `{"max_concurrency": 3}` | Accept both during deprecation |
| `/status.json` | `target`, `min_floor`, `controller`, `phantom_estimate`, `band`, `avg_wait_seconds`, `p95_wait_seconds`, `cooling_down`, `history` | `max_concurrency`, `controller: "static"`; drop phantom/band/wait/p95/cooling/history | Remove dropped fields; update dashboard HTML |
| `/metrics` | `sluice_*` prefixed metrics | `switchboard_*` prefixed | Rename; update any dashboards/alerts |
| Persistence | `*.history` sidecar files (SQLiteHistoryStore) | Dropped | Old files are harmless orphans; document cleanup |
| Python API | `from sluice.*` | `from switchboard.*` | No compat layer — switchboard is the only consumer |
| `pyproject.toml` | `sluice>=1.3.9,<2.0` | removed | `pip install -e ".[dev]"` works without sluice |
| Config | `history_store_path` (from route-table store) | Dropped | Remove from config; no error if still present (ignored) |

The `sluice` dependency in `pyproject.toml` is removed. Operators who
still run sluice standalone (not via switchboard) are unaffected — sluice
remains as its own package.

## 11. Risk assessment

| Risk | Mitigation |
|---|---|
| Simplified reconcile breaks box/breaker detection | Unit tests port the exact detection logic from sluice.reconcile; integration tests verify 429 → breaker open → cooldown → probe |
| Permit calculation double-counts local sessions | Use `external = max(0, observed - local_held)` formula; unit test with simulated local holds |
| Header-driven providers lose AIMD safety | Static tightening policy: close on stale headers, zero headroom, positive Retry-After. Unit test all three conditions. |
| Conversation fingerprint is unstable (client rewrites first message) | Documented as expected behavior: rewritten conversation = new pin. Fingerprint-miss metric detects silent failures. |
| Identical first messages collide | Acceptable: both conversations route to the same provider — still a valid routing decision. |
| Body buffering unbounded | Require finite `max_request_body_bytes` when pinning/model_map enabled; startup validation fails if unbounded. |
| Credential injection leaks keys in logs | `forward_key` is never logged. The proxy logs provider *name*, not credential. Same posture as `usage_api_key` today. |
| Missing `forward_key_env` env var | Fail-closed: startup fails with `_ConfigError`. Never silently passthrough. |
| Affinity table eviction causes flapping | Configurable `affinity_max_entries`; eviction counter surfaced in status. |
| Removing sluice breaks downstream consumers | switchboard is the only consumer. sluice remains as its own package for standalone use. |
