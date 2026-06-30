"""Diff + apply logic: reconcile a Warpgate server to a desired state.

The reconciler computes, for each resource kind (roles, target-groups, targets,
users), the set of *create*, *update* and *delete* actions needed to make the
live server match the desired state, then optionally applies them.

Matching is done by ``name`` (the natural key in the YAML); Warpgate's own
UUIDs are resolved from the live state.

Order matters:

1. roles            (targets/users reference them)
2. target-groups    (targets reference them via group_id)
3. targets          (+ their role assignments)
4. users            (+ their role assignments)

Deletion runs in reverse dependency order, and only when ``prune`` is true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .client import WarpgateClient
from .models import Role, Target, TargetGroup, User


class Action(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass
class Change:
    """A single planned change."""

    action: Action
    kind: str  # "role" | "target-group" | "target" | "user"
    name: str
    detail: str = ""

    def __str__(self) -> str:
        line = f"{self.action.value:<7} {self.kind:<13} {self.name}"
        return f"{line}  ({self.detail})" if self.detail else line


@dataclass
class Plan:
    """The full set of changes for one reconcile run."""

    changes: list[Change] = field(default_factory=list)

    def add(self, action: Action, kind: str, name: str, detail: str = "") -> None:
        self.changes.append(Change(action, kind, name, detail))

    @property
    def empty(self) -> bool:
        return not self.changes

    def of(self, action: Action) -> list[Change]:
        return [c for c in self.changes if c.action == action]

    def __str__(self) -> str:
        if self.empty:
            return "No changes. Server already matches desired state."
        return "\n".join(str(c) for c in self.changes)


def _by_name(
    items: list[dict[str, Any]], key: str = "name"
) -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items if key in item}


class Reconciler:
    """Compute and apply the diff between desired and live Warpgate state."""

    def __init__(self, client: WarpgateClient) -> None:
        self.client = client

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def reconcile(
        self,
        *,
        roles: list[Role] | None = None,
        target_groups: list[TargetGroup] | None = None,
        targets: list[Target] | None = None,
        users: list[User] | None = None,
        prune: bool = False,
        dry_run: bool = False,
    ) -> Plan:
        """Reconcile the given desired state onto the server.

        Only the resource kinds explicitly provided (non-``None``) are
        reconciled. Passing ``None`` for a kind leaves it untouched (and never
        prunes it), which lets callers manage a subset.
        """
        roles = roles or []
        target_groups = target_groups or []
        targets = list(targets or [])
        users = users or []

        # Targets nested in groups are part of the managed target set.
        for group in target_groups:
            targets.extend(group.targets)

        plan = Plan()

        if roles is not None:
            self._reconcile_roles(roles, plan, prune, dry_run)
        if target_groups is not None:
            self._reconcile_groups(target_groups, plan, prune, dry_run)
        if targets is not None:
            self._reconcile_targets(targets, plan, prune, dry_run)
        if users is not None:
            self._reconcile_users(users, plan, prune, dry_run)

        return plan

    # ------------------------------------------------------------------ #
    # Roles
    # ------------------------------------------------------------------ #
    def _reconcile_roles(
        self, desired: list[Role], plan: Plan, prune: bool, dry_run: bool
    ) -> None:
        live = _by_name(self.client.list_roles())
        for role in desired:
            body = role.to_api_body()
            if role.name not in live:
                plan.add(Action.CREATE, "role", role.name)
                if not dry_run:
                    self.client.create_role(body)
            elif _role_differs(role, live[role.name]):
                plan.add(Action.UPDATE, "role", role.name)
                if not dry_run:
                    self.client.update_role(live[role.name]["id"], body)

        if prune:
            desired_names = {r.name for r in desired}
            for name, obj in live.items():
                if name not in desired_names:
                    plan.add(Action.DELETE, "role", name)
                    if not dry_run:
                        self.client.delete_role(obj["id"])

    # ------------------------------------------------------------------ #
    # Target groups
    # ------------------------------------------------------------------ #
    def _reconcile_groups(
        self, desired: list[TargetGroup], plan: Plan, prune: bool, dry_run: bool
    ) -> None:
        live = _by_name(self.client.list_target_groups())
        for group in desired:
            body = group.to_api_body()
            if group.name not in live:
                plan.add(Action.CREATE, "target-group", group.name)
                if not dry_run:
                    self.client.create_target_group(body)
            elif _group_differs(group, live[group.name]):
                plan.add(Action.UPDATE, "target-group", group.name)
                if not dry_run:
                    self.client.update_target_group(live[group.name]["id"], body)

        if prune:
            desired_names = {g.name for g in desired}
            for name, obj in live.items():
                if name not in desired_names:
                    plan.add(Action.DELETE, "target-group", name)
                    if not dry_run:
                        self.client.delete_target_group(obj["id"])

    # ------------------------------------------------------------------ #
    # Targets (+ role assignments)
    # ------------------------------------------------------------------ #
    def _reconcile_targets(
        self, desired: list[Target], plan: Plan, prune: bool, dry_run: bool
    ) -> None:
        live = _by_name(self.client.list_targets())
        role_ids = _by_name(self.client.list_roles())
        group_ids = _by_name(self.client.list_target_groups())

        for target in desired:
            group_id = (
                group_ids.get(target.group, {}).get("id")
                if target.group
                else None
            )
            body = target.to_api_body(group_id=group_id)
            if target.name not in live:
                plan.add(Action.CREATE, "target", target.name)
                if not dry_run:
                    created = self.client.create_target(body)
                    self._sync_target_roles(
                        created["id"], target, role_ids, plan, dry_run,
                        creating=True,
                    )
            else:
                obj = live[target.name]
                if _target_differs(target, obj):
                    plan.add(Action.UPDATE, "target", target.name)
                    if not dry_run:
                        self.client.update_target(obj["id"], body)
                self._sync_target_roles(
                    obj["id"], target, role_ids, plan, dry_run, creating=False
                )

        if prune:
            desired_names = {t.name for t in desired}
            for name, obj in live.items():
                if name not in desired_names:
                    plan.add(Action.DELETE, "target", name)
                    if not dry_run:
                        self.client.delete_target(obj["id"])

    def _sync_target_roles(
        self,
        target_id: str,
        target: Target,
        role_ids: dict[str, dict[str, Any]],
        plan: Plan,
        dry_run: bool,
        *,
        creating: bool,
    ) -> None:
        desired = set(target.roles)
        # Fetch current roles even in dry-run (it is a read); a freshly-created
        # target has none yet, so skip the read only when creating.
        current: set[str] = set()
        if not creating:
            current = {r["name"] for r in self.client.list_target_roles(target_id)}

        for role in desired - current:
            if role not in role_ids:
                continue
            plan.add(Action.UPDATE, "target", target.name, f"+role {role}")
            if not dry_run:
                self.client.add_target_role(target_id, role_ids[role]["id"])
        for role in current - desired:
            if role not in role_ids:
                continue
            plan.add(Action.UPDATE, "target", target.name, f"-role {role}")
            if not dry_run:
                self.client.remove_target_role(target_id, role_ids[role]["id"])

    # ------------------------------------------------------------------ #
    # Users (+ role assignments)
    # ------------------------------------------------------------------ #
    def _reconcile_users(
        self, desired: list[User], plan: Plan, prune: bool, dry_run: bool
    ) -> None:
        live = _by_name(self.client.list_users(), key="username")
        role_ids = _by_name(self.client.list_roles())

        for user in desired:
            body = user.to_api_body()
            if user.name not in live:
                plan.add(Action.CREATE, "user", user.name)
                if not dry_run:
                    created = self.client.create_user(body)
                    self._sync_user_roles(
                        created["id"], user, role_ids, plan, dry_run,
                        creating=True,
                    )
            else:
                obj = live[user.name]
                if _user_differs(user, obj):
                    plan.add(Action.UPDATE, "user", user.name)
                    if not dry_run:
                        self.client.update_user(obj["id"], body)
                self._sync_user_roles(
                    obj["id"], user, role_ids, plan, dry_run, creating=False
                )

        if prune:
            desired_names = {u.name for u in desired}
            for name, obj in live.items():
                if name not in desired_names:
                    plan.add(Action.DELETE, "user", name)
                    if not dry_run:
                        self.client.delete_user(obj["id"])

    def _sync_user_roles(
        self,
        user_id: str,
        user: User,
        role_ids: dict[str, dict[str, Any]],
        plan: Plan,
        dry_run: bool,
        *,
        creating: bool,
    ) -> None:
        desired = set(user.roles)
        # Fetch current roles even in dry-run (read); a freshly-created user
        # has none yet, so skip the read only when creating.
        current: set[str] = set()
        if not creating:
            current = {r["name"] for r in self.client.list_user_roles(user_id)}

        for role in desired - current:
            if role not in role_ids:
                continue
            plan.add(Action.UPDATE, "user", user.name, f"+role {role}")
            if not dry_run:
                self.client.add_user_role(user_id, role_ids[role]["id"])
        for role in current - desired:
            if role not in role_ids:
                continue
            # Warpgate auto-assigns roles flagged is_default to every user and
            # refuses their removal (the DELETE returns 204 but the role
            # re-attaches). Treat default roles as implicit: never try to
            # remove them, otherwise the plan never converges.
            if role_ids[role].get("is_default"):
                continue
            plan.add(Action.UPDATE, "user", user.name, f"-role {role}")
            if not dry_run:
                self.client.remove_user_role(user_id, role_ids[role]["id"])


# --------------------------------------------------------------------------- #
# Diff helpers — compare desired model to live API object
# --------------------------------------------------------------------------- #
def _role_differs(desired: Role, live: dict[str, Any]) -> bool:
    return (
        desired.description != live.get("description", "")
        or desired.default != live.get("is_default", False)
    )


def _group_differs(desired: TargetGroup, live: dict[str, Any]) -> bool:
    if desired.description != live.get("description", ""):
        return True
    if desired.color is not None:
        # Compare in wire format; the live API returns PascalCase colors.
        if desired.to_api_body().get("color") != live.get("color"):
            return True
    return False


def _target_differs(desired: Target, live: dict[str, Any]) -> bool:
    if desired.description != live.get("description", ""):
        return True
    # Compare only the option fields we manage; the live object carries extra
    # server-side fields (allow_insecure_algos, jump_host, ...) we leave alone.
    desired_opts = desired.options()
    live_opts = live.get("options", {})
    for key, value in desired_opts.items():
        if live_opts.get(key) != value:
            return True
    return False


def _user_differs(desired: User, live: dict[str, Any]) -> bool:
    return desired.description != live.get("description", "")
