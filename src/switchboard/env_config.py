"""Environment-variable overrides for provider configuration (Plan 021 D6).

Precedence is **env > store (GUI) > TOML**. The store beating TOML is Plan
020 D1 and is what makes GUI edits survive a restart; this module adds the
tier above it, so a deployment can always assert control over whatever the
GUI last wrote.

Why a deployment needs that lever: the store lives on a PVC and outranks the
mounted config, so a bad GUI edit is not fixable by editing the configmap and
rolling the pod. Env is the one channel a k8s Deployment or `docker run`
always owns. (The blunter instrument — clearing store rows outright — is
:mod:`switchboard.config_reset`.)

Naming::

    SWITCHBOARD_PROVIDER_<NAME>_<FIELD>

``<NAME>`` is the provider name upper-cased with every non-alphanumeric
character replaced by ``_``, so ``opencode-go`` becomes ``OPENCODE_GO``::

    SWITCHBOARD_PROVIDER_OPENCODE_GO_UPSTREAM=https://opencode.ai/zen/go/v1
    SWITCHBOARD_PROVIDER_OPENCODE_GO_TARGET=2
    SWITCHBOARD_PROVIDER_ZAI_ENABLED=false

This module is pure with respect to the environment: the mapping is passed
in, never read from :data:`os.environ` here, so the behaviour is testable
without mutating process state.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

log = logging.getLogger("switchboard.env_config")

ENV_PREFIX = "SWITCHBOARD_PROVIDER_"

#: field name in a TOML-shaped provider section -> how to coerce the string.
#: Only fields that are safe to set from a deployment appear here. Notably
#: absent: `api_key`. A raw credential must arrive through `api_key_env`
#: indirection so it is never a value this module can echo into a config
#: dump or an error message.
_FIELDS: dict[str, str] = {
    "UPSTREAM": "upstream",
    "TYPE": "type",
    "TARGET": "int",
    "AUTH_HEADER": "auth_header",
    "AUTH_PREFIX": "auth_prefix",
    "API_KEY_ENV": "api_key_env",
    "USAGE_KEY_ENV": "usage_key_env",
    "DASHBOARD_URL": "dashboard_url",
    "DASHBOARD_TOKEN_ENV": "dashboard_token_env",
    "ENABLED": "bool",
}

_INT_FIELDS = {"TARGET"}
_BOOL_FIELDS = {"ENABLED"}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class EnvOverrideError(ValueError):
    """A malformed or ambiguous env override. Fail closed rather than guess."""


def env_name_for(provider: str) -> str:
    """The env-var stem a provider's overrides use."""
    return re.sub(r"[^A-Za-z0-9]", "_", provider).upper()


def _field_key(field: str) -> str:
    """Map an env FIELD suffix to its provider-section key."""
    spec = _FIELDS[field]
    if spec in ("int", "bool"):
        return field.lower()
    return spec


def _coerce(provider: str, field: str, raw: str) -> Any:
    if field in _INT_FIELDS:
        try:
            value = int(raw)
        except ValueError:
            raise EnvOverrideError(
                f"{ENV_PREFIX}{env_name_for(provider)}_{field} must be an "
                f"integer, got {raw!r}"
            ) from None
        if value < 0:
            raise EnvOverrideError(
                f"{ENV_PREFIX}{env_name_for(provider)}_{field} must not be "
                f"negative, got {value}"
            )
        return value
    if field in _BOOL_FIELDS:
        lowered = raw.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise EnvOverrideError(
            f"{ENV_PREFIX}{env_name_for(provider)}_{field} must be a boolean "
            f"(one of {sorted(_TRUE | _FALSE)}), got {raw!r}"
        )
    return raw


def collect_overrides(
    provider_names: set[str], environ: Mapping[str, str]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Parse overrides for ``provider_names`` out of ``environ``.

    Returns ``(overrides, unmatched)`` — the per-provider field maps, and the
    env-var names that look like overrides but match no known provider.

    Resolution is *from the known names outward*, not by parsing the variable:
    ``SWITCHBOARD_PROVIDER_OPENCODE_GO_UPSTREAM`` cannot be split back into a
    name and a field unambiguously (``OPENCODE`` + ``GO_UPSTREAM`` is an
    equally valid reading). Computing each known provider's stem and looking
    for its variables removes the guesswork entirely.

    Raises :class:`EnvOverrideError` when two providers share a stem — with
    ``opencode-go`` and ``opencode_go`` both configured, an override for
    either is unattributable, and silently applying it to one is worse than
    refusing to start.
    """
    stems: dict[str, str] = {}
    for name in sorted(provider_names):
        stem = env_name_for(name)
        if stem in stems:
            raise EnvOverrideError(
                f"providers {stems[stem]!r} and {name!r} both map to the env "
                f"stem {ENV_PREFIX}{stem}_*, so an override for either is "
                "ambiguous; rename one of them"
            )
        stems[stem] = name

    overrides: dict[str, dict[str, Any]] = {}
    claimed: set[str] = set()

    for stem, name in stems.items():
        for field in _FIELDS:
            var = f"{ENV_PREFIX}{stem}_{field}"
            if var not in environ:
                continue
            claimed.add(var)
            raw = environ[var]
            if not raw.strip():
                # An empty value is almost always an unset k8s Secret key or a
                # shell expansion that produced nothing. Applying it would
                # blank a working field; refusing is noisy but recoverable.
                raise EnvOverrideError(f"{var} is set but empty")
            overrides.setdefault(name, {})[_field_key(field)] = _coerce(
                name, field, raw
            )

    unmatched = sorted(
        var
        for var in environ
        if var.startswith(ENV_PREFIX) and var not in claimed
    )
    return overrides, unmatched


def apply_overrides(
    effective: dict[str, dict[str, Any]], environ: Mapping[str, str]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]], list[str]]:
    """Apply env overrides on top of an already-merged provider set.

    ``effective`` is the TOML+store merge from
    :meth:`~switchboard.config_store.ConfigStoreManager.effective_providers`.

    Unlike D1's store-over-TOML rule, env overrides merge **per field** rather
    than replacing a provider wholesale. Wholesale replacement is right for
    the store, where the GUI writes a complete row; it would be wrong here,
    where a Deployment typically pins one thing — a target, an upstream — and
    has no business discarding the rest of the configuration to do it.

    Returns ``(effective, field_sources, unmatched)``. ``field_sources`` maps
    provider -> field -> ``"env"`` for everything env owns, so
    ``/admin/config/effective`` can answer "why is it this value" and the GUI
    can lock those inputs.
    """
    overrides, unmatched = collect_overrides(set(effective), environ)

    field_sources: dict[str, dict[str, str]] = {}
    for name, fields in overrides.items():
        section = effective.get(name)
        if section is None:  # pragma: no cover - collect_overrides filters these
            continue
        enabled = fields.pop("enabled", None)
        for key, value in fields.items():
            section[key] = value
            field_sources.setdefault(name, {})[key] = "env"
        if enabled is False:
            # Deliberately after the field loop: a Deployment that both pins a
            # field and disables the provider means the disable.
            log.warning(
                "provider %r disabled by %s%s_ENABLED",
                name, ENV_PREFIX, env_name_for(name),
            )
            effective.pop(name, None)
            field_sources.setdefault(name, {})["enabled"] = "env"
        elif enabled is True:
            field_sources.setdefault(name, {})["enabled"] = "env"

    if overrides:
        log.info(
            "env overrides applied: %s",
            ", ".join(
                f"{n}({','.join(sorted(f))})"
                for n, f in sorted(field_sources.items())
            ),
        )
    for var in unmatched:
        # Inert rather than dangerous, so not fatal — but silence would let an
        # operator believe a deployment controls a field it does not. Surfaced
        # through /admin/config/effective as well as here.
        log.warning(
            "%s does not match any configured provider and was ignored", var
        )

    return effective, field_sources, unmatched
