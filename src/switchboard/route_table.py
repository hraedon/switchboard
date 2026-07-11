"""Route table manager — CRUD + optional SQLite persistence."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from switchboard.control import RouteEntry, RouteTable


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
        self._sqlite_path: str | None = sqlite_path
        self._db: sqlite3.Connection | None = None

        if sqlite_path is not None:
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(
                sqlite_path,
                check_same_thread=False,
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS routes "
                "(key TEXT PRIMARY KEY, providers TEXT, created_at REAL)"
            )
            self._db.commit()
            self._load_from_db()

    def _load_from_db(self) -> None:
        db = self._db
        if db is None:
            return
        cursor = db.execute("SELECT key, providers FROM routes")
        for key, providers_json in cursor:
            providers_list: list[str] = json.loads(providers_json)
            self._entries[key] = RouteEntry(
                key=key,
                providers=tuple(providers_list),
            )

    def lookup(self, hashed_key: str) -> tuple[str, ...]:
        """Return ordered provider list for a hashed key, or default."""
        entry = self._entries.get(hashed_key)
        if entry is not None:
            return entry.providers
        return self._default_providers

    def add_entry(
        self,
        hashed_key: str,
        providers: list[str] | tuple[str, ...],
    ) -> None:
        """Add or update a route entry. Persists to SQLite if configured."""
        provider_tuple = tuple(providers)
        self._entries[hashed_key] = RouteEntry(
            key=hashed_key,
            providers=provider_tuple,
        )
        if self._db is not None:
            self._db.execute(
                "INSERT OR REPLACE INTO routes (key, providers, created_at) "
                "VALUES (?, ?, ?)",
                (hashed_key, json.dumps(list(provider_tuple)), time.time()),
            )
            self._db.commit()

    def remove_entry(self, hashed_key: str) -> bool:
        """Remove a route entry. Returns True if found and removed."""
        found = hashed_key in self._entries
        if found:
            del self._entries[hashed_key]
            if self._db is not None:
                self._db.execute(
                    "DELETE FROM routes WHERE key = ?",
                    (hashed_key,),
                )
                self._db.commit()
        return found

    def list_entries(self) -> list[RouteEntry]:
        """Return all route entries."""
        return list(self._entries.values())

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
        """
        route_section = config.get("route", {})
        if not isinstance(route_section, dict):
            return

        default = route_section.get("default", {})
        if isinstance(default, dict):
            providers = default.get("providers")
            if isinstance(providers, list):
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
