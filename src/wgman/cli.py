"""Command-line interface for warpgate-man.

Usage::

    warpgate-man --config wgman.yaml diff
    warpgate-man --config wgman.yaml apply
    warpgate-man --config wgman.yaml apply --prune
    warpgate-man --config wgman.yaml --server prod apply

Exit codes:
    0  success
    1  runtime / API error
    2  usage / configuration error
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .config import Config, load_config
from .exceptions import ConfigError, WgmanError
from .manager import WarpgateManager
from .models import ServerConfig

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

ENV_CONFIG = "WARPGATE_MAN_CONFIG"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warpgate-man",
        description=(
            "Declarative manager for Warpgate bastion configuration. "
            "Reconciles targets, target-groups, users and roles from a YAML "
            "file."
        ),
        epilog=(
            "Examples:\n"
            "  warpgate-man --config wgman.yaml diff\n"
            "  warpgate-man --config wgman.yaml apply\n"
            "  warpgate-man --config wgman.yaml apply --prune\n"
            "  warpgate-man --config wgman.yaml --server prod apply\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="FILE",
        default=os.environ.get(ENV_CONFIG),
        help=(
            "path to the YAML configuration file "
            f"(env: {ENV_CONFIG}; default: search wgman.yaml, "
            "~/.config/wgman/config.yaml, /etc/wgman/config.yaml)"
        ),
    )
    parser.add_argument(
        "-s",
        "--server",
        metavar="NAME",
        help=(
            "only act on this named server from the config "
            "(default: all servers)"
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    diff_p = sub.add_parser(
        "diff",
        help="show what would change, without writing anything",
    )
    diff_p.add_argument(
        "--prune",
        action="store_true",
        help="also show server-side entities that would be deleted",
    )

    apply_p = sub.add_parser(
        "apply",
        help="create and update entities to match the config",
    )
    apply_p.add_argument(
        "--prune",
        action="store_true",
        help="also delete server-side entities absent from the config",
    )

    return parser


def _select_servers(config: Config, name: str | None) -> list[ServerConfig]:
    if name is not None:
        return [config.server(name)]
    if not config.servers:
        raise ConfigError("config defines no servers")
    return config.servers


def _run_for_server(
    server: ServerConfig, config: Config, *, apply: bool, prune: bool
) -> bool:
    """Run diff/apply for one server. Returns True if there were changes."""
    header = f"== {server.name} ({server.url}) =="
    print(header, file=sys.stderr)
    with WarpgateManager.from_server(server) as mgr:
        plan = mgr.reconcile_config(config, prune=prune, dry_run=not apply)
        print(plan)
    return not plan.empty


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        servers = _select_servers(config, args.server)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    apply = args.command == "apply"
    prune = bool(getattr(args, "prune", False))

    try:
        any_changes = False
        for server in servers:
            changed = _run_for_server(
                server, config, apply=apply, prune=prune
            )
            any_changes = any_changes or changed
    except WgmanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return EXIT_ERROR

    if not apply and any_changes:
        print(
            "\nRun 'apply' to perform these changes.", file=sys.stderr
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
