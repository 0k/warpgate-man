"""Tests for the reconcile (diff + apply) logic using a fake client.

The fake client records calls and serves a controllable live state, letting us
assert on the computed Plan and the writes performed.
"""

import pytest

from wgman.models import PruneConfig, Role, Target, TargetGroup, User
from wgman.reconcile import Action, Reconciler

# Prune scope that enables every kind — the old default before prune became
# targets-only. Used by tests that specifically exercise role/user pruning.
PRUNE_ALL = PruneConfig(
    prune_targets=True,
    prune_target_groups=True,
    prune_roles=True,
    prune_users=True,
)


class FakeClient:
    """In-memory stand-in for WarpgateClient."""

    def __init__(self, *, roles=None, groups=None, targets=None, users=None,
                 target_roles=None, user_roles=None, user_keys=None):
        self._roles = roles or []
        self._groups = groups or []
        self._targets = targets or []
        self._users = users or []
        self._target_roles = target_roles or {}
        self._user_roles = user_roles or {}
        # {user_id: [{"id": ..., "label": ..., "openssh_public_key": ...}]}
        self._user_keys = user_keys or {}
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
        created = {"id": f"role-{body['name']}", **body}
        # Mirror the real server: created roles appear in later listings.
        self._roles.append(created)
        return created

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

    def list_user_public_keys(self, user_id):
        return list(self._user_keys.get(user_id, []))

    def add_user_public_key(self, user_id, label, openssh_public_key):
        self.calls.append(
            ("add_user_public_key", user_id, label, openssh_public_key)
        )
        return {"id": f"key-{label}", "label": label,
                "openssh_public_key": openssh_public_key}

    def delete_user_public_key(self, user_id, key_id):
        self.calls.append(("delete_user_public_key", user_id, key_id))

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
        plan = rec.reconcile(
            roles=[Role("admin")], prune=True, prune_config=PRUNE_ALL
        )
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
        plan = rec.reconcile(users=[], prune=True, prune_config=PRUNE_ALL)
        assert ("user", "ghost") in _action_names(plan, Action.DELETE)
        assert ("delete_user", "u9") in client.calls


class TestPlanFormatting:
    def test_empty_plan_message(self):
        client = FakeClient()
        rec = Reconciler(client)
        plan = rec.reconcile()
        assert plan.empty
        assert "already matches" in str(plan)


class TestRoleCreationOrdering:
    """A new role must be creatable AND assignable in a single pass."""

    def test_apply_creates_role_then_assigns_to_existing_target(self):
        client = FakeClient(
            targets=[{"id": "t1", "name": "srv", "options": {},
                      "description": ""}],
        )
        rec = Reconciler(client)
        target = Target("srv", kind="ssh", host="h", auth="publickey",
                        roles=["ssh-dev"])
        rec.reconcile(roles=[Role("ssh-dev")], targets=[target])
        assert ("create_role", {"name": "ssh-dev", "description": "",
                                "is_default": False}) in client.calls
        assert ("add_target_role", "t1", "role-ssh-dev") in client.calls

    def test_apply_creates_role_then_assigns_to_new_user(self):
        client = FakeClient()
        rec = Reconciler(client)
        user = User("alice", roles=["ssh-dev"])
        rec.reconcile(roles=[Role("ssh-dev")], users=[user])
        assert ("add_user_role", "user-alice", "role-ssh-dev") in client.calls

    def test_dry_run_plans_assignment_of_uncreated_role(self):
        client = FakeClient(
            targets=[{"id": "t1", "name": "srv", "options": {},
                      "description": ""}],
        )
        rec = Reconciler(client)
        target = Target("srv", kind="ssh", host="h", auth="publickey",
                        roles=["ssh-dev"])
        plan = rec.reconcile(
            roles=[Role("ssh-dev")], targets=[target], dry_run=True
        )
        details = [(c.name, c.detail) for c in plan.of(Action.UPDATE)]
        assert ("srv", "+role ssh-dev") in details
        # dry-run: nothing actually written
        assert not any(c[0].startswith(("create_", "add_", "remove_"))
                       for c in client.calls)


KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc alice@laptop"
KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdef alice@desktop"


class TestUserPublicKeySync:
    def test_new_user_gets_keys(self):
        client = FakeClient()
        rec = Reconciler(client)
        rec.reconcile(users=[User("alice", public_keys=[KEY_A])])
        assert ("add_user_public_key", "user-alice", "alice@laptop", KEY_A) \
            in client.calls

    def test_existing_user_missing_key_added(self):
        client = FakeClient(
            users=[{"id": "u1", "username": "alice", "description": ""}],
            user_keys={"u1": [{"id": "k1", "label": "old",
                               "openssh_public_key": KEY_A}]},
        )
        rec = Reconciler(client)
        plan = rec.reconcile(
            users=[User("alice", public_keys=[KEY_A, KEY_B])]
        )
        details = [(c.name, c.detail) for c in plan.of(Action.UPDATE)]
        assert ("alice", "+key alice@desktop") in details
        assert ("add_user_public_key", "u1", "alice@desktop", KEY_B) \
            in client.calls
        # KEY_A already present: not re-added
        assert not any(c[:2] == ("add_user_public_key", "u1")
                       and c[3] == KEY_A for c in client.calls)

    def test_extraneous_key_removed(self):
        client = FakeClient(
            users=[{"id": "u1", "username": "alice", "description": ""}],
            user_keys={"u1": [
                {"id": "k1", "label": "keep", "openssh_public_key": KEY_A},
                {"id": "k2", "label": "stale", "openssh_public_key": KEY_B},
            ]},
        )
        rec = Reconciler(client)
        plan = rec.reconcile(users=[User("alice", public_keys=[KEY_A])])
        details = [(c.name, c.detail) for c in plan.of(Action.UPDATE)]
        assert ("alice", "-key stale") in details
        assert ("delete_user_public_key", "u1", "k2") in client.calls

    def test_user_without_declared_keys_untouched(self):
        client = FakeClient(
            users=[{"id": "u1", "username": "alice", "description": ""}],
            user_keys={"u1": [{"id": "k1", "label": "manual",
                               "openssh_public_key": KEY_A}]},
        )
        rec = Reconciler(client)
        plan = rec.reconcile(users=[User("alice")])
        assert not any("key" in c.detail for c in plan.of(Action.UPDATE))
        assert not any(c[0].startswith("delete_user_public_key")
                       for c in client.calls)

    def test_idempotent_when_keys_match(self):
        client = FakeClient(
            users=[{"id": "u1", "username": "alice", "description": ""}],
            user_keys={"u1": [{"id": "k1", "label": "l",
                               "openssh_public_key": KEY_A}]},
        )
        rec = Reconciler(client)
        plan = rec.reconcile(users=[User("alice", public_keys=[KEY_A])])
        assert plan.empty

    def test_whitespace_normalisation_no_churn(self):
        # Server stores the key with extra spacing: must still match.
        client = FakeClient(
            users=[{"id": "u1", "username": "alice", "description": ""}],
            user_keys={"u1": [{"id": "k1", "label": "l",
                               "openssh_public_key":
                               "ssh-ed25519   AAAAC3NzaC1lZDI1NTE5AAAAIabc  alice@laptop"}]},
        )
        rec = Reconciler(client)
        plan = rec.reconcile(users=[User("alice", public_keys=[KEY_A])])
        assert plan.empty

    def test_dry_run_plans_but_does_not_write(self):
        client = FakeClient()
        rec = Reconciler(client)
        plan = rec.reconcile(
            users=[User("alice", public_keys=[KEY_A])], dry_run=True
        )
        details = [(c.name, c.detail) for c in plan.of(Action.UPDATE)]
        assert ("alice", "+key alice@laptop") in details
        assert not any(c[0] == "add_user_public_key" for c in client.calls)

    def test_key_without_comment_gets_blob_label(self):
        bare = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabcdefghijkl"
        client = FakeClient()
        rec = Reconciler(client)
        rec.reconcile(users=[User("alice", public_keys=[bare])])
        add = next(c for c in client.calls if c[0] == "add_user_public_key")
        assert add[2] == "...AAAAIabcdefghijkl"[-15:] or add[2].startswith("...")


class TestPruneScope:
    """Per-kind prune toggles and keep-lists (PruneConfig)."""

    def _mixed_client(self):
        return FakeClient(
            roles=[{"id": "r9", "name": "stale-role", "description": "",
                    "is_default": False}],
            targets=[{"id": "t9", "name": "stale-target",
                      "options": {}, "description": ""}],
            users=[{"id": "u9", "username": "ghost", "description": ""}],
        )

    def test_default_prunes_targets_only(self):
        """--prune with the default PruneConfig deletes targets, not users/roles."""
        client = self._mixed_client()
        rec = Reconciler(client)
        plan = rec.reconcile(
            roles=[], targets=[], users=[], prune=True,
        )
        deleted = _action_names(plan, Action.DELETE)
        assert ("target", "stale-target") in deleted
        assert ("user", "ghost") not in deleted
        assert ("role", "stale-role") not in deleted

    def test_enable_user_prune(self):
        client = self._mixed_client()
        rec = Reconciler(client)
        pc = PruneConfig(prune_targets=True, prune_users=True)
        plan = rec.reconcile(
            roles=[], targets=[], users=[], prune=True, prune_config=pc,
        )
        deleted = _action_names(plan, Action.DELETE)
        assert ("user", "ghost") in deleted
        assert ("role", "stale-role") not in deleted  # roles still off

    def test_keep_list_protects_user(self):
        client = FakeClient(
            users=[{"id": "u1", "username": "admin", "description": ""},
                   {"id": "u2", "username": "ghost", "description": ""}],
        )
        rec = Reconciler(client)
        pc = PruneConfig(prune_users=True, keep_users={"admin"})
        plan = rec.reconcile(
            users=[], prune=True, prune_config=pc,
        )
        deleted = _action_names(plan, Action.DELETE)
        assert ("user", "ghost") in deleted
        assert ("user", "admin") not in deleted
        assert ("delete_user", "u1") not in client.calls
        assert ("delete_user", "u2") in client.calls

    def test_prune_false_ignores_config(self):
        """No --prune master switch => nothing deleted regardless of scope."""
        client = self._mixed_client()
        rec = Reconciler(client)
        pc = PruneConfig(prune_targets=True, prune_users=True, prune_roles=True)
        plan = rec.reconcile(
            roles=[], targets=[], users=[], prune=False, prune_config=pc,
        )
        assert plan.of(Action.DELETE) == []
