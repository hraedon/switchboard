"""Model-map manager — CRUD + optional SQLite persistence (WI-017/012).

Mirrors :class:`~switchboard.route_table.RouteTableManager`: in-memory state
keyed by model name, with optional SQLite persistence that shares the
route-table store connection.  The pure :class:`~switchboard.control.ModelMap`
is the frozen snapshot this manager hands to the routing core; all I/O and
mutation live here, in the shell (AGENTS.md: the routing core stays pure).

Same persistence shape as the route table: when a SQLite connection is
supplied (the route-table store), model aliases persist across restarts so a
deploy does not resurrect stale mappings; without one, aliases are seeded
fresh from the TOML config each boot.

Write ordering (WI-12b): mutations persist to SQLite *first* and touch memory
only after the commit succeeds.  The old order (memory first) meant a failed
DB write left memory claiming a change the store never saw — the admin API
reported success and a restart silently reverted it.  With DB-first ordering
a store failure raises out of the mutator, memory stays consistent with disk,
and the caller can surface the error honestly.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from switchboard.control import ModelMap

log = logging.getLogger("switchboard.model_map")


class ModelMapManager:
    """In-memory model map with CRUD + optional SQLite persistence."""

    def __init__(
        self,
        *,
        db: sqlite3.Connection | None = None,
        valid_providers: frozenset[str] | None = None,
    ) -> None:
        self._routes: dict[str, dict[str, str]] = {}
        self._db: sqlite3.Connection | None = db
        self._valid_providers = valid_providers

        if db is not None:
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_map "
                "(model TEXT PRIMARY KEY, aliases TEXT)"
            )
            db.commit()
            self._load_from_db()

    def _load_from_db(self) -> None:
        """Load persisted rows, distrusting the store (WI-12b).

        The DB is operator-writable state that outlives config changes, so
        two failure modes must not crash boot or route silently nowhere:

        - A row whose aliases JSON is corrupt is logged and skipped (the row
          stays in the DB for forensics); the remaining rows still load.
        - When ``valid_providers`` was supplied, alias keys naming providers
          absent from the current config are dropped with a warning — a row
          with no known provider left is skipped entirely.  ``None`` means
          no validation, preserving prior behavior for bare-store callers.
        """
        db = self._db
        if db is None:
            return
        cursor = db.execute("SELECT model, aliases FROM model_map")
        for model, aliases_json in cursor:
            try:
                aliases: dict[str, str] = json.loads(aliases_json)
            except json.JSONDecodeError as exc:
                log.warning(
                    "model-map row %r has corrupt aliases JSON, skipping: %s",
                    model, exc,
                )
                continue
            if not isinstance(aliases, dict):
                log.warning(
                    "model-map row %r aliases is not an object, skipping",
                    model,
                )
                continue
            # Drop empty-string aliases: a persisted null/empty value makes
            # providers_for() include a provider while alias_for() returns
            # an empty string, silently rewriting the upstream model to "".
            aliases = {k: v for k, v in aliases.items() if isinstance(v, str) and v}
            if not aliases:
                log.warning(
                    "model-map row %r has no non-empty aliases, skipping",
                    model,
                )
                continue
            if self._valid_providers is not None:
                unknown = sorted(
                    k for k in aliases if k not in self._valid_providers
                )
                if unknown:
                    aliases = {
                        k: v for k, v in aliases.items()
                        if k in self._valid_providers
                    }
                    if not aliases:
                        log.warning(
                            "model-map row %r names only unknown "
                            "provider(s) %s, skipping",
                            model, ", ".join(unknown),
                        )
                        continue
                    log.warning(
                        "model-map row %r dropped alias(es) for unknown "
                        "provider(s) %s",
                        model, ", ".join(unknown),
                    )
            self._routes[model] = aliases

    def get_model_map(self) -> ModelMap:
        """Return a frozen ModelMap snapshot for the pure routing function.

        Returns an empty (feature-off) ModelMap when nothing is mapped, so the
        proxy's ``routes`` truthiness check behaves as before.
        """
        if not self._routes:
            return ModelMap()
        return ModelMap(
            routes={m: dict(a) for m, a in self._routes.items()}
        )

    def list_models(self) -> list[tuple[str, dict[str, str]]]:
        """Return all ``(model, aliases)`` pairs, deep-copied."""
        return [(m, dict(a)) for m, a in self._routes.items()]

    def set_model(
        self, model: str, aliases: dict[str, str]
    ) -> None:
        """Add or replace a model's per-provider aliases.

        Persists to SQLite if configured.  ``aliases`` maps provider name →
        that provider's model string.

        DB first, memory second: if the write raises (disk full, locked,
        corrupt store), memory is untouched and the exception propagates so
        the caller never reports a save that a restart would revert.
        """
        stored = {str(k): str(v) for k, v in aliases.items() if str(v)}
        if not stored:
            raise ValueError("model map requires at least one non-empty alias")
        if self._db is not None:
            self._db.execute(
                "INSERT OR REPLACE INTO model_map (model, aliases) "
                "VALUES (?, ?)",
                (model, json.dumps(stored)),
            )
            self._db.commit()
        self._routes[model] = stored

    def remove_model(self, model: str) -> bool:
        """Remove a model entry. Returns True if found and removed.

        Same DB-first ordering as :meth:`set_model`: a failed delete raises
        with the in-memory entry still present, keeping memory and disk in
        agreement.
        """
        found = model in self._routes
        if found:
            if self._db is not None:
                self._db.execute(
                    "DELETE FROM model_map WHERE model = ?",
                    (model,),
                )
                self._db.commit()
            del self._routes[model]
        return found

    def load_from_config(
        self, config: dict[str, Any], *, overwrite: bool = False
    ) -> None:
        """Load model aliases from a parsed TOML config dict.

        Expects a ``[model]`` section whose keys are model names and whose
        values are tables of ``provider -> alias``::

            [model."umans-kimi-k2.7"]
            umans = "umans-kimi-k2.7"
            ollama-cloud = "kimi-k2.7-code"

        Provider-name validation is the caller's responsibility — the CLI's
        ``_validate_config`` rejects unknown providers at startup (surfacing
        them loudly rather than dropping them silently), and the admin write
        handler validates against live providers.  This method stores what it
        is given, mirroring :meth:`RouteTableManager.load_from_config`.

        When ``overwrite`` is False (the default), config entries seed only
        absent models — persisted runtime entries are preserved (WI-006.7
        shape).  When True, config entries override persisted entries.
        """
        model_section = config.get("model", {})
        if not isinstance(model_section, dict):
            return

        for model_name, provider_map in model_section.items():
            if not isinstance(provider_map, dict):
                continue
            entry: dict[str, str] = {}
            for pn, alias in provider_map.items():
                if isinstance(alias, str) and alias:
                    entry[str(pn)] = alias
            if not entry:
                continue
            if not overwrite and model_name in self._routes:
                continue
            self.set_model(model_name, entry)
