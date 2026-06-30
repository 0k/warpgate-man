"""Exception hierarchy for wgman."""

from __future__ import annotations


class WgmanError(Exception):
    """Base class for all wgman errors."""


class ConfigError(WgmanError):
    """Raised when the YAML configuration is invalid or cannot be parsed."""


class InterpolationError(ConfigError):
    """Raised when an ``${ENV}`` reference cannot be resolved."""


class ApiError(WgmanError):
    """Raised when the Warpgate admin API returns an error response."""

    def __init__(self, message: str, status_code: int | None = None,
                 body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
