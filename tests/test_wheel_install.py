"""Clean wheel install test — Plan 006 §6, simplified by Plan 018.

Verifies that switchboard can be built as a wheel and installed into a
fresh virtual environment without relying on the editable install: builds
the wheel, creates a clean venv, installs it, and smoke-tests import,
``--version``, and ``build_parser()``.

Since Plan 018 removed the private ``sluice`` dependency, the install must
also NOT pull in anything named ``sluice`` — the public PyPI package of
that name is an unrelated project, so its appearance here would mean a
dependency regression or a supply-chain surprise.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd* with capture_output and text mode, asserting success."""
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def test_clean_wheel_install(tmp_path: Path) -> None:
    """Build a wheel, install in a clean venv, smoke-test the package."""
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()

    # --- Build the wheel ----------------------------------------------------
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

    sb_wheels = sorted(wheel_dir.glob("switchboard-*.whl"))
    assert len(sb_wheels) == 1, f"expected 1 switchboard wheel, found {sb_wheels}"

    # --- Create clean venv -------------------------------------------------
    venv_dir = tmp_path / "venv"
    _run([sys.executable, "-m", "venv", str(venv_dir)])

    venv_python = venv_dir / "bin" / "python"
    venv_bin = venv_dir / "bin"

    # --- Install the wheel (pip pulls httpx/uvicorn from the index) ---------
    _run([str(venv_python), "-m", "pip", "install", str(sb_wheels[0])])

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

    # --- 4. nothing named sluice came along --------------------------------
    result = _run(
        [
            str(venv_python),
            "-c",
            (
                "import importlib.metadata as m\n"
                "try:\n"
                "    v = m.distribution('sluice').version\n"
                "except m.PackageNotFoundError:\n"
                "    print('absent')\n"
                "else:\n"
                "    print(v)\n"
            ),
        ],
    )
    assert result.stdout.strip() == "absent", (
        "the sluice dependency was removed in Plan 018, but "
        f"sluice {result.stdout.strip()} arrived in a clean install"
    )
