"""Mechanical gate against committing work-domain identifiers.

Three complementary checks:

1. Always-on (no configuration): no tracked file may live under ``samples/``.
   ``.gitignore`` is advisory — ``git add -f`` bypasses it — so this guard makes
   an accidental force-add of a real identifier-bearing data file fail CI. The
   ``samples/`` directory holds real environment data (hostnames, service
   accounts, principal handles) that must never be committed (AGENTS.md).

2. Secret-driven: when ``SWITCHBOARD_FORBIDDEN_IDENTIFIERS`` is set (a
   whitespace-separated list of real identifiers — hostnames, emails, service
   accounts, principal handles, personal names), every tracked path and blob is
   scanned as unmodified bytes, including binary files and force-added build
   directories. UTF-8 plus both UTF-16 and UTF-32 byte orders are covered.
   Gitlink names are scanned; LFS pointers fail closed because their referenced
   bytes are not present to inspect. Outside CI policy mode it is a no-op (exit
   0) until the secret is configured, so local hooks do not block a fresh clone.

   **Multi-word identifiers are double-quoted** (``"two words"``) and match any
   separator run — spaced, hyphenated, underscored, dotted, or wrapped across a
   line break. Before this, the parser split unconditionally on whitespace, so a
   multi-word identifier could not be expressed at all: its halves became short
   tokens that the length filter dropped. A real two-word work-domain name sat
   undetected in sixteen repositories — eight of them public — because of that
   blind spot. Any denylist entry containing a space must stay quoted.

3. CI policy (``--ci``): the publication declaration must be valid and a public
   repository must have a usable configured denylist. PR CI executes this copy
   from the trusted base branch while scanning contributor blobs only as data.

Run locally: python scripts/check_committed_identifiers.py
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from check_publication_plumbing import PlumbingError, Visibility, load_declaration

MIN_IDENTIFIER_LENGTH = 4
# Separators a multi-word identifier may be written with. A two-word domain name
# appears in the wild as "two words", "two-words", "two_words", "two.words", and
# — in wrapped prose — with a line break between the words. A phrase entry
# matches all of those forms; see _phrase_pattern.
_PHRASE_SEPARATOR = r"[\s._\-]+"
REPO_GATE_CAPABILITIES = frozenset(
    {
        "raw-all-blobs",
        "trusted-tree",
        "history-tree-range",
        "control-safe-messages",
        "raw-message-bytes",
        "lfs-fail-closed",
        "redacted-logs",
    }
)
# Root-level gitignored data dirs that must never contain a tracked file. The
# guard matches the first path component so a legitimate nested code dir named
# ``samples`` (e.g. ``tests/samples/``) is not a false positive.
_GUARDED_DIRS = frozenset({"samples"})
_VENV_DIR = ".venv"


@dataclass(frozen=True)
class Violation:
    identifier: str
    path: Path
    line_number: int
    line: str
    byte_offset: int | None = None
    source_commit: str | None = None


@dataclass(frozen=True)
class GitTreeFile:
    path: Path
    object_id: str
    object_type: str
    raw_path: bytes


_DECLARATION_FILENAME = "publication.toml"


class GateError(Exception):
    """A condition that prevents the gate from judging the tree.

    Raised instead of letting a traceback escape: a publication gate that cannot
    complete its scan must fail *clean* (exit 1), never look like a pass and
    never bury the reason in a stack trace.
    """


def _filter_identifiers(identifiers: frozenset[str]) -> frozenset[str]:
    """Casefold, collapse internal whitespace, drop empty or short identifiers.

    Internal whitespace is collapsed to a single space so a phrase entry is
    normalized regardless of how it was spaced in the denylist; scan_text then
    matches any separator run.
    """
    return frozenset(
        " ".join(_normalize_casefold(token).split())
        for token in (i.strip() for i in identifiers)
        if len(" ".join(token.split())) >= MIN_IDENTIFIER_LENGTH
    )


def _normalize_casefold(value: str) -> str:
    """Compatibility-normalize then casefold for canonical Unicode matching."""
    return unicodedata.normalize("NFKC", value).casefold()


def parse_identifier_set(raw: str) -> frozenset[str]:
    """Build a normalized set of identifiers from the raw denylist.

    Accepts whitespace-separated tokens (the CI-secret form) and/or one token
    per line. Full-line and trailing ``#`` comments are stripped, so a
    human-maintained denylist file may document itself without every comment
    word becoming a forbidden token.

    **Multi-word identifiers must be double-quoted** (``"two words"``). Before
    this, the parser split unconditionally on whitespace, so a multi-word
    identifier could not be *expressed* — the two halves became two short
    tokens, each dropped by the length filter. A real two-word work-domain name
    sat undetected in sixteen repositories because of that. Quoted entries are
    kept whole and matched with a flexible separator (see scan_text). The audit
    that found the leak is recorded in docs/publication-review.md.

    Raises ValueError on unbalanced quoting: a denylist we cannot parse must
    fail the gate loudly, never degrade to a partial token set.
    """
    tokens: set[str] = set()
    for line in raw.splitlines() or [raw]:
        content = line.split("#", 1)[0].strip()
        if not content:
            continue
        try:
            tokens.update(shlex.split(content))
        except ValueError as exc:  # unbalanced quote
            raise ValueError(
                f"denylist entry could not be parsed (check quoting): {exc}"
            ) from exc
    return _filter_identifiers(frozenset(tokens))


def _phrase_pattern(identifier: str) -> re.Pattern[str]:
    """Compile a multi-word identifier into a flexible-separator regex.

    Internal whitespace matches any run of whitespace, ``.``, ``_``, or ``-``,
    so one denylist entry covers the spaced, hyphenated, underscored, dotted,
    and line-wrapped spellings. Everything else is escaped literally.
    """
    parts = [re.escape(word) for word in identifier.split()]
    return re.compile(_PHRASE_SEPARATOR.join(parts))


def scan_text(text: str, identifiers: frozenset[str]) -> Iterator[Violation]:
    """Yield a violation for every occurrence of one of *identifiers*.

    The match is case-insensitive and counts any substring occurrence; real
    identifiers such as ``WORK-DOMAIN`` can legitimately appear inside longer
    tokens.

    Single-word identifiers are matched line by line. Multi-word identifiers are
    matched against the whole text with a flexible separator, so a phrase that
    prose wrapped across a line break is still caught; the reported line is the
    one the match starts on.
    """
    identifiers = _filter_identifiers(identifiers)
    if not identifiers:
        return
    words = frozenset(i for i in identifiers if " " not in i)
    phrases = frozenset(i for i in identifiers if " " in i)

    lines = text.splitlines()
    for line_number, line in enumerate(lines, start=1):
        folded = _normalize_casefold(line)
        for identifier in words:
            start = 0
            while True:
                offset = folded.find(identifier, start)
                if offset == -1:
                    break
                yield Violation(
                    identifier=identifier,
                    path=Path("."),
                    line_number=line_number,
                    line=line,
                )
                start = offset + len(identifier)

    if not phrases:
        return
    folded_text = _normalize_casefold(text)
    for identifier in phrases:
        for match in _phrase_pattern(identifier).finditer(folded_text):
            line_number = folded_text.count("\n", 0, match.start()) + 1
            yield Violation(
                identifier=identifier,
                path=Path("."),
                line_number=line_number,
                line=lines[line_number - 1] if line_number <= len(lines) else "",
            )


def _encoded_identifier_patterns(identifier: str) -> tuple[re.Pattern[bytes], ...]:
    """Patterns for an identifier's UTF-8 and UTF-16 raw-byte encodings."""
    words = identifier.split()
    patterns: list[re.Pattern[bytes]] = []
    for encoding, separator in (
        ("utf-8", rb"[\s._\-]+"),
        ("utf-16-le", rb"(?:[\x09-\x0d ._\-]\x00)+"),
        ("utf-16-be", rb"(?:\x00[\x09-\x0d ._\-])+"),
        ("utf-32-le", rb"(?:[\x09-\x0d ._\-]\x00\x00\x00)+"),
        ("utf-32-be", rb"(?:\x00\x00\x00[\x09-\x0d ._\-])+")
    ):
        encoded_words = [re.escape(word.encode(encoding)) for word in words]
        pattern = separator.join(encoded_words)
        patterns.append(re.compile(pattern, re.IGNORECASE))
    return tuple(patterns)


def scan_bytes(data: bytes, identifiers: frozenset[str]) -> Iterator[Violation]:
    """Scan unmodified bytes, including binary data, for encoded identifiers."""
    for identifier in _filter_identifiers(identifiers):
        matched_spans: list[tuple[int, int]] = []
        for pattern in _encoded_identifier_patterns(identifier):
            for match in pattern.finditer(data):
                if any(
                    match.start() < end and start < match.end()
                    for start, end in matched_spans
                ):
                    continue
                matched_spans.append(match.span())
                yield Violation(
                    identifier=identifier,
                    path=Path("."),
                    line_number=0,
                    line="",
                    byte_offset=match.start(),
                )


def scan_content(data: bytes, identifiers: frozenset[str]) -> Iterator[Violation]:
    """Combine binary byte matching with Unicode-casefold decoded matching."""
    raw_violations = list(scan_bytes(data, identifiers))
    yield from raw_violations
    raw_identifiers = {violation.identifier for violation in raw_violations}
    remaining = _filter_identifiers(identifiers) - raw_identifiers
    if not remaining:
        return
    seen: set[tuple[str, int]] = set()
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        try:
            text = data.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        for violation in scan_text(text, remaining):
            key = (violation.identifier, violation.line_number)
            if key in seen:
                continue
            seen.add(key)
            yield violation


_LFS_VERSION_LINES = frozenset(
    {
        b"version https://git-lfs.github.com/spec/v1",
        b"version https://hawser.github.com/spec/v1",
    }
)


def _is_lfs_pointer(data: bytes) -> bool:
    """Recognize current, extension-bearing, and legacy Git LFS pointers."""
    first_line = data.splitlines()[0] if data else b""
    return first_line.rstrip(b"\r") in _LFS_VERSION_LINES


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


def scan_files(
    identifiers: frozenset[str],
    paths: list[Path],
    *,
    unreadable: list[Path] | None = None,
) -> list[Violation]:
    """Scan every readable tracked file's raw bytes for forbidden identifiers.

    Returns the violations. A tracked file the gate could not read is collected
    into *unreadable* when a list is supplied (WI-027): silently skipping an
    unreadable file lets one containing a forbidden identifier pass, which is
    precisely the fails-open case this gate exists to prevent.

    The out-parameter is deliberate. This script is COPIED into every repo in the
    estate and several of them test ``scan_files`` directly, so returning a tuple
    instead of a list broke seven repositories' test suites at once. An optional
    keyword collector keeps the signature backward compatible while still letting
    the CLI fail closed on an unreadable file.
    """
    violations: list[Violation] = []
    if unreadable is None:
        unreadable = []
    for path in paths:
        for violation in scan_content(os.fsencode(path), identifiers):
            violations.append(replace(violation, path=path, line=""))
        # A tracked symlink's blob content is its target path, not file data.
        # Scan the target string without following the link: following it either
        # leaves the repo (wrong thing to scan) or fails on a broken link and
        # looks like an unreadable file. The target itself can carry a forbidden
        # identifier, so it is scanned rather than skipped.
        if path.is_symlink():
            target = os.fsencode(os.readlink(path))
            for violation in scan_content(target, identifiers):
                violations.append(replace(violation, path=path, line=""))
            continue
        try:
            data = path.read_bytes()
        except OSError:
            unreadable.append(path)
            continue
        for violation in scan_content(data, identifiers):
            violations.append(replace(violation, path=path))
        if _is_lfs_pointer(data):
            unreadable.append(path)
            continue
    return violations


def _run_git(args: list[str]) -> str:
    """Run a git command and return stdout.

    A git failure raises GateError so the gate exits 1 with a readable reason
    (WI-027): a CI gate must fail clean, not emit a CalledProcessError traceback
    that reads as an infrastructure crash rather than a blocked publication.
    """
    # argv is passed through verbatim, bare "git" included. Resolving it to an
    # absolute path via shutil.which is arguably better hygiene, but this script is
    # COPIED into every repo and several of them assert on the exact argv
    # (`== ["git", "diff", "--cached"]`), so absolute paths broke three test
    # suites. The S607 partial-path finding that motivated it only ever applied to
    # check_publication_plumbing.py, whose literal argv ruff can see statically;
    # here the list is a parameter, so the rule does not fire. Respect the
    # fleet-wide contract.
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise GateError(
            f"git command failed ({' '.join(args)}): "
            f"exit {exc.returncode}: {(exc.stderr or '').strip()}"
        ) from exc
    except OSError as exc:
        raise GateError(f"could not run git ({' '.join(args)}): {exc}") from exc
    return result.stdout


def _run_git_bytes(args: list[str]) -> bytes:
    """Run git without text framing and return stdout bytes."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise GateError(
            f"git command failed ({' '.join(args)}): exit {exc.returncode}: {stderr}"
        ) from exc
    except OSError as exc:
        raise GateError(f"could not run git ({' '.join(args)}): {exc}") from exc
    return result.stdout


def _paths_from_git(args: list[str]) -> list[Path]:
    """Run a NUL-delimited git path command and return Paths.

    No filtering is applied here — the always-on samples/ guard needs to see
    every tracked path so it can detect a force-add and scans every path.
    """
    paths: list[Path] = []
    for raw in _run_git(args).split("\0"):
        if not raw:
            continue
        paths.append(Path(raw))
    return paths


def collect_tracked_paths() -> list[Path]:
    """Return tracked file paths from ``git ls-files``, excluding obvious skips."""
    return _paths_from_git(["git", "ls-files", "-z"])


def collect_range_commits(rev_range: str) -> list[str]:
    hashes = _run_git(
        ["git", "rev-list", "--reverse", *rev_range.split()]
    ).splitlines()
    commits: list[str] = []
    for sha in hashes:
        sha = sha.strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", sha) is None:
            raise GateError("git rev-list returned an invalid commit object id")
        commits.append(sha)
    return commits


def collect_range_messages(rev_range: str) -> list[tuple[str, bytes]]:
    """Return ``(sha, message)`` for every commit in *rev_range*.

    Commit messages are a publication channel the content gate never covered:
    the tracked-tree scan reads files, so an identifier named only in a message
    is invisible to it. That blind spot is not hypothetical — a public repo
    carried work-domain identifiers in three commit messages, two of which were
    the very commits that redacted those identifiers from the files. The message
    described what the diff removed.
    """
    # rev_range may carry several git-log arguments (the pre-push new-branch case
    # passes "<sha> --not --remotes=<name>"), so it is split rather than passed
    # as one opaque argument.
    # Resolve hashes first, then read each message independently. Message bytes
    # are never used as framing, so ASCII control characters (including RS, US,
    # and NUL) cannot terminate one record or hide the next one.
    messages: list[tuple[str, bytes]] = []
    for sha in collect_range_commits(rev_range):
        commit = _run_git_bytes(["git", "cat-file", "commit", sha])
        _headers, separator, message = commit.partition(b"\n\n")
        if not separator:
            raise GateError(f"commit {sha[:9]} has no header/message separator")
        messages.append((sha, message))
    return messages


def collect_tree_files(revision: str) -> list[GitTreeFile]:
    """Return blob paths and object IDs from *revision* without checkout."""
    output = _run_git_bytes(
        ["git", "ls-tree", "-rz", "--full-tree", revision]
    )
    files: list[GitTreeFile] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            _mode, object_type, object_id = metadata.split(b" ", 2)
        except ValueError as exc:
            raise GateError("git ls-tree returned a malformed record") from exc
        files.append(
            GitTreeFile(
                path=Path(raw_path.decode("utf-8", errors="replace")),
                object_id=object_id.decode("ascii"),
                object_type=object_type.decode("ascii"),
                raw_path=raw_path,
            )
        )
    return files


def scan_tree_files(
    identifiers: frozenset[str],
    files: list[GitTreeFile],
    *,
    seen_object_ids: set[str] | None = None,
    source_commit: str | None = None,
) -> list[Violation]:
    """Scan blobs from a git tree without executing or checking out their content."""
    violations: list[Violation] = []
    if seen_object_ids is None:
        seen_object_ids = set()
    for entry in files:
        for violation in scan_content(entry.raw_path, identifiers):
            violations.append(
                replace(
                    violation,
                    path=entry.path,
                    line="",
                    source_commit=source_commit,
                )
            )
        if entry.object_type == "commit":
            for violation in scan_bytes(entry.object_id.encode("ascii"), identifiers):
                violations.append(
                    replace(
                        violation,
                        path=entry.path,
                        line="",
                        source_commit=source_commit,
                    )
                )
            continue
        if entry.object_type != "blob":
            raise GateError("git ls-tree returned an unsupported object type")
        if entry.object_id in seen_object_ids:
            continue
        seen_object_ids.add(entry.object_id)
        blob = _run_git_bytes(["git", "cat-file", "blob", entry.object_id])
        for violation in scan_content(blob, identifiers):
            violations.append(
                replace(violation, path=entry.path, source_commit=source_commit)
            )
        if _is_lfs_pointer(blob):
            path = _sanitize_path(entry.path, identifiers)
            commit = f" in commit {source_commit[:9]}" if source_commit else ""
            raise GateError(
                f"tracked LFS pointer at {path}{commit} hides content the gate cannot scan"
            )
    return violations


def scan_tree_range(
    identifiers: frozenset[str],
    rev_range: str,
) -> list[Violation]:
    """Scan each newly introduced commit tree, deduplicating repeated blob objects."""
    violations: list[Violation] = []
    seen_object_ids: set[str] = set()
    for commit in collect_range_commits(rev_range):
        files = collect_tree_files(commit)
        leaked = leaked_tracked_files(
            [entry.path for entry in files],
            _GUARDED_DIRS,
        )
        if leaked:
            path = _sanitize_path(leaked[0], identifiers)
            raise GateError(
                f"tracked guarded path {path} is present in commit {commit[:9]}"
            )
        violations.extend(
            scan_tree_files(
                identifiers,
                files,
                seen_object_ids=seen_object_ids,
                source_commit=commit,
            )
        )
    return violations


def collect_staged_paths() -> list[Path]:
    """Return staged (added/copied/modified/renamed) paths for the pre-commit hook.

    Scans only what is about to be committed rather than the whole tree, so the
    local gate is fast enough to run on every commit. Deletions are excluded
    (``--diff-filter=ACMT``) because there is nothing to scan. Type changes (T) ARE
    included: re-staging a regular file as a symlink whose target names a forbidden
    identifier is otherwise invisible to the hook. ``--no-renames``
    decomposes renames into add+delete so the new path (e.g. a file moved into
    ``samples/``) is included as an addition and caught by the always-on guard.
    """
    return _paths_from_git(
        [
            "git", "diff", "--cached", "--name-only",
            "--diff-filter=ACMT", "--no-renames", "-z",
        ]
    )


def _redact_location(value: str, identifier: str) -> str:
    pattern = (
        _phrase_pattern(identifier)
        if " " in identifier
        else re.compile(re.escape(identifier), re.IGNORECASE)
    )
    return pattern.sub("[REDACTED]", value)


def _redact_identifiers(value: str, identifiers: frozenset[str]) -> str:
    for identifier in identifiers:
        value = _redact_location(value, identifier)
        folded = _normalize_casefold(value)
        remains = (
            _phrase_pattern(identifier).search(folded) is not None
            if " " in identifier
            else _normalize_casefold(identifier) in folded
        )
        if remains:
            return "[REDACTED PATH]"
    return value


def _sanitize_controls(value: str) -> str:
    """Escape control characters so diagnostics cannot inject log commands."""
    sanitized: list[str] = []
    for char in value:
        codepoint = ord(char)
        if unicodedata.category(char).startswith("C") or char in {"\u2028", "\u2029"}:
            escape = (
                f"\\x{codepoint:02x}"
                if codepoint <= 0xFF
                else f"\\u{codepoint:04x}"
            )
            sanitized.append(escape)
        else:
            sanitized.append(char)
    return "".join(sanitized)


def _sanitize_path(path: Path, identifiers: frozenset[str]) -> str:
    return _sanitize_controls(_redact_identifiers(str(path), identifiers))


def print_report(violations: list[Violation]) -> None:
    violations.sort(key=lambda v: (str(v.path), v.line_number, v.identifier))
    identifiers = frozenset(violation.identifier for violation in violations)
    print("Committed identifier violations detected:", file=sys.stderr)
    for v in violations:
        path = _sanitize_path(v.path, identifiers)
        location = (
            f"byte offset {v.byte_offset}"
            if v.byte_offset is not None
            else f"line {v.line_number}"
        )
        commit = f"commit {v.source_commit[:9]}: " if v.source_commit else ""
        print(
            f"  {commit}{path}: {location}: forbidden identifier detected",
            file=sys.stderr,
        )
    print(f"\nTotal: {len(violations)} violation(s)", file=sys.stderr)


def leaked_tracked_files(paths: list[Path], guarded: frozenset[str]) -> list[Path]:
    """Tracked paths refused by the always-on guard.

    Guarded data dirs match only the first component, so a nested code directory
    named ``samples`` is legitimate. Anything under a ``.venv/`` directory at
    any depth is refused; a file merely named ``.venv`` is scanned normally.
    """
    return [
        p
        for p in paths
        if (p.parts and p.parts[0] in guarded) or _VENV_DIR in p.parts[:-1]
    ]


# Set by main() from --staged. In staged mode the publication verdict must come
# from the INDEX -- the bytes the commit records -- not the worktree: otherwise a
# commit that stages visibility="public" while the worktree still says
# "private-until-review" (the publication flip, exactly where this matters) is
# judged private and skipped. A one-element list so main() can set it without a
# global statement.
_DECLARATION_FROM_INDEX: list[bool] = [False]


def _staged_declaration_text() -> str | None:
    """The stage-0 index content of the declaration, or None if it is not staged.

    Absence from the index is the only None. A conflicted entry, a non-regular
    entry (symlink, submodule) or an undecodable blob is a GateError: those are
    present-but-unreadable, not "never opted in".
    """
    listing = _run_git(
        ["git", "ls-files", "--stage", "-z", "--", f":(top,literal){_DECLARATION_FILENAME}"]
    )
    entries = [e for e in listing.split("\0") if e]
    if not entries:
        return None
    if len(entries) != 1:
        raise GateError(
            f"{_DECLARATION_FILENAME} has a conflicted index entry; the gate cannot "
            "tell whether this repo is public, so it will not pass."
        )
    meta = entries[0].split("\t", 1)[0].split()
    if len(meta) != 3 or meta[2] != "0" or meta[0] not in ("100644", "100755"):
        raise GateError(
            f"{_DECLARATION_FILENAME} is staged but is not a regular file; the gate "
            "cannot tell whether this repo is public, so it will not pass."
        )
    # Bytes, decoded as UTF-8 here: _run_git decodes with the LOCALE codec, which
    # on Windows (cp1252) accepts any byte and would hide a non-UTF-8 blob.
    blob_argv = ["git", "cat-file", "blob", meta[1]]
    try:
        blob = subprocess.run(blob_argv, capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        raise GateError(
            f"could not read the staged {_DECLARATION_FILENAME} ({exc}); the gate "
            "cannot tell whether this repo is public, so it will not pass."
        ) from exc
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateError(
            f"the staged {_DECLARATION_FILENAME} is not valid UTF-8 ({exc}); the gate "
            "cannot tell whether this repo is public, so it will not pass."
        ) from exc


def _git_or_none(args: list[str]) -> str | None:
    """Return the stdout of a git command, or None when it fails (optional lookups)."""
    # No UnicodeDecodeError arm: unreachable in practice. The only caller asks
    # `rev-parse --verify -q HEAD`, whose stdout is a hex object id or nothing and
    # whose stderr -q silences. Were a decode error ever raised here it now
    # propagates and the gate exits 1. Do not translate it into GateError: that
    # would read as "no HEAD", which is the never-opted-in skip. Re-check this
    # before adding a caller.
    try:
        return _run_git(args)
    except GateError:
        return None


def _text_declares_private(text: str) -> bool:
    """True only when a declaration cleanly names "private-until-review".

    Anything else -- public, an unknown value, a missing key, unparseable text --
    is not a safe last word before a deletion: a public -> garbage -> delete
    sequence would otherwise launder a public declaration into "never opted in".
    """
    try:
        section = tomllib.loads(text).get("publication")
    except tomllib.TOMLDecodeError:
        return False
    declared = section.get("visibility") if isinstance(section, dict) else None
    if not isinstance(declared, str):
        return False
    return declared.strip().casefold() == "private-until-review"


# Every history read on the absence path ignores replace refs and the
# commit-graph cache: both rewrite the parent graph without touching a commit
# object, and the walk below would trust either. (Bare "git" first, as _run_git
# passes it.)
_HISTORY_GIT = ["git", "--no-replace-objects", "-c", "core.commitGraph=false"]


def _history_run(args: list[str], stdin: bytes | None = None) -> bytes:
    """Run a history-reading git command (see _HISTORY_GIT); a failure is a GateError."""
    try:
        return subprocess.run(
            [*_HISTORY_GIT, *args], input=stdin, capture_output=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        raise GateError(f"could not read the declaration history ({exc})") from exc


def _require_whole_history() -> None:
    """Refuse, rather than judge, a history the gate cannot see whole or trust.

    Each check closes a demonstrated exit 0 on a public repo with no denylist:
    a shallow clone (the graft read as a root; actions/checkout is shallow by
    default); a grafts file (rewrites parents); a partial/--filter clone (a
    declaration blob it does not have answers "missing", the same as no
    declaration); and objects whose bytes do not match their id or a forged
    commit-graph (git fsck verifies both; the walk itself verifies neither).
    """
    # Anything but a literal "false" (including a git too old to know the flag,
    # which echoes it back) is treated as shallow.
    if _run_git(["git", "rev-parse", "--is-shallow-repository"]).strip() != "false":
        raise GateError(
            f"{_DECLARATION_FILENAME} is absent and this is a shallow clone, so the "
            "history that decides whether it was ever declared public cannot be "
            "read; the gate will not treat it as never opted in. Fetch full "
            "history (git fetch --unshallow; in CI, actions/checkout with "
            "fetch-depth: 0), or restore the declaration."
        )
    grafts = _run_git(["git", "rev-parse", "--git-path", "info/grafts"]).strip()
    if os.path.lexists(grafts):
        raise GateError(
            f"{_DECLARATION_FILENAME} is absent and this repository has a grafts file "
            f"({grafts}), which rewrites the history that decides whether it was ever "
            "declared public; the gate will not judge it. Remove the grafts file, or "
            "restore the declaration."
        )
    listing = _history_run(["rev-list", "--objects", "--missing=print", "HEAD", "--all"])
    if any(line.startswith(b"?") for line in listing.splitlines()):
        raise GateError(
            f"{_DECLARATION_FILENAME} is absent and this clone is missing objects "
            "(a partial/--filter clone, or a damaged repository), so a missing "
            "declaration cannot be told from an unavailable one; the gate will not "
            "treat it as never opted in. Use a full clone (no --filter), or restore "
            "the declaration."
        )
    _require_verified_objects()


def _require_verified_objects() -> None:
    """Refuse an object store git fsck does not verify, or one selected by the environment.

    Runs before every absence verdict, the "unborn" one included: commits
    rewritten as blobs, a pack whose index was removed, or GIT_OBJECT_DIRECTORY
    pointed at an empty store all made a repository WITH history look like one
    with no commits (exit 0).
    """
    for var in ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        if os.environ.get(var):
            raise GateError(
                f"{_DECLARATION_FILENAME} is absent and {var} is set, so the history the "
                "gate would judge is not this repository's own object store; unset it, "
                "or restore the declaration."
            )
    # fsck with the default config, so a commit-graph present is verified too.
    fsck_argv = ["git", "--no-replace-objects", "fsck", "--full", "--no-dangling", "--no-progress"]
    try:
        subprocess.run(fsck_argv, capture_output=True, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise GateError(
            f"{_DECLARATION_FILENAME} is absent and this repository fails git fsck "
            f"({exc}); a history whose objects do not verify cannot decide whether "
            "it was ever declared public, so the gate will not treat it as never "
            "opted in. Repair the repository (or re-clone), or restore the declaration."
        ) from exc


def _absent_declaration_verdict() -> bool:
    """Verdict for a repo whose declaration is ABSENT: False, unless that is unsafe.

    Absence is the "never opted in" skip. But deleting a declaration that said
    public does not make the remote private: it only disarms the gate. So the
    whole history decides, not one log query: a commit WITH a declaration is safe
    only if it cleanly says "private-until-review"; a commit WITHOUT one is safe
    only if every parent is safe (a root commit without one is safe -- never opted
    in). The state being judged (the index or worktree with no declaration) has
    HEAD as its parent, so absence is a skip exactly when HEAD is safe -- and,
    since an orphan branch or a fresh root cut from a public repo is still that
    public repo, only when every tip of the graph of HEAD and all refs (local,
    remote-tracking, tags; not refs/stash, a local snapshot) is safe too. A tip
    is a commit no other walked commit descends from: a ref at an ANCESTOR of a
    clean private declaration is superseded by it, which is what lets a repo
    with old public-era tags leave the publication system.

    Limit, stated plainly: a lineage whose every ref was deleted locally is not
    judged. Whether its commits are still in the object database depends on
    garbage collection (git gc --prune makes the state indistinguishable from a
    repo that never had them), and counting dangling commits would refuse
    ordinary rebased-away and dropped-stash history. CI judges a fresh full
    clone, which carries the remote's refs. This covers plain
    and merge deletions, laundering through an invalid declaration, a merge that
    joins an unsafe absent lineage to a private one, and orphan branches. To leave
    the publication system, declare "private-until-review" and remove the file in
    a later commit.

    A history the gate cannot see whole or trust is refused, not judged (see
    _require_whole_history).
    """
    if _git_or_none(["git", "rev-parse", "--verify", "-q", "HEAD"]) is None:
        _require_verified_objects()
        # Genuinely unborn only when the object database holds no commit at all
        # (the first commit). A HEAD that does not resolve in a repository WITH
        # history -- a broken symref, an orphan branch, refs deleted so the
        # lineage is dangling -- would otherwise read as a safe root.
        kinds = _history_run(["cat-file", "--batch-all-objects", "--batch-check=%(objecttype)"])
        if b"commit" in kinds.split():
            raise GateError(
                f"{_DECLARATION_FILENAME} is absent and HEAD does not resolve, but this "
                "repository holds commits; the gate cannot tell whether it was ever "
                "declared public, so it will not treat it as never opted in. Restore "
                "the declaration, or check out a branch."
            )
        return False
    _require_whole_history()
    graph: dict[str, list[str]] = {}
    order: list[str] = []
    walk = _history_run(
        ["rev-list", "--topo-order", "--parents", "HEAD", "--exclude=refs/stash", "--all"]
    )
    for line in walk.decode("ascii", "replace").splitlines():
        if line.strip():
            commit, *parents = line.split()
            graph[commit] = parents
            order.append(commit)
    head = _run_git(["git", "rev-parse", "--verify", "HEAD^{commit}"]).strip()
    parented = {p for parents in graph.values() for p in parents}
    tips = [c for c in order if c not in parented]
    # %(objectmode) (git >= 2.45): a symlink or gitlink at publication.toml is
    # reported as a blob too, and its target string could read as a private
    # declaration. Only a regular file is a declaration, as on every other path.
    checked = (
        _history_run(
            ["cat-file", "--batch-check=%(objectmode) %(objecttype) %(objectname)"],
            "".join(f"{c}:{_DECLARATION_FILENAME}\n" for c in order).encode(),
        )
        .decode("utf-8", "replace")
        .splitlines()
    )
    if len(checked) != len(order):
        raise GateError("could not read the declaration history (short batch-check output)")
    present: dict[str, str | None] = {}
    for commit, row in zip(order, checked, strict=True):
        fields = row.split()
        if row.endswith(" missing"):
            present[commit] = None
        elif len(fields) != 3 or fields[0] not in ("100644", "100755") or fields[1] != "blob":
            present[commit] = ""  # present but not a regular file: never a clean private
        else:
            present[commit] = fields[2]
    blobs = sorted({oid for oid in present.values() if oid})
    private_blob: dict[str, bool] = {}
    if blobs:
        out = _history_run(["cat-file", "--batch"], "".join(f"{oid}\n" for oid in blobs).encode())
        pos = 0
        for oid in blobs:
            header_end = out.index(b"\n", pos)
            size = int(out[pos:header_end].split()[2])
            data = out[header_end + 1 : header_end + 1 + size]
            pos = header_end + 1 + size + 1
            try:
                private_blob[oid] = _text_declares_private(data.decode("utf-8"))
            except UnicodeDecodeError:
                private_blob[oid] = False
    safe: dict[str, bool] = {}
    for commit in reversed(order):  # --topo-order lists children first
        entry = present[commit]
        if entry is not None:
            safe[commit] = bool(entry) and private_blob.get(entry, False)
        else:
            safe[commit] = all(safe.get(p, True) for p in graph[commit])
    if safe.get(head, False) and all(safe[t] for t in tips):
        return False
    raise GateError(
        f"{_DECLARATION_FILENAME} is absent, but this repository's history declared a "
        'visibility other than "private-until-review" (on this branch, or on another '
        "ref) without a later clean private declaration; removing a declaration does "
        "not make the remote private, so the gate will not treat it as never opted "
        'in. Restore it, or declare "private-until-review" before removing it.'
    )


def _declares_public() -> bool:
    """True when this repo's publication.toml declares public visibility.

    Governs whether a missing denylist is a no-op or a hard failure. The
    distinction is the whole point: a private-until-review repo must stay
    clonable and committable without the secret, but a PUBLIC repo whose gate is
    unconfigured is a silent pass — the scan prints "skipping" and exits 0, and
    nothing downstream can tell that apart from a clean tree.

    Absence of the file is False (fail-open): a repo that never opted into the
    publication system is not suddenly blocked. A file that is PRESENT but
    unparseable is a GateError, not False — that repo did opt in, and guessing
    its visibility is exactly the coin-flip this function exists to remove.
    """
    try:
        repo_root = Path(_run_git(["git", "rev-parse", "--show-toplevel"]).strip())
    except GateError as exc:
        # Fail closed. This used to return False ("not public"), which turned a
        # broken or missing git into a skip in --message-file / --rev-range mode:
        # those modes return straight after the verdict, so nothing later
        # surfaced the error and a public repo exited 0 having scanned nothing.
        raise GateError(
            "could not resolve the repository root, so the gate cannot read the "
            f"publication declaration and will not pass: {exc}"
        ) from exc

    text: str | None
    if _DECLARATION_FROM_INDEX[0]:
        text = _staged_declaration_text()
        if text is None:
            return _absent_declaration_verdict()
    else:
        path = repo_root / _DECLARATION_FILENAME
        # Only genuine absence is the "never opted in" skip. A path that exists
        # but is not a regular file (a directory, or a symlink -- dangling or
        # not) used to take the same branch via `not path.is_file()`, so a
        # stray directory or link silently disarmed a public repo's gate.
        if not os.path.lexists(path):
            return _absent_declaration_verdict()
        if path.is_symlink() or not path.is_file():
            raise GateError(
                f"{_DECLARATION_FILENAME} is present but is not a regular file; the "
                "gate cannot tell whether this repo is public, so it will not pass."
            )
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise GateError(
                f"{_DECLARATION_FILENAME} is present but could not be read ({exc}); "
                "the gate cannot tell whether this repo is public, so it will not pass."
            ) from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise GateError(
            f"{_DECLARATION_FILENAME} is present but could not be parsed ({exc}); "
            "the gate cannot tell whether this repo is public, so it will not pass."
        ) from exc

    section = raw.get("publication")
    if not isinstance(section, dict):
        raise GateError(
            f"{_DECLARATION_FILENAME} has no [publication] table; the gate cannot "
            "tell whether this repo is public, so it will not pass."
        )
    # The set of legal visibilities is closed, and owned by Visibility in
    # check_publication_plumbing.py. Compare against it rather than against the
    # bare string "public": anything outside the set is a GateError, not a quiet
    # "not public".
    #
    # This used to be `str(section.get("visibility", "")).strip() == "public"`,
    # which coerced every other shape into the fail-OPEN branch. On a public repo
    # that silently disarmed the gate -- the exact "nothing was scanned and CI is
    # green" failure this function exists to prevent. A wrong-cased value
    # ("Public"), a missing visibility key, an empty string, and a non-string
    # (visibility = true) all took that branch. The declaration flip is precisely
    # the commit where this matters: a case typo or a dropped line in the
    # publication-review commit disarmed every later run.
    if "visibility" not in section:
        raise GateError(
            f"{_DECLARATION_FILENAME} has a [publication] table but no visibility "
            "key; the gate cannot tell whether this repo is public, so it will "
            "not pass."
        )
    declared = section["visibility"]
    if not isinstance(declared, str):
        raise GateError(
            f"{_DECLARATION_FILENAME} declares visibility={declared!r}, which is "
            f"{type(declared).__name__} rather than a string; the gate cannot tell "
            "whether this repo is public, so it will not pass."
        )
    normalised = declared.strip().casefold()
    if normalised == "public":
        return True
    if normalised == "private-until-review":
        return False
    raise GateError(
        f"{_DECLARATION_FILENAME} declares visibility={declared!r}, which is not "
        'one of "public" or "private-until-review"; the gate cannot tell whether '
        "this repo is public, so it will not pass."
    )


def _unconfigured(reason: str) -> None:
    """Handle a denylist that is unset or unusable.

    Returns quietly (caller no-ops) for a non-public repo; raises GateError for a
    public one.
    """
    if _declares_public():
        raise GateError(
            f"{reason} but {_DECLARATION_FILENAME} declares visibility=\"public\". "
            "A public repo with an unconfigured gate is a silent pass, so this is "
            # The env-name placeholder below sits on a line of its own. The longest
            # name in the estate is 52 characters, and folding it into a prose line
            # pushes the SUBSTITUTED file past 100 columns while the template itself
            # still looks clean. (This comment may not name the placeholder: it would
            # be substituted too, and would itself go over.)
            "a failure, not a skip. Provide the denylist via the "
            "SWITCHBOARD_FORBIDDEN_IDENTIFIERS environment variable "
            "(in CI, the secret of that name: org-level where the repo is in an "
            "org, otherwise a repo-level secret)."
        )
    print(f"{reason}; skipping identifier gate.", file=sys.stderr)


def _resolve_identifiers() -> frozenset[str] | None:
    """Return the configured denylist, or None if the gate should no-op.

    Shared by the message-scanning modes so they honor exactly the same
    configured/unconfigured semantics as the tracked-tree scan.
    """
    raw = os.environ.get("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", "")
    if not raw.strip():
        _unconfigured(
            "SWITCHBOARD_FORBIDDEN_IDENTIFIERS is empty or unset"
        )
        return None
    identifiers = parse_identifier_set(raw)
    if not identifiers:
        _unconfigured(
            "SWITCHBOARD_FORBIDDEN_IDENTIFIERS contained no usable "
            f"identifiers (minimum length is {MIN_IDENTIFIER_LENGTH} "
            "characters)"
        )
        return None
    return identifiers


def _enforce_ci_policy() -> None:
    """Fail closed when CI cannot provide a trustworthy identifier scan.

    Public repositories require a valid publication declaration and a usable
    configured denylist.
    """
    try:
        declaration = load_declaration(Path.cwd())
    except PlumbingError as exc:
        raise GateError(f"publication policy is invalid: {exc}") from exc
    if declaration is None:
        raise GateError(
            "publication.toml is missing; CI cannot determine whether the identifier "
            "gate must be configured"
        )

    if declaration.visibility is not Visibility.PUBLIC:
        return

    raw = os.environ.get("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", "")
    if not raw.strip():
        raise GateError(
            "publication.toml declares visibility=public but the configured denylist "
            "secret is empty or unset"
        )
    if not parse_identifier_set(raw):
        raise GateError(
            "publication.toml declares visibility=public but the configured denylist "
            f"secret contains no usable identifiers (minimum length is "
            f"{MIN_IDENTIFIER_LENGTH} characters)"
        )


def _report_message_violations(label: str, violations: list[Violation]) -> None:
    print(f"Forbidden identifier in {_sanitize_controls(label)}:", file=sys.stderr)
    for v in sorted(violations, key=lambda v: (v.line_number, v.identifier)):
        location = (
            f"byte offset {v.byte_offset}"
            if v.byte_offset is not None
            else f"line {v.line_number}"
        )
        print(f"  {location}: forbidden identifier detected", file=sys.stderr)
    print(
        "\nA commit message is published with the commit. Rewrite the message "
        "without the identifier (the canonical denylist is the authority on what "
        "may not appear).",
        file=sys.stderr,
    )


def _scan_message_file(path: Path) -> int:
    """commit-msg hook mode: scan the proposed commit message."""
    identifiers = _resolve_identifiers()
    if identifiers is None:
        return 0
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GateError(f"could not read the commit message file {path}: {exc}") from exc
    # git puts everything after a scissors line out of the commit; comment lines
    # are stripped too. Scan only what will actually be recorded.
    kept = [line for line in data.splitlines() if not line.startswith(b"#")]
    violations = list(scan_content(b"\n".join(kept), identifiers))
    if violations:
        _report_message_violations("the proposed commit message", violations)
        return 1
    return 0


def _scan_rev_range(rev_range: str) -> int:
    """pre-push mode: scan every commit message about to be published."""
    identifiers = _resolve_identifiers()
    if identifiers is None:
        return 0
    failed = False
    for sha, body in collect_range_messages(rev_range):
        violations = list(scan_content(body, identifiers))
        if violations:
            _report_message_violations(f"commit message {sha[:9]}", violations)
            failed = True
    return 1 if failed else 0


def _scan_index_snapshot(identifiers: frozenset[str], paths: list[Path], **kwargs: Any) -> Any:
    """``scan_files`` over the STAGED (stage-0 index) content of *paths*.

    --staged is the pre-commit hook, so it must judge the bytes the commit will
    record. Scanning the worktree let a staged forbidden identifier hide behind a
    clean unstaged copy (stage it, then overwrite the file) -- and blocked clean
    commits whose worktree held unstaged junk. The stage-0 blobs are read RAW with
    ``git cat-file --batch`` -- no smudge filter, no EOL conversion, exactly the
    bytes the commit records (``checkout-index`` applies both, so a smudge filter
    could strip a token from the scanned copy) -- written into a private temporary
    directory, and scanned by the unchanged ``scan_files``, so binary/encoding
    handling is the tree scan's. A staged symlink is recreated as a symlink to its
    staged target string, which scan_files scans without following. Reported paths
    are mapped back to repo-relative.

    Fail closed: a path with no stage-0 regular-file or symlink entry (an unmerged
    entry, a gitlink) is left out of the snapshot, which scan_files reports as
    unreadable.
    """
    repo_root = _run_git(["git", "rev-parse", "--show-toplevel"]).strip()
    wanted = {p.as_posix() for p in paths}
    entries: dict[str, tuple[str, str]] = {}
    for record in _run_git(["git", "-C", repo_root, "ls-files", "--stage", "-z"]).split("\0"):
        if not record or "\t" not in record:
            continue
        meta, name = record.split("\t", 1)
        mode, oid, stage = meta.split()
        if name in wanted and stage == "0" and mode in ("100644", "100755", "120000"):
            entries[name] = (mode, oid)
    blobs: dict[str, bytes] = {}
    if entries:
        names = sorted(entries)
        request = "".join(f"{entries[n][1]}\n" for n in names).encode()
        # Bare "git", as _run_git passes it: the argv is a variable, as there.
        batch_argv = ["git", "-C", repo_root, "cat-file", "--batch"]
        try:
            proc = subprocess.run(
                batch_argv,
                input=request,
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            raise GateError(
                f"could not read the staged content to scan it ({exc}); the gate "
                "will not pass a commit it could not fully scan."
            ) from exc
        out, pos = proc.stdout, 0
        for name in names:
            header_end = out.index(b"\n", pos)
            header = out[pos:header_end].split()
            if len(header) != 3 or header[1] != b"blob":
                raise GateError(
                    f"could not read the staged blob for {name!r}; the gate will not "
                    "pass a commit it could not fully scan."
                )
            size = int(header[2])
            blobs[name] = out[header_end + 1 : header_end + 1 + size]
            pos = header_end + 1 + size + 1
    with tempfile.TemporaryDirectory(prefix="identifier-gate-index-") as tmp:
        base = Path(tmp)
        for name, data in blobs.items():
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if entries[name][0] == "120000":
                    os.symlink(os.fsdecode(data), target)
                else:
                    target.write_bytes(data)
            except OSError as exc:
                raise GateError(
                    f"could not materialise the staged entry {name!r} to scan it ({exc}); "
                    "the gate will not pass a commit it could not fully scan."
                ) from exc

        def back(p: Path) -> Path:
            try:
                return Path(p).relative_to(base)
            except ValueError:
                return Path(p)

        unreadable = kwargs.get("unreadable")
        result = scan_files(identifiers, [base / p for p in paths], **kwargs)
        if isinstance(unreadable, list):
            unreadable[:] = [back(p) for p in unreadable]
        if isinstance(result, tuple):
            found, missed = result
            return [replace(v, path=back(v.path)) for v in found], [back(p) for p in missed]
        return [replace(v, path=back(v.path)) for v in result]


def _run(args: argparse.Namespace) -> int:
    if args.ci:
        _enforce_ci_policy()
    if args.message_file is not None:
        return _scan_message_file(Path(args.message_file))
    if args.rev_range is not None:
        return _scan_rev_range(args.rev_range)
    if args.tree_range is not None:
        identifiers = _resolve_identifiers()
        if identifiers is None:
            return 0
        violations = scan_tree_range(identifiers, args.tree_range)
        if violations:
            print_report(violations)
            return 1
        return 0

    tree_files = collect_tree_files(args.tree) if args.tree is not None else None
    paths = (
        [entry.path for entry in tree_files]
        if tree_files is not None
        else (collect_staged_paths() if args.staged else collect_tracked_paths())
    )
    raw = os.environ.get("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", "")
    report_identifiers = parse_identifier_set(raw) if raw.strip() else frozenset()

    # 1. Always-on: no tracked file under a guarded (gitignored) data dir. This
    #    catches a ``git add -f samples/...`` leak regardless of secret config.
    leaked = leaked_tracked_files(paths, _GUARDED_DIRS)
    if leaked:
        print("Tracked files under a forbidden data directory detected:", file=sys.stderr)
        for p in sorted(leaked, key=str):
            print(f"  {_sanitize_path(p, report_identifiers)}", file=sys.stderr)
        print(
            "\nThese paths are not source: samples/ holds real identifier-bearing "
            "data, and anything under .venv/ is generated environment content. "
            "Remove them from the index: git rm --cached -r <path>.",
            file=sys.stderr,
        )
        return 1

    # 2. Secret-driven: scan tracked text files (outside guarded dirs) for
    #    forbidden identifiers. No-op until the secret is configured.
    if not raw.strip():
        _unconfigured(
            "SWITCHBOARD_FORBIDDEN_IDENTIFIERS is empty or unset"
        )
        return 0

    identifiers = parse_identifier_set(raw)
    if not identifiers:
        _unconfigured(
            "SWITCHBOARD_FORBIDDEN_IDENTIFIERS contained no usable "
            f"identifiers (minimum length is {MIN_IDENTIFIER_LENGTH} "
            "characters)"
        )
        return 0

    unreadable: list[Path] = []
    if tree_files is not None:
        violations = scan_tree_files(identifiers, tree_files)
    else:
        # --staged judges the index blobs (what the commit records), never the worktree.
        scan = _scan_index_snapshot if args.staged else scan_files
        violations = scan(identifiers, paths, unreadable=unreadable)
    if violations:
        print_report(violations)
        return 1
    if unreadable:
        print("Tracked files could not be read; the gate cannot clear them:", file=sys.stderr)
        for p in sorted(unreadable, key=str):
            print(f"  {_sanitize_path(p, identifiers)}", file=sys.stderr)
        print(
            "\nAn unreadable tracked file may contain a forbidden identifier. Fix the "
            "permissions (or untrack the file) and re-run; the gate will not pass a "
            "tree it could not fully scan.",
            file=sys.stderr,
        )
        return 1

    return 0


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
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Enforce publication and trusted-event policy before scanning in CI.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--message-file",
        metavar="PATH",
        help="Scan a proposed commit message (for the commit-msg hook) instead "
        "of files. Comment lines are ignored, as git strips them.",
    )
    mode.add_argument(
        "--rev-range",
        metavar="RANGE",
        help="Scan the commit messages in a git rev range (for the pre-push "
        "hook), e.g. origin/main..HEAD.",
    )
    mode.add_argument(
        "--tree",
        metavar="REVISION",
        help="Scan tracked blobs from a git tree without checking them out.",
    )
    mode.add_argument(
        "--tree-range",
        metavar="RANGE",
        help="Scan every commit tree in a git revision range, deduplicating blobs.",
    )
    args = parser.parse_args(argv)
    _DECLARATION_FROM_INDEX[0] = bool(args.staged)

    try:
        return _run(args)
    except GateError as exc:
        raw = os.environ.get("SWITCHBOARD_FORBIDDEN_IDENTIFIERS", "")
        try:
            identifiers = parse_identifier_set(raw) if raw.strip() else frozenset()
        except ValueError:
            identifiers = frozenset()
        message = _sanitize_controls(_redact_identifiers(str(exc), identifiers))
        print(f"identifier gate could not complete: {message}", file=sys.stderr)
        return 1
    except ValueError as exc:
        # Unparseable denylist (bad quoting). Fail closed, loudly.
        print(f"identifier gate denylist is invalid: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
