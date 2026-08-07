"""Run the dashboard GUI harness under pytest (Plan 020 WI-8).

The default-route editor is the first control on the dashboard that WRITES,
so its behaviour needs pinning: a poll landing mid-edit must not discard the
operator's typing, a 400 must surface the provider name the server rejected,
and provider names must be escaped rather than injected.

The assertions live in `tests/gui/default_route.mjs` because they exercise
JavaScript. This wrapper exists so they run in the normal `pytest` sweep
rather than rotting as a script nobody remembers to invoke.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "gui" / "default_route.mjs"


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; the GUI harness needs a JS runtime",
)
def test_default_route_editor_behaviour() -> None:
    result = subprocess.run(
        ["node", str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(
            "GUI harness reported failures:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    # Guard against the harness silently doing nothing — an exit code of 0
    # with no checks run would otherwise read as a pass.
    assert "checks passed" in result.stdout, result.stdout
    assert "FAIL" not in result.stdout, result.stdout
