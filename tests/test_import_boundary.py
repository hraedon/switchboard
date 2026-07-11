"""Import boundary test: control.py imports stdlib only, shell imports control one-way."""

from __future__ import annotations

import importlib
import sys


def test_control_imports_stdlib_only() -> None:
    """switchboard.control must not import anything outside the stdlib."""
    importlib.import_module("switchboard.control")
    for name, module in sys.modules.items():
        if module is None:
            continue
        if not name.startswith("switchboard.control"):
            continue
        # Check all modules imported by control
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if hasattr(attr, "__module__") and attr.__module__:
                mod_name = attr.__module__
                if mod_name.startswith("_") or mod_name.startswith("switchboard.control"):
                    continue
                # Allow stdlib modules
                if mod_name in sys.builtin_module_names:
                    continue
                # Check if it's a stdlib module
                spec = importlib.util.find_spec(mod_name.split(".")[0])
                if spec is not None and spec.origin:
                    stdlib_path = spec.origin
                    if "site-packages" in stdlib_path or "dist-packages" in stdlib_path:
                        raise AssertionError(
                            f"switchboard.control imports non-stdlib module: {mod_name}"
                        )


def test_control_no_httpx() -> None:
    """control must not import httpx."""
    mod = importlib.import_module("switchboard.control")
    assert not hasattr(mod, "httpx"), "control must not import httpx"


def test_control_no_asyncio() -> None:
    """control must not import asyncio."""
    mod = importlib.import_module("switchboard.control")
    assert not hasattr(mod, "asyncio"), "control must not import asyncio"
