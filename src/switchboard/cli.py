"""``switchboard`` command-line entry point.

``switchboard serve`` runs the multi-provider routing proxy.

Config precedence: flags → environment variables → config file → built-in defaults.
"""

from __future__ import annotations

import argparse
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

    config_path = _resolve("config", args)
    config_data: dict[str, Any] = {}
    if config_path:
        config_data = _load_toml_config(str(config_path))

    _validate_config_pre_build(config_data)

    providers = build_provider_contexts_from_config(config_data)
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

    store_path = _resolve_route_table_store(args, config_data)

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
        headroom = routing_section.get("headroom_threshold")
        if isinstance(headroom, (int, float)) and not isinstance(headroom, bool):
            rc_kwargs["headroom_threshold"] = float(headroom)
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

    app = ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=routing_config,
        admin_token=admin_token if isinstance(admin_token, str) else None,
        queue_timeout=queue_timeout,
        drain_timeout=drain_timeout,
        overload_config=overload_config,
        overload_statuses=overload_statuses,
        model_map=model_map,
        estimator=estimator,
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
    if model_map is not None:
        log.info("  model_map:         %d model(s)", len(model_map.routes))
    if overload_config is not None:
        log.info("  overload:          threshold=%d", overload_config.threshold)
    if estimator is not None:
        log.info("  threshold:         monitoring %s", estimator.provider_name)

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
