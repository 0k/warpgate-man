"""Exception hierarchy for wgman."""

from __future__ import annotations


class WgmanError(Exception):
    """Base class for all wgman errors."""


class ConfigError(WgmanError):
    """Raised when the YAML configuration is invalid or cannot be parsed."""


class InterpolationError(ConfigError):
    """Raised when an ``${ENV}`` reference cannot be resolved."""


class ApiError(WgmanError):
    """Raised when a Warpgate API returns an error response."""

    def __init__(self, message: str, status_code: int | None = None,
                 body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AuthenticationError(ApiError):
    """HTTP 401: Warpgate rejected the token.

    The token is wrong, revoked, or expired — Warpgate could not tell *who*
    is calling.
    """


class AuthorizationError(ApiError):
    """HTTP 403: Warpgate knows the caller but refuses the operation.

    Typically a personal (non-admin) token used on the admin API.
    """


class UnsupportedApiError(ApiError):
    """HTTP 404 on an API root: the server does not expose that API.

    Raised when a whole API surface is missing (e.g. a Warpgate too old to
    serve the user API), not when a single resource is absent.
    """
