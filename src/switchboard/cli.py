"""``switchboard`` command-line entry point.

``switchboard serve`` runs the multi-provider routing proxy.

Config precedence: flags → environment variables → config file → built-in defaults.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

from switchboard import __version__

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
}


class _ConfigError(Exception):
    """Raised when the configuration is invalid."""


def _resolve(key: str, args: argparse.Namespace) -> Any:
    """Resolve a config value: flag → env var → default."""
    flag_value = getattr(args, key, None)
    if flag_value is not None:
        return flag_value
    env_key = f"{_ENV_PREFIX}{key.upper()}"
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return env_value
    return _DEFAULTS.get(key)


def _resolve_float(key: str, args: argparse.Namespace) -> float:
    """Resolve a float config value."""
    value = _resolve(key, args)
    try:
        return float(value)
    except (ValueError, TypeError):
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

    return errors


def _validate_config_pre_build(
    config_data: dict[str, Any],
) -> None:
    """Validate config before building provider contexts (WI-006.8).

    Checks that don't need live provider contexts: empty upstreams,
    invalid targets, duplicate names, unknown provider types, and
    hashed-key format for file-defined routes.
    """
    import re

    errors: list[str] = []

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
) -> None:
    """Validate all configuration references (WI-006.8).

    Raises ``_ConfigError`` on the first validation failure.
    """
    errors: list[str] = []

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
                    elif p not in providers:
                        errors.append(
                            f"default route references unknown provider: '{p}'"
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
                    elif p not in providers:
                        errors.append(
                            f"route '{section_name}' references unknown "
                            f"provider: '{p}'"
                        )

    routing_section = config_data.get("routing", {})
    if isinstance(routing_section, dict):
        threshold = routing_section.get("failover_threshold_seconds")
        if threshold is not None:
            if not isinstance(threshold, int) or isinstance(threshold, bool):
                errors.append("routing.failover_threshold_seconds must be an integer")
            elif threshold < 0:
                errors.append("routing.failover_threshold_seconds must be >= 0")
        margin = routing_section.get("failover_margin")
        if margin is not None:
            if not isinstance(margin, int) or isinstance(margin, bool):
                errors.append("routing.failover_margin must be an integer")
            elif margin < 0:
                errors.append("routing.failover_margin must be >= 0")
        dwell = routing_section.get("dwell_interval")
        if dwell is not None:
            if not isinstance(dwell, (int, float)) or isinstance(dwell, bool):
                errors.append("routing.dwell_interval must be a number")
            elif dwell < 0:
                errors.append("routing.dwell_interval must be >= 0")
        headroom = routing_section.get("headroom_threshold")
        if headroom is not None:
            if not isinstance(headroom, (int, float)) or isinstance(headroom, bool):
                errors.append("routing.headroom_threshold must be a number")
            elif headroom < 0.0 or headroom > 1.0:
                errors.append(
                    "routing.headroom_threshold must be between 0.0 and 1.0"
                )
        token_thresh = routing_section.get("token_budget_threshold")
        if token_thresh is not None:
            if not isinstance(token_thresh, (int, float)) or isinstance(token_thresh, bool):
                errors.append(
                    "routing.token_budget_threshold must be a number"
                )
            elif token_thresh < 0.0 or token_thresh > 1.0:
                errors.append(
                    "routing.token_budget_threshold must be "
                    "between 0.0 and 1.0"
                )
        usage_24h_thresh = routing_section.get("usage_24h_threshold")
        if usage_24h_thresh is not None:
            if not isinstance(usage_24h_thresh, (int, float)) or isinstance(usage_24h_thresh, bool):
                errors.append(
                    "routing.usage_24h_threshold must be a number"
                )
            elif usage_24h_thresh < 0.0 or usage_24h_thresh > 1.0:
                errors.append(
                    "routing.usage_24h_threshold must be "
                    "between 0.0 and 1.0"
                )
        opportunistic_enabled = routing_section.get("opportunistic_enabled")
        if opportunistic_enabled is not None and not isinstance(
            opportunistic_enabled, bool
        ):
            errors.append("routing.opportunistic_enabled must be a boolean")
        opportunistic_min_headroom = routing_section.get(
            "opportunistic_min_headroom"
        )
        if opportunistic_min_headroom is not None:
            if (
                not isinstance(opportunistic_min_headroom, (int, float))
                or isinstance(opportunistic_min_headroom, bool)
            ):
                errors.append(
                    "routing.opportunistic_min_headroom must be a number"
                )
            elif (
                opportunistic_min_headroom <= 0.0
                or opportunistic_min_headroom > 1.0
            ):
                errors.append(
                    "routing.opportunistic_min_headroom must be in (0.0, 1.0]"
                )
        opportunistic_reset_window = routing_section.get(
            "opportunistic_reset_window"
        )
        if opportunistic_reset_window is not None:
            if (
                not isinstance(opportunistic_reset_window, (int, float))
                or isinstance(opportunistic_reset_window, bool)
            ):
                errors.append(
                    "routing.opportunistic_reset_window must be a number"
                )
            elif opportunistic_reset_window <= 0.0:
                errors.append(
                    "routing.opportunistic_reset_window must be > 0"
                )
        opportunistic_margin = routing_section.get("opportunistic_margin")
        if opportunistic_margin is not None:
            if (
                not isinstance(opportunistic_margin, (int, float))
                or isinstance(opportunistic_margin, bool)
            ):
                errors.append(
                    "routing.opportunistic_margin must be a number"
                )
            elif opportunistic_margin < 0.0 or opportunistic_margin >= 1.0:
                errors.append(
                    "routing.opportunistic_margin must be in [0.0, 1.0)"
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
                errors.append(
                    f"usage_24h_budget.'{prov_name}': "
                    f"references unknown provider"
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
                errors.append(
                    f"token_budget.'{prov_name}': references unknown provider"
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
                    errors.append(
                        f"model '{model_name}': references unknown provider '{provider_name}'"
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
                errors.append(
                    f"threshold.provider references unknown provider: '{tp}'"
                )

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
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
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
    from switchboard.control import ModelMap, RoutingConfig
    from switchboard.estimator import ThresholdEstimator
    from switchboard.overload import OverloadConfig
    from switchboard.providers import build_provider_contexts_from_config
    from switchboard.proxy import ProxyApp
    from switchboard.route_table import RouteTableManager
    from switchboard.token_budget import TokenBudgetTracker
    from switchboard.usage_history import UsageHistoryTracker

    config_path = _resolve("config", args)
    config_data: dict[str, Any] = {}
    if config_path:
        config_data = _load_toml_config(str(config_path))

    _validate_config_pre_build(config_data)

    store_path = _resolve_route_table_store(args, config_data)

    providers = build_provider_contexts_from_config(
        config_data,
        history_store_path=store_path,
    )
    if not providers:
        raise _ConfigError(
            "no providers configured — provide a TOML config with [provider.*] sections"
        )

    try:
        _validate_config(config_data, providers)
    except _ConfigError:
        for ctx in providers.values():
            ctx.reconcile._stopped = True
        raise

    default_providers: tuple[str, ...] = ()
    route_section = config_data.get("route", {})
    if isinstance(route_section, dict):
        default_cfg = route_section.get("default", {})
        if isinstance(default_cfg, dict):
            providers_list = default_cfg.get("providers")
            if isinstance(providers_list, list):
                default_providers = tuple(providers_list)

    if not default_providers:
        default_providers = tuple(providers.keys())

    for name in default_providers:
        if name not in providers:
            raise _ConfigError(
                f"default route references unknown provider: {name}"
            )

    try:
        route_table = RouteTableManager(
            default_providers=default_providers,
            sqlite_path=store_path,
        )
    except Exception as exc:
        raise _ConfigError(
            f"failed to open route table store: {exc}"
        ) from exc

    route_table.load_from_config(config_data, overwrite=store_path is None)

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
        if rc_kwargs:
            routing_config = RoutingConfig(**rc_kwargs)

    model_map: ModelMap | None = None
    model_section = config_data.get("model", {})
    if isinstance(model_section, dict) and model_section:
        routes: dict[str, dict[str, str]] = {}
        for model_name, provider_map in model_section.items():
            if not isinstance(provider_map, dict):
                continue
            entry: dict[str, str] = {}
            for pn, alias in provider_map.items():
                if isinstance(alias, str) and pn in providers:
                    entry[pn] = alias
            if entry:
                routes[model_name] = entry
        if routes:
            model_map = ModelMap(routes=routes)

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

    admin_token = _resolve("admin_token", args)

    queue_timeout = _resolve_float("queue_timeout", args)
    drain_timeout = _resolve_float("drain_timeout", args)

    listen = _resolve("listen", args)
    host, port = _parse_listen(str(listen))

    log_level = _resolve("log_level", args)

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

    app = ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=routing_config,
        admin_token=admin_token if isinstance(admin_token, str) else None,
        queue_timeout=queue_timeout,
        drain_timeout=drain_timeout,
        overload_config=overload_config,
        overload_statuses=overload_statuses,
        reroute_statuses=reroute_statuses,
        reroute_max_attempts=reroute_max_attempts,
        model_map=model_map,
        estimator=estimator,
        budget_tracker=budget_tracker,
        usage_history_tracker=usage_history_tracker,
    )

    log.info("switchboard %s starting", __version__)
    log.info("  listen:            %s:%d", host, port)
    log.info("  providers:         %s", ", ".join(providers.keys()))
    log.info("  default_route:     %s", " -> ".join(default_providers))
    log.info("  queue_timeout:     %.1fs", queue_timeout)
    log.info("  drain_timeout:     %.1fs", drain_timeout)
    if store_path:
        log.info("  route_table_store: %s", store_path)
    if config_path:
        log.info("  config:            %s", config_path)
    if admin_token:
        log.info("  admin_token:       set")
    else:
        log.info("  admin_token:       disabled")
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
    if model_map is not None:
        log.info("  model_map:         %d model(s)", len(model_map.routes))
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
