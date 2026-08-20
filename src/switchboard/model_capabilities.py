"""Pure model-capabilities core — the model-contract engine (Plan 027).

This module turns per-provider model observations into per-model
**contracts**: what is guaranteed for a logical model across every provider
that may serve it. It imports **nothing outside the standard library**, does
**no I/O**, and reads **no clock**: ``now``, ``observed_at`` and freshness
bounds are passed in as arguments so every computation is reproducible and
unit-testable without a network.

Enforced by tests/test_import_boundary.py.

Conservatism rules (the whole point of the module):

* Missing metadata stays **unknown** — it is never guessed from model names.
* Numeric limits aggregate to the **minimum**; sets (modalities, reasoning
  levels) to the **intersection**; booleans use **three-valued** logic, so
  one known ``FALSE`` guarantees ``false``, but a ``TRUE`` guarantee needs
  every participant to have said ``true``.
* Reasoning levels intersect **only when the exact wire encoding agrees**
  (``reasoning_effort`` vs the ``reasoning`` object). A divergence yields no
  advertised levels, never a mix.
* An **unknown or stale participant** makes the affected field not-guaranteed;
  the best-effort value is still computed and exposed *separately* (its
  field source is ``observed``) so the operator can see it without a client
  taking it as a promise.
* **Operator caps** may only LOWER verified limits (source ``capped``) or
  FILL gaps the providers never reported (source ``declared`` — an operator
  assertion, not evidence). A cap set above an observed limit is clamped to
  the observed value and reported as a **deviation**: a declared ceiling
  above what the wire will actually honour is a lie the operator meets at
  the upstream.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Field source tags — what the operator (and the client) may rely on.
SOURCE_VERIFIED = "verified"  # fresh evidence from every participant
SOURCE_CAPPED = "capped"  # operator cap below the verified minimum
SOURCE_DECLARED = "declared"  # operator cap filling a gap; not evidence
SOURCE_OBSERVED = "observed"  # best-effort value, NOT guaranteed
SOURCE_UNKNOWN = "unknown"  # no usable value at all


class TriState(Enum):
    """A capability flag that can honestly be unknown."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


# Reasoning wire formats a provider may accept on the OpenAI-compatible
# wire (hard rule 5: switchboard never translates bodies, so the exact
# encoding is part of the contract).
REASONING_FORMAT_EFFORT = "reasoning_effort"  # top-level "reasoning_effort": "low"
REASONING_FORMAT_OBJECT = "reasoning_object"  # "reasoning": {"effort": "low"}


@dataclass(frozen=True)
class ModelObservation:
    """One provider's observed capability for one upstream model.

    ``alias`` is the model id AS THE PROVIDER NAMES IT (the model map's
    per-provider alias); ``provider`` is the switchboard provider name.
    Every capability field that the upstream's listing did not carry stays
    at its unknown default — absence of evidence, never a guess.
    """

    provider: str
    alias: str
    context_limit: int | None = None
    input_limit: int | None = None
    output_limit: int | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    tool_calling: TriState = TriState.UNKNOWN
    reasoning: TriState = TriState.UNKNOWN
    reasoning_format: str | None = None
    reasoning_levels: tuple[str, ...] = ()
    #: Wall-clock epoch seconds when the listing was observed (the shell
    #: supplies it; the core only compares, it never reads a clock).
    observed_at: float = 0.0
    #: Where the observation came from (Plan 027: ``"discovery"``).
    source: str = "discovery"
    #: Digest of the provider's effective config at observation time, so a
    #: config change makes the evidence visibly stale.
    config_fingerprint: str = ""


def _positive_int(value: Any) -> int | None:
    """Accept a positive integer-valued token; reject bools, junk, non-finites."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        n = int(value)
    else:
        return None
    return n if n > 0 else None


def _modalities(value: Any) -> tuple[str, ...] | None:
    """A non-empty list of unique non-empty strings, else None (unknown)."""
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for v in value:
        if not isinstance(v, str) or not v:
            return None
        if v not in out:
            out.append(v)
    return tuple(out) or None


def _levels(value: Any) -> tuple[str, ...] | None:
    """A list of level names (duplicates dropped), else None."""
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for v in value:
        if not isinstance(v, str) or not v:
            return None
        if v not in out:
            out.append(v)
    return tuple(out) or None


def _params(item: dict[str, Any]) -> list[str] | None:
    """The OpenRouter-style ``supported_parameters`` list, if well-formed."""
    raw = item.get("supported_parameters")
    if not isinstance(raw, list):
        return None
    if not all(isinstance(p, str) for p in raw):
        return None
    return [p for p in raw if p]


def extract_listing_items(payload: Any) -> list[Any]:
    """The per-model items of an OpenAI-compatible ``/models`` payload.

    Accepts the three shapes the probe meets in the wild:
    ``{"data": [...]}``, ``{"models": [...]}``, and a bare list.
    """
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, dict):
        data_arr = payload.get("data")
        if isinstance(data_arr, list):
            return list(data_arr)
        models_arr = payload.get("models")
        if isinstance(models_arr, list):
            return list(models_arr)
    return []


def normalize_model_item(
    item: Any,
    *,
    provider: str,
    observed_at: float,
    config_fingerprint: str = "",
) -> ModelObservation | None:
    """One raw listing entry → a normalized :class:`ModelObservation`.

    Shape-driven, never name-driven: whatever fields the upstream actually
    sent are read (the OpenAI base shape carries ids only; vLLM adds
    ``max_model_len``; LiteLLM adds ``max_input_tokens``/``max_output_tokens``;
    OpenRouter adds context, modalities, and ``supported_parameters``), and
    whatever it did not send stays unknown.

    Returns None for entries without a usable string id — a model listing
    with no id is not a model.
    """
    if isinstance(item, str):
        alias: str = item
        item = {}
    elif isinstance(item, dict):
        raw_id = item.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            return None
        alias = raw_id
    else:
        return None

    top = item.get("top_provider")
    top = top if isinstance(top, dict) else {}
    arch = item.get("architecture")
    arch = arch if isinstance(arch, dict) else {}

    # Context: vLLM's max_model_len, the OpenAI-family context_length, or
    # the OpenRouter top_provider's context_length.
    context = (
        _positive_int(item.get("max_model_len"))
        or _positive_int(item.get("context_length"))
        or _positive_int(top.get("context_length"))
    )
    # Input / output: LiteLLM-style per-direction limits; OpenRouter exposes
    # the output side on top_provider as max_completion_tokens.
    input_limit = _positive_int(item.get("max_input_tokens"))
    output = (
        _positive_int(item.get("max_output_tokens"))
        or _positive_int(top.get("max_completion_tokens"))
    )

    # Modalities: OpenRouter nests them under architecture; some gateways
    # carry them at the top level.
    in_mod = (
        _modalities(arch.get("input_modalities"))
        or _modalities(item.get("input_modalities"))
    )
    out_mod = (
        _modalities(arch.get("output_modalities"))
        or _modalities(item.get("output_modalities"))
    )

    params = _params(item)
    if params is None:
        tool_calling = TriState.UNKNOWN
        reasoning = TriState.UNKNOWN
        reasoning_format: str | None = None
    else:
        if {"tools", "tool_choice"} & frozenset(params):
            tool_calling = TriState.TRUE
        else:
            tool_calling = TriState.FALSE
        if "reasoning_effort" in params:
            reasoning = TriState.TRUE
            # "reasoning" alongside "reasoning_effort" means the object form
            # is accepted as well (the richer control); the object wins.
            reasoning_format = (
                REASONING_FORMAT_OBJECT
                if "reasoning" in params
                else REASONING_FORMAT_EFFORT
            )
        elif "reasoning" in params:
            reasoning = TriState.TRUE
            reasoning_format = REASONING_FORMAT_OBJECT
        else:
            reasoning = TriState.FALSE
            reasoning_format = None
    # Accepted effort values are honoured only when the payload names them
    # explicitly; the wire format alone does not enumerate valid levels.
    levels = _levels(item.get("reasoning_efforts")) or _levels(
        item.get("reasoning_levels")
    )

    return ModelObservation(
        provider=provider,
        alias=alias,
        context_limit=context,
        input_limit=input_limit,
        output_limit=output,
        input_modalities=in_mod or (),
        output_modalities=out_mod or (),
        tool_calling=tool_calling,
        reasoning=reasoning,
        reasoning_format=reasoning_format,
        reasoning_levels=levels or (),
        observed_at=observed_at,
        config_fingerprint=config_fingerprint,
    )


def parse_model_listing(
    payload: Any,
    *,
    provider: str,
    observed_at: float,
    config_fingerprint: str = "",
) -> list[ModelObservation]:
    """Parse a fully-parsed ``/models`` payload into observations.

    Pure: the JSON decode happens in the shell (where the body is bounded);
    this function only walks the decoded structure. Entries without an id
    are skipped rather than crashing the parse; order is preserved and ids
    are de-duplicated (first occurrence wins).
    """
    out: list[ModelObservation] = []
    seen: set[str] = set()
    for item in extract_listing_items(payload):
        obs = normalize_model_item(
            item,
            provider=provider,
            observed_at=observed_at,
            config_fingerprint=config_fingerprint,
        )
        if obs is not None and obs.alias not in seen:
            seen.add(obs.alias)
            out.append(obs)
    return out


def frozenset_to_tuple(values: set[str]) -> tuple[str, ...]:
    """A canonical (sorted) tuple — deterministic rendering."""
    return tuple(sorted(values))


def _and_tristates(states: tuple[TriState, ...]) -> TriState:
    """Three-valued AND: any known FALSE guarantees false; an unknown makes
    the result unknown; every-TRUE guarantees true; no evidence = unknown."""
    if TriState.FALSE in states:
        return TriState.FALSE
    if TriState.UNKNOWN in states:
        return TriState.UNKNOWN
    if states and all(s is TriState.TRUE for s in states):
        return TriState.TRUE
    return TriState.UNKNOWN


def _min_non_none(values: tuple[int | None, ...]) -> tuple[int | None, int]:
    """(min over the non-None values, count of non-None values)."""
    known = [v for v in values if v is not None]
    return (min(known) if known else None), len(known)


def _intersect_sets(
    sets: tuple[tuple[str, ...] | None, ...],
) -> tuple[tuple[str, ...] | None, int]:
    """Intersection over the non-None sets, plus how many had one.

    The normalizer expresses a missing set as None (never an empty set), so
    "no participant reported" and "a participant reported nothing" cannot be
    conflated.
    """
    known = [s for s in sets if s is not None]
    if not known:
        return None, 0
    result = set(known[0])
    for s in known[1:]:
        result &= set(s)
    return frozenset_to_tuple(result), len(known)


def _complete(total: int, known: int) -> bool:
    return total > 0 and known == total


@dataclass(frozen=True)
class FieldValue:
    """A contract field: its value plus the strength of the evidence behind it."""

    value: Any
    source: str


@dataclass(frozen=True)
class Deviation:
    """A configured operator cap that could not be honoured as written."""

    field_name: str
    #: "cap_exceeds_observed" | "cap_disjoint_from_observed"
    #: | "cap_conflicts_with_observed"
    kind: str
    configured: Any
    observed: Any
    detail: str = ""


@dataclass(frozen=True)
class ParticipantStatus:
    """How one participant certified (or failed to) the contract."""

    provider: str
    alias: str
    #: "fresh" | "stale" | "missing"
    state: str
    observed_at: float | None = None
    #: True/False when the current config fingerprint is known to match or
    #: differ from the observation's; None when either side is unknown.
    fingerprint_current: bool | None = None


@dataclass(frozen=True)
class ModelCaps:
    """Operator-declared ceilings / fill-ins for one logical model.

    Every field defaults to None = "operator said nothing"; caps may only
    lower verified limits or fill gaps (module docstring).
    """

    context_limit: int | None = None
    input_limit: int | None = None
    output_limit: int | None = None
    input_modalities: tuple[str, ...] | None = None
    output_modalities: tuple[str, ...] | None = None
    tool_calling: bool | None = None
    reasoning: bool | None = None
    #: Declared accepted effort levels (implies the operator asserts
    #: reasoning support for exactly the listed levels).
    reasoning_levels: tuple[str, ...] | None = None
    #: Declared wire encoding for the reasoning control (one of
    #: REASONING_FORMAT_EFFORT / REASONING_FORMAT_OBJECT). Declared levels
    #: without a format cannot be rendered to any wire shape.
    reasoning_format: str | None = None


def _numeric_field(
    observed: tuple[int | None, ...], cap: int | None, name: str
) -> tuple[FieldValue, tuple[Deviation, ...]]:
    """Aggregate one numeric limit over the participants, apply an operator cap."""
    total = len(observed)
    value, known = _min_non_none(observed)
    verified = _complete(total, known)
    if cap is None:
        if value is None:
            return FieldValue(None, SOURCE_UNKNOWN), ()
        return FieldValue(value, SOURCE_VERIFIED if verified else SOURCE_OBSERVED), ()
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        # A malformed cap is dead config: treated as absent.
        source = (
            SOURCE_VERIFIED
            if verified
            else SOURCE_OBSERVED if value is not None else SOURCE_UNKNOWN
        )
        return FieldValue(value, source), ()
    if value is None:
        # Nobody observed the field; the cap fills the gap — declared.
        return FieldValue(cap, SOURCE_DECLARED), ()
    if cap > value:
        # Above what the wire will honour: clamp and report.
        return (
            FieldValue(value, SOURCE_VERIFIED if verified else SOURCE_OBSERVED),
            (
                Deviation(
                    field_name=name,
                    kind="cap_exceeds_observed",
                    configured=cap,
                    observed=value,
                    detail=(
                        f"operator cap {cap} exceeds the observed minimum "
                        f"{value}; clamped to the observed value"
                    ),
                ),
            ),
        )
    if cap < value:
        return FieldValue(cap, SOURCE_CAPPED), ()
    return FieldValue(value, SOURCE_VERIFIED if verified else SOURCE_OBSERVED), ()


def _modality_field(
    observed: tuple[tuple[str, ...] | None, ...],
    cap: tuple[str, ...] | None,
    name: str,
) -> tuple[FieldValue, tuple[Deviation, ...]]:
    total = len(observed)
    value, known = _intersect_sets(observed)
    complete = _complete(total, known)
    if cap is None:
        if value is None:
            return FieldValue(None, SOURCE_UNKNOWN), ()
        return FieldValue(value, SOURCE_VERIFIED if complete else SOURCE_OBSERVED), ()
    if not cap:
        # Declared as "no modalities at all": a restriction, sound in either
        # direction. It intersects to empty against any observed set.
        return (
            FieldValue((), SOURCE_CAPPED if complete else SOURCE_DECLARED),
            (),
        )
    if value is None:
        return FieldValue(tuple(sorted(cap)), SOURCE_DECLARED), ()
    inter = frozenset_to_tuple(set(value) & set(cap))
    if not inter:
        return (
            FieldValue((), SOURCE_CAPPED),
            (
                Deviation(
                    field_name=name,
                    kind="cap_disjoint_from_observed",
                    configured=tuple(sorted(cap)),
                    observed=value,
                    detail=(
                        "operator cap names no modality the providers "
                        "observed; the guaranteed set is empty"
                    ),
                ),
            ),
        )
    if set(inter) == set(value):
        source = SOURCE_VERIFIED if complete else SOURCE_OBSERVED
    else:
        source = SOURCE_CAPPED
    return FieldValue(inter, source), ()


def _bool_field(
    observed: tuple[TriState, ...], cap: bool | None, name: str
) -> tuple[FieldValue, tuple[Deviation, ...]]:
    """Three-valued AND over the participants, with operator caps applied.

    A cap of False only restricts (it contradicts no evidence). A cap of
    True asserts universal support: it is honoured only when every observed
    participant agrees; a single observed FALSE wins and the conflict is
    reported as a deviation (fail safe — never advertise a control a known
    participant rejects).
    """
    result = _and_tristates(observed)
    known = len([s for s in observed if s is not TriState.UNKNOWN])
    complete = _complete(len(observed), known)
    if cap is None:
        if result is TriState.UNKNOWN:
            return FieldValue(None, SOURCE_UNKNOWN), ()
        value = result is TriState.TRUE
        return (
            FieldValue(value, SOURCE_VERIFIED if complete else SOURCE_OBSERVED),
            (),
        )
    if not cap:
        if result is TriState.TRUE:
            return FieldValue(False, SOURCE_CAPPED), ()
        if result is TriState.FALSE:
            return (
                FieldValue(False, SOURCE_VERIFIED if complete else SOURCE_OBSERVED),
                (),
            )
        return FieldValue(False, SOURCE_DECLARED), ()
    # cap is True: an assertion of universal support.
    if TriState.FALSE in observed:
        return (
            FieldValue(False, SOURCE_VERIFIED if complete else SOURCE_OBSERVED),
            (
                Deviation(
                    field_name=name,
                    kind="cap_conflicts_with_observed",
                    configured=True,
                    observed=False,
                    detail=(
                        "operator cap asserts support, but at least one "
                        "participant's listing says otherwise; the observed "
                        "FALSE wins (fail safe)"
                    ),
                ),
            ),
        )
    if result is TriState.TRUE:
        return (
            FieldValue(True, SOURCE_VERIFIED if complete else SOURCE_OBSERVED),
            (),
        )
    return FieldValue(True, SOURCE_DECLARED), ()


@dataclass(frozen=True)
class ModelContract:
    """The guaranteed (and merely observed) capability of a logical model.

    Every capability is a :class:`FieldValue` — the value plus the strength
    of the evidence behind it (``verified`` / ``capped`` / ``declared`` /
    ``observed`` / ``unknown``). A client surface may only publish fields
    whose source is ``verified`` or ``capped``; ``declared`` and ``observed``
    values are exposed on the admin surface separately, never as promises.
    """

    model: str
    #: True when every participant certified with fresh evidence.
    participants_verified: bool
    participants: tuple[ParticipantStatus, ...]
    context_limit: FieldValue = field(
        default_factory=lambda: FieldValue(None, SOURCE_UNKNOWN)
    )
    input_limit: FieldValue = field(
        default_factory=lambda: FieldValue(None, SOURCE_UNKNOWN)
    )
    output_limit: FieldValue = field(
        default_factory=lambda: FieldValue(None, SOURCE_UNKNOWN)
    )
    input_modalities: FieldValue = field(
        default_factory=lambda: FieldValue(None, SOURCE_UNKNOWN)
    )
    output_modalities: FieldValue = field(
        default_factory=lambda: FieldValue(None, SOURCE_UNKNOWN)
    )
    tool_calling: FieldValue = field(
        default_factory=lambda: FieldValue(None, SOURCE_UNKNOWN)
    )
    reasoning: FieldValue = field(
        default_factory=lambda: FieldValue(None, SOURCE_UNKNOWN)
    )
    #: One of REASONING_FORMAT_* — set only when support is true AND the
    #: fresh participants agree on the encoding (or the operator declared
    #: it against an unencodable-as-yet observation set, see below).
    reasoning_format: str | None = None
    reasoning_levels: FieldValue = field(
        default_factory=lambda: FieldValue(None, SOURCE_UNKNOWN)
    )
    #: True when fresh participants disagree on the reasoning wire encoding —
    #: the admin surface must say the failover set is incoherent about
    #: reasoning, not merely "reasoning: unknown".
    reasoning_divergent: bool = False
    deviations: tuple[Deviation, ...] = ()

    #: Contract field names, in rendering order.
    FIELD_NAMES: tuple[str, ...] = (
        "context_limit",
        "input_limit",
        "output_limit",
        "input_modalities",
        "output_modalities",
        "tool_calling",
        "reasoning",
        "reasoning_levels",
    )

    def field(self, name: str) -> FieldValue:
        value: object = getattr(self, name, None)
        if isinstance(value, FieldValue):
            return value
        return FieldValue(None, SOURCE_UNKNOWN)

    def guaranteed(self) -> bool:
        """True when every participant certified fresh AND every published
        field is backed by verified/capped evidence — i.e. the client block
        :meth:`publishable` renders is a true contract.

        A missing field does not break the guarantee of the fields present.
        """
        if not self.participants_verified:
            return False
        for name in self.FIELD_NAMES:
            fv = self.field(name)
            if fv.value is None or fv.value is TriState.UNKNOWN:
                continue
            if fv.source not in (SOURCE_VERIFIED, SOURCE_CAPPED):
                return False
        return True

    def publishable(self) -> dict[str, Any]:
        """The client-facing subset: verified/capped, non-unknown fields only.

        Absence means unknown — a client should never have to treat a
        published ``null`` as a feature flag.
        """
        out: dict[str, Any] = {}
        for name in (
            "context_limit",
            "input_limit",
            "output_limit",
            "input_modalities",
            "output_modalities",
            "tool_calling",
            "reasoning",
            "reasoning_levels",
        ):
            fv = self.field(name)
            if fv.source not in (SOURCE_VERIFIED, SOURCE_CAPPED):
                continue
            if fv.value is None or fv.value is TriState.UNKNOWN:
                continue
            if name in ("tool_calling", "reasoning", "reasoning_levels"):
                if isinstance(fv.value, TriState):
                    out[name] = fv.value is TriState.TRUE
                elif isinstance(fv.value, tuple):
                    out[name] = list(fv.value)
                else:
                    out[name] = bool(fv.value)
            elif isinstance(fv.value, tuple):
                out[name] = list(fv.value)
            else:
                out[name] = fv.value
        if (
            out.get("reasoning") is True
            and self.reasoning_format is not None
        ):
            out["reasoning_format"] = self.reasoning_format
        return out


def compute_contract(
    model: str,
    participants: tuple[str, ...],
    observations: Mapping[str, ModelObservation | None],
    caps: ModelCaps | None = None,
    *,
    now: float,
    max_age: float,
    current_fingerprints: Mapping[str, str] | None = None,
) -> ModelContract:
    """The LCD contract for ``model`` over its participant set.

    ``participants`` are the provider names that may serve the model for the
    caller's route (model-map alias holders ∩ the route's candidate set).
    They count into the contract **regardless of transient health**: a
    temporarily closed provider returns, and the compatibility contract must
    already cover it. ``observations`` maps provider name → the freshest
    stored observation (aliases pre-resolved by the caller from the model
    map; the alias is carried for reporting). A participant is *fresh* when
    its observation is at most ``max_age`` old at ``now`` and — when its
    current config fingerprint is supplied via ``current_fingerprints`` —
    matches.

    Pure: no I/O, no clock, no randomness.
    """
    caps = caps or ModelCaps()
    current_fps = dict(current_fingerprints or {})

    statuses: list[ParticipantStatus] = []
    fresh: dict[str, ModelObservation] = {}
    for provider in participants:
        obs = observations.get(provider)
        if obs is None:
            statuses.append(
                ParticipantStatus(provider=provider, alias="", state="missing")
            )
            continue
        age = now - obs.observed_at
        current = current_fps.get(provider)
        fingerprint_current: bool | None = None
        if current is not None and obs.config_fingerprint:
            fingerprint_current = current == obs.config_fingerprint
        is_fresh = age <= max_age and fingerprint_current in (None, True)
        statuses.append(
            ParticipantStatus(
                provider=provider,
                alias=obs.alias,
                state="fresh" if is_fresh else "stale",
                observed_at=obs.observed_at,
                fingerprint_current=fingerprint_current,
            )
        )
        if is_fresh:
            fresh[provider] = obs

    def col(attr: str) -> tuple[Any, ...]:
        out: list[Any] = []
        for provider in participants:
            obs = fresh.get(provider)
            if obs is None:
                out.append(None)
                continue
            value: Any = getattr(obs, attr)
            # The normalizer stores an unreported set as an empty tuple (the
            # dataclass has no per-set None), and _intersect_sets needs
            # None to mean "absent" — an empty tuple would otherwise
            # aggregate to a VERIFIED empty set, which tells the client
            # "supports no input modalities" instead of "unknown".
            if (
                attr in ("input_modalities", "output_modalities")
                and isinstance(value, tuple)
                and not value
            ):
                value = None
            out.append(value)
        return tuple(out)

    total = len(participants)
    all_fresh = _complete(total, len(fresh))

    deviations: list[Deviation] = []
    ctx_val, dev = _numeric_field(col("context_limit"), caps.context_limit, "context_limit")
    in_val, dev2 = _numeric_field(col("input_limit"), caps.input_limit, "input_limit")
    out_val, dev3 = _numeric_field(col("output_limit"), caps.output_limit, "output_limit")
    im_val, dev4 = _modality_field(
        col("input_modalities"), caps.input_modalities, "input_modalities"
    )
    om_val, dev5 = _modality_field(
        col("output_modalities"), caps.output_modalities, "output_modalities"
    )
    tc_val, dev6 = _bool_field(col("tool_calling"), caps.tool_calling, "tool_calling")
    re_val, dev7 = _bool_field(col("reasoning"), caps.reasoning, "reasoning")
    deviations.extend(dev + dev2 + dev3 + dev4 + dev5 + dev6 + dev7)

    # Reasoning wire format: meaningful only when support is true. Fresh
    # participants that support reasoning must agree on the encoding; a
    # divergence falls back to boolean-only (levels are never advertised).
    reasoning_true = [
        o for o in fresh.values()
        if o.reasoning is TriState.TRUE and o.reasoning_format is not None
    ]
    formats = {o.reasoning_format for o in reasoning_true}
    divergent = len(formats) > 1
    reasoning_format: str | None
    if re_val.value is True and not divergent and len(formats) == 1:
        reasoning_format = reasoning_true[0].reasoning_format
    elif re_val.value is True and not divergent and caps.reasoning_format:
        # Verified support but no participant revealed the encoding (or it
        # was observed on a participant that did not enumerate it): the
        # operator's declared format stands for rendering only.
        reasoning_format = caps.reasoning_format
    else:
        reasoning_format = None

    # Reasoning levels: intersect across the fresh reasoning participants
    # ONLY when the wire encoding agrees and every one of them named its
    # accepted levels. A declared cap may then narrow — never widen.
    levels_value: Any = None
    levels_source = SOURCE_UNKNOWN
    if re_val.value is True and reasoning_true and not divergent:
        certifiers = [o for o in reasoning_true if o.reasoning_levels]
        if certifiers and len(certifiers) == len(reasoning_true):
            common = set(certifiers[0].reasoning_levels)
            for o in certifiers[1:]:
                common &= set(o.reasoning_levels)
            if common:
                levels_value = frozenset_to_tuple(common)
                levels_source = SOURCE_VERIFIED if all_fresh else SOURCE_OBSERVED
    if re_val.value is True and caps.reasoning_levels:
        declared = tuple(sorted(caps.reasoning_levels))
        if isinstance(levels_value, tuple):
            narrowed = frozenset_to_tuple(set(levels_value) & set(declared))
            if not narrowed:
                levels_value = None
                levels_source = SOURCE_UNKNOWN
                deviations.append(
                    Deviation(
                        field_name="reasoning_levels",
                        kind="cap_disjoint_from_observed",
                        configured=declared,
                        observed=None,
                        detail=(
                            "declared reasoning levels name none the "
                            "providers observed; no levels advertised"
                        ),
                    )
                )
            else:
                levels_value = narrowed
                if set(narrowed) != set(declared):
                    levels_source = SOURCE_CAPPED
        else:
            # No observed level set: the operator's declaration fills the
            # gap — declared, not verified (and wire-able only with a
            # format, which the block above already resolved).
            levels_value = declared
            levels_source = SOURCE_DECLARED

    return ModelContract(
        model=model,
        participants_verified=all_fresh,
        participants=tuple(statuses),
        context_limit=ctx_val,
        input_limit=in_val,
        output_limit=out_val,
        input_modalities=im_val,
        output_modalities=om_val,
        tool_calling=tc_val,
        reasoning=re_val,
        reasoning_format=reasoning_format,
        reasoning_levels=FieldValue(levels_value, levels_source),
        reasoning_divergent=divergent,
        deviations=tuple(deviations),
    )


# ── Client-facing rendering ──────────────────────────────────────────────────


def render_client_models(contracts: tuple[ModelContract, ...]) -> dict[str, Any]:
    """The OpenAI-compatible ``/v1/models`` body for the synthesized catalog.

    Deterministic: models are emitted in sorted id order; ``created`` is the
    fixed epoch (switchboard does not know the upstreams' model creation
    times and inventing them would make the field worse than useless); and
    no provider name or credential appears anywhere. The per-model
    ``x-switchboard`` block carries the guaranteed-only publishable fields
    plus the overall ``guaranteed`` flag.
    """
    data = []
    for contract in sorted(contracts, key=lambda c: c.model):
        entry: dict[str, Any] = {
            "id": contract.model,
            "object": "model",
            "created": 0,
            "owned_by": "switchboard",
        }
        publishable = contract.publishable()
        if publishable:
            entry["x-switchboard"] = {
                "guaranteed": contract.guaranteed(),
                **publishable,
            }
        data.append(entry)
    return {"object": "list", "data": data}


# ── OpenCode adapter ─────────────────────────────────────────────────────────


def _opencode_reasoning_options(
    reasoning_format: str, level: str
) -> dict[str, Any]:
    """The exact per-level wire options for the verified encoding."""
    if reasoning_format == REASONING_FORMAT_EFFORT:
        return {"reasoningEffort": level}
    return {"reasoning": {"effort": level}}


def render_opencode_model(config: dict[str, Any], contract: ModelContract) -> None:
    """Fill one OpenCode model entry from a contract (mutates ``config``).

    Evidenced fields only: a ``declared`` or merely ``observed`` value is
    NOT written into a machine-consumed client config — the operator sees
    it in /admin/model-capabilities and adds it by hand if they choose.

    Reasoning gets the strictest treatment: ``reasoning: true`` is written
    only when support is verified/capped AND the accepted levels are
    verified/capped (then written as named ``variants`` in the verified wire
    format). The combination "verified support, unverified levels" is
    omitted entirely: a bare ``reasoning: true`` makes OpenCode fabricate
    generic effort variants for a control at least one failover target may
    reject or ignore.
    """
    def pub(name: str) -> FieldValue | None:
        fv = contract.field(name)
        if fv.source in (SOURCE_VERIFIED, SOURCE_CAPPED) and fv.value is not None:
            return fv
        return None

    limit: dict[str, Any] = {}
    for field_name, key in (
        ("context_limit", "context"),
        ("input_limit", "input"),
        ("output_limit", "output"),
    ):
        fv = pub(field_name)
        if fv is not None:
            limit[key] = fv.value
    if limit:
        config["limit"] = limit

    im = pub("input_modalities")
    if im is not None and isinstance(im.value, tuple):
        config["input"] = list(im.value)
        if "image" in im.value:
            config["attachment"] = True
    om = pub("output_modalities")
    if om is not None and isinstance(om.value, tuple):
        config["output"] = list(om.value)

    tc = pub("tool_calling")
    if tc is not None:
        config["tool_call"] = (
            tc.value is TriState.TRUE
            if isinstance(tc.value, TriState)
            else bool(tc.value)
        )

    re_fv = pub("reasoning")
    levels = pub("reasoning_levels")
    if (
        re_fv is not None
        and levels is not None
        and isinstance(levels.value, tuple)
        and levels.value
        and contract.reasoning_format is not None
    ):
        config["reasoning"] = True
        config["variants"] = {
            level: {
                "options": _opencode_reasoning_options(
                    contract.reasoning_format, level
                )
            }
            for level in sorted(set(levels.value))
        }


def render_opencode_config(
    contracts: tuple[ModelContract, ...],
    *,
    base_url: str,
    provider_name: str = "switchboard",
    api_key: str = "<your-switchboard-route-key>",
) -> dict[str, Any]:
    """A paste-ready OpenCode custom-provider block for the estate.

    ``base_url`` is the client-facing switchboard URL (the shell derives it
    from the request's Host). ``api_key`` is a placeholder by design:
    switchboard does not mint route keys into a document that may end up
    pasted somewhere else.
    """
    models: dict[str, Any] = {}
    for contract in sorted(contracts, key=lambda c: c.model):
        entry: dict[str, Any] = {"name": contract.model}
        render_opencode_model(entry, contract)
        models[contract.model] = entry
    return {
        "provider": {
            provider_name: {
                "name": "Switchboard",
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": base_url, "apiKey": api_key},
                "models": models,
            }
        }
    }


def observation_to_dict(obs: ModelObservation) -> dict[str, Any]:
    """JSON-safe form of an observation for the admin surfaces."""
    return {
        "provider": obs.provider,
        "alias": obs.alias,
        "context_limit": obs.context_limit,
        "input_limit": obs.input_limit,
        "output_limit": obs.output_limit,
        "input_modalities": list(obs.input_modalities),
        "output_modalities": list(obs.output_modalities),
        "tool_calling": obs.tool_calling.value,
        "reasoning": obs.reasoning.value,
        "reasoning_format": obs.reasoning_format,
        "reasoning_levels": list(obs.reasoning_levels),
        "observed_at": obs.observed_at,
        "source": obs.source,
        "config_fingerprint": obs.config_fingerprint,
    }


def contract_to_dict(contract: ModelContract) -> dict[str, Any]:
    """JSON-safe form of a contract for the admin matrix."""
    out: dict[str, Any] = {
        "model": contract.model,
        "guaranteed": contract.guaranteed(),
        "participants_verified": contract.participants_verified,
        "reasoning_divergent": contract.reasoning_divergent,
        "reasoning_format": contract.reasoning_format,
        "participants": [
            {
                "provider": p.provider,
                "alias": p.alias,
                "state": p.state,
                "observed_at": p.observed_at,
                "fingerprint_current": p.fingerprint_current,
            }
            for p in contract.participants
        ],
        "deviations": [
            {
                "field": d.field_name,
                "kind": d.kind,
                "configured": d.configured,
                "observed": d.observed,
                "detail": d.detail,
            }
            for d in contract.deviations
        ],
    }
    for name in contract.FIELD_NAMES:
        fv = contract.field(name)
        value: Any
        if fv.value is None or fv.value is TriState.UNKNOWN:
            value = None
        elif isinstance(fv.value, TriState):
            value = fv.value.value
        elif isinstance(fv.value, tuple):
            value = list(fv.value)
        else:
            value = fv.value
        out[name] = {"value": value, "source": fv.source}
    return out


__all__ = [
    "REASONING_FORMAT_EFFORT",
    "REASONING_FORMAT_OBJECT",
    "SOURCE_CAPPED",
    "SOURCE_DECLARED",
    "SOURCE_OBSERVED",
    "SOURCE_UNKNOWN",
    "SOURCE_VERIFIED",
    "Deviation",
    "FieldValue",
    "ModelCaps",
    "ModelContract",
    "ModelObservation",
    "ParticipantStatus",
    "TriState",
    "compute_contract",
    "contract_to_dict",
    "extract_listing_items",
    "normalize_model_item",
    "observation_to_dict",
    "parse_model_listing",
    "render_client_models",
    "render_opencode_config",
    "render_opencode_model",
]
