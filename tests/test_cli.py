"""End-to-end CLI tests driving main() against a mocked Warpgate API."""

import textwrap

import httpx
import pytest
import respx

from wgman.cli import main

BASE = "https://wg.example.com:8888"
API = f"{BASE}/@warpgate/admin/api"

CONFIG = textwrap.dedent(
    """
    servers:
      - name: prod
        url: https://wg.example.com:8888
        api-key: ${WG_TOKEN}
    roles:
      - name: admin
    targets:
      - name: jump
        kind: ssh
        host: 192.168.1.1
        roles: [admin]
    users:
      - name: alice
        roles: [admin]
    """
)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WG_TOKEN", "secret")
    p = tmp_path / "wgman.yaml"
    p.write_text(CONFIG)
    return p


def _mock_empty_server():
    """Mock a fresh Warpgate server with no existing entities."""
    respx.get(f"{API}/roles").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/target-groups").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{API}/targets").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/users").mock(return_value=httpx.Response(200, json=[]))


@respx.mock
def test_diff_does_not_write(config_file, capsys):
    _mock_empty_server()
    create = respx.post(f"{API}/roles").mock(
        return_value=httpx.Response(200, json={"id": "r1"})
    )
    rc = main(["--config", str(config_file), "diff"])
    assert rc == 0
    assert not create.called  # diff must not write
    out = capsys.readouterr().out
    assert "create" in out


@respx.mock
def test_apply_creates_entities(config_file, capsys):
    _mock_empty_server()
    role = respx.post(f"{API}/roles").mock(
        return_value=httpx.Response(200, json={"id": "r-admin", "name": "admin"})
    )
    target = respx.post(f"{API}/targets").mock(
        return_value=httpx.Response(200, json={"id": "t1", "name": "jump"})
    )
    user = respx.post(f"{API}/users").mock(
        return_value=httpx.Response(200, json={"id": "u1", "username": "alice"})
    )
    respx.post(f"{API}/targets/t1/roles/r-admin").mock(
        return_value=httpx.Response(201)
    )
    respx.post(f"{API}/users/u1/roles/r-admin").mock(
        return_value=httpx.Response(201)
    )

    rc = main(["--config", str(config_file), "apply"])
    assert rc == 0
    assert role.called and target.called and user.called


@respx.mock
def test_unknown_server_returns_usage_error(config_file):
    rc = main(["--config", str(config_file), "--server", "ghost", "diff"])
    assert rc == 2


def test_missing_config_returns_usage_error(tmp_path):
    rc = main(["--config", str(tmp_path / "nope.yaml"), "diff"])
    assert rc == 2
