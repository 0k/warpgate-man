"""Synchronous HTTP client for the Warpgate admin API.

The admin API is rooted at ``/@warpgate/admin/api`` and authenticated with the
``X-Warpgate-Token`` header. This client exposes thin CRUD wrappers for the
resources wgman manages: targets, target-groups, roles, users, and the
role-assignment join endpoints.

Entities are returned as raw dicts (as the API delivers them); the higher
layers (``reconcile``) map them onto the dataclasses in ``models``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

from .exceptions import ApiError

ADMIN_API_PATH = "/@warpgate/admin/api/"


class WarpgateClient:
    """A thin synchronous client over the Warpgate admin API."""

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        timeout: float = 30.0,
        _client: httpx.Client | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self._base = urljoin(self.url + "/", ADMIN_API_PATH.lstrip("/"))
        self._client = _client or httpx.Client(
            verify=verify_tls,
            timeout=timeout,
            headers={"X-Warpgate-Token": api_key},
        )

    # -- context manager --------------------------------------------------- #
    def __enter__(self) -> "WarpgateClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- low-level --------------------------------------------------------- #
    def _request(
        self, method: str, path: str, *, json: Any | None = None
    ) -> Any:
        endpoint = urljoin(self._base, path.lstrip("/"))
        try:
            resp = self._client.request(method, endpoint, json=json)
        except httpx.HTTPError as exc:
            raise ApiError(f"{method} {path} failed: {exc}") from exc

        if resp.status_code >= 400:
            raise ApiError(
                f"{method} {path} returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

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
