"""Env-tier provider overrides (Plan 021 D6, WI-6).

Precedence is env > store (GUI) > TOML. The store beating TOML is what makes
GUI edits survive a restart; env is the tier above it, so a Deployment can
always assert control over whatever the GUI last wrote.
"""

from __future__ import annotations

import pytest

from switchboard.env_config import (
    EnvOverrideError,
    apply_overrides,
    collect_overrides,
    env_name_for,
)


def _effective() -> dict[str, dict[str, object]]:
    return {
        "opencode-go": {
            "upstream": "https://opencode.ai/zen/go/v1",
            "type": "generic",
            "target": 4,
            "api_key_env": "SWITCHBOARD_OPENCODE_GO_KEY",
        },
        "zai": {"upstream": "https://api.z.ai/x", "type": "generic", "target": 2},
    }


# --- naming ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "stem"),
    [
        ("opencode-go", "OPENCODE_GO"),
        ("ollama-cloud", "OLLAMA_CLOUD"),
        ("zai", "ZAI"),
        ("z.ai coding", "Z_AI_CODING"),
    ],
)
def test_env_name_for(provider: str, stem: str) -> None:
    assert env_name_for(provider) == stem


def test_colliding_stems_refuse_to_start() -> None:
    """`opencode-go` and `opencode_go` both map to OPENCODE_GO.

    An override for either is unattributable. Picking one silently would apply
    a deployment's intent to the wrong provider — worse than refusing.
    """
    with pytest.raises(EnvOverrideError, match="ambiguous"):
        collect_overrides({"opencode-go", "opencode_go"}, {})


# --- application -----------------------------------------------------------


def test_env_overrides_a_single_field_without_discarding_the_rest() -> None:
    """Per-field merge, unlike D1's wholesale store-replaces-TOML rule.

    A Deployment usually pins one value; it has no business dropping the
    provider's credential wiring to do it.
    """
    eff, sources, unmatched = apply_overrides(
        _effective(), {"SWITCHBOARD_PROVIDER_OPENCODE_GO_TARGET": "1"}
    )
    assert eff["opencode-go"]["target"] == 1
    assert eff["opencode-go"]["upstream"] == "https://opencode.ai/zen/go/v1"
    assert eff["opencode-go"]["api_key_env"] == "SWITCHBOARD_OPENCODE_GO_KEY"
    assert sources == {"opencode-go": {"target": "env"}}
    assert unmatched == []


def test_env_beats_the_store_value() -> None:
    """The whole point: whatever the GUI wrote, the Deployment wins."""
    eff, sources, _ = apply_overrides(
        _effective(),
        {"SWITCHBOARD_PROVIDER_ZAI_UPSTREAM": "https://override.example/v9"},
    )
    assert eff["zai"]["upstream"] == "https://override.example/v9"
    assert sources["zai"]["upstream"] == "env"


def test_env_can_disable_a_provider() -> None:
    eff, sources, _ = apply_overrides(
        _effective(), {"SWITCHBOARD_PROVIDER_ZAI_ENABLED": "false"}
    )
    assert "zai" not in eff
    assert "opencode-go" in eff
    assert sources["zai"]["enabled"] == "env"


def test_disable_wins_over_a_field_pin_on_the_same_provider() -> None:
    eff, _, _ = apply_overrides(
        _effective(),
        {
            "SWITCHBOARD_PROVIDER_ZAI_TARGET": "9",
            "SWITCHBOARD_PROVIDER_ZAI_ENABLED": "0",
        },
    )
    assert "zai" not in eff


@pytest.mark.parametrize("raw", ["true", "TRUE", "yes", "on", "1"])
def test_enabled_true_spellings_keep_the_provider(raw: str) -> None:
    eff, _, _ = apply_overrides(
        _effective(), {"SWITCHBOARD_PROVIDER_ZAI_ENABLED": raw}
    )
    assert "zai" in eff


def test_unmatched_override_is_reported_not_silently_dropped() -> None:
    """A typo leaves the operator believing a field is deployment-controlled.

    Inert, so not fatal — but it must be visible, which is why it is returned
    for /admin/config/effective rather than only logged.
    """
    _, _, unmatched = apply_overrides(
        _effective(), {"SWITCHBOARD_PROVIDER_TYPO_UPSTREAM": "https://x"}
    )
    assert unmatched == ["SWITCHBOARD_PROVIDER_TYPO_UPSTREAM"]


def test_unrelated_env_is_untouched() -> None:
    eff, sources, unmatched = apply_overrides(
        _effective(),
        {"PATH": "/usr/bin", "SWITCHBOARD_ADMIN_TOKEN": "x", "HOME": "/root"},
    )
    assert eff == _effective()
    assert sources == {}
    assert unmatched == []


def test_provider_key_env_is_not_an_unmatched_override() -> None:
    """SWITCHBOARD_OPENCODE_GO_KEY lacks the PROVIDER_ infix, so it is not
    an override at all — reporting it would train operators to ignore the
    warning."""
    _, _, unmatched = apply_overrides(
        _effective(), {"SWITCHBOARD_OPENCODE_GO_KEY": "sk-secret"}
    )
    assert unmatched == []


# --- fail closed -----------------------------------------------------------


def test_non_integer_target_is_fatal() -> None:
    """A silently-ignored target sizes the gate wrong and throttles a
    provider for the life of the deployment."""
    with pytest.raises(EnvOverrideError, match="must be an integer"):
        apply_overrides(
            _effective(), {"SWITCHBOARD_PROVIDER_ZAI_TARGET": "lots"}
        )


def test_negative_target_is_fatal() -> None:
    with pytest.raises(EnvOverrideError, match="must not be negative"):
        apply_overrides(
            _effective(), {"SWITCHBOARD_PROVIDER_ZAI_TARGET": "-1"}
        )


def test_non_boolean_enabled_is_fatal() -> None:
    with pytest.raises(EnvOverrideError, match="must be a boolean"):
        apply_overrides(
            _effective(), {"SWITCHBOARD_PROVIDER_ZAI_ENABLED": "maybe"}
        )


def test_empty_value_is_fatal() -> None:
    """An empty value is almost always an unset Secret key or a shell
    expansion that produced nothing; applying it would blank a working
    field."""
    with pytest.raises(EnvOverrideError, match="set but empty"):
        apply_overrides(
            _effective(), {"SWITCHBOARD_PROVIDER_ZAI_UPSTREAM": "   "}
        )


def test_no_env_var_can_carry_a_raw_credential() -> None:
    """`api_key` is deliberately not an overridable field.

    Credentials must arrive by `api_key_env` indirection so a raw key is never
    a value this module could echo into a config dump or an error message.
    """
    eff, sources, unmatched = apply_overrides(
        _effective(), {"SWITCHBOARD_PROVIDER_ZAI_API_KEY": "sk-leak"}
    )
    assert "api_key" not in eff["zai"]
    assert sources == {}
    # It is reported as unmatched rather than accepted.
    assert unmatched == ["SWITCHBOARD_PROVIDER_ZAI_API_KEY"]
