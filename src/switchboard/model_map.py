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

#: Columns that postdate the table's first ship, as
#: ``{column: SQLite type}``.  Applied by :meth:`ModelMapManager._migrate_columns`
#: on every open — ``CREATE TABLE IF NOT EXISTS`` never alters an existing
#: table, so a pre-026 store file needs this to gain ``preference``.
_MIGRATION_COLUMNS: dict[str, str] = {"preference": "TEXT"}


class ModelMapManager:
    """In-memory model map with CRUD + optional SQLite persistence."""

    def __init__(
        self,
        *,
        db: sqlite3.Connection | None = None,
        valid_providers: frozenset[str] | None = None,
    ) -> None:
        self._routes: dict[str, dict[str, str]] = {}
        self._preferences: dict[str, tuple[str, ...]] = {}
        self._db: sqlite3.Connection | None = db
        self._valid_providers = valid_providers

        if db is not None:
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_map "
                "(model TEXT PRIMARY KEY, aliases TEXT, preference TEXT)"
            )
            self._migrate_columns()
            db.commit()
            self._load_from_db()

    def _migrate_columns(self) -> None:
        """Add columns that postdate the table's first ship (Plan 026 W3.1).

        Same idempotent shape as
        :meth:`~switchboard.config_store.ConfigStoreManager._migrate_columns`:
        PRAGMA table_info says what exists, each missing column is ADDed as
        nullable TEXT (which SQLite applies without rewriting rows), and a
        second boot is a no-op.  Both live stores predate Plan 026, so this runs
        for real exactly once per store file.
        """
        db = self._db
        if db is None:
            return
        present = {r[1] for r in db.execute("PRAGMA table_info(model_map)")}
        for column, ctype in _MIGRATION_COLUMNS.items():
            if column not in present:
                db.execute(
                    f"ALTER TABLE model_map ADD COLUMN {column} {ctype}"
                )
                log.info(
                    "model map: migrated model_map, added column %s", column
                )

    def _coerce_preference(
        self, model: str, raw: Any, aliases: dict[str, str]
    ) -> tuple[str, ...]:
        """Interpret a stored preference value, distrusting the store.

        The DB is operator-writable state that outlives config changes, and a
        preference is only meaningful relative to the aliases that survived
        loading, so every disagreement resolves to "no preference" (plus a
        warning) rather than to a crash or to a routing rule nobody wrote:

        - NULL / empty → no preference (the common case: a migrated row).
        - Not a JSON array of non-empty strings, or with duplicates → dropped
          whole, because a half-understood order is not an order.
        - Names without an alias for this model (a provider dropped by
          ``valid_providers``, or an alias removed by an older build) are
          dropped individually; an empty remainder is no preference.
        """
        if raw is None or raw == "":
            return ()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning(
                "model-map row %r has corrupt preference JSON, ignoring it: %s",
                model, exc,
            )
            return ()
        if not isinstance(parsed, list) or not all(
            isinstance(p, str) and p for p in parsed
        ):
            log.warning(
                "model-map row %r preference is not a list of provider names, "
                "ignoring it", model,
            )
            return ()
        if len(set(parsed)) != len(parsed):
            log.warning(
                "model-map row %r preference repeats a provider, ignoring it",
                model,
            )
            return ()
        kept = tuple(p for p in parsed if p in aliases)
        if len(kept) != len(parsed):
            dropped = sorted(set(parsed) - set(kept))
            log.warning(
                "model-map row %r preference names provider(s) %s with no "
                "alias for the model, dropping them",
                model, ", ".join(dropped),
            )
        return kept

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
        - A malformed ``preference`` (Plan 026 W3.1) costs the row nothing: it
          is treated as unset, with a warning (see :meth:`_coerce_preference`).
        """
        db = self._db
        if db is None:
            return
        cursor = db.execute("SELECT model, aliases, preference FROM model_map")
        for model, aliases_json, preference_json in cursor:
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
            preference = self._coerce_preference(
                model, preference_json, aliases
            )
            if preference:
                self._preferences[model] = preference

    def get_model_map(self) -> ModelMap:
        """Return a frozen ModelMap snapshot for the pure routing function.

        Returns an empty (feature-off) ModelMap when nothing is mapped, so the
        proxy's ``routes`` truthiness check behaves as before.
        """
        if not self._routes:
            return ModelMap()
        return ModelMap(
            routes={m: dict(a) for m, a in self._routes.items()},
            preferences=dict(self._preferences),
        )

    def list_models(self) -> list[tuple[str, dict[str, str]]]:
        """Return all ``(model, aliases)`` pairs, deep-copied.

        Deliberately still a 2-tuple: ``/status.json`` and ``GET
        /admin/model-map`` both build their payloads from it, and preference is
        an additive surface (see :meth:`preference_for` /
        :meth:`list_preferences`) rather than a shape change to those.
        """
        return [(m, dict(a)) for m, a in self._routes.items()]

    def preference_for(self, model: str) -> tuple[str, ...]:
        """The stored provider preference for ``model``; ``()`` when unset."""
        return self._preferences.get(model, ())

    def list_preferences(self) -> dict[str, list[str]]:
        """Every model that has a preference → its ordered provider names."""
        return {m: list(p) for m, p in self._preferences.items()}

    def set_model(
        self,
        model: str,
        aliases: dict[str, str],
        preference: list[str] | None = None,
    ) -> None:
        """Add or replace a model's per-provider aliases (and preference).

        Persists to SQLite if configured.  ``aliases`` maps provider name →
        that provider's model string.

        ``preference`` (Plan 026 W3) is the operator's provider order for this
        model.  It is validated here, at the one write path every caller shares:

        - every named provider must hold an alias for this model.  A preference
          for an alias-less provider is dead config at best — the model map
          reorders, it never adds — so it is refused by name rather than stored
          and silently ignored at ranking time.
        - a repeated name is refused: two positions for one provider is not an
          order, it is a typo.
        - ``None`` and ``[]`` both mean "no preference", stored as SQL NULL.

        DB first, memory second: if the write raises (disk full, locked,
        corrupt store), memory is untouched and the exception propagates so
        the caller never reports a save that a restart would revert.
        """
        stored = {str(k): str(v) for k, v in aliases.items() if str(v)}
        if not stored:
            raise ValueError("model map requires at least one non-empty alias")
        pref: tuple[str, ...] = ()
        if preference:
            pref = tuple(str(p) for p in preference)
            if len(set(pref)) != len(pref):
                raise ValueError(
                    "preference repeats a provider; each may appear once"
                )
            missing = [p for p in pref if p not in stored]
            if missing:
                raise ValueError(
                    "preference names provider(s) with no alias for "
                    f"'{model}': {', '.join(missing)}"
                )
        if self._db is not None:
            self._db.execute(
                "INSERT OR REPLACE INTO model_map (model, aliases, preference) "
                "VALUES (?, ?, ?)",
                (
                    model,
                    json.dumps(stored),
                    json.dumps(list(pref)) if pref else None,
                ),
            )
            self._db.commit()
        self._routes[model] = stored
        if pref:
            self._preferences[model] = pref
        else:
            self._preferences.pop(model, None)

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
            self._preferences.pop(model, None)
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

        TOML carries aliases only: Plan 026 W3.1 keeps preference a store/GUI
        concept, so a config overwrite must not silently discard an operator's
        preference.  Any part of it that still holds an alias under the new
        entry is carried across (and a part that no longer does could not be
        honoured anyway).
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
            carried = [
                p for p in self.preference_for(model_name) if p in entry
            ]
            self.set_model(model_name, entry, carried or None)
