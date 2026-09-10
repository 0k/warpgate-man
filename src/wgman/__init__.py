"""wgman — declarative manager for Warpgate bastion configuration.

Public API:

- :class:`WarpgateManager`  — high-level entry point (reconcile / diff).
- Models: :class:`Target`, :class:`TargetGroup`, :class:`Role`, :class:`User`,
  :class:`ServerConfig`.
- :func:`load_config` / :class:`Config` — YAML loading and validation.
- :class:`Plan` — the result of a reconcile/diff run.
- Exceptions: :class:`WgmanError`, :class:`ConfigError`, :class:`ApiError`.

For the *user* side of a Warpgate server (what one authenticated user may
reach, rather than what an admin declares):

- :class:`WarpgateUserClient` — the user API (``/@warpgate/api``).
- :mod:`wgman.sshconfig` — render an ssh client config from it
  (:class:`BastionInfo`, :func:`render`).
- :mod:`wgman.access` — the same "who reaches what" computed from the
  *desired* state instead, for callers holding no bastion credential.
"""

from __future__ import annotations

from . import access, sshconfig
from .client import WarpgateClient, WarpgateUserClient
from .config import Config, load_config
from .exceptions import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    ConfigError,
    InterpolationError,
    UnsupportedApiError,
    WgmanError,
)
from .manager import WarpgateManager
from .models import Role, ServerConfig, Target, TargetGroup, User
from .reconcile import Action, Change, Plan, Reconciler
from .sshconfig import BastionInfo, SshConfigError

__version__ = "0.1.0"

__all__ = [
    "WarpgateManager",
    "WarpgateClient",
    "WarpgateUserClient",
    "Reconciler",
    "Config",
    "load_config",
    "Target",
    "TargetGroup",
    "Role",
    "User",
    "ServerConfig",
    "Plan",
    "Change",
    "Action",
    "sshconfig",
    "access",
    "BastionInfo",
    "WgmanError",
    "ConfigError",
    "InterpolationError",
    "ApiError",
    "AuthenticationError",
    "AuthorizationError",
    "UnsupportedApiError",
    "SshConfigError",
    "__version__",
]
