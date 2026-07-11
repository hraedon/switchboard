"""Mechanical gate against committing work-domain identifiers.

Two complementary checks:

1. Always-on (no configuration): no tracked file may live under ``samples/``.
   ``.gitignore`` is advisory — ``git add -f`` bypasses it — so this guard makes
   an accidental force-add of a real identifier-bearing data file fail CI. The
   ``samples/`` directory holds real environment data (hostnames, service
   accounts, principal handles) that must never be committed (AGENTS.md).

2. Secret-driven: when ``SWITCHBOARD_FORBIDDEN_IDENTIFIERS`` is set (a
   whitespace-separated list of real identifiers — hostnames, emails, service
   accounts, principal handles, personal names), every tracked text file
   outside ``samples/`` is scanned for those identifiers. This catches real
   names that leaked into docs, tests, or reflections. It is a no-op (exit 0)
   until the secret is configured, so it never blocks a fresh clone or a fork
   without the secret.

Run locally: python scripts/check_committed_identifiers.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

MIN_IDENTIFIER_LENGTH = 4
_BINARY_SNIFF_LEN = 8192
# Dirs skipped by the identifier scan: .venv is build output. The always-on
# guard below handles root-level samples/ (which holds real identifier-bearing
# data); nested directories named samples/ (e.g. tests/samples/) are legitimate
# code dirs and SHOULD be scanned.
_SKIP_DIRS = frozenset({".venv"})
# Root-level gitignored data dirs that must never contain a tracked file. The
# guard matches the first path component so a legitimate nested code dir named
# ``samples`` (e.g. ``tests/samples/``) is not a false positive.
_GUARDED_DIRS = frozenset({"samples"})


@dataclass(frozen=True)
class Violation:
    identifier: str
    path: Path
    line_number: int
    line: str


def _filter_identifiers(identifiers: frozenset[str]) -> frozenset[str]:
    """Lowercase, strip, and drop empty or short identifiers."""
    return frozenset(
        token.lower()
        for token in (i.strip() for i in identifiers)
        if len(token) >= MIN_IDENTIFIER_LENGTH
    )


def parse_identifier_set(raw: str) -> frozenset[str]:
    """Build a normalized set of identifiers from the raw denylist.

    Accepts whitespace-separated tokens (the CI-secret form) and/or one token
    per line. Full-line and trailing ``#`` comments are stripped, so a
    human-maintained denylist file may document itself without every comment
    word becoming a forbidden token.
    """
    tokens: set[str] = set()
    for line in raw.splitlines() or [raw]:
        content = line.split("#", 1)[0].strip()
        if content:
            tokens.update(content.split())
    return _filter_identifiers(frozenset(tokens))


def scan_text(text: str, identifiers: frozenset[str]) -> Iterator[Violation]:
    """Yield a violation for every occurrence of one of *identifiers*.

    The match is case-insensitive and counts any substring occurrence; real
    identifiers such as ``WORK-DOMAIN`` can legitimately appear inside longer
    tokens.
    """
    identifiers = _filter_identifiers(identifiers)
    if not identifiers:
        return
    for line_number, line in enumerate(text.splitlines(), start=1):
        lower = line.lower()
        for identifier in identifiers:
            start = 0
            while True:
                offset = lower.find(identifier, start)
                if offset == -1:
                    break
                yield Violation(
                    identifier=identifier,
                    path=Path("."),
                    line_number=line_number,
                    line=line,
                )
                start = offset + len(identifier)


def _sniff_encoding(chunk: bytes) -> str | None:
    """Return the text encoding if *chunk* starts with a known BOM, else None."""
    if chunk.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if chunk.startswith(b"\xfe\xff"):
        return "utf-16-be"
    if chunk.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return None


def _is_binary(chunk: bytes) -> bool:
    """Heuristic: null byte present without a recognized text BOM → binary."""
    if _sniff_encoding(chunk) is not None:
        return False
    return b"\x00" in chunk


def scan_files(identifiers: frozenset[str], paths: list[Path]) -> list[Violation]:
    """Scan every readable text file in *paths* for forbidden identifiers.

    UTF-16 files (common in Windows tooling output) are detected via BOM and
    decoded correctly rather than misclassified as binary by the null-byte
    heuristic.
    """
    violations: list[Violation] = []
    for path in paths:
        try:
            with path.open("rb") as f:
                chunk = f.read(_BINARY_SNIFF_LEN)
        except OSError:
            continue
        if _is_binary(chunk):
            continue
        encoding = _sniff_encoding(chunk) or "utf-8"
        try:
            text = path.read_text(encoding=encoding, errors="replace")
        except OSError:
            continue
        for violation in scan_text(text, identifiers):
            violations.append(replace(violation, path=path))
    return violations


def _paths_from_git(args: list[str]) -> list[Path]:
    """Run a NUL-delimited git path command and return Paths.

    No filtering is applied here — the always-on samples/ guard needs to see
    every tracked path so it can detect a force-add. The identifier scan
    filters out _SKIP_DIRS separately.
    """
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=True,
    )
    paths: list[Path] = []
    for raw in result.stdout.split("\0"):
        if not raw:
            continue
        paths.append(Path(raw))
    return paths


def collect_tracked_paths() -> list[Path]:
    """Return tracked file paths from ``git ls-files``, excluding obvious skips."""
    return _paths_from_git(["git", "ls-files", "-z"])


def collect_staged_paths() -> list[Path]:
    """Return staged (added/copied/modified/renamed) paths for the pre-commit hook.

    Scans only what is about to be committed rather than the whole tree, so the
    local gate is fast enough to run on every commit. Deletions are excluded
    (``--diff-filter=ACM``) because there is nothing to scan. ``--no-renames``
    decomposes renames into add+delete so the new path (e.g. a file moved into
    ``samples/``) is included as an addition and caught by the always-on guard.
    """
    return _paths_from_git(
        [
            "git", "diff", "--cached", "--name-only",
            "--diff-filter=ACM", "--no-renames", "-z",
        ]
    )


def print_report(violations: list[Violation]) -> None:
    violations.sort(key=lambda v: (str(v.path), v.line_number, v.identifier))
    print("Committed identifier violations detected:", file=sys.stderr)
    for v in violations:
        print(f"  {v.path}:{v.line_number}: {v.identifier!r}", file=sys.stderr)
        print(f"      {v.line.rstrip()}", file=sys.stderr)
    print(f"\nTotal: {len(violations)} violation(s)", file=sys.stderr)


def leaked_tracked_files(paths: list[Path], guarded: frozenset[str]) -> list[Path]:
    """Tracked files whose root component is a guarded (gitignored) data dir.

    Matches only the first path component so a nested code directory that happens
    to be named ``samples`` (e.g. ``tests/samples/``) is not a false positive.
    """
    return [p for p in paths if p.parts and p.parts[0] in guarded]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate that prevents committing forbidden domain identifiers.",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Scan only staged files (for the pre-commit hook) instead of the "
        "full tracked tree (the CI default).",
    )
    args = parser.parse_args(argv)

    paths = collect_staged_paths() if args.staged else collect_tracked_paths()

    # 1. Always-on: no tracked file under a guarded (gitignored) data dir. This
    #    catches a ``git add -f samples/...`` leak regardless of secret config.
    leaked = leaked_tracked_files(paths, _GUARDED_DIRS)
    if leaked:
        print("Tracked files under a gitignored data directory detected:", file=sys.stderr)
        for p in sorted(leaked, key=str):
            print(f"  {p}", file=sys.stderr)
        print(
            "\nThese paths are gitignored by convention (samples/ holds real "
            "identifier-bearing data — hostnames, service accounts, principal "
            "handles). Remove them from the index: git rm --cached -r <path>.",
            file=sys.stderr,
        )
        return 1

    # 2. Secret-driven: scan tracked text files (outside guarded dirs) for
    #    forbidden identifiers. No-op until the secret is configured.
    raw = os.environ.get("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", "")
    if not raw.strip():
        print(
            "SWITCHBOARD_FORBIDDEN_IDENTIFIERS is empty or unset; skipping identifier gate.",
            file=sys.stderr,
        )
        return 0

    identifiers = parse_identifier_set(raw)
    if not identifiers:
        print(
            "SWITCHBOARD_FORBIDDEN_IDENTIFIERS contained no usable identifiers (minimum "
            f"length is {MIN_IDENTIFIER_LENGTH} characters); skipping gate.",
            file=sys.stderr,
        )
        return 0

    scan_paths = [p for p in paths if not any(part in _SKIP_DIRS for part in p.parts)]
    violations = scan_files(identifiers, scan_paths)
    if violations:
        print_report(violations)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
