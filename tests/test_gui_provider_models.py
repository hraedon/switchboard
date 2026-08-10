"""Run the provider-model enumeration + auto-match GUI harness under pytest
(Plan 024 WI-2/WI-3).

The management forms fetch from /admin/providers/<name>/models and POST to
/admin/model-map, so their behaviour needs pinning: datalist population, scan
offer detection, merge-on-apply (not replace), and failure surfacing.
The assertions live in `tests/gui/provider_models.mjs`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "gui" / "provider_models.mjs"


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; the GUI harness needs a JS runtime",
)
def test_provider_models_management_behaviour() -> None:
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
