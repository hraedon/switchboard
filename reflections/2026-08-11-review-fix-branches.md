# Cross-lineage review (fix-branches) — opencode-go/deepseek-v4-flash, 2026-08-11

## VERDICT: request-changes

One fail-safe regression at the gate (advisory sources at boot with no last-known-good), several smaller gaps.

---

## BLOCKING

### B1. Advisory fail-open at boot: a first-fetch failure opens the gate to full capacity with zero evidence — `src/switchboard/reconcile.py:596-611`, via `src/switchboard/direct_usage.py:602-618` and `src/switchboard/dashboard.py:153-167`

The advisory exemption only consults the *reading's* data-driven zero:

```python
if not ok and not self._advisory:
    return 0
r = reading.reading
if r.requests_remaining is not None and r.requests_remaining <= 0:
    return 0
if r.tokens_remaining is not None and r.tokens_remaining <= 0:
    return 0
return self._max_concurrency
```

Both advisory sources' no-LKG failsafe reading (`_serve_lkg_or_failsafe`) is a **synthetic** `LimitState` with `requests_remaining=None`, `tokens_remaining=None` (`direct_usage.py` leaves them at the dataclass default; `dashboard.py:107` sets them `None` explicitly). So when the *very first* fetch fails — expired opencode-go cookie, wrong z.ai key, dashboard down at boot, Cloudflare blip during startup — neither zero check fires and `_compute_header_permits` returns `self._max_concurrency`, resizing the gate from 0 to **full capacity**. Pre-fix, `if not ok: return 0` kept the gate closed for exactly this case. That is a silent fail-closed → fail-open flip on the boot path, and it contradicts the function's own docstring ("a provider whose last real reading said 'exhausted' stays closed until a successful poll says otherwise" — at boot there *is* no last real reading; the reading is fabricated, so "last-known-good" is a fiction).

**Why it is not worse (and why the fix is one line):** the live blast radius is currently bounded. `ready` (`_first_poll_ok`) stays False, so `snapshot_provider_state` reports `UNKNOWN` and the routing core never selects the provider for immediate/queue/backstop; `_admit`'s second pass also guards `not ctx.reconcile.ready` (proxy.py:1936). The provider receives no traffic while unready. The residual harms: (a) the dashboard shows the provider with an *open* gate and full `effective_permits` for an upstream whose scrape has never succeeded (misleading to exactly the operator the dashboard exists for); (b) on the all-ineligible path, the terminal fallback's gate state drives the 503 — an open gate yields `reason="no_capacity"`/`RETRY_AFTER_SHORT` (proxy.py:1548-1558 falls through) instead of the honest saturation hint; (c) the invariant the commit's own tests assert elsewhere ("data-driven zero still applies") silently does not hold here.

**Fix:** the loop already knows whether any successful poll ever happened:

```python
if not ok and (not self._advisory or not self._first_poll_ok):
    return 0
```

`_first_poll_ok` is only True after a successful fetch, which is exactly when a real last-known-good exists. This matches the umans polled path (`_compute_polled_permits` → `min(self._last_permits, ...)` from an initial 0 gate — boot-closed by construction). **No test covers the no-LKG case** — every test in `tests/test_advisory_containment.py` establishes an LKG with a successful tick before failing.

---

## NON-BLOCKING

### N1. DEGRADED authoritative-source backstop turns a fast 503 into a slow one — `src/switchboard/control.py:1409-1410` (`_stage_queue_candidate` step 4, same logic in the 476c0cc-era code)

A DEGRADED non-primary is now a queue backstop regardless of *why* it is DEGRADED. For a header (authoritative) source, a failed fetch closes its gate to 0 — so the queue wait on the backstop is doomed by construction and burns the full `queue_timeout` before the 503 that pre-fix arrived at immediately. The commit message frames this as "a failed scrape no longer turns into a 503", which is true only for advisory sources; for authoritative sources it is "a fast 503 becomes a slow 503". If this is deliberate ("queueing on it beats the 503" applies only when the gate could open), a comment or an availability check (`ctx.reconcile.gate_closed_reason() != "saturated"`) before offering the backstop would keep the fail-safe latency honest. Worth a test either way: `test_degraded_fallback_is_last_resort_backstop` uses an AVAILABLE degraded provider, so the closed-gate backstop path is untested.

### N2. Edit-form "clearing an alias removes that provider" has no confirmation — `src/switchboard/static/dashboard.html:963-964, 1060-1099`

The semantics are correctly implemented (the save handler drops empty inputs at :1067-1068 and `POST /admin/model-map` replaces the whole alias set — `model_map.set_model`, admin.py:1378) and the note at :964 documents it. But a mis-click that clears one alias in a multi-alias model permanently deletes that provider's mapping with no undo and no confirm, while the sibling destructive actions in this file (`deleteModel` :1220, `releaseQuarantine` :1239) both `confirm()`. Suggest a confirm when the save shrinks the alias set, or an explicit per-alias remove button.

### N3. Guard tests that pass against pre-fix code — `tests/test_advisory_containment.py`, `tests/test_control.py`

Against pre-fix code these pass unchanged and would not catch a revert:
- `test_authoritative_fetch_failure_closes_gate` (unchanged behavior), `test_unknown_source_defaults_to_authoritative` (no advisory logic existed → same 0), `test_advisory_failure_respects_lkg_exhaustion` (0 either way — pre-fix via `not ok`, post-fix via the data-driven zero), `test_degraded_fallback_never_outranks_fresh_backstop`, `test_degraded_fallback_yields_to_fresh_busy_fallback`, `test_pace_post_dwell_conversation_pin_still_holds`.

They are legitimate contract guards, but the fix-detecting set is exactly five: `test_advisory_fetch_failure_keeps_gate_open`, `test_advisory_failure_with_recent_429_clamps_to_one`, `test_degraded_fallback_is_last_resort_backstop`, `test_pace_post_dwell_pin_goes_inert`, `test_pace_post_dwell_ignores_failback_hysteresis` (I traced each against the pre-fix branches — all fail pre-fix). The GUI tests are all fix-detecting (pre-fix there was no `mf-umans-list`, no `scanPinned`, no `edit-model-0`, no `added ✓` marking). Acceptable, but the "revert ⇒ red" claim should name only the five.

### N4. Commit-message overclaim, 476c0cc

"the gate is sized from the last-known-good reading … the data-driven zero … still apply" — true only once an LKG exists; at boot the reading is the synthetic failsafe and the gate opens wide (B1). The message should state the no-LKG boot policy explicitly rather than implying the LKG invariant always holds. c309609 and b380967 messages match their code.

### N5. `PolledTruthSource` carries no `advisory` attribute — `src/switchboard/truth.py:50`

Harmless today (the umans polled path never consults the flag, and `getattr` default False is the fail-safe choice — attack direction 1 confirmed: every source that reaches `_compute_header_permits` is correctly classified, and the default-false default is fail-closed for any future source). But the commit's own contract is "every TruthSource implementation carries the right value", and this one is the exception; add `advisory = False` with a one-line comment for documentation completeness and so the classification test can enumerate all five.

### N6. `dashboard_provider` typo degrades silently — `src/switchboard/providers.py:295-299, 170-175` + `src/switchboard/cli.py:148-155`

Validation is TOML-path only and checks non-empty, not existence (it can't — the dashboard isn't consulted at startup). A wrong id (e.g. `"opencode-go"` instead of `"opencode"`) means "no reading for provider X" forever → failsafe → with B1's gate behavior, an open gate for a provider that has never had a reading. The pre-fix failure mode this commit fixes (mismatched name → permanent failsafe) can therefore be re-created by a typo with no startup warning. A single boot-time log line when the first `/readings` fetch finds no matching provider would make the misconfiguration visible.

### N7. PACE inert pins are never actively purged — `src/switchboard/control.py:1298-1313` + proxy affinity table

Under PACE with `pin_conversations=False`, every dwell-expired pin becomes a dead table entry that only leaves via LRU eviction (`affinity_max_entries`, default 1024). Bounded, so not a leak, and pre-existing behavior — but since the pin now does nothing post-dwell, the table fills with inert entries at exactly the rate failovers occur, and the eviction-counter churn is the only observable. A dwell-expiry sweep would keep the table honest; at minimum worth a comment.

---

## Attack directions — verified-clean items

1. **Advisory classification completeness**: all sources reaching `_compute_header_permits` are correct (Header=False; DirectUsage/Dashboard/Null=True); umans path genuinely untouched (`_compute_polled_permits` never reads the flag); `getattr(..., False)` default is fail-closed for unknown sources. Only N5 (PolledTruthSource attribute missing, benign).
2. **Backstop ordering/resurrection**: cannot outrank fresh (queue/backstop strictly ordered after fresh queue + primary-in-immediate, control.py:1403-1410); cannot resurrect CLOSED (skipped first in classify, :1051-1052) or model-filtered/quarantined providers (removed in `_stage_filter` before classify, or expressed via `servable_providers`); reroute treats the backstop consistently (proxy.py:1648-1659 includes `queue_candidate` in alternatives).
3. **PACE inert pin**: traced every branch of the affinity block for `strategy=pace, pin_conversations=False` — within-dwell fronts, post-dwell is inert, no-pin path never re-fronts the primary, DEGRADED-affinity block skipped entirely. No path reorders on an expired pin.
4. **`_compute_header_permits`**: LKG exhaustion zero and the stale+429 `min(permits, 1)` clamp (reconcile.py:408-409) both survive the advisory exemption — except the no-LKG boot case (B1).
5. **GUI**: no `scanPinned` wedge — `_fetchProviderModels` never rejects (its promise always resolves, :907-925), so no uncaught exception can strand the pin; every exit from `runAutoMatchScan` wires a Dismiss (:1118-1122, :1163-1168), and add-model-btn/edit-model both unpin. No unescaped server-derived string in the new HTML — routes table `providers.map(esc)` (:1820), click-to-fill `esc(m)` (:1031-1033), datalist `esc(m)` (:1028), scan offers `esc(...)` (:1150-1160), prefill via value-assignment not innerHTML (:994-1004). The `+-0.123` fix is correct (:1361). The alias-clearing semantics match the server (N2 is the only concern).
6. **Revert-detection**: five control/advisory tests + all GUI tests fail pre-fix (N3 for the guard set).
7. **Overclaim**: only 476c0cc's LKG phrasing (N4); c309609 and b380967 messages are accurate as written.
