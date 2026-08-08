"""Run the Add-Provider form GUI harness under pytest (Plan 021 WI-5).

The provider form writes (POST /admin/providers, POST /admin/providers/discover),
so its behaviour needs pinning: a poll landing mid-edit must not discard the
form, the registry pick must prefill, the save must POST the right payload, a
rejection must surface the server message, and names must be escaped.

The assertions live in `tests/gui/providers_form.mjs`. This wrapper runs them
in the normal `pytest` sweep.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "gui" / "providers_form.mjs"


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; the GUI harness needs a JS runtime",
)
def test_providers_form_behaviour() -> None:
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
