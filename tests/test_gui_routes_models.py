"""Run the keyed-route + model-map management GUI harness under pytest
(Plan 020 WI-7/WI-8).

The management forms write (POST/DELETE /admin/routes, /admin/model-map), so
their behaviour needs pinning: freeze-on-edit, payload shape, error surfacing,
delete wiring, and escaping of attacker-influenced keys/model names.

The assertions live in `tests/gui/routes_models.mjs`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "gui" / "routes_models.mjs"


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; the GUI harness needs a JS runtime",
)
def test_routes_models_management_behaviour() -> None:
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
