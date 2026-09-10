"""Tests for the Odoo desired-state source (wgman.odoo)."""

import logging
import textwrap

import pytest

from wgman import odoo
from wgman.cli import main
from wgman.config import Config, load_config
from wgman.exceptions import ConfigError
from wgman.models import Target
from wgman.odoo import (
    OdooConfig,
    OdooError,
    OdooTargetsConfig,
    OdooUserSelection,
    equipment_to_target,
    fetch_state,
    normalize_domain,
    parse_ssh_target,
    record_to_user,
)


def make_query(records_by_model):
    """Build an injectable query callable from {model: records}.

    Records the calls in ``query.calls`` as (model, domain, fields).
    """
    def query(model, domain, fields):
        query.calls.append((model, domain, fields))
        return records_by_model.get(model, [])

    query.calls = []
    return query


# --------------------------------------------------------------------------- #
# parse_ssh_target
# --------------------------------------------------------------------------- #
class TestParseSshTarget:
    def test_host_only(self):
        assert parse_ssh_target("host.example.com") == (
            "root", "host.example.com", 22,
        )

    def test_host_and_port(self):
        assert parse_ssh_target("host.example.com:2222") == (
            "root", "host.example.com", 2222,
        )

    def test_user_and_host(self):
        assert parse_ssh_target("admin@host.example.com") == (
            "admin", "host.example.com", 22,
        )

    def test_full_form(self):
        assert parse_ssh_target("admin@10.1.2.3:2222") == (
            "admin", "10.1.2.3", 2222,
        )

    def test_ip_host(self):
        assert parse_ssh_target("192.168.1.1") == ("root", "192.168.1.1", 22)

    def test_surrounding_whitespace(self):
        assert parse_ssh_target("  host.example.com  ") == (
            "root", "host.example.com", 22,
        )

    @pytest.mark.parametrize(
        "value",
        ["", "user@", ":22", "user@:22", "host:", "host:abc",
         "a b", "user@host@host"],
    )
    def test_malformed(self, value):
        with pytest.raises(ConfigError):
            parse_ssh_target(value)

    @pytest.mark.parametrize("value", ["host:0", "host:65536", "host:99999"])
    def test_port_out_of_range(self, value):
        with pytest.raises(ConfigError):
            parse_ssh_target(value)

    def test_where_in_error_message(self):
        with pytest.raises(ConfigError, match="equipment 'srv-01'"):
            parse_ssh_target("bad value", where="odoo: equipment 'srv-01'")


# --------------------------------------------------------------------------- #
# equipment_to_target
# --------------------------------------------------------------------------- #
class TestEquipmentToTarget:
    def test_nominal(self):
        target = equipment_to_target(
            {"id": 1, "name": "srv-01", "ssh_target": "admin@srv1.example.com:2222"}
        )
        assert target.name == "srv-01"
        assert target.kind == "ssh"
        assert target.host == "srv1.example.com"
        assert target.port == 2222
        assert target.username == "admin"
        assert target.auth == {"kind": "publickey"}
        assert target.roles == []

    def test_defaults(self):
        target = equipment_to_target(
            {"id": 2, "name": "srv-02", "ssh_target": "srv2.example.com"}
        )
        assert target.username == "root"
        assert target.port == 22

    def test_missing_name(self):
        with pytest.raises(ConfigError, match="has no name"):
            equipment_to_target({"id": 3, "ssh_target": "h.example.com"})

    def test_false_ssh_target(self):
        # Odoo returns False for unset char fields.
        with pytest.raises(ConfigError, match="no usable 'ssh_target'"):
            equipment_to_target({"id": 4, "name": "srv-04", "ssh_target": False})

    def test_error_names_equipment(self):
        with pytest.raises(ConfigError, match="equipment 'srv-05'"):
            equipment_to_target(
                {"id": 5, "name": "srv-05", "ssh_target": "not valid !"}
            )


# --------------------------------------------------------------------------- #
# fetch_state (with injected query)
# --------------------------------------------------------------------------- #
ODOO_CONFIG = OdooConfig(
    url="https://odoo.example.com", db="testdb", user="sync", password="pw"
)

RECORDS = [
    {"id": 1, "name": "srv-01", "ssh_target": "srv1.example.com"},
    {"id": 2, "name": "srv-02", "ssh_target": "admin@srv2.example.com:2222"},
]


class TestFetchState:
    def test_maps_all_records(self):
        query = make_query({"maintenance.equipment": RECORDS})
        state = fetch_state(ODOO_CONFIG, query=query)
        assert [t.name for t in state.targets] == ["srv-01", "srv-02"]
        assert all(t.kind == "ssh" for t in state.targets)
        assert state.users == []
        assert state.roles == []

    def test_default_targets_domain(self):
        query = make_query({})
        fetch_state(ODOO_CONFIG, query=query)
        assert query.calls == [
            ("maintenance.equipment", [["ssh_target", "!=", False]],
             ["name", "ssh_target"]),
        ]

    def test_custom_targets_domain_passthrough(self):
        domain = [["ssh_target", "!=", False], ["category_id.name", "=", "vps"]]
        config = OdooConfig(
            url="u", db="d", user="l",
            targets=OdooTargetsConfig(domain=domain, roles=["ssh-dev"]),
        )
        query = make_query({"maintenance.equipment": RECORDS})
        state = fetch_state(config, query=query)
        assert query.calls[0][1] == domain
        # roles applied to every target + auto-declared
        assert all(t.roles == ["ssh-dev"] for t in state.targets)
        assert [r.name for r in state.roles] == ["ssh-dev"]

    def test_users_selection(self):
        config = OdooConfig(
            url="u", db="d", user="l",
            users=[
                OdooUserSelection(
                    domain=[["employee_id.job_id.name", "=", "Dev"]],
                    roles=["ssh-dev"],
                ),
            ],
        )
        query = make_query({
            "maintenance.equipment": [],
            "res.users": [
                {"id": 6, "login": "alice@example.com"},
                {"id": 7, "login": "bob@example.com"},
            ],
        })
        state = fetch_state(config, query=query)
        assert [(u.name, u.roles) for u in state.users] == [
            ("alice@example.com", ["ssh-dev"]),
            ("bob@example.com", ["ssh-dev"]),
        ]
        assert ("res.users", [["employee_id.job_id.name", "=", "Dev"]],
                ["login"]) in query.calls
        assert [r.name for r in state.roles] == ["ssh-dev"]

    def test_user_in_multiple_selections_accumulates_roles(self):
        config = OdooConfig(
            url="u", db="d", user="l",
            users=[
                OdooUserSelection(domain=[["a", "=", 1]], roles=["r1"]),
                OdooUserSelection(domain=[["b", "=", 2]], roles=["r2", "r1"]),
            ],
        )
        query = make_query({
            "res.users": [{"id": 6, "login": "alice@example.com"}],
        })
        state = fetch_state(config, query=query)
        (user,) = state.users
        assert user.roles == ["r1", "r2"]
        assert [r.name for r in state.roles] == ["r1", "r2"]

    def test_user_without_login_raises(self):
        config = OdooConfig(
            url="u", db="d", user="l",
            users=[OdooUserSelection(domain=[["a", "=", 1]])],
        )
        query = make_query({"res.users": [{"id": 9, "login": False}]})
        with pytest.raises(ConfigError, match="no usable login"):
            fetch_state(config, query=query)


class TestNormalizeDomain:
    def test_triplets_and_operators(self):
        domain = ["|", ["a", "=", 1], ["b", "in", [1, 2]]]
        assert normalize_domain(domain, "t") == domain

    def test_not_a_list(self):
        with pytest.raises(ConfigError, match="must be a list"):
            normalize_domain("nope", "t")

    def test_bad_operator(self):
        with pytest.raises(ConfigError, match="invalid operator"):
            normalize_domain(["OR", ["a", "=", 1]], "t")

    def test_bad_triplet(self):
        with pytest.raises(ConfigError, match="expected .field, operator"):
            normalize_domain([["a", "="]], "t")


class TestRecordToUser:
    def test_nominal(self):
        user = record_to_user({"id": 3, "login": "x@y.z"}, ["r"])
        assert (user.name, user.roles) == ("x@y.z", ["r"])


class TestLoginErrorMessage:
    """A rejected Odoo login must say so in one actionable line.

    Odoo answers a bad password with an HTTP 200 carrying a full
    server-side Python traceback (~33 lines) ending in ``AccessDenied``.
    Relaying that verbatim buries the cause, and leaks the server's
    filesystem layout and addon list into the terminal. The raw text
    stays reachable on the exception chain for debugging.
    """

    ACCESS_DENIED = (
        "(200) Odoo Server Error (path='/web/session/authenticate'):\n"
        "  | Traceback (most recent call last):\n"
        "  |   File \"/opt/odoo/custom/src/odoo/odoo/http.py\", line 2215\n"
        "  |     return service_model.retrying(func, env=self.env)\n"
        "  |   File \"/opt/odoo/auto/addons/website/models/res_users.py\"\n"
        "  |     raise AccessDenied()\n"
        "  | odoo.exceptions.AccessDenied: Access Denied"
    )

    def _login_failure(self, monkeypatch, exc):
        """Drive ``_make_default_query`` with a login that raises ``exc``."""
        import wgman.odoo as odoo_mod

        class FakeSession:
            def login(self, db, user, password):
                raise exc

        class FakeOdoo:
            def __init__(self, url, verify=True):
                self.session = FakeSession()

        monkeypatch.setattr(
            odoo_mod, "_import_oerpc",
            lambda: (FakeOdoo, type(exc), RuntimeError),
        )
        cfg = OdooConfig(
            url="https://odoo.example.coop", db="odoo18", user="me@example.coop"
        )
        with pytest.raises(OdooError) as excinfo:
            odoo_mod._make_default_query(cfg)
        return excinfo.value

    def test_access_denied_is_one_line(self, monkeypatch):
        err = self._login_failure(
            monkeypatch, ApiErrorStub(self.ACCESS_DENIED)
        )
        assert "\n" not in str(err)

    def test_access_denied_names_cause_and_remedy(self, monkeypatch):
        err = self._login_failure(
            monkeypatch, ApiErrorStub(self.ACCESS_DENIED)
        )
        message = str(err)
        assert "password" in message
        assert "me@example.coop" in message and "odoo18" in message

    def test_access_denied_does_not_leak_server_internals(self, monkeypatch):
        err = self._login_failure(
            monkeypatch, ApiErrorStub(self.ACCESS_DENIED)
        )
        message = str(err)
        assert "Traceback" not in message
        assert "/opt/odoo" not in message

    def test_raw_detail_survives_on_the_exception_chain(self, monkeypatch):
        err = self._login_failure(
            monkeypatch, ApiErrorStub(self.ACCESS_DENIED)
        )
        assert "AccessDenied" in str(err.__cause__)

    def test_other_failures_keep_their_detail(self, monkeypatch):
        """Only access-denied is summarised; anything else stays verbatim.

        A network or protocol failure carries no traceback to hide, and
        its detail is what makes it diagnosable.
        """
        err = self._login_failure(
            monkeypatch, ApiErrorStub("could not connect to host")
        )
        assert "could not connect to host" in str(err)


class ApiErrorStub(Exception):
    """Stands in for ``oerpc.api.common.ApiError`` (a bare Exception)."""


class TestOerpcNoiseSuppressed:
    """oerpc's Python-2-only xmlrpc modules must not spam stderr.

    ``oerpc.api.api_modules()`` imports every API module on first session
    use and logs an ERROR for the two that need Python 2's ``cStringIO``.
    They are irrelevant to the JSON-RPC transport we use, but with no
    logging configured Python's last-resort handler prints them to stderr
    ahead of our own output.
    """

    def test_legacy_xmlrpc_scan_stays_off_stderr(self, capsys):
        api = pytest.importorskip("oerpc.api.common")
        # Strip the root handlers pytest installs: otherwise the record is
        # consumed there and the last-resort handler (the one the fix must
        # bypass) never runs, making the test pass for the wrong reason.
        root = logging.getLogger()
        saved = root.handlers[:]
        root.handlers.clear()
        api.api_modules.cache_clear()
        try:
            try:
                api.Odoo("http://localhost").session
            except Exception:
                pass  # no live server; we only care about the import noise
        finally:
            root.handlers[:] = saved
        assert "cStringIO" not in capsys.readouterr().err


class TestSshKeysFetch:
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc alice@laptop"

    def _config(self):
        return OdooConfig(
            url="u", db="d", user="l",
            users=[OdooUserSelection(domain=[["a", "=", 1]], roles=["r"])],
        )

    def test_keys_attached_to_users(self):
        query = make_query({
            "res.users": [{"id": 6, "login": "alice@example.com"}],
            "ssh.key": [
                {"id": 1, "user_id": [6, "Alice"], "key": self.KEY},
            ],
        })
        state = fetch_state(self._config(), query=query)
        (user,) = state.users
        assert user.public_keys == [self.KEY]
        # keys queried with a user_id domain
        assert ("ssh.key", [["user_id", "in", [6]]],
                ["user_id", "key"]) in query.calls

    def test_no_keys_query_without_users(self):
        config = OdooConfig(url="u", db="d", user="l")
        query = make_query({})
        fetch_state(config, query=query)
        assert not any(model == "ssh.key" for model, _, _ in query.calls)

    def test_user_without_keys_gets_empty_list(self):
        query = make_query({
            "res.users": [{"id": 6, "login": "alice@example.com"}],
            "ssh.key": [],
        })
        state = fetch_state(self._config(), query=query)
        assert state.users[0].public_keys == []

    def test_whitespace_normalised_and_dedup(self):
        messy = "ssh-ed25519   AAAAC3NzaC1lZDI1NTE5AAAAIabc   alice@laptop"
        query = make_query({
            "res.users": [{"id": 6, "login": "alice@example.com"}],
            "ssh.key": [
                {"id": 1, "user_id": [6, "A"], "key": messy},
                {"id": 2, "user_id": [6, "A"], "key": self.KEY},
            ],
        })
        state = fetch_state(self._config(), query=query)
        assert state.users[0].public_keys == [self.KEY]

    def test_invalid_key_raises(self):
        query = make_query({
            "res.users": [{"id": 6, "login": "alice@example.com"}],
            "ssh.key": [{"id": 1, "user_id": [6, "A"], "key": "not a key"}],
        })
        with pytest.raises(ConfigError, match="OpenSSH public key"):
            fetch_state(self._config(), query=query)

    def test_empty_key_skipped(self):
        query = make_query({
            "res.users": [{"id": 6, "login": "alice@example.com"}],
            "ssh.key": [{"id": 1, "user_id": [6, "A"], "key": False}],
        })
        state = fetch_state(self._config(), query=query)
        assert state.users[0].public_keys == []

    def test_backslash_wrapped_key_unwrapped(self):
        # Key pasted into Odoo with a hard line wrap (backslash + newline
        # splitting the base64 blob) — seen in prod (ssh-rsa, id=4).
        wrapped = (
            "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDPabc\\\n"
            "defGHIjkl+mn/opQRstu== stephan-mobile"
        )
        query = make_query({
            "res.users": [{"id": 6, "login": "s@example.com"}],
            "ssh.key": [{"id": 1, "user_id": [6, "S"], "key": wrapped}],
        })
        state = fetch_state(self._config(), query=query)
        (key,) = state.users[0].public_keys
        assert key == ("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDPabc"
                       "defGHIjkl+mn/opQRstu== stephan-mobile")

    def test_plain_newline_wrapped_key_unwrapped(self):
        wrapped = "ssh-ed25519 AAAAC3NzaC1lZDI1\nNTE5AAAAIabc alice@laptop"
        query = make_query({
            "res.users": [{"id": 6, "login": "a@example.com"}],
            "ssh.key": [{"id": 1, "user_id": [6, "A"], "key": wrapped}],
        })
        state = fetch_state(self._config(), query=query)
        (key,) = state.users[0].public_keys
        assert key == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc alice@laptop"


# --------------------------------------------------------------------------- #
# YAML parsing of odoo.targets / odoo.users sections
# --------------------------------------------------------------------------- #
class TestOdooSelectionSections:
    def test_full_sections(self, tmp_path):
        p = tmp_path / "wgman.yaml"
        p.write_text(textwrap.dedent(
            """
            servers: []
            odoo:
              url: https://odoo.example.com
              db: mydb
              user: sync@example.com
              targets:
                domain:
                  - [ssh_target, "!=", false]
                  - [category_id.name, "=", vps]
                roles: [ssh-dev]
              users:
                - domain:
                    - [active, "=", true]
                    - [employee_id.job_id.name, in, [Dev, DevOps]]
                  roles: [ssh-dev]
                - domain:
                    - "|"
                    - [login, "=", boss@example.com]
                    - [login, "=", cto@example.com]
                  roles: [admin]
            """
        ))
        cfg = load_config(p)
        odoo = cfg.odoo
        assert odoo.targets.domain == [
            ["ssh_target", "!=", False],
            ["category_id.name", "=", "vps"],
        ]
        assert odoo.targets.roles == ["ssh-dev"]
        assert len(odoo.users) == 2
        assert odoo.users[0].domain == [
            ["active", "=", True],
            ["employee_id.job_id.name", "in", ["Dev", "DevOps"]],
        ]
        assert odoo.users[0].roles == ["ssh-dev"]
        assert odoo.users[1].domain[0] == "|"
        assert odoo.users[1].roles == ["admin"]

    def test_defaults_without_sections(self, tmp_path):
        p = tmp_path / "wgman.yaml"
        p.write_text(textwrap.dedent(
            """
            servers: []
            odoo:
              url: https://odoo.example.com
              db: mydb
              user: sync@example.com
            """
        ))
        odoo = load_config(p).odoo
        assert odoo.targets.domain == [["ssh_target", "!=", False]]
        assert odoo.targets.roles == []
        assert odoo.users == []

    def test_users_selection_requires_domain(self, tmp_path):
        p = tmp_path / "wgman.yaml"
        p.write_text(textwrap.dedent(
            """
            servers: []
            odoo:
              url: u
              db: d
              user: l
              users:
                - roles: [ssh-dev]
            """
        ))
        with pytest.raises(ConfigError, match="missing required key 'domain'"):
            load_config(p)

    def test_unknown_key_rejected(self, tmp_path):
        p = tmp_path / "wgman.yaml"
        p.write_text(textwrap.dedent(
            """
            servers: []
            odoo:
              url: u
              db: d
              user: l
              targets:
                domains: [[a, "=", 1]]
            """
        ))
        with pytest.raises(ConfigError, match="unknown key"):
            load_config(p)


# --------------------------------------------------------------------------- #
# Config.merge_users / merge_roles
# --------------------------------------------------------------------------- #
class TestMergeUsersRoles:
    def test_merge_users(self):
        from wgman.models import User

        config = Config()
        config.merge_users([User(name="alice@example.com", roles=["r"])])
        assert [u.name for u in config.users] == ["alice@example.com"]

    def test_merge_users_collision(self):
        from wgman.models import User

        config = Config(users=[User(name="alice@example.com")])
        with pytest.raises(ConfigError, match="collides"):
            config.merge_users([User(name="alice@example.com")])

    def test_merge_roles_dedups_silently(self):
        from wgman.models import Role

        config = Config(roles=[Role(name="admin")])
        config.merge_roles([Role(name="admin"), Role(name="ssh-dev")])
        assert [r.name for r in config.roles] == ["admin", "ssh-dev"]


# --------------------------------------------------------------------------- #
# CLI fetch with users + roles
# --------------------------------------------------------------------------- #
class TestCliFetchFullState:
    CONFIG = textwrap.dedent(
        """
        servers: []
        odoo:
          url: https://odoo.example.com
          db: mydb
          user: sync@example.com
          password: pw
          targets:
            roles: [ssh-dev]
          users:
            - domain:
                - [employee_id.job_id.name, "=", Dev]
              roles: [ssh-dev]
        """
    )

    @pytest.fixture
    def full_stub(self, monkeypatch):
        def fake_make_query(cfg):
            def query(model, domain, fields):
                if model == "maintenance.equipment":
                    return RECORDS
                if model == "res.users":
                    return [{"id": 6, "login": "alice@example.com"}]
                return []
            return query

        monkeypatch.setattr("wgman.odoo._make_default_query", fake_make_query)

    def test_fetch_lists_targets_users_roles(
        self, tmp_path, full_stub, capsys
    ):
        import yaml as yaml_mod

        p = tmp_path / "wgman.yaml"
        p.write_text(self.CONFIG)
        rc = main(["-c", str(p), "fetch"])
        assert rc == 0
        data = yaml_mod.safe_load(capsys.readouterr().out)
        assert [t["name"] for t in data["targets"]] == ["srv-01", "srv-02"]
        assert all(t["roles"] == ["ssh-dev"] for t in data["targets"])
        assert data["roles"] == [{"name": "ssh-dev"}]
        assert data["users"] == [
            {"name": "alice@example.com", "roles": ["ssh-dev"]}
        ]

    def test_fetch_output_roundtrips_with_users(
        self, tmp_path, full_stub, capsys
    ):
        p = tmp_path / "wgman.yaml"
        p.write_text(self.CONFIG)
        main(["-c", str(p), "fetch"])
        out = capsys.readouterr().out
        state = tmp_path / "state.yaml"
        state.write_text("servers: []\n" + out)
        cfg = load_config(state)
        assert [u.name for u in cfg.users] == ["alice@example.com"]
        assert [r.name for r in cfg.roles] == ["ssh-dev"]
        # role references validate (roles present)
        assert all(t.roles == ["ssh-dev"] for t in cfg.root_targets)


# --------------------------------------------------------------------------- #
# OdooConfig parsing (config.py 'odoo:' section)
# --------------------------------------------------------------------------- #
class TestOdooConfigSection:
    def test_parsed_from_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ODOO_PW", "sekret")
        p = tmp_path / "wgman.yaml"
        p.write_text(textwrap.dedent(
            """
            servers:
              - name: prod
                url: https://wg.example.com:8888
                api-key: tok
            odoo:
              url: https://odoo.example.com
              db: mydb
              user: sync@example.com
              password: ${ODOO_PW}
            """
        ))
        config = load_config(p)
        assert config.odoo == OdooConfig(
            url="https://odoo.example.com",
            db="mydb",
            user="sync@example.com",
            password="sekret",
        )

    def test_absent_section(self, tmp_path):
        p = tmp_path / "wgman.yaml"
        p.write_text("servers:\n  - {name: p, url: u, api-key: k}\n")
        assert load_config(p).odoo is None

    def test_incomplete_section(self, tmp_path):
        p = tmp_path / "wgman.yaml"
        p.write_text(
            "servers: []\nodoo:\n  db: mydb\n  user: sync@example.com\n"
        )
        with pytest.raises(ConfigError, match="missing required key 'url'"):
            load_config(p)

    def test_db_is_optional(self, tmp_path):
        """Most servers host exactly one database, which they will name on
        request; asking the user to retype it only invites a typo whose
        failure mode is an opaque authentication error."""
        p = tmp_path / "wgman.yaml"
        p.write_text(textwrap.dedent(
            """
            servers: []
            odoo:
              url: https://odoo.example.com
              user: sync@example.com
            """
        ))
        assert load_config(p).odoo.db is None


# --------------------------------------------------------------------------- #
# Database discovery
#
# Odoo answers /web/database/list unauthenticated, so the database can be
# resolved before the login that needs it. Servers may disable the listing
# (list_db = False), which is why 'db' stays declarable.
# --------------------------------------------------------------------------- #
class TestResolveDatabase:
    def test_declared_db_is_used_without_asking_the_server(self):
        """A declared name must short-circuit: no HTTP call may happen.

        respx raises on unmocked requests, so any lookup fails this test.
        """
        import respx

        with respx.mock:
            cfg = OdooConfig(
                url="https://odoo.example.com", db="mydb", user="u"
            )
            assert odoo.resolve_database(cfg) == "mydb"

    def test_single_database_is_adopted(self):
        import httpx
        import respx

        with respx.mock:
            route = respx.post(
                "https://odoo.example.com/web/database/list"
            ).mock(return_value=httpx.Response(200, json={"result": ["odoo18"]}))
            cfg = OdooConfig(url="https://odoo.example.com", user="u")

            assert odoo.resolve_database(cfg) == "odoo18"
            assert route.called

    def test_several_databases_is_an_error_naming_them(self):
        """Guessing among several would silently talk to the wrong data."""
        import httpx
        import respx

        with respx.mock:
            respx.post("https://odoo.example.com/web/database/list").mock(
                return_value=httpx.Response(
                    200, json={"result": ["prod", "staging"]}
                )
            )
            cfg = OdooConfig(url="https://odoo.example.com", user="u")

            with pytest.raises(OdooError, match="prod, staging"):
                odoo.resolve_database(cfg)

    def test_listing_disabled_is_reported_with_a_remedy(self):
        """`list_db = False` is standard hardening, not a bug: say how to
        proceed rather than reporting a bare HTTP failure."""
        import httpx
        import respx

        with respx.mock:
            respx.post("https://odoo.example.com/web/database/list").mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            cfg = OdooConfig(url="https://odoo.example.com", user="u")

            with pytest.raises(OdooError, match="'db'"):
                odoo.resolve_database(cfg)

    def test_empty_listing_is_an_error(self):
        import httpx
        import respx

        with respx.mock:
            respx.post("https://odoo.example.com/web/database/list").mock(
                return_value=httpx.Response(200, json={"result": []})
            )
            cfg = OdooConfig(url="https://odoo.example.com", user="u")

            with pytest.raises(OdooError, match="no database"):
                odoo.resolve_database(cfg)

    def test_unreachable_server_is_reported(self):
        import httpx
        import respx

        with respx.mock:
            respx.post("https://odoo.example.com/web/database/list").mock(
                side_effect=httpx.ConnectError("nope")
            )
            cfg = OdooConfig(url="https://odoo.example.com", user="u")

            with pytest.raises(OdooError, match="cannot reach"):
                odoo.resolve_database(cfg)

    @pytest.mark.parametrize("code", [301, 302, 307, 308])
    def test_redirect_is_re_posted(self, code):
        """Typing the bare domain of a site that redirects to www. is
        normal, so the hop must work — and it must stay a POST.

        httpx's own follow_redirects implements browser semantics, where
        301/302 downgrade a POST to a GET; this JSON-RPC endpoint does not
        answer GET, so the redirect must be re-POSTed by hand.
        """
        import httpx
        import respx

        with respx.mock:
            respx.post("https://odoo.example.com/web/database/list").mock(
                return_value=httpx.Response(
                    code,
                    headers={
                        "Location":
                            "https://www.odoo.example.com/web/database/list"
                    },
                )
            )
            methods = []

            def record(request):
                methods.append(request.method)
                return httpx.Response(200, json={"result": ["odoo18"]})

            respx.route(host="www.odoo.example.com").mock(side_effect=record)
            cfg = OdooConfig(url="https://odoo.example.com", user="u")

            assert odoo.resolve_database(cfg) == "odoo18"
            assert methods == ["POST"]

    def test_redirect_loop_does_not_hang(self):
        import httpx
        import respx

        with respx.mock:
            respx.post("https://odoo.example.com/web/database/list").mock(
                return_value=httpx.Response(
                    302,
                    headers={
                        "Location":
                            "https://odoo.example.com/web/database/list"
                    },
                )
            )
            cfg = OdooConfig(url="https://odoo.example.com", user="u")

            with pytest.raises(OdooError, match="would not say"):
                odoo.resolve_database(cfg)

    def test_trailing_slash_in_url_is_tolerated(self):
        import httpx
        import respx

        with respx.mock:
            respx.post("https://odoo.example.com/web/database/list").mock(
                return_value=httpx.Response(200, json={"result": ["odoo18"]})
            )
            cfg = OdooConfig(url="https://odoo.example.com/", user="u")

            assert odoo.resolve_database(cfg) == "odoo18"


# --------------------------------------------------------------------------- #
# Config.merge_targets
# --------------------------------------------------------------------------- #
class TestMergeTargets:
    def test_merges_as_root_targets(self):
        config = Config()
        config.merge_targets(
            [Target(name="srv-01", kind="ssh", host="h1", auth="publickey")]
        )
        assert [t.name for t in config.root_targets] == ["srv-01"]

    def test_collision_with_existing(self):
        config = Config(
            root_targets=[
                Target(name="srv-01", kind="ssh", host="h0", auth="publickey")
            ]
        )
        with pytest.raises(ConfigError, match="collides"):
            config.merge_targets(
                [Target(name="srv-01", kind="ssh", host="h1", auth="publickey")]
            )

    def test_collision_within_batch(self):
        config = Config()
        batch = [
            Target(name="srv-01", kind="ssh", host="h1", auth="publickey"),
            Target(name="srv-01", kind="ssh", host="h2", auth="publickey"),
        ]
        with pytest.raises(ConfigError, match="collides"):
            config.merge_targets(batch)


# --------------------------------------------------------------------------- #
# CLI: fetch
# --------------------------------------------------------------------------- #
@pytest.fixture
def odoo_config_file(tmp_path):
    p = tmp_path / "wgman.yaml"
    p.write_text(textwrap.dedent(
        """
        servers:
          - name: prod
            url: https://wg.example.com:8888
            api-key: tok
        odoo:
          url: https://odoo.example.com
          db: mydb
          user: sync@example.com
          password: pw
        """
    ))
    return p


@pytest.fixture
def stub_fetcher(monkeypatch):
    """Replace the oerpc-backed query factory with a canned-records stub.

    Yields the list of configs passed to the factory (one per fetch_state
    call); each stub query serves RECORDS for equipments, nothing else.
    """
    calls = []

    def fake_make_query(cfg):
        calls.append(cfg)
        return lambda model, domain, fields: (
            RECORDS if model == "maintenance.equipment" else []
        )

    monkeypatch.setattr("wgman.odoo._make_default_query", fake_make_query)
    return calls


class TestCliFetch:
    def test_prints_targets_yaml(self, odoo_config_file, stub_fetcher, capsys):
        rc = main(["-c", str(odoo_config_file), "fetch"])
        assert rc == 0
        out = capsys.readouterr().out
        import yaml as yaml_mod

        data = yaml_mod.safe_load(out)
        assert data == {
            "targets": [
                {
                    "name": "srv-01",
                    "kind": "ssh",
                    "host": "srv1.example.com",
                    "port": 22,
                    "username": "root",
                    "auth": "publickey",
                },
                {
                    "name": "srv-02",
                    "kind": "ssh",
                    "host": "srv2.example.com",
                    "port": 2222,
                    "username": "admin",
                    "auth": "publickey",
                },
            ]
        }

    def test_fetch_output_roundtrips_as_config(
        self, odoo_config_file, stub_fetcher, capsys, tmp_path
    ):
        """The fetch listing is valid desired-state config."""
        main(["-c", str(odoo_config_file), "fetch"])
        out = capsys.readouterr().out
        p = tmp_path / "state.yaml"
        p.write_text("servers: []\n" + out)
        config = load_config(p)
        assert [t.name for t in config.root_targets] == ["srv-01", "srv-02"]

    def test_cli_overrides(self, odoo_config_file, stub_fetcher, capsys):
        rc = main([
            "-c", str(odoo_config_file), "--odoo-url", "https://other.example.com",
            "--odoo-db", "otherdb", "fetch",
        ])
        assert rc == 0
        (cfg,) = stub_fetcher
        assert cfg.url == "https://other.example.com"
        assert cfg.db == "otherdb"
        assert cfg.user == "sync@example.com"  # from config file
        assert cfg.password == "pw"  # from config file

    def test_no_odoo_source(self, tmp_path, capsys):
        p = tmp_path / "wgman.yaml"
        p.write_text("servers:\n  - {name: p, url: u, api-key: k}\n")
        rc = main(["-c", str(p), "fetch"])
        assert rc == 2
        assert "no Odoo source" in capsys.readouterr().err

    def test_incomplete_cli_only_source(self, tmp_path, capsys):
        """Only url and user are mandatory; the database can be discovered."""
        p = tmp_path / "wgman.yaml"
        p.write_text("servers:\n  - {name: p, url: u, api-key: k}\n")
        rc = main([
            "-c", str(p), "--odoo-url", "https://odoo.example.com", "fetch",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--odoo-user" in err
        assert "--odoo-db" not in err

    def test_password_prompt_no_tty(self, tmp_path, stub_fetcher, capsys):
        p = tmp_path / "wgman.yaml"
        p.write_text(textwrap.dedent(
            """
            servers: []
            odoo:
              url: https://odoo.example.com
              db: mydb
              user: sync@example.com
            """
        ))
        # pytest runs with stdin not a TTY.
        rc = main(["-c", str(p), "fetch"])
        assert rc == 2
        assert "not a TTY" in capsys.readouterr().err

    def test_password_prompt_tty(
        self, tmp_path, stub_fetcher, capsys, monkeypatch
    ):
        p = tmp_path / "wgman.yaml"
        p.write_text(textwrap.dedent(
            """
            servers: []
            odoo:
              url: https://odoo.example.com
              db: mydb
              user: sync@example.com
            """
        ))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("getpass.getpass", lambda prompt: "typed-pw")
        rc = main(["-c", str(p), "fetch"])
        assert rc == 0
        (cfg,) = stub_fetcher
        assert cfg.password == "typed-pw"


class TestCliFetchLenientEnv:
    """fetch must not require env vars used only by other sections."""

    CONFIG = textwrap.dedent(
        """
        servers:
          - name: prod
            url: https://wg.example.com:8888
            api-key: ${WG_UNSET_TOKEN}
        odoo:
          url: https://odoo.example.com
          db: mydb
          user: sync@example.com
          password: pw
        """
    )

    def test_fetch_ignores_unset_server_env(
        self, tmp_path, stub_fetcher, capsys, monkeypatch
    ):
        monkeypatch.delenv("WG_UNSET_TOKEN", raising=False)
        p = tmp_path / "wgman.yaml"
        p.write_text(self.CONFIG)
        rc = main(["-c", str(p), "fetch"])
        assert rc == 0
        assert "srv-01" in capsys.readouterr().out

    def test_diff_still_strict(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("WG_UNSET_TOKEN", raising=False)
        p = tmp_path / "wgman.yaml"
        p.write_text(self.CONFIG)
        rc = main(["-c", str(p), "diff"])
        assert rc == 2
        assert "WG_UNSET_TOKEN" in capsys.readouterr().err

    def test_unset_password_env_falls_back_to_prompt_error(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.delenv("ODOO_UNSET_PW", raising=False)
        p = tmp_path / "wgman.yaml"
        p.write_text(textwrap.dedent(
            """
            servers: []
            odoo:
              url: https://odoo.example.com
              db: mydb
              user: sync@example.com
              password: ${ODOO_UNSET_PW}
            """
        ))
        # stdin is not a TTY under pytest -> must error, not use '${...}'
        # as a literal password.
        rc = main(["-c", str(p), "fetch"])
        assert rc == 2
        assert "not a TTY" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# CLI: diff with Odoo-sourced targets
# --------------------------------------------------------------------------- #
class TestCliDiffWithOdoo:
    def test_odoo_targets_reach_diff(
        self, odoo_config_file, stub_fetcher, capsys
    ):
        import httpx
        import respx

        base = "https://wg.example.com:8888"
        api = f"{base}/@warpgate/admin/api"
        with respx.mock:
            for endpoint in ("roles", "target-groups", "targets", "users"):
                respx.get(f"{api}/{endpoint}").mock(
                    return_value=httpx.Response(200, json=[])
                )
            rc = main(["-c", str(odoo_config_file), "diff"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "srv-01" in out
        assert "srv-02" in out
