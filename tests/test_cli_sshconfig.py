"""End-to-end CLI tests for the ``ssh-config`` subcommand.

This is the end-user path: the same config file and the same ``api-key``
field as the admin commands, but the key is a *personal* token and the
command only reads.
"""

import textwrap

import httpx
import pytest
import respx

from wgman.cli import main

BASE = "https://wg.example.com:8888"
USER_API = f"{BASE}/@warpgate/api"

BASE2 = "https://wg2.example.com:8888"
USER_API2 = f"{BASE2}/@warpgate/api"

CONFIG = textwrap.dedent(
    """
    servers:
      - name: prod
        url: https://wg.example.com:8888
        api-key: ${WG_TOKEN}
    """
)

CONFIG_TWO_SERVERS = textwrap.dedent(
    """
    servers:
      - name: prod
        url: https://wg.example.com:8888
        api-key: ${WG_TOKEN}
      - name: staging
        url: https://wg2.example.com:8888
        api-key: ${WG_TOKEN}
    """
)

INFO = {
    "username": "alice",
    "external_host": "wg.example.com",
    "external_hosts": {"ssh": "ssh.wg.example.com"},
    "ports": {"ssh": 2222},
}

TARGETS = [
    {"id": "1", "name": "web-01", "kind": "Ssh"},
    {"id": "2", "name": "db-01", "kind": "Ssh"},
    {"id": "3", "name": "intranet", "kind": "Http"},
]


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WG_TOKEN", "personal-token")
    p = tmp_path / "wgman.yaml"
    p.write_text(CONFIG)
    return p


@pytest.fixture
def two_server_config(tmp_path, monkeypatch):
    monkeypatch.setenv("WG_TOKEN", "personal-token")
    p = tmp_path / "wgman.yaml"
    p.write_text(CONFIG_TWO_SERVERS)
    return p


def _mock_user_api(info=None, targets=None, api=USER_API):
    respx.get(f"{api}/info").mock(
        return_value=httpx.Response(200, json=info if info is not None else INFO)
    )
    respx.get(f"{api}/targets").mock(
        return_value=httpx.Response(
            200, json=targets if targets is not None else TARGETS
        )
    )


# --------------------------------------------------------------------------- #
# nominal
# --------------------------------------------------------------------------- #
@respx.mock
def test_prints_stanzas_for_reachable_ssh_targets(config_file, capsys):
    _mock_user_api()
    rc = main(["--config", str(config_file), "ssh-config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Host web-01" in out
    assert "Host db-01" in out


@respx.mock
def test_connects_through_the_bastion(config_file, capsys):
    _mock_user_api()
    main(["--config", str(config_file), "ssh-config"])
    out = capsys.readouterr().out
    assert "HostName ssh.wg.example.com" in out
    assert "Port     2222" in out
    assert "User     alice:web-01" in out


@respx.mock
def test_non_ssh_targets_are_omitted(config_file, capsys):
    _mock_user_api()
    main(["--config", str(config_file), "ssh-config"])
    assert "intranet" not in capsys.readouterr().out


@respx.mock
def test_never_writes_to_the_server(config_file):
    _mock_user_api()
    # respx raises on any unmocked call, so a write attempt fails the test.
    assert main(["--config", str(config_file), "ssh-config"]) == 0


@respx.mock
def test_does_not_touch_the_admin_api(config_file):
    # An end user's token has no admin rights; calling the admin API would
    # 403. Only the two user endpoints are mocked.
    _mock_user_api()
    assert main(["--config", str(config_file), "ssh-config"]) == 0


@respx.mock
def test_prefix_option(config_file, capsys):
    _mock_user_api()
    main(["--config", str(config_file), "ssh-config", "--prefix", "wg-"])
    out = capsys.readouterr().out
    assert "Host wg-web-01" in out


@respx.mock
def test_output_file(config_file, tmp_path, capsys):
    _mock_user_api()
    dest = tmp_path / "wg.sshconfig"
    rc = main(
        ["--config", str(config_file), "ssh-config", "--output", str(dest)]
    )
    assert rc == 0
    assert "Host web-01" in dest.read_text()
    # Nothing on stdout, so the file is the single source of the config.
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# multi-server
# --------------------------------------------------------------------------- #
@respx.mock
def test_multiple_servers_are_prefixed_by_server_name(
    two_server_config, capsys
):
    # Target names are unique per server, not across servers: without a
    # prefix the aliases would collide and ssh would silently use the first.
    _mock_user_api()
    _mock_user_api(api=USER_API2)
    rc = main(["--config", str(two_server_config), "ssh-config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Host prod-web-01" in out
    assert "Host staging-web-01" in out


@respx.mock
def test_server_option_narrows_and_drops_prefix(two_server_config, capsys):
    _mock_user_api()
    rc = main(
        ["--config", str(two_server_config), "--server", "prod", "ssh-config"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Host web-01" in out
    assert "staging" not in out


# --------------------------------------------------------------------------- #
# refusals and edge cases
# --------------------------------------------------------------------------- #
@respx.mock
def test_global_admin_token_is_refused(config_file, capsys):
    # Warpgate answers username=null for the global admin token and returns
    # an empty target list. Emitting an empty config would look like "you
    # can reach nothing"; say what is actually wrong instead.
    _mock_user_api(info=dict(INFO, username=None), targets=[])
    rc = main(["--config", str(config_file), "ssh-config"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no authenticated user" in err
    assert "personal API token" in err


@respx.mock
def test_rejected_token_reports_authentication_failure(config_file, capsys):
    respx.get(f"{USER_API}/info").mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    rc = main(["--config", str(config_file), "ssh-config"])
    assert rc == 1
    assert "authentication failed" in capsys.readouterr().err


@respx.mock
def test_old_server_without_user_api_is_reported(config_file, capsys):
    respx.get(f"{USER_API}/info").mock(return_value=httpx.Response(404))
    rc = main(["--config", str(config_file), "ssh-config"])
    assert rc == 1
    assert "user API" in capsys.readouterr().err


@respx.mock
def test_no_reachable_target_warns_but_succeeds(config_file, capsys):
    _mock_user_api(targets=[])
    rc = main(["--config", str(config_file), "ssh-config"])
    assert rc == 0
    assert "no ssh target reachable" in capsys.readouterr().err


@respx.mock
def test_unknown_server_returns_usage_error(config_file):
    rc = main(
        ["--config", str(config_file), "--server", "ghost", "ssh-config"]
    )
    assert rc == 2
