"""Quarantine, and the discrimination that keeps it from being a foot-gun.

Plan 023. The feature exists because five consecutive failures should stop the
bleeding and demand a human. It is dangerous because the five failures that
prompted it were caused by the *client* — a `Python-urllib` User-Agent that
opencode.ai's Cloudflare rule bans (error 1010), forwarded verbatim by
switchboard. Counting those would have quarantined a healthy provider, then its
fallback when the retry replayed the same header, until the model was
unservable.

So the tests that matter most here are the ones proving a caller-caused failure
never quarantines anything — including one built from the actual block page.
"""

from __future__ import annotations

import pytest

from switchboard.control import FailureAttribution, classify_failure
from switchboard.quarantine import (
    QuarantineStore,
    QuarantineTracker,
)

# Trimmed from the real response observed on 2026-08-09 through the deployed
# pod. Kept verbatim in shape (HTML body, cf-ray, server: cloudflare) because
# the whole discriminator rests on those signals.
_CLOUDFLARE_1010_BODY = (
    "<!doctype html><html><head><title>Access denied | opencode.ai used "
    "Cloudflare to restrict access</title></head><body>"
    "<h2>What happened?</h2><p>The owner of this website (opencode.ai) has "
    "banned your access based on your browser's signature "
    "(a285465e5aef0049-ua45).</p></body></html>"
)
_CLOUDFLARE_1010_HEADERS = {
    "server": "cloudflare",
    "cf-ray": "a285460cd8eb8b77",
    "content-type": "text/html; charset=UTF-8",
}

_API_403_BODY = '{"error":{"message":"Invalid API key","type":"auth_error"}}'
_API_403_HEADERS = {"content-type": "application/json"}


# ── attribution ────────────────────────────────────────────────────────────


def test_the_real_cloudflare_block_is_the_callers_fault() -> None:
    """The exact failure that prompted this feature must NOT count. It was a
    banned client User-Agent; the next provider's edge would reject it too."""
    assert (
        classify_failure(403, _CLOUDFLARE_1010_HEADERS, _CLOUDFLARE_1010_BODY)
        is FailureAttribution.CALLER
    )


def test_a_json_403_from_the_api_is_the_providers_fault() -> None:
    """A refused credential is this pair being broken, and is exactly what an
    operator wants quarantined."""
    assert (
        classify_failure(403, _API_403_HEADERS, _API_403_BODY)
        is FailureAttribution.PROVIDER
    )


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
def test_server_errors_are_the_providers_fault(status: int) -> None:
    assert classify_failure(status, {}, "") is FailureAttribution.PROVIDER


def test_transport_failure_is_the_providers_fault() -> None:
    """No status at all: connect, TLS, or timeout."""
    assert classify_failure(None) is FailureAttribution.PROVIDER


@pytest.mark.parametrize("status", [200, 201, 204, 301, 302])
def test_success_resets(status: int) -> None:
    assert classify_failure(status, {}, "") is FailureAttribution.NONE


def test_429_is_not_a_fault() -> None:
    """Quota exhaustion is normal operation. The breaker and the usage-error
    reroute own it; a busy provider must never become a quarantined one."""
    assert classify_failure(429, {}, "") is FailureAttribution.NONE


@pytest.mark.parametrize("status", [400, 404, 405, 422])
def test_malformed_requests_are_the_callers_fault(status: int) -> None:
    assert classify_failure(status, {}, "") is FailureAttribution.CALLER


def test_edge_detected_by_header_even_with_a_json_body() -> None:
    """An edge that answers in JSON is still an edge. Either signal suffices."""
    assert (
        classify_failure(403, {"cf-ray": "abc"}, '{"m":"nope"}')
        is FailureAttribution.CALLER
    )


def test_edge_detected_by_body_even_without_headers() -> None:
    """A CDN that strips its own headers still returns HTML."""
    assert (
        classify_failure(401, {}, "<html>go away</html>")
        is FailureAttribution.CALLER
    )


def test_ambiguity_resolves_to_caller() -> None:
    """An empty body with no headers is unattributable. A missed quarantine
    costs requests; a false one costs a provider, then its fallback."""
    assert classify_failure(403, {}, "") is FailureAttribution.CALLER


# ── counting ───────────────────────────────────────────────────────────────


def _tracker(**kw: object) -> QuarantineTracker:
    return QuarantineTracker(clock=lambda: 1000.0, **kw)  # type: ignore[arg-type]


def test_five_provider_failures_quarantine_the_pair() -> None:
    t = _tracker()
    for i in range(4):
        assert t.record("p", "m", FailureAttribution.PROVIDER, status=500) is False
        assert t.is_quarantined("p", "m") is False, f"early at {i + 1}"
    assert t.record("p", "m", FailureAttribution.PROVIDER, status=500) is True
    assert t.is_quarantined("p", "m") is True


def test_a_success_resets_the_streak() -> None:
    """Five scattered failures across a day are not the same event as five in
    a row, and only the latter means the pair is broken now."""
    t = _tracker()
    for _ in range(4):
        t.record("p", "m", FailureAttribution.PROVIDER, status=500)
    t.record("p", "m", FailureAttribution.NONE)
    for _ in range(4):
        t.record("p", "m", FailureAttribution.PROVIDER, status=500)
    assert t.is_quarantined("p", "m") is False


def test_caller_failures_never_quarantine() -> None:
    """The incident this feature came from: fifty client-caused 403s must
    leave a healthy provider serving."""
    t = _tracker()
    for _ in range(50):
        t.record(
            "opencode-go", "deepseek-v4-flash",
            classify_failure(403, _CLOUDFLARE_1010_HEADERS, _CLOUDFLARE_1010_BODY),
            status=403,
        )
    assert t.is_quarantined("opencode-go", "deepseek-v4-flash") is False
    assert t.entries() == []


def test_caller_failures_do_not_reset_a_real_streak() -> None:
    """A client-caused failure is evidence of nothing, in either direction."""
    t = _tracker()
    for _ in range(4):
        t.record("p", "m", FailureAttribution.PROVIDER, status=500)
    t.record("p", "m", FailureAttribution.CALLER, status=400)
    assert t.record("p", "m", FailureAttribution.PROVIDER, status=500) is True


def test_quarantine_is_per_model_not_per_provider() -> None:
    """A model map entry pointing at a model the vendor retired must not cost
    the vendor its other models."""
    t = _tracker()
    for _ in range(5):
        t.record("p", "dead-model", FailureAttribution.PROVIDER, status=404 and 500)
    assert t.is_quarantined("p", "dead-model") is True
    assert t.is_quarantined("p", "live-model") is False
    assert t.filter_candidates(("p", "q"), "live-model") == ("p", "q")
    assert t.filter_candidates(("p", "q"), "dead-model") == ("q",)


def test_threshold_zero_disables_quarantining() -> None:
    t = _tracker(threshold=0)
    for _ in range(20):
        t.record("p", "m", FailureAttribution.PROVIDER, status=500)
    assert t.is_quarantined("p", "m") is False
    # Counting still happens, so the pair remains visible.
    assert t.counters()["p/m"] == 20


def test_entry_carries_enough_to_decide_without_logs() -> None:
    t = _tracker()
    for _ in range(5):
        t.record("p", "m", FailureAttribution.PROVIDER, status=502, detail="bad gateway")
    (entry,) = t.entries()
    assert entry.provider == "p"
    assert entry.model == "m"
    assert entry.failures == 5
    assert entry.last_status == 502
    assert "bad gateway" in entry.last_detail
    assert entry.first_failure_at > 0


def test_release_restores_service_and_clears_the_counter() -> None:
    t = _tracker()
    for _ in range(5):
        t.record("p", "m", FailureAttribution.PROVIDER, status=500)
    assert t.release("p", "m") is True
    assert t.is_quarantined("p", "m") is False
    # The counter resets too: a released pair gets a full five chances again,
    # not one.
    for _ in range(4):
        t.record("p", "m", FailureAttribution.PROVIDER, status=500)
    assert t.is_quarantined("p", "m") is False


def test_release_of_an_unquarantined_pair_reports_false() -> None:
    assert _tracker().release("p", "m") is False


def test_release_all() -> None:
    t = _tracker()
    for model in ("a", "b"):
        for _ in range(5):
            t.record("p", model, FailureAttribution.PROVIDER, status=500)
    assert t.release_all() == 2
    assert t.entries() == []


def test_further_failures_while_quarantined_do_not_re_fire() -> None:
    """record() returns True exactly once per quarantine, so the caller can
    use it to alert without spamming."""
    t = _tracker()
    fired = [t.record("p", "m", FailureAttribution.PROVIDER, status=500) for _ in range(10)]
    assert fired.count(True) == 1


# ── persistence ────────────────────────────────────────────────────────────


class _MemStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def as_store(self) -> QuarantineStore:
        return QuarantineStore(
            load=lambda: self.rows,
            save=lambda rows: setattr(self, "rows", rows),
        )


def test_quarantine_survives_a_restart() -> None:
    """A pod restart must not silently un-quarantine something no human has
    looked at — that turns a standing alarm into a five-minute one."""
    mem = _MemStore()
    t = QuarantineTracker(store=mem.as_store(), clock=lambda: 1000.0)
    for _ in range(5):
        t.record("p", "m", FailureAttribution.PROVIDER, status=500)
    assert mem.rows, "nothing was persisted"

    restarted = QuarantineTracker(store=mem.as_store(), clock=lambda: 2000.0)
    assert restarted.is_quarantined("p", "m") is True


def test_release_is_persisted() -> None:
    mem = _MemStore()
    t = QuarantineTracker(store=mem.as_store(), clock=lambda: 1000.0)
    for _ in range(5):
        t.record("p", "m", FailureAttribution.PROVIDER, status=500)
    t.release("p", "m")
    restarted = QuarantineTracker(store=mem.as_store(), clock=lambda: 2000.0)
    assert restarted.is_quarantined("p", "m") is False


def test_a_malformed_persisted_row_is_skipped_not_fatal() -> None:
    """Losing a quarantine costs some failed requests. Refusing to start costs
    the whole estate."""
    mem = _MemStore()
    mem.rows = [
        {"provider": "good", "model": "m", "failures": 5,
         "first_failure_at": 1.0, "last_failure_at": 2.0},
        {"model": "no-provider-key"},
        "not even a dict",  # type: ignore[list-item]
    ]
    t = QuarantineTracker(store=mem.as_store())
    assert t.is_quarantined("good", "m") is True
    assert len(t.entries()) == 1


def test_an_unreadable_store_does_not_prevent_startup() -> None:
    def _boom() -> list[dict[str, object]]:
        raise OSError("disk gone")

    t = QuarantineTracker(
        store=QuarantineStore(load=_boom, save=lambda rows: None)
    )
    assert t.entries() == []


def test_config_store_round_trip(tmp_path) -> None:
    """The real persistence path, not the in-memory stand-in."""
    from switchboard.config_store import ConfigStoreManager
    from switchboard.quarantine import config_store_quarantine_store

    db = str(tmp_path / "store.sqlite3")
    store = ConfigStoreManager(sqlite_path=db)
    t = QuarantineTracker(
        store=config_store_quarantine_store(store), clock=lambda: 1000.0
    )
    for _ in range(5):
        t.record("p", "m", FailureAttribution.PROVIDER, status=500)
    store.close()

    reopened = ConfigStoreManager(sqlite_path=db)
    restarted = QuarantineTracker(
        store=config_store_quarantine_store(reopened)
    )
    assert restarted.is_quarantined("p", "m") is True
    (entry,) = restarted.entries()
    assert entry.last_status == 500
