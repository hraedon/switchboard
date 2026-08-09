"""Config switchboard will never read must be rejected, not ignored (WI-003).

The reported failure: `route_table_store = "..."` written at the top level of
a config file. That is the spelling of both the CLI flag (`--route-table-store`)
and the env var (`SWITCHBOARD_ROUTE_TABLE_STORE`), and it sits in `_DEFAULTS`
next to genuine top-level keys, so it is the obvious guess. switchboard read
`[route_table] store` instead, found nothing, and started with an in-memory
route table — healthy, serving, and silently discarding every runtime edit at
the next restart.

A rejected config costs one startup. An ignored one costs however long it takes
someone to notice their persisted routes were never persisted.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from switchboard.cli import _ConfigError, _validate_config_pre_build

_MINIMAL_PROVIDER = {
    "provider": {"umans": {"upstream": "https://api.example.com", "target": 1}}
}


def _validate(extra: dict) -> None:
    _validate_config_pre_build({**_MINIMAL_PROVIDER, **extra})


def test_top_level_route_table_store_is_rejected() -> None:
    """The reported bug. Must fail, and must say where the key belongs."""
    with pytest.raises(_ConfigError) as excinfo:
        _validate({"route_table_store": "/var/lib/switchboard/routes.sqlite3"})
    message = str(excinfo.value)
    assert "route_table_store" in message
    # The error is only useful if it contains the fix.
    assert "[route_table]" in message
    assert "store" in message


def test_the_correct_form_is_accepted() -> None:
    _validate({"route_table": {"store": "/var/lib/switchboard/routes.sqlite3"}})


def test_non_string_store_is_rejected() -> None:
    """Same failure in a different costume: the store looks configured and
    isn't."""
    with pytest.raises(_ConfigError, match="route_table.store"):
        _validate({"route_table": {"store": 1234}})


def test_typo_inside_route_table_is_rejected() -> None:
    """`path` instead of `store` would otherwise be silently ignored."""
    with pytest.raises(_ConfigError, match="route_table.path"):
        _validate({"route_table": {"path": "/tmp/x.sqlite3"}})


@pytest.mark.parametrize(
    ("key", "expected_section"),
    [
        ("strategy", "[routing]"),
        ("dwell_interval", "[routing]"),
        ("pace_burn_rate_per_day", "[routing]"),
        ("providers", "[route.default]"),
        ("upstream", "[provider."),
        ("target", "[provider."),
    ],
)
def test_other_misplaced_keys_name_their_section(
    key: str, expected_section: str
) -> None:
    """Every one of these is a plausible top-level guess for a key that lives
    in a section. Naming the section is the difference between a five-second
    fix and a bisect."""
    with pytest.raises(_ConfigError) as excinfo:
        _validate({key: "x" if key != "target" else 1})
    assert expected_section in str(excinfo.value)


def test_unknown_section_is_rejected_and_lists_the_known_ones() -> None:
    with pytest.raises(_ConfigError) as excinfo:
        _validate({"routeing": {"strategy": "pace"}})
    message = str(excinfo.value)
    assert "routeing" in message
    assert "[routing]" in message


def test_unknown_scalar_is_rejected() -> None:
    with pytest.raises(_ConfigError, match="admin_tokn"):
        _validate({"admin_tokn": "secret"})


@pytest.mark.parametrize(
    "key",
    [
        "listen",
        "log_level",
        "admin_token",
        "queue_timeout",
        "drain_timeout",
        "max_request_body_bytes",
    ],
)
def test_genuine_top_level_keys_still_pass(key: str) -> None:
    """The whitelist must not have gained a false positive: every key
    switchboard actually reads at the top level has to survive."""
    values: dict[str, object] = {
        "listen": "127.0.0.1:8801",
        "log_level": "INFO",
        "admin_token": "t",
        "queue_timeout": 30.0,
        "drain_timeout": 25.0,
        "max_request_body_bytes": 1024,
    }
    _validate({key: values[key]})


@pytest.mark.parametrize(
    "section",
    [
        "route",
        "routing",
        "route_table",
        "model",
        "overload",
        "reroute",
        "threshold",
        "token_budget",
        "usage_24h_budget",
    ],
)
def test_genuine_sections_still_pass(section: str) -> None:
    _validate({section: {}})


@pytest.mark.parametrize(
    "path", sorted(Path(__file__).parent.parent.glob("examples/*.toml"))
)
def test_shipped_examples_validate(path: Path) -> None:
    """A whitelist is only as good as its coverage. If an example config we
    ship is rejected, the whitelist is wrong — not the example."""
    _validate_config_pre_build(tomllib.loads(path.read_text()))
