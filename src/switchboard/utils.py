"""Admin utility functions — ASGI helpers, auth, CORS, cookie building.

Absorbed from sluice.admin (Plan 017). Contains only the utility
functions switchboard imports; route handlers are in switchboard.admin.
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from switchboard.session import SESSION_COOKIE, verify_session

log = logging.getLogger("switchboard.utils")

Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]

_LOOPBACK_NETWORKS = frozenset(
    {
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("::ffff:127.0.0.0/104"),
    }
)

_SESSION_TTL = 2_592_000  # 30 days
_MAX_CONFIG_BODY = 8192


def parse_trusted_proxies(
    raw: str | None,
) -> frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse a comma-separated CIDR/IP list into a frozenset of networks."""
    if not raw:
        return frozenset()
    nets: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        net = ipaddress.ip_network(token, strict=False)
        nets.add(net)
    return frozenset(nets)


def _peer_ip(scope: Scope) -> str | None:
    client = scope.get("client")
    if not client:
        return None
    ip: str = client[0]
    return ip


def peer_is_trusted(
    scope: Scope,
    trusted: frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """True if the immediate TCP peer is in the trusted-proxy allowlist."""
    ip_str = _peer_ip(scope)
    if ip_str is None:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if any(ip in net for net in _LOOPBACK_NETWORKS):
        return True
    if trusted:
        return any(ip in net for net in trusted)
    return False


def forwarded_proto_https(
    scope: Scope,
    trusted: frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """True iff X-Forwarded-Proto: https was set by a trusted peer."""
    if not peer_is_trusted(scope, trusted):
        return False
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-proto":
            first: str = v.decode("latin-1").split(",")[0].strip().lower()
            return first == "https"
    return False


def forwarded_client_ip(
    scope: Scope,
    trusted: frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    """Return the original client IP from X-Forwarded-For when trusted."""
    if not peer_is_trusted(scope, trusted):
        return None
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-for":
            first = v.decode("latin-1").split(",")[0].strip()
            return first or None
    return None


def cors_extra_headers(
    cors_allow_origin: str | None,
    existing: list[tuple[bytes, bytes]] | None,
) -> list[tuple[bytes, bytes]]:
    """Append CORS headers when an allow-origin is configured."""
    if not cors_allow_origin:
        return existing or []
    headers = list(existing or [])
    headers.append(
        (b"access-control-allow-origin", cors_allow_origin.encode("latin-1"))
    )
    headers.append(
        (b"access-control-allow-methods", b"GET, POST, PUT, DELETE, OPTIONS")
    )
    headers.append(
        (b"access-control-allow-headers", b"Authorization, Content-Type, Cookie")
    )
    headers.append((b"access-control-max-age", b"600"))
    if cors_allow_origin != "*":
        headers.append((b"vary", b"Origin"))
        headers.append((b"access-control-allow-credentials", b"true"))
    return headers


async def send_json(
    send: Send,
    status: int,
    body: dict[str, Any],
    *,
    retry_after: int | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    payload = json.dumps(body).encode()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    if retry_after is not None:
        headers.append((b"retry-after", str(retry_after).encode()))
    if extra_headers:
        headers.extend(extra_headers)
    await send(
        {"type": "http.response.start", "status": status, "headers": headers}
    )
    await send(
        {"type": "http.response.body", "body": payload, "more_body": False}
    )


async def send_text(
    send: Send,
    status: int,
    body: str,
    *,
    content_type: str = "text/plain",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    payload = body.encode()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(payload)).encode()),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send(
        {"type": "http.response.start", "status": status, "headers": headers}
    )
    await send(
        {"type": "http.response.body", "body": payload, "more_body": False}
    )


def is_admin_auth_value(value: bytes, admin_token: str | None) -> bool:
    """Check if an Authorization header value matches admin credentials."""
    if not admin_token:
        return False
    bearer_expected = f"Bearer {admin_token}".encode()
    if hmac.compare_digest(value, bearer_expected):
        return True
    if value.lower().startswith(b"basic "):
        try:
            decoded = base64.b64decode(value[6:]).decode("utf-8")
            _, _, password = decoded.partition(":")
            if hmac.compare_digest(
                password.encode("utf-8"),
                admin_token.encode("utf-8"),
            ):
                return True
        except Exception:
            pass
    return False


def _get_session_cookie(scope: Scope) -> str | None:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                part = part.strip()
                if part.startswith(f"{SESSION_COOKIE}="):
                    val: str = part[len(SESSION_COOKIE) + 1:]
                    return val
    return None


def _should_set_secure(
    scope: Scope,
    trusted_proxies: frozenset[
        ipaddress.IPv4Network | ipaddress.IPv6Network
    ] = frozenset(),
) -> bool:
    if scope.get("scheme") == "https":
        return True
    if forwarded_proto_https(scope, trusted_proxies):
        return True
    server = scope.get("server")
    return bool(
        server
        and server[0] in (
            "127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1",
        )
    )


def build_set_cookie(
    value: str,
    max_age: int,
    scope: Scope,
    trusted_proxies: frozenset[
        ipaddress.IPv4Network | ipaddress.IPv6Network
    ] = frozenset(),
) -> bytes:
    """Build a Set-Cookie header value for the session cookie."""
    parts = [
        f"{SESSION_COOKIE}={value}",
        "HttpOnly",
        "SameSite=Strict",
        "Path=/",
        f"Max-Age={max_age}",
    ]
    if _should_set_secure(scope, trusted_proxies):
        parts.append("Secure")
    return "; ".join(parts).encode("latin-1")


def check_admin_auth(
    scope: Scope, admin_token: str | None, *, now: float | None = None
) -> bool:
    """Return True if the request is authorized for admin routes."""
    if not admin_token:
        return True
    if now is None:
        now = time.time()
    for k, v in scope.get("headers", []):
        if k == b"authorization" and is_admin_auth_value(v, admin_token):
            return True
    cookie_value = _get_session_cookie(scope)
    if cookie_value is not None:
        return verify_session(cookie_value, admin_token, now)
    return False


def check_csrf(scope: Scope, admin_token: str | None) -> bool:
    """CSRF check for cookie-authenticated mutation requests."""
    for k, v in scope.get("headers", []):
        if k == b"authorization" and is_admin_auth_value(v, admin_token):
            return True
    for k, v in scope.get("headers", []):
        if k == b"sec-fetch-site":
            site: str = v.decode("latin-1").strip().lower()
            return site == "same-origin"
    return True


async def read_body(receive: Receive) -> bytes:
    """Read the request body from ASGI receive(), capped at _MAX_CONFIG_BODY."""
    body: bytes = b""
    while True:
        event = await receive()
        if event["type"] == "http.request":
            body += event.get("body", b"")
            if len(body) > _MAX_CONFIG_BODY:
                raise ValueError("request body too large")
            if not event.get("more_body", False):
                return body
        elif event["type"] == "http.disconnect":
            raise ConnectionError("client disconnected during body upload")
