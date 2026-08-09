"""Run the routing-strategy GUI harness under pytest (Plan 020 WI-14).

The strategy editor changes how every request is routed, live, from a browser.
That makes it the highest-consequence writing control on the dashboard, so the
behaviours worth pinning are the ones an operator would be misled by: a poll
landing mid-edit discarding their typing, a rejected value showing a bare
status instead of the server's reason, the pace panel appearing when pace is
not selected, or a stale weekly reading being scored in the surplus ranking
when the routing core would refuse to score it.

The assertions live in `tests/gui/routing_config.mjs` because they exercise
JavaScript. This wrapper exists so they run in the normal `pytest` sweep
rather than rotting as a script nobody remembers to invoke.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "gui" / "routing_config.mjs"


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; the GUI harness needs a JS runtime",
)
def test_routing_config_editor_behaviour() -> None:
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
