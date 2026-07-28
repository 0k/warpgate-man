"""Synchronous HTTP clients for the Warpgate HTTP APIs.

Warpgate exposes two distinct API surfaces, both authenticated with the same
``X-Warpgate-Token`` header:

- the **admin API** at ``/@warpgate/admin/api`` — CRUD over targets,
  target-groups, roles and users. Requires an admin token.
- the **user API** at ``/@warpgate/api`` — what the authenticated *user* may
  see: the targets they can reach and the server's connection info. Any
  personal token works; admins are users too.

:class:`_Transport` owns what the two share (base URL, auth header, TLS,
timeout, request/response, error translation); :class:`WarpgateClient` and
:class:`WarpgateUserClient` add the endpoints of their respective API.

Entities are returned as raw dicts (as the API delivers them); the higher
layers (``reconcile``, ``sshconfig``) map them onto the dataclasses in
``models``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

from .exceptions import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    UnsupportedApiError,
)

ADMIN_API_PATH = "/@warpgate/admin/api/"
USER_API_PATH = "/@warpgate/api/"


class _Transport:
    """Shared HTTP plumbing for one Warpgate API surface.

    Holds the connection (URL, token header, TLS, timeout) and turns HTTP
    failures into the :class:`~wgman.exceptions.ApiError` hierarchy, so
    callers get errors phrased as *what went wrong and what to do*, rather
    than a bare status code.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        api_path: str,
        *,
        api_label: str,
        verify_tls: bool = True,
        timeout: float = 30.0,
        _client: httpx.Client | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.api_label = api_label
        self._base = urljoin(self.url + "/", api_path.lstrip("/"))
        self._client = _client or httpx.Client(
            verify=verify_tls,
            timeout=timeout,
            headers={"X-Warpgate-Token": api_key},
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self, method: str, path: str, *, json: Any | None = None
    ) -> Any:
        endpoint = urljoin(self._base, path.lstrip("/"))
        try:
            resp = self._client.request(method, endpoint, json=json)
        except httpx.HTTPError as exc:
            raise ApiError(f"{method} {path} failed: {exc}") from exc

        if resp.status_code >= 400:
            raise self._error(method, path, resp)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _error(
        self, method: str, path: str, resp: httpx.Response
    ) -> ApiError:
        """Translate an HTTP error response into an :class:`ApiError`.

        The raw status and body remain available on the exception; only the
        message is rewritten to state the cause.
        """
        status, body = resp.status_code, resp.text
        if status == 401:
            return AuthenticationError(
                f"authentication failed on {self.url}: Warpgate rejected "
                "the token (wrong, revoked, or expired)",
                status_code=status,
                body=body,
            )
        if status == 403:
            return AuthorizationError(
                f"permission denied on {self.url}: this token may not "
                f"{method} {path} on the {self.api_label}",
                status_code=status,
                body=body,
            )
        return ApiError(
            f"{method} {path} returned HTTP {status}",
            status_code=status,
            body=body,
        )


class WarpgateClient:
    """A thin synchronous client over the Warpgate **admin** API.

    Every endpoint here needs an admin token; a personal token is refused by
    Warpgate with :class:`~wgman.exceptions.AuthorizationError`.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        timeout: float = 30.0,
        _client: httpx.Client | None = None,
    ) -> None:
        self._transport = _Transport(
            url,
            api_key,
            ADMIN_API_PATH,
            api_label="admin API",
            verify_tls=verify_tls,
            timeout=timeout,
            _client=_client,
        )

    @property
    def url(self) -> str:
        return self._transport.url

    @property
    def api_key(self) -> str:
        return self._transport.api_key

    # -- context manager --------------------------------------------------- #
    def __enter__(self) -> "WarpgateClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._transport.close()

    # -- low-level --------------------------------------------------------- #
    def _request(
        self, method: str, path: str, *, json: Any | None = None
    ) -> Any:
        return self._transport.request(method, path, json=json)

    # -- targets ----------------------------------------------------------- #
    def list_targets(self) -> list[dict[str, Any]]:
        return self._request("GET", "targets") or []

    def create_target(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "targets", json=body)

    def update_target(self, target_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"targets/{target_id}", json=body)

    def delete_target(self, target_id: str) -> None:
        self._request("DELETE", f"targets/{target_id}")

    def list_target_roles(self, target_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"targets/{target_id}/roles") or []

    def add_target_role(self, target_id: str, role_id: str) -> None:
        self._request("POST", f"targets/{target_id}/roles/{role_id}")

    def remove_target_role(self, target_id: str, role_id: str) -> None:
        self._request("DELETE", f"targets/{target_id}/roles/{role_id}")

    # -- target groups ----------------------------------------------------- #
    def list_target_groups(self) -> list[dict[str, Any]]:
        return self._request("GET", "target-groups") or []

    def create_target_group(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "target-groups", json=body)

    def update_target_group(
        self, group_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request("PUT", f"target-groups/{group_id}", json=body)

    def delete_target_group(self, group_id: str) -> None:
        self._request("DELETE", f"target-groups/{group_id}")

    # -- roles ------------------------------------------------------------- #
    def list_roles(self) -> list[dict[str, Any]]:
        return self._request("GET", "roles") or []

    def create_role(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "roles", json=body)

    def update_role(self, role_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"role/{role_id}", json=body)

    def delete_role(self, role_id: str) -> None:
        self._request("DELETE", f"role/{role_id}")

    # -- users ------------------------------------------------------------- #
    def list_users(self) -> list[dict[str, Any]]:
        return self._request("GET", "users") or []

    def create_user(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "users", json=body)

    def update_user(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"users/{user_id}", json=body)

    def delete_user(self, user_id: str) -> None:
        self._request("DELETE", f"users/{user_id}")

    def list_user_roles(self, user_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"users/{user_id}/roles") or []

    def add_user_role(self, user_id: str, role_id: str) -> None:
        # This endpoint declares an (optional) JSON request body
        # (AddUserRoleRequest); poem returns HTTP 415 if no JSON is sent, so we
        # always send an empty object.
        self._request("POST", f"users/{user_id}/roles/{role_id}", json={})

    def remove_user_role(self, user_id: str, role_id: str) -> None:
        self._request("DELETE", f"users/{user_id}/roles/{role_id}")

    # -- user public-key credentials ---------------------------------------- #
    def list_user_public_keys(self, user_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"users/{user_id}/credentials/public-keys"
        ) or []

    def add_user_public_key(
        self, user_id: str, label: str, openssh_public_key: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"users/{user_id}/credentials/public-keys",
            json={"label": label, "openssh_public_key": openssh_public_key},
        )

    def delete_user_public_key(self, user_id: str, key_id: str) -> None:
        self._request(
            "DELETE", f"users/{user_id}/credentials/public-keys/{key_id}"
        )


# --------------------------------------------------------------------------- #
# User API
# --------------------------------------------------------------------------- #
class WarpgateUserClient:
    """A thin synchronous client over the Warpgate **user** API.

    This is the caller's own view of the bastion: who Warpgate thinks they
    are, how to reach the SSH listener, and which targets their roles grant.
    Any personal token works — an admin's personal token is filtered by role
    just like anyone else's.

    One credential is *not* usable here: the server's global admin token
    (``--enable-admin-token``). It is a machine credential bound to no user,
    so Warpgate reports no username and returns an empty target list. See
    :meth:`get_info` and :meth:`list_targets`.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        timeout: float = 30.0,
        _client: httpx.Client | None = None,
    ) -> None:
        self._transport = _Transport(
            url,
            api_key,
            USER_API_PATH,
            api_label="user API",
            verify_tls=verify_tls,
            timeout=timeout,
            _client=_client,
        )

    @property
    def url(self) -> str:
        return self._transport.url

    @property
    def api_key(self) -> str:
        return self._transport.api_key

    # -- context manager --------------------------------------------------- #
    def __enter__(self) -> "WarpgateUserClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._transport.close()

    # -- low-level --------------------------------------------------------- #
    def _request(
        self, method: str, path: str, *, json: Any | None = None
    ) -> Any:
        try:
            return self._transport.request(method, path, json=json)
        except ApiError as exc:
            # The user API is a whole surface a server may not have: a 404
            # here means "no such API", not "no such resource" (this client
            # only ever calls collection endpoints, never a resource by id).
            if exc.status_code == 404:
                raise UnsupportedApiError(
                    f"{self._transport.url} does not expose the Warpgate"
                    f" user API at {USER_API_PATH} (server too old?)",
                    status_code=exc.status_code,
                    body=exc.body,
                ) from exc
            raise

    # -- endpoints --------------------------------------------------------- #
    def get_info(self) -> dict[str, Any]:
        """Server info for the authenticated caller.

        Notable keys: ``username`` (the authenticated Warpgate user, or
        ``None`` when the credential is not bound to a user), ``ports`` and
        ``external_hosts`` (per-protocol maps giving the externally reachable
        host/port, honouring reverse-proxy and NAT configuration).
        """
        return self._request("GET", "info") or {}

    def list_targets(self) -> list[dict[str, Any]]:
        """The targets the authenticated user can reach.

        Warpgate filters this server-side by role intersection; the entries
        carry ``name``/``kind``/``description``/``group`` but never the
        backend host, port or username (those stay server-side).
        """
        return self._request("GET", "targets") or []
