"""Validate a no-secret push dispatcher before the trusted gate uses its event."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

CANONICAL_DISPATCH_PATH = ".github/workflows/identifier-gate-dispatch.yml"


class DispatchError(Exception):
    """The dispatcher run is not the canonical event source."""


def validate_dispatch(
    workflow: Mapping[str, Any],
    run: Mapping[str, Any],
    jobs_payload: Mapping[str, Any],
    *,
    event_workflow_id: int,
    event_head_sha: str,
) -> str:
    """Return ``scan`` or ``delete`` after exact workflow/run/job validation."""
    if workflow.get("id") != event_workflow_id:
        raise DispatchError("dispatcher workflow ID is not canonical")
    if workflow.get("path") != CANONICAL_DISPATCH_PATH or workflow.get("state") != "active":
        raise DispatchError("dispatcher workflow path or state is not canonical")
    if (
        run.get("workflow_id") != event_workflow_id
        or run.get("head_sha") != event_head_sha
        or run.get("event") != "push"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
    ):
        raise DispatchError("dispatcher run identity, SHA, or conclusion is invalid")

    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list) or jobs_payload.get("total_count") != len(jobs):
        raise DispatchError("dispatcher jobs response is malformed or incomplete")
    named = {
        job.get("name"): job
        for job in jobs
        if isinstance(job, dict) and isinstance(job.get("name"), str)
    }
    if set(named) != {"dispatch", "branch-deletion"}:
        raise DispatchError("dispatcher run has unexpected jobs")
    outcomes = {
        name: (job.get("status"), job.get("conclusion")) for name, job in named.items()
    }
    if outcomes == {
        "dispatch": ("completed", "success"),
        "branch-deletion": ("completed", "skipped"),
    }:
        required_step = "Dispatch trusted identifier scan"
        mode = "scan"
    elif outcomes == {
        "dispatch": ("completed", "skipped"),
        "branch-deletion": ("completed", "success"),
    }:
        required_step = "Acknowledge branch deletion"
        mode = "delete"
    else:
        raise DispatchError("dispatcher jobs do not describe one safe event mode")
    successful_steps = {
        step.get("name")
        for step in named["dispatch" if mode == "scan" else "branch-deletion"].get(
            "steps", []
        )
        if isinstance(step, dict)
        and step.get("status") == "completed"
        and step.get("conclusion") == "success"
    }
    if required_step not in successful_steps:
        raise DispatchError("dispatcher's exact required step did not succeed")
    return mode


def _api_get(url: str, token: str) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise DispatchError("GitHub API returned a non-object response")
    return payload


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("DISPATCH_RUN_ID", "")
    workflow_id = os.environ.get("DISPATCH_WORKFLOW_ID", "")
    head_sha = os.environ.get("DISPATCH_HEAD_SHA", "")
    if (
        not token
        or not repository
        or not run_id.isdecimal()
        or not workflow_id.isdecimal()
        or len(head_sha) not in {40, 64}
    ):
        print("dispatcher authorization inputs are missing or invalid", file=sys.stderr)
        return 1
    base = f"https://api.github.com/repos/{repository}"
    try:
        workflow = _api_get(f"{base}/actions/workflows/identifier-gate-dispatch.yml", token)
        run = _api_get(f"{base}/actions/runs/{run_id}", token)
        jobs = _api_get(f"{base}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", token)
        mode = validate_dispatch(
            workflow,
            run,
            jobs,
            event_workflow_id=int(workflow_id),
            event_head_sha=head_sha,
        )
    except (DispatchError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"dispatcher authorization refused: {type(exc).__name__}", file=sys.stderr)
        return 1
    output = os.environ.get("GITHUB_OUTPUT", "")
    if not output:
        print("dispatcher authorization has no GitHub output channel", file=sys.stderr)
        return 1
    try:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"mode={mode}\n")
    except OSError:
        print("dispatcher authorization could not write its result", file=sys.stderr)
        return 1
    print("canonical no-secret dispatcher authorized trusted gate execution")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
