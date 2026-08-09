# Plan 022 — Direct usage solicitation

Status: **Wave 1 implemented, unvalidated against a live provider.** Written
after the code, which is the wrong order and the reason this plan exists —
see §1. Authored 2026-08-09.

Depends on: Plan 020 Wave 4 (the weekly-window fields this populates and the
pace strategy that consumes them). Interacts with: Plan 017 (deployment — this
is what would let switchboard run without a usage-dashboard alongside it).

## 1. Why this plan exists

The code landed first. `direct_usage.py` arrived as ~550 lines in a working
tree with no plan, no work item, and no recorded decision, bundled into an
unrelated batch. Every other structural change in this repo got a plan before
it got an implementation, and the discipline is not ceremony: it is what
forces the question this change actually raises, which is not "does it parse"
but "should switchboard be in the business of scraping vendor consoles at
all". That question deserved an answer before 550 lines, and it gets one here.

The plan is written to be honest about that ordering rather than to
retroactively pretend the design came first.

## 2. What it does

switchboard's routing signals come from a **truth source** per provider. Until
now those were:

| Truth source | Where the data comes from |
|---|---|
| `PolledTruthSource` | the provider's own `/v1/usage`-style endpoint |
| `HeaderTruthSource` | rate-limit headers on the responses we already make |
| `DashboardTruthSource` | a separate usage-dashboard deployment's `/readings` API |
| `NullTruthSource` | nothing; the provider is never quota-routed |

`DashboardTruthSource` is the only one that carries a **weekly** window, and
weekly is what pace routing (Plan 020 D5) ranks on. So today, pace routing
requires running usage-dashboard next to switchboard.

Direct usage solicitation adds a fifth: switchboard fetches each provider's
usage from the vendor's own surface and normalises it into the same
`LimitState`, weekly fields included. The dependency on usage-dashboard goes
away for the providers that support it.

Three fetchers, in descending order of how much they should be trusted:

| Provider | Surface | Durability |
|---|---|---|
| `zai` / `zai-coding-plan` | JSON API, `/api/monitor/usage/quota/limit` | a real API; stable-ish |
| `opencode-go` | regex over the workspace page's SolidJS hydration blob | breaks whenever the page is rebuilt |
| `ollama-cloud` | regex over the settings page HTML | breaks whenever the page is restyled |

## 3. The honest tradeoff

**This is scraping.** Two of the three fetchers parse markup that no vendor
has promised to keep stable, using a browser User-Agent and a session cookie
copied out of a logged-in browser. That has three consequences worth stating
plainly rather than discovering later:

1. **It will break, without warning, on someone else's schedule.** Not "if".
   A frontend rebuild at opencode.ai silently ends the weekly signal.
2. **A session cookie is a bearer credential for a whole account**, broader
   than the API key it sits next to, and it expires on its own timetable.
3. **A vendor may consider it unwelcome.** It is the operator's own account
   and their own usage data, which is why this is defensible at all, but it
   is not an interface anyone agreed to provide.

Set against that: the alternative is running and maintaining a second service
(usage-dashboard) purely to observe quota, for a homelab-scale deployment,
and pace routing is worthless without a weekly signal from somewhere.

**The decision: ship it, opt-in, and make its failure loud.** Not because
scraping is good, but because the failure mode is genuinely contained — see
§4 — and a broken scrape costs a routing *optimisation*, never availability.

## 4. Why a broken scraper cannot hurt routing

This is the property that makes the tradeoff acceptable, and it is worth
being able to point at:

- A failed fetch serves last-known-good with `ok=False`, exactly as
  `DashboardTruthSource` does.
- `snapshot_provider_state` maps weekly fields **only from a fresh reading**,
  so a stale one produces `weekly_remaining_fraction=None`.
- `pace_surplus` returns `None` for any provider that is not `FRESH` or has
  no weekly data.
- `pace_rank` puts unscored providers after scored ones **in table order** —
  never starved, never scored on guesses.
- If every provider goes unscored, `pace` degrades to exactly `ordered`.

So the worst case of a silently-rotted parser is that pace routing quietly
becomes the default strategy. Traffic still flows. Nothing 503s.

The corollary is that a rotted parser is **invisible from routing behaviour**,
which is why WI-3 exists.

## 5. Work items

### WI-1 — Fetchers and truth source (landed, unvalidated)

`DirectUsageTruthSource` implementing the `TruthSource` protocol, plus the
three fetchers, selected by provider `type`. Opt-in per provider via
`direct_usage = true`. Unsupported types fall through to the existing truth
source rather than erroring — enabling it on a provider with no fetcher is a
no-op, not a boot failure.

Truth-source precedence: `direct_usage` → `dashboard_url` → provider-type
default.

**Done when:** a provider with `direct_usage = true` reports weekly data in
`/status.json` without a usage-dashboard. *(Code complete; the "without a
usage-dashboard" half is unverified against a live provider — see WI-4.)*

### WI-2 — Credentials must not be improvised (this PR)

The first cut accepted `api_key` as a fallback for a missing cookie, which
would have sent a provider's upstream bearer token to ollama.com or
opencode.ai as a raw `Cookie:` header — the wrong credential, to the wrong
place, in the wrong form, silently. Removed: a cookie must be configured
explicitly, via `direct_usage_cookie_env`, or the fetcher is not built.

The cookie contract is also unified. It was inconsistent — opencode wrapped
the value as `auth=<value>` while ollama passed it raw, so the same operator
input worked for one and silently failed for the other. Both now take a full
cookie string (`name=value; …`), and a bare token is accepted for opencode
with the `auth=` prefix supplied, since that is the shape a browser copy
actually produces.

**Done when:** no code path can send an API key as a cookie, and a
misconfigured cookie fails loudly at construction rather than as a 403 forty
minutes later.

### WI-3 — Make rot visible (this PR)

A fetch that succeeds (HTTP 200) but yields no usable numbers is a **parser
failure**, and it is not the same event as a network failure, an expired
cookie, or a provider that has no weekly quota. Today they are
indistinguishable: all four produce "no weekly signal".

- `DirectUsageParseError`, raised when a fetcher gets a response it cannot
  extract from, logged distinctly from transport failures.
- A `parse_failures` counter per provider, surfaced in `/status.json`, so a
  rotted parser is a number that goes up rather than a silence.
- A one-time `WARNING` at first parse failure naming the provider and the
  surface, because the counter is only useful to someone already looking.

**Done when:** a fetcher pointed at a changed page produces a distinct,
countable, logged event rather than a silent degradation.

### WI-4 — Live validation (open, gates the plan)

Point one provider at direct usage on a real account, confirm the weekly
numbers match what the vendor's own page shows, and leave it running long
enough to see a weekly window actually roll over.

**Done when:** `/status.json` weekly figures agree with the vendor UI, and the
reset boundary is observed rather than assumed.

### WI-5 — Concurrency semantics (open)

The fetchers synthesise `limit=1, hard_cap=2, requests_limit=100,
concurrent_sessions=0` because `LimitState` demands them and the vendor
surfaces do not report concurrency. The reconcile loop sizes the permit gate
from those numbers, so enabling `direct_usage` on a provider that previously
used `PolledTruthSource` or `HeaderTruthSource` changes its **concurrency**
behaviour, not just its usage signal. That is a real side effect, currently
undocumented and unmeasured.

Either separate the quota signal from the concurrency signal in `LimitState`,
or document the synthesised values as part of the contract and pick them
deliberately rather than by convenience.

**Done when:** the concurrency consequence of enabling direct usage is either
eliminated or written down.

## 6. Deliberate non-goals

- **Not the default.** `direct_usage` is off unless asked for, per provider.
- **No new provider types.** Only types with a fetcher are affected.
- **No browser automation.** If a surface needs JavaScript to render its
  numbers, it is out of scope — that is the line where this stops being a
  pragmatic shortcut and starts being a second product.
- **No credential capture flow.** Cookies come from the environment. There is
  no "log in for me" affordance and there should not be.

## 7. When to abandon this

Written down now so the decision is not re-litigated from scratch under
pressure later. Retire direct usage for a provider when any of these is true:

- the vendor ships a real usage API (use it — that is `PolledTruthSource`),
- the parser has needed fixing more than twice in a quarter,
- a vendor indicates the access is unwelcome,
- usage-dashboard is being run anyway for other reasons.

The fetchers are deliberately isolated in one module with one protocol, so
deleting a fetcher is a small change rather than an unpicking.
