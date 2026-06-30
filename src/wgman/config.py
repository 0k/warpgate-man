"""YAML configuration loading, validation and ``${ENV}`` interpolation.

The configuration is a single YAML file describing:

- ``servers``        — Warpgate servers to manage (url + api-key).
- ``target-groups``  — organisational containers, each holding ``targets``.
- ``targets``        — ungrouped targets at the root.
- ``roles``          — Warpgate roles.
- ``users``          — Warpgate users and their roles.

Keys are dash-cased. Secrets are referenced via ``${VAR}`` and interpolated
from the environment at load time.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ConfigError, InterpolationError
from .models import Role, ServerConfig, Target, TargetGroup, User

# Default locations searched when --config is not given, in order.
DEFAULT_CONFIG_PATHS = (
    Path("wgman.yaml"),
    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    / "wgman"
    / "config.yaml",
    Path("/etc/wgman/config.yaml"),
)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate(value: str) -> str:
    """Replace every ``${VAR}`` in *value* with ``os.environ['VAR']``.

    Raises :class:`InterpolationError` if a referenced variable is unset.
    """

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise InterpolationError(
                f"environment variable {name!r} referenced in config is not set"
            )
        return os.environ[name]

    return _ENV_RE.sub(repl, value)


def _interpolate_tree(node: Any) -> Any:
    """Recursively interpolate ``${ENV}`` in all string leaves of *node*."""
    if isinstance(node, str):
        return interpolate(node)
    if isinstance(node, dict):
        return {k: _interpolate_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate_tree(v) for v in node]
    return node


@dataclass
class Config:
    """The fully-parsed configuration."""

    servers: list[ServerConfig] = field(default_factory=list)
    target_groups: list[TargetGroup] = field(default_factory=list)
    root_targets: list[Target] = field(default_factory=list)
    roles: list[Role] = field(default_factory=list)
    users: list[User] = field(default_factory=list)

    # -- convenience accessors -------------------------------------------- #
    def server(self, name: str) -> ServerConfig:
        for s in self.servers:
            if s.name == name:
                return s
        raise ConfigError(f"no server named {name!r} in config")

    def all_targets(self) -> list[Target]:
        """Every target, grouped and ungrouped, as a flat list."""
        targets = list(self.root_targets)
        for group in self.target_groups:
            targets.extend(group.targets)
        return targets

    # -- parsing ----------------------------------------------------------- #
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        if not isinstance(data, dict):
            raise ConfigError("top-level config must be a mapping")

        _validate_unique(
            [s.get("name") for s in data.get("servers", [])], "server"
        )

        servers = [
            ServerConfig.from_dict(s) for s in data.get("servers", [])
        ]
        target_groups = [
            TargetGroup.from_dict(g) for g in data.get("target-groups", [])
        ]
        root_targets = [
            Target.from_dict(t, where="target") for t in data.get("targets", [])
        ]
        roles = [Role.from_dict(r) for r in data.get("roles", [])]
        users = [User.from_dict(u) for u in data.get("users", [])]

        config = cls(
            servers=servers,
            target_groups=target_groups,
            root_targets=root_targets,
            roles=roles,
            users=users,
        )
        config.validate()
        return config

    # -- cross-entity validation ------------------------------------------ #
    def validate(self) -> None:
        """Validate cross-references (unique names, roles exist)."""
        _validate_unique([g.name for g in self.target_groups], "target-group")
        _validate_unique([r.name for r in self.roles], "role")
        _validate_unique([u.name for u in self.users], "user")
        _validate_unique([t.name for t in self.all_targets()], "target")

        known_roles = {r.name for r in self.roles}
        for target in self.all_targets():
            for role in target.roles:
                if role not in known_roles:
                    raise ConfigError(
                        f"target {target.name!r} references unknown role "
                        f"{role!r}"
                    )
        for user in self.users:
            for role in user.roles:
                if role not in known_roles:
                    raise ConfigError(
                        f"user {user.name!r} references unknown role {role!r}"
                    )


def _validate_unique(names: list[Any], kind: str) -> None:
    seen: set[Any] = set()
    for name in names:
        if name in seen:
            raise ConfigError(f"duplicate {kind} name {name!r}")
        seen.add(name)


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate the configuration from *path*.

    If *path* is ``None``, the default locations are searched in order.
    """
    resolved = _resolve_path(path)
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {resolved}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {resolved}: {exc}") from exc

    if data is None:
        raise ConfigError(f"config file {resolved} is empty")

    data = _interpolate_tree(data)
    return Config.from_dict(data)


def _resolve_path(path: str | Path | None) -> Path:
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(p) for p in DEFAULT_CONFIG_PATHS)
    raise ConfigError(f"no config file found (searched: {searched})")
