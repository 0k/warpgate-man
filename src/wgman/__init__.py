"""wgman — declarative manager for Warpgate bastion configuration.

Public API:

- :class:`WarpgateManager`  — high-level entry point (reconcile / diff).
- Models: :class:`Target`, :class:`TargetGroup`, :class:`Role`, :class:`User`,
  :class:`ServerConfig`.
- :func:`load_config` / :class:`Config` — YAML loading and validation.
- :class:`Plan` — the result of a reconcile/diff run.
- Exceptions: :class:`WgmanError`, :class:`ConfigError`, :class:`ApiError`.
"""

from __future__ import annotations

from .client import WarpgateClient
from .config import Config, load_config
from .exceptions import (
    ApiError,
    ConfigError,
    InterpolationError,
    WgmanError,
)
from .manager import WarpgateManager
from .models import Role, ServerConfig, Target, TargetGroup, User
from .reconcile import Action, Change, Plan, Reconciler

__version__ = "0.1.0"

__all__ = [
    "WarpgateManager",
    "WarpgateClient",
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
    "WgmanError",
    "ConfigError",
    "InterpolationError",
    "ApiError",
    "__version__",
]
