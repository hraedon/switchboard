"""Tests for the persisted capability-observations store (Plan 027 W3)."""

from __future__ import annotations

import json
import sqlite3

from switchboard.capability_store import CapabilityStore
from switchboard.model_capabilities import ModelObservation, TriState


def _obs(
    provider: str = "p",
    alias: str = "m1",
    **kw,
) -> ModelObservation:
    base: dict = dict(provider=provider, alias=alias, observed_at=1000.0)
    base.update(kw)
    return ModelObservation(**base)


def test_upsert_and_lookup_in_memory() -> None:
    store = CapabilityStore()
    written = store.upsert("p", [_obs(), _obs(alias="m2", context_limit=5)])
    assert written == 2
    assert store.for_alias("p", "m1").context_limit is None
    assert store.for_alias("p", "m2").context_limit == 5
    assert store.for_alias("q", "m1") is None
    assert set(store.for_provider("p")) == {"m1", "m2"}
    assert len(store.all()) == 2


def test_upsert_empty_listing_is_refused() -> None:
    store = CapabilityStore()
    store.upsert("p", [_obs()])
    written = store.upsert("p", [])
    assert written == 0
    # Previous listing intact: storing "serves nothing" from a parse quirk
    # must not erase every contract for the provider.
    assert store.for_alias("p", "m1") is not None


def test_upsert_replaces_previous_listing() -> None:
    store = CapabilityStore()
    store.upsert("p", [_obs(alias="m1"), _obs(alias="m2")])
    store.upsert("p", [_obs(alias="m2", context_limit=9)])
    # m1 dropped from the new listing stops certifying.
    assert store.for_alias("p", "m1") is None
    assert store.for_alias("p", "m2").context_limit == 9
    assert set(store.for_provider("p")) == {"m2"}


def test_drop_provider() -> None:
    store = CapabilityStore()
    store.upsert("p", [_obs()])
    store.upsert("q", [_obs(provider="q")])
    assert store.drop_provider("p") == 1
    assert store.for_provider("p") == {}
    assert store.for_alias("q", "m1") is not None
    assert store.drop_provider("ghost") == 0


def test_sqlite_round_trip() -> None:
    db = sqlite3.connect(":memory:")
    store = CapabilityStore(db=db)
    store.upsert(
        "p",
        [
            _obs(
                context_limit=131072,
                input_modalities=("text", "image"),
                tool_calling=TriState.TRUE,
                reasoning=TriState.TRUE,
                reasoning_format="reasoning_effort",
                reasoning_levels=("low", "high"),
                config_fingerprint="fp-1",
            )
        ],
    )
    # A second store on the same connection sees the rows (DB-first).
    reloaded = CapabilityStore(db=db)
    obs = reloaded.for_alias("p", "m1")
    assert obs is not None
    assert obs.context_limit == 131072
    assert obs.input_modalities == ("text", "image")
    assert obs.tool_calling is TriState.TRUE
    assert obs.reasoning is TriState.TRUE
    assert obs.reasoning_format == "reasoning_effort"
    assert obs.reasoning_levels == ("low", "high")
    assert obs.observed_at == 1000.0
    assert obs.config_fingerprint == "fp-1"
    assert obs.source == "discovery"


def test_corrupt_row_is_skipped_not_fatal() -> None:
    db = sqlite3.connect(":memory:")
    store = CapabilityStore(db=db)
    store.upsert("p", [_obs(), _obs(alias="good", context_limit=7)])
    # Corrupt one row's payload directly.
    db.execute(
        "UPDATE model_capability_observations "
        "SET payload = 'not-json' WHERE alias = 'm1'",
    )
    db.commit()
    reloaded = CapabilityStore(db=db)
    assert reloaded.for_alias("p", "m1") is None
    assert reloaded.for_alias("p", "good").context_limit == 7


def test_distrusts_stored_field_types_on_load() -> None:
    db = sqlite3.connect(":memory:")
    store = CapabilityStore(db=db)
    store.upsert("p", [_obs(tool_calling=TriState.TRUE)])
    db.execute(
        "UPDATE model_capability_observations "
        "SET payload = ? WHERE alias = 'm1'",
        (
            json.dumps(
                {
                    "alias": "m1",
                    "context_limit": "-5",
                    "input_limit": True,
                    "input_modalities": "text",
                    "tool_calling": "maybe",
                    "observed_at": "not-a-number",
                }
            ),
        ),
    )
    db.commit()
    reloaded = CapabilityStore(db=db)
    obs = reloaded.for_alias("p", "m1")
    assert obs is not None
    assert obs.context_limit is None
    assert obs.input_limit is None
    assert obs.input_modalities == ()
    assert obs.tool_calling is TriState.UNKNOWN
    # observed_at fell back to the row's column (valid), never to a guess.
    assert obs.observed_at == 1000.0


def test_load_failure_leaves_store_usable() -> None:
    # A connection that rejects queries: the store must start empty and keep
    # working in memory (the boot never crashes on a foreign schema).
    class BrokenDB:
        def execute(self, *_a, **_kw):
            raise sqlite3.OperationalError("table locked")

        def commit(self):
            raise sqlite3.OperationalError("table locked")

    store = CapabilityStore(db=BrokenDB())  # type: ignore[arg-type]
    assert store.all() == []
