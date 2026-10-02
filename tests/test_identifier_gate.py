from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gate_module() -> ModuleType:
    return _load_script_module("check_committed_identifiers")


def _load_script_module(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        name,
        SCRIPTS_DIR / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate() -> ModuleType:
    return _load_gate_module()


def _write_publication(path: Path, visibility: str = "public") -> None:
    path.write_text(
        "[publication]\n"
        'remote_owner = "example"\n'
        'author_email = ["publisher@example.invalid"]\n'
        f'visibility = "{visibility}"\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "empty or unset"),
        ("   ", "empty or unset"),
        ("abc", "contains no usable identifiers"),
    ],
)
def test_public_ci_rejects_missing_or_unusable_denylist(
    gate: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: str | None,
    message: str,
) -> None:
    _write_publication(tmp_path / "publication.toml")
    monkeypatch.chdir(tmp_path)
    if raw is None:
        monkeypatch.delenv("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", raising=False)
    else:
        monkeypatch.setenv("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", raw)

    assert gate.main(["--ci"]) == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    [
        "[publication\n",
        (
            "[publication]\n"
            'remote_owner = "example"\n'
            'author_email = ["publisher@example.invalid"]\n'
            'visibility = "unknown"\n'
        ),
    ],
)
def test_ci_rejects_malformed_or_unknown_publication_config(
    gate: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    (tmp_path / "publication.toml").write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", "example-forbidden-token")

    assert gate.main(["--ci"]) == 1
    assert "publication policy is invalid" in capsys.readouterr().err


@pytest.mark.parametrize(
    "field",
    [
        "remote_owner = 7",
        "author_email = 7",
        'author_email = ["publisher@example.invalid", 7]',
        "visibility = 7",
    ],
)
def test_ci_rejects_invalid_publication_field_types(
    gate: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    values = {
        "remote_owner": 'remote_owner = "example"',
        "author_email": 'author_email = ["publisher@example.invalid"]',
        "visibility": 'visibility = "public"',
    }
    values[field.split(" =", 1)[0]] = field
    (tmp_path / "publication.toml").write_text(
        "[publication]\n" + "\n".join(values.values()) + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", "example-forbidden-token")

    assert gate.main(["--ci"]) == 1
    assert "publication policy is invalid" in capsys.readouterr().err


def test_public_ci_scans_with_configured_denylist(
    gate: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_publication(tmp_path / "publication.toml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", "example-forbidden-token")
    monkeypatch.setattr(gate, "collect_tracked_paths", lambda: [])

    assert gate.main(["--ci"]) == 0


def test_trusted_workflow_owns_the_only_gate() -> None:
    workflow = (REPO_ROOT / ".github/workflows/identifier-gate.yml").read_text(
        encoding="utf-8"
    )
    ci_workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow_paths = sorted((REPO_ROOT / ".github/workflows").glob("*.yml")) + sorted(
        (REPO_ROOT / ".github/workflows").glob("*.yaml")
    )
    scanner_workflows = [
        path
        for path in workflow_paths
        if "check_committed_identifiers.py" in path.read_text(encoding="utf-8")
    ]

    assert scanner_workflows == [REPO_ROOT / ".github/workflows/identifier-gate.yml"]
    assert "  workflow_run:\n" in workflow
    assert "workflows: [identifier-gate-dispatch]" in workflow
    assert "pull_request_target:" in workflow
    assert "check_committed_identifiers.py" not in ci_workflow
    assert "fetch-depth: 0" in workflow
    assert "--tree-range \"${{ steps.range.outputs.range }}\"" in workflow
    assert "--rev-range \"${{ steps.range.outputs.range }}\"" in workflow


def test_fork_pr_mutation_is_never_checked_out_or_executed() -> None:
    workflow = (REPO_ROOT / ".github/workflows/identifier-gate.yml").read_text(
        encoding="utf-8"
    )

    assert "pull_request_target:" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert 'git fetch --no-tags origin "pull/$PR_NUMBER/head"' in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "ref: ${{ github.event.pull_request.head.sha }}" not in workflow
    assert "--tree-range" in workflow
    assert "Push and PR commits are fetched as git objects and never checked out or run" in workflow


def test_push_range_policy_covers_delete_force_and_initial_cases() -> None:
    workflow = (REPO_ROOT / ".github/workflows/identifier-gate.yml").read_text(
        encoding="utf-8"
    )
    dispatcher = (
        REPO_ROOT / ".github/workflows/identifier-gate-dispatch.yml"
    ).read_text(encoding="utf-8")

    assert "secrets." not in dispatcher
    assert "github.event.deleted" in dispatcher
    assert "Acknowledge branch deletion" in dispatcher
    assert "Authorize canonical no-secret push dispatcher" in workflow
    assert "github.event.workflow_run.workflow_id" in workflow
    assert "github.event.workflow_run.head_sha" in workflow
    assert 'range="$parent..$PUSH_HEAD_SHA"' in workflow
    assert 'git merge-base "$default_ref" "$PUSH_HEAD_SHA"' in workflow
    assert "default-branch publication has no safe parent baseline" in workflow
    assert "branch publication has no safe default-branch baseline" in workflow


def test_push_dispatch_authorization_binds_workflow_run_jobs_and_head_sha() -> None:
    authorizer = _load_script_module("authorize_identifier_dispatch")
    head_sha = "7" * 40
    workflow = {
        "id": 17,
        "path": ".github/workflows/identifier-gate-dispatch.yml",
        "state": "active",
    }
    run = {
        "workflow_id": 17,
        "head_sha": head_sha,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    jobs = {
        "total_count": 2,
        "jobs": [
            {
                "name": "dispatch",
                "status": "completed",
                "conclusion": "success",
                "steps": [
                    {
                        "name": "Dispatch trusted identifier scan",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ],
            },
            {
                "name": "branch-deletion",
                "status": "completed",
                "conclusion": "skipped",
                "steps": [],
            },
        ],
    }

    assert authorizer.validate_dispatch(
        workflow,
        run,
        jobs,
        event_workflow_id=17,
        event_head_sha=head_sha,
    ) == "scan"

    with pytest.raises(authorizer.DispatchError):
        authorizer.validate_dispatch(
            workflow,
            run,
            jobs,
            event_workflow_id=17,
            event_head_sha="8" * 40,
        )


def test_commit_message_collection_is_control_character_safe(
    gate: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = "a" * 40
    second = "b" * 40
    messages: dict[str, bytes] = {
        first: b"prefix\x1eforged-record\x1fforbidden-token\x00suffix\n",
        second: b"ordinary message\n",
    }
    monkeypatch.setattr(gate, "_run_git", lambda _args: f"{first}\n{second}\n")
    monkeypatch.setattr(
        gate,
        "_run_git_bytes",
        lambda args: b"tree " + b"c" * 40 + b"\n\n" + messages[args[-1]],
    )
    monkeypatch.setattr(gate, "collect_tree_files", lambda _revision: [])

    collected = gate.collect_range_messages("base..head")

    assert collected == [
        (first, messages[first]),
        (second, messages[second]),
    ]
    assert b"forbidden-token" in collected[0][1]
    monkeypatch.setenv("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", "forbidden-token")
    assert gate._scan_rev_range("base..head") == 1
    assert "forbidden-token" not in capsys.readouterr().err


def test_tree_mode_scans_git_blobs_without_worktree_reads(
    gate: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_id = "c" * 40

    def git_bytes(args: list[str]) -> bytes:
        if args[1] == "ls-tree":
            return f"100644 blob {object_id}\tdocs/input.txt\0".encode()
        assert args == ["git", "cat-file", "blob", object_id]
        return b"contains forbidden-token\n"

    monkeypatch.setattr(gate, "_run_git_bytes", git_bytes)

    files = gate.collect_tree_files("head")
    violations = gate.scan_tree_files(frozenset({"forbidden-token"}), files)

    assert [(violation.path, violation.byte_offset) for violation in violations] == [
        (Path("docs/input.txt"), 9)
    ]


def test_tree_range_catches_add_then_remove_and_deduplicates_blobs(
    gate: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = "1" * 40
    second = "2" * 40
    forbidden_blob = "a" * 40
    clean_blob = "b" * 40
    shared_blob = "c" * 40
    cat_calls: list[str] = []
    monkeypatch.setattr(gate, "_run_git", lambda _args: f"{first}\n{second}\n")

    def git_bytes(args: list[str]) -> bytes:
        if args[1] == "ls-tree":
            blob = forbidden_blob if args[-1] == first else clean_blob
            return (
                f"100644 blob {blob}\tdocs/transient.txt\0"
                f"100644 blob {shared_blob}\tdocs/shared.txt\0"
            ).encode()
        object_id = args[-1]
        cat_calls.append(object_id)
        return {
            forbidden_blob: b"forbidden-token\n",
            clean_blob: b"clean\n",
            shared_blob: b"shared clean\n",
        }[object_id]

    monkeypatch.setattr(gate, "_run_git_bytes", git_bytes)

    violations = gate.scan_tree_range(frozenset({"forbidden-token"}), "base..head")

    assert len(violations) == 1
    assert violations[0].source_commit == first
    assert violations[0].path == Path("docs/transient.txt")
    assert cat_calls.count(shared_blob) == 1


def test_tree_range_rejects_intermediate_lfs_pointer_removed_later(
    gate: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = "1" * 40
    second = "2" * 40
    lfs_blob = "d" * 40
    clean_blob = "e" * 40
    monkeypatch.setattr(gate, "_run_git", lambda _args: f"{first}\n{second}\n")

    def git_bytes(args: list[str]) -> bytes:
        if args[1] == "ls-tree":
            blob = lfs_blob if args[-1] == first else clean_blob
            return f"100644 blob {blob}\tasset.bin\0".encode()
        if args[-1] == lfs_blob:
            return (
                b"version https://git-lfs.github.com/spec/v1\n"
                b"oid sha256:" + b"0" * 64 + b"\nsize 1\n"
            )
        return b"clean\n"

    monkeypatch.setattr(gate, "_run_git_bytes", git_bytes)

    with pytest.raises(gate.GateError, match=f"commit {first[:9]}"):
        gate.scan_tree_range(frozenset({"forbidden-token"}), "base..head")


def test_reports_redact_tokens_and_source_lines(
    gate: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "forbidden-token"
    source = f"private source containing {token}"
    violation = gate.Violation(
        identifier=token,
        path=Path(f"docs/{token}.txt"),
        line_number=7,
        line=source,
    )
    casefold_violation = gate.Violation(
        identifier="strasse",
        path=Path("docs/Straße.txt"),
        line_number=8,
        line="",
    )

    gate.print_report([violation, casefold_violation])
    gate._report_message_violations("commit message abc123", [violation])
    report = capsys.readouterr().err

    assert token not in report
    assert source not in report
    assert "Straße" not in report
    assert "docs/[REDACTED].txt: line 7" in report
    assert "[REDACTED PATH]: line 8" in report
    assert "commit message abc123" in report
    assert "line 7" in report


def test_image_publish_depends_on_successful_current_main_gate() -> None:
    workflow = (REPO_ROOT / ".github/workflows/image.yml").read_text(encoding="utf-8")

    assert "workflow_run:" in workflow
    assert "workflows: [identifier-gate]" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'workflow_run'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "refs/remotes/origin/main" in workflow
    assert "needs: authorize" in workflow
    assert "needs.authorize.outputs.current == 'true'" in workflow
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in workflow
    assert "Build approved main commit without publishing" in workflow
    assert workflow.index("Verify the installed switchboard is ours") < workflow.index(
        "Publish immutable approved image"
    )
    assert "packages: write" not in workflow.split("  publish:", 1)[0]
    assert "  publish:\n" in workflow
    assert "      packages: write" in workflow.split("  publish:\n", 1)[1]
    assert "ghcr.io/hraedon/switchboard:main" not in workflow
    assert 'docker push "$target"' in workflow
    assert "Bind authorization to canonical gate workflow and exact scan job" in workflow
    assert "github.event.workflow_run.workflow_id" in workflow
    assert "github.event.workflow_run.id" in workflow
    assert "actions: read" in workflow
    assert "APPROVED_SHA: ${{ github.event.workflow_run.head_sha }}" in workflow
    authorizer = (SCRIPTS_DIR / "authorize_image_gate_run.py").read_text(encoding="utf-8")
    assert 'CANONICAL_CI_PATH = ".github/workflows/ci.yml"' in authorizer
    for required in ("test (3.12)", "test (3.13)", "test (3.14)"):
        assert required in authorizer
    for required in ("ruff", "mypy", "pytest", "chaos harness"):
        assert required in authorizer


def test_image_authorizer_requires_canonical_workflow_and_exact_scan_job() -> None:
    authorizer = _load_script_module("authorize_image_gate_run")
    canonical = {
        "id": 42,
        "path": ".github/workflows/identifier-gate.yml",
        "state": "active",
    }
    successful_jobs = {
        "total_count": 1,
        "jobs": [
            {
                "name": "scan",
                "status": "completed",
                "conclusion": "success",
                "steps": [
                    {"name": name, "status": "completed", "conclusion": "success"}
                    for name in authorizer.REQUIRED_GATE_STEPS
                ],
            },
        ],
    }

    authorizer.validate_gate_run(canonical, successful_jobs, 42)

    rejected = [
        ({**canonical, "id": 41}, successful_jobs, 42),
        ({**canonical, "path": ".github/workflows/other.yml"}, successful_jobs, 42),
        ({**canonical, "state": "disabled_manually"}, successful_jobs, 42),
        (canonical, {"total_count": 0, "jobs": successful_jobs["jobs"]}, 42),
        (
            canonical,
            {
                "total_count": 1,
                "jobs": [
                    {
                        **successful_jobs["jobs"][0],
                        "conclusion": "failure",
                    },
                ],
            },
            42,
        ),
        (
            canonical,
            {
                "total_count": 1,
                "jobs": [
                    {
                        **successful_jobs["jobs"][0],
                        "steps": successful_jobs["jobs"][0]["steps"][:-1],
                    }
                ],
            },
            42,
        ),
    ]
    for workflow, jobs, workflow_id in rejected:
        with pytest.raises(authorizer.AuthorizationError):
            authorizer.validate_gate_run(workflow, jobs, workflow_id)


def test_image_authorizer_requires_exact_successful_ci_matrix_and_steps() -> None:
    authorizer = _load_script_module("authorize_image_gate_run")
    workflow = {"id": 84, "path": ".github/workflows/ci.yml", "state": "active"}
    approved_sha = "a" * 40
    runs = {
        "workflow_runs": [
            {
                "id": 100,
                "head_sha": approved_sha,
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]
    }
    required_steps = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in ("ruff", "mypy", "pytest", "chaos harness")
    ]
    jobs = {
        "total_count": 3,
        "jobs": [
            {
                "name": f"test ({version})",
                "status": "completed",
                "conclusion": "success",
                "steps": required_steps,
            }
            for version in ("3.12", "3.13", "3.14")
        ],
    }

    assert authorizer.validate_ci_workflow(workflow) == 84
    assert authorizer.find_successful_ci_run(runs, approved_sha) == 100
    authorizer.validate_ci_jobs(jobs)

    missing_chaos = {
        **jobs,
        "jobs": [
            {**jobs["jobs"][0], "steps": required_steps[:-1]},
            *jobs["jobs"][1:],
        ],
    }
    with pytest.raises(authorizer.AuthorizationError):
        authorizer.validate_ci_jobs(missing_chaos)
    assert authorizer.find_successful_ci_run(runs, "b" * 40) is None


def test_raw_commit_messages_use_multi_encoding_scanner_and_redacted_logs(
    gate: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sha = "f" * 40
    message = "prefix forbidden-token suffix".encode("utf-32-le") + b"\x00\xff"
    monkeypatch.setattr(gate, "_run_git", lambda _args: f"{sha}\n")
    monkeypatch.setattr(
        gate,
        "_run_git_bytes",
        lambda _args: b"tree " + b"0" * 40 + b"\n\n" + message,
    )
    monkeypatch.setattr(gate, "collect_tree_files", lambda _revision: [])
    monkeypatch.setenv("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", "forbidden-token")

    assert gate._scan_rev_range("base..head") == 1
    report = capsys.readouterr().err
    assert "forbidden-token" not in report
    assert "byte offset" in report


@pytest.mark.parametrize(
    "encoding",
    ["utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"],
)
def test_unicode_casefold_matches_accented_and_expanding_variants(
    gate: ModuleType,
    encoding: str,
) -> None:
    data = "CAFE\u0301 and Straße and \uff21\uff23\uff2d\uff25".encode(encoding)

    violations = list(gate.scan_content(data, frozenset({"café", "strasse", "acme"})))

    assert {violation.identifier for violation in violations} == {"café", "strasse", "acme"}


def test_unicode_casefold_commit_message_is_redacted(
    gate: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sha = "9" * 40
    message = "message CAFE\u0301 and Straße".encode("utf-16-be")
    monkeypatch.setattr(gate, "_run_git", lambda _args: f"{sha}\n")
    monkeypatch.setattr(
        gate,
        "_run_git_bytes",
        lambda _args: b"tree " + b"0" * 40 + b"\n\n" + message,
    )
    monkeypatch.setattr(gate, "collect_tree_files", lambda _revision: [])
    monkeypatch.setenv("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", '"café" strasse')

    assert gate._scan_rev_range("base..head") == 1
    report = capsys.readouterr().err
    assert "CAFÉ" not in report
    assert "Straße" not in report
    assert "forbidden identifier detected" in report


def test_raw_scan_covers_binary_utf16_and_forced_venv(gate: ModuleType) -> None:
    identifiers = frozenset({"forbidden-token"})
    binary = b"\x00\xffprefix" + b"forbidden-token" + b"\x00suffix"
    utf16 = "prefix forbidden-token suffix".encode("utf-16-le")
    utf32 = "prefix forbidden-token suffix".encode("utf-32-be")

    binary_hits = list(gate.scan_bytes(binary, identifiers))
    utf16_hits = list(gate.scan_bytes(utf16, identifiers))
    utf32_hits = list(gate.scan_bytes(utf32, identifiers))

    assert [hit.byte_offset for hit in binary_hits] == [8]
    assert [hit.byte_offset for hit in utf16_hits] == [14]
    assert len(utf32_hits) == 1
    assert utf32_hits[0].byte_offset in {28, 31}
    assert "_SKIP_DIRS" not in (SCRIPTS_DIR / "check_committed_identifiers.py").read_text(
        encoding="utf-8"
    )


def test_tree_scans_gitlink_and_control_sanitized_path_names(
    gate: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_id = "d" * 40
    raw_path = b"modules/forbidden-token\n::error::pwn\xe2\x80\xae"
    monkeypatch.setattr(
        gate,
        "_run_git_bytes",
        lambda _args: b"160000 commit " + object_id.encode() + b"\t" + raw_path + b"\0",
    )

    files = gate.collect_tree_files("head")
    violations = gate.scan_tree_files(frozenset({"forbidden-token"}), files)
    gate.print_report(violations)
    report = capsys.readouterr().err

    assert len(violations) == 1
    assert "forbidden-token" not in report
    assert "\\x0a::error::pwn" in report
    assert "\\u202e" in report
    assert "\n::error::pwn" not in report


@pytest.mark.parametrize(
    "pointer",
    [
        (
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:" + b"0" * 64 + b"\nsize 1\n"
        ),
        (
            b"version https://git-lfs.github.com/spec/v1\r\n"
            b"ext-0 example value\r\n"
            b"oid sha256:" + b"0" * 64 + b"\r\nsize 1\r\n"
        ),
        (
            b"version https://hawser.github.com/spec/v1\n"
            b"oid sha256:" + b"0" * 64 + b"\nsize 1\n"
        ),
    ],
)
def test_lfs_pointer_forms_fail_closed_without_claiming_blob_coverage(
    gate: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    pointer: bytes,
) -> None:
    object_id = "e" * 40
    files = [
        gate.GitTreeFile(
            path=Path("asset.bin"),
            object_id=object_id,
            object_type="blob",
            raw_path=b"asset.bin",
        )
    ]
    monkeypatch.setattr(
        gate,
        "_run_git_bytes",
        lambda _args: pointer,
    )

    with pytest.raises(gate.GateError, match="LFS pointer"):
        gate.scan_tree_files(frozenset({"forbidden-token"}), files)


def test_secret_workflow_actions_are_sha_pinned_and_pr_ref_is_verified() -> None:
    workflow = (REPO_ROOT / ".github/workflows/identifier-gate.yml").read_text(
        encoding="utf-8"
    )
    image_workflow = (REPO_ROOT / ".github/workflows/image.yml").read_text(
        encoding="utf-8"
    )

    uses = [
        line.split("uses:", 1)[1].strip().split()[0]
        for line in (workflow + image_workflow).splitlines()
        if "uses:" in line
    ]
    assert uses
    assert all(len(value.rsplit("@", 1)[1]) == 40 for value in uses)
    assert 'types: [opened, reopened, synchronize, edited]' in workflow
    assert 'fetched=$(git rev-parse "FETCH_HEAD^{commit}")' in workflow
    assert 'if [ "$fetched" != "$PR_HEAD_SHA" ]' in workflow
    assert "Verify repository gate compatibility" in workflow


def test_external_sync_compatibility_contract_is_explicit(gate: ModuleType) -> None:
    compatibility = (SCRIPTS_DIR / "check_identifier_gate_compatibility.py").read_text(
        encoding="utf-8"
    )

    assert {
        "raw-all-blobs",
        "trusted-tree",
        "history-tree-range",
        "control-safe-messages",
        "raw-message-bytes",
        "lfs-fail-closed",
        "redacted-logs",
    } == gate.REPO_GATE_CAPABILITIES
    assert "external sync" in compatibility
    assert "stale template" in compatibility


def test_branch_protection_is_documented_as_external_fail_closed_prerequisite() -> None:
    checker = (SCRIPTS_DIR / "check_branch_protection_prerequisite.py").read_text(
        encoding="utf-8"
    )
    documentation = (
        REPO_ROOT / "docs/ops/identifier-gate-prerequisites.md"
    ).read_text(encoding="utf-8")

    assert 'REQUIRED_CONTEXT = "identifier-gate / scan"' in checker
    assert "check=True" in checker
    assert "return 1" in checker
    assert "does not create" in documentation
    assert "owner approval" in documentation
