"""Check the external main-branch protection prerequisite; never mutates GitHub."""

from __future__ import annotations

import json
import shutil
import subprocess

REPOSITORY = "hraedon/switchboard"
BRANCH = "main"
REQUIRED_CONTEXT = "identifier-gate / scan"


def main() -> int:
    gh = shutil.which("gh")
    if gh is None:
        print("branch-protection prerequisite: gh is unavailable; refusing to assume")
        return 1
    try:
        result = subprocess.run(
            [
                gh,
                "api",
                f"repos/{REPOSITORY}/branches/{BRANCH}/protection",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        protection = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        print("branch-protection prerequisite could not be verified; refusing to assume")
        return 1

    status = protection.get("required_status_checks") or {}
    contexts = set(status.get("contexts") or [])
    contexts.update(
        check.get("context", "")
        for check in status.get("checks") or []
        if isinstance(check, dict)
    )
    problems: list[str] = []
    if REQUIRED_CONTEXT not in contexts:
        problems.append(f"required status check {REQUIRED_CONTEXT!r} is absent")
    if status.get("strict") is not True:
        problems.append("required status checks are not strict/up-to-date")
    if protection.get("required_pull_request_reviews") is None:
        problems.append("pull-request review protection is absent")
    if problems:
        print("branch-protection prerequisite is not satisfied:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("branch-protection prerequisite is satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
