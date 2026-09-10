"""Render an OpenSSH client configuration for one user's Warpgate targets.

The bastion is the only host the SSH client ever talks to: it presents its own
host key, and the *target* is selected through the SSH username, encoded as
``<warpgate-user>:<target-name>``. So a stanza reaching target ``web-01``
through bastion ``wg.example.com`` looks like::

    Host web-01
        HostName wg.example.com
        Port     2222
        User     alice:web-01

Consequences worth stating, because they are easy to get wrong:

- The backend's own username (the ``username`` of the target's SSH options)
  must NOT appear. Warpgate dials the backend with its stored credentials;
  the ``User`` field carries the *selector*, nothing else.
- ``known_hosts`` pins the bastion, not each backend — Warpgate verifies
  backend host keys itself. Emitting ``StrictHostKeyChecking no`` would
  disable the one check that still protects the user.

This module is pure: it turns already-fetched API payloads into text and
performs no I/O. :func:`render` is the entry point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .exceptions import WgmanError

#: Warpgate's SSH username separator between user and target. Warpgate parses
#: both ``:`` and ``#``; ``:`` is what its own UI generates.
TARGET_SELECTOR_SEP = ":"

#: Protocol key under ``info.ports`` / ``info.external_hosts``.
SSH_PROTOCOL = "ssh"

#: Target kind (Warpgate wire format) this renderer emits stanzas for.
SSH_TARGET_KIND = "Ssh"

#: Warpgate's default SSH listener port, used when an endpoint names no port.
DEFAULT_SSH_PORT = 2222

#: Characters safe in a ``Host`` alias. OpenSSH splits on whitespace and
#: treats ``*``, ``?`` and ``!`` as pattern syntax, so anything outside this
#: set is replaced.
_SAFE_ALIAS_RE = re.compile(r"[^A-Za-z0-9._-]+")


class SshConfigError(WgmanError):
    """Raised when the server's answers cannot yield a usable ssh config."""


@dataclass(frozen=True)
class BastionInfo:
    """How to reach one Warpgate server's SSH listener, and as whom.

    Built from ``GET /@warpgate/api/info`` — see :meth:`from_info`.
    """

    username: str
    host: str
    port: int

    @classmethod
    def declared(
        cls, *, username: str, host: str, port: int = DEFAULT_SSH_PORT
    ) -> "BastionInfo":
        """A bastion endpoint supplied by the caller, not discovered.

        Used by the Odoo-sourced path, where nobody holds a Warpgate
        credential to ask ``GET /info``: the user knows their bastion, and
        their identity comes from the Odoo session they authenticated.
        """
        if not username:
            raise SshConfigError("no username: cannot build an ssh selector")
        if not host:
            raise SshConfigError(
                "no bastion host: pass --bastion HOST[:PORT], or set "
                "'ssh-host' on the server in the config"
            )
        return cls(username=username, host=host, port=port)

    @classmethod
    def from_info(cls, info: dict[str, Any], *, url: str) -> "BastionInfo":
        """Extract the SSH connection facts from an ``info`` payload.

        Raises :class:`SshConfigError` when the payload cannot describe a
        usable SSH endpoint for a *named* user, stating which fact is
        missing and what to do about it.
        """
        username = info.get("username")
        if not username:
            raise SshConfigError(
                f"{url} reports no authenticated user for this token. "
                "A global admin token (--enable-admin-token) is bound to no "
                "Warpgate user and can reach no target; use a personal API "
                "token instead (Warpgate web UI -> your profile -> API "
                "tokens)."
            )

        host = _external_host(info, url=url)
        port = _ssh_port(info, url=url)
        return cls(username=username, host=host, port=port)


def _external_host(info: dict[str, Any], *, url: str) -> str:
    """The externally reachable SSH host of the bastion.

    Prefers the per-protocol ``external_hosts.ssh`` (which honours a
    protocol-specific ``ssh.external_host``), then the server-wide
    ``external_host``.
    """
    hosts = info.get("external_hosts") or {}
    host = hosts.get(SSH_PROTOCOL) or info.get("external_host")
    if not host:
        raise SshConfigError(
            f"{url} does not advertise an external SSH host: set "
            "'external_host' (or 'ssh.external_host') in the Warpgate "
            "server configuration so it can tell clients how to reach it."
        )
    return str(host)


def _ssh_port(info: dict[str, Any], *, url: str) -> int:
    """The externally reachable SSH port of the bastion."""
    port = (info.get("ports") or {}).get(SSH_PROTOCOL)
    if port is None:
        raise SshConfigError(
            f"{url} does not advertise an SSH port: the SSH protocol may be "
            "disabled on this server."
        )
    return int(port)


def parse_endpoint(
    value: str, *, default_port: int = DEFAULT_SSH_PORT
) -> tuple[str, int]:
    """Parse ``HOST[:PORT]`` into ``(host, port)``.

    An IPv6 literal must be bracketed (``[::1]:2222``), exactly as ssh and
    every URL syntax require: a bare ``::1`` is all colons and cannot be
    told apart from a host/port pair.
    """
    text = value.strip()
    if not text:
        raise SshConfigError("empty bastion address: expected HOST[:PORT]")

    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep or not host:
            raise SshConfigError(
                f"malformed bracketed address {value!r}: expected [HOST] "
                "or [HOST]:PORT"
            )
        port_part = rest[1:] if rest.startswith(":") else ""
        if rest and not rest.startswith(":"):
            raise SshConfigError(f"trailing junk after ']' in {value!r}")
    elif text.count(":") > 1:
        # Unbracketed IPv6: ambiguous by construction, so say exactly that.
        raise SshConfigError(
            f"ambiguous address {value!r}: bracket an IPv6 literal, "
            "e.g. [2001:db8::1]:2222"
        )
    else:
        host, _, port_part = text.partition(":")

    if not host:
        raise SshConfigError(f"no host in bastion address {value!r}")
    if not port_part:
        return host, default_port
    try:
        port = int(port_part)
    except ValueError:
        raise SshConfigError(
            f"invalid port {port_part!r} in bastion address {value!r}"
        ) from None
    if not 0 < port < 65536:
        raise SshConfigError(f"port {port} out of range (1-65535) in {value!r}")
    return host, port


def alias_for(name: str, prefix: str = "") -> str:
    """A ``Host`` alias for target *name*, safe for an ssh config file.

    Unsafe runs are collapsed to a single ``_`` so distinct targets keep
    distinct aliases; *prefix* is prepended verbatim.
    """
    safe = _SAFE_ALIAS_RE.sub("_", name).strip("_")
    if not safe:
        raise SshConfigError(
            f"target {name!r} has no character usable in an ssh Host alias; "
            "rename the target or use --prefix"
        )
    return f"{prefix}{safe}"


def selector_for(username: str, target_name: str) -> str:
    """The SSH username selecting *target_name* as *username*.

    Warpgate target names may contain spaces, but ssh_config(5) splits
    keyword arguments on whitespace and rejects the line ("extra arguments
    at end of line") — which invalidates the *whole file*, not just this
    stanza. Such values are double-quoted, as ssh unquotes them back to the
    exact selector Warpgate expects.
    """
    selector = f"{username}{TARGET_SELECTOR_SEP}{target_name}"
    if '"' in selector:
        raise SshConfigError(
            f"target {target_name!r} contains a double quote, which cannot "
            "be expressed in an ssh_config User value; rename the target"
        )
    return f'"{selector}"' if _needs_quoting(selector) else selector


def _needs_quoting(value: str) -> bool:
    """Whether *value* must be quoted to survive ssh_config(5) parsing."""
    return any(c.isspace() for c in value)


def ssh_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The SSH targets among *targets*, sorted by name.

    Other kinds (HTTP, MySQL, Postgres, Kubernetes) are not reachable through
    an ssh client and are dropped.
    """
    return sorted(
        (t for t in targets if t.get("kind") == SSH_TARGET_KIND),
        key=lambda t: str(t.get("name", "")),
    )


def render(
    targets: list[dict[str, Any]],
    info: BastionInfo,
    *,
    prefix: str = "",
    header: str | None = None,
) -> str:
    """Render ssh-config stanzas for every SSH target in *targets*.

    *targets* are entries from ``GET /@warpgate/api/targets`` (already
    filtered by Warpgate to what the user may reach). Returns the empty
    string when none of them is an SSH target.
    """
    stanzas: list[str] = []
    for target in ssh_targets(targets):
        name = str(target["name"])
        lines = [f"Host {alias_for(name, prefix)}"]
        description = str(target.get("description") or "").strip()
        if description:
            lines.insert(0, f"# {description}")
        lines += [
            f"    HostName {info.host}",
            f"    Port     {info.port}",
            f"    User     {selector_for(info.username, name)}",
        ]
        stanzas.append("\n".join(lines))

    if not stanzas:
        return ""

    body = "\n\n".join(stanzas) + "\n"
    return f"{header}\n{body}" if header else body
