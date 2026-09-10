"""High-level public API: :class:`WarpgateManager`.

This is the entry point intended for library users (e.g. Odoo). It wraps the
HTTP client and reconciler behind a clean, synchronous interface that accepts
plain Python lists of model objects.

Example
-------
>>> from wgman import WarpgateManager, Target, Role, User
>>> mgr = WarpgateManager(url="https://wg:8888", api_key="...")
>>> plan = mgr.reconcile(
...     roles=[Role("admin")],
...     targets=[Target("box", kind="ssh", host="10.0.0.1",
...                      username="root", auth="publickey", roles=["admin"])],
...     users=[User("alice", roles=["admin"])],
... )
>>> print(plan)
"""

from __future__ import annotations

from .client import WarpgateClient
from .config import Config
from .models import PruneConfig, Role, ServerConfig, Target, TargetGroup, User
from .reconcile import Plan, Reconciler


class WarpgateManager:
    """Manage one Warpgate server's targets, groups, users and roles."""

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        timeout: float = 30.0,
        client: WarpgateClient | None = None,
    ) -> None:
        self.client = client or WarpgateClient(
            url, api_key, verify_tls=verify_tls, timeout=timeout
        )

    @classmethod
    def from_server(
        cls, server: ServerConfig, *, timeout: float = 30.0
    ) -> "WarpgateManager":
        """Build a manager from a :class:`ServerConfig`.

        Raises :class:`ConfigError` if that server declares no ``api-key``:
        this manager talks to the admin API, so the token is mandatory here.
        """
        return cls(
            server.url,
            server.require_api_key(),
            verify_tls=server.verify_tls,
            timeout=timeout,
        )

    # -- context manager --------------------------------------------------- #
    def __enter__(self) -> "WarpgateManager":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    # -- reconciliation ---------------------------------------------------- #
    def reconcile(
        self,
        *,
        roles: list[Role] | None = None,
        target_groups: list[TargetGroup] | None = None,
        targets: list[Target] | None = None,
        users: list[User] | None = None,
        prune: bool = False,
        prune_config: PruneConfig | None = None,
        dry_run: bool = False,
    ) -> Plan:
        """Reconcile the given desired state onto the server.

        - ``prune=True`` deletes server-side entities absent from the desired
          state (off by default for safety).
        - ``prune_config`` scopes which kinds are pruned and protects names
          (defaults to targets-only via :meth:`PruneConfig.default`).
        - ``dry_run=True`` computes the plan without performing any writes.
        """
        reconciler = Reconciler(self.client)
        return reconciler.reconcile(
            roles=roles,
            target_groups=target_groups,
            targets=targets,
            users=users,
            prune=prune,
            prune_config=prune_config,
            dry_run=dry_run,
        )

    def diff(
        self,
        *,
        roles: list[Role] | None = None,
        target_groups: list[TargetGroup] | None = None,
        targets: list[Target] | None = None,
        users: list[User] | None = None,
        prune: bool = False,
        prune_config: PruneConfig | None = None,
    ) -> Plan:
        """Compute the plan without applying it (``reconcile(dry_run=True)``)."""
        return self.reconcile(
            roles=roles,
            target_groups=target_groups,
            targets=targets,
            users=users,
            prune=prune,
            prune_config=prune_config,
            dry_run=True,
        )

    # -- config-driven convenience ---------------------------------------- #
    def reconcile_config(
        self, config: Config, *, prune: bool = False, dry_run: bool = False
    ) -> Plan:
        """Reconcile a full parsed :class:`Config` onto this server.

        The config's ``prune:`` section (if any) governs prune scope; when
        absent, :meth:`PruneConfig.default` applies (targets only).
        """
        return self.reconcile(
            roles=config.roles,
            target_groups=config.target_groups,
            targets=config.root_targets,
            users=config.users,
            prune=prune,
            prune_config=config.prune,
            dry_run=dry_run,
        )
