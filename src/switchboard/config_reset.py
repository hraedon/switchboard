"""Reclaim configuration state from the store (Plan 021 D7).

The store outranks the mounted TOML for providers (Plan 020 D1), model-map
entries and route entries (both seed-only when a store is configured), and
the default route (Plan 020 WI-8a). That is deliberate — it is what makes a
GUI edit survive a restart. The cost is that a bad GUI edit is **not** fixable
by editing the configmap and rolling the pod: the store wins again on the next
boot, and it lives on a PVC that outlives the pod.

This is not hypothetical. On the live deployment (2026-08-08) the store's
model map excluded ``opencode-go`` for ``glm-5.2`` while ``configmap.yaml``
included it, so every ``glm-5.2`` request routed away from the primary
provider. With no admin token set, neither the configmap nor the API could
correct it — the only remaining route in was hand-editing SQLite on the PVC.

Env overrides (:mod:`switchboard.env_config`) cover fields that *have* an env
var. Reset is the blunt instrument for everything else: delete the store rows
for a section so the declared configuration becomes authoritative again.

Two entry points, because the failure mode includes "the API is unreachable":

* ``POST /admin/config/reset`` at runtime.
* ``SWITCHBOARD_CONFIG_RESET`` at boot, applied before the merge.

Both name every row they delete in the log. A reset that quietly discards
operator state would be its own incident.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("switchboard.config_reset")

#: section -> (table, human description). Every store-backed surface that can
#: outrank declared configuration appears here; anything that cannot is
#: deliberately absent (history and usage rings are observations, not config,
#: and clearing them would destroy evidence rather than reclaim control).
SECTIONS: dict[str, tuple[str, str]] = {
    "providers": ("provider_config", "GUI-managed provider rows"),
    "model-map": ("model_map", "model alias rows"),
    "routes": ("routes", "keyed route entries"),
    "route-default": ("route_default", "the persisted default route"),
    "routing-config": ("routing_config", "runtime routing overlay"),
}

ALL = "all"


class ResetError(ValueError):
    """An unknown section name. Named sections only — never a wildcard typo."""


def parse_sections(raw: str) -> list[str]:
    """Parse a comma-separated section list, or ``all``.

    Raises :class:`ResetError` naming the valid sections. A typo must not
    silently reset nothing (the operator would conclude the store was already
    clean) nor everything (unrecoverable).
    """
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    if not requested:
        raise ResetError("no sections given")
    if ALL in requested:
        if len(requested) > 1:
            raise ResetError(f"{ALL!r} cannot be combined with other sections")
        return sorted(SECTIONS)
    unknown = [s for s in requested if s not in SECTIONS]
    if unknown:
        raise ResetError(
            f"unknown section(s): {', '.join(unknown)}; "
            f"valid sections are {', '.join(sorted(SECTIONS))} or {ALL!r}"
        )
    # Preserve caller order but drop duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for s in requested:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


def _row_labels(db: sqlite3.Connection, table: str) -> list[str]:
    """Best-effort human labels for the rows about to be deleted."""
    try:
        if table == "route_default":
            row = db.execute("SELECT providers FROM route_default").fetchone()
            return [str(row[0])] if row else []
        if table == "routing_config":
            row = db.execute("SELECT overlay FROM routing_config").fetchone()
            return [str(row[0])] if row else []
        key = "key" if table == "routes" else (
            "model" if table == "model_map" else "name"
        )
        return [str(r[0]) for r in db.execute(f"SELECT {key} FROM {table}")]
    except sqlite3.OperationalError:
        return []


def reset_sections(
    db: sqlite3.Connection | None, sections: list[str]
) -> dict[str, list[str]]:
    """Delete the store rows for ``sections``. Returns section -> row labels.

    A missing table is not an error: the store is created lazily per feature,
    so a deployment that has never used the model map has no ``model_map``
    table, and "reset something that was never written" is a success.

    With no store configured (``db is None``) there is nothing to reclaim and
    every section reports empty — the caller should still report success, since
    the declared config is already authoritative in that mode.
    """
    deleted: dict[str, list[str]] = {}
    if db is None:
        return {s: [] for s in sections}

    for section in sections:
        table, description = SECTIONS[section]
        labels = _row_labels(db, table)
        try:
            db.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            deleted[section] = []
            continue
        deleted[section] = labels
        if labels:
            log.warning(
                "config reset: cleared %d %s (%s)",
                len(labels), description, ", ".join(labels),
            )
        else:
            log.info("config reset: %s already empty", description)
    db.commit()
    return deleted
