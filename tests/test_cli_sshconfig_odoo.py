"""End-to-end CLI tests for ``ssh-config --from-odoo``.

This is the credential-free path: the user authenticates to Odoo, which
supplies both their identity and the target list, and the bastion address
comes from the command line or the config. Warpgate is never contacted —
the tests assert that too, since needing no bastion token is the whole
point of this mode.

The Odoo transport is injected (as in ``test_odoo.py``); no oerpc, no
network.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest

from wgman import cli
from wgman.cli import main

BASTION = "ssh.wg.example.com"

CONFIG = textwrap.dedent(
    """
    servers:
      - name: prod
        url: https://wg.example.com:8888
        api-key: ${WG_TOKEN}

    odoo:
      url: https://odoo.example.com
      db: mydb
      user: alice@example.com
      password: s3cret
    """
)

CONFIG_WITH_BASTION = textwrap.dedent(
    """
    servers:
      - name: prod
        url: https://wg.example.com:8888
        api-key: ${WG_TOKEN}
        ssh-host: ssh.wg.example.com
        ssh-port: 2222

    odoo:
      url: https://odoo.example.com
      db: mydb
      user: alice@example.com
      password: s3cret
    """
)

CONFIG_WITH_ROLES = textwrap.dedent(
    """
    servers:
      - name: prod
        url: https://wg.example.com:8888
        api-key: ${WG_TOKEN}
        ssh-host: ssh.wg.example.com

    odoo:
      url: https://odoo.example.com
      db: mydb
      user: alice@example.com
      password: s3cret
      targets:
        domain: [["ssh_target", "!=", false]]
        roles: [sysadmin]
      users:
        - domain: [["login", "=", "alice@example.com"]]
          roles: [sysadmin]
        - domain: [["login", "=", "bob@example.com"]]
          roles: [developers]
    """
)

EQUIPMENT = [
    {"id": 1, "name": "jev-prod", "ssh_target": "gestion.jardinenvie.com"},
    {"id": 2, "name": "ceres-prod", "ssh_target": "root@ceres.swiss:22"},
]


def fake_query(equipment=None, users=None, keys=None, logins=None):
    """An injected Odoo transport answering by model.

    *logins* answers the whoami lookup (``res.users`` filtered on the
    configured login); *users* answers the selection queries.
    """
    equipment = EQUIPMENT if equipment is None else equipment

    def query(model, domain, fields):
        if model == "maintenance.equipment":
            return list(equipment)
        if model == "res.users":
            # The whoami lookup filters on ["login", "=", ...]; selections
            # use whatever the config declared.
            flat = [d for d in domain if isinstance(d, list)]
            if any(d[:2] == ["login", "="] for d in flat) and logins is not None:
                wanted = next(d[2] for d in flat if d[:2] == ["login", "="])
                return [u for u in logins if u["login"] == wanted]
            return list(users or [])
        if model == "ssh.key":
            return list(keys or [])
        return []

    return query


@pytest.fixture
def odoo_stub(monkeypatch):
    """Route the CLI's Odoo access through an injected query."""

    def install(query):
        monkeypatch.setattr(cli.odoo, "make_query", lambda config: query)
        monkeypatch.setattr(
            cli.odoo,
            "_make_default_query",
            lambda config: query,
            raising=False,
        )

    return install


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WG_TOKEN", "unused-by-this-path")

    def write(text):
        p = tmp_path / "wgman.yaml"
        p.write_text(text)
        return p

    return write


class TestRendersFromOdoo:
    def test_emits_a_stanza_per_odoo_target(
        self, config_file, odoo_stub, capsys
    ):
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))
        path = config_file(CONFIG)

        rc = main(
            ["-c", str(path), "ssh-config", "--from-odoo",
             "--bastion", f"{BASTION}:2222"]
        )
        out = capsys.readouterr().out

        assert rc == 0
        assert "Host jev-prod" in out
        assert "Host ceres-prod" in out

    def test_user_is_the_odoo_login_selector(
        self, config_file, odoo_stub, capsys
    ):
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))
        path = config_file(CONFIG)

        main(["-c", str(path), "ssh-config", "--from-odoo",
              "--bastion", BASTION])
        out = capsys.readouterr().out

        assert "User     alice@example.com:jev-prod" in out

    def test_points_at_the_bastion_never_the_backend(
        self, config_file, odoo_stub, capsys
    ):
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))
        path = config_file(CONFIG)

        main(["-c", str(path), "ssh-config", "--from-odoo",
              "--bastion", f"{BASTION}:2222"])
        out = capsys.readouterr().out

        assert f"HostName {BASTION}" in out
        # The Odoo ssh_target is the BACKEND address: it must never leak
        # into a client config, or the client would bypass the bastion.
        assert "gestion.jardinenvie.com" not in out
        assert "ceres.swiss" not in out
        # Nor may the backend account appear as the ssh user.
        assert "User     root" not in out

    def test_contacts_no_warpgate_server(self, config_file, odoo_stub, capsys):
        # respx is not installed here on purpose: any real HTTP call would
        # fail. Assert positively that the client is never constructed.
        called = []

        class Boom:
            def __init__(self, *a, **kw):
                called.append(a)
                raise AssertionError("Warpgate must not be contacted")

        import wgman.cli as cli_mod

        original = cli_mod.WarpgateUserClient
        cli_mod.WarpgateUserClient = Boom
        try:
            odoo_stub(
                fake_query(logins=[{"id": 7, "login": "alice@example.com"}])
            )
            path = config_file(CONFIG)
            rc = main(["-c", str(path), "ssh-config", "--from-odoo",
                       "--bastion", BASTION])
        finally:
            cli_mod.WarpgateUserClient = original

        assert rc == 0
        assert called == []


class TestBastionAddressResolution:
    def test_config_supplies_it_when_no_flag(
        self, config_file, odoo_stub, capsys
    ):
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))
        path = config_file(CONFIG_WITH_BASTION)

        rc = main(["-c", str(path), "ssh-config", "--from-odoo"])
        out = capsys.readouterr().out

        assert rc == 0
        assert f"HostName {BASTION}" in out
        assert "Port     2222" in out

    def test_flag_overrides_the_config(self, config_file, odoo_stub, capsys):
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))
        path = config_file(CONFIG_WITH_BASTION)

        main(["-c", str(path), "ssh-config", "--from-odoo",
              "--bastion", "other.example.com:2022"])
        out = capsys.readouterr().out

        assert "HostName other.example.com" in out
        assert "Port     2022" in out

    def test_missing_address_is_a_usage_error_naming_both_remedies(
        self, config_file, odoo_stub, capsys
    ):
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))
        path = config_file(CONFIG)

        rc = main(["-c", str(path), "ssh-config", "--from-odoo"])
        err = capsys.readouterr().err

        assert rc == 2
        assert "--bastion" in err and "ssh-host" in err

    def test_default_port_when_config_names_only_the_host(
        self, config_file, odoo_stub, capsys
    ):
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))
        path = config_file(CONFIG_WITH_ROLES)  # ssh-host, no ssh-port

        main(["-c", str(path), "ssh-config", "--from-odoo"])
        out = capsys.readouterr().out

        assert "Port     2222" in out


class TestRoleFiltering:
    def test_user_sees_only_targets_their_role_grants(
        self, config_file, odoo_stub, capsys
    ):
        # alice is sysadmin (as are the targets); bob is developers only.
        odoo_stub(
            fake_query(
                users=[{"id": 7, "login": "alice@example.com"}],
                logins=[{"id": 7, "login": "alice@example.com"}],
            )
        )
        path = config_file(CONFIG_WITH_ROLES)

        rc = main(["-c", str(path), "ssh-config", "--from-odoo"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "Host jev-prod" in out

    def test_user_without_the_role_gets_a_warning_and_no_stanza(
        self, config_file, odoo_stub, capsys
    ):
        bob = textwrap.dedent(
            """
            servers:
              - name: prod
                url: https://wg.example.com:8888
                api-key: ${WG_TOKEN}
                ssh-host: ssh.wg.example.com

            odoo:
              url: https://odoo.example.com
              db: mydb
              user: bob@example.com
              password: s3cret
              targets:
                roles: [sysadmin]
              users:
                - domain: [["login", "=", "bob@example.com"]]
                  roles: [developers]
            """
        )
        odoo_stub(
            fake_query(
                users=[{"id": 8, "login": "bob@example.com"}],
                logins=[{"id": 8, "login": "bob@example.com"}],
            )
        )
        path = config_file(bob)

        rc = main(["-c", str(path), "ssh-config", "--from-odoo"])
        captured = capsys.readouterr()

        assert rc == 0
        assert "no ssh target reachable by bob@example.com" in captured.err
        assert "Host " not in captured.out


class TestNeedsNoWarpgateCredential:
    """The premise of this mode: the user holds no bastion token. The
    config's ``api-key: ${WG_TOKEN}`` must therefore not be required to
    resolve — an unset variable it never reads cannot be a blocker."""

    def test_runs_with_the_warpgate_token_unset(
        self, tmp_path, monkeypatch, odoo_stub, capsys
    ):
        monkeypatch.delenv("WG_TOKEN", raising=False)
        path = tmp_path / "wgman.yaml"
        path.write_text(CONFIG_WITH_BASTION)
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))

        rc = main(["-c", str(path), "ssh-config", "--from-odoo"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "Host jev-prod" in out

    def test_runs_with_no_config_file_at_all(
        self, tmp_path, monkeypatch, odoo_stub, capsys
    ):
        # Nothing to be found in any default location: the Odoo source is
        # fully defined on the command line, and the password is typed at
        # the prompt (the documented contract — there is deliberately no
        # --odoo-password flag).
        from pathlib import Path

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "wgman.config.DEFAULT_CONFIG_PATHS",
            (Path(tmp_path) / "absent.yaml",),
        )
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "s3cret")
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))

        # --odoo-* are global flags: they precede the subcommand.
        rc = main([
            "--odoo-url", "https://odoo.example.com",
            "--odoo-db", "mydb",
            "--odoo-user", "alice@example.com",
            "ssh-config", "--from-odoo",
            "--bastion", f"{BASTION}:2222",
        ])
        out = capsys.readouterr().out

        assert rc == 0
        assert "Host jev-prod" in out
        assert f"HostName {BASTION}" in out


class TestWhoamiFailures:
    def test_unknown_login_is_reported_not_silently_empty(
        self, config_file, odoo_stub, capsys
    ):
        odoo_stub(fake_query(logins=[]))  # no res.users match
        path = config_file(CONFIG)

        rc = main(["-c", str(path), "ssh-config", "--from-odoo",
                   "--bastion", BASTION])
        err = capsys.readouterr().err

        assert rc == 1
        assert "alice@example.com" in err


class TestOutputFile:
    def test_writes_to_the_named_file(
        self, tmp_path, config_file, odoo_stub, capsys
    ):
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))
        path = config_file(CONFIG)
        out_file = tmp_path / "warpgate.sshconfig"

        rc = main(["-c", str(path), "ssh-config", "--from-odoo",
                   "--bastion", BASTION, "-o", str(out_file)])

        assert rc == 0
        assert "Host jev-prod" in out_file.read_text()


# --------------------------------------------------------------------------- #
# The real ssh binary must accept the generated file. A single malformed
# line invalidates the WHOLE config, so string assertions are insufficient.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh not available")
class TestRealSshAcceptsOdooOutput:
    def test_ssh_resolves_an_odoo_sourced_alias_to_the_bastion(
        self, tmp_path, config_file, odoo_stub
    ):
        odoo_stub(fake_query(logins=[{"id": 7, "login": "alice@example.com"}]))
        path = config_file(CONFIG)
        out_file = tmp_path / "sshconfig"

        main(["-c", str(path), "ssh-config", "--from-odoo",
              "--bastion", f"{BASTION}:2222", "-o", str(out_file)])

        proc = subprocess.run(
            ["ssh", "-F", str(out_file), "-G", "jev-prod"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            f"ssh rejected the generated config: {proc.stderr.strip()}"
        )
        settings = dict(
            line.split(" ", 1)
            for line in proc.stdout.splitlines()
            if " " in line
        )
        assert settings["hostname"] == BASTION
        assert settings["port"] == "2222"
        assert settings["user"] == "alice@example.com:jev-prod"
