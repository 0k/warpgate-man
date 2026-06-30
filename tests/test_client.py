"""Tests for the Warpgate admin API HTTP client (mocked with respx)."""

import httpx
import pytest
import respx

from wgman.client import WarpgateClient
from wgman.exceptions import ApiError

BASE = "https://wg.example.com:8888"
API = f"{BASE}/@warpgate/admin/api"


@pytest.fixture
def client():
    c = WarpgateClient(BASE, "tok")
    yield c
    c.close()


@respx.mock
def test_token_header_sent(client):
    route = respx.get(f"{API}/targets").mock(
        return_value=httpx.Response(200, json=[])
    )
    client.list_targets()
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["X-Warpgate-Token"] == "tok"


@respx.mock
def test_list_targets_returns_json(client):
    respx.get(f"{API}/targets").mock(
        return_value=httpx.Response(200, json=[{"id": "1", "name": "a"}])
    )
    assert client.list_targets() == [{"id": "1", "name": "a"}]


@respx.mock
def test_create_target_posts_body(client):
    route = respx.post(f"{API}/targets").mock(
        return_value=httpx.Response(200, json={"id": "new"})
    )
    body = {"name": "x", "options": {"kind": "ssh"}}
    result = client.create_target(body)
    assert result == {"id": "new"}
    import json
    assert json.loads(route.calls.last.request.content) == body


@respx.mock
def test_role_endpoint_uses_singular_path(client):
    # Warpgate update/delete role lives at /role/{id}, not /roles/{id}.
    route = respx.put(f"{API}/role/abc").mock(
        return_value=httpx.Response(200, json={"id": "abc"})
    )
    client.update_role("abc", {"name": "r"})
    assert route.called


@respx.mock
def test_assign_target_role(client):
    route = respx.post(f"{API}/targets/t1/roles/r1").mock(
        return_value=httpx.Response(201)
    )
    client.add_target_role("t1", "r1")
    assert route.called


@respx.mock
def test_assign_user_role_sends_json_body(client):
    # The user-role endpoint declares a JSON body; we must send one (empty
    # object) or Warpgate responds HTTP 415.
    route = respx.post(f"{API}/users/u1/roles/r1").mock(
        return_value=httpx.Response(201, json={})
    )
    client.add_user_role("u1", "r1")
    assert route.called
    req = route.calls.last.request
    assert req.content == b"{}"
    assert "application/json" in req.headers["content-type"]


@respx.mock
def test_204_returns_none(client):
    respx.delete(f"{API}/targets/t1").mock(return_value=httpx.Response(204))
    assert client.delete_target("t1") is None


@respx.mock
def test_http_error_raises_api_error(client):
    respx.get(f"{API}/targets").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(ApiError) as exc:
        client.list_targets()
    assert exc.value.status_code == 500
    assert "boom" in (exc.value.body or "")


@respx.mock
def test_transport_error_raises_api_error(client):
    respx.get(f"{API}/targets").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with pytest.raises(ApiError, match="failed"):
        client.list_targets()
