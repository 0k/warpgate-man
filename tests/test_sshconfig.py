"""Tests for the ssh_config(5) renderer.

These pin the *specification* of what a user must get to reach a Warpgate
target with a plain ``ssh <alias>``:

- the client connects to the BASTION (never the backend host),
- the target is selected through the SSH username ``<user>:<target>``,
- the backend's own account never appears,
- nothing weakens host-key checking, which protects the bastion hop.
"""

import shutil
import subprocess

import pytest

from wgman.sshconfig import (
    BastionInfo,
    SshConfigError,
    alias_for,
    render,
    selector_for,
    ssh_targets,
)

URL = "https://wg.example.com:8888"

INFO = {
    "username": "alice",
    "external_host": "wg.example.com",
    "external_hosts": {"ssh": "ssh.wg.example.com"},
    "ports": {"ssh": 2222, "http": 8888},
}


def target(name, kind="Ssh", **extra):
    return {"id": f"id-{name}", "name": name, "kind": kind, **extra}


# --------------------------------------------------------------------------- #
# BastionInfo.from_info
# --------------------------------------------------------------------------- #
class TestBastionInfo:
    def test_reads_username_host_and_port(self):
        info = BastionInfo.from_info(INFO, url=URL)
        assert info.username == "alice"
        assert info.host == "ssh.wg.example.com"
        assert info.port == 2222

    def test_protocol_specific_host_wins_over_global(self):
        # ssh.external_host exists precisely to override the global one
        # (split DNS, dedicated SSH endpoint...).
        info = BastionInfo.from_info(INFO, url=URL)
        assert info.host == "ssh.wg.example.com"

    def test_falls_back_to_global_external_host(self):
        payload = dict(INFO, external_hosts={})
        assert BastionInfo.from_info(payload, url=URL).host == "wg.example.com"

    def test_global_admin_token_is_refused(self):
        # Warpgate returns username=null for the global admin token: it is
        # bound to no user, so "targets I can reach" is meaningless.
        payload = dict(INFO, username=None)
        with pytest.raises(SshConfigError, match="no authenticated user"):
            BastionInfo.from_info(payload, url=URL)

    def test_admin_token_error_names_the_remedy(self):
        payload = dict(INFO, username=None)
        with pytest.raises(SshConfigError, match="personal API token"):
            BastionInfo.from_info(payload, url=URL)

    def test_missing_external_host_is_an_error(self):
        payload = dict(INFO, external_hosts={}, external_host=None)
        with pytest.raises(SshConfigError, match="external SSH host"):
            BastionInfo.from_info(payload, url=URL)

    def test_missing_ssh_port_is_an_error(self):
        payload = dict(INFO, ports={"http": 8888})
        with pytest.raises(SshConfigError, match="SSH port"):
            BastionInfo.from_info(payload, url=URL)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class TestSelector:
    def test_uses_colon_separator(self):
        assert selector_for("alice", "web-01") == "alice:web-01"

    def test_plain_selector_is_not_quoted(self):
        assert '"' not in selector_for("alice", "web-01")

    def test_selector_with_space_is_quoted(self):
        # ssh_config(5) splits on whitespace and rejects the line otherwise,
        # which invalidates the entire file.
        assert selector_for("alice", "db main") == '"alice:db main"'

    def test_double_quote_in_name_is_an_error(self):
        with pytest.raises(SshConfigError, match="double quote"):
            selector_for("alice", 'we"ird')


class TestAlias:
    def test_plain_name_is_kept(self):
        assert alias_for("web-01") == "web-01"

    @pytest.mark.parametrize(
        "name",
        ["a b", "a*b", "a?b", "a!b", "a/b", "a#b"],
    )
    def test_ssh_unsafe_characters_are_replaced(self, name):
        # Whitespace separates patterns and * ? ! are glob syntax in
        # ssh_config(5): none may survive into a Host alias.
        alias = alias_for(name)
        assert not (set(alias) & set(" *?!/#"))

    def test_prefix_is_prepended(self):
        assert alias_for("web", prefix="wg-") == "wg-web"

    def test_unusable_name_is_an_error(self):
        with pytest.raises(SshConfigError, match="Host alias"):
            alias_for("***")

    def test_distinct_names_keep_distinct_aliases(self):
        assert alias_for("a b") != alias_for("a c")


class TestSshTargets:
    def test_non_ssh_kinds_are_dropped(self):
        targets = [
            target("web", kind="Http"),
            target("db", kind="Postgres"),
            target("box"),
        ]
        assert [t["name"] for t in ssh_targets(targets)] == ["box"]

    def test_sorted_by_name(self):
        targets = [target("c"), target("a"), target("b")]
        assert [t["name"] for t in ssh_targets(targets)] == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
class TestRender:
    @pytest.fixture
    def info(self):
        return BastionInfo.from_info(INFO, url=URL)

    def test_stanza_points_at_the_bastion(self, info):
        out = render([target("web-01")], info)
        assert "Host web-01" in out
        assert "HostName ssh.wg.example.com" in out
        assert "Port     2222" in out

    def test_user_is_the_selector(self, info):
        out = render([target("web-01")], info)
        assert "User     alice:web-01" in out

    def test_backend_account_never_leaks(self, info):
        # The API does not expose it, and emitting it would be wrong: the
        # User field carries the selector, and Warpgate dials the backend
        # with its own stored credentials.
        out = render([target("web-01", username="root", host="10.0.0.9")], info)
        assert "root" not in out
        assert "10.0.0.9" not in out

    def test_never_weakens_host_key_checking(self, info):
        # known_hosts pins the bastion; disabling the check would remove the
        # only verification the client still performs.
        out = render([target("web-01")], info)
        assert "StrictHostKeyChecking" not in out
        assert "UserKnownHostsFile" not in out

    def test_no_wildcard_host_block(self, info):
        # A 'Host *' stanza would leak these settings onto every host in the
        # user's ssh config.
        out = render([target("web-01")], info)
        assert "Host *" not in out

    def test_all_ssh_targets_are_rendered(self, info):
        out = render([target("a"), target("b")], info)
        assert "Host a" in out and "Host b" in out

    def test_non_ssh_targets_are_skipped(self, info):
        out = render([target("web", kind="Http")], info)
        assert out == ""

    def test_empty_when_no_targets(self, info):
        assert render([], info) == ""

    def test_prefix_applies_to_alias_not_selector(self, info):
        out = render([target("web")], info, prefix="wg-")
        assert "Host wg-web" in out
        # The selector must keep the real target name or Warpgate cannot
        # resolve it.
        assert "User     alice:web" in out

    def test_description_is_emitted_as_comment(self, info):
        out = render([target("web", description="front server")], info)
        assert "# front server" in out

    def test_header_is_included(self, info):
        out = render([target("web")], info, header="# prod")
        assert out.startswith("# prod\n")

    def test_output_is_valid_stanza_shape(self, info):
        out = render([target("web")], info)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert lines[0] == "Host web"
        # Continuation lines must be indented, else ssh reads them as new
        # top-level keywords.
        assert all(ln.startswith("    ") for ln in lines[1:])

    def test_target_name_with_space_is_quoted(self, info):
        out = render([target("db main")], info)
        assert 'User     "alice:db main"' in out


# --------------------------------------------------------------------------- #
# Integration: the real ssh binary must accept what we emit.
#
# String assertions cannot tell whether OpenSSH actually parses the file; a
# single malformed line invalidates the WHOLE config, silently breaking every
# other host in it. `ssh -G` resolves a host and reports parse errors, so it
# is the authority on validity here.
# --------------------------------------------------------------------------- #
ssh_binary = pytest.mark.skipif(
    shutil.which("ssh") is None, reason="ssh binary not available"
)


@ssh_binary
class TestRealSshParsesOutput:
    @pytest.fixture
    def resolve(self, tmp_path):
        def _resolve(targets, alias, prefix=""):
            info = BastionInfo.from_info(INFO, url=URL)
            path = tmp_path / "sshconfig"
            path.write_text(render(targets, info, prefix=prefix))
            proc = subprocess.run(
                ["ssh", "-F", str(path), "-G", alias],
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, (
                f"ssh rejected the generated config: {proc.stderr.strip()}"
            )
            return dict(
                line.split(" ", 1)
                for line in proc.stdout.splitlines()
                if " " in line
            )

        return _resolve

    def test_resolves_to_the_bastion(self, resolve):
        settings = resolve([target("web-01")], "web-01")
        assert settings["hostname"] == "ssh.wg.example.com"
        assert settings["port"] == "2222"

    def test_resolves_the_selector_as_user(self, resolve):
        settings = resolve([target("web-01")], "web-01")
        assert settings["user"] == "alice:web-01"

    def test_target_name_with_space_survives_round_trip(self, resolve):
        # The alias is sanitised, but the selector ssh sends must be the
        # real target name or Warpgate cannot resolve it.
        settings = resolve([target("db main")], "db_main")
        assert settings["user"] == "alice:db main"

    def test_one_odd_target_does_not_break_the_others(self, resolve):
        # A parse error anywhere invalidates the entire file, so a single
        # awkward target name must not take the rest down with it.
        targets = [target("db main"), target("web-01")]
        assert resolve(targets, "web-01")["user"] == "alice:web-01"

    def test_prefixed_alias_resolves(self, resolve):
        settings = resolve([target("web-01")], "wg-web-01", prefix="wg-")
        assert settings["user"] == "alice:web-01"
