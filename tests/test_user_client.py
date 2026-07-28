"""Tests for the Warpgate user API client and the ApiError translation.

The user API (``/@warpgate/api``) is what an ordinary bastion user may read:
their own identity, the server's connection info, and the targets their roles
grant. It shares the token header with the admin API but nothing else.
"""

import httpx
import pytest
import respx

from wgman.client import WarpgateClient, WarpgateUserClient
from wgman.exceptions import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    UnsupportedApiError,
)

BASE = "https://wg.example.com:8888"
USER_API = f"{BASE}/@warpgate/api"
ADMIN_API = f"{BASE}/@warpgate/admin/api"


@pytest.fixture
def client():
    c = WarpgateUserClient(BASE, "tok")
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# routing and auth
# --------------------------------------------------------------------------- #
class TestUserApiRouting:
    @respx.mock
    def test_targets_hit_the_user_api_not_the_admin_one(self, client):
        # The whole point: a personal token must never be sent to the admin
        # API, which would refuse it.
        route = respx.get(f"{USER_API}/targets").mock(
            return_value=httpx.Response(200, json=[])
        )
        client.list_targets()
        assert route.called

    @respx.mock
    def test_same_token_header_as_admin_client(self, client):
        route = respx.get(f"{USER_API}/info").mock(
            return_value=httpx.Response(200, json={})
        )
        client.get_info()
        assert route.calls.last.request.headers["X-Warpgate-Token"] == "tok"

    @respx.mock
    def test_info_returns_payload(self, client):
        respx.get(f"{USER_API}/info").mock(
            return_value=httpx.Response(200, json={"username": "alice"})
        )
        assert client.get_info() == {"username": "alice"}

    @respx.mock
    def test_targets_returns_payload(self, client):
        respx.get(f"{USER_API}/targets").mock(
            return_value=httpx.Response(
                200, json=[{"id": "1", "name": "web", "kind": "Ssh"}]
            )
        )
        assert client.list_targets()[0]["name"] == "web"


# --------------------------------------------------------------------------- #
# error translation
# --------------------------------------------------------------------------- #
class TestErrorTranslation:
    @respx.mock
    def test_401_says_the_token_was_rejected(self, client):
        respx.get(f"{USER_API}/info").mock(
            return_value=httpx.Response(401, text="nope")
        )
        with pytest.raises(AuthenticationError, match="authentication failed"):
            client.get_info()

    @respx.mock
    def test_401_keeps_status_and_body(self, client):
        respx.get(f"{USER_API}/info").mock(
            return_value=httpx.Response(401, text="nope")
        )
        with pytest.raises(AuthenticationError) as exc:
            client.get_info()
        assert exc.value.status_code == 401
        assert "nope" in (exc.value.body or "")

    @respx.mock
    def test_403_on_user_api_names_that_api(self, client):
        respx.get(f"{USER_API}/targets").mock(
            return_value=httpx.Response(403, text="denied")
        )
        with pytest.raises(AuthorizationError, match="user API"):
            client.list_targets()

    @respx.mock
    def test_404_means_the_api_is_absent(self, client):
        # This client only calls collection endpoints, so a 404 is "no such
        # API" (server too old), never "no such resource".
        respx.get(f"{USER_API}/targets").mock(
            return_value=httpx.Response(404, text="")
        )
        with pytest.raises(UnsupportedApiError, match="user API"):
            client.list_targets()

    @respx.mock
    def test_other_errors_stay_generic(self, client):
        respx.get(f"{USER_API}/info").mock(
            return_value=httpx.Response(500, text="boom")
        )
        with pytest.raises(ApiError) as exc:
            client.get_info()
        assert exc.value.status_code == 500

    @respx.mock
    def test_admin_client_403_names_the_admin_api(self):
        # A personal token used with 'apply' must be told what it lacks.
        with WarpgateClient(BASE, "tok") as admin:
            respx.get(f"{ADMIN_API}/targets").mock(
                return_value=httpx.Response(403, text="denied")
            )
            with pytest.raises(AuthorizationError, match="admin API"):
                admin.list_targets()


# --------------------------------------------------------------------------- #
# the admin client must keep working exactly as before
# --------------------------------------------------------------------------- #
class TestAdminClientUnchanged:
    @respx.mock
    def test_still_targets_the_admin_api(self):
        with WarpgateClient(BASE, "tok") as admin:
            route = respx.get(f"{ADMIN_API}/targets").mock(
                return_value=httpx.Response(200, json=[])
            )
            admin.list_targets()
            assert route.called

    def test_exposes_url_and_api_key(self):
        with WarpgateClient(BASE, "tok") as admin:
            assert admin.url == BASE
            assert admin.api_key == "tok"
