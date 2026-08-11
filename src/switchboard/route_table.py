"""Route table manager — CRUD + optional SQLite persistence."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from switchboard.control import RouteEntry, RouteTable

log = logging.getLogger("switchboard.route_table")


class RouteTableManager:
    """In-memory route table with CRUD + optional SQLite persistence."""

    def __init__(
        self,
        *,
        default_providers: tuple[str, ...] = (),
        sqlite_path: str | None = None,
    ) -> None:
        self._entries: dict[str, RouteEntry] = {}
        self._default_providers: tuple[str, ...] = default_providers
        #: True when :attr:`_default_providers` came from the store rather
        #: than from TOML or the constructor. Drives the D1 precedence rule
        #: in :meth:`load_from_config`.
        self._default_from_store: bool = False
        self._sqlite_path: str | None = sqlite_path
        self._db: sqlite3.Connection | None = None

        if sqlite_path is not None:
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(
                sqlite_path,
                check_same_thread=False,
            )
            # The file is shared with ConfigStoreManager and may hold
            # credentials (provider api_key in stored mode). Tighten perms
            # before any row is written; some volumes (k8s) forbid chmod,
            # so degrade quietly rather than fail boot.
            try:
                os.chmod(sqlite_path, 0o600)
            except OSError:
                log.debug(
                    "route table: could not chmod %s to 0600", sqlite_path
                )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS routes "
                "(key TEXT PRIMARY KEY, providers TEXT, created_at REAL)"
            )
            # The default route is a single row in its own table rather than a
            # sentinel key in `routes`: `routes` is keyed by hashed API key and
            # its rows are enumerated by list_entries(), so a sentinel would
            # surface in the GUI route list as a phantom keyed route.
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS route_default "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), "
                "providers TEXT, updated_at REAL)"
            )
            self._db.commit()
            self._load_from_db()

    def _load_from_db(self) -> None:
        db = self._db
        if db is None:
            return
        cursor = db.execute("SELECT key, providers FROM routes")
        for key, providers_json in cursor:
            try:
                providers_list = json.loads(providers_json)
            except (TypeError, json.JSONDecodeError):
                log.warning(
                    "route table: keyed row %s is not valid JSON; "
                    "ignoring it", key,
                )
                continue
            if not isinstance(providers_list, list) or not all(
                isinstance(p, str) for p in providers_list
            ):
                log.warning(
                    "route table: keyed row %s is not a list of strings; "
                    "ignoring it", key,
                )
                continue
            self._entries[key] = RouteEntry(
                key=key,
                providers=tuple(providers_list),
            )

        row = db.execute(
            "SELECT providers FROM route_default WHERE id = 1"
        ).fetchone()
        if row is None:
            return
        try:
            stored_default = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            # A corrupt row must not brick boot — the TOML/constructor default
            # stands and the operator can rewrite it through the API.
            log.warning(
                "route table: default route row is not valid JSON; "
                "ignoring it and keeping the config-declared default"
            )
            return
        if not isinstance(stored_default, list) or not all(
            isinstance(p, str) for p in stored_default
        ):
            log.warning(
                "route table: default route row is not a list of strings; "
                "ignoring it and keeping the config-declared default"
            )
            return
        if not stored_default:
            # An empty persisted default would route nothing anywhere. Treat
            # it as absent rather than as an instruction to black-hole traffic.
            log.warning(
                "route table: persisted default route is empty; "
                "keeping the config-declared default"
            )
            return
        self._default_providers = tuple(stored_default)
        self._default_from_store = True

    def lookup(self, hashed_key: str) -> tuple[str, ...]:
        """Return ordered provider list for a hashed key, or default."""
        entry = self._entries.get(hashed_key)
        if entry is not None:
            return entry.providers
        return self._default_providers

    def get_entry(self, hashed_key: str) -> tuple[str, ...] | None:
        """Return the providers for a keyed entry, or None on no keyed match.

        Unlike :meth:`lookup` (which returns the default route on a miss),
        this distinguishes a keyed-route hit from the default fallback. HMAC
        key rotation (Plan 008 §3) needs that distinction: the proxy tries
        several hash candidates (new secret, then previous) and must know
        whether each actually hit a keyed entry before falling back to the
        default route.
        """
        entry = self._entries.get(hashed_key)
        if entry is not None:
            return entry.providers
        return None

    def add_entry(
        self,
        hashed_key: str,
        providers: list[str] | tuple[str, ...],
    ) -> None:
        """Add or update a route entry. Persists to SQLite if configured.

        DB-first: the write commits before memory is touched, so a store
        failure leaves the live route table unchanged (the caller surfaces
        the error) rather than diverged from disk.
        """
        provider_tuple = tuple(providers)
        if self._db is not None:
            self._db.execute(
                "INSERT OR REPLACE INTO routes (key, providers, created_at) "
                "VALUES (?, ?, ?)",
                (hashed_key, json.dumps(list(provider_tuple)), time.time()),
            )
            self._db.commit()
        self._entries[hashed_key] = RouteEntry(
            key=hashed_key,
            providers=provider_tuple,
        )

    def remove_entry(self, hashed_key: str) -> bool:
        """Remove a route entry. Returns True if found and removed.

        DB-first: the delete commits before memory is touched."""
        found = hashed_key in self._entries
        if found:
            if self._db is not None:
                self._db.execute(
                    "DELETE FROM routes WHERE key = ?",
                    (hashed_key,),
                )
                self._db.commit()
            del self._entries[hashed_key]
        return found

    def list_entries(self) -> list[RouteEntry]:
        """Return all route entries."""
        return list(self._entries.values())

    @property
    def db(self) -> sqlite3.Connection | None:
        """The SQLite connection, if persistence is configured."""
        return self._db

    @property
    def default_providers(self) -> tuple[str, ...]:
        """The default provider list for unregistered route keys."""
        return self._default_providers

    @property
    def default_from_store(self) -> bool:
        """True when the live default route was loaded from the store.

        Lets the boot merge tell an operator-set default (which must survive
        a restart) apart from one derived from TOML or from the all-providers
        fallback.
        """
        return self._default_from_store

    def set_default_providers(
        self, providers: tuple[str, ...], *, persist: bool = False
    ) -> None:
        """Replace the default provider list.

        Without ``persist`` this is an in-memory change only. That is the
        boot-merge path (Plan 020 WI-4): the config store shares this
        manager's SQLite connection, so the table must be constructed before
        the effective provider set — the fallback default — is known, and the
        derived values computed there (the all-providers fallback, the
        tombstone filter) are *conclusions about this boot*, not operator
        intent. Persisting them would freeze a transient condition: re-enable
        a tombstoned provider and the filtered default would still be on disk.

        ``persist=True`` is the operator path (Plan 020 WI-8) — a default set
        through the admin API is intent, and must survive a restart. It then
        takes precedence over the TOML default, per D1's store-wins rule.
        """
        self._default_providers = providers
        if not persist:
            # The flag tracks the provenance of the CURRENT value, so an
            # in-memory replacement clears it: what is live is now a derived
            # value, not the row on disk. Only `load_from_config` reads it and
            # only before this point in the boot merge, so this is hygiene
            # rather than a fix — but a flag that outlives the value it
            # describes is exactly the kind of thing a later caller trusts.
            self._default_from_store = False
            return
        if self._db is not None:
            self._db.execute(
                "INSERT OR REPLACE INTO route_default "
                "(id, providers, updated_at) VALUES (1, ?, ?)",
                (json.dumps(list(providers)), time.time()),
            )
            self._db.commit()
        self._default_from_store = True

    def get_route_table(self) -> RouteTable:
        """Return a frozen RouteTable snapshot for the pure routing function."""
        return RouteTable(
            entries=dict(self._entries),
            default_providers=self._default_providers,
        )

    def load_from_config(
        self, config: dict[str, Any], *, overwrite: bool = False
    ) -> None:
        """Load route entries from a parsed TOML config dict.

        Expects [route.default] and optional [route.<key>] sections:

        [route.default]
        providers = ["umans", "ollama"]

        [route.key_abc123]
        providers = ["umans", "ollama"]

        When ``overwrite`` is False (the default), file entries seed only
        absent keys — persisted runtime entries are preserved (WI-006.7).
        When True, file entries override persisted entries.

        The default route follows the same rule (Plan 020 WI-8): a default
        persisted by an operator through the admin API outranks the TOML
        default, so that editing it in the GUI is not silently undone by the
        next restart. ``overwrite=True`` — which the boot merge passes when
        there is no store at all — restores TOML-wins.
        """
        route_section = config.get("route", {})
        if not isinstance(route_section, dict):
            return

        default = route_section.get("default", {})
        if isinstance(default, dict):
            providers = default.get("providers")
            if isinstance(providers, list) and (
                overwrite or not self._default_from_store
            ):
                self._default_providers = tuple(providers)

        for section_name, section_data in route_section.items():
            if section_name == "default":
                continue
            if not isinstance(section_data, dict):
                continue
            providers = section_data.get("providers")
            if isinstance(providers, list):
                if not overwrite and section_name in self._entries:
                    continue
                self.add_entry(section_name, tuple(providers))

    def close(self) -> None:
        """Close SQLite connection if open."""
        if self._db is not None:
            self._db.close()
            self._db = None
