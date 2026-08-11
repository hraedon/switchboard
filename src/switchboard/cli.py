"""``switchboard`` command-line entry point.

``switchboard serve`` runs the multi-provider routing proxy.

Config precedence: flags → environment variables → config file → built-in defaults.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from switchboard import __version__
from switchboard.config_reset import ResetError, parse_sections, reset_sections
from switchboard.control import (
    MUTABLE_ROUTING_FIELDS,
    ROUTING_BOOL_FIELDS,
    ROUTING_FIELD_BOUNDS,
    ROUTING_STRATEGIES,
    RoutingStrategy,
    coerce_routing_value,
    validate_routing_field,
)
from switchboard.env_config import EnvOverrideError, apply_overrides

log = logging.getLogger("switchboard.cli")

_ENV_PREFIX = "SWITCHBOARD_"

_DEFAULTS: dict[str, Any] = {
    "listen": "127.0.0.1:8801",
    "log_level": "INFO",
    "config": None,
    "admin_token": None,
    "queue_timeout": 30.0,
    "drain_timeout": 25.0,
    "route_table_store": None,
    "max_request_body_bytes": None,
}

_LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR")


class _ConfigError(Exception):
    """Raised when the configuration is invalid."""


def _resolve(
    key: str,
    args: argparse.Namespace,
    config_data: dict[str, Any] | None = None,
) -> Any:
    """Resolve a config value: flag → env var → config file → default.

    A top-level key in the TOML config only applies when neither the CLI
    flag nor the ``SWITCHBOARD_<KEY>`` env var supplied a value; argparse's
    ``default=None`` is treated as "not supplied", never as a user choice.
    """
    flag_value = getattr(args, key, None)
    if flag_value is not None:
        return flag_value
    env_key = f"{_ENV_PREFIX}{key.upper()}"
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return env_value
    if config_data is not None and key in config_data:
        return config_data[key]
    return _DEFAULTS.get(key)


def _resolve_float(
    key: str,
    args: argparse.Namespace,
    config_data: dict[str, Any] | None = None,
) -> float:
    """Resolve a float config value: flag → env var → config file → default.

    A present-but-invalid value raises ``_ConfigError`` naming the key
    instead of silently substituting the default: a typo'd timeout must
    fail startup. "Absent" (no flag, env var, or config key) still uses
    the built-in default. Explicit type checks reject booleans, which
    ``float(True)`` would otherwise silently turn into ``1.0``.
    """
    flag_value = getattr(args, key, None)
    if flag_value is not None:
        return float(flag_value)
    env_key = f"{_ENV_PREFIX}{key.upper()}"
    env_value = os.environ.get(env_key)
    if env_value is not None:
        try:
            return float(env_value)
        except ValueError:
            raise _ConfigError(
                f"{key}: {env_key} is not a number: {env_value!r}"
            ) from None
    if config_data is not None and key in config_data:
        value = config_data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _ConfigError(
                f"{key}: config value must be a number, got {value!r}"
            )
        return float(value)
    return float(_DEFAULTS[key])


def _load_toml_config(path: str) -> dict[str, Any]:
    """Load a TOML config file."""
    import tomllib

    p = Path(path)
    if not p.exists():
        raise _ConfigError(f"config file not found: {path}")
    with p.open("rb") as f:
        return tomllib.load(f)


def _validate_provider_config(
    name: str, cfg: dict[str, object]
) -> list[str]:
    """Validate a single provider config section. Returns list of errors."""
    errors: list[str] = []

    upstream = cfg.get("upstream")
    if not isinstance(upstream, str) or not upstream.strip():
        errors.append(f"provider '{name}': missing or empty 'upstream'")

    provider_type = cfg.get("type", "generic")
    if not isinstance(provider_type, str):
        errors.append(f"provider '{name}': 'type' must be a string")

    target = cfg.get("target", 3)
    if isinstance(target, int) and not isinstance(target, bool):
        if target < 0:
            errors.append(f"provider '{name}': 'target' must be >= 0")
    elif not isinstance(target, int) or isinstance(target, bool):
        errors.append(f"provider '{name}': 'target' must be an integer")

    direct_usage = cfg.get("direct_usage")
    if direct_usage is not None and not isinstance(direct_usage, bool):
        errors.append(f"provider '{name}': direct_usage must be a boolean")

    for key in ("direct_usage_stale_ttl", "direct_usage_poll_interval"):
        val = cfg.get(key)
        if val is not None and (
            not isinstance(val, (int, float)) or isinstance(val, bool)
        ):
            errors.append(f"provider '{name}': {key} must be a number")

    # A cookie in the config file would be committed. It is a session
    # credential for a whole account, broader than the API key beside it, so
    # the only accepted form is an environment variable name (Plan 022 WI-2).
    if cfg.get("direct_usage_cookie") is not None:
        errors.append(
            f"provider '{name}': direct_usage_cookie must not be set in "
            "config — use direct_usage_cookie_env to name an environment "
            "variable instead"
        )

    return errors


# Every key switchboard reads at the top level of a config file. A key that is
# not here is not "extra config" — it is config the operator wrote and
# switchboard will never look at (WI-003).
_KNOWN_TOP_LEVEL_SCALARS: frozenset[str] = frozenset({
    "listen",
    "log_level",
    "admin_token",
    "queue_timeout",
    "drain_timeout",
    "max_request_body_bytes",
})

_KNOWN_TOP_LEVEL_TABLES: frozenset[str] = frozenset({
    "provider",
    "route",
    "route_table",
    "routing",
    "model",
    "overload",
    "reroute",
    "threshold",
    "token_budget",
    "usage_24h_budget",
})

# Mistakes worth naming the fix for, rather than only saying "unknown key".
# `route_table_store` is the one that has actually bitten (WI-003): it is the
# spelling of both the CLI flag and the env var, and it sits in `_DEFAULTS`
# beside genuine top-level keys, so writing it at the top level of a TOML file
# is the obvious guess — and it was silently ignored, leaving an operator with
# a store that looked configured and persisted nothing.
_MISPLACED_TOP_LEVEL_KEYS: dict[str, str] = {
    "route_table_store": '[route_table]\nstore = "..."',
    "store": '[route_table]\nstore = "..."',
    "strategy": '[routing]\nstrategy = "..."',
    "dwell_interval": "[routing]\ndwell_interval = ...",
    "failback_delay": "[routing]\nfailback_delay = ...",
    "headroom_ranking": "[routing]\nheadroom_ranking = ...",
    "pace_burn_rate_per_day": "[routing]\npace_burn_rate_per_day = ...",
    "pace_flap_margin": "[routing]\npace_flap_margin = ...",
    "providers": '[route.default]\nproviders = ["..."]',
    "upstream": '[provider.<name>]\nupstream = "..."',
    "target": "[provider.<name>]\ntarget = ...",
}


def _validate_unknown_top_level(config_data: dict[str, Any]) -> list[str]:
    """Reject top-level keys switchboard will never read (WI-003).

    A misplaced key is worse than a rejected one: switchboard starts, reports
    healthy, and quietly does not do the thing the operator configured. The
    reported instance was `route_table_store` written at the top level — the
    spelling of the flag and the env var — which left the route table
    in-memory while the config file said otherwise, so every runtime edit
    vanished at the next restart with nothing to explain it.
    """
    errors: list[str] = []
    known = _KNOWN_TOP_LEVEL_SCALARS | _KNOWN_TOP_LEVEL_TABLES
    for key, value in config_data.items():
        if key in known:
            continue
        correction = _MISPLACED_TOP_LEVEL_KEYS.get(key)
        if correction is not None:
            errors.append(
                f"'{key}' is not a top-level key and would be ignored. "
                f"Write it as:\n    {correction.replace(chr(10), chr(10) + '    ')}"
            )
        elif isinstance(value, dict):
            errors.append(
                f"unknown config section '[{key}]' — switchboard does not read "
                "it. Known sections: "
                + ", ".join(f"[{s}]" for s in sorted(_KNOWN_TOP_LEVEL_TABLES))
            )
        else:
            errors.append(
                f"unknown top-level key '{key}' — switchboard does not read "
                "it. Known keys: " + ", ".join(sorted(_KNOWN_TOP_LEVEL_SCALARS))
            )
    return errors


def _validate_serve_keys(config_data: dict[str, Any]) -> list[str]:
    """Validate top-level serve keys present in the TOML config (WI-3b).

    Mirrors the validation argparse applies to the equivalent CLI flag, so a
    config typo fails startup with a switchboard error instead of surfacing
    later as a uvicorn error or a silently substituted default.
    """
    errors: list[str] = _validate_unknown_top_level(config_data)

    rt_section = config_data.get("route_table")
    if rt_section is not None:
        if not isinstance(rt_section, dict):
            errors.append("[route_table] must be a table")
        else:
            store = rt_section.get("store")
            if store is not None and not isinstance(store, str):
                # Silently ignoring a non-string here is the same failure in a
                # different costume: the store looks configured and isn't.
                errors.append(
                    f"route_table.store must be a string path, got {store!r}"
                )
            for extra in set(rt_section) - {"store"}:
                errors.append(
                    f"unknown key 'route_table.{extra}' — the only key in "
                    "[route_table] is 'store'"
                )

    log_level = config_data.get("log_level")
    if log_level is not None:
        if not isinstance(log_level, str):
            errors.append(f"log_level must be a string, got {log_level!r}")
        elif log_level not in _LOG_LEVEL_CHOICES:
            errors.append(
                "log_level must be one of "
                + ", ".join(_LOG_LEVEL_CHOICES)
                + f" (got {log_level!r})"
            )

    for key in ("queue_timeout", "drain_timeout"):
        value = config_data.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            errors.append(f"{key} must be a number, got {value!r}")

    listen = config_data.get("listen")
    if listen is not None and not isinstance(listen, str):
        errors.append(f"listen must be a string, got {listen!r}")

    admin_token = config_data.get("admin_token")
    if admin_token is not None and not isinstance(admin_token, str):
        errors.append(f"admin_token must be a string, got {admin_token!r}")

    return errors


def _validate_config_pre_build(
    config_data: dict[str, Any],
) -> None:
    """Validate config before building provider contexts (WI-006.8, WI-3b).

    Checks that don't need live provider contexts: empty upstreams,
    invalid targets, duplicate names, unknown provider types, hashed-key
    format for file-defined routes, and top-level serve keys.
    """
    import re

    errors: list[str] = []
    errors.extend(_validate_serve_keys(config_data))

    raw_providers = config_data.get("provider", {})
    if isinstance(raw_providers, dict):
        for name, raw in raw_providers.items():
            if not isinstance(raw, dict):
                errors.append(f"provider '{name}': config must be a table")
                continue
            errors.extend(_validate_provider_config(name, raw))

    route_section = config_data.get("route", {})
    if isinstance(route_section, dict):
        for section_name in route_section:
            if section_name == "default":
                continue
            if not re.match(r"^[0-9a-f]{64}$", section_name):
                errors.append(
                    f"route '{section_name}': key must be a SHA-256 hash "
                    f"(64 hex chars)"
                )

    if errors:
        raise _ConfigError("; ".join(errors))


def _validate_config(
    config_data: dict[str, Any],
    providers: dict[str, Any],
    *,
    tombstoned: frozenset[str] = frozenset(),
) -> None:
    """Validate all configuration references (WI-006.8).

    Raises ``_ConfigError`` on the first validation failure.

    ``tombstoned`` names providers the config store has disabled
    (Plan 020 D1). A TOML reference to one of those is a deliberate
    operator action, not a typo, and must not brick the next boot —
    it degrades to a warning, and the routing core already skips
    candidates with no live state. A name in neither set stays a
    hard error: that IS the typo case validation exists for.
    """
    errors: list[str] = []
    warnings: list[str] = []

    def _check_ref(name: object, message: str) -> None:
        if not isinstance(name, str):
            return
        if name in providers:
            return
        if name in tombstoned:
            warnings.append(f"{message} (disabled in the config store)")
        else:
            errors.append(message)

    raw_providers = config_data.get("provider", {})
    if isinstance(raw_providers, dict):
        seen_names: set[str] = set()
        for name, raw in raw_providers.items():
            if name in seen_names:
                errors.append(f"duplicate provider name: '{name}'")
            seen_names.add(name)
            if isinstance(raw, dict):
                errors.extend(_validate_provider_config(name, raw))

    route_section = config_data.get("route", {})
    if isinstance(route_section, dict):
        default_cfg = route_section.get("default", {})
        if isinstance(default_cfg, dict):
            default_providers = default_cfg.get("providers")
            if isinstance(default_providers, list):
                for p in default_providers:
                    if not isinstance(p, str):
                        errors.append(
                            f"default route: provider '{p}' must be a string"
                        )
                    else:
                        _check_ref(
                            p,
                            f"default route references unknown provider: '{p}'",
                        )

        for section_name, section_data in route_section.items():
            if section_name == "default":
                continue
            if not isinstance(section_data, dict):
                continue
            providers_list = section_data.get("providers")
            if isinstance(providers_list, list):
                for p in providers_list:
                    if not isinstance(p, str):
                        errors.append(
                            f"route '{section_name}': provider must be a string"
                        )
                    else:
                        _check_ref(
                            p,
                            f"route '{section_name}' references unknown "
                            f"provider: '{p}'",
                        )

    routing_section = config_data.get("routing", {})
    if isinstance(routing_section, dict):
        # Every routing field is range-checked against the single shared
        # table in control.ROUTING_FIELD_BOUNDS, which the admin API
        # (PUT /admin/config/routing) also reads.  These two surfaces used to
        # carry independent copies of the bounds and had drifted apart;
        # test_config_surfaces asserts they agree.
        for field_name in (
            *ROUTING_FIELD_BOUNDS,
            *sorted(ROUTING_BOOL_FIELDS),
            "strategy",
        ):
            value = routing_section.get(field_name)
            if value is None:
                continue
            message = validate_routing_field(field_name, value)
            if message is not None:
                errors.append(f"routing.{message}")
        # Individually valid but mutually exclusive: strategy="headroom"
        # already means what headroom_ranking=true means, and strategy="pace"
        # contradicts it. Fail rather than silently pick one.
        strategy = routing_section.get("strategy")
        if (
            isinstance(strategy, str)
            and strategy in ROUTING_STRATEGIES
            and strategy != "ordered"
        ):
            hr = routing_section.get("headroom_ranking")
            if isinstance(hr, bool) and hr:
                errors.append(
                    f"routing.strategy={strategy} and headroom_ranking=true "
                    "are mutually exclusive — use strategy alone"
                )

    usage_24h_budget_section = config_data.get("usage_24h_budget", {})
    if isinstance(usage_24h_budget_section, dict):
        for prov_name, prov_cfg in usage_24h_budget_section.items():
            if not isinstance(prov_cfg, dict):
                errors.append(
                    f"usage_24h_budget.'{prov_name}': must be a table"
                )
                continue
            cap = prov_cfg.get("cap_tokens")
            if not isinstance(cap, int) or isinstance(cap, bool):
                errors.append(
                    f"usage_24h_budget.'{prov_name}': "
                    f"cap_tokens must be an integer"
                )
            elif cap <= 0:
                errors.append(
                    f"usage_24h_budget.'{prov_name}': cap_tokens must be > 0"
                )
            if prov_name not in providers:
                _check_ref(
                    prov_name,
                    f"usage_24h_budget.'{prov_name}': "
                    f"references unknown provider",
                )

    token_budget_section = config_data.get("token_budget", {})
    if isinstance(token_budget_section, dict):
        for prov_name, prov_cfg in token_budget_section.items():
            if not isinstance(prov_cfg, dict):
                errors.append(
                    f"token_budget.'{prov_name}': must be a table"
                )
                continue
            cap = prov_cfg.get("cap_tokens")
            if not isinstance(cap, int) or isinstance(cap, bool):
                errors.append(
                    f"token_budget.'{prov_name}': cap_tokens must be an integer"
                )
            elif cap <= 0:
                errors.append(
                    f"token_budget.'{prov_name}': cap_tokens must be > 0"
                )
            window = prov_cfg.get("window_seconds", 3600.0)
            if not isinstance(window, (int, float)) or isinstance(
                window, bool
            ):
                errors.append(
                    f"token_budget.'{prov_name}': "
                    f"window_seconds must be a number"
                )
            elif window <= 0:
                errors.append(
                    f"token_budget.'{prov_name}': "
                    f"window_seconds must be > 0"
                )
            soft = prov_cfg.get("soft_threshold", 0.85)
            if not isinstance(soft, (int, float)) or isinstance(
                soft, bool
            ):
                errors.append(
                    f"token_budget.'{prov_name}': "
                    f"soft_threshold must be a number"
                )
            elif not (0.0 < soft <= 1.0):
                errors.append(
                    f"token_budget.'{prov_name}': "
                    f"soft_threshold must be in (0.0, 1.0]"
                )
            if prov_name not in providers:
                _check_ref(
                    prov_name,
                    f"token_budget.'{prov_name}': references unknown provider",
                )

    model_section = config_data.get("model", {})
    if isinstance(model_section, dict):
        for model_name, provider_map in model_section.items():
            if not isinstance(provider_map, dict):
                errors.append(
                    f"model '{model_name}': must be a table of provider → alias"
                )
                continue
            for provider_name, alias in provider_map.items():
                if not isinstance(alias, str):
                    errors.append(
                        f"model '{model_name}': alias for '{provider_name}' must be a string"
                    )
                if provider_name not in providers:
                    _check_ref(
                        provider_name,
                        f"model '{model_name}': references unknown provider "
                        f"'{provider_name}'",
                    )

    overload_section = config_data.get("overload", {})
    if isinstance(overload_section, dict):
        threshold = overload_section.get("threshold")
        if threshold is not None:
            if not isinstance(threshold, int) or isinstance(threshold, bool):
                errors.append("overload.threshold must be an integer")
            elif threshold < 1:
                errors.append("overload.threshold must be >= 1")
        for key in ("cooldown_default", "cooldown_min", "cooldown_max"):
            val = overload_section.get(key)
            if val is not None and (
                not isinstance(val, (int, float)) or isinstance(val, bool)
            ):
                errors.append(f"overload.{key} must be a number")
        statuses = overload_section.get("statuses")
        if statuses is not None:
            if not isinstance(statuses, list):
                errors.append("overload.statuses must be a list of integers")
            else:
                for s in statuses:
                    if not isinstance(s, int) or isinstance(s, bool):
                        errors.append("overload.statuses must be a list of integers")
                        break

    threshold_section = config_data.get("threshold", {})
    if isinstance(threshold_section, dict):
        tp = threshold_section.get("provider")
        if tp is not None:
            if not isinstance(tp, str):
                errors.append("threshold.provider must be a string")
            elif tp not in providers:
                _check_ref(
                    tp,
                    f"threshold.provider references unknown provider: '{tp}'",
                )

    for warning in warnings:
        log.warning("config: %s", warning)
    if errors:
        raise _ConfigError("; ".join(errors))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchboard",
        description="Multi-provider routing proxy for LLM APIs.",
    )
    parser.add_argument(
        "--version", action="version", version=f"switchboard {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the routing proxy")
    serve.add_argument(
        "--listen", default=None, help="host:port (default: 127.0.0.1:8801)"
    )
    serve.add_argument(
        "--config", default=None, help="path to TOML config file"
    )
    serve.add_argument(
        "--admin-token", default=None, help="token gating admin routes"
    )
    serve.add_argument(
        "--no-admin-token",
        action="store_true",
        default=False,
        help=(
            "explicitly disable the admin token (the admin surface becomes "
            "readable by anything that can reach the pod). Without this flag "
            "or an explicit --admin-token / SWITCHBOARD_ADMIN_TOKEN, "
            "switchboard refuses to start — an unset token is not 'secure by "
            "default', it is readable by all and writable by none"
        ),
    )
    serve.add_argument(
        "--log-level",
        default=None,
        choices=_LOG_LEVEL_CHOICES,
    )
    serve.add_argument(
        "--queue-timeout",
        type=float,
        default=None,
        help="max seconds to wait for a permit (default: 30)",
    )
    serve.add_argument(
        "--drain-timeout",
        type=float,
        default=None,
        help="seconds to wait for in-flight on shutdown (default: 25)",
    )
    serve.add_argument(
        "--max-request-body-bytes",
        type=int,
        default=None,
        help="cap buffered request bodies (required for pin_conversations)",
    )
    serve.add_argument(
        "--route-table-store",
        default=None,
        help="path to SQLite file for route table persistence (default: in-memory)",
    )

    return parser


def _parse_listen(listen: str) -> tuple[str, int]:
    """Parse host:port string."""
    if ":" not in listen:
        raise _ConfigError(f"invalid --listen format: {listen} (expected host:port)")
    host, port_str = listen.rsplit(":", 1)
    try:
        port = int(port_str)
    except ValueError:
        raise _ConfigError(f"invalid port in --listen: {port_str}") from None
    return host, port


def _resolve_route_table_store(
    args: argparse.Namespace,
    config_data: dict[str, Any],
) -> str | None:
    """Resolve route table store path: flag → env → TOML → None (WI-006.7)."""
    flag_value = getattr(args, "route_table_store", None)
    if isinstance(flag_value, str):
        return flag_value
    env_value = os.environ.get(f"{_ENV_PREFIX}ROUTE_TABLE_STORE")
    if env_value is not None:
        return env_value
    rt_section = config_data.get("route_table", {})
    if isinstance(rt_section, dict):
        store = rt_section.get("store")
        if isinstance(store, str):
            return store
    return None


def _build_serve_app(
    args: argparse.Namespace,
) -> tuple[Any, str, int, str, float]:
    """Build the ProxyApp + bind params from CLI args, env, and config file."""
    from switchboard.config_store import ConfigStoreManager
    from switchboard.control import RoutingConfig
    from switchboard.estimator import ThresholdEstimator
    from switchboard.model_map import ModelMapManager
    from switchboard.overload import OverloadConfig
    from switchboard.providers import build_provider_contexts_from_config
    from switchboard.proxy import ProxyApp
    from switchboard.quarantine import (
        QuarantineTracker,
        config_store_quarantine_store,
    )
    from switchboard.route_table import RouteTableManager
    from switchboard.speed import SpeedSampler
    from switchboard.token_budget import TokenBudgetTracker
    from switchboard.usage_history import UsageHistoryTracker

    config_path = _resolve("config", args)
    config_data: dict[str, Any] = {}
    if config_path:
        config_data = _load_toml_config(str(config_path))

    _validate_config_pre_build(config_data)

    store_path = _resolve_route_table_store(args, config_data)

    # Boot merge (Plan 020 WI-4, D1): the config store shares the route
    # table's SQLite connection, so the route table is built FIRST — seeded
    # with the config-declared default route only; the fallback default
    # (all providers) is not known until the store has overlaid the TOML.
    default_providers: tuple[str, ...] = ()
    route_section = config_data.get("route", {})
    if isinstance(route_section, dict):
        default_cfg = route_section.get("default", {})
        if isinstance(default_cfg, dict):
            providers_list = default_cfg.get("providers")
            if isinstance(providers_list, list):
                default_providers = tuple(providers_list)

    # Boot reset (Plan 021 D7) MUST happen before any manager is constructed:
    # RouteTableManager and ConfigStoreManager load their tables into memory in
    # __init__, so clearing rows afterwards would leave the process running the
    # state it was told to discard. Done on a throwaway connection for that
    # reason.
    reset_raw = os.environ.get(f"{_ENV_PREFIX}CONFIG_RESET")
    if reset_raw and reset_raw.strip():
        try:
            sections = parse_sections(reset_raw)
        except ResetError as exc:
            raise _ConfigError(
                f"{_ENV_PREFIX}CONFIG_RESET: {exc}"
            ) from exc
        if store_path is None:
            log.warning(
                "%sCONFIG_RESET=%s ignored: no route table store is "
                "configured, so the declared config is already authoritative",
                _ENV_PREFIX, reset_raw,
            )
        else:
            reset_db = sqlite3.connect(store_path)
            try:
                reset_sections(reset_db, sections)
            finally:
                reset_db.close()

    try:
        route_table = RouteTableManager(
            default_providers=default_providers,
            sqlite_path=store_path,
        )
    except Exception as exc:
        raise _ConfigError(
            f"failed to open route table store: {exc}"
        ) from exc

    # Matches the route table's dual mode: no store path = memory-only
    # (the admin provider endpoints work, but their writes don't survive
    # a restart).
    if route_table.db is not None:
        config_store = ConfigStoreManager(db=route_table.db)
    else:
        config_store = ConfigStoreManager()

    # D1 precedence: a store row replaces its TOML section wholesale, and a
    # disabled row (tombstone) removes the TOML provider from the effective
    # set. The strict TOML validation above already ran on the raw file.
    # NOTE: `effective` sections carry raw credentials (construction path
    # only) — they are never serialized.
    effective = config_store.effective_providers(config_data)

    # Env tier (Plan 021 D6): applied last so a Deployment outranks whatever
    # the GUI wrote. Per-field, not wholesale — a deployment usually pins one
    # value and has no business discarding the rest of a provider to do it.
    try:
        effective, env_field_sources, unmatched_env = apply_overrides(
            effective, os.environ
        )
    except EnvOverrideError as exc:
        raise _ConfigError(str(exc)) from exc

    tombstoned_providers = frozenset(
        str(row["name"])
        for row in config_store.list_providers()
        if not row["enabled"]
    )

    providers = build_provider_contexts_from_config(
        {"provider": effective},
        history_store_path=store_path,
    )
    if not providers:
        raise _ConfigError(
            "no providers configured — provide a TOML config with [provider.*] sections"
        )

    try:
        _validate_config(
            config_data, providers, tombstoned=tombstoned_providers
        )
    except _ConfigError:
        for ctx in providers.values():
            ctx.reconcile._stopped = True
        raise

    route_table.load_from_config(config_data, overwrite=store_path is None)

    # Resolve the default route only AFTER load_from_config: that call is the
    # last writer of the default before this point (it installs the TOML
    # default, unless an operator-set default in the store outranks it — Plan
    # 020 WI-8, D1). Reading the route table's own view rather than the local
    # TOML-derived tuple is what lets a stored default survive to here; an
    # earlier version filtered the local copy and was clobbered (cycle-2
    # review, finding 1).
    declared_default = tuple(route_table.default_providers)
    default_from_store = route_table.default_from_store

    unknown = [
        name for name in declared_default
        if name not in providers and name not in tombstoned_providers
    ]
    if unknown:
        if not default_from_store:
            raise _ConfigError(
                f"default route references unknown provider: {unknown[0]}"
            )
        # A stored default naming providers this config no longer defines is
        # the one case that must NOT be fatal: the operator set it through the
        # GUI, so crashing here would let a GUI edit brick the next boot with
        # no way back in except hand-editing SQLite. Drop the dead names and
        # carry on, matching WI-0's log-and-skip precedent for model-map rows.
        log.warning(
            "default route: dropping provider(s) %s from the stored default "
            "— not defined by the current config",
            ", ".join(unknown),
        )
        declared_default = tuple(
            name for name in declared_default if name not in unknown
        )

    if not declared_default:
        declared_default = tuple(providers.keys())

    # A tombstoned name in the declared default would just be dead weight in
    # every routing decision (admission skips it) — drop it and say so, so
    # the operator sees the default that actually fires (wave 0+1 review,
    # finding 6).
    live_default = tuple(
        name for name in declared_default if name not in tombstoned_providers
    )
    if live_default != declared_default:
        dropped = [n for n in declared_default if n in tombstoned_providers]
        log.warning(
            "default route: dropping tombstoned provider(s) %s — effective "
            "default is %s",
            ", ".join(dropped),
            list(live_default),
        )
    if not live_default:
        raise _ConfigError(
            "default route has no live providers — every declared "
            "provider is disabled in the config store"
        )

    default_providers = live_default
    # persist=False: everything derived above is a conclusion about THIS boot
    # (the all-providers fallback, the unknown/tombstone filters), not operator
    # intent. Writing it back would freeze a transient condition on disk.
    route_table.set_default_providers(default_providers)

    routing_config = RoutingConfig()
    routing_section = config_data.get("routing", {})
    if isinstance(routing_section, dict):
        rc_kwargs: dict[str, Any] = {}
        threshold = routing_section.get("failover_threshold_seconds")
        if isinstance(threshold, int) and not isinstance(threshold, bool):
            rc_kwargs["failover_threshold_seconds"] = threshold
        margin = routing_section.get("failover_margin")
        if isinstance(margin, int) and not isinstance(margin, bool):
            rc_kwargs["failover_margin"] = margin
        dwell = routing_section.get("dwell_interval")
        if isinstance(dwell, (int, float)) and not isinstance(dwell, bool):
            rc_kwargs["dwell_interval"] = float(dwell)
        failback_delay = routing_section.get("failback_delay")
        if isinstance(failback_delay, (int, float)) and not isinstance(failback_delay, bool):
            rc_kwargs["failback_delay"] = float(failback_delay)
        headroom = routing_section.get("headroom_threshold")
        if isinstance(headroom, (int, float)) and not isinstance(headroom, bool):
            rc_kwargs["headroom_threshold"] = float(headroom)
        headroom_ranking = routing_section.get("headroom_ranking")
        if isinstance(headroom_ranking, bool):
            rc_kwargs["headroom_ranking"] = headroom_ranking
        token_thresh = routing_section.get("token_budget_threshold")
        if isinstance(token_thresh, (int, float)) and not isinstance(token_thresh, bool):
            rc_kwargs["token_budget_threshold"] = float(token_thresh)
        usage_24h_thresh = routing_section.get("usage_24h_threshold")
        if isinstance(usage_24h_thresh, (int, float)) and not isinstance(usage_24h_thresh, bool):
            rc_kwargs["usage_24h_threshold"] = float(usage_24h_thresh)
        opportunistic_enabled = routing_section.get("opportunistic_enabled")
        if isinstance(opportunistic_enabled, bool):
            rc_kwargs["opportunistic_enabled"] = opportunistic_enabled
        opportunistic_min_headroom = routing_section.get(
            "opportunistic_min_headroom"
        )
        if isinstance(opportunistic_min_headroom, (int, float)) and not isinstance(
            opportunistic_min_headroom, bool
        ):
            rc_kwargs["opportunistic_min_headroom"] = float(
                opportunistic_min_headroom
            )
        opportunistic_reset_window = routing_section.get(
            "opportunistic_reset_window"
        )
        if isinstance(opportunistic_reset_window, (int, float)) and not isinstance(
            opportunistic_reset_window, bool
        ):
            rc_kwargs["opportunistic_reset_window"] = float(
                opportunistic_reset_window
            )
        opportunistic_margin = routing_section.get("opportunistic_margin")
        if isinstance(opportunistic_margin, (int, float)) and not isinstance(
            opportunistic_margin, bool
        ):
            rc_kwargs["opportunistic_margin"] = float(opportunistic_margin)
        pin_conversations = routing_section.get("pin_conversations")
        if isinstance(pin_conversations, bool):
            rc_kwargs["pin_conversations"] = pin_conversations
        affinity_max = routing_section.get("affinity_max_entries")
        if (
            isinstance(affinity_max, int)
            and not isinstance(affinity_max, bool)
            and affinity_max >= 1
        ):
            rc_kwargs["affinity_max_entries"] = affinity_max
        strategy = routing_section.get("strategy")
        if isinstance(strategy, str) and strategy in (
            "ordered",
            "headroom",
            "pace",
        ):
            rc_kwargs["strategy"] = RoutingStrategy(strategy)
        pace_burn_rate = routing_section.get("pace_burn_rate_per_day")
        if isinstance(pace_burn_rate, (int, float)) and not isinstance(
            pace_burn_rate, bool
        ):
            rc_kwargs["pace_burn_rate_per_day"] = float(pace_burn_rate)
        pace_flap = routing_section.get("pace_flap_margin")
        if isinstance(pace_flap, (int, float)) and not isinstance(pace_flap, bool):
            rc_kwargs["pace_flap_margin"] = float(pace_flap)
        q_threshold = routing_section.get("quarantine_threshold")
        if isinstance(q_threshold, int) and not isinstance(q_threshold, bool):
            rc_kwargs["quarantine_threshold"] = q_threshold
        if rc_kwargs:
            routing_config = RoutingConfig(**rc_kwargs)

    # A routing knob changed through the admin API outranks TOML on the next
    # boot (Plan 020 D1: the store wins), so that a strategy an operator
    # selected in the GUI is not silently undone by the next pod restart.
    # Only the fields they actually set are overlaid; everything else keeps
    # following the file. A value that no longer validates is dropped with a
    # warning rather than failing the boot — a stale preference must never
    # cost availability.
    stored_overlay = config_store.get_routing_overlay()
    if stored_overlay:
        overlay_kwargs = dict(rc_kwargs) if isinstance(routing_section, dict) else {}
        applied: list[str] = []
        for name, value in stored_overlay.items():
            if name not in MUTABLE_ROUTING_FIELDS:
                continue
            message = validate_routing_field(name, value)
            if message is not None:
                log.warning("ignoring persisted routing.%s: %s", name, message)
                continue
            overlay_kwargs[name] = coerce_routing_value(name, value)
            applied.append(name)
        if applied:
            routing_config = RoutingConfig(**overlay_kwargs)
            log.info(
                "  routing overlay:   %s (from the config store)",
                ", ".join(sorted(applied)),
            )

    # Quarantine (Plan 023). Persisted through the config store so a restart
    # cannot silently un-quarantine a pair no human has looked at.
    quarantine = QuarantineTracker(
        threshold=routing_config.quarantine_threshold,
        store=config_store_quarantine_store(config_store),
    )

    # valid_providers guards SQLite-loaded aliases against providers that
    # were since removed from the config — without it a stale row makes its
    # model route nowhere, silently (WI-12b).
    model_map_mgr = ModelMapManager(
        db=route_table.db,
        valid_providers=frozenset(effective),
    )
    model_map_mgr.load_from_config(
        config_data, overwrite=store_path is None
    )

    overload_config: OverloadConfig | None = None
    overload_statuses: frozenset[int] | None = None
    overload_section = config_data.get("overload", {})
    if isinstance(overload_section, dict):
        oc_kwargs: dict[str, Any] = {}
        threshold = overload_section.get("threshold")
        if isinstance(threshold, int) and not isinstance(threshold, bool):
            oc_kwargs["threshold"] = threshold
        for key in ("cooldown_default", "cooldown_min", "cooldown_max"):
            val = overload_section.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                oc_kwargs[key] = float(val)
        if oc_kwargs:
            try:
                overload_config = OverloadConfig(**oc_kwargs)
            except ValueError as exc:
                raise _ConfigError(f"invalid overload config: {exc}") from exc
        statuses_raw = overload_section.get("statuses")
        if isinstance(statuses_raw, list):
            overload_statuses = frozenset(int(s) for s in statuses_raw)

    # Usage-error reroute (Plan 010 reactive half). Absent section = disabled,
    # which keeps request-body streaming untouched for deployments that have
    # not opted in.
    reroute_statuses: frozenset[int] | None = None
    reroute_max_attempts = 0
    reroute_section = config_data.get("reroute", {})
    if isinstance(reroute_section, dict) and reroute_section:
        enabled = reroute_section.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _ConfigError("reroute.enabled must be a boolean")
        raw_attempts = reroute_section.get("max_attempts", 1)
        if isinstance(raw_attempts, bool) or not isinstance(raw_attempts, int):
            raise _ConfigError("reroute.max_attempts must be an integer")
        if raw_attempts < 0:
            raise _ConfigError("reroute.max_attempts must be >= 0")
        reroute_max_attempts = raw_attempts if enabled else 0
        statuses_raw = reroute_section.get("statuses")
        if statuses_raw is not None:
            if not isinstance(statuses_raw, list) or not statuses_raw:
                raise _ConfigError(
                    "reroute.statuses must be a non-empty list of integers"
                )
            parsed: set[int] = set()
            for raw_status in statuses_raw:
                if isinstance(raw_status, bool) or not isinstance(raw_status, int):
                    raise _ConfigError(
                        "reroute.statuses entries must be integers"
                    )
                # Rerouting a success or a redirect would discard a served
                # response; rerouting a client fault would spray a bad request
                # across the estate.
                if not 400 <= raw_status <= 599:
                    raise _ConfigError(
                        "reroute.statuses entries must be 4xx or 5xx "
                        f"(got {raw_status})"
                    )
                parsed.add(raw_status)
            reroute_statuses = frozenset(parsed)

    admin_token = _resolve("admin_token", args, config_data)
    # WI-008: check_admin_auth fails OPEN when the token is absent — every
    # admin route becomes readable, and mutating endpoints return 405. An
    # unset token is not "secure by default"; it is readable by all and
    # writable by none. Mirror the api_key_env precedent: refuse to start
    # unless the operator explicitly opts out with --no-admin-token.
    no_admin_token = getattr(args, "no_admin_token", False)
    if not admin_token and not no_admin_token:
        raise _ConfigError(
            "admin_token is not set — the admin surface would be readable "
            "by anything on the network. Set --admin-token (or the "
            "SWITCHBOARD_ADMIN_TOKEN env var), or pass --no-admin-token to "
            "explicitly accept the open surface. An unset token is not "
            "'secure by default'; it is readable by all and writable by none."
        )
    if isinstance(admin_token, str):
        admin_token = admin_token if admin_token else None

    queue_timeout = _resolve_float("queue_timeout", args, config_data)
    drain_timeout = _resolve_float("drain_timeout", args, config_data)
    max_body_raw = _resolve("max_request_body_bytes", args, config_data)
    max_request_body_bytes: int | None = None
    if max_body_raw is not None:
        try:
            max_request_body_bytes = int(max_body_raw)
        except (TypeError, ValueError) as exc:
            raise _ConfigError(
                "max_request_body_bytes must be an integer"
            ) from exc
        if max_request_body_bytes <= 0:
            raise _ConfigError(
                "max_request_body_bytes must be > 0"
            )

    listen = _resolve("listen", args, config_data)
    host, port = _parse_listen(str(listen))

    log_level = _resolve("log_level", args, config_data)

    estimator: ThresholdEstimator | None = None
    threshold_section = config_data.get("threshold", {})
    if isinstance(threshold_section, dict):
        est_provider = threshold_section.get("provider")
        if isinstance(est_provider, str) and est_provider in providers:
            est_db = route_table.db
            estimator = ThresholdEstimator(
                provider_name=est_provider,
                db=est_db,
            )
            estimator.load()

    budget_tracker: TokenBudgetTracker | None = None
    token_budget_section = config_data.get("token_budget", {})
    if isinstance(token_budget_section, dict) and token_budget_section:
        from switchboard.budget import TokenBudgetConfig

        budget_configs: dict[str, TokenBudgetConfig] = {}
        for prov_name, prov_cfg in token_budget_section.items():
            if not isinstance(prov_cfg, dict):
                continue
            if prov_name not in providers:
                continue
            cap = prov_cfg.get("cap_tokens")
            if not isinstance(cap, int) or isinstance(cap, bool):
                continue
            window = prov_cfg.get("window_seconds", 3600.0)
            if not isinstance(window, (int, float)) or isinstance(window, bool):
                window = 3600.0
            soft = prov_cfg.get("soft_threshold", 0.85)
            if not isinstance(soft, (int, float)) or isinstance(soft, bool):
                soft = 0.85
            with contextlib.suppress(ValueError):
                budget_configs[prov_name] = TokenBudgetConfig(
                    cap_tokens=cap,
                    window_seconds=float(window),
                    soft_threshold=float(soft),
                )
        if budget_configs:
            budget_tracker = TokenBudgetTracker(
                configs=budget_configs,
                db=route_table.db,
            )
            budget_tracker.load()

    usage_history_tracker: UsageHistoryTracker | None = None
    usage_24h_budget_section = config_data.get("usage_24h_budget", {})
    if not isinstance(usage_24h_budget_section, dict):
        usage_24h_budget_section = {}
    for name, ctx in providers.items():
        if ctx.usage_base_url and ctx.usage_api_key:
            if usage_history_tracker is None:
                usage_history_tracker = UsageHistoryTracker()
            cap_tokens: int | None = None
            prov_budget = usage_24h_budget_section.get(name)
            if isinstance(prov_budget, dict):
                raw_cap = prov_budget.get("cap_tokens")
                if isinstance(raw_cap, int) and not isinstance(raw_cap, bool) and raw_cap > 0:
                    cap_tokens = raw_cap
            usage_history_tracker.register(
                name,
                base_url=ctx.usage_base_url,
                api_key=ctx.usage_api_key,
                auth_header=ctx.usage_auth_header,
                cap_tokens=cap_tokens,
            )

    # Boot TOML sections, threaded to the admin layer for the D1 tombstone
    # and /admin/config/effective paths. They may carry an inline api_key;
    # every serialization surface masks them.
    raw_provider_tables = config_data.get("provider")
    toml_provider_sections: dict[str, dict[str, Any]] = {}
    if isinstance(raw_provider_tables, dict):
        for prov_name, prov_section in raw_provider_tables.items():
            if isinstance(prov_section, dict):
                toml_provider_sections[str(prov_name)] = dict(prov_section)

    # Per-provider speed statistics (Plan 020 Wave 3): always-on display data
    # — TTFB / duration / tokens-per-second feed /status.json, /metrics, and
    # the dashboard. It never influences a routing decision (Wave 4 would).
    speed_sampler = SpeedSampler()

    # Body-buffering features (model map, usage-error reroute, conversation
    # pinning) buffer the full request body in memory — an unbounded buffer
    # is a memory-exhaustion vector (a large chunked request can exhaust the
    # pod's memory limit), so a finite max_request_body_bytes is required
    # when ANY of them is active.
    has_model_map_entries = bool(model_map_mgr.get_model_map().routes)
    _buffering_features: list[str] = []
    if has_model_map_entries:
        _buffering_features.append("model map")
    if reroute_max_attempts > 0:
        _buffering_features.append("reroute")
    if routing_config.pin_conversations:
        _buffering_features.append("pin_conversations")
    if _buffering_features and max_request_body_bytes is None:
        raise _ConfigError(
            f"{'/'.join(_buffering_features)} requires max_request_body_bytes "
            "to be set to a finite limit (unbounded buffering is a memory risk)"
        )

    # Route-key HMAC secret (Plan 008 §3). Env-only by design — a credential
    # must not sit in a committed TOML. SWITCHBOARD_ROUTE_KEY_SECRET is the
    # current key; SWITCHBOARD_ROUTE_KEY_SECRET_PREV is the previous key,
    # kept for a bounded dual-read window so stored entries hashed under the
    # old key still route until they are re-added under the new one. Absent
    # both, route keys are plain SHA-256 (full backward compatibility).
    # Values are stripped so a stray-whitespace env value ("  ") cannot
    # become a degenerate, trivially-guessable HMAC key.
    route_key_secrets = tuple(
        s
        for s in (
            v.strip() if isinstance(v, str) else None
            for v in (
                os.environ.get("SWITCHBOARD_ROUTE_KEY_SECRET"),
                os.environ.get("SWITCHBOARD_ROUTE_KEY_SECRET_PREV"),
            )
        )
        if s
    )
    if route_key_secrets:
        log.info(
            "route-key HMAC enabled (%d active secret%s)",
            len(route_key_secrets),
            "" if len(route_key_secrets) == 1 else "s",
        )
        # Enabling HMAC changes the digest every stored entry must match. Any
        # entry hashed without a secret (TOML [route.*] sections are always
        # plain digests; SQLite rows written before HMAC was enabled are too)
        # will stop matching and silently fall to the default route. The
        # dual-read window covers secret-A → secret-B rotation, NOT the
        # plain→HMAC adoption — warn so the operator re-adds them.
        existing = route_table.list_entries()
        if existing:
            log.warning(
                "route-key HMAC enabled with %d existing keyed route(s); "
                "entries hashed without a secret will no longer match — "
                "re-add them via POST /admin/routes so they are stored "
                "under the HMAC key",
                len(existing),
            )

    app = ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=routing_config,
        admin_token=admin_token if isinstance(admin_token, str) else None,
        queue_timeout=queue_timeout,
        drain_timeout=drain_timeout,
        max_request_body_bytes=max_request_body_bytes,
        overload_config=overload_config,
        overload_statuses=overload_statuses,
        reroute_statuses=reroute_statuses,
        reroute_max_attempts=reroute_max_attempts,
        model_map_mgr=model_map_mgr,
        estimator=estimator,
        budget_tracker=budget_tracker,
        usage_history_tracker=usage_history_tracker,
        speed_sampler=speed_sampler,
        quarantine=quarantine,
        config_store=config_store,
        toml_provider_names=frozenset(toml_provider_sections),
        toml_provider_sections=toml_provider_sections,
        env_field_sources=env_field_sources,
        unmatched_env=unmatched_env,
        route_key_secrets=route_key_secrets,
    )

    store_backed = {
        str(row["name"])
        for row in config_store.list_providers()
        if row["enabled"]
    }
    n_store = sum(1 for name in providers if name in store_backed)

    log.info("switchboard %s starting", __version__)
    log.info("  listen:            %s:%d", host, port)
    log.info(
        "  providers:         %s (%d from TOML, %d from store)",
        ", ".join(providers.keys()),
        len(providers) - n_store,
        n_store,
    )
    log.info("  default_route:     %s", " -> ".join(default_providers))
    log.info("  queue_timeout:     %.1fs", queue_timeout)
    log.info("  drain_timeout:     %.1fs", drain_timeout)
    if store_path:
        log.info("  route_table_store: %s", store_path)
    if config_path:
        log.info("  config:            %s", config_path)
    if admin_token:
        log.info("  admin_token:       set")
    elif no_admin_token:
        log.warning(
            "  admin_token:       DISABLED (--no-admin-token) — admin "
            "surface is readable by anything on the network"
        )
    else:
        log.warning("  admin_token:       disabled")
    if routing_config.failback_delay > 0:
        log.info(
            "  failback_delay:    %.1fs",
            routing_config.failback_delay,
        )
    if routing_config.headroom_ranking:
        log.info("  headroom_ranking:  enabled")
    if routing_config.opportunistic_enabled:
        log.info(
            "  opportunistic:     min_headroom=%.2f reset_window=%.1fs margin=%.2f",
            routing_config.opportunistic_min_headroom,
            routing_config.opportunistic_reset_window,
            routing_config.opportunistic_margin,
        )
    if quarantine.entries():
        log.warning(
            "  quarantine:        %d pair(s) still quarantined from a "
            "previous run — they take no traffic until released",
            len(quarantine.entries()),
        )
    if routing_config.strategy != RoutingStrategy.ORDERED:
        log.info(
            "  strategy:          %s",
            routing_config.strategy.value,
        )
    if model_map_mgr is not None and model_map_mgr.list_models():
        log.info(
            "  model_map:         %d model(s)",
            len(model_map_mgr.list_models()),
        )
    if overload_config is not None:
        log.info("  overload:          threshold=%d", overload_config.threshold)
    if estimator is not None:
        log.info("  threshold:         monitoring %s", estimator.provider_name)
    if budget_tracker is not None:
        log.info(
            "  token_budget:      %s",
            ", ".join(
                f"{p}={c.cap_tokens}" for p, c in budget_tracker._configs.items()
            ),
        )
    if usage_history_tracker is not None:
        log.info(
            "  usage_history:     %s",
            ", ".join(
                name for name in providers
                if usage_history_tracker.has_provider(name)
            ),
        )
        capped = [
            f"{p}={c}" for p, c in usage_history_tracker._caps.items()
        ]
        if capped:
            log.info(
                "  usage_24h_budget:  %s (threshold=%.2f)",
                ", ".join(capped),
                routing_config.usage_24h_threshold,
            )

    return app, host, port, str(log_level).lower(), drain_timeout


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        app, host, port, log_level, drain_timeout = _build_serve_app(args)
    except _ConfigError as exc:
        print(f"switchboard: error: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
        timeout_graceful_shutdown=max(30, int(drain_timeout) + 5),
    )
    server = uvicorn.Server(config)
    server.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _cmd_serve(args)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
