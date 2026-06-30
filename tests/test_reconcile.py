"""Tests for the reconcile (diff + apply) logic using a fake client.

The fake client records calls and serves a controllable live state, letting us
assert on the computed Plan and the writes performed.
"""

import pytest

from wgman.models import Role, Target, TargetGroup, User
from wgman.reconcile import Action, Reconciler


class FakeClient:
    """In-memory stand-in for WarpgateClient."""

    def __init__(self, *, roles=None, groups=None, targets=None, users=None,
                 target_roles=None, user_roles=None):
        self._roles = roles or []
        self._groups = groups or []
        self._targets = targets or []
        self._users = users or []
        self._target_roles = target_roles or {}
        self._user_roles = user_roles or {}
        self.calls = []

    # listing
    def list_roles(self):
        return list(self._roles)

    def list_target_groups(self):
        return list(self._groups)

    def list_targets(self):
        return list(self._targets)

    def list_users(self):
        return list(self._users)

    def list_target_roles(self, target_id):
        return [{"name": n} for n in self._target_roles.get(target_id, [])]

    def list_user_roles(self, user_id):
        return [{"name": n} for n in self._user_roles.get(user_id, [])]

    # mutations (record + return a fake created object)
    def create_role(self, body):
        self.calls.append(("create_role", body))
        return {"id": f"role-{body['name']}", **body}

    def update_role(self, role_id, body):
        self.calls.append(("update_role", role_id, body))
        return {"id": role_id, **body}

    def delete_role(self, role_id):
        self.calls.append(("delete_role", role_id))

    def create_target_group(self, body):
        self.calls.append(("create_target_group", body))
        return {"id": f"group-{body['name']}", **body}

    def update_target_group(self, group_id, body):
        self.calls.append(("update_target_group", group_id, body))
        return {"id": group_id, **body}

    def delete_target_group(self, group_id):
        self.calls.append(("delete_target_group", group_id))

    def create_target(self, body):
        self.calls.append(("create_target", body))
        return {"id": f"target-{body['name']}", **body}

    def update_target(self, target_id, body):
        self.calls.append(("update_target", target_id, body))
        return {"id": target_id, **body}

    def delete_target(self, target_id):
        self.calls.append(("delete_target", target_id))

    def add_target_role(self, target_id, role_id):
        self.calls.append(("add_target_role", target_id, role_id))

    def remove_target_role(self, target_id, role_id):
        self.calls.append(("remove_target_role", target_id, role_id))

    def create_user(self, body):
        self.calls.append(("create_user", body))
        return {"id": f"user-{body['username']}", **body}

    def update_user(self, user_id, body):
        self.calls.append(("update_user", user_id, body))
        return {"id": user_id, **body}

    def delete_user(self, user_id):
        self.calls.append(("delete_user", user_id))

    def add_user_role(self, user_id, role_id):
        self.calls.append(("add_user_role", user_id, role_id))

    def remove_user_role(self, user_id, role_id):
        self.calls.append(("remove_user_role", user_id, role_id))

    def names_of(self, op):
        return [c for c in self.calls if c[0] == op]


def _action_names(plan, action):
    return {(c.kind, c.name) for c in plan.of(action)}


class TestRoles:
    def test_create_missing_role(self):
        client = FakeClient(roles=[])
        rec = Reconciler(client)
        plan = rec.reconcile(roles=[Role("admin")])
        assert ("role", "admin") in _action_names(plan, Action.CREATE)
        assert client.names_of("create_role")

    def test_no_change_when_identical(self):
        client = FakeClient(
            roles=[{"id": "1", "name": "admin", "description": "",
                    "is_default": False}]
        )
        rec = Reconciler(client)
        plan = rec.reconcile(roles=[Role("admin")])
        assert plan.of(Action.CREATE) == []
        assert plan.of(Action.UPDATE) == []

    def test_update_on_default_change(self):
        client = FakeClient(
            roles=[{"id": "1", "name": "admin", "description": "",
                    "is_default": False}]
        )
        rec = Reconciler(client)
        plan = rec.reconcile(roles=[Role("admin", default=True)])
        assert ("role", "admin") in _action_names(plan, Action.UPDATE)

    def test_prune_deletes_extra(self):
        client = FakeClient(
            roles=[{"id": "1", "name": "admin", "description": "",
                    "is_default": False},
                   {"id": "2", "name": "stale", "description": "",
                    "is_default": False}]
        )
        rec = Reconciler(client)
        plan = rec.reconcile(roles=[Role("admin")], prune=True)
        assert ("role", "stale") in _action_names(plan, Action.DELETE)
        assert ("delete_role", "2") in client.calls

    def test_no_prune_keeps_extra(self):
        client = FakeClient(
            roles=[{"id": "2", "name": "stale", "description": "",
                    "is_default": False}]
        )
        rec = Reconciler(client)
        plan = rec.reconcile(roles=[], prune=False)
        assert plan.of(Action.DELETE) == []


class TestDryRun:
    def test_dry_run_performs_no_writes(self):
        client = FakeClient(roles=[])
        rec = Reconciler(client)
        plan = rec.reconcile(roles=[Role("admin")], dry_run=True)
        assert ("role", "admin") in _action_names(plan, Action.CREATE)
        assert client.calls == []  # nothing written


class TestTargetsWithRoles:
    def test_create_target_assigns_roles(self):
        client = FakeClient(
            roles=[{"id": "r-admin", "name": "admin", "description": "",
                    "is_default": False}],
            targets=[],
        )
        rec = Reconciler(client)
        target = Target("box", kind="ssh", host="h", roles=["admin"])
        plan = rec.reconcile(roles=[Role("admin")], targets=[target])
        assert ("target", "box") in _action_names(plan, Action.CREATE)
        assert ("add_target_role", "target-box", "r-admin") in client.calls

    def test_existing_target_role_removed_when_absent(self):
        client = FakeClient(
            roles=[{"id": "r-admin", "name": "admin", "description": "",
                    "is_default": False}],
            targets=[{"id": "t1", "name": "box", "description": "",
                      "options": {"kind": "Ssh", "host": "h", "port": 22,
                                  "username": "root",
                                  "auth": {"kind": "PublicKey"}}}],
            target_roles={"t1": ["admin"]},
        )
        rec = Reconciler(client)
        target = Target("box", kind="ssh", host="h", roles=[])
        rec.reconcile(roles=[Role("admin")], targets=[target])
        assert ("remove_target_role", "t1", "r-admin") in client.calls

    def test_dry_run_detects_existing_role_no_spurious_add(self):
        # Regression: dry-run must still READ current roles, otherwise an
        # already-assigned role is wrongly reported as a +role addition.
        client = FakeClient(
            roles=[{"id": "r-admin", "name": "admin", "description": "",
                    "is_default": False}],
            targets=[{"id": "t1", "name": "box", "description": "",
                      "options": {"kind": "Ssh", "host": "h", "port": 22,
                                  "username": "root",
                                  "auth": {"kind": "PublicKey"}}}],
            target_roles={"t1": ["admin"]},
        )
        rec = Reconciler(client)
        target = Target("box", kind="ssh", host="h", roles=["admin"])
        plan = rec.reconcile(
            roles=[Role("admin")], targets=[target], dry_run=True
        )
        # Role already present => no +role change planned.
        assert all("+role" not in c.detail for c in plan.changes)

    def test_target_in_group_gets_group_id(self):
        client = FakeClient(
            roles=[{"id": "r", "name": "admin", "description": "",
                    "is_default": False}],
            groups=[{"id": "g1", "name": "dbs", "description": ""}],
            targets=[],
        )
        rec = Reconciler(client)
        group = TargetGroup(
            name="dbs",
            targets=[Target("pg", kind="ssh", host="h", group="dbs",
                            roles=["admin"])],
        )
        rec.reconcile(roles=[Role("admin")], target_groups=[group])
        create = next(c for c in client.calls if c[0] == "create_target")
        assert create[1]["group_id"] == "g1"


class TestUsersWithRoles:
    def test_create_user_assigns_roles(self):
        client = FakeClient(
            roles=[{"id": "r-admin", "name": "admin", "description": "",
                    "is_default": False}],
            users=[],
        )
        rec = Reconciler(client)
        plan = rec.reconcile(
            roles=[Role("admin")],
            users=[User("alice", roles=["admin"])],
        )
        assert ("user", "alice") in _action_names(plan, Action.CREATE)
        assert ("add_user_role", "user-alice", "r-admin") in client.calls

    def test_user_matched_by_username(self):
        client = FakeClient(
            roles=[],
            users=[{"id": "u1", "username": "alice", "description": ""}],
        )
        rec = Reconciler(client)
        plan = rec.reconcile(users=[User("alice")])
        assert plan.of(Action.CREATE) == []

    def test_dry_run_detects_existing_user_role(self):
        # Regression: dry-run reads current user roles.
        client = FakeClient(
            roles=[{"id": "r-admin", "name": "admin", "description": "",
                    "is_default": False}],
            users=[{"id": "u1", "username": "alice", "description": ""}],
            user_roles={"u1": ["admin"]},
        )
        rec = Reconciler(client)
        plan = rec.reconcile(
            roles=[Role("admin")],
            users=[User("alice", roles=["admin"])],
            dry_run=True,
        )
        assert all("+role" not in c.detail for c in plan.changes)

    def test_default_role_never_removed(self):
        # Warpgate auto-assigns is_default roles and refuses removal; the
        # reconciler must not try to remove them (otherwise it never converges).
        client = FakeClient(
            roles=[{"id": "r-demo", "name": "demo", "description": "",
                    "is_default": False},
                   {"id": "r-staff", "name": "staff", "description": "",
                    "is_default": True}],
            users=[{"id": "u1", "username": "alice", "description": ""}],
            user_roles={"u1": ["demo", "staff"]},  # staff auto-attached
        )
        rec = Reconciler(client)
        # config only declares 'demo' for alice
        plan = rec.reconcile(
            roles=[],
            users=[User("alice", roles=["demo"])],
        )
        # staff is default => must NOT be removed
        assert all("-role staff" not in c.detail for c in plan.changes)
        assert ("remove_user_role", "u1", "r-staff") not in client.calls

    def test_prune_deletes_extra_user(self):
        client = FakeClient(
            users=[{"id": "u9", "username": "ghost", "description": ""}],
        )
        rec = Reconciler(client)
        plan = rec.reconcile(users=[], prune=True)
        assert ("user", "ghost") in _action_names(plan, Action.DELETE)
        assert ("delete_user", "u9") in client.calls


class TestPlanFormatting:
    def test_empty_plan_message(self):
        client = FakeClient()
        rec = Reconciler(client)
        plan = rec.reconcile()
        assert plan.empty
        assert "already matches" in str(plan)
