"""Run the Routing Explain GUI harness under pytest (Plan 026 W1.6).

The card answers "who would serve model X right now, and why". Two failure
modes make it worse than nothing: the 5 s poll wiping an answer mid-read (so
the operator re-asks and gets a different estate), and a server-derived
provider name rendering as markup. Both are pinned in the harness.

The assertions live in `tests/gui/route_explain.mjs` because they exercise
JavaScript. This wrapper exists so they run in the normal `pytest` sweep rather
than rotting as a script nobody remembers to invoke.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "gui" / "route_explain.mjs"


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; the GUI harness needs a JS runtime",
)
def test_route_explain_card_behaviour() -> None:
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
