# Plan 025 — Peak-aware routing & GUI config completeness

Status: **Ratified by Paul 2026-08-11 ("draft and implement the plan"), implemented same session.**

Depends on: the advisory-containment and pace-failback fixes (branch
`fix/advisory-containment-pace-failback`, stacked under this plan), the config
store (Plan 020 Wave 1), runtime provider lifecycle (Plan 020 Waves 0+1), and
the `dashboard_provider` mapping key that made the usage-dashboard truth
source actually match the lab's provider names.

## 1. What Paul asked for

> What I ideally want the system to do is burn usage such that I'm not
> leaving usage on the table. This should avoid burning usage where that will
> be expensive (zai during peak hours, opencode go on models that are
> expensive).

and, ratifying this plan:

> I'd also like it if configuration via the GUI were complete enough that I
> could add API keys for new providers and have those persist.

Three asks:

1. **Peak-hours awareness.** `strategy = "pace"` is the burn-the-surplus
   ordering Paul wants, but enabling it today would front z.ai on surplus
   during its peak window (Mon–Fri 14:00–18:00 UTC+8), when GLM Coding Plan
   usage burns quota at the expensive rate. The system needs a way to say
   "this provider is expensive right now — don't prefer it", so pace can be
   turned on without doing the one thing Paul explicitly said to avoid.
2. **GUI credential completeness.** The server side of stored credentials
   has existed since Plan 020 Wave 1 (`key_mode='stored'`, write-only,
   masked everywhere, survives restart — live-proven). But the Add Provider
   form only ever offered the `api_key_env` path, which requires the env var
   to already exist in the server's environment — impossible for a genuinely
   new provider added from the browser, and the dry-build correctly 400s on
   the unset var. The GUI made the working feature unreachable; hence the
   field impression that key persistence "doesn't work".
3. **Whatever else makes sense** — explicitly including not preserving GUI
   behaviour that gets in the way.

## 2. Why the naive versions are wrong

**Peak as a clock read in the routing core.** `control.py` is pure and
stdlib-only, receives a *monotonic* `now`, and the import-boundary tests
forbid wall-clock reads. That prohibition is load-bearing (deterministic
replay, testability), so peak state must arrive as a *shell-computed input*
on `ProviderState`, exactly like `weekly_remaining_fraction` does. The shell
(`snapshot_provider_state`) owns the wall clock; the core sees a boolean.

**Peak as a hard exclusion.** Dropping an in-peak provider from the plan
recreates the availability failure Plan 022's containment work just fixed —
"expensive" is not "broken". Peak must *demote*, not exclude: an in-peak
provider serves only when nothing cheaper can take the request. This is the
same shape as the trailing-24h demotion (Plan 013), including its "may demote
the primary" property — the provider stays a queue backstop and the terminal
fallback.

**A boolean `zai_peak = true` provider flag.** Hardcoding one vendor's window
means the next provider with time-of-day pricing (Qwen's token plan is
already documented: off-peak 22:00–08:00 UTC+8 daily) needs another code
change. The window *spec* belongs in config; only the evaluation belongs in
code. usage-dashboard's `shared/offpeak.py` already models these windows as
pure functions — this plan ports the concept (fixed-offset timezone, weekday
set, cross-midnight ranges), not the code, since switchboard's spec must be
data, not functions.

**Fixing the GUI by adding a second "stored key" form.** The Add and Edit
forms already share `renderProviderForm`; a parallel form would drift. The
right change is a credential-mode selector in the existing form (env /
stored / passthrough), with Edit allowed to *switch* modes — previously Edit
was locked to the row's existing mode, so a provider created with the wrong
mode could only be fixed by delete + re-add (losing routes pointing at it —
the name is the route key).

## 3. Design

### 3a. Peak window spec

New per-provider config key `peak_windows`: a list of window specs, each

    "<days> <start>-<end> <utcoffset>"
    e.g. "mon-fri 14:00-18:00 +08:00"     (z.ai GLM Coding Plan peak)
         "daily 08:00-22:00 +08:00"       (Qwen token plan peak)

- `<days>`: `daily`, a single day (`mon`), a range (`mon-fri`), or a
  comma-set (`mon,wed,fri`). Day names are the vendor-local weekday.
- `<start>-<end>`: 24h `HH:MM`. `end` is exclusive. `end <= start` means the
  window crosses midnight (`22:00-08:00`); the *day* constraint applies to
  the window's start.
- `<utcoffset>`: mandatory fixed offset (`+08:00`, `-07:00`, `Z`). Fixed
  offsets only — the vendors that matter (SGT) don't observe DST, and a
  zoneinfo dependency buys ambiguity (host tz data) for no current need.

Parsing lives in a new shell module `switchboard/peak.py` (datetime is fine
there): `parse_peak_windows(list[str]) -> tuple[PeakWindow, ...]` raising
`ValueError` with the offending spec, and
`in_peak(windows, now_epoch) -> bool` plus
`next_boundary(windows, now_epoch) -> float | None` for display.

Validation runs at every ingestion point: TOML boot (fail startup with the
bad spec named), config-store upsert (400 on the admin surface), and the
dry-build path inherits both.

### 3b. Routing semantics

- `ProviderState` gains `in_peak: bool = False` (pure data; no clock).
- `snapshot_provider_state` computes it from the context's parsed windows.
- In the eligibility loop, `in_peak` joins the demotion signals
  (headroom / token budget / 24h): an in-peak provider is demoted from
  `immediate` to `queue_eligible`. Like the 24h signal — and unlike headroom
  — it **may demote the primary**: expensive-is-expensive regardless of
  table position. Demoted ≠ dropped: queue backstop and terminal fallback
  are unchanged.
- Interaction with `pace`: demotion happens *before* strategy ordering, so
  an in-peak provider never enters the surplus race. Off-peak it competes
  normally. No pace-specific code needed.
- No enable knob: configuring `peak_windows` on a provider *is* the opt-in.

### 3c. Config store completeness

`provider_config` gains two nullable TEXT columns, with an idempotent
boot-time migration (`PRAGMA table_info` → `ALTER TABLE ADD COLUMN` for each
missing column — both live instances have existing store files):

- `dashboard_provider` — the usage-dashboard id this provider reads
  (mirrors the TOML key added under Plan 022's follow-ups).
- `peak_windows` — JSON array of window specs, validated on upsert.

Both flow through `to_provider_section`, the masked read surface, the
effective-config view, `_PROVIDER_BODY_FIELDS` (admin create/update), and
the GUI edit form. TOML remains an equal citizen (env > store > TOML
precedence unchanged).

### 3d. GUI

- **Add Provider** gains a credential-mode selector: *env var* (unchanged),
  *paste key* (`key_mode='stored'`, sent once, write-only thereafter), and
  *passthrough* (explicit, labelled as forwarding the client's key).
- **Edit Provider** gains the same selector, pre-set to the row's mode.
  Switching to `stored` requires typing the key; switching to `env` requires
  the var name; switching to `passthrough` is a one-click downgrade with the
  warning inline. Staying in `stored` keeps the existing write-only
  leave-blank-to-keep behaviour.
- Edit (and Add's advanced rows) expose `dashboard_provider` and
  `peak_windows` (one spec per line in a textarea-like input; sent as a
  list).
- Provider cards show a `peak` badge with the boundary countdown when the
  provider has windows configured (`peak (ends 2h 10m)` / `off-peak (peak in
  3h)`), fed by two new per-provider status fields.

### 3e. Out of scope (unchanged from the review's findings)

- Per-model provider preference/ordering (the model map stays a set filter).
- Per-provider cost weights or price tables (Plan 009 WS-G territory).
- Per-provider pace burn rates.
- Scheduling/automation of config changes.

## 4. Validation

- Unit: spec parser (day sets, ranges, cross-midnight, offsets, rejects),
  `in_peak` boundary behaviour (start inclusive, end exclusive, weekday roll
  in the *window's* timezone, not the host's).
- Control: in-peak demotion (primary and non-primary), pace × peak (an
  in-peak provider with the best surplus is not fronted; off-peak it is),
  demoted-still-backstop.
- Store: migration adds columns to a pre-025 DB without touching rows;
  upsert rejects bad specs; round-trip through `to_provider_section`.
- Admin: create with `key_mode='stored'` + new fields end-to-end.
- GUI harness: credential modes on add, mode switch on edit, peak fields
  round-trip, peak badge rendering.
- Live (mvmcc03): `strategy = "pace"` + zai `peak_windows`; verify zai is
  demoted during a synthetic in-peak check, pace ranks the rest by surplus,
  and a smoke completion serves.
