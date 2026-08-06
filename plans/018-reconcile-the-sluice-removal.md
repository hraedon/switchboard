# Plan 018 — Reconcile the rescued sluice removal onto main

Status: proposed execution plan (authored 2026-08-06)

Depends on: nothing. Blocks: Plan 019 (the drop-sluice + multi-account work
itself), which cannot land until this reconciliation is done.

## 1. What happened

A substantially complete removal of the sluice dependency existed as
**uncommitted working-tree state** on a second host, based on `73fa1ad`
("Plans 014-016"). It was found while reconciling that clone after the
repository's history was scrubbed and made public — and it was one
`git clean` from being lost. Advice to re-clone the host would have
destroyed it.

It is now preserved three ways: branch `feat/drop-sluice` on origin (replayed
onto scrubbed history, so it is safe to push), branch
`wip/drop-sluice-rescued` on the second host (**never push this** — its
ancestry contains the pre-scrub commits), and a patch plus tarball in
`~/switchboard-sluice-removal-rescue`.

The work is real and it passes: **411 tests green at its base, with no
`sluice` import remaining in `src/`.** It vendors the flow-control core
switchboard actually used into `switchboard.{limit,gate,reconcile,session,
truth,utils}` and drops sluice from `pyproject.toml` entirely. The author's
own plan (now `plans/019-...`) had already been adversarially reviewed by two
lineages and carries nine amendments.

## 2. The problem this plan solves

`feat/drop-sluice` is **43 commits behind main** and cannot be merged as-is.
Main moved a long way while the work sat uncommitted: the usage-error reroute,
per-provider credentials, strict config validation, the model-map admin API,
the container image, k8s manifests and CI.

Ten files changed on both sides. The overlap is concentrated:

| file | changed on branch | changed on main |
|---|---|---|
| `src/switchboard/proxy.py` | 182 | 453 |
| `src/switchboard/cli.py` | 100 | 184 |
| `src/switchboard/providers.py` | 128 | 51 |
| `src/switchboard/control.py` | 11 | 51 |

A blind `git rebase` of the branch onto main means resolving those four files
by hand against 43 commits of unrelated change — and the failure mode is not a
conflict marker, it is silently dropping one side's semantics. The reroute
loop, the credential replacement and the vendored gate all live in
`proxy.py`.

## 3. Approach: replay the direction that has fewer conflicts

**Do not rebase the branch onto main. Bring the branch's work to main
instead**, in the order that minimises hand-merging.

The insight is that most of the removal is *new files*, which conflict with
nothing:

1. **Land the new modules first, unused.** `limit.py`, `gate.py`,
   `reconcile.py`, `session.py`, `truth.py`, `utils.py` and
   `tests/fixtures/readings.json` are additions. Cherry-pick them onto main
   with their tests and merge them while nothing imports them. Main keeps
   depending on sluice at this point and stays green. This is the large,
   reviewable, low-risk half.
2. **Then switch the call sites, one module at a time**, in dependency order:
   `providers.py` → `dashboard.py` → `admin.py` → `cli.py` → `proxy.py`.
   Each step is a small diff against *current* main rather than a merge
   against a fork point, and each ends with a green suite. `proxy.py` goes
   last because it is where both sides changed most.
3. **Delete the dependency last**: remove `sluice` from `pyproject.toml`,
   `[tool.uv.sources]`, the CI sibling clone step, and the Dockerfile's named
   build context. Only after nothing imports it.

Verify at each step with the full trio, and treat a red suite as a stop
rather than something to fix forward:

```
uv run --extra dev --with pip pytest -q
uv run --extra dev ruff check src/ tests/
uv run --extra dev mypy src/switchboard/
```

## 4. What must not be lost

The branch predates all of this. Whoever does the reconciliation must confirm
each of these still holds afterwards, because a careless merge of `proxy.py`
or `cli.py` would silently revert them:

- **Usage-error reroute**, including the stateful loop guard, the probe, and
  the `should_reroute` predicate in `control.py`.
- **Per-provider credentials**: every inbound credential header stripped
  before the provider's own key is applied, and a configured `api_key_env`
  naming an unset variable failing startup. (Plan 019's own review reached
  the same fail-closed conclusion independently — finding #8 — so the two
  designs agree; make sure only one implementation survives.)
- **Exhausted-estate observability** (`usage_giveups_total`).
- **Strict config validation** of serve keys.
- **Model-map admin API and dashboard**, including that `control.py` stays
  stdlib-only.
- The permit-ownership fix in `_acquire_with_disconnect`: a transferred
  permit must not be released by the helper. The branch vendors its own gate,
  so **re-check this against the vendored implementation** rather than
  assuming the fix carried over.

## 5. Scope note

Plan 019 bundles two things: dropping sluice, and adding multi-account
support with conversation pinning. **Land the removal first and merge it
before starting the multi-account work.** They are separable, the removal is
what unblocks the deployment friction, and bundling them makes the review
that much harder to do honestly.

## 6. Why this is worth doing

Removing sluice deletes an entire class of friction that has already cost
real time: the sibling-checkout requirement, the `[tool.uv.sources]` path pin
that silently broke CI, the named BuildKit context in the Dockerfile, the
extra clone step in the workflow, and one of the two package-name collisions
with an unrelated public project. The dependency also no longer earns its
keep — sluice's own reason for existing largely went away with the provider
it was written for.

## 7. Done when

- No `sluice` import remains in `src/`, and it is absent from
  `pyproject.toml`, CI and the Dockerfile.
- The full suite is green, and every item in §4 is verified present.
- `feat/drop-sluice` is merged or deleted, and the rescue artefacts on the
  second host are cleaned up once the work is safely on main.
