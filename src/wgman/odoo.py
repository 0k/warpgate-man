"""Odoo as a source of desired Warpgate state.

Queries an Odoo server and maps records to Warpgate entities:

- **targets**: ``maintenance.equipment`` records selected by a configurable
  Odoo domain (default: ``ssh_target`` set) become SSH targets. The
  ``ssh_target`` field format is ``[user@]DOMAIN_OR_IP[:PORT]`` (defaults
  ``root`` / ``22``); the target label is the equipment ``name``; auth is
  ``publickey``.
- **users** (optional): ``res.users`` records selected by per-entry Odoo
  domains become Warpgate users (username = Odoo ``login``), each entry
  granting its listed roles.
- **roles**: every role referenced by the ``targets``/``users`` sections is
  auto-declared, so access wiring (shared role => access) needs no separate
  role listing.

Selection is expressed as raw Odoo *domains* in the YAML config (lists of
``[field, operator, value]`` triplets plus ``"|"``/``"&"``/``"!"`` operator
strings), passed verbatim to ``search_read`` — full expressive freedom, no
wgman-side query logic to maintain.

The RPC transport is the ``oerpc`` library (JSON-RPC), imported lazily so
wgman keeps working without it when no Odoo source is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .exceptions import ConfigError, WgmanError
from .models import Role, Target, User


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]

TARGETS_MODEL = "maintenance.equipment"
TARGETS_FIELD = "ssh_target"
USERS_MODEL = "res.users"
SSH_KEYS_MODEL = "ssh.key"

# Loose OpenSSH public key shape: type, base64 blob, optional comment.
_OPENSSH_KEY_RE = re.compile(
    r"^(?:ssh|ecdsa)-[a-z0-9-]+\s+[A-Za-z0-9+/=]+(?:\s+\S.*)?$"
)

# Default target selection: equipments with an ssh_target set.
DEFAULT_TARGETS_DOMAIN: list[Any] = [[TARGETS_FIELD, "!=", False]]

_DOMAIN_OPERATORS = ("|", "&", "!")

# [user@]HOST[:PORT] — HOST is a domain name or IP (no spaces, no '@'/':').
_SSH_TARGET_RE = re.compile(
    r"""^
    (?:(?P<user>[^@\s:]+)@)?          # optional user@
    (?P<host>[A-Za-z0-9._-]+)         # domain or IPv4
    (?::(?P<port>\d+))?               # optional :port
    $""",
    re.VERBOSE,
)

# A query callable: (model, domain, fields) -> records.
Query = Callable[[str, list[Any], list[str]], list[dict[str, Any]]]


class OdooError(WgmanError):
    """Raised when talking to Odoo fails (connection, auth, RPC)."""


def normalize_domain(value: Any, where: str) -> list[Any]:
    """Validate an Odoo domain from YAML and return it as a plain list.

    A domain is a list whose elements are either operator strings
    (``"|"``, ``"&"``, ``"!"``) or ``[field, operator, value]`` triplets.
    """
    if not isinstance(value, list):
        raise ConfigError(f"{where}: domain must be a list")
    out: list[Any] = []
    for i, elt in enumerate(value):
        if isinstance(elt, str):
            if elt not in _DOMAIN_OPERATORS:
                raise ConfigError(
                    f"{where}: domain[{i}]: invalid operator {elt!r} "
                    f"(expected one of {', '.join(_DOMAIN_OPERATORS)})"
                )
            out.append(elt)
        elif isinstance(elt, list) and len(elt) == 3 and isinstance(elt[0], str):
            out.append(list(elt))
        else:
            raise ConfigError(
                f"{where}: domain[{i}]: expected [field, operator, value] "
                f"or an operator string, got {elt!r}"
            )
    return out


@dataclass
class OdooTargetsConfig:
    """Target selection (Odoo domain) + roles granted to all Odoo targets."""

    domain: list[Any] = field(
        default_factory=lambda: [list(t) for t in DEFAULT_TARGETS_DOMAIN]
    )
    roles: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], where: str = "odoo.targets"
    ) -> "OdooTargetsConfig":
        kwargs: dict[str, Any] = {}
        if "domain" in data:
            kwargs["domain"] = normalize_domain(data["domain"], where)
        kwargs["roles"] = list(data.get("roles", []))
        unknown = set(data) - {"domain", "roles"}
        if unknown:
            raise ConfigError(
                f"{where}: unknown key(s): {', '.join(sorted(unknown))}"
            )
        return cls(**kwargs)


@dataclass
class OdooUserSelection:
    """One user selection: an Odoo domain over res.users + granted roles."""

    domain: list[Any]
    roles: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], where: str = "odoo.users[]"
    ) -> "OdooUserSelection":
        domain = normalize_domain(_require(data, "domain", where), where)
        roles = list(data.get("roles", []))
        unknown = set(data) - {"domain", "roles"}
        if unknown:
            raise ConfigError(
                f"{where}: unknown key(s): {', '.join(sorted(unknown))}"
            )
        return cls(domain=domain, roles=roles)


@dataclass
class OdooConfig:
    """Connection details + selection config for the Odoo source."""

    url: str
    db: str
    user: str
    password: str | None = None
    verify_tls: bool = True
    targets: OdooTargetsConfig = field(default_factory=OdooTargetsConfig)
    users: list[OdooUserSelection] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str = "odoo") -> "OdooConfig":
        targets_data = data.get("targets")
        users_data = data.get("users", [])
        if not isinstance(users_data, list):
            raise ConfigError(f"{where}.users: must be a list of selections")
        return cls(
            url=_require(data, "url", where),
            db=_require(data, "db", where),
            user=_require(data, "user", where),
            password=data.get("password"),
            verify_tls=bool(data.get("verify-tls", True)),
            targets=(
                OdooTargetsConfig.from_dict(targets_data, f"{where}.targets")
                if targets_data
                else OdooTargetsConfig()
            ),
            users=[
                OdooUserSelection.from_dict(u, f"{where}.users[{i}]")
                for i, u in enumerate(users_data)
            ],
        )


@dataclass
class OdooState:
    """The desired-state entities fetched from Odoo."""

    targets: list[Target] = field(default_factory=list)
    users: list[User] = field(default_factory=list)
    roles: list[Role] = field(default_factory=list)


def parse_ssh_target(value: str, *, where: str = "ssh-target") -> tuple[str, str, int]:
    """Parse ``[user@]DOMAIN_OR_IP[:PORT]`` into ``(username, host, port)``.

    Defaults: user ``root``, port ``22``.
    """
    match = _SSH_TARGET_RE.match(value.strip())
    if not match:
        raise ConfigError(
            f"{where}: invalid ssh target {value!r} "
            "(expected [user@]DOMAIN_OR_IP[:PORT])"
        )
    port_s = match.group("port")
    port = int(port_s) if port_s else 22
    if not (0 < port < 65536):
        raise ConfigError(f"{where}: invalid port {port} in {value!r}")
    return (match.group("user") or "root", match.group("host"), port)


def equipment_to_target(
    record: dict[str, Any], roles: list[str] | None = None
) -> Target:
    """Map one ``maintenance.equipment`` record to an SSH :class:`Target`."""
    name = record.get("name")
    if not name:
        raise ConfigError(
            f"odoo: equipment id={record.get('id')!r} has no name"
        )
    ssh_target = record.get(TARGETS_FIELD)
    if not ssh_target or not isinstance(ssh_target, str):
        raise ConfigError(
            f"odoo: equipment {name!r} has no usable {TARGETS_FIELD!r} value"
        )
    username, host, port = parse_ssh_target(
        ssh_target, where=f"odoo: equipment {name!r}"
    )
    return Target(
        name=name,
        kind="ssh",
        host=host,
        port=port,
        username=username,
        auth="publickey",
        roles=list(roles or []),
    )


def _make_default_query(config: OdooConfig) -> Query:
    """Log in to Odoo via oerpc and return a ``search_read`` query callable."""
    try:
        from oerpc.api.common import Odoo, ApiError
        from oerpc.rpc import RpcError
    except ImportError as exc:
        raise OdooError(
            "the 'oerpc' package is required for the Odoo source "
            "(pip install warpgate-man[odoo])"
        ) from exc

    oe = Odoo(config.url, verify=config.verify_tls)
    try:
        oe.session.login(config.db, config.user, config.password)
    except (ApiError, RpcError) as exc:
        raise OdooError(
            f"cannot authenticate to Odoo at {config.url!r} "
            f"(db {config.db!r}, user {config.user!r}): {exc}"
        ) from exc

    def query(
        model: str, domain: list[Any], fields: list[str]
    ) -> list[dict[str, Any]]:
        try:
            return oe.object.search_read(model, domain, fields)
        except (ApiError, RpcError) as exc:
            raise OdooError(
                f"cannot read {model} records from Odoo: {exc}"
            ) from exc

    return query


def record_to_user(record: dict[str, Any], roles: list[str]) -> User:
    """Map one ``res.users`` record to a Warpgate :class:`User`."""
    login = record.get("login")
    if not login or not isinstance(login, str):
        raise ConfigError(
            f"odoo: user id={record.get('id')!r} has no usable login"
        )
    return User(name=login, roles=list(roles))


def normalize_public_key(value: Any, *, where: str) -> str | None:
    """Validate/normalise one OpenSSH public key from an ``ssh.key`` record.

    Tolerates keys pasted with hard line wraps (newlines splitting the
    base64 blob, optionally with ``\\`` line-continuations): fragments of
    the blob are glued back together. Returns the normalised single-line
    key, or ``None`` for empty values. Raises :class:`ConfigError` for
    non-empty values that do not look like an OpenSSH public key (better
    to fail loudly than to push garbage credentials to the bastion).
    """
    if not value or not isinstance(value, str):
        return None
    # Undo hard line wraps: drop backslash continuations, then tokenize on
    # any whitespace (collapses runs of spaces AND newline fragments).
    unwrapped = value.replace("\\\r\n", "").replace("\\\n", "")
    parts = unwrapped.split()
    key = " ".join(parts)
    if len(parts) > 2:
        blob = parts[1]
        i = 2
        while i < len(parts) and re.fullmatch(r"[A-Za-z0-9+/=]+", parts[i]) \
                and not parts[i].startswith(("ssh-", "ecdsa-")):
            # A short trailing pure-base64 token could theoretically be a
            # comment, but comments are overwhelmingly non-base64
            # (user@host); gluing is the safe default for wrapped keys.
            blob += parts[i]
            i += 1
        key = " ".join([parts[0], blob] + parts[i:])
    if not _OPENSSH_KEY_RE.match(key):
        raise ConfigError(
            f"{where}: value does not look like an OpenSSH public key: "
            f"{key[:60]!r}..."
        )
    return key


def fetch_state(
    config: OdooConfig,
    *,
    query: Query | None = None,
) -> OdooState:
    """Fetch the full Odoo-sourced desired state (targets, users, roles).

    *query* may be injected for testing; it defaults to the oerpc-based
    JSON-RPC implementation (single login, one ``search_read`` per model
    selection).
    """
    q = query or _make_default_query(config)

    targets = [
        equipment_to_target(record, config.targets.roles)
        for record in q(
            TARGETS_MODEL, config.targets.domain, ["name", TARGETS_FIELD]
        )
    ]

    # Users: merge roles per login across selections (a user may match
    # several selections; roles accumulate). Track Odoo ids for key lookup.
    user_roles: dict[str, list[str]] = {}
    user_ids: dict[int, str] = {}
    for selection in config.users:
        for record in q(USERS_MODEL, selection.domain, ["login"]):
            user = record_to_user(record, selection.roles)
            acc = user_roles.setdefault(user.name, [])
            for role in user.roles:
                if role not in acc:
                    acc.append(role)
            if isinstance(record.get("id"), int):
                user_ids[record["id"]] = user.name

    # SSH public keys: one query for all selected users (ssh.key model,
    # custom Elabore addon: user_id + key).
    user_keys: dict[str, list[str]] = {}
    if user_ids:
        for record in q(
            SSH_KEYS_MODEL,
            [["user_id", "in", list(user_ids)]],
            ["user_id", "key"],
        ):
            # user_id comes back as [id, display_name] over RPC.
            raw_uid = record.get("user_id")
            uid = raw_uid[0] if isinstance(raw_uid, list) else raw_uid
            if not isinstance(uid, int):
                continue
            login = user_ids.get(uid)
            if login is None:
                continue
            key = normalize_public_key(
                record.get("key"),
                where=f"odoo: ssh.key id={record.get('id')!r} of {login!r}",
            )
            if key and key not in user_keys.setdefault(login, []):
                user_keys[login].append(key)

    users = [
        User(name=login, roles=roles,
             public_keys=user_keys.get(login, []))
        for login, roles in user_roles.items()
    ]

    # Auto-declare every referenced role.
    role_names: list[str] = []
    for name in config.targets.roles:
        if name not in role_names:
            role_names.append(name)
    for selection in config.users:
        for name in selection.roles:
            if name not in role_names:
                role_names.append(name)
    roles = [Role(name=name) for name in role_names]

    return OdooState(targets=targets, users=users, roles=roles)
