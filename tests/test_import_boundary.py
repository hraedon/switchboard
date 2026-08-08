"""Import boundary tests.

Enforces:
- control.py imports stdlib only (no I/O, no clock, no network)
- shell modules import control one-way (control never imports shell)
- no module imports sluice at all (Plan 018 removed the dependency; the
  public PyPI package of that name is an unrelated project)
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path


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


def test_proxy_imports_control() -> None:
    """switchboard.proxy must import switchboard.control (one-way dependency)."""
    proxy = importlib.import_module("switchboard.proxy")
    assert hasattr(proxy, "route_decision"), "proxy must import route_decision from control"
    assert hasattr(proxy, "RoutingConfig"), "proxy must import RoutingConfig from control"
    assert hasattr(proxy, "hash_route_key"), "proxy must import hash_route_key from control"


def test_admin_does_not_import_proxy() -> None:
    """switchboard.admin must not import switchboard.proxy (avoid circular)."""
    importlib.import_module("switchboard.admin")
    import switchboard.admin
    assert not hasattr(switchboard.admin, "ProxyApp"), \
        "admin must not import ProxyApp from proxy"


def _collect_sluice_imports(src_dir: Path) -> list[tuple[str, str, int]]:
    """Walk all .py files under src_dir, return (file, name, line) for every
    name imported from a sluice.* module."""
    results: list[tuple[str, str, int]] = []
    for py_file in sorted(src_dir.rglob("*.py")):
        rel = str(py_file.relative_to(src_dir.parent))
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("sluice"):
                    for alias in node.names:
                        results.append((rel, alias.name, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("sluice"):
                        results.append((rel, alias.name, node.lineno))
    return results


def test_no_sluice_imports_at_all() -> None:
    """Plan 018 removed the sluice dependency entirely. Any import of a
    sluice.* module — in src or tests — is a regression: the public PyPI
    package of that name is an unrelated project, so a reintroduced import
    would resolve to a stranger's code."""
    root = Path(__file__).resolve().parent.parent
    offenders = [
        f"{rel}:{line} imports '{name}' from sluice"
        for tree_dir in (root / "src" / "switchboard", root / "tests")
        for rel, name, line in _collect_sluice_imports(tree_dir)
    ]
    assert not offenders, (
        "sluice was removed in Plan 018 but is imported again:\n"
        + "\n".join(offenders)
    )


def test_pure_modules_import_stdlib_only() -> None:
    """The pure cores are stdlib-only, no I/O, no clock, no network.

    AST-scanned (robust against the ``dir()``-based check's blind spot for
    module objects) for: control, limit, overload, threshold, budget. Each
    must import only stdlib modules — never httpx/asyncio/sluice and never
    another switchboard submodule (the shell imports the core one-way).
    """
    root = Path(__file__).resolve().parent.parent / "src" / "switchboard"
    pure_modules = ("control", "limit", "overload", "threshold", "budget", "speed")
    forbidden_prefixes = ("httpx", "sluice", "switchboard")
    for mod_name in pure_modules:
        src = (root / f"{mod_name}.py").read_text()
        tree = ast.parse(src, filename=f"{mod_name}.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported = node.module
                assert not imported.startswith(forbidden_prefixes), (
                    f"switchboard.{mod_name} imports non-stdlib/shell "
                    f"module: {imported}"
                )
                _assert_stdlib(imported, mod_name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported = alias.name
                    assert not imported.startswith(forbidden_prefixes), (
                        f"switchboard.{mod_name} imports non-stdlib/shell "
                        f"module: {imported}"
                    )
                    _assert_stdlib(imported, mod_name)


def _assert_stdlib(imported: str, mod_name: str) -> None:
    """Assert a top-level module name is part of the stdlib."""
    top = imported.split(".")[0]
    stdlib_names = getattr(sys, "stdlib_module_names", None)
    if stdlib_names is not None:
        assert top in stdlib_names or top in sys.builtin_module_names, (
            f"switchboard.{mod_name} imports non-stdlib module: {imported}"
        )
        return
    # Python < 3.10 fallback: resolve by location.
    spec = importlib.util.find_spec(top)
    if spec is not None and spec.origin and (
        "site-packages" in spec.origin or "dist-packages" in spec.origin
    ):
        raise AssertionError(
            f"switchboard.{mod_name} imports non-stdlib module: {imported}"
        )
