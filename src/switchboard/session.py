"""Session primitives for the dashboard login page.

Stdlib-only session mint/verify pair and a global login throttle. The
session cookie is signed with an HMAC keyed off a hash of the admin
token — no new secret to provision, and rotating the admin token
revokes every outstanding session without server-side state.

Absorbed from sluice.session (Plan 017): renamed cookie to
``switchboard_session``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections import deque

_SESSION_CONTEXT = b"switchboard-session-v1"
_DEFAULT_TTL = 2_592_000  # 30 days

SESSION_COOKIE = "switchboard_session"


def mint_session(admin_token: str, now: float, ttl: int = _DEFAULT_TTL) -> str:
    """Mint a signed session cookie value: ``expiry.hmac_sha256(session_key, expiry)``."""
    session_key = hashlib.sha256(
        _SESSION_CONTEXT + admin_token.encode()
    ).digest()
    expiry = int(now + ttl)
    sig = hmac.new(session_key, str(expiry).encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"{expiry}.{sig_b64}"


def verify_session(
    cookie_value: str | None, admin_token: str | None, now: float
) -> bool:
    """Verify a session cookie value.

    Returns True only when the cookie is well-formed, the signature
    matches under hmac.compare_digest, and expiry > now. Every
    degenerate input returns False; this function never raises.
    """
    if not cookie_value or not admin_token:
        return False
    parts = cookie_value.split(".")
    if len(parts) != 2:
        return False
    expiry_str, sig_str = parts
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if expiry <= now:
        return False
    session_key = hashlib.sha256(
        _SESSION_CONTEXT + admin_token.encode()
    ).digest()
    expected = hmac.new(
        session_key, str(expiry).encode(), hashlib.sha256
    ).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=")
    return hmac.compare_digest(expected_b64, sig_str.encode("utf-8"))


class LoginThrottle:
    """Global in-memory login throttle."""

    def __init__(
        self, max_failures: int = 20, lockout_seconds: int = 120
    ) -> None:
        self._max_failures = max_failures
        self._lockout_seconds = lockout_seconds
        self._failures: deque[float] = deque()

    def _evict(self, now: float) -> None:
        cutoff = now - self._lockout_seconds
        while self._failures and self._failures[0] <= cutoff:
            self._failures.popleft()

    def is_locked(self, now: float) -> bool:
        self._evict(now)
        return len(self._failures) >= self._max_failures

    def record_failure(self, now: float) -> None:
        self._failures.append(now)
        self._evict(now)

    def record_success(self, now: float) -> None:
        """No-op: success does not reset a lockout."""

    def retry_after(self, now: float) -> int:
        """Seconds until the lockout would expire (0 when not locked)."""
        self._evict(now)
        if self._max_failures <= 0:
            return 0
        if len(self._failures) < self._max_failures:
            return 0
        idx = len(self._failures) - self._max_failures
        target = self._failures[idx]
        return max(1, int(target + self._lockout_seconds - now))
