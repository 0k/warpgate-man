"""Command-line interface for warpgate-man.

Usage::

    warpgate-man --config wgman.yaml diff
    warpgate-man --config wgman.yaml apply
    warpgate-man --config wgman.yaml apply --prune
    warpgate-man --config wgman.yaml --server prod apply
    warpgate-man fetch
    warpgate-man fetch --odoo-url https://odoo.example.com --odoo-db mydb \
        --odoo-user admin

Exit codes:
    0  success
    1  runtime / API error
    2  usage / configuration error
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

import yaml

from . import __version__
from .config import Config, load_config
from .exceptions import ConfigError, WgmanError
from .manager import WarpgateManager
from .models import ServerConfig
from typing import Any

from .odoo import OdooConfig, OdooTargetsConfig, fetch_state

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
    parser.add_argument(
        "--odoo-url",
        metavar="URL",
        help="Odoo server URL (overrides the config 'odoo:' section)",
    )
    parser.add_argument(
        "--odoo-db",
        metavar="DB",
        help="Odoo database name (overrides the config 'odoo:' section)",
    )
    parser.add_argument(
        "--odoo-user",
        metavar="LOGIN",
        help="Odoo login (overrides the config 'odoo:' section)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "fetch",
        help=(
            "query Odoo for SSH targets and print them as YAML "
            "(the desired state that diff/apply would use)"
        ),
    )

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


def _resolve_odoo_config(
    config: Config, args: argparse.Namespace
) -> OdooConfig | None:
    """The effective Odoo source: config 'odoo:' section + CLI overrides.

    Returns ``None`` when neither the config nor the CLI define an Odoo
    source. Raises :class:`ConfigError` when the definition is incomplete.
    """
    overrides = {
        "url": args.odoo_url,
        "db": args.odoo_db,
        "user": args.odoo_user,
    }
    if config.odoo is None and not any(overrides.values()):
        return None

    base = config.odoo
    url = overrides["url"] or (base.url if base else None)
    db = overrides["db"] or (base.db if base else None)
    user = overrides["user"] or (base.user if base else None)
    missing = [k for k, v in (("url", url), ("db", db), ("user", user))
               if not v]
    if missing or url is None or db is None or user is None:
        raise ConfigError(
            "incomplete Odoo source definition: missing "
            + ", ".join(f"--odoo-{k}" for k in missing)
            + " (or the matching keys in the config 'odoo:' section)"
        )
    return OdooConfig(
        url=url,
        db=db,
        user=user,
        password=base.password if base else None,
        verify_tls=base.verify_tls if base else True,
        targets=base.targets if base else OdooTargetsConfig(),
        users=base.users if base else [],
    )


def _ensure_odoo_password(odoo: OdooConfig) -> None:
    """Prompt interactively for the Odoo password when not configured."""
    if odoo.password and "${" in odoo.password:
        # Un-interpolated ${ENV} reference kept by lenient loading: the
        # variable is unset, so treat the password as absent.
        odoo.password = None
    if odoo.password:
        return
    if not sys.stdin.isatty():
        raise ConfigError(
            "no Odoo password in config and stdin is not a TTY: set the "
            "'password' key in the 'odoo:' section (use ${ENV} for secrets)"
        )
    odoo.password = getpass.getpass(
        f"Odoo password for {odoo.user} on {odoo.url} (db {odoo.db}): "
    )


def _load_odoo_state(config: Config, args: argparse.Namespace) -> bool:
    """Fetch Odoo state (if a source is defined) and merge into *config*.

    Returns True when an Odoo source was used.
    """
    odoo = _resolve_odoo_config(config, args)
    if odoo is None:
        return False
    _ensure_odoo_password(odoo)
    state = fetch_state(odoo)
    config.merge_targets(state.targets)
    config.merge_users(state.users)
    config.merge_roles(state.roles)
    return True


def _run_fetch(config: Config, args: argparse.Namespace) -> int:
    odoo = _resolve_odoo_config(config, args)
    if odoo is None:
        print(
            "error: no Odoo source: add an 'odoo:' section to the config "
            "or pass --odoo-url/--odoo-db/--odoo-user",
            file=sys.stderr,
        )
        return EXIT_USAGE
    _ensure_odoo_password(odoo)
    state = fetch_state(odoo)
    listing: dict[str, Any] = {
        "targets": [t.to_config_dict() for t in state.targets],
    }
    if state.roles:
        listing["roles"] = [{"name": r.name} for r in state.roles]
    if state.users:
        users_listing = []
        for u in state.users:
            entry: dict[str, Any] = {"name": u.name}
            if u.roles:
                entry["roles"] = list(u.roles)
            if u.public_keys:
                entry["public-keys"] = list(u.public_keys)
            users_listing.append(entry)
        listing["users"] = users_listing
    print(
        yaml.safe_dump(
            listing,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        ),
        end="",
    )
    return EXIT_OK


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
        # ``fetch`` does not talk to Warpgate: tolerate unset ${ENV}
        # references (e.g. the Warpgate api-key) in the config.
        config = load_config(
            args.config, strict_env=args.command != "fetch"
        )
    except ConfigError as exc:
        # ``fetch`` can run without any config file when the Odoo source is
        # fully defined on the command line.
        if (
            args.command == "fetch"
            and args.config is None
            and args.odoo_url
            and args.odoo_db
            and args.odoo_user
        ):
            config = Config()
        else:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE

    if args.command == "fetch":
        try:
            return _run_fetch(config, args)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except WgmanError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        except KeyboardInterrupt:
            print("aborted", file=sys.stderr)
            return EXIT_ERROR

    try:
        servers = _select_servers(config, args.server)
        _load_odoo_state(config, args)
        config.validate()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except WgmanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

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
