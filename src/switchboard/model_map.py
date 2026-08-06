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
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from switchboard.control import ModelMap


class ModelMapManager:
    """In-memory model map with CRUD + optional SQLite persistence."""

    def __init__(self, *, db: sqlite3.Connection | None = None) -> None:
        self._routes: dict[str, dict[str, str]] = {}
        self._db: sqlite3.Connection | None = db

        if db is not None:
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_map "
                "(model TEXT PRIMARY KEY, aliases TEXT)"
            )
            db.commit()
            self._load_from_db()

    def _load_from_db(self) -> None:
        db = self._db
        if db is None:
            return
        cursor = db.execute("SELECT model, aliases FROM model_map")
        for model, aliases_json in cursor:
            aliases: dict[str, str] = json.loads(aliases_json)
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
        """
        stored = {str(k): str(v) for k, v in aliases.items()}
        self._routes[model] = stored
        if self._db is not None:
            self._db.execute(
                "INSERT OR REPLACE INTO model_map (model, aliases) "
                "VALUES (?, ?)",
                (model, json.dumps(stored)),
            )
            self._db.commit()

    def remove_model(self, model: str) -> bool:
        """Remove a model entry. Returns True if found and removed."""
        found = model in self._routes
        if found:
            del self._routes[model]
            if self._db is not None:
                self._db.execute(
                    "DELETE FROM model_map WHERE model = ?",
                    (model,),
                )
                self._db.commit()
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
                if isinstance(alias, str):
                    entry[str(pn)] = alias
            if not entry:
                continue
            if not overwrite and model_name in self._routes:
                continue
            self.set_model(model_name, entry)
