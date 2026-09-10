"""Dataclasses describing the desired Warpgate state.

These mirror the Warpgate admin-API data model (verified against
warp-tech/warpgate). Each model knows how to:

- be built from a parsed YAML mapping (``from_dict``), and
- be serialised into the JSON body expected by the admin API
  (``to_api_body``).

The Warpgate target ``options`` is a discriminated union on ``kind``
(``ssh``/``http``/``mysql``/``postgres``/``kubernetes``), flattened so that the
``kind`` key appears at the top level of the target object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .exceptions import ConfigError

# Target kinds supported by Warpgate (lowercase = user-facing YAML values).
TARGET_KINDS = ("ssh", "http", "mysql", "postgres", "kubernetes")

# SSH/database auth kinds (user-facing YAML values).
SSH_AUTH_KINDS = ("publickey", "password", "iam_role")
DB_AUTH_KINDS = ("password", "iam_role")

# UI colors accepted by Warpgate target-groups (user-facing YAML values).
GROUP_COLORS = (
    "primary", "secondary", "success", "danger",
    "warning", "info", "light", "dark",
)

# Wire-format mapping for the BootstrapThemeColor enum (PascalCase on the API,
# verified against warpgate 0.25.4).
_COLOR_WIRE = {c: c.capitalize() for c in GROUP_COLORS}

# Wire-format mapping: Warpgate's admin API uses PascalCase discriminators
# (verified against the live OpenAPI spec of warpgate 0.25.4). We accept
# friendly lowercase values in YAML and translate at the API boundary.
_KIND_WIRE = {
    "ssh": "Ssh",
    "http": "Http",
    "mysql": "MySql",
    "postgres": "Postgres",
    "kubernetes": "Kubernetes",
}
_AUTH_WIRE = {
    "publickey": "PublicKey",
    "password": "Password",
    "iam_role": "IamRole",
}

# Default TLS block required by http/mysql/postgres target options.
_DEFAULT_TLS = {"mode": "Preferred", "verify": False}


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


# --------------------------------------------------------------------------- #
# Role
# --------------------------------------------------------------------------- #
@dataclass
class Role:
    """A Warpgate role. Roles link users to targets (shared role => access)."""

    name: str
    description: str = ""
    default: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str = "role") -> "Role":
        name = _require(data, "name", where)
        return cls(
            name=name,
            description=data.get("description", ""),
            default=bool(data.get("default", False)),
        )

    def to_api_body(self) -> "dict[str, Any]":
        return {
            "name": self.name,
            "description": self.description,
            "is_default": self.default,
        }


# --------------------------------------------------------------------------- #
# User
# --------------------------------------------------------------------------- #
@dataclass
class User:
    """A Warpgate user.

    ``roles`` are role names granting target access. ``public_keys`` is an
    optional list of OpenSSH public keys (``ssh-ed25519 AAAA... comment``);
    when non-empty, wgman strictly synchronises the user's public-key
    credentials to this list (add missing, remove extraneous). Empty means
    keys are not managed for this user.
    """

    name: str
    description: str = ""
    roles: list[str] = field(default_factory=list)
    public_keys: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str = "user") -> "User":
        name = _require(data, "name", where)
        return cls(
            name=name,
            description=data.get("description", ""),
            roles=list(data.get("roles", [])),
            public_keys=list(data.get("public-keys", [])),
        )

    def to_api_body(self) -> "dict[str, Any]":
        # Role assignment is done through dedicated endpoints, not the user
        # create/update body.
        return {
            "username": self.name,
            "description": self.description,
        }


# --------------------------------------------------------------------------- #
# Target
# --------------------------------------------------------------------------- #
@dataclass
class Target:
    """A Warpgate target (destination).

    ``kind`` selects the option set:

    - ``ssh`` / ``mysql`` / ``postgres``: ``host``, ``port``, ``username``,
      ``auth``.
    - ``http``: ``url``, optional ``external_host``.

    ``auth`` is either a shorthand string (e.g. ``"publickey"``) or a mapping
    ``{"kind": "password", "password": "..."}``.

    ``roles`` lists the role names allowed to reach this target.
    ``group`` is the owning target-group name (``None`` for ungrouped).
    """

    name: str
    kind: str = "ssh"
    description: str = ""
    roles: list[str] = field(default_factory=list)
    group: str | None = None

    # ssh / mysql / postgres
    host: str | None = None
    port: int | None = None
    username: str | None = None
    auth: Any = None

    # http
    url: str | None = None
    external_host: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in TARGET_KINDS:
            raise ConfigError(
                f"target {self.name!r}: invalid kind {self.kind!r} "
                f"(expected one of {', '.join(TARGET_KINDS)})"
            )
        self._validate()

    # -- validation -------------------------------------------------------- #
    def _validate(self) -> None:
        where = f"target {self.name!r}"
        if self.kind == "http":
            if not self.url:
                raise ConfigError(f"{where}: http target requires 'url'")
        else:
            if not self.host:
                raise ConfigError(f"{where}: {self.kind} target requires 'host'")
            self._normalise_auth()

    def _normalise_auth(self) -> None:
        """Normalise ``auth`` into a dict ``{"kind": ..., ...}``."""
        where = f"target {self.name!r}"
        valid = SSH_AUTH_KINDS if self.kind == "ssh" else DB_AUTH_KINDS

        auth = self.auth
        if auth is None:
            auth = "publickey" if self.kind == "ssh" else "password"

        if isinstance(auth, str):
            auth = {"kind": auth}
        elif isinstance(auth, dict):
            auth = dict(auth)
            if "kind" not in auth:
                raise ConfigError(f"{where}: auth mapping requires a 'kind' key")
        else:
            raise ConfigError(f"{where}: auth must be a string or a mapping")

        if auth["kind"] not in valid:
            raise ConfigError(
                f"{where}: invalid auth kind {auth['kind']!r} for {self.kind} "
                f"(expected one of {', '.join(valid)})"
            )
        if auth["kind"] == "password" and "password" not in auth:
            raise ConfigError(f"{where}: password auth requires a 'password'")
        self.auth = auth

    def _auth_wire(self) -> "dict[str, Any]":
        """The ``auth`` block in Warpgate wire format (PascalCase ``kind``)."""
        auth = dict(self.auth)
        auth["kind"] = _AUTH_WIRE[auth["kind"]]
        return auth

    # -- (de)serialisation ------------------------------------------------- #
    @classmethod
    def from_dict(cls, data: dict[str, Any], *, group: str | None = None,
                  where: str = "target") -> "Target":
        name = _require(data, "name", where)
        return cls(
            name=name,
            kind=data.get("kind", "ssh"),
            description=data.get("description", ""),
            roles=list(data.get("roles", [])),
            group=group,
            host=data.get("host"),
            port=data.get("port"),
            username=data.get("username"),
            auth=data.get("auth"),
            url=data.get("url"),
            external_host=data.get("external-host", data.get("external_host")),
        )

    def to_config_dict(self) -> "dict[str, Any]":
        """This target as a YAML-config-style mapping (dash-cased keys).

        Round-trips with :meth:`from_dict`: the output is valid under the
        ``targets:`` key of a wgman config file.
        """
        data: dict[str, Any] = {"name": self.name, "kind": self.kind}
        if self.description:
            data["description"] = self.description
        if self.kind == "http":
            data["url"] = self.url
            if self.external_host:
                data["external-host"] = self.external_host
        else:
            data["host"] = self.host
            if self.port is not None:
                data["port"] = self.port
            if self.username is not None:
                data["username"] = self.username
            if self.auth is not None:
                auth = dict(self.auth)
                data["auth"] = (
                    auth["kind"] if list(auth) == ["kind"] else auth
                )
        if self.roles:
            data["roles"] = list(self.roles)
        return data

    def options(self) -> "dict[str, Any]":
        """The Warpgate ``options`` block, in admin-API wire format.

        The ``kind`` discriminator and the ``auth.kind`` are PascalCase, and
        the ``tls`` block is included where the API requires it
        (http/mysql/postgres). Verified against warpgate 0.25.4.
        """
        if self.kind == "ssh":
            return {
                "kind": _KIND_WIRE["ssh"],
                "host": self.host,
                "port": self.port if self.port is not None else 22,
                "username": self.username or "root",
                "auth": self._auth_wire(),
            }
        if self.kind in ("mysql", "postgres"):
            default_port = 3306 if self.kind == "mysql" else 5432
            return {
                "kind": _KIND_WIRE[self.kind],
                "host": self.host,
                "port": self.port if self.port is not None else default_port,
                "username": self.username or "root",
                "auth": self._auth_wire(),
                "tls": dict(_DEFAULT_TLS),
            }
        if self.kind == "http":
            opts: dict[str, Any] = {
                "kind": _KIND_WIRE["http"],
                "url": self.url,
                "tls": dict(_DEFAULT_TLS),
            }
            if self.external_host:
                opts["external_host"] = self.external_host
            return opts
        raise ConfigError(f"target {self.name!r}: unhandled kind {self.kind!r}")

    def to_api_body(self, *, group_id: str | None = None) -> "dict[str, Any]":
        """Body for ``POST/PUT /targets`` (``options`` flattened)."""
        body: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "options": self.options(),
        }
        if group_id is not None:
            body["group_id"] = group_id
        return body


# --------------------------------------------------------------------------- #
# TargetGroup
# --------------------------------------------------------------------------- #
@dataclass
class TargetGroup:
    """A Warpgate target-group: an organisational container for targets."""

    name: str
    color: str | None = None
    description: str = ""
    targets: list[Target] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.color is not None and self.color not in GROUP_COLORS:
            raise ConfigError(
                f"target-group {self.name!r}: invalid color {self.color!r} "
                f"(expected one of {', '.join(GROUP_COLORS)})"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str = "target-group") -> "TargetGroup":
        name = _require(data, "name", where)
        targets = [
            Target.from_dict(t, group=name, where=f"target-group {name!r}")
            for t in data.get("targets", [])
        ]
        return cls(
            name=name,
            color=data.get("color"),
            description=data.get("description", ""),
            targets=targets,
        )

    def to_api_body(self) -> "dict[str, Any]":
        body: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.color is not None:
            body["color"] = _COLOR_WIRE[self.color]
        return body


# --------------------------------------------------------------------------- #
# PruneConfig
# --------------------------------------------------------------------------- #
# Resource kinds that prune can act on (YAML/attribute names).
PRUNE_KINDS = ("targets", "target-groups", "roles", "users")


@dataclass
class PruneConfig:
    """Scopes which resource kinds ``--prune`` deletes, and protects names.

    Each ``prune_<kind>`` flag turns deletion on/off for that kind. The
    ``keep_<kind>`` sets list names that are never deleted even when that
    kind is pruned (e.g. always keep the ``admin`` user).

    The default (:meth:`default`) prunes targets only — the safe choice
    when the desired state is sourced from Odoo, which supplies targets but
    no users/roles/groups.
    """

    prune_targets: bool = True
    prune_target_groups: bool = False
    prune_roles: bool = False
    prune_users: bool = False

    keep_targets: set[str] = field(default_factory=set)
    keep_target_groups: set[str] = field(default_factory=set)
    keep_roles: set[str] = field(default_factory=set)
    keep_users: set[str] = field(default_factory=set)

    @classmethod
    def default(cls) -> "PruneConfig":
        """The default prune scope: targets only."""
        return cls()

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str = "prune") -> "PruneConfig":
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: must be a mapping")

        known = set(PRUNE_KINDS) | {f"keep-{k}" for k in PRUNE_KINDS}
        for key in data:
            if key not in known:
                raise ConfigError(
                    f"{where}: unknown key {key!r} "
                    f"(expected one of {', '.join(sorted(known))})"
                )

        def flag(kind: str, default: bool) -> bool:
            return bool(data.get(kind, default))

        def keep(kind: str) -> set[str]:
            return set(data.get(f"keep-{kind}", []))

        return cls(
            prune_targets=flag("targets", True),
            prune_target_groups=flag("target-groups", False),
            prune_roles=flag("roles", False),
            prune_users=flag("users", False),
            keep_targets=keep("targets"),
            keep_target_groups=keep("target-groups"),
            keep_roles=keep("roles"),
            keep_users=keep("users"),
        )


# --------------------------------------------------------------------------- #
# ServerConfig
# --------------------------------------------------------------------------- #
@dataclass
class ServerConfig:
    """Connection details for one Warpgate server.

    ``ssh_host``/``ssh_port`` describe the *bastion's* SSH listener — the
    address an ssh client dials. They are optional and used only to render
    an ssh config without asking the server (see ``ssh-config
    --from-odoo``): the admin declares them once so end users, who hold no
    Warpgate credential, need not discover them.

    They are NOT the admin API endpoint (that is ``url``), and NOT any
    backend machine's address.

    ``api_key`` is likewise optional, because it is what authenticates
    *toward the bastion* rather than what identifies a server. Commands
    that never call Warpgate — ``ssh-config --from-odoo``, whose whole
    premise is that the user holds no token — must be able to read the
    ``ssh-host`` of a server the config describes without inventing a
    credential for it. Every code path that does authenticate goes
    through :meth:`require_api_key`, which fails with a remedy naming the
    server.
    """

    name: str
    url: str
    api_key: str | None = None
    verify_tls: bool = True
    ssh_host: str | None = None
    ssh_port: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str = "server") -> "ServerConfig":
        name = _require(data, "name", where)
        url = _require(data, "url", f"server {name!r}")
        return cls(
            name=name,
            url=url,
            api_key=data.get("api-key"),
            verify_tls=bool(data.get("verify-tls", True)),
            ssh_host=data.get("ssh-host"),
            ssh_port=_optional_port(data.get("ssh-port"), f"server {name!r}"),
        )

    def require_api_key(self) -> str:
        """The token, or a :class:`ConfigError` naming the server.

        Call this at the point of authentication, never at parse time: a
        config may legitimately describe a server it holds no token for.
        """
        if not self.api_key:
            raise ConfigError(
                f"server {self.name!r}: no 'api-key' in config, but this "
                "command authenticates to Warpgate. Add 'api-key' to that "
                "server (use ${ENV} for the secret), or use a command that "
                "needs no token, such as 'ssh-config --from-odoo'"
            )
        return self.api_key


def _optional_port(value: Any, where: str) -> int | None:
    """Parse an optional TCP port, rejecting out-of-range values loudly."""
    if value is None:
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: ssh-port must be a number, got {value!r}") from None
    if not 0 < port < 65536:
        raise ConfigError(f"{where}: ssh-port {port} out of range (1-65535)")
    return port
