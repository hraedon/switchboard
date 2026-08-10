"""Run the quarantine dashboard panel GUI harness under pytest
(Plan 023 WI-4).

The dashboard renders quarantined provider/model pairs with their evidence
and a Release button that calls DELETE /admin/quarantine/<provider>/<model>.
The assertions live in `tests/gui/quarantine_panel.mjs`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "gui" / "quarantine_panel.mjs"


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; the GUI harness needs a JS runtime",
)
def test_quarantine_panel_management_behaviour() -> None:
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
    assert "checks passed" in result.stdout, result.stdout
    assert "FAIL" not in result.stdout, result.stdout
