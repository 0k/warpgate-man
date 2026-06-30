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
    """A Warpgate user. ``roles`` are role names granting target access."""

    name: str
    description: str = ""
    roles: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str = "user") -> "User":
        name = _require(data, "name", where)
        return cls(
            name=name,
            description=data.get("description", ""),
            roles=list(data.get("roles", [])),
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
# ServerConfig
# --------------------------------------------------------------------------- #
@dataclass
class ServerConfig:
    """Connection details for one Warpgate server."""

    name: str
    url: str
    api_key: str
    verify_tls: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str = "server") -> "ServerConfig":
        name = _require(data, "name", where)
        url = _require(data, "url", f"server {name!r}")
        api_key = _require(data, "api-key", f"server {name!r}")
        return cls(
            name=name,
            url=url,
            api_key=api_key,
            verify_tls=bool(data.get("verify-tls", True)),
        )
