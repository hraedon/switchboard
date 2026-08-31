# Identifier gate: external prerequisite

The repository-local gate is fail-closed and scans pushes plus pull requests,
but it cannot prevent an actor with direct write access from bypassing a check
that runs after a push. GitHub branch protection (or an equivalent ruleset) must
therefore require **`identifier-gate / scan`** on `main`, require branches to be
up to date, and require pull-request review before merge.

Verify that external state, without modifying it:

```console
python scripts/check_branch_protection_prerequisite.py
```

The check exits nonzero when GitHub cannot be queried, classic branch protection
is absent, or the required settings are missing. It deliberately does not create
or modify protection. Applying a protection rule or ruleset requires explicit
owner approval.

Push scanning is intentionally split: the branch-controlled
`identifier-gate-dispatch.yml` workflow has no secrets, while the actual scan is
performed by `identifier-gate.yml` from the trusted default branch after binding
the dispatcher workflow ID, path, run ID, job mode, and head SHA through the
GitHub API. A ruleset must prevent deletion, suppression, or unreviewed mutation
of the dispatcher and trusted gate workflow files. Without that owner-managed
rule, repository code cannot force a modified branch to emit a push event.

The trusted gate handles fork pull requests through `pull_request_target` and
does not enable `merge_group`: GitHub evaluates a merge-group workflow from the
synthetic merge commit, so repository code cannot guarantee that the
secret-bearing scanner is still the trusted base copy. A future merge-queue
rollout must first establish an owner-approved trusted workflow/ruleset design.
