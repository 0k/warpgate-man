"""Tests for YAML config loading, validation and ${ENV} interpolation."""

import textwrap

import pytest

from wgman.config import Config, interpolate, load_config
from wgman.exceptions import ConfigError, InterpolationError


class TestInterpolation:
    def test_basic(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "abc123")
        assert interpolate("${MY_TOKEN}") == "abc123"

    def test_embedded(self, monkeypatch):
        monkeypatch.setenv("H", "host")
        assert interpolate("a-${H}-b") == "a-host-b"

    def test_missing_var_raises(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        with pytest.raises(InterpolationError, match="NOPE"):
            interpolate("${NOPE}")

    def test_no_placeholder_passthrough(self):
        assert interpolate("plain") == "plain"


EXAMPLE = textwrap.dedent(
    """
    servers:
      - name: prod
        url: https://wg.example.com:8888
        api-key: ${WG_TOKEN}
    roles:
      - name: admin
      - name: dba
    target-groups:
      - name: databases
        color: info
        targets:
          - name: pg-main
            kind: ssh
            host: 10.0.0.5
            username: postgres
            auth: publickey
            roles: [dba, admin]
    targets:
      - name: jump
        kind: ssh
        host: 192.168.1.1
        roles: [admin]
    users:
      - name: alice
        roles: [admin, dba]
    """
)


class TestLoadConfig:
    def _write(self, tmp_path, content):
        p = tmp_path / "wgman.yaml"
        p.write_text(content)
        return p

    def test_full_example(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WG_TOKEN", "secret-token")
        cfg = load_config(self._write(tmp_path, EXAMPLE))
        assert isinstance(cfg, Config)
        assert cfg.server("prod").api_key == "secret-token"
        assert {r.name for r in cfg.roles} == {"admin", "dba"}
        # grouped + ungrouped targets are flattened
        names = {t.name for t in cfg.all_targets()}
        assert names == {"pg-main", "jump"}
        # nested target carries its group
        pg = next(t for t in cfg.all_targets() if t.name == "pg-main")
        assert pg.group == "databases"

    def test_interpolation_applied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WG_TOKEN", "xyz")
        cfg = load_config(self._write(tmp_path, EXAMPLE))
        assert cfg.servers[0].api_key == "xyz"

    def test_missing_env_var_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WG_TOKEN", raising=False)
        with pytest.raises(InterpolationError):
            load_config(self._write(tmp_path, EXAMPLE))

    def test_unknown_role_reference_raises(self, tmp_path):
        content = textwrap.dedent(
            """
            roles:
              - name: admin
            users:
              - name: bob
                roles: [ghost]
            """
        )
        with pytest.raises(ConfigError, match="unknown role 'ghost'"):
            load_config(self._write(tmp_path, content))

    def test_duplicate_target_raises(self, tmp_path):
        content = textwrap.dedent(
            """
            roles:
              - name: admin
            targets:
              - name: dup
                kind: ssh
                host: a
              - name: dup
                kind: ssh
                host: b
            """
        )
        with pytest.raises(ConfigError, match="duplicate target name 'dup'"):
            load_config(self._write(tmp_path, content))

    def test_empty_file_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="is empty"):
            load_config(self._write(tmp_path, ""))

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "does-not-exist.yaml")

    def test_server_lookup_unknown_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WG_TOKEN", "x")
        cfg = load_config(self._write(tmp_path, EXAMPLE))
        with pytest.raises(ConfigError, match="no server named 'ghost'"):
            cfg.server("ghost")


class TestPruneSection:
    def _write(self, tmp_path, content):
        p = tmp_path / "wgman.yaml"
        p.write_text(content)
        return p

    def test_absent_section(self, tmp_path):
        cfg = load_config(self._write(
            tmp_path, "servers:\n  - {name: p, url: u, api-key: k}\n"
        ))
        assert cfg.prune is None

    def test_parsed(self, tmp_path):
        cfg = load_config(self._write(tmp_path, textwrap.dedent(
            """
            servers:
              - {name: p, url: u, api-key: k}
            prune:
              targets: true
              users: false
              keep-users: [admin, valentin]
              keep-roles: [admin]
            """
        )))
        assert cfg.prune is not None
        assert cfg.prune.prune_targets is True
        assert cfg.prune.prune_users is False
        assert cfg.prune.keep_users == {"admin", "valentin"}
        assert cfg.prune.keep_roles == {"admin"}

    def test_defaults_within_section(self, tmp_path):
        # An empty-ish prune section still yields targets-only defaults.
        cfg = load_config(self._write(tmp_path, textwrap.dedent(
            """
            servers:
              - {name: p, url: u, api-key: k}
            prune:
              keep-users: [admin]
            """
        )))
        assert cfg.prune.prune_targets is True
        assert cfg.prune.prune_users is False
        assert cfg.prune.prune_roles is False
        assert cfg.prune.prune_target_groups is False
        assert cfg.prune.keep_users == {"admin"}

    def test_unknown_key_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown key 'targts'"):
            load_config(self._write(tmp_path, textwrap.dedent(
                """
                servers:
                  - {name: p, url: u, api-key: k}
                prune:
                  targts: true
                """
            )))
