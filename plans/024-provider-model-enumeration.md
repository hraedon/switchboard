# Plan 024 — Provider model enumeration & auto-match

Status: **Implemented and live-validated.** Authored 2026-08-09. Review
findings fixed 2026-08-10.

Depends on: the model map (Plan 010 Feature B) for the (provider, model) pairs
this surfaces and auto-creates, the config store (Plan 020 Wave 1) for the
persisted model map, and the existing `/admin/providers/<name>/test` reachability
probe (Plan 020 WI-3), which this generalises from a yes/no status to a model
list.

## 1. What Paul asked for

> Add functionality to the GUI to enumerate the models from a provider and make
> those visible when doing mappings. Also make providers autoadd (or prompt to
> be added) when they offer an exact match to a defined model (glm-5.2 ->
> glm-5.2).

Two asks:

1. When the operator is building a model map entry, they should not have to
   type model strings blind. Switchboard can already reach the upstream's
   `/models` endpoint (the reachability probe does exactly that) — it should
   surface what the provider actually offers, so an operator mapping `glm-5.2`
   can see "ollama-cloud offers `glm-5.2`" without leaving the dashboard.
2. A provider that offers a model whose name exactly matches a model the
   operator has already defined in the map is a candidate for one-click
   wiring: if `glm-5.2` is defined and ollama-cloud serves `glm-5.2`, offer to
   add that alias to the existing entry.

## 2. Why the naive version is wrong

Two temptations to resist, both with prior art in this repo:

**Auto-add without confirmation.** The quarantine plan (Plan 023) documents
why a breaker that fires on bad evidence takes healthy providers out of
service. The mirror applies here: silently writing to the model map on the
strength of a `/models` listing would change routing behaviour without an
operator decision. A provider listing `gpt-4` is not consent to route `gpt-4`
to it — the operator may have reasons (cost, latency, capability) that the
listing cannot see. So: **prompt, do not auto-write.** The auto-match is an
offer surfaced in the GUI, applied by an explicit click.

**Reading the request body's `model` field.** AGENTS.md hard rule 6 (cache
transparency) permits reading the request body's `model` field only for the
bounded, opt-in exceptions (Plan 010 model rewrite, Plan 019 conversation
fingerprinting). Enumeration is a separate concern that reads the upstream's
own `/models` surface, never a client request body. It does not touch the
cache-transparency guarantees.

## 3. What it does

### Backend

A new admin endpoint, generalising the existing reachability probe:

```
GET /admin/providers/<name>/models
```

It issues `GET {upstream}/models` with the provider's own credential (resolved
exactly as the forwarding path presents it — the same `api_key`/`auth_header`/
`auth_prefix` the test endpoint uses), parses the OpenAI-compatible `data`
array, and returns the model IDs. Auth + CSRF gated like the test endpoint,
because each call spends a request against a real upstream.

Response shape:

```json
{
  "ok": true,
  "status": 200,
  "latency_ms": 42.0,
  "models": ["glm-5.2", "kimi-k3", "deepseek-v4-flash"],
  "detail": ""
}
```

On transport failure, parse failure, or non-2xx, `ok` is false and `models` is
empty — the operator sees the failure reason in `detail`, same as the test
endpoint. No credential is ever echoed (sentinel greps in the test, same
discipline as the test endpoint).

### GUI

Two changes, both in the model-map section:

1. **Per-provider "show models" affordance in the add-mapping form.** Each
   alias input row gains a "show models" button. Clicking it fetches
   `/admin/providers/<name>/models` and renders the IDs as a `<datalist>` on
   the input, so the operator types-and-matches rather than copy-pasting from
   a vendor console. A non-empty result also shows the count.

2. **Auto-match card.** A "Scan for exact-match models" button in the Model
   Map section fetches each configured provider's models, then for each
   defined model name checks whether any provider offers an exact string
   match that the model map does not already wire. Matches are listed as
   one-click "Add `glm-5.2 → ollama-cloud: glm-5.2`" offers. Clicking one
   POSTs the alias to `/admin/model-map` (merging into the existing entry's
   aliases, not replacing them). No write happens without the click.

The scan is sequential per provider (not parallel) to avoid hammering a
single upstream with concurrent `/models` calls. A provider that fails to
answer is listed as "unavailable" rather than aborting the scan — a partial
answer is more useful than none.

## 4. Why a failed enumeration cannot hurt routing

Same property that makes direct usage (Plan 022) safe: enumeration is
read-only and purely advisory. The model map is not modified by enumeration,
only by an explicit POST the operator approves. A failed `/models` fetch
produces an empty list and a detail string — no alias is created, no routing
decision changes. The worst case of a rotted or unreachable `/models` endpoint
is that the operator types the alias manually, exactly as they do today.

## 5. Work items

### WI-1 — Backend enumeration endpoint (this PR)

`GET /admin/providers/<name>/models` in `admin.py`, generalising
`handle_provider_test` to parse the response body's `data` array. Shares the
test endpoint's auth/CSRF/credential-resolution/no-echo discipline.

**Done when:** the endpoint returns a model list for a 200 OpenAI-shaped
response, reports `ok: false` with a detail on transport/parse/non-2xx
failure, and no credential appears in the response.

### WI-2 — GUI model enumeration in the add-mapping form (this PR)

Per-provider "show models" button → `<datalist>` of model IDs on the alias
input. Fetches on click, not on every keystroke (one upstream call per
provider per form open).

**Done when:** clicking "show models" for a provider populates the alias
input's datalist, and a parse failure shows the reason instead of silently
emptying.

### WI-3 — GUI auto-match scan (this PR)

"Scan for exact-match models" button → per-provider `/models` fetch → exact
string match against defined model names → offers to add the alias. Explicit
click to apply; no automatic writes.

**Done when:** a provider serving `glm-5.2` produces an offer for a defined
`glm-5.2` model, clicking the offer adds the alias without disturbing the
entry's other aliases, and a provider that already wires the model produces no
offer.

### WI-4 — Live validation (done)

Point the deployed instance at a real provider and confirm the enumerated
list matches the vendor's documented surface.

**Done:** live-validated 2026-08-09 against all three production upstreams
via the deployed pod's `/admin/providers/<name>/models` endpoint:
opencode-go (25 models, 200/JSON), ollama-cloud (18 models, 200/JSON),
zai-coding-plan (8 models, 200/JSON). Parser correct on all three
OpenAI-shaped `data` arrays. Cited from the Opus directive's pre-verified
evidence; corroborated by the full test suite.

## 6. Deliberate non-goals

- **No automatic model-map writes.** Enumeration is advisory; the operator
  clicks to apply an auto-match offer. Silent writes would change routing
  without a decision, the quarantine plan's lesson applied in reverse.
- **No fuzzy matching.** "Exact match" means string equality. `glm-5.2` ≠
  `glm5.2` ≠ `glm-5.2-turbo`. Fuzzy matching would surface false offers and
  erode trust in the feature; the operator can still type a fuzzy alias
  manually.
- **No background scanning.** Enumeration happens on operator action, not on a
  timer. A background scan would spend upstream calls on every provider
  periodically for a feature that is used rarely and on demand.
- **No cross-provider deduplication of the list.** Each provider's `/models`
  is its own surface. The GUI shows them per-provider, not merged.
- **No body-field reading.** Enumeration reads the upstream `/models`
  endpoint, never a client request body. The cache-transparency guarantees in
  AGENTS.md are untouched.