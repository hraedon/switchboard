"""Clean wheel install test — Plan 006 §6.

Verifies that switchboard can be built as a wheel and installed into a
fresh virtual environment without relying on the editable install or
sibling source directories.  The test builds wheels for both switchboard
and sluice (sluice is not yet on PyPI at the required version), creates a
clean venv, installs both, and smoke-tests import, ``--version``, and
``build_parser()``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SLUICE_SOURCE_DIR = Path(os.environ.get("SLUICE_SOURCE_DIR", str(_PROJECT_ROOT.parent / "sluice")))


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd* with capture_output and text mode, asserting success."""
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def test_clean_wheel_install(tmp_path: Path) -> None:
    """Build a wheel, install in a clean venv, smoke-test the package."""
    if not _SLUICE_SOURCE_DIR.is_dir():
        pytest.skip(f"sluice source not found at {_SLUICE_SOURCE_DIR}")

    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()

    # --- Build wheels -------------------------------------------------------
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(_PROJECT_ROOT),
            "--no-deps",
            "-w",
            str(wheel_dir),
        ],
    )
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(_SLUICE_SOURCE_DIR),
            "--no-deps",
            "-w",
            str(wheel_dir),
        ],
    )

    sb_wheels = sorted(wheel_dir.glob("switchboard-*.whl"))
    sluice_wheels = sorted(wheel_dir.glob("sluice-*.whl"))
    assert len(sb_wheels) == 1, f"expected 1 switchboard wheel, found {sb_wheels}"
    assert len(sluice_wheels) == 1, f"expected 1 sluice wheel, found {sluice_wheels}"

    # --- Create clean venv -------------------------------------------------
    venv_dir = tmp_path / "venv"
    _run([sys.executable, "-m", "venv", str(venv_dir)])

    venv_python = venv_dir / "bin" / "python"
    venv_bin = venv_dir / "bin"

    # --- Install both wheels (pip resolves cross-deps + pulls httpx/uvicorn) -
    _run(
        [str(venv_python), "-m", "pip", "install", str(sb_wheels[0]), str(sluice_wheels[0])],
    )

    # --- 1. import switchboard ---------------------------------------------
    result = _run(
        [str(venv_python), "-c", "import switchboard; print(switchboard.__version__)"],
    )
    assert result.stdout.strip() == "0.1.0"

    # --- 2. switchboard --version ------------------------------------------
    result = _run([str(venv_bin / "switchboard"), "--version"])
    assert "0.1.0" in result.stdout

    # --- 3. build_parser() works ------------------------------------------
    _run(
        [
            str(venv_python),
            "-c",
            "from switchboard.cli import build_parser; p = build_parser(); assert p is not None",
        ],
    )
