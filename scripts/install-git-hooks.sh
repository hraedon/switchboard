#!/usr/bin/env bash
# Point this clone's git hooks at the tracked githooks/ directory.
set -euo pipefail
repo_root="$(git rev-parse --show-toplevel)"
chmod +x "$repo_root/githooks/pre-commit"
git -C "$repo_root" config core.hooksPath githooks
echo "core.hooksPath -> githooks (pre-commit identifier gate active)"
echo "Next: provide a denylist via \$SWITCHBOARD_FORBIDDEN_IDENTIFIERS or"
echo "      .identifiers-denylist.local (gitignored)."
