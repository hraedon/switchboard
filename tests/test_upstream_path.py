"""Upstream path composition (Plan 021 WI-1).

Switchboard used to concatenate the client's verbatim path onto the provider
base, which forced clients to omit the ``/v1`` that every OpenAI-compatible
``baseURL`` conventionally carries — send it and you got
``/zen/go/v1/v1/chat/completions`` and a 404 from the upstream, which reads
as switchboard being broken.

The rule now: **the base declares the version if it has one.** A leading
version segment on the client path is dropped only when the base's last
segment is itself a version. That is what lets a provider base be pasted
verbatim from a vendor quickstart while a bare-host base keeps working.
"""

from __future__ import annotations

import httpx
import pytest

from switchboard.control import compose_upstream_path
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp, RoutingConfig
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource

# The three providers configured on the live deployment, with the base URLs
# their vendors document. Each expectation was verified to return 200 against
# the real upstream on 2026-08-08 — these are not guesses about path shape.
OPENCODE = "https://opencode.ai/zen/go/v1"
OLLAMA = "https://ollama.com/v1"
ZAI = "https://api.z.ai/api/coding/paas/v4"


@pytest.mark.parametrize(
    ("base", "client_path", "expected"),
    [
        # --- versioned base + conventional client path: the version is dropped
        (OPENCODE, "/v1/chat/completions",
         "https://opencode.ai/zen/go/v1/chat/completions"),
        (OLLAMA, "/v1/chat/completions",
         "https://ollama.com/v1/chat/completions"),
        (ZAI, "/v1/chat/completions",
         "https://api.z.ai/api/coding/paas/v4/chat/completions"),

        # --- versioned base + today's bare client path: unchanged behaviour.
        # This is the compatibility property. Every deployment serving traffic
        # right now sends the bare form, and must keep working byte-for-byte.
        (OPENCODE, "/chat/completions",
         "https://opencode.ai/zen/go/v1/chat/completions"),
        (OLLAMA, "/chat/completions",
         "https://ollama.com/v1/chat/completions"),
        (ZAI, "/chat/completions",
         "https://api.z.ai/api/coding/paas/v4/chat/completions"),

        # --- UNVERSIONED base: the client's version must SURVIVE. A bare-host
        # base relying on the client to supply /v1 is a working configuration
        # today; dropping it unconditionally would break it.
        ("https://api.example.com", "/v1/chat/completions",
         "https://api.example.com/v1/chat/completions"),
        ("https://api.example.com", "/v1beta/models",
         "https://api.example.com/v1beta/models"),
        ("https://api.example.com", "/chat/completions",
         "https://api.example.com/chat/completions"),

        # --- at most one segment, leading position only
        ("https://x.test/v1", "/v1/v1/chat",
         "https://x.test/v1/v1/chat"),
        ("https://x.test/v1", "/v1/models/v1/foo",
         "https://x.test/v1/models/v1/foo"),

        # --- version spellings
        ("https://x.test/v1beta", "/v1/models", "https://x.test/v1beta/models"),
        ("https://x.test/v2", "/v2/embeddings", "https://x.test/v2/embeddings"),
        ("https://x.test/v1alpha2", "/v1/x", "https://x.test/v1alpha2/x"),

        # --- a segment that merely starts with "v" is not a version
        ("https://x.test/vision", "/v1/chat",
         "https://x.test/vision/v1/chat"),
        ("https://x.test/v1", "/vision/chat",
         "https://x.test/v1/vision/chat"),

        # --- query strings survive intact
        (OLLAMA, "/v1/models?limit=2&x=y",
         "https://ollama.com/v1/models?limit=2&x=y"),
        ("https://api.example.com", "/v1/models?limit=2",
         "https://api.example.com/v1/models?limit=2"),

        # --- trailing slashes on the base are irrelevant
        ("https://ollama.com/v1/", "/v1/chat/completions",
         "https://ollama.com/v1/chat/completions"),
        ("https://ollama.com/v1///", "/chat/completions",
         "https://ollama.com/v1/chat/completions"),

        # --- degenerate paths must not produce a stray trailing slash
        (OLLAMA, "/", "https://ollama.com/v1"),
        (OLLAMA, "/v1", "https://ollama.com/v1"),
        (OLLAMA, "", "https://ollama.com/v1"),
    ],
)
def test_composition(base: str, client_path: str, expected: str) -> None:
    assert compose_upstream_path(base, client_path) == expected


def test_empty_query_string_adds_no_separator() -> None:
    """A '?' with nothing after it would change the request the upstream sees."""
    assert compose_upstream_path(OLLAMA, "/v1/models") == "https://ollama.com/v1/models"


# --------------------------------------------------------------- integration


def _ctx(name: str, upstream: str) -> ProviderContext:
    gate = PermitGate(initial_capacity=1)
    truth = NullTruthSource(provider="generic")
    return ProviderContext(
        name=name,
        upstream_url=upstream,
        gate=gate,
        reconcile=ReconciliationLoop(
            truth_source=truth,
            gate=gate,
            max_concurrency=1,
            provider_type="generic",
            breaker_config=BreakerConfig(),
        ),
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )


def _scope(path: str, query: bytes = b"") -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }


def _app(providers: dict[str, ProviderContext]) -> ProxyApp:
    return ProxyApp(
        providers=providers,
        route_table=RouteTableManager(default_providers=tuple(providers)),
        routing_config=RoutingConfig(),
    )


def test_build_url_uses_composition() -> None:
    app = _app({"ollama-cloud": _ctx("ollama-cloud", OLLAMA)})
    url = app._build_url(app._providers["ollama-cloud"], _scope("/v1/chat/completions"))
    assert url == "https://ollama.com/v1/chat/completions"


def test_build_url_preserves_query_from_scope() -> None:
    app = _app({"ollama-cloud": _ctx("ollama-cloud", OLLAMA)})
    url = app._build_url(
        app._providers["ollama-cloud"], _scope("/v1/models", b"limit=2")
    )
    assert url == "https://ollama.com/v1/models?limit=2"


def test_reroute_composes_against_the_new_providers_base() -> None:
    """The plan called this the obvious place for a bug to hide.

    A rerouted request must be composed against the provider it is moving TO,
    not the one it came from — the two rarely share a path shape. Here the
    same client path must land on three different upstream layouts.
    """
    app = _app({
        "opencode-go": _ctx("opencode-go", OPENCODE),
        "zai": _ctx("zai", ZAI),
        "bare": _ctx("bare", "https://api.example.com"),
    })
    scope = _scope("/v1/chat/completions")

    assert app._build_url(app._providers["opencode-go"], scope) == (
        "https://opencode.ai/zen/go/v1/chat/completions"
    )
    assert app._build_url(app._providers["zai"], scope) == (
        "https://api.z.ai/api/coding/paas/v4/chat/completions"
    )
    # The unversioned base keeps the client's /v1 — same request, different rule.
    assert app._build_url(app._providers["bare"], scope) == (
        "https://api.example.com/v1/chat/completions"
    )
