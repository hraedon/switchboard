"""The two routing-config surfaces must agree.

switchboard accepts routing configuration through two doors: TOML (validated
by ``_validate_config`` at boot and by ``switchboard config validate``) and the
admin API (``PUT /admin/config/routing``). They are separate code paths, and
they had already drifted once — the API rejected ``headroom_threshold = 0.0``,
the documented "disabled" value, and ``pace_burn_rate_per_day = 1.0``, both of
which TOML accepted. An operator hitting that writes a config file that works
at boot and is refused by the GUI, with no way to tell which door is wrong.

Both now read ``control.ROUTING_FIELD_BOUNDS``. These tests drive the two
surfaces end to end with the same probe values and assert the verdicts match,
so a future change that bypasses the shared table fails here rather than in
production.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from switchboard.admin import (
    handle_config_get,
    handle_routing_config_update,
    routing_config_payload,
)
from switchboard.cli import _ConfigError, _validate_config
from switchboard.control import (
    MUTABLE_ROUTING_FIELDS,
    ROUTING_BOOL_FIELDS,
    ROUTING_FIELD_BOUNDS,
    ROUTING_STRATEGIES,
    RoutingConfig,
    coerce_routing_value,
)


class _FakeProxyApp:
    def __init__(self, config: RoutingConfig) -> None:
        self._routing_config = config

    @property
    def routing_config(self) -> RoutingConfig:
        return self._routing_config

    def update_routing_config(self, config: RoutingConfig) -> None:
        self._routing_config = config


def _make_receive(body: bytes):
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _make_send():
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    return messages, send


def _authed_scope() -> dict[str, Any]:
    return {
        "type": "http",
        "method": "PUT",
        "path": "/admin/config/routing",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
        "query_string": b"",
    }


async def _api_accepts(field: str, value: object) -> bool:
    """Does PUT /admin/config/routing accept this field/value?"""
    app = _FakeProxyApp(RoutingConfig())
    messages, send = _make_send()
    await handle_routing_config_update(
        send,
        _make_receive(json.dumps({field: value}).encode()),
        app,
        "admin-secret",
        _authed_scope(),
    )
    start = next(m for m in messages if m["type"] == "http.response.start")
    return bool(start["status"] == 200)


def _toml_accepts(field: str, value: object) -> bool:
    """Does TOML validation accept this field/value?"""
    try:
        _validate_config({"routing": {field: value}}, {})
    except _ConfigError as exc:
        return field not in str(exc)
    return True


def _probes(field: str) -> list[object]:
    """Values that straddle every boundary of a field's accepted range."""
    if field == "strategy":
        return [*ROUTING_STRATEGIES, "sideways", "", 1, True, None]
    if field in ROUTING_BOOL_FIELDS:
        return [True, False, "true", 1, 0, None]

    bound = ROUTING_FIELD_BOUNDS[field]
    values: list[object] = [float("nan"), float("inf"), "0.5", True]
    for edge in (bound.minimum, bound.maximum):
        if edge is None:
            continue
        values.extend([edge, edge - 0.001, edge + 0.001, edge - 1, edge + 1])
    if bound.integer:
        values.extend([1, 2, 1.5])
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(MUTABLE_ROUTING_FIELDS))
async def test_surfaces_agree_on_every_mutable_field(field: str) -> None:
    """For every runtime-mutable routing field, TOML and the admin API return
    the same verdict on the same value — at, inside, and outside each bound."""
    disagreements: list[str] = []
    for value in _probes(field):
        if value is None:
            continue  # TOML has no null; absence means "unset" on both sides.
        via_toml = _toml_accepts(field, value)
        via_api = await _api_accepts(field, value)
        if via_toml != via_api:
            disagreements.append(
                f"{field}={value!r}: toml={'accept' if via_toml else 'reject'} "
                f"api={'accept' if via_api else 'reject'}"
            )
    assert not disagreements, "surfaces disagree:\n  " + "\n  ".join(disagreements)


@pytest.mark.asyncio
async def test_disabled_sentinel_zero_is_accepted_by_both() -> None:
    """0.0 is the documented "signal off" value for the threshold knobs. The
    admin API used to reject it while TOML accepted it, so an operator could
    not turn a threshold off from the GUI."""
    for field in (
        "headroom_threshold",
        "token_budget_threshold",
        "usage_24h_threshold",
        "opportunistic_margin",
        "pace_flap_margin",
    ):
        assert _toml_accepts(field, 0.0), f"TOML rejected {field}=0.0"
        assert await _api_accepts(field, 0.0), f"admin API rejected {field}=0.0"


@pytest.mark.asyncio
async def test_inclusive_maximum_is_accepted_by_both() -> None:
    """1.0 is in range for these; the admin API used to reject it."""
    for field in ("pace_burn_rate_per_day", "opportunistic_min_headroom"):
        assert _toml_accepts(field, 1.0), f"TOML rejected {field}=1.0"
        assert await _api_accepts(field, 1.0), f"admin API rejected {field}=1.0"


async def _put(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """PUT the payload and return (status, decoded body)."""
    app = _FakeProxyApp(RoutingConfig())
    messages, send = _make_send()
    await handle_routing_config_update(
        send,
        _make_receive(json.dumps(payload).encode()),
        app,
        "admin-secret",
        _authed_scope(),
    )
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(body)


# ── reporting surfaces ─────────────────────────────────────────────────────
#
# Three surfaces report a RoutingConfig, and each used to hand-list its fields.
# `quarantine_threshold` (Plan 023) was settable, validated and persisted but
# missing from two of the three, so an operator had no way to read back what
# the running proxy was actually using. These tests fail if a knob is added
# without reaching every surface, which is the failure that happened.


@pytest.mark.asyncio
async def test_get_config_reports_every_mutable_field() -> None:
    messages, send = _make_send()
    await handle_config_get(send, RoutingConfig())
    body = json.loads(
        b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
    )
    missing = [f for f in MUTABLE_ROUTING_FIELDS if f not in body]
    assert not missing, f"GET /admin/config does not report: {missing}"


@pytest.mark.asyncio
async def test_put_response_reports_every_mutable_field() -> None:
    status, body = await _put({"dwell_interval": 12.0})
    assert status == 200
    missing = [f for f in MUTABLE_ROUTING_FIELDS if f not in body]
    assert not missing, f"the PUT response does not report: {missing}"


def test_status_json_reports_every_mutable_field() -> None:
    """/status.json is the surface an operator reads first; a knob absent from
    it is a knob they cannot confirm without shell access to the pod."""
    payload = routing_config_payload(RoutingConfig())
    missing = [f for f in MUTABLE_ROUTING_FIELDS if f not in payload]
    assert not missing, f"/status.json routing_config does not report: {missing}"


def test_the_reported_payload_is_json_serialisable() -> None:
    """``strategy`` is an enum; every surface must emit its value, not repr."""
    payload = routing_config_payload(RoutingConfig())
    assert json.loads(json.dumps(payload))["strategy"] in ROUTING_STRATEGIES


# ── value fidelity ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_integer_knob_survives_the_admin_api_as_an_integer() -> None:
    """Both runtime paths cast with float() regardless of the declared type,
    so `quarantine_threshold = 3` came back as 3.0 through the API while TOML
    kept it an int — the same config value reading differently per door."""
    status, body = await _put({"quarantine_threshold": 3})
    assert status == 200
    assert body["quarantine_threshold"] == 3
    assert isinstance(body["quarantine_threshold"], int), (
        f"got {body['quarantine_threshold']!r}, a "
        f"{type(body['quarantine_threshold']).__name__}"
    )


@pytest.mark.parametrize("field", sorted(MUTABLE_ROUTING_FIELDS))
def test_coercion_matches_the_type_the_dataclass_declares(field: str) -> None:
    """Whatever `coerce_routing_value` returns is written straight into
    RoutingConfig, so it must match the declared type of that field."""
    declared = type(getattr(RoutingConfig(), field))
    probe: object
    if field == "strategy":
        probe = ROUTING_STRATEGIES[0]
    elif field in ROUTING_BOOL_FIELDS:
        probe = True
    elif ROUTING_FIELD_BOUNDS[field].integer:
        probe = 2
    else:
        probe = 0.25
    coerced = coerce_routing_value(field, probe)
    assert isinstance(coerced, declared), (
        f"{field}: coerced to {type(coerced).__name__}, "
        f"RoutingConfig declares {declared.__name__}"
    )


def test_every_mutable_field_has_a_bound_or_is_typed() -> None:
    """A field the admin API will write must be range-checked or a known type.
    Adding a field to MUTABLE_ROUTING_FIELDS without a bounds entry would
    otherwise let an unvalidated value into RoutingConfig."""
    for field in MUTABLE_ROUTING_FIELDS:
        assert (
            field in ROUTING_FIELD_BOUNDS
            or field in ROUTING_BOOL_FIELDS
            or field == "strategy"
        ), f"{field} is mutable but has no bounds entry and no known type"
