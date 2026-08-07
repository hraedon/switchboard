"""Provider config store — GUI-managed provider rows + optional SQLite persistence.

Plan 020 WI-1 (D1/D2). Providers have always been boot-time TOML; the GUI
needs to create and modify them at runtime and have that survive a restart.
This manager owns those rows, mirroring the route-table / model-map shape:
in-memory state as the source of truth for reads, with optional SQLite
persistence (own file via ``sqlite_path=`` or a shared connection via
``db=``).

Precedence rule (D1): TOML is loaded first, then the store is overlaid — a
store row with the same provider name wins **wholesale** (no field merging;
merging is where silent config bugs live), and a disabled store row removes
the provider from the effective set even if the TOML declares it.
:meth:`ConfigStoreManager.effective_providers` implements exactly this, and
returns TOML-shaped sections so the existing
:func:`switchboard.providers.build_provider_contexts_from_config` path is
reused unchanged.

Credentials (D2): GUI-entered keys (``key_mode='stored'``) live in the
store's SQLite file, chmod ``0600``. They are write-only through this API —
:meth:`get` / :meth:`list_providers` mask them to ``api_key_set`` + a last-4
hint; only :meth:`resolve_key` and the construction-path
:meth:`to_provider_section` / :meth:`effective_providers` carry the raw
credential, and those must never feed a serialization surface unmasked.
No log line, ``repr``, or error message emitted here contains a credential.
Encryption-at-rest is a follow-on WI; the ``key_mode`` discriminator leaves
room for it without a migration.

Fail-safety mirrors :mod:`switchboard.history`'s contract: loading is
per-row fail-safe (a malformed row logs a warning naming the row and is
skipped; a whole-table failure logs and yields an empty store) — boot must
never crash on a bad store. Writes are DB-first: on DB failure the mutation
raises and memory is unchanged, so what the admin sees is what persisted.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from switchboard.truth import get_provider

log = logging.getLogger("switchboard.config_store")

_KEY_MODES = ("env", "stored", "passthrough")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_config (
    name TEXT PRIMARY KEY,
    account TEXT NOT NULL DEFAULT 'default',
    upstream TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    target INTEGER NOT NULL,
    key_mode TEXT NOT NULL CHECK(key_mode IN ('env','stored','passthrough')),
    api_key_env TEXT,
    api_key_stored TEXT,
    auth_header TEXT,
    auth_prefix TEXT,
    dashboard_url TEXT,
    dashboard_token_env TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""

_COLUMNS = (
    "name",
    "account",
    "upstream",
    "provider_type",
    "target",
    "key_mode",
    "api_key_env",
    "api_key_stored",
    "auth_header",
    "auth_prefix",
    "dashboard_url",
    "dashboard_token_env",
    "enabled",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class _ProviderRow:
    """One provider's stored configuration.

    ``api_key_stored`` is excluded from the dataclass repr so no debug
    surface (including ``repr(manager)`` internals) can leak it.
    """

    name: str
    account: str
    upstream: str
    provider_type: str
    target: int
    key_mode: str
    api_key_env: str | None
    api_key_stored: str | None = field(repr=False)
    auth_header: str | None = None
    auth_prefix: str | None = None
    dashboard_url: str | None = None
    dashboard_token_env: str | None = None
    enabled: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0


class ConfigStoreManager:
    """In-memory provider config with CRUD + optional SQLite persistence.

    Construct with either ``sqlite_path=`` (own file — created ``0600``
    because it may hold credentials) or ``db=`` (share an existing
    connection, e.g. the route-table store), never both. ``clock`` exists
    for deterministic ``created_at``/``updated_at`` in tests.
    """

    def __init__(
        self,
        *,
        db: sqlite3.Connection | None = None,
        sqlite_path: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if db is not None and sqlite_path is not None:
            raise ValueError("pass either db= or sqlite_path=, not both")

        self._rows: dict[str, _ProviderRow] = {}
        self._clock = clock
        self._db: sqlite3.Connection | None = db

        if sqlite_path is not None:
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(sqlite_path, check_same_thread=False)
            # The file may hold credentials (key_mode='stored'); tighten it
            # before any row is written. Some volumes (k8s) forbid chmod —
            # degrade quietly rather than fail boot.
            try:
                os.chmod(sqlite_path, 0o600)
            except OSError:
                log.debug(
                    "config store: could not chmod %s to 0600", sqlite_path
                )

        if self._db is not None:
            try:
                self._db.execute(_SCHEMA)
                self._db.commit()
            except sqlite3.Error:
                # Boot must not crash on a bad store file; later writes will
                # raise and surface the problem to the admin instead.
                log.warning(
                    "config store: could not ensure provider_config table; "
                    "store starts empty and writes may fail",
                    exc_info=True,
                )
            else:
                self._load_from_db()

    # -- loading ----------------------------------------------------------

    def _load_from_db(self) -> None:
        db = self._db
        if db is None:
            return
        try:
            cursor = db.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM provider_config"
            )
            fetched = cursor.fetchall()
        except sqlite3.Error:
            log.warning(
                "config store: failed to read provider_config; "
                "starting with an empty store",
                exc_info=True,
            )
            return
        for raw in fetched:
            # Per-row fail-safety: one bad row must not take out the rest.
            # The warning names the row but never echoes column values —
            # a corrupt row may still hold a real credential.
            try:
                row = _ProviderRow(
                    name=str(raw[0]),
                    account=str(raw[1]),
                    upstream=str(raw[2]),
                    provider_type=str(raw[3]),
                    target=int(raw[4]),
                    key_mode=str(raw[5]),
                    api_key_env=_opt_str(raw[6]),
                    api_key_stored=_opt_str(raw[7]),
                    auth_header=_opt_str(raw[8]),
                    auth_prefix=_opt_str(raw[9]),
                    dashboard_url=_opt_str(raw[10]),
                    dashboard_token_env=_opt_str(raw[11]),
                    enabled=1 if raw[12] else 0,
                    created_at=float(raw[13]),
                    updated_at=float(raw[14]),
                )
                _validate_row(row)
            except (ValueError, TypeError, IndexError):
                log.warning(
                    "config store: skipping malformed provider_config row "
                    "%r",
                    raw[0] if raw else "<unnamed>",
                )
                continue
            self._rows[row.name] = row

    # -- mutation (DB-first) ----------------------------------------------

    def upsert(self, name: str, fields: dict[str, object]) -> None:
        """Create or replace a provider row (whole-row semantics).

        Every call replaces the row from ``fields``; unspecified optional
        fields reset to their defaults — with one exception: for an existing
        provider, an absent/None ``api_key_stored`` with ``key_mode='stored'``
        KEEPS the already-stored key. Keys are write-only through the API, so
        the GUI never round-trips them; absence on edit means "unchanged",
        not "clear".

        Raises :class:`ValueError` with an admin-surfaceable message on
        validation failure. DB-first: the row is committed before memory is
        updated, so a DB failure raises and leaves memory unchanged.
        """
        if not name:
            raise ValueError("provider name must be a non-empty string")
        existing = self._rows.get(name)
        now = self._clock()
        row = self._build_row(name, fields, existing, now)
        _validate_row(row)

        if self._db is not None:
            placeholders = ", ".join("?" for _ in _COLUMNS)
            self._db.execute(
                f"INSERT OR REPLACE INTO provider_config "
                f"({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                (
                    row.name,
                    row.account,
                    row.upstream,
                    row.provider_type,
                    row.target,
                    row.key_mode,
                    row.api_key_env,
                    row.api_key_stored,
                    row.auth_header,
                    row.auth_prefix,
                    row.dashboard_url,
                    row.dashboard_token_env,
                    row.enabled,
                    row.created_at,
                    row.updated_at,
                ),
            )
            self._db.commit()
        self._rows[name] = row
        # Name only — the fields dict may carry a credential.
        log.info("config store: upserted provider %r", name)

    def _build_row(
        self,
        name: str,
        fields: dict[str, object],
        existing: _ProviderRow | None,
        now: float,
    ) -> _ProviderRow:
        upstream = _req_str(fields, "upstream")
        provider_type = _req_str(fields, "provider_type")
        target = _req_int(fields, "target")
        key_mode = _req_str(fields, "key_mode")

        api_key_stored = _opt_str_field(fields, "api_key_stored")
        # Write-only key semantics: on edit, an absent key means "keep".
        if (
            key_mode == "stored"
            and api_key_stored is None
            and existing is not None
        ):
            api_key_stored = existing.api_key_stored

        account_val = _opt_str_field(fields, "account")
        enabled_raw = fields.get("enabled", 1)
        if not isinstance(enabled_raw, (bool, int)):
            raise ValueError("field 'enabled' must be a boolean")

        return _ProviderRow(
            name=name,
            account=account_val if account_val is not None else "default",
            upstream=upstream,
            provider_type=provider_type,
            target=target,
            key_mode=key_mode,
            api_key_env=_opt_str_field(fields, "api_key_env"),
            api_key_stored=api_key_stored,
            auth_header=_opt_str_field(fields, "auth_header"),
            auth_prefix=_opt_str_field(fields, "auth_prefix"),
            dashboard_url=_opt_str_field(fields, "dashboard_url"),
            dashboard_token_env=_opt_str_field(fields, "dashboard_token_env"),
            enabled=1 if enabled_raw else 0,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )

    def remove(self, name: str) -> bool:
        """Remove a provider row. DB-first; True if found and removed."""
        if name not in self._rows:
            return False
        if self._db is not None:
            self._db.execute(
                "DELETE FROM provider_config WHERE name = ?", (name,)
            )
            self._db.commit()
        del self._rows[name]
        log.info("config store: removed provider %r", name)
        return True

    # -- masked read surface ------------------------------------------------

    def get(self, name: str) -> dict[str, object] | None:
        """Return one provider as a MASKED dict, or None if absent.

        Never includes ``api_key_stored``; exposes ``api_key_set`` and a
        last-4 ``api_key_hint`` instead. Safe to serialize as-is.
        """
        row = self._rows.get(name)
        if row is None:
            return None
        return _masked(row)

    def list_providers(self) -> list[dict[str, object]]:
        """Return all providers as MASKED dicts (see :meth:`get`)."""
        return [_masked(row) for row in self._rows.values()]

    # -- unmasked construction path -----------------------------------------

    def resolve_key(self, name: str) -> tuple[str, str, str] | None:
        """Resolve a provider's credential configuration, UNMASKED.

        Returns ``(key_mode, credential_or_env_name, auth_header)`` — the
        middle element is the raw stored credential for ``key_mode='stored'``,
        the env var NAME for ``'env'``, and ``''`` for ``'passthrough'``; the
        auth header falls back to ``'authorization'``. None if the provider
        is not in the store.

        This is the ONLY unmasked accessor besides the construction-path
        section builders, intended solely for provider construction. It must
        never be called from a serialization path (status, admin GET, logs,
        metrics) — use :meth:`get` / :meth:`list_providers` there.
        """
        row = self._rows.get(name)
        if row is None:
            return None
        if row.key_mode == "stored":
            credential = row.api_key_stored or ""
        elif row.key_mode == "env":
            credential = row.api_key_env or ""
        else:
            credential = ""
        return (row.key_mode, credential, row.auth_header or "authorization")

    def to_provider_section(self, name: str) -> dict[str, object]:
        """Return a dict shaped exactly like a parsed TOML ``[provider.<name>]``.

        Construction-path only: for ``key_mode='stored'`` the section carries
        the raw credential under the inline ``api_key`` key (that is what
        :func:`~switchboard.providers.build_provider_contexts_from_config`
        reads), ``'env'`` maps to ``api_key_env``, and ``'passthrough'`` maps
        to neither. Do NOT serialize this without masking — use :meth:`get`
        for any read surface.

        Raises :class:`KeyError` if the provider is not in the store.
        """
        row = self._rows.get(name)
        if row is None:
            raise KeyError(f"provider {name!r} is not in the config store")
        section: dict[str, object] = {
            "upstream": row.upstream,
            "type": row.provider_type,
            "target": row.target,
        }
        if row.key_mode == "env" and row.api_key_env is not None:
            section["api_key_env"] = row.api_key_env
        elif row.key_mode == "stored" and row.api_key_stored is not None:
            section["api_key"] = row.api_key_stored
        # Optional keys are omitted (not set to None): the construction
        # path's _str_or/_optional_str treat a missing key as the default.
        if row.auth_header is not None:
            section["auth_header"] = row.auth_header
        if row.auth_prefix is not None:
            section["auth_prefix"] = row.auth_prefix
        if row.dashboard_url is not None:
            section["dashboard_url"] = row.dashboard_url
        if row.dashboard_token_env is not None:
            section["dashboard_token_env"] = row.dashboard_token_env
        return section

    def effective_providers(
        self, toml_config: dict[str, object]
    ) -> dict[str, dict[str, object]]:
        """Merge TOML providers with store rows per the D1 precedence rule.

        Starts from the ``[provider.*]`` tables, then overlays store rows: a
        store row with the same name REPLACES the TOML table wholesale (no
        field merging), and a disabled row (``enabled=0``) REMOVES the
        provider from the effective set even if the TOML declares it.

        Returns name → TOML-shaped section (see :meth:`to_provider_section`),
        ready for ``build_provider_contexts_from_config({"provider": ...})``.
        Construction-path only — stored-key sections carry raw credentials.
        """
        effective: dict[str, dict[str, object]] = {}
        raw_providers = toml_config.get("provider")
        if isinstance(raw_providers, dict):
            for name, section in raw_providers.items():
                if isinstance(section, dict):
                    effective[str(name)] = dict(section)
        for name, row in self._rows.items():
            if row.enabled:
                effective[name] = self.to_provider_section(name)
            else:
                effective.pop(name, None)
        return effective

    # -- misc ----------------------------------------------------------------

    @property
    def db(self) -> sqlite3.Connection | None:
        """The SQLite connection, if persistence is configured."""
        return self._db

    def close(self) -> None:
        """Close the SQLite connection if open."""
        if self._db is not None:
            self._db.close()
            self._db = None

    def __repr__(self) -> str:
        # Names only — rows hold credentials.
        return (
            f"ConfigStoreManager(providers={sorted(self._rows)}, "
            f"persistent={self._db is not None})"
        )


def _masked(row: _ProviderRow) -> dict[str, object]:
    """Serialize a row with the stored credential replaced by set/hint."""
    key = row.api_key_stored or ""
    return {
        "name": row.name,
        "account": row.account,
        "upstream": row.upstream,
        "provider_type": row.provider_type,
        "target": row.target,
        "key_mode": row.key_mode,
        "api_key_env": row.api_key_env,
        "api_key_set": bool(key),
        "api_key_hint": key[-4:] if key else "",
        "auth_header": row.auth_header,
        "auth_prefix": row.auth_prefix,
        "dashboard_url": row.dashboard_url,
        "dashboard_token_env": row.dashboard_token_env,
        "enabled": bool(row.enabled),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _validate_row(row: _ProviderRow) -> None:
    """Raise ValueError (admin-surfaceable as a 400) on an invalid row.

    Messages never include credential values — only which constraint failed.
    """
    if not row.upstream:
        raise ValueError(
            f"provider {row.name!r}: 'upstream' must be a non-empty URL"
        )
    # Delegates unknown-type wording to the registry (lists valid types).
    get_provider(row.provider_type)
    if row.target < 1:
        raise ValueError(
            f"provider {row.name!r}: 'target' must be >= 1, got {row.target}"
        )
    if row.key_mode not in _KEY_MODES:
        raise ValueError(
            f"provider {row.name!r}: 'key_mode' must be one of "
            f"{list(_KEY_MODES)}, got {row.key_mode!r}"
        )
    if row.key_mode == "env" and not row.api_key_env:
        raise ValueError(
            f"provider {row.name!r}: key_mode='env' requires a non-empty "
            "'api_key_env' (the environment variable name)"
        )
    if row.key_mode == "stored" and not row.api_key_stored:
        raise ValueError(
            f"provider {row.name!r}: key_mode='stored' requires "
            "'api_key_stored' (the credential) on first write"
        )


def _req_str(fields: dict[str, object], key: str) -> str:
    v = fields.get(key)
    if not isinstance(v, str) or not v:
        raise ValueError(f"field {key!r} is required and must be a non-empty string")
    return v


def _req_int(fields: dict[str, object], key: str) -> int:
    v = fields.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"field {key!r} is required and must be an integer")
    return v


def _opt_str_field(fields: dict[str, object], key: str) -> str | None:
    v = fields.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"field {key!r} must be a string when present")
    return v


def _opt_str(v: object) -> str | None:
    return v if isinstance(v, str) else None
