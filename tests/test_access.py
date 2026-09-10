"""Who reaches what, from the desired state.

These verify the rule stated in ``wgman.access``: shared role grants
access, default roles are held by everyone, and a declaration silent
about roles is not a declaration that nobody has access.
"""

from __future__ import annotations

import pytest

from wgman.access import (
    reachable_targets,
    ssh_targets,
    to_render_payload,
    user_role_names,
)
from wgman.models import Role, Target, User


def ssh(name: str, roles: list[str] | None = None, **kw) -> Target:
    return Target(name=name, kind="ssh", host=f"{name}.example.com",
                  roles=list(roles or []), **kw)


class TestSharedRoleGrantsAccess:
    def test_user_reaches_target_sharing_a_role(self):
        user = User(name="alice", roles=["sysadmin"])
        targets = [ssh("web", ["sysadmin"]), ssh("db", ["dba"])]
        roles = [Role("sysadmin"), Role("dba")]

        assert [t.name for t in reachable_targets(user, targets, roles)] == ["web"]

    def test_user_without_shared_role_reaches_nothing(self):
        user = User(name="bob", roles=["developers"])
        targets = [ssh("web", ["sysadmin"])]
        roles = [Role("sysadmin"), Role("developers")]

        assert reachable_targets(user, targets, roles) == []

    def test_several_roles_union(self):
        user = User(name="carol", roles=["dba", "developers"])
        targets = [ssh("web", ["sysadmin"]), ssh("db", ["dba"]),
                   ssh("ci", ["developers"])]
        roles = [Role("sysadmin"), Role("dba"), Role("developers")]

        got = [t.name for t in reachable_targets(user, targets, roles)]
        assert got == ["db", "ci"]

    def test_target_with_several_roles_matches_any(self):
        user = User(name="alice", roles=["dba"])
        targets = [ssh("shared", ["sysadmin", "dba"])]

        got = reachable_targets(user, targets, [Role("sysadmin"), Role("dba")])
        assert [t.name for t in got] == ["shared"]


class TestDefaultRoles:
    """Warpgate materialises a default role onto every user, so it is held
    by everyone even when the user declares no role."""

    def test_default_role_is_held_by_everyone(self):
        user = User(name="newcomer")
        targets = [ssh("common", ["everyone"]), ssh("secret", ["admins"])]
        roles = [Role("everyone", default=True), Role("admins")]

        got = [t.name for t in reachable_targets(user, targets, roles)]
        assert got == ["common"]

    def test_user_role_names_includes_defaults(self):
        user = User(name="alice", roles=["dba"])
        roles = [Role("everyone", default=True), Role("dba")]

        assert user_role_names(user, roles) == {"dba", "everyone"}

    def test_non_default_role_is_not_implicit(self):
        user = User(name="alice")
        targets = [ssh("web", ["sysadmin"])]

        assert reachable_targets(user, targets, [Role("sysadmin")]) == []


class TestNoRolesDeclaredAnywhere:
    """The real Elabore config sources machines but models no access: a
    literal intersection would hand every user an empty file."""

    def test_all_targets_visible_when_nothing_declares_a_role(self):
        user = User(name="alice")
        targets = [ssh("web"), ssh("db")]

        got = [t.name for t in reachable_targets(user, targets, [])]
        assert got == ["web", "db"]

    def test_a_single_declared_role_reinstates_the_intersection(self):
        # As soon as ONE target declares a role, silence stops meaning
        # "not modelled" and starts meaning "not granted".
        user = User(name="alice")
        targets = [ssh("web"), ssh("db", ["sysadmin"])]

        got = reachable_targets(user, targets, [Role("sysadmin")])
        assert got == []

    def test_roles_declared_only_on_the_user_also_reinstate_it(self):
        user = User(name="alice", roles=["sysadmin"])
        targets = [ssh("web")]

        assert reachable_targets(user, targets, [Role("sysadmin")]) == []


class TestSshTargetsOnly:
    def test_non_ssh_kinds_are_dropped(self):
        targets = [
            ssh("box"),
            Target(name="site", kind="http", url="https://example.com"),
            Target(name="mydb", kind="mysql", host="10.0.0.9",
                   auth={"kind": "password", "password": "x"}),
        ]
        assert [t.name for t in ssh_targets(targets)] == ["box"]

    def test_sorted_by_name(self):
        assert [t.name for t in ssh_targets([ssh("zeta"), ssh("alpha")])] == [
            "alpha", "zeta",
        ]


class TestRenderPayload:
    def test_shape_matches_what_the_renderer_consumes(self):
        payload = to_render_payload(ssh("web", description="Front"))
        # The renderer keys off the flat PascalCase discriminator, as the
        # user API delivers it.
        assert payload == {
            "name": "web", "kind": "Ssh", "description": "Front",
        }

    def test_payload_feeds_the_renderer_unchanged(self):
        from wgman.sshconfig import BastionInfo, render

        out = render(
            [to_render_payload(ssh("web"))],
            BastionInfo.declared(username="alice", host="wg.example.com"),
        )
        assert "Host web" in out
        assert "User     alice:web" in out
