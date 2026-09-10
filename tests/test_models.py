"""Tests for the data models and their Warpgate API mapping."""

import pytest

from wgman.exceptions import ConfigError
from wgman.models import Role, ServerConfig, Target, TargetGroup, User


class TestRole:
    def test_minimal(self):
        role = Role.from_dict({"name": "admin"})
        assert role.name == "admin"
        assert role.default is False
        assert role.to_api_body() == {
            "name": "admin",
            "description": "",
            "is_default": False,
        }

    def test_default_flag_maps_to_is_default(self):
        role = Role.from_dict({"name": "dev", "default": True})
        assert role.to_api_body()["is_default"] is True

    def test_missing_name_raises(self):
        with pytest.raises(ConfigError, match="missing required key 'name'"):
            Role.from_dict({})


class TestUser:
    def test_roles_collected(self):
        user = User.from_dict({"name": "alice", "roles": ["admin", "dba"]})
        assert user.roles == ["admin", "dba"]

    def test_api_body_has_username_not_roles(self):
        # Role assignment is a separate endpoint, not part of the user body.
        user = User.from_dict({"name": "bob", "roles": ["x"]})
        body = user.to_api_body()
        assert body == {"username": "bob", "description": ""}


class TestTargetSSH:
    def test_shorthand_auth(self):
        # Warpgate wire format uses PascalCase discriminators (verified
        # against the live 0.25.4 OpenAPI spec).
        t = Target.from_dict(
            {"name": "box", "kind": "ssh", "host": "10.0.0.1",
             "username": "root", "auth": "publickey"}
        )
        opts = t.options()
        assert opts == {
            "kind": "Ssh",
            "host": "10.0.0.1",
            "port": 22,
            "username": "root",
            "auth": {"kind": "PublicKey"},
        }

    def test_default_port_and_username(self):
        t = Target.from_dict({"name": "b", "kind": "ssh", "host": "h"})
        opts = t.options()
        assert opts["port"] == 22
        assert opts["username"] == "root"
        assert opts["auth"] == {"kind": "PublicKey"}

    def test_password_auth_long_form(self):
        t = Target.from_dict(
            {"name": "b", "kind": "ssh", "host": "h",
             "auth": {"kind": "password", "password": "s3cr3t"}}
        )
        assert t.options()["auth"] == {"kind": "Password", "password": "s3cr3t"}

    def test_password_auth_without_password_raises(self):
        with pytest.raises(ConfigError, match="password auth requires"):
            Target.from_dict(
                {"name": "b", "kind": "ssh", "host": "h",
                 "auth": {"kind": "password"}}
            )

    def test_invalid_auth_kind_raises(self):
        with pytest.raises(ConfigError, match="invalid auth kind"):
            Target.from_dict(
                {"name": "b", "kind": "ssh", "host": "h", "auth": "nope"}
            )

    def test_missing_host_raises(self):
        with pytest.raises(ConfigError, match="requires 'host'"):
            Target.from_dict({"name": "b", "kind": "ssh"})

    def test_flattened_body_has_options(self):
        t = Target.from_dict({"name": "b", "kind": "ssh", "host": "h"})
        body = t.to_api_body(group_id="g-123")
        assert body["name"] == "b"
        assert body["options"]["kind"] == "Ssh"
        assert body["group_id"] == "g-123"


class TestTargetHTTP:
    def test_http_requires_url(self):
        with pytest.raises(ConfigError, match="http target requires 'url'"):
            Target.from_dict({"name": "web", "kind": "http"})

    def test_http_external_host_dash_key(self):
        t = Target.from_dict(
            {"name": "web", "kind": "http", "url": "http://x:8080",
             "external-host": "app.example.com"}
        )
        opts = t.options()
        assert opts == {
            "kind": "Http",
            "url": "http://x:8080",
            "tls": {"mode": "Preferred", "verify": False},
            "external_host": "app.example.com",
        }


class TestTargetDB:
    def test_mysql_default_port(self):
        t = Target.from_dict(
            {"name": "db", "kind": "mysql", "host": "h",
             "auth": {"kind": "password", "password": "p"}}
        )
        assert t.options()["port"] == 3306

    def test_postgres_default_port(self):
        t = Target.from_dict(
            {"name": "db", "kind": "postgres", "host": "h",
             "auth": {"kind": "password", "password": "p"}}
        )
        assert t.options()["port"] == 5432

    def test_invalid_kind_raises(self):
        with pytest.raises(ConfigError, match="invalid kind"):
            Target.from_dict({"name": "x", "kind": "ftp", "host": "h"})


class TestTargetGroup:
    def test_nested_targets_inherit_group(self):
        g = TargetGroup.from_dict(
            {"name": "dbs", "color": "info",
             "targets": [{"name": "pg", "kind": "ssh", "host": "h"}]}
        )
        assert g.targets[0].group == "dbs"
        # Color is emitted in PascalCase wire format (BootstrapThemeColor).
        assert g.to_api_body() == {
            "name": "dbs", "description": "", "color": "Info"
        }

    def test_invalid_color_raises(self):
        with pytest.raises(ConfigError, match="invalid color"):
            TargetGroup.from_dict({"name": "g", "color": "rainbow"})


class TestServerConfig:
    def test_dash_keys(self):
        s = ServerConfig.from_dict(
            {"name": "prod", "url": "https://x", "api-key": "tok",
             "verify-tls": False}
        )
        assert s.api_key == "tok"
        assert s.verify_tls is False

    def test_optional_api_key_parses(self):
        """No 'api-key' is legal: an end user holds no Warpgate token.

        The key is required to *authenticate*, not to describe a server —
        see ``require_api_key``.
        """
        s = ServerConfig.from_dict(
            {"name": "p", "url": "https://x", "ssh-host": "bastion"}
        )
        assert s.api_key is None
        assert s.ssh_host == "bastion"

    def test_require_api_key_returns_the_token(self):
        s = ServerConfig.from_dict(
            {"name": "p", "url": "https://x", "api-key": "tok"}
        )
        assert s.require_api_key() == "tok"

    def test_require_api_key_raises_when_absent(self):
        s = ServerConfig.from_dict({"name": "p", "url": "https://x"})
        with pytest.raises(ConfigError, match="server 'p': no 'api-key'"):
            s.require_api_key()

    def test_no_call_site_reads_api_key_directly(self):
        """``api_key`` may be None, so every consumer must go through
        ``require_api_key()``. A direct read would send an empty token and
        get an opaque 401 instead of a message naming the server.

        Structural, because a new call site added later would not fail any
        behavioural test — it would simply reintroduce the bad error.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "src" / "wgman"
        offenders = []
        for path in src.rglob("*.py"):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if ".api_key" not in line or "require_api_key" in line:
                    continue
                # models.py defines the field and the accessor; client.py
                # holds the token it was constructed with.
                if path.name in ("models.py", "client.py"):
                    continue
                offenders.append(f"{path.name}:{n}: {line.strip()}")

        assert not offenders, (
            "these read ServerConfig.api_key directly; use "
            "require_api_key() so a missing token is reported with its "
            "server name:\n" + "\n".join(offenders)
        )
