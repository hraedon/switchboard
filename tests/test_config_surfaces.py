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

from switchboard.admin import handle_routing_config_update
from switchboard.cli import _ConfigError, _validate_config
from switchboard.control import (
    MUTABLE_ROUTING_FIELDS,
    ROUTING_BOOL_FIELDS,
    ROUTING_FIELD_BOUNDS,
    ROUTING_STRATEGIES,
    RoutingConfig,
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
