"""``switchboard`` command-line entry point.

``switchboard serve`` runs the multi-provider routing proxy.

Config precedence: flags → environment variables → config file → built-in defaults.
"""

from __future__ import annotations

import argparse
import logging
import sys

from switchboard import __version__

log = logging.getLogger("switchboard.cli")

_ENV_PREFIX = "SWITCHBOARD_"

_DEFAULTS: dict[str, object] = {
    "listen": "127.0.0.1:8801",
    "log_level": "INFO",
    "config": None,
    "admin_token": None,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchboard",
        description="Multi-provider routing proxy for LLM APIs.",
    )
    parser.add_argument("--version", action="version", version=f"switchboard {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the routing proxy")
    serve.add_argument("--listen", default=None, help="host:port (default: 127.0.0.1:8801)")
    serve.add_argument("--config", default=None, help="path to TOML config file")
    serve.add_argument("--admin-token", default=None, help="token gating admin routes")
    serve.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        print("switchboard serve — not yet implemented (see plans/001)", file=sys.stderr)
        return 1
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
