"""Pure-core tests for the model-capability contract engine (Plan 027 W2).

No I/O, no clock: every instant is an explicit argument.
"""

from __future__ import annotations

from switchboard.model_capabilities import (
    REASONING_FORMAT_EFFORT,
    REASONING_FORMAT_OBJECT,
    SOURCE_CAPPED,
    SOURCE_DECLARED,
    SOURCE_OBSERVED,
    SOURCE_UNKNOWN,
    SOURCE_VERIFIED,
    ModelCaps,
    ModelObservation,
    TriState,
    compute_contract,
    extract_listing_items,
    parse_model_listing,
    render_client_models,
    render_opencode_config,
    render_opencode_model,
)

NOW = 1_000_000.0
MAX_AGE = 86400.0


def obs(provider: str, alias: str = "m1", **kw) -> ModelObservation:
    base: dict = dict(provider=provider, alias=alias, observed_at=NOW)
    base.update(kw)
    return ModelObservation(**base)


def contract(
    model: str = "m1",
    participants: tuple[str, ...] = ("a", "b"),
    observations: dict | None = None,
    caps: ModelCaps | None = None,
    now: float = NOW,
    max_age: float = MAX_AGE,
    **kw,
):
    return compute_contract(
        model,
        participants,
        observations or {},
        caps,
        now=now,
        max_age=max_age,
        **kw,
    )


# ── Listing parsing ───────────────────────────────────────────────────────────


def test_extract_listing_items_shapes() -> None:
    assert extract_listing_items({"data": [1, 2]}) == [1, 2]
    assert extract_listing_items({"models": [3]}) == [3]
    assert extract_listing_items([4, 5]) == [4, 5]
    assert extract_listing_items({"other": [6]}) == []
    assert extract_listing_items("junk") == []
    assert extract_listing_items(None) == []


def test_parse_openai_base_shape_carries_only_id() -> None:
    out = parse_model_listing(
        {"data": [{"id": "m1"}]}, provider="p", observed_at=NOW
    )
    assert len(out) == 1
    o = out[0]
    assert o.alias == "m1"
    assert o.context_limit is None
    assert o.tool_calling is TriState.UNKNOWN
    assert o.reasoning is TriState.UNKNOWN
    assert o.reasoning_format is None
    assert o.reasoning_levels == ()


def test_parse_vllm_max_model_len() -> None:
    out = parse_model_listing(
        {"data": [{"id": "m1", "max_model_len": 131072}]},
        provider="p",
        observed_at=NOW,
    )
    assert out[0].context_limit == 131072
    assert out[0].input_limit is None


def test_parse_litellm_direction_limits() -> None:
    out = parse_model_listing(
        {
            "data": [
                {
                    "id": "m1",
                    "max_input_tokens": 120000,
                    "max_output_tokens": 16384,
                }
            ]
        },
        provider="p",
        observed_at=NOW,
    )
    assert out[0].input_limit == 120000
    assert out[0].output_limit == 16384
    assert out[0].context_limit is None


def test_parse_openrouter_rich_shape() -> None:
    out = parse_model_listing(
        {
            "data": [
                {
                    "id": "m1",
                    "top_provider": {
                        "context_length": 200000,
                        "max_completion_tokens": 8192,
                    },
                    "architecture": {
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                    },
                    "supported_parameters": [
                        "tools",
                        "tool_choice",
                        "reasoning_effort",
                        "reasoning",
                    ],
                    "reasoning_efforts": ["low", "high"],
                }
            ]
        },
        provider="p",
        observed_at=NOW,
    )
    o = out[0]
    assert o.context_limit == 200000
    assert o.output_limit == 8192
    assert o.input_modalities == ("text", "image")
    assert o.output_modalities == ("text",)
    assert o.tool_calling is TriState.TRUE
    assert o.reasoning is TriState.TRUE
    # "reasoning" alongside "reasoning_effort": the object form wins.
    assert o.reasoning_format == REASONING_FORMAT_OBJECT
    assert o.reasoning_levels == ("low", "high")


def test_reasoning_effort_only_means_effort_format() -> None:
    out = parse_model_listing(
        {
            "data": [
                {
                    "id": "m1",
                    "supported_parameters": ["tools", "reasoning_effort"],
                }
            ]
        },
        provider="p",
        observed_at=NOW,
    )
    assert out[0].reasoning_format == REASONING_FORMAT_EFFORT


def test_no_supported_parameters_keeps_tristates_unknown() -> None:
    out = parse_model_listing(
        {"data": [{"id": "m1", "max_output_tokens": 8}]},
        provider="p",
        observed_at=NOW,
    )
    assert out[0].tool_calling is TriState.UNKNOWN
    assert out[0].reasoning is TriState.UNKNOWN
    assert out[0].reasoning_format is None


def test_parse_skips_entries_without_id_and_deduplicates() -> None:
    out = parse_model_listing(
        {
            "data": [
                {"name": "no id"},
                {"id": ""},
                {"id": "m1", "max_model_len": 100},
                {"id": "m1", "max_model_len": 999},
                {"id": 42},
            ]
        },
        provider="p",
        observed_at=NOW,
    )
    assert [o.alias for o in out] == ["m1"]
    # First occurrence wins.
    assert out[0].context_limit == 100


def test_parse_bare_string_items() -> None:
    out = parse_model_listing(
        ["m1", "m2"], provider="p", observed_at=NOW
    )
    assert [o.alias for o in out] == ["m1", "m2"]
    assert out[0].context_limit is None


# ── LCD aggregation ───────────────────────────────────────────────────────────


def test_all_fresh_all_true_gives_verified() -> None:
    c = contract(
        observations={
            "a": obs("a", tool_calling=TriState.TRUE),
            "b": obs("b", tool_calling=TriState.TRUE),
        }
    )
    fv = c.field("tool_calling")
    assert fv.value is True
    assert fv.source == SOURCE_VERIFIED
    assert c.participants_verified is True
    assert c.guaranteed() is True


def test_one_false_wins_regardless_of_true() -> None:
    c = contract(
        observations={
            "a": obs("a", tool_calling=TriState.TRUE),
            "b": obs("b", tool_calling=TriState.FALSE),
        }
    )
    fv = c.field("tool_calling")
    assert fv.value is False
    assert fv.source == SOURCE_VERIFIED


def test_true_plus_unknown_is_unknown_not_true() -> None:
    c = contract(
        observations={
            "a": obs("a", tool_calling=TriState.TRUE),
            "b": obs("b"),
        }
    )
    fv = c.field("tool_calling")
    assert fv.value is None
    assert fv.source == SOURCE_UNKNOWN


def test_stale_participant_demotes_numeric_to_observed() -> None:
    # Stale evidence contributes no value at all: the aggregate covers the
    # fresh set only, and its source demotes to observed because not every
    # participant certified.
    c = contract(
        observations={
            "a": obs("a", context_limit=131072),
            "b": obs("b", context_limit=65536, observed_at=NOW - MAX_AGE - 1),
        }
    )
    fv = c.field("context_limit")
    assert fv.value == 131072
    assert fv.source == SOURCE_OBSERVED
    assert c.participants_verified is False
    assert c.guaranteed() is False
    by_provider = {p.provider: p for p in c.participants}
    assert by_provider["a"].state == "fresh"
    assert by_provider["b"].state == "stale"


def test_fingerprint_mismatch_makes_fresh_evidence_stale() -> None:
    c = contract(
        observations={
            "a": obs("a", context_limit=131072, config_fingerprint="f1"),
            "b": obs("b", context_limit=131072, config_fingerprint="f1"),
        },
        current_fingerprints={"a": "f1", "b": "f2"},
    )
    by_provider = {p.provider: p for p in c.participants}
    assert by_provider["a"].fingerprint_current is True
    assert by_provider["b"].fingerprint_current is False
    assert by_provider["b"].state == "stale"
    fv = c.field("context_limit")
    assert fv.value == 131072
    assert fv.source == SOURCE_OBSERVED


def test_missing_participant_counts_into_total() -> None:
    c = contract(
        observations={"a": obs("a", context_limit=131072)}
    )
    by_provider = {p.provider: p for p in c.participants}
    assert by_provider["b"].state == "missing"
    assert by_provider["b"].alias == ""
    fv = c.field("context_limit")
    assert fv.value == 131072
    assert fv.source == SOURCE_OBSERVED
    assert c.guaranteed() is False


def test_numeric_min_across_participants() -> None:
    c = contract(
        observations={
            "a": obs("a", output_limit=16384),
            "b": obs("b", output_limit=8192),
        }
    )
    assert c.field("output_limit").value == 8192


def test_modalities_intersect() -> None:
    c = contract(
        observations={
            "a": obs("a", input_modalities=("text", "image")),
            "b": obs("b", input_modalities=("text",)),
        }
    )
    fv = c.field("input_modalities")
    assert fv.value == ("text",)
    assert fv.source == SOURCE_VERIFIED


def test_reasoning_levels_intersect_only_when_format_agrees() -> None:
    c = contract(
        observations={
            "a": obs(
                "a",
                reasoning=TriState.TRUE,
                reasoning_format=REASONING_FORMAT_EFFORT,
                reasoning_levels=("low", "medium", "high"),
            ),
            "b": obs(
                "b",
                reasoning=TriState.TRUE,
                reasoning_format=REASONING_FORMAT_EFFORT,
                reasoning_levels=("medium", "high"),
            ),
        }
    )
    fv = c.field("reasoning_levels")
    assert fv.value == ("high", "medium")
    assert fv.source == SOURCE_VERIFIED
    assert c.reasoning_format == REASONING_FORMAT_EFFORT
    assert c.reasoning_divergent is False


def test_reasoning_format_divergence_advertises_no_levels() -> None:
    c = contract(
        observations={
            "a": obs(
                "a",
                reasoning=TriState.TRUE,
                reasoning_format=REASONING_FORMAT_EFFORT,
                reasoning_levels=("low", "high"),
            ),
            "b": obs(
                "b",
                reasoning=TriState.TRUE,
                reasoning_format=REASONING_FORMAT_OBJECT,
                reasoning_levels=("low", "high"),
            ),
        }
    )
    assert c.reasoning_divergent is True
    assert c.reasoning_format is None
    fv = c.field("reasoning_levels")
    assert fv.value is None
    assert fv.source == SOURCE_UNKNOWN
    # Boolean support is still guaranteed (everyone said true).
    assert c.field("reasoning").value is True


# ── Operator caps ─────────────────────────────────────────────────────────────


def test_cap_below_verified_minimum_is_capped() -> None:
    c = contract(
        observations={
            "a": obs("a", context_limit=131072),
            "b": obs("b", context_limit=131072),
        },
        caps=ModelCaps(context_limit=32768),
    )
    fv = c.field("context_limit")
    assert fv.value == 32768
    assert fv.source == SOURCE_CAPPED
    # No deviations for an honourable cap.
    assert c.deviations == ()


def test_cap_above_observed_is_clamped_with_deviation() -> None:
    c = contract(
        observations={
            "a": obs("a", context_limit=131072),
            "b": obs("b", context_limit=131072),
        },
        caps=ModelCaps(context_limit=999999),
    )
    fv = c.field("context_limit")
    assert fv.value == 131072
    assert fv.source == SOURCE_VERIFIED
    assert len(c.deviations) == 1
    d = c.deviations[0]
    assert d.kind == "cap_exceeds_observed"
    assert d.configured == 999999
    assert d.observed == 131072


def test_cap_fills_missing_field_as_declared() -> None:
    c = contract(
        observations={"a": obs("a"), "b": obs("b")},
        caps=ModelCaps(context_limit=131072),
    )
    fv = c.field("context_limit")
    assert fv.value == 131072
    assert fv.source == SOURCE_DECLARED
    assert c.guaranteed() is False


def test_declared_bool_cap_fails_safe_against_observed_false() -> None:
    c = contract(
        observations={
            "a": obs("a", tool_calling=TriState.FALSE),
            "b": obs("b", tool_calling=TriState.TRUE),
        },
        caps=ModelCaps(tool_calling=True),
    )
    fv = c.field("tool_calling")
    assert fv.value is False
    kinds = [d.kind for d in c.deviations]
    assert "cap_conflicts_with_observed" in kinds


def test_false_cap_restricts_verified_true() -> None:
    c = contract(
        observations={
            "a": obs("a", reasoning=TriState.TRUE),
            "b": obs("b", reasoning=TriState.TRUE),
        },
        caps=ModelCaps(reasoning=False),
    )
    fv = c.field("reasoning")
    assert fv.value is False
    assert fv.source == SOURCE_CAPPED
    assert c.reasoning_format is None


def test_reasoning_levels_cap_narrows_never_widens() -> None:
    c = contract(
        observations={
            "a": obs(
                "a",
                reasoning=TriState.TRUE,
                reasoning_format=REASONING_FORMAT_EFFORT,
                reasoning_levels=("low", "medium", "high"),
            ),
        },
        participants=("a",),
        caps=ModelCaps(reasoning_levels=("low", "medium")),
    )
    fv = c.field("reasoning_levels")
    assert fv.value == ("low", "medium")
    # The result equals the declared set, which the verified intersection
    # already covers: still an evidenced guarantee, not a cap artifact.
    assert fv.source == SOURCE_VERIFIED


def test_reasoning_levels_cap_with_unobserved_level_is_capped() -> None:
    c = contract(
        observations={
            "a": obs(
                "a",
                reasoning=TriState.TRUE,
                reasoning_format=REASONING_FORMAT_EFFORT,
                reasoning_levels=("low", "high"),
            ),
        },
        participants=("a",),
        caps=ModelCaps(reasoning_levels=("low", "medium")),
    )
    fv = c.field("reasoning_levels")
    assert fv.value == ("low",)
    assert fv.source == SOURCE_CAPPED


def test_reasoning_levels_cap_disjoint_advertises_nothing() -> None:
    c = contract(
        observations={
            "a": obs(
                "a",
                reasoning=TriState.TRUE,
                reasoning_format=REASONING_FORMAT_EFFORT,
                reasoning_levels=("low", "high"),
            ),
        },
        participants=("a",),
        caps=ModelCaps(reasoning_levels=("turbo",)),
    )
    fv = c.field("reasoning_levels")
    assert fv.value is None
    kinds = [d.kind for d in c.deviations]
    assert "cap_disjoint_from_observed" in kinds


def test_declared_levels_and_format_without_evidence() -> None:
    c = contract(
        observations={
            "a": obs("a", tool_calling=TriState.TRUE),
            "b": obs("b", tool_calling=TriState.TRUE),
        },
        caps=ModelCaps(
            reasoning=True,
            reasoning_format=REASONING_FORMAT_EFFORT,
            reasoning_levels=("low", "high"),
        ),
    )
    assert c.field("reasoning").value is True
    assert c.field("reasoning").source == SOURCE_DECLARED
    assert c.field("reasoning_levels").value == ("high", "low")
    assert c.field("reasoning_levels").source == SOURCE_DECLARED
    assert c.reasoning_format == REASONING_FORMAT_EFFORT
    assert c.guaranteed() is False


def test_modality_cap_disjoint_yields_empty_set_with_deviation() -> None:
    c = contract(
        observations={
            "a": obs("a", input_modalities=("text", "image")),
            "b": obs("b", input_modalities=("text",)),
        },
        caps=ModelCaps(input_modalities=("audio",)),
    )
    fv = c.field("input_modalities")
    assert fv.value == ()
    kinds = [d.kind for d in c.deviations]
    assert "cap_disjoint_from_observed" in kinds


# ── Client rendering ──────────────────────────────────────────────────────────


def test_publishable_includes_only_verified_or_capped() -> None:
    c = contract(
        observations={
            "a": obs("a", output_limit=8192),
            "b": obs("b", output_limit=8192),
        }
    )
    pub = c.publishable()
    assert pub == {"output_limit": 8192}


def test_publishable_translates_tristates_and_tuples() -> None:
    c = contract(
        observations={
            "a": obs(
                "a",
                tool_calling=TriState.TRUE,
                input_modalities=("text", "image"),
            ),
            "b": obs(
                "b",
                tool_calling=TriState.TRUE,
                input_modalities=("text", "image"),
            ),
        }
    )
    pub = c.publishable()
    assert pub["tool_calling"] is True
    # Sets render deterministically sorted.
    assert pub["input_modalities"] == ["image", "text"]


def test_render_client_models_deterministic_and_leak_free() -> None:
    c1 = contract(
        model="zz",
        participants=("a",),
        observations={"a": obs("a", "zz-alias", output_limit=16)},
    )
    c2 = contract(
        model="aa",
        participants=("a",),
        observations={"a": obs("a", "aa-alias")},
    )
    body = render_client_models((c1, c2))
    assert body["object"] == "list"
    ids = [d["id"] for d in body["data"]]
    assert ids == ["aa", "zz"]
    for d in body["data"]:
        assert d["object"] == "model"
        assert d["created"] == 0
        assert d["owned_by"] == "switchboard"
        for key in d:
            assert key != "provider"
    text = str(body)
    assert "a-alias" not in text


def test_render_client_models_omits_x_switchboard_without_evidence() -> None:
    c = contract(
        participants=("a",),
        observations={"a": obs("a")},
    )
    body = render_client_models((c,))
    assert "x-switchboard" not in body["data"][0]


# ── OpenCode adapter ──────────────────────────────────────────────────────────


def test_opencode_model_writes_evidenced_fields_only() -> None:
    c = contract(
        observations={
            "a": obs(
                "a",
                context_limit=131072,
                input_limit=120000,
                output_limit=16384,
                input_modalities=("text", "image"),
                output_modalities=("text",),
                tool_calling=TriState.TRUE,
            ),
            "b": obs(
                "b",
                context_limit=131072,
                input_limit=120000,
                output_limit=16384,
                input_modalities=("text", "image"),
                output_modalities=("text",),
                tool_calling=TriState.TRUE,
            ),
        }
    )
    entry: dict = {"name": "m1"}
    render_opencode_model(entry, c)
    assert entry["limit"] == {
        "context": 131072,
        "input": 120000,
        "output": 16384,
    }
    assert entry["input"] == ["image", "text"]
    assert entry["attachment"] is True
    assert entry["output"] == ["text"]
    assert entry["tool_call"] is True
    # Reasoning has no evidence: it must not be invented.
    assert "reasoning" not in entry


def test_opencode_reasoning_needs_verified_levels_and_format() -> None:
    entry: dict = {"name": "m1"}
    render_opencode_model(entry, contract(
        observations={
            "a": obs(
                "a",
                reasoning=TriState.TRUE,
                reasoning_format=REASONING_FORMAT_EFFORT,
                reasoning_levels=("low", "high"),
            ),
        },
        participants=("a",),
    ))
    assert entry["reasoning"] is True
    assert entry["variants"] == {
        "low": {"options": {"reasoningEffort": "low"}},
        "high": {"options": {"reasoningEffort": "high"}},
    }


def test_opencode_reasoning_object_format_wires_reasoning_object() -> None:
    entry: dict = {"name": "m1"}
    render_opencode_model(entry, contract(
        observations={
            "a": obs(
                "a",
                reasoning=TriState.TRUE,
                reasoning_format=REASONING_FORMAT_OBJECT,
                reasoning_levels=("low",),
            ),
        },
        participants=("a",),
    ))
    assert entry["variants"] == {
        "low": {"options": {"reasoning": {"effort": "low"}}}
    }


def test_opencode_verifyed_support_without_levels_is_omitted() -> None:
    entry: dict = {"name": "m1"}
    render_opencode_model(entry, contract(
        observations={
            "a": obs(
                "a",
                reasoning=TriState.TRUE,
                reasoning_format=REASONING_FORMAT_EFFORT,
            ),
        },
        participants=("a",),
    ))
    assert "reasoning" not in entry


def test_opencode_declared_values_are_not_written() -> None:
    entry: dict = {"name": "m1"}
    render_opencode_model(entry, contract(
        observations={"a": obs("a")},
        participants=("a",),
        caps=ModelCaps(context_limit=131072),
    ))
    assert "limit" not in entry


def test_render_opencode_config_stable_shape() -> None:
    c = contract(
        model="mm",
        participants=("a",),
        observations={"a": obs("a", "mm-alias", output_limit=8192)},
    )
    cfg = render_opencode_config((c,), base_url="https://sb.example.com")
    prov = cfg["provider"]["switchboard"]
    assert prov["options"] == {
        "baseURL": "https://sb.example.com",
        "apiKey": "<your-switchboard-route-key>",
    }
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert "mm" in prov["models"]
    text = str(cfg)
    assert "mm-alias" not in text
    assert "sb-alias" not in text


def test_numeric_single_participant_is_observed() -> None:
    # One participant certifies it, but a contract over a bigger candidate
    # set must not promise it: the value is best-effort, source observed.
    c = contract(
        observations={"a": obs("a", context_limit=131072)}
    )
    assert c.field("context_limit").source == SOURCE_OBSERVED
    assert c.field("context_limit").value == 131072
    assert c.guaranteed() is False
