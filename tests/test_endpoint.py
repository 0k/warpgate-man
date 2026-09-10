"""Parsing a user-supplied bastion address, and building a BastionInfo
from it without asking any server."""

from __future__ import annotations

import pytest

from wgman.sshconfig import (
    DEFAULT_SSH_PORT,
    BastionInfo,
    SshConfigError,
    parse_endpoint,
)


class TestParseEndpoint:
    def test_host_only_uses_the_default_port(self):
        assert parse_endpoint("wg.example.com") == ("wg.example.com", DEFAULT_SSH_PORT)

    def test_host_and_port(self):
        assert parse_endpoint("wg.example.com:2222") == ("wg.example.com", 2222)

    def test_ipv4(self):
        assert parse_endpoint("10.0.0.1:22") == ("10.0.0.1", 22)

    def test_surrounding_whitespace_is_tolerated(self):
        assert parse_endpoint("  wg.example.com:2222  ") == ("wg.example.com", 2222)

    def test_explicit_default_port_override(self):
        assert parse_endpoint("wg.example.com", default_port=22) == (
            "wg.example.com", 22,
        )

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_is_rejected(self, value):
        with pytest.raises(SshConfigError, match="empty bastion address"):
            parse_endpoint(value)

    def test_missing_host_is_rejected(self):
        with pytest.raises(SshConfigError, match="no host"):
            parse_endpoint(":2222")

    def test_non_numeric_port_is_rejected(self):
        with pytest.raises(SshConfigError, match="invalid port"):
            parse_endpoint("wg.example.com:ssh")

    @pytest.mark.parametrize("port", ["0", "65536", "99999"])
    def test_out_of_range_port_is_rejected(self, port):
        with pytest.raises(SshConfigError, match="out of range"):
            parse_endpoint(f"wg.example.com:{port}")


class TestIPv6:
    """An IPv6 literal is all colons: it must be bracketed, or host and
    port cannot be told apart."""

    def test_bracketed_with_port(self):
        assert parse_endpoint("[2001:db8::1]:2222") == ("2001:db8::1", 2222)

    def test_bracketed_without_port(self):
        assert parse_endpoint("[::1]") == ("::1", DEFAULT_SSH_PORT)

    def test_bare_ipv6_is_refused_rather_than_mangled(self):
        with pytest.raises(SshConfigError, match="ambiguous"):
            parse_endpoint("2001:db8::1")

    def test_unclosed_bracket_is_rejected(self):
        with pytest.raises(SshConfigError, match="malformed"):
            parse_endpoint("[2001:db8::1")

    def test_empty_brackets_are_rejected(self):
        with pytest.raises(SshConfigError, match="malformed"):
            parse_endpoint("[]:2222")

    def test_junk_after_bracket_is_rejected(self):
        with pytest.raises(SshConfigError, match="trailing junk"):
            parse_endpoint("[::1]x")


class TestBastionInfoDeclared:
    def test_builds_from_supplied_facts(self):
        info = BastionInfo.declared(
            username="alice@example.com", host="wg.example.com", port=2222
        )
        assert (info.username, info.host, info.port) == (
            "alice@example.com", "wg.example.com", 2222,
        )

    def test_defaults_the_port(self):
        info = BastionInfo.declared(username="alice", host="wg.example.com")
        assert info.port == DEFAULT_SSH_PORT

    def test_empty_username_is_refused(self):
        with pytest.raises(SshConfigError, match="no username"):
            BastionInfo.declared(username="", host="wg.example.com")

    def test_empty_host_names_the_remedy(self):
        with pytest.raises(SshConfigError, match="--bastion"):
            BastionInfo.declared(username="alice", host="")
