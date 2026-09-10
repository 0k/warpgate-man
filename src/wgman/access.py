"""Who may reach which target, computed from the *desired* state.

Warpgate answers this question itself — ``GET /@warpgate/api/targets`` is
filtered server-side — but only for a caller holding that user's own API
token. When the desired state comes from Odoo, nobody holds such a token:
the user authenticated to *Odoo*, not to the bastion. This module answers
the same question from the declared state instead.

The rule replicates Warpgate's ``authorized_target_ids`` (verified against
upstream ``warpgate-core/src/config_providers/db.rs``): a user reaches a
target when they share at least one role. Warpgate materialises a
``default`` role as a real assignment on every user, so a default role is
treated here as held by everyone.

One deliberate divergence, and the reason it exists
---------------------------------------------------
Warpgate answers over *rows that exist*: no shared role means no access.
Here the input is a *declaration*, and a declaration can be silent about
roles entirely — a config that names machines but no roles is not saying
"nobody reaches anything", it is not modelling access at all. Applying the
intersection literally to that would emit an empty ssh config for every
user, which reads as "you have no access" when the truth is "access is not
described here".

So: when neither side declares any role, every target is visible. As soon
as roles appear on either side, the intersection governs. See
:func:`reachable_targets`.
"""

from __future__ import annotations

from .models import Role, Target, User


def default_role_names(roles: list[Role]) -> set[str]:
    """Names of roles Warpgate auto-assigns to every user."""
    return {r.name for r in roles if r.default}


def user_role_names(user: User, roles: list[Role]) -> set[str]:
    """Every role *user* effectively holds, default roles included."""
    return set(user.roles) | default_role_names(roles)


def reachable_targets(
    user: User, targets: list[Target], roles: list[Role]
) -> list[Target]:
    """The targets *user* can reach, by shared role.

    Returns targets in the order given. When no role is declared anywhere
    — neither on the user, nor on any target, nor as a default role — the
    declaration does not model access and every target is returned; see
    the module docstring for why.
    """
    declared = default_role_names(roles) | set(user.roles)
    for target in targets:
        declared |= set(target.roles)
    if not declared:
        return list(targets)

    held = user_role_names(user, roles)
    return [t for t in targets if held & set(t.roles)]


def ssh_targets(targets: list[Target]) -> list[Target]:
    """The SSH targets among *targets*, sorted by name.

    Other kinds cannot be reached through an ssh client. This mirrors
    :func:`wgman.sshconfig.ssh_targets`, which filters the *wire* payloads
    of the live path; here the inputs are :class:`~wgman.models.Target`
    models from the desired state.
    """
    return sorted((t for t in targets if t.kind == "ssh"), key=lambda t: t.name)


def to_render_payload(target: Target) -> dict[str, object]:
    """One desired-state target as the dict :func:`wgman.sshconfig.render`
    expects.

    The renderer consumes user-API payloads, whose discriminator is the
    flat PascalCase ``kind``. Converting here keeps the renderer a single
    implementation shared by both provenances, rather than teaching it two
    input shapes.
    """
    from .sshconfig import SSH_TARGET_KIND

    return {
        "name": target.name,
        "kind": SSH_TARGET_KIND,
        "description": target.description,
    }
