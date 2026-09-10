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

from . import __version__, access, odoo, sshconfig
from .client import WarpgateUserClient
from .config import Config, load_config
from .exceptions import ConfigError, WgmanError
from .manager import WarpgateManager
from .models import ServerConfig, User
from .sshconfig import BastionInfo
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
        help=(
            "Odoo database name (overrides the config 'odoo:' section). "
            "Optional: when omitted the server is asked, which succeeds "
            "when it hosts exactly one database"
        ),
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

    ssh_p = sub.add_parser(
        "ssh-config",
        help=(
            "print an ssh client configuration for the targets your token "
            "can reach"
        ),
        description=(
            "Ask each configured server which targets the api-key's user "
            "may reach, and print matching ssh_config(5) stanzas. The "
            "connection goes through the bastion, so the emitted 'User' is "
            "the '<user>:<target>' selector, never the backend account. "
            "Needs a personal API token; the server's global admin token is "
            "bound to no user and is refused."
        ),
    )
    ssh_p.add_argument(
        "--from-odoo",
        action="store_true",
        help=(
            "build the config from Odoo instead of asking Warpgate: "
            "authenticate to Odoo as yourself and render the targets your "
            "roles grant. Needs no Warpgate API token, only the bastion "
            "address (--bastion, or 'ssh-host' in the config)"
        ),
    )
    ssh_p.add_argument(
        "--bastion",
        metavar="HOST[:PORT]",
        help=(
            "the bastion's ssh address, for --from-odoo "
            f"(default port {sshconfig.DEFAULT_SSH_PORT}; overrides the "
            "config's 'ssh-host'/'ssh-port')"
        ),
    )
    ssh_p.add_argument(
        "--prefix",
        metavar="STR",
        help=(
            "prepend STR to every Host alias (default: none for a single "
            "server, '<server>-' when several are queried)"
        ),
    )
    ssh_p.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help=(
            "write to FILE instead of stdout (pair with 'Include FILE' in "
            "~/.ssh/config)"
        ),
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
    # 'db' is optional: an Odoo server names its own database when it hosts
    # only one (see odoo.resolve_database), so it is not listed as missing.
    db = overrides["db"] or (base.db if base else None)
    user = overrides["user"] or (base.user if base else None)
    missing = [k for k, v in (("url", url), ("user", user)) if not v]
    if missing or url is None or user is None:
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
    # The database may not be known yet (it is resolved at login time when
    # not declared); saying "db None" would be worse than saying nothing.
    where = f"{odoo.user} on {odoo.url}"
    if odoo.db:
        where += f" (db {odoo.db})"
    odoo.password = getpass.getpass(f"Odoo password for {where}: ")


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


def _bastion_endpoint(
    server: ServerConfig | None, args: argparse.Namespace
) -> tuple[str, int]:
    """The bastion ssh address for ``--from-odoo``: CLI over config.

    The flag wins so a user with no config file can still run standalone;
    the config keys exist so an admin can declare it once and their users
    type nothing.
    """
    if args.bastion:
        return sshconfig.parse_endpoint(args.bastion)
    if server is not None and server.ssh_host:
        return (
            server.ssh_host,
            server.ssh_port
            if server.ssh_port is not None
            else sshconfig.DEFAULT_SSH_PORT,
        )
    raise ConfigError(
        "no bastion address: pass --bastion HOST[:PORT], or set "
        "'ssh-host' on a server in the config file"
    )


def _run_ssh_config_from_odoo(config: Config, args: argparse.Namespace) -> str:
    """Render this Odoo account's ssh config, without any Warpgate call.

    The user authenticates to Odoo, which tells us who they are; the
    desired state tells us which targets their roles grant. Warpgate is
    never contacted — by design, since the point is to need no bastion
    credential.
    """
    odoo_config = _resolve_odoo_config(config, args)
    if odoo_config is None:
        raise ConfigError(
            "--from-odoo needs an Odoo source: add an 'odoo:' section to "
            "the config, or pass --odoo-url/--odoo-db/--odoo-user"
        )
    _ensure_odoo_password(odoo_config)

    # The bastion address is resolved BEFORE the network round-trip: a
    # missing address is a usage error, and making the user type their
    # password only to be told that would be gratuitous.
    server = None
    if config.servers:
        server = (
            config.server(args.server) if args.server else config.servers[0]
        )
    host, port = _bastion_endpoint(server, args)

    # One Odoo login for both queries (whoami + state), not two.
    query = odoo.make_query(odoo_config)
    login = odoo.resolve_login(odoo_config, query=query)
    state = odoo.fetch_state(odoo_config, query=query)

    config.merge_targets(state.targets)
    config.merge_users(state.users)
    config.merge_roles(state.roles)

    me = next(
        (u for u in config.users if u.name == login),
        User(name=login),
    )
    targets = access.reachable_targets(
        me, access.ssh_targets(config.all_targets()), config.roles
    )
    if not targets:
        print(
            f"warning: no ssh target reachable by {login}",
            file=sys.stderr,
        )
        return ""

    info = sshconfig.BastionInfo.declared(
        username=login, host=host, port=port
    )
    return sshconfig.render(
        [access.to_render_payload(t) for t in targets],
        info,
        prefix=args.prefix or "",
        header=(
            f"# warpgate targets for {login} via {host}:{port}\n"
            f"# generated from {odoo_config.url} (desired state) — run "
            "'apply' first if the bastion is not up to date"
        ),
    )


def _run_ssh_config(config: Config, args: argparse.Namespace) -> int:
    """Print ssh stanzas for the targets each server's api-key can reach."""
    if args.from_odoo:
        return _emit_ssh_config(_run_ssh_config_from_odoo(config, args), args)

    servers = _select_servers(config, args.server)

    # Target names are unique per server, not across servers: prefix with the
    # server name when several are queried, so aliases stay unambiguous.
    default_prefix = len(servers) > 1
    sections: list[str] = []
    for server in servers:
        prefix = (
            args.prefix
            if args.prefix is not None
            else (f"{server.name}-" if default_prefix else "")
        )
        with WarpgateUserClient(
            server.url, server.require_api_key(), verify_tls=server.verify_tls
        ) as client:
            info = BastionInfo.from_info(client.get_info(), url=server.url)
            targets = client.list_targets()

        rendered = sshconfig.render(
            targets,
            info,
            prefix=prefix,
            header=f"# {server.name} ({server.url}) — as {info.username}",
        )
        if not rendered:
            print(
                f"warning: {server.name}: no ssh target reachable by "
                f"{info.username}",
                file=sys.stderr,
            )
            continue
        sections.append(rendered)

    return _emit_ssh_config("\n".join(sections), args)


def _emit_ssh_config(output: str, args: argparse.Namespace) -> int:
    """Write the rendered config to ``--output`` or stdout."""
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(output)
        except OSError as exc:
            print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(output, end="")
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

    # Commands that never contact Warpgate must not require the Warpgate
    # api-key to resolve: ``fetch`` reads only Odoo, and ``ssh-config
    # --from-odoo`` is defined by the user holding NO bastion token. An
    # unset ${ENV} they never read cannot be allowed to block them.
    odoo_only = args.command == "fetch" or (
        args.command == "ssh-config" and args.from_odoo
    )
    try:
        config = load_config(args.config, strict_env=not odoo_only)
    except ConfigError as exc:
        # These can also run with no config file at all, when the Odoo
        # source is fully defined on the command line.
        if (
            odoo_only
            and args.config is None
            and args.odoo_url
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

    if args.command == "ssh-config":
        # Read-only and user-scoped: no desired state is involved, so the
        # Odoo source and the cross-entity validation do not apply.
        try:
            return _run_ssh_config(config, args)
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
    except ConfigError as exc:
        # e.g. a server with no 'api-key' on a path that authenticates:
        # the config is at fault, not the server's answer.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
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
