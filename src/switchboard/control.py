"""Pure, deterministic routing core — the truth path.

This module is the routing decision engine. It imports **nothing outside the
standard library**, does **no I/O**, and reads **no clock**: the current time
and every provider state are passed in as arguments so decisions are fully
reproducible and unit-testable without a network.

Enforced by tests/test_import_boundary.py.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
from dataclasses import dataclass, field
from enum import Enum


class Availability(Enum):
    """Provider availability state for routing decisions.

    Categorical eligibility replaces the scalar pressure comparison from
    Plans 001-005.  The decision proceeds: filter out CLOSED, separate by
    signal freshness, then place AVAILABLE candidates ahead of BUSY ones.
    """

    AVAILABLE = "available"  # eligible and permit available now
    BUSY = "busy"  # eligible but no permit available now
    CLOSED = "closed"  # boxed, breaker-open, administratively closed
    UNKNOWN = "unknown"  # not ready or signal too stale to trust


class SignalFreshness(Enum):
    """How fresh the provider's truth signal is.

    Staleness semantics (Plan 006 §3.2):

    * ``FRESH`` — may be selected or preferred normally.
    * ``DEGRADED`` — last-known-good may keep an already-primary route serving
      within a bounded TTL, but it is not a new failover target.
    * ``UNKNOWN`` — excluded from failover preference; admitted only under an
      explicit route policy.  Unknown data never maps to zero pressure.
    """

    FRESH = "fresh"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class RoutingStrategy(Enum):
    """How to order immediate candidates (Plan 020 Wave 4, D5).

    * ``ORDERED`` — table order (default, current behavior). The primary fronts
      unless an affinity pin or opportunistic burn overrides it.
    * ``HEADROOM`` — order by ``usage_headroom`` descending (Plan 015). Data-
      bearing candidates precede ones without headroom data; ties break on
      table order. Same as ``headroom_ranking = True``.
    * ``PACE`` — order by quota surplus descending (Plan 020 D5). A provider
      that will not plausibly spend its remaining quota before reset is
      use-it-or-lose-it and should be burned first. Only providers with a FRESH
      weekly-window signal are scored; unscored providers rank after scored
      ones in table order (they are never starved). ``pace_flap_margin`` is a
      deadband, not hysteresis with memory: it compares the top two candidates
      rather than the currently-serving one, which is enough to stop
      per-request alternation between near-equal providers.
    """

    ORDERED = "ordered"
    HEADROOM = "headroom"
    PACE = "pace"


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declarative provider capability metadata (Plan 008 §4).

    Routes declare required capability surfaces; the router filters
    incompatible candidates before pressure/admission ranking.  No request
    body inspection is performed.
    """

    surfaces: frozenset[str]  # e.g. {"chat-completions", "messages"}
    api_family: str  # exact wire contract identifier
    streaming: bool = True
    tool_calling_profile: str | None = None
    context_class: str | None = None
    credential_domain: str = ""
    cache_domain: str = ""


@dataclass(frozen=True)
class RouteAffinity:
    """Bounded route affinity state for stickiness/failback (Plan 008 §5).

    Supplied explicitly to the pure core by the proxy.  The pure function
    uses this as input only — it does not update affinity state.  The caller
    (proxy) updates affinity after failover or failback.
    """

    provider: str
    selected_at: float
    failover_reason: str = ""
    healthy_observations: int = 0


@dataclass(frozen=True)
class ProviderState:
    """Snapshot of one provider's state at a point in time.

    Assembled by the shell from each provider's reconcile loop and gate.
    Pure data — no I/O, no clock.
    """

    name: str
    availability: Availability
    available_permits: int
    queue_depth: int
    retry_after_seconds: int | None
    signal_freshness: SignalFreshness
    capabilities: ProviderCapabilities | None = None
    usage_headroom: float | None = None
    quota_resets_in: float | None = None
    # Seconds until the quota window this headroom refers to resets.
    # None = unknown (never promotes -- fail safe).
    token_utilization: float | None = None
    # Per-provider token-budget soft threshold (Plan 012 §4): the utilization
    # fraction at which this provider starts bleeding traffic to alternatives.
    # When set, it overrides the global ``token_budget_threshold`` for this
    # provider.  None = fall back to the global threshold (default 0.0 = off).
    token_soft_threshold: float | None = None
    usage_24h_utilization: float | None = None
    # tokens_24h / cap_tokens (Plan 013). 0.0 = none used; 1.0 = at cap.
    # None = no 24h budget configured or no data (no filtering).
    weekly_remaining_fraction: float | None = None
    # Remaining fraction of the WEEKLY quota window (Plan 020 D6). 1.0 = full,
    # 0.0 = exhausted. None = no weekly signal. The pace strategy scores on
    # this; the session-window ``usage_headroom`` stays a separate signal.
    weekly_reset_in: float | None = None
    # Seconds until the weekly quota window resets (Plan 020 D6). None =
    # unknown. The pace surplus formula uses this to compute the expected
    # burn-down against a nominal ``burn_rate_per_day``.


@dataclass(frozen=True)
class RouteEntry:
    """A route table entry mapping a hashed key to an ordered provider list."""

    key: str  # SHA-256 hash of the raw API key
    providers: tuple[str, ...]  # ordered: [primary, fallback_1, ...]
    required_capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RouteTable:
    """The full route table. Entries + a default provider list."""

    entries: dict[str, RouteEntry] = field(default_factory=dict)
    default_providers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelMap:
    """Per-provider model-name aliases (Plan 010 Feature B).

    Providers label the same model differently (umans ``umans-kimi-k2.7`` vs
    ollama-cloud ``kimi-k2.7-code``).  ``routes`` maps the **incoming** model
    string (what the client sends) to ``{provider_name: that provider's model
    string}``.  Used for two things:

    * **Candidate filtering** — only providers with an alias for the requested
      model can serve it, so failover never routes a model to a provider that
      doesn't offer it.
    * **Egress rewrite** — the ``model`` field is rewritten to the chosen
      provider's alias (only when it differs, so the primary path stays
      byte-identical).

    A model absent from ``routes`` is not filtered or rewritten — switchboard
    behaves exactly as today (forward original bytes).  Empty ``routes`` = the
    whole feature is off.
    """

    routes: dict[str, dict[str, str]] = field(default_factory=dict)

    def __contains__(self, model: str) -> bool:
        return model in self.routes

    def providers_for(self, model: str) -> frozenset[str]:
        """Providers that declare an alias for ``model`` (empty if unmapped)."""
        entry = self.routes.get(model)
        return frozenset(entry.keys()) if entry else frozenset()

    def alias_for(self, model: str, provider: str) -> str | None:
        """The model string ``provider`` expects for ``model``, or None."""
        entry = self.routes.get(model)
        return entry.get(provider) if entry else None


@dataclass(frozen=True)
class RoutingConfig:
    """Routing engine parameters.

    ``failover_threshold_seconds`` and ``failover_margin`` are retained for
    display and potential future tie-breaking but are no longer the primary
    decision mechanism (Plan 006 replaced scalar pressure comparison with
    categorical eligibility).

    ``dwell_interval`` (Plan 008 §5) is the minimum time in seconds to stay
    on a fallback before failing back to the primary.

    ``failback_delay`` (Plan 014) is the minimum continuous time in seconds
    the primary must be FRESH+AVAILABLE before an affinity pin is released.
    0.0 = disabled (Plan 008 §5 behaviour: fail back on the first healthy
    poll after ``dwell_interval``).
    """

    failover_threshold_seconds: int = 10
    failover_margin: int = 5
    dwell_interval: float = 30.0
    failback_delay: float = 0.0
    headroom_threshold: float = 0.0
    headroom_ranking: bool = False
    # Order `immediate` candidates by usage_headroom (descending) before
    # affinity/primary fronting. Providers without headroom data sort after
    # data-bearing ones, in table order.
    token_budget_threshold: float = 0.0
    usage_24h_threshold: float = 0.0
    # 0.0 = disabled. >0 = providers whose usage_24h_utilization >= this are
    # demoted from immediate to queue_eligible — INCLUDING the primary
    # (Plan 013 §2: the trailing-24h penalty is what the primary's gate
    # cannot see coming, so the usual no-primary-demotion rule does not
    # apply to this signal).
    opportunistic_enabled: bool = False
    opportunistic_min_headroom: float = 0.5
    # only when >= half the window remains
    opportunistic_reset_window: float = 21600.0
    # seconds; only inside the last 6 h
    opportunistic_margin: float = 0.10
    # winner must lead the runner-up by this
    pin_conversations: bool = False
    # When True, the proxy pins each conversation (by a fingerprint of its
    # first user message) to the selected provider and does NOT fail back to
    # the primary while the pinned provider stays in ``immediate`` (FRESH and
    # not demoted to queue-eligible).  When the pinned provider drops to
    # BUSY/CLOSED/UNKNOWN (or is demoted by headroom/budget/24h signals),
    # normal failover selects the next best and re-pins.  The affinity key is
    # the conversation fingerprint, not the API-key hash (Plan 019 §6).
    affinity_max_entries: int = 1024
    # Bounded LRU affinity table size.  API-key-hash mode (default) needs ~1k;
    # conversation pinning (many concurrent conversations) benefits from 8k+.
    # An eviction counter is surfaced so operators can detect pin loss.
    strategy: RoutingStrategy = RoutingStrategy.ORDERED
    # How to order immediate candidates (Plan 020 Wave 4 D5). Default
    # ``ORDERED`` preserves today's behavior. ``PACE`` ranks by quota surplus
    # (use-it-or-lose-it); ``HEADROOM`` is equivalent to ``headroom_ranking``.
    pace_burn_rate_per_day: float = 0.14
    # Nominal daily burn-down of the weekly quota (Plan 020 D5). Paul's number:
    # ~a weekly quota consumed evenly, 100%/7d ≈ 14.3%. The surplus formula is
    # ``remaining_fraction - burn_rate_per_day * days_until_reset``.
    pace_flap_margin: float = 0.05
    # Minimum surplus advantage the leader must hold over the runner-up to
    # re-rank (Plan 020 D5 deadband). Prevents two near-equal providers from
    # alternating per-request. Default 0.05 = 5 percentage points.


@dataclass(frozen=True)
class FieldBound:
    """An inclusive/exclusive numeric range for one routing config field."""

    minimum: float | None = None
    minimum_inclusive: bool = True
    maximum: float | None = None
    maximum_inclusive: bool = True
    integer: bool = False

    def describe(self) -> str:
        """Render the range in interval notation, e.g. ``[0.0, 1.0)``."""
        if self.minimum is None and self.maximum is None:
            return "any number"
        if self.maximum is None:
            op = ">=" if self.minimum_inclusive else ">"
            return f"{op} {self.minimum}"
        if self.minimum is None:
            op = "<=" if self.maximum_inclusive else "<"
            return f"{op} {self.maximum}"
        left = "[" if self.minimum_inclusive else "("
        right = "]" if self.maximum_inclusive else ")"
        return f"in {left}{self.minimum}, {self.maximum}{right}"


# The single source of truth for routing-field ranges.  Both config surfaces
# read it: TOML validation (``switchboard config validate`` / boot) and the
# admin API (``PUT /admin/config/routing``).  They used to carry independent
# copies of these bounds and had silently drifted apart — the API rejected
# ``headroom_threshold = 0.0`` (the documented "disabled" value) and
# ``pace_burn_rate_per_day = 1.0``, both of which TOML accepted.  Keep this
# table as the only place a bound is written down; ``test_config_surfaces``
# asserts the two surfaces agree.
ROUTING_FIELD_BOUNDS: dict[str, FieldBound] = {
    "failover_threshold_seconds": FieldBound(minimum=0, integer=True),
    "failover_margin": FieldBound(minimum=0, integer=True),
    "dwell_interval": FieldBound(minimum=0.0),
    "failback_delay": FieldBound(minimum=0.0),
    "affinity_max_entries": FieldBound(minimum=1, integer=True),
    "headroom_threshold": FieldBound(minimum=0.0, maximum=1.0),
    "token_budget_threshold": FieldBound(minimum=0.0, maximum=1.0),
    "usage_24h_threshold": FieldBound(minimum=0.0, maximum=1.0),
    "opportunistic_min_headroom": FieldBound(
        minimum=0.0, minimum_inclusive=False, maximum=1.0,
    ),
    "opportunistic_reset_window": FieldBound(
        minimum=0.0, minimum_inclusive=False,
    ),
    "opportunistic_margin": FieldBound(
        minimum=0.0, maximum=1.0, maximum_inclusive=False,
    ),
    "pace_burn_rate_per_day": FieldBound(minimum=0.0, maximum=1.0),
    "pace_flap_margin": FieldBound(
        minimum=0.0, maximum=1.0, maximum_inclusive=False,
    ),
}

# Routing fields that are booleans rather than numbers.
ROUTING_BOOL_FIELDS: frozenset[str] = frozenset(
    {"headroom_ranking", "opportunistic_enabled", "pin_conversations"}
)

# Valid ``[routing] strategy`` values, derived from the enum so a new
# strategy cannot be added without both surfaces accepting it.
ROUTING_STRATEGIES: tuple[str, ...] = tuple(s.value for s in RoutingStrategy)


# Routing fields ``PUT /admin/config/routing`` will apply, in the order errors
# are reported.  Everything else in RoutingConfig is rejected at runtime:
# ``affinity_max_entries`` (resizing the live table would evict active pins),
# ``pin_conversations`` (it decides whether the proxy buffers request bodies,
# which is a startup decision), and ``failover_threshold_seconds`` /
# ``failover_margin`` (retained for display only).
MUTABLE_ROUTING_FIELDS: tuple[str, ...] = (
    "strategy",
    "pace_burn_rate_per_day",
    "pace_flap_margin",
    "dwell_interval",
    "failback_delay",
    "headroom_threshold",
    "headroom_ranking",
    "token_budget_threshold",
    "usage_24h_threshold",
    "opportunistic_enabled",
    "opportunistic_min_headroom",
    "opportunistic_reset_window",
    "opportunistic_margin",
)


def validate_routing_field(field: str, value: object) -> str | None:
    """Validate one routing field's value against :data:`ROUTING_FIELD_BOUNDS`.

    Returns an error message *without* a surface-specific prefix (the caller
    adds ``routing.`` for TOML), or ``None`` when the value is acceptable.
    Fields with no entry in the table are not range-checked here and return
    ``None`` — the caller is responsible for anything structural.
    """
    if field == "strategy":
        if not isinstance(value, str) or value not in ROUTING_STRATEGIES:
            joined = ", ".join(ROUTING_STRATEGIES)
            return f"{field} must be one of: {joined}"
        return None

    if field in ROUTING_BOOL_FIELDS:
        if not isinstance(value, bool):
            return f"{field} must be a boolean"
        return None

    bound = ROUTING_FIELD_BOUNDS.get(field)
    if bound is None:
        return None

    # bool is a subclass of int; a boolean is never a valid number here.
    if isinstance(value, bool):
        return f"{field} must be {'an integer' if bound.integer else 'a number'}"
    if bound.integer:
        if not isinstance(value, int):
            return f"{field} must be an integer"
    elif not isinstance(value, (int, float)):
        return f"{field} must be a number"

    numeric = float(value)
    # JSON accepts NaN/Infinity as literals; both poison the routing math
    # (every NaN comparison is False, so a NaN threshold silently disables
    # the signal it was meant to enforce).
    if not math.isfinite(numeric):
        return f"{field} must be a finite number"
    if bound.minimum is not None:
        if bound.minimum_inclusive:
            if numeric < bound.minimum:
                return f"{field} must be {bound.describe()}"
        elif numeric <= bound.minimum:
            return f"{field} must be {bound.describe()}"
    if bound.maximum is not None:
        if bound.maximum_inclusive:
            if numeric > bound.maximum:
                return f"{field} must be {bound.describe()}"
        elif numeric >= bound.maximum:
            return f"{field} must be {bound.describe()}"
    return None


@dataclass(frozen=True)
class AdmissionPlan:
    """Ordered admission plan produced by the routing decision.

    The proxy consumes this plan as follows:

    1. Try each ``immediate_candidate`` with a non-blocking gate acquire
       (``timeout=0``).
    2. Forward through the first successful acquisition.
    3. If all immediate attempts lose the snapshot race, perform one final
       non-blocking pass over the remaining eligible candidates.
    4. If configured, wait only on ``queue_candidate`` for the remaining
       queue budget.
    5. After queue timeout, return an honest 503 derived from
       ``terminal_fallback``'s structural signal.
    """

    immediate_candidates: tuple[str, ...]
    queue_candidate: str | None
    terminal_fallback: str
    reason: str


def hash_route_key(raw_key: str, secret: str | None = None) -> str:
    """Hash a raw API key to its stored route identifier.

    With no ``secret`` (the default, and the pre-HMAC behaviour) this is a
    plain SHA-256 digest. With a ``secret`` it is HMAC-SHA-256, which defeats
    rainbow-table matching of stored digests should the route-table store
    leak: without the secret, a guessed API key cannot be confirmed against a
    stored digest (Plan 008 §3). Rotation uses a bounded dual-read window —
    the caller hashes with the new secret, and on a lookup miss retries with
    the previous secret until stored entries are re-added under the new key.

    Pure, deterministic, stdlib-only (``hashlib`` + ``hmac``).
    """
    if not secret:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return hmac.new(
        secret.encode("utf-8"), raw_key.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def extract_conversation_fingerprint(body: bytes) -> str | None:
    """Extract a stable fingerprint from the first user message in a request body.

    Plan 019 §6.2 — all sessions from one opencode instance share an API key,
    so the API-key hash groups every conversation under one affinity entry.
    The stable per-conversation identifier is the first user message's text
    content; this returns its SHA-256 hash (or None when no user message with
    text is present, in which case the caller falls back to the route-key hash).

    Pure and stdlib-only: ``json`` + ``hashlib``, no I/O.  The body is only
    read when ``pin_conversations`` is opt-in, and bytes forwarded upstream
    are never altered.
    """
    import json

    try:
        data = json.loads(body)
    except (ValueError, TypeError, RecursionError):
        # RecursionError: deeply-nested JSON (e.g. 300k nested arrays) is
        # valid syntax that overflows the parser — catch it so a crafted
        # body cannot crash the request path.  Returns None (fall back to
        # the route-key hash), never raises.
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
                # Multi-modal: hash the first text part.
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if isinstance(text, str):
                            return hashlib.sha256(
                                text.encode("utf-8")
                            ).hexdigest()
    return None


#: A path segment that names an API version: v1, v2, v1beta, v1alpha2.
_VERSION_SEGMENT = re.compile(r"^v\d+(?:[a-z]+\d*)?$")


def compose_upstream_path(base: str, client_path: str) -> str:
    """Compose an upstream URL from a provider base and the client's path.

    Plan 021 D2. Clients must not have to accommodate switchboard: pointing
    one at ``https://switchboard.<host>/v1`` — the shape every
    OpenAI-compatible ``baseURL`` conventionally takes — has to work. But the
    provider base is most useful when it can be pasted verbatim from the
    vendor's own quickstart, and those usually already end in a version
    (``https://ollama.com/v1``, ``.../zen/go/v1``, ``.../paas/v4``). Naive
    concatenation doubles the version and 404s.

    **The base declares the version if it has one.** When the base's last
    segment looks like a version, a leading version segment on the client
    path is redundant and is dropped. When the base carries no version, the
    client's is preserved — that is what keeps a bare-host base (the natural
    OpenAI-style setup, working today) working unchanged.

    At most one segment is ever dropped, and only in leading position, so a
    ``v1`` appearing later in an endpoint is left alone.

        >>> compose_upstream_path("https://ollama.com/v1", "/v1/chat/completions")
        'https://ollama.com/v1/chat/completions'
        >>> compose_upstream_path("https://api.example.com", "/v1/chat/completions")
        'https://api.example.com/v1/chat/completions'

    Pure and deterministic: no I/O, no network, no clock.
    """
    base = base.rstrip("/")
    path, sep, query = client_path.partition("?")

    segments = [s for s in path.split("/") if s]

    base_tail = base.rsplit("/", 1)[-1]
    if (
        segments
        and _VERSION_SEGMENT.match(segments[0])
        and _VERSION_SEGMENT.match(base_tail)
    ):
        segments = segments[1:]

    composed = base + ("/" + "/".join(segments) if segments else "")
    return composed + (sep + query if sep else "")


def _satisfies_capabilities(
    state: ProviderState,
    required: frozenset[str],
) -> bool:
    """Check whether a provider satisfies required capability surfaces.

    Providers without capabilities metadata are NOT filtered (backward
    compat).  A provider satisfies requirements if all required surfaces are
    in the provider's capabilities surfaces.
    """
    if not required:
        return True
    caps = state.capabilities
    if caps is None:
        return True
    return required <= caps.surfaces


def pace_surplus(state: ProviderState, burn_rate_per_day: float) -> float | None:
    """Quota surplus for one provider (Plan 020 D5).

    ``surplus = remaining_fraction - expected_burn``
    where ``expected_burn = burn_rate_per_day * days_until_reset``.

    Returns ``None`` when the provider has no FRESH weekly signal (stale or
    unscored providers rank after scored ones in table order and are never
    starved — fail safe).

    A positive surplus means the provider has more quota than it will plausibly
    spend before reset → use-it-or-lose-it → should be burned first (ranked
    highest). A negative surplus means the provider is burning faster than
    nominal and should be conserved.

    Pure and deterministic: no I/O, no clock — the weekly reset countdown is
    already a ``seconds_until_reset`` value in the frozen ``ProviderState``.
    """
    if state.weekly_remaining_fraction is None or state.weekly_reset_in is None:
        return None
    if state.signal_freshness != SignalFreshness.FRESH:
        return None
    days_until_reset = state.weekly_reset_in / 86400.0
    expected_burn = burn_rate_per_day * days_until_reset
    return state.weekly_remaining_fraction - expected_burn


def pace_rank(
    immediate: list[str],
    states: dict[str, ProviderState],
    candidates: tuple[str, ...],
    config: RoutingConfig,
) -> bool:
    """Reorder ``immediate`` by pace surplus descending (Plan 020 D5, WI-13).

    Scored providers rank first (highest surplus), unscored providers follow
    in table order. The ranking is stable within each group. Deadband: when
    the leader's surplus advantage over the runner-up is less than
    ``pace_flap_margin``, table order is preserved to avoid flapping between
    near-equal providers.

    Mutates ``immediate`` in place — the caller already separated immediate
    candidates; this only reorders them. Returns ``True`` when the ranking
    changed the order (i.e. a non-table-order result), ``False`` when the
    deadband guard or all-unscored path left it unchanged.
    """
    scored: list[tuple[str, float]] = []
    unscored: list[str] = []
    for name in immediate:
        st = states.get(name)
        if st is None:
            unscored.append(name)
            continue
        s = pace_surplus(st, config.pace_burn_rate_per_day)
        if s is None:
            unscored.append(name)
        else:
            scored.append((name, s))

    if not scored:
        return False  # nothing to rank; leave table order

    order = {name: idx for idx, name in enumerate(candidates)}

    def _sort_key(item: tuple[str, float]) -> tuple[float, int]:
        return (-item[1], order[item[0]])

    scored.sort(key=_sort_key)

    # Deadband: if the leader's advantage < pace_flap_margin, keep table
    # order to avoid per-request flapping between near-equal providers.  But
    # still enforce the scored-first invariant (unscored providers never
    # outrank scored ones) — the plan's guardrail holds even under the deadband.
    if len(scored) >= 2:
        best_surplus = scored[0][1]
        runner_up_surplus = scored[1][1]
        if best_surplus - runner_up_surplus < config.pace_flap_margin:
            # Too close to re-rank: keep scored (table order via the sort
            # ties) before unscored, but do not re-order by surplus.
            scored.sort(key=lambda item: order[item[0]])
            new_order = [name for name, _ in scored] + unscored
            changed = new_order != immediate
            immediate[:] = new_order
            return changed

    new_order = [name for name, _ in scored] + unscored
    changed = new_order != immediate
    immediate[:] = new_order
    return changed


def _opportunistic_target(
    immediate: list[str],
    primary: str,
    states: dict[str, ProviderState],
    config: RoutingConfig,
) -> str | None:
    """Select an opportunistic quota-burn target, or None.

    A candidate qualifies when it is not the primary, is in ``immediate``
    (therefore FRESH and AVAILABLE), reports measured headroom above the
    configured floor, and reports a quota reset within the burn window.
    The best qualifier wins only if it leads the runner-up by the configured
    margin (a single qualifier needs no margin).  Ties break on ``immediate``
    order (table order after ranking).
    """
    if not config.opportunistic_enabled:
        return None

    qualifiers: list[tuple[str, float]] = []
    for name in immediate:
        if name == primary:
            continue
        state = states.get(name)
        if state is None:
            continue
        headroom = state.usage_headroom
        if headroom is None or headroom < config.opportunistic_min_headroom:
            continue
        resets_in = state.quota_resets_in
        if resets_in is None or not (0.0 < resets_in <= config.opportunistic_reset_window):
            continue
        qualifiers.append((name, headroom))

    if not qualifiers:
        return None

    # argmax headroom; deterministic tiebreak: earlier in immediate order.
    order = {name: idx for idx, name in enumerate(immediate)}

    def _sort_key(item: tuple[str, float]) -> tuple[float, int]:
        return (-item[1], order[item[0]])

    qualifiers.sort(key=_sort_key)
    best_name, best_headroom = qualifiers[0]
    if len(qualifiers) == 1:
        return best_name
    runnerup_headroom = qualifiers[1][1]
    if best_headroom - runnerup_headroom >= config.opportunistic_margin:
        return best_name
    return None


def route_decision(
    states: dict[str, ProviderState],
    table: RouteTable,
    route_key: str,
    config: RoutingConfig,
    *,
    now: float,
    affinity: RouteAffinity | None = None,
    servable_providers: frozenset[str] | None = None,
    healthy_since: dict[str, float] | None = None,
) -> AdmissionPlan:
    """Pure routing decision. Returns an :class:`AdmissionPlan`.

    The decision proceeds in this order (Plans 006, 008, 014, 015, 016):

    1. Resolve the route key to an ordered candidate list.
    2. Filter out candidates that do not serve the request's model when a model
       map is configured (Plan 010 Feature B).
    3. Filter out candidates whose capabilities don't satisfy the route's
       required capabilities (Plan 008 §4).
    4. Reject missing and closed candidates.
    5. Separate fresh candidates from unknown/stale candidates and demote
       pressured non-primary candidates to queue-eligible (headroom, token
       budget, trailing-24h usage; Plan 013 allows the last to demote the
       primary).
    6. Place candidates with immediate permits (AVAILABLE) first.
    7. Order immediate candidates by the configured strategy: ``ORDERED``
       (default, table order), ``HEADROOM`` (``usage_headroom`` descending,
       Plan 015; ``headroom_ranking = True`` is equivalent), or ``PACE``
       (weekly quota surplus descending, Plan 020 D5). Data-bearing candidates
       precede ones without data; ties break on table order. Pace applies
       a ``pace_flap_margin`` deadband so near-equal providers don't
       alternate per-request.
    8. Apply affinity stickiness / dwell / failback logic (Plan 008 §5).
       After ``dwell_interval``, if the primary is in immediate and
       ``failback_delay`` is configured, failback requires the primary to have
       been continuously FRESH+AVAILABLE for at least ``failback_delay``
       seconds (Plan 014).  When no affinity pin is active, opportunistically
       front a qualifying quota-burn fallback carrying measured headroom above
       ``opportunistic_min_headroom`` and a reset within
       ``opportunistic_reset_window`` (Plan 016); subordinate to affinity,
       de-preference only.
    9. Preserve primary preference among equally admissible candidates when
       neither an affinity pin nor an opportunistic target pinned the front.
    10. Select at most one explicit queue candidate.
    11. Preserve the configured primary as the terminal safe-failure target
        so its gate can provide the canonical rejection when nothing is usable.
        (When model or capability filtering removes the configured primary,
        the terminal fallback is the first *surviving* candidate, so the
        canonical rejection comes from a provider that can actually serve the
        request — and the queue backstop tracks that same effective primary.)

    ``healthy_since`` maps provider name → the monotonic instant that provider
    first became continuously FRESH+AVAILABLE.  It is consulted by name for the
    *effective* primary (post-filtering), so a model map that excludes the
    configured primary does not leave the hysteresis check reading the wrong
    provider's clock.  ``None`` or a missing entry means "never observed
    healthy" and holds the affinity pin until a clock is established.

    Guarantees:

    * **Fail safe** — when all providers are closed, the plan's
      ``terminal_fallback`` is the primary; the proxy forwards to it and lets
      its gate return 503.  Never silently drop a request.
    * **Stale data never improves preference** — unknown/stale providers are
      excluded from failover by default (``fresh-only-for-failover`` policy).
    * **Model-servability filtering** — when the requested model is mapped in
      a configured model map, only providers that declare an alias for it are
      eligible.
    * **Capability filtering** — providers whose declared surfaces don't
      include all required surfaces are excluded before admission ranking.
    * **Bounded stickiness** — after failover, the routing core prefers the
      affinity provider for at least ``dwell_interval`` seconds before
      considering failback to the primary.
    * **Failback hysteresis** — when ``failback_delay > 0``, failback to the
      primary requires the primary to have been continuously FRESH+AVAILABLE
      for at least ``failback_delay`` seconds.  A single unhealthy poll resets
      the continuity clock.
    * **Opt-in headroom ranking** — when ``headroom_ranking`` is enabled,
      immediate candidates are ordered by ``usage_headroom`` descending before
      affinity/primary fronting; absence of data never outranks a measured
      provider.
    * **Opportunistic quota burn (Plan 016)** — opt-in; subordinate to an
      active affinity pin; de-preference only: the primary remains
      immediate-eligible, queue backstop, and terminal fallback.  Stale or
      unmeasured data never promotes.
    * **Pure** — ``now``, ``healthy_since``, and all states are arguments.  No
      I/O, no clock.
    * **Deterministic** — same inputs produce the same plan.
    """
    entry = table.entries.get(route_key)
    candidates = entry.providers if entry is not None else table.default_providers

    if not candidates:
        raise ValueError("no providers configured")

    primary = candidates[0]

    # --- Model-servability filtering (Plan 010 Feature B) ---
    # When the request's model is mapped, only providers that declare an alias
    # for it are eligible — failover never routes a model to a provider that
    # doesn't serve it.  ``None`` means unmapped/no-map: no filtering (today's
    # behaviour).  The configured primary is preserved as terminal_fallback so a
    # fully-unservable request still gets a canonical rejection from its gate.
    if servable_providers is not None:
        servable = tuple(n for n in candidates if n in servable_providers)
        if not servable:
            return AdmissionPlan(
                immediate_candidates=(),
                queue_candidate=None,
                terminal_fallback=primary,
                reason="model_unservable",
            )
        candidates = servable
        primary = candidates[0]

    # --- Capability filtering (Plan 008 §4) ---
    required_caps = entry.required_capabilities if entry is not None else frozenset()
    if required_caps:
        filtered: list[str] = []
        for name in candidates:
            state = states.get(name)
            if state is None:
                filtered.append(name)
                continue
            if _satisfies_capabilities(state, required_caps):
                filtered.append(name)
        if not filtered:
            return AdmissionPlan(
                immediate_candidates=(),
                queue_candidate=None,
                terminal_fallback=primary,
                reason="capability_filtered",
            )
        candidates = tuple(filtered)
        primary = candidates[0]

    immediate: list[str] = []
    queue_eligible: list[str] = []

    for name in candidates:
        state = states.get(name)
        if state is None:
            continue
        if state.availability == Availability.CLOSED:
            continue

        is_primary = name == primary

        if (
            state.signal_freshness == SignalFreshness.FRESH
            or (
                state.signal_freshness == SignalFreshness.DEGRADED
                and is_primary
            )
        ):
            low_headroom = (
                not is_primary
                and config.headroom_threshold > 0
                and state.usage_headroom is not None
                and state.usage_headroom < config.headroom_threshold
            )
            # Per-provider soft_threshold (Plan 012 §4) overrides the global
            # token_budget_threshold when set; None falls back to the global,
            # whose default 0.0 disables the signal entirely.  This is what
            # makes an operator's ``[token_budget.<p>] soft_threshold = 0.85``
            # actually take effect — previously it was parsed, validated, and
            # displayed but never consumed (dead config).
            budget_threshold = (
                state.token_soft_threshold
                if state.token_soft_threshold is not None
                else config.token_budget_threshold
            )
            over_budget = (
                not is_primary
                and budget_threshold > 0
                and state.token_utilization is not None
                and state.token_utilization
                >= budget_threshold
            )
            # Plan 013: trailing-24h usage — the ONE proactive signal that
            # may demote the primary (no `not is_primary` guard).  Demotion
            # de-prefers only: the primary stays queue-eligible backstop.
            over_24h = (
                config.usage_24h_threshold > 0
                and state.usage_24h_utilization is not None
                and state.usage_24h_utilization >= config.usage_24h_threshold
            )
            if low_headroom or over_budget or over_24h:
                queue_eligible.append(name)
            elif state.availability == Availability.AVAILABLE:
                immediate.append(name)
            elif state.availability == Availability.BUSY:
                queue_eligible.append(name)
        # UNKNOWN: excluded from failover preference (fresh-only-for-failover)

    # --- Immediate-candidate ordering (Plan 015 / Plan 020 D5) ---
    # The strategy determines how immediate candidates are ordered before
    # affinity/primary fronting. ORDERED (default) leaves table order intact.
    # HEADROOM orders by usage_headroom descending (Plan 015). PACE orders by
    # weekly quota surplus descending (Plan 020 D5). ``headroom_ranking = True``
    # is treated as ``strategy = HEADROOM`` for backward compat.
    pace_changed = False
    use_headroom = (
        config.strategy == RoutingStrategy.HEADROOM or config.headroom_ranking
    )
    if use_headroom and len(immediate) > 1:
        order = {name: i for i, name in enumerate(candidates)}

        def _rank_key(name: str) -> tuple[int, float, int]:
            st = states.get(name)
            h = st.usage_headroom if st else None
            # data-bearing first (headroom desc), then table order
            return (0 if h is not None else 1, -(h or 0.0), order[name])

        immediate.sort(key=_rank_key)
    elif config.strategy == RoutingStrategy.PACE and len(immediate) > 1:
        pace_changed = pace_rank(immediate, states, candidates, config)

    # --- Affinity stickiness / dwell / failback (Plan 008 §5) ---
    affinity_reason = ""
    affinity_state = (
        states.get(affinity.provider) if affinity is not None else None
    )
    affinity_fresh = (
        affinity_state is not None
        and affinity_state.signal_freshness == SignalFreshness.FRESH
    )
    if (
        affinity is not None
        and affinity.provider != primary
        and affinity.provider in immediate
        and affinity_fresh
    ):
        if (now - affinity.selected_at) < config.dwell_interval:
            immediate.remove(affinity.provider)
            immediate.insert(0, affinity.provider)
            affinity_reason = "affinity_dwell"
        elif primary in immediate and not config.pin_conversations:
            primary_clock = (
                healthy_since.get(primary) if healthy_since else None
            )
            hysteresis = (
                config.failback_delay > 0
                and (
                    primary_clock is None
                    or (now - primary_clock) < config.failback_delay
                )
            )
            if hysteresis:
                immediate.remove(affinity.provider)
                immediate.insert(0, affinity.provider)
                affinity_reason = "affinity_hysteresis"
            else:
                immediate.remove(primary)
                immediate.insert(0, primary)
        else:
            # No failback: either the primary is not in immediate, or
            # conversation pinning (Plan 019 §6) holds the pin past dwell
            # as long as the pinned provider stays FRESH + AVAILABLE.
            immediate.remove(affinity.provider)
            immediate.insert(0, affinity.provider)
            affinity_reason = (
                "affinity_pinned" if config.pin_conversations else ""
            )
    else:
        # Conversation pinning (Plan 019 §6): when a pin is active on the
        # *primary* (the if-block's `affinity.provider != primary` guard
        # routed us here), hold it — do NOT let opportunistic quota-burn
        # front a fallback and permanently migrate a pinned conversation.
        if (
            config.pin_conversations
            and affinity is not None
            and affinity.provider in immediate
        ):
            immediate.remove(affinity.provider)
            immediate.insert(0, affinity.provider)
            affinity_reason = "affinity_pinned"
        elif config.strategy != RoutingStrategy.PACE:
            # ORDERED / HEADROOM only: consider an opportunistic burn, and
            # otherwise front the primary.
            #
            # PACE deliberately skips BOTH. pace_rank (step 7) has already
            # ordered `immediate` by weekly quota surplus, which is Plan 016's
            # use-it-or-lose-it philosophy promoted from an opportunistic
            # exception to the primary ordering (Plan 020 D5). Re-fronting the
            # primary here would undo exactly the ordering the operator asked
            # for, and running opportunism on top would let a session-window
            # signal override a weekly-window decision. So under PACE the
            # primary can lose the front — it is NOT demoted (it stays
            # immediate-eligible, queue backstop, and terminal fallback), but
            # it is not privileged either. An affinity pin still outranks the
            # ranking; that is handled above.
            target = _opportunistic_target(
                immediate, primary, states, config
            )
            if target is not None:
                immediate.remove(target)
                immediate.insert(0, target)
                affinity_reason = "opportunistic"
            elif primary in immediate:
                immediate.remove(primary)
                immediate.insert(0, primary)

    # Select at most one queue candidate: prefer primary if eligible.
    queue_candidate: str | None = None
    if primary in queue_eligible:
        queue_candidate = primary
    elif queue_eligible:
        queue_candidate = queue_eligible[0]

    if not immediate and queue_candidate is None:
        return AdmissionPlan(
            immediate_candidates=(),
            queue_candidate=None,
            terminal_fallback=primary,
            reason="no_eligible_candidates",
        )

    if immediate:
        if affinity_reason:
            reason = affinity_reason
        elif immediate[0] == primary:
            reason = "primary_available"
        elif config.strategy == RoutingStrategy.PACE and pace_changed and primary in immediate:
            reason = "pace_failover"
        else:
            reason = "failover"
    else:
        reason = "queue_only"

    return AdmissionPlan(
        immediate_candidates=tuple(immediate),
        queue_candidate=queue_candidate,
        terminal_fallback=primary,
        reason=reason,
    )


# ── Usage-error reroute (Plan 010, reactive half) ─────────────────────────


#: Upstream statuses that mean "this provider cannot serve you right now"
#: rather than "your request is wrong". Every provider in the estate signals
#: exhaustion with one of these: 429 (rate/quota), 402 (billing/credit
#: exhausted), 503/529 (overloaded / temporarily unavailable). 500 and 502 are
#: deliberately absent — a genuine upstream bug or bad gateway is not a usage
#: signal, and rerouting it would silently spray a broken request across every
#: provider in turn.
DEFAULT_REROUTE_STATUSES: frozenset[int] = frozenset({402, 429, 503, 529})


def should_reroute(
    *,
    status: int,
    reroute_statuses: frozenset[int],
    reroutes_done: int,
    max_attempts: int,
    body_replayable: bool,
    response_started: bool,
    alternatives_remain: bool,
) -> bool:
    """Decide whether a usage-error response should be retried elsewhere.

    Pure predicate — the proxy owns the I/O, this owns the rule. Every clause
    is a safety property, not a preference:

    * ``response_started`` — once a byte has reached the client the request is
      committed to that upstream; a "retry" would concatenate two responses.
      This is the invariant that makes rerouting safe at all.
    * ``body_replayable`` — a streamed (unbuffered) body has already been
      consumed by the first attempt and cannot be sent again.
    * ``alternatives_remain`` — retrying the same pressured provider is just a
      slower failure, and is what the client's own retry loop already does.
    * ``reroutes_done``/``max_attempts`` — ``max_attempts`` counts RETRIES, not
      total tries, so 1 means "try the primary, then at most one other".
      Bounded so a fully-exhausted estate degrades to a single error rather
      than a fan-out across every provider in turn.
    """
    if response_started:
        return False
    if not body_replayable:
        return False
    if not alternatives_remain:
        return False
    if reroutes_done >= max_attempts:
        return False
    return status in reroute_statuses
