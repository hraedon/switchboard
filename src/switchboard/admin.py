"""Admin route handlers — health, readiness, status, metrics, route table CRUD, dashboard.

Stateless functions that receive the proxy's state as arguments. Shared
utilities (``send_json``, ``send_text``, ``check_admin_auth``) are borrowed
from :mod:`sluice.admin` to avoid duplication. Switchboard-specific handlers
build multi-provider status payloads and manage route table CRUD.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from sluice.admin import (
    build_set_cookie,
    check_admin_auth,
    check_csrf,
    cors_extra_headers,
    read_body,
    send_json,
    send_text,
)
from sluice.session import (
    SESSION_COOKIE,
    LoginThrottle,
    mint_session,
)

from switchboard import __version__

if TYPE_CHECKING:
    from switchboard.providers import ProviderContext
    from switchboard.proxy import RoutingMetrics
    from switchboard.route_table import RouteTableManager

log = logging.getLogger("switchboard.admin")

Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json; charset=utf-8",
}

_DASHBOARD_HTML = (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
_LOGIN_HTML = (_STATIC_DIR / "login.html").read_text(encoding="utf-8")

_SESSION_COOKIE = SESSION_COOKIE
_SESSION_TTL = 2_592_000


async def handle_healthz(send: Send) -> None:
    """GET /healthz — always 200."""
    await send_json(send, 200, {"status": "ok"})


async def handle_readyz(
    send: Send,
    providers: dict[str, ProviderContext],
) -> None:
    """GET /readyz — 200 if all providers ready, 503 otherwise."""
    ready = (
        all(ctx.reconcile.ready for ctx in providers.values())
        if providers
        else False
    )
    if ready:
        await send_json(send, 200, {"status": "ready"})
    else:
        await send_json(send, 503, {"status": "not ready"})


def _provider_status(ctx: ProviderContext) -> dict[str, Any]:
    """Build a status dict for one provider."""
    r = ctx.reconcile
    breaker = r.breaker_state
    band = r.band
    return {
        "gate_closed_reason": r.gate_closed_reason(),
        "effective_permits": r.effective_permits_count,
        "in_flight": ctx.gate.held,
        "queue_depth": ctx.gate.queue_depth,
        "available_permits": ctx.gate.available,
        "capacity": ctx.gate.capacity,
        "ready": r.ready,
        "total_429s": r.total_429s,
        "total_requests_forwarded": r.total_requests_forwarded,
        "breaker": breaker.name if hasattr(breaker, "name") else str(breaker),
        "band": band.name if hasattr(band, "name") else str(band),
        "upstream_url": ctx.upstream_url,
    }


def _build_status_payload(
    providers: dict[str, ProviderContext],
    route_table: RouteTableManager,
    routing_metrics: RoutingMetrics,
    build_sha: str | None = None,
) -> dict[str, Any]:
    """Build the full status payload for /status.json."""
    provider_states: dict[str, Any] = {}
    for name, ctx in providers.items():
        provider_states[name] = _provider_status(ctx)

    routes: dict[str, list[str]] = {}
    for entry in route_table.list_entries():
        routes[entry.key] = list(entry.providers)
    routes["default"] = list(route_table.default_providers)

    return {
        "providers": provider_states,
        "route_table": routes,
        "routing_metrics": {
            "forwarded_per_provider": dict(routing_metrics.forwarded_per_provider),
            "failovers": routing_metrics.failovers,
            "routing_decisions": routing_metrics.routing_decisions,
            "recent_decisions": list(routing_metrics.recent_decisions),
            "evicted_decisions": routing_metrics.evicted_decisions,
        },
        "version": __version__,
        "build": build_sha,
    }


async def send_status_json(
    send: Send,
    providers: dict[str, ProviderContext],
    route_table: RouteTableManager,
    routing_metrics: RoutingMetrics,
    build_sha: str | None = None,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /status.json — per-provider state + route table + routing metrics."""
    payload = _build_status_payload(
        providers, route_table, routing_metrics, build_sha
    )
    await send_json(
        send, 200, payload,
        extra_headers=[
            *cors_extra_headers(cors_allow_origin, None),
            (b"cache-control", b"no-store"),
        ],
    )


async def send_prometheus(
    send: Send,
    providers: dict[str, ProviderContext],
    routing_metrics: RoutingMetrics,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /metrics — Prometheus text exposition format."""
    lines: list[str] = []

    lines.append(
        "# HELP switchboard_routing_decisions Total routing decisions made"
    )
    lines.append("# TYPE switchboard_routing_decisions counter")
    lines.append(f"switchboard_routing_decisions {routing_metrics.routing_decisions}")

    lines.append(
        "# HELP switchboard_failovers Total failovers (non-primary selected)"
    )
    lines.append("# TYPE switchboard_failovers counter")
    lines.append(f"switchboard_failovers {routing_metrics.failovers}")

    lines.append(
        "# HELP switchboard_evicted_decisions "
        "Total routing decisions evicted from the bounded recent_decisions ring"
    )
    lines.append("# TYPE switchboard_evicted_decisions counter")
    lines.append(
        f"switchboard_evicted_decisions {routing_metrics.evicted_decisions}"
    )

    lines.append(
        "# HELP switchboard_forwarded_per_provider "
        "Total requests forwarded per provider"
    )
    lines.append("# TYPE switchboard_forwarded_per_provider counter")
    for name, count in sorted(routing_metrics.forwarded_per_provider.items()):
        lines.append(
            f'switchboard_forwarded_per_provider{{provider="{name}"}} {count}'
        )

    lines.append("# HELP switchboard_in_flight Currently held permits")
    lines.append("# TYPE switchboard_in_flight gauge")
    lines.append(
        "# HELP switchboard_effective_permits Current effective permit count"
    )
    lines.append("# TYPE switchboard_effective_permits gauge")
    lines.append("# HELP switchboard_queue_depth Current queue depth")
    lines.append("# TYPE switchboard_queue_depth gauge")
    lines.append(
        "# HELP switchboard_total_429s Total 429s received from upstream"
    )
    lines.append("# TYPE switchboard_total_429s counter")
    lines.append(
        "# HELP switchboard_total_forwarded Total requests forwarded upstream"
    )
    lines.append("# TYPE switchboard_total_forwarded counter")

    for name, ctx in sorted(providers.items()):
        r = ctx.reconcile
        labels = f'provider="{name}"'
        lines.append(f"switchboard_in_flight{{{labels}}} {ctx.gate.held}")
        lines.append(
            f"switchboard_effective_permits{{{labels}}} {r.effective_permits_count}"
        )
        lines.append(f"switchboard_queue_depth{{{labels}}} {ctx.gate.queue_depth}")
        lines.append(f"switchboard_total_429s{{{labels}}} {r.total_429s}")
        lines.append(
            f"switchboard_total_forwarded{{{labels}}} {r.total_requests_forwarded}"
        )

    text = "\n".join(lines) + "\n"
    await send_text(
        send, 200, text,
        content_type="text/plain; version=0.0.4; charset=utf-8",
        extra_headers=[
            *cors_extra_headers(cors_allow_origin, None),
            (b"cache-control", b"no-store"),
        ],
    )


async def handle_route_list(
    send: Send,
    route_table: RouteTableManager,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /admin/routes — list all route entries."""
    entries = []
    for entry in route_table.list_entries():
        entries.append({
            "key": entry.key,
            "providers": list(entry.providers),
        })
    body = {
        "entries": entries,
        "default": list(route_table.default_providers),
    }
    await send_json(
        send, 200, body,
        extra_headers=[
            *cors_extra_headers(cors_allow_origin, None),
            (b"cache-control", b"no-store"),
        ],
    )


async def handle_route_add(
    send: Send,
    receive: Receive,
    route_table: RouteTableManager,
    admin_token: str | None,
    scope: Scope,
    cors_allow_origin: str | None = None,
    providers: dict[str, ProviderContext] | None = None,
) -> None:
    """POST /admin/routes — add or update a route entry.

    Body: ``{"key": "<raw API key>", "providers": ["umans", "ollama"]}``
    The server hashes the key before storing. The raw key is never persisted.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ct = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers", [])
            if k == b"content-type"
        ),
        "",
    )
    if not ct.lower().startswith("application/json"):
        await send_json(
            send, 415,
            {"error": "Content-Type must be application/json"},
            extra_headers=cors,
        )
        return

    try:
        body = await read_body(receive)
    except ValueError:
        await send_json(
            send, 413, {"error": "request body too large"},
            extra_headers=cors,
        )
        return
    except ConnectionError:
        return

    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        await send_json(
            send, 400, {"error": "invalid JSON body"},
            extra_headers=cors,
        )
        return

    if not isinstance(data, dict):
        await send_json(
            send, 400, {"error": "body must be a JSON object"},
            extra_headers=cors,
        )
        return

    raw_key = data.get("key")
    providers_raw = data.get("providers")

    if not isinstance(raw_key, str) or not raw_key:
        await send_json(
            send, 400, {"error": "missing required field 'key'"},
            extra_headers=cors,
        )
        return
    if not isinstance(providers_raw, list) or not providers_raw:
        await send_json(
            send, 400, {"error": "missing required field 'providers'"},
            extra_headers=cors,
        )
        return
    if not all(isinstance(p, str) for p in providers_raw):
        await send_json(
            send, 400, {"error": "providers must be a list of strings"},
            extra_headers=cors,
        )
        return
    if providers is not None:
        unknown = [p for p in providers_raw if p not in providers]
        if unknown:
            await send_json(
                send, 400,
                {"error": f"unknown provider(s): {', '.join(unknown)}"},
                extra_headers=cors,
            )
            return

    from switchboard.control import hash_route_key

    hashed = hash_route_key(raw_key)
    route_table.add_entry(hashed, providers_raw)

    log.info("route added: %s -> %s", hashed[:16] + "...", providers_raw)

    await send_json(
        send, 200,
        {"key": hashed, "providers": list(providers_raw)},
        extra_headers=cors,
    )


async def handle_route_delete(
    send: Send,
    route_table: RouteTableManager,
    admin_token: str | None,
    scope: Scope,
    hashed_key: str,
    cors_allow_origin: str | None = None,
) -> None:
    """DELETE /admin/routes/<key> — remove a route entry."""
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    removed = route_table.remove_entry(hashed_key)
    if not removed:
        await send_json(send, 404, {"error": "route not found"}, extra_headers=cors)
        return

    log.info("route removed: %s", hashed_key[:16] + "...")
    await send_json(send, 200, {"removed": True}, extra_headers=cors)


async def handle_config_get(
    send: Send,
    routing_config: Any,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /admin/config — current routing config."""
    body = {
        "failover_threshold_seconds": routing_config.failover_threshold_seconds,
        "failover_margin": routing_config.failover_margin,
        "dwell_interval": routing_config.dwell_interval,
    }
    await send_json(
        send, 200, body,
        extra_headers=cors_extra_headers(cors_allow_origin, None),
    )


async def serve_static(path: str, send: Send) -> None:
    """Serve a file from the switchboard static directory."""
    rel = path[len("/static/"):]
    try:
        file_path = (_STATIC_DIR / rel).resolve()
        file_path.relative_to(_STATIC_DIR)
    except (ValueError, OSError):
        await send_text(send, 404, "Not found")
        return
    if not file_path.is_file():
        await send_text(send, 404, "Not found")
        return
    ext = file_path.suffix.lower()
    content_type = _STATIC_CONTENT_TYPES.get(ext, "application/octet-stream")
    data = file_path.read_bytes()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(data)).encode()),
        (b"cache-control", b"public, max-age=3600"),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": data, "more_body": False})


async def send_dashboard(
    send: Send,
    cors_allow_origin: str | None = None,
) -> None:
    """Serve the dashboard HTML page."""
    await send_text(
        send, 200, _DASHBOARD_HTML,
        content_type="text/html; charset=utf-8",
        extra_headers=cors_extra_headers(cors_allow_origin, None),
    )


async def send_login_page(
    send: Send,
    cors_allow_origin: str | None = None,
) -> None:
    """Serve the login HTML page."""
    await send_text(
        send, 200, _LOGIN_HTML,
        content_type="text/html; charset=utf-8",
        extra_headers=cors_extra_headers(cors_allow_origin, None),
    )


async def handle_login_get(
    send: Send,
    admin_token: str | None,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /login — serve the login form, or 404 if no token configured."""
    if not admin_token:
        await send_text(send, 404, "Not found")
        return
    await send_login_page(send, cors_allow_origin)


async def handle_login_post(
    send: Send,
    receive: Receive,
    admin_token: str | None,
    scope: Scope,
    throttle: LoginThrottle,
    trusted_proxies: frozenset[
        ipaddress.IPv4Network | ipaddress.IPv6Network
    ] = frozenset(),
) -> None:
    """POST /login — verify token, set session cookie, redirect to /."""
    if not admin_token:
        await send_text(send, 404, "Not found")
        return

    now = time.time()

    if throttle.is_locked(now):
        retry = throttle.retry_after(now)
        log.warning("login throttled — retry_after=%d", retry)
        await send_json(
            send, 429,
            {"error": "too many attempts", "retry_after": retry},
            retry_after=retry,
        )
        return

    try:
        body = await read_body(receive)
    except ValueError:
        await send_text(send, 413, "request body too large")
        return
    except ConnectionError:
        return

    params = parse_qs(body.decode("utf-8", errors="replace"))
    token = params.get("token", [""])[0]

    if not token or not hmac.compare_digest(
        token.encode("utf-8"), admin_token.encode("utf-8")
    ):
        throttle.record_failure(now)
        log.warning("login failed — remote=%s", _extract_remote(scope))
        await asyncio.sleep(0.2)
        await send_text(
            send, 303, "",
            extra_headers=[(b"location", b"/login?error=1")],
        )
        return

    throttle.record_success(now)
    cookie_value = mint_session(admin_token, now, _SESSION_TTL)
    set_cookie = build_set_cookie(
        cookie_value, _SESSION_TTL, scope, trusted_proxies
    )
    await send_text(
        send, 303, "",
        extra_headers=[(b"location", b"/"), (b"set-cookie", set_cookie)],
    )


async def handle_logout(
    send: Send,
    admin_token: str | None,
    scope: Scope,
    trusted_proxies: frozenset[
        ipaddress.IPv4Network | ipaddress.IPv6Network
    ] = frozenset(),
) -> None:
    """POST /logout — clear session cookie and redirect to /login."""
    if not admin_token:
        await send_text(send, 303, "", extra_headers=[(b"location", b"/")])
        return
    if not check_csrf(scope, admin_token):
        await send_text(send, 403, "cross-site request blocked")
        return
    set_cookie = build_set_cookie("", 0, scope, trusted_proxies)
    await send_text(
        send, 303, "",
        extra_headers=[(b"location", b"/login"), (b"set-cookie", set_cookie)],
    )


def _extract_remote(scope: Scope) -> str:
    """Extract the client IP from the ASGI scope."""
    client = scope.get("client")
    return client[0] if client else "unknown"
