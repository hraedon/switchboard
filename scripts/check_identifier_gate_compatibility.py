"""Fail closed if an external template sync removed repo-required gate features."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REQUIRED = frozenset(
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


def main() -> int:
    scanner = Path(__file__).with_name("check_committed_identifiers.py")
    spec = importlib.util.spec_from_file_location("switchboard_identifier_scanner", scanner)
    if spec is None or spec.loader is None:
        print("identifier gate compatibility check could not load the scanner", file=sys.stderr)
        return 1
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        print(
            "identifier gate compatibility check could not import the scanner: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    actual = getattr(module, "REPO_GATE_CAPABILITIES", frozenset())
    missing = REQUIRED - frozenset(actual)
    if missing:
        print(
            "identifier gate scanner is incompatible with this repository. "
            "An external sync may have installed a stale template; update that "
            "template to preserve Switchboard's repository-required capabilities "
            "before syncing again.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
