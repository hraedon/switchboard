"""Authorize image publication against the canonical identifier-gate run."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

CANONICAL_WORKFLOW_PATH = ".github/workflows/identifier-gate.yml"
CANONICAL_CI_PATH = ".github/workflows/ci.yml"
SCAN_JOB_NAME = "scan"
REQUIRED_GATE_STEPS = frozenset(
    {
        "Check out trusted scanner",
        "Set up Python",
        "Verify repository gate compatibility",
        "Authorize canonical no-secret push dispatcher",
        "Fetch and derive the exact publication range",
        "Scan every introduced commit tree as data using trusted scanner code",
        "Scan commit messages using trusted scanner code",
    }
)
REQUIRED_CI_JOBS = frozenset({"test (3.12)", "test (3.13)", "test (3.14)"})
REQUIRED_CI_STEPS = frozenset({"ruff", "mypy", "pytest", "chaos harness"})


class AuthorizationError(Exception):
    """The triggering run cannot safely authorize publication."""


def validate_gate_run(
    workflow: Mapping[str, Any],
    jobs_payload: Mapping[str, Any],
    event_workflow_id: int,
) -> None:
    """Validate canonical identity and the exact trusted scan job result."""
    if workflow.get("id") != event_workflow_id:
        raise AuthorizationError("triggering workflow ID is not canonical")
    if workflow.get("path") != CANONICAL_WORKFLOW_PATH:
        raise AuthorizationError("canonical workflow path does not match repository policy")
    if workflow.get("state") != "active":
        raise AuthorizationError("canonical identifier-gate workflow is not active")

    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        raise AuthorizationError("triggering run jobs response is malformed")
    if jobs_payload.get("total_count") != len(jobs):
        raise AuthorizationError("triggering run jobs response is incomplete")
    scans = [job for job in jobs if isinstance(job, dict) and job.get("name") == SCAN_JOB_NAME]
    if len(scans) != 1:
        raise AuthorizationError("triggering run does not contain exactly one scan job")
    scan = scans[0]
    if scan.get("status") != "completed" or scan.get("conclusion") != "success":
        raise AuthorizationError("triggering run's exact scan job did not succeed")
    steps = scan.get("steps")
    if not isinstance(steps, list):
        raise AuthorizationError("triggering scan job has no inspectable steps")
    successful_steps = {
        step.get("name")
        for step in steps
        if isinstance(step, dict)
        and step.get("status") == "completed"
        and step.get("conclusion") == "success"
    }
    if not successful_steps >= REQUIRED_GATE_STEPS:
        raise AuthorizationError("triggering scan job lacks a required successful step")


def validate_ci_workflow(workflow: Mapping[str, Any]) -> int:
    """Validate the canonical CI workflow and return its immutable numeric ID."""
    workflow_id = workflow.get("id")
    if not isinstance(workflow_id, int):
        raise AuthorizationError("canonical CI workflow has no numeric ID")
    if workflow.get("path") != CANONICAL_CI_PATH or workflow.get("state") != "active":
        raise AuthorizationError("canonical CI workflow identity is invalid")
    return workflow_id


def find_successful_ci_run(runs_payload: Mapping[str, Any], approved_sha: str) -> int | None:
    """Return the newest complete successful push CI run for *approved_sha*."""
    runs = runs_payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise AuthorizationError("CI workflow runs response is malformed")
    candidates = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("head_sha") == approved_sha
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and isinstance(run.get("id"), int)
    ]
    return max((run["id"] for run in candidates), default=None)


def validate_ci_jobs(jobs_payload: Mapping[str, Any]) -> None:
    """Require every Python matrix job and every quality/chaos step to succeed."""
    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list) or jobs_payload.get("total_count") != len(jobs):
        raise AuthorizationError("CI jobs response is malformed or incomplete")
    named = {
        job.get("name"): job
        for job in jobs
        if isinstance(job, dict) and isinstance(job.get("name"), str)
    }
    if set(named) != REQUIRED_CI_JOBS:
        raise AuthorizationError("CI run does not contain the exact required matrix jobs")
    for name in REQUIRED_CI_JOBS:
        job = named[name]
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            raise AuthorizationError(f"CI job {name} did not succeed")
        steps = job.get("steps")
        if not isinstance(steps, list):
            raise AuthorizationError(f"CI job {name} has no inspectable steps")
        successful_steps = {
            step.get("name")
            for step in steps
            if isinstance(step, dict)
            and step.get("status") == "completed"
            and step.get("conclusion") == "success"
        }
        if not successful_steps >= REQUIRED_CI_STEPS:
            raise AuthorizationError(f"CI job {name} lacks a required successful step")


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
        raise AuthorizationError("GitHub API returned a non-object response")
    return payload


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("TRIGGER_RUN_ID", "")
    workflow_id = os.environ.get("TRIGGER_WORKFLOW_ID", "")
    approved_sha = os.environ.get("APPROVED_SHA", "")
    if (
        not token
        or not repository
        or not run_id.isdecimal()
        or not workflow_id.isdecimal()
        or len(approved_sha) not in {40, 64}
    ):
        print("image authorization inputs are missing or invalid", file=sys.stderr)
        return 1
    base = f"https://api.github.com/repos/{repository}"
    try:
        workflow = _api_get(f"{base}/actions/workflows/identifier-gate.yml", token)
        jobs = _api_get(f"{base}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", token)
        validate_gate_run(workflow, jobs, int(workflow_id))
        ci_workflow = _api_get(f"{base}/actions/workflows/ci.yml", token)
        ci_workflow_id = validate_ci_workflow(ci_workflow)
        query = urllib.parse.urlencode(
            {"head_sha": approved_sha, "event": "push", "per_page": 100}
        )
        ci_run_id: int | None = None
        for _attempt in range(60):
            runs = _api_get(
                f"{base}/actions/workflows/{ci_workflow_id}/runs?{query}", token
            )
            ci_run_id = find_successful_ci_run(runs, approved_sha)
            if ci_run_id is not None:
                break
            time.sleep(10)
        if ci_run_id is None:
            raise AuthorizationError("complete successful CI run was not found in time")
        ci_jobs = _api_get(
            f"{base}/actions/runs/{ci_run_id}/jobs?filter=latest&per_page=100", token
        )
        validate_ci_jobs(ci_jobs)
    except (AuthorizationError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        # Include the reason, not just the class. Every AuthorizationError
        # message in this file is a static string written here -- no API
        # payload, no token, nothing from the network -- so printing it leaks
        # nothing, and without it a refusal reads only "AuthorizationError"
        # and the operator has to re-derive which of a dozen checks tripped.
        print(f"image authorization refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("canonical identifier-gate scan job authorized image publication")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
