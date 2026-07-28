# warpgate-man — agent notes

Declarative manager for Warpgate bastion configuration. Reads a YAML file
describing desired state (targets, target-groups, users, roles) and reconciles
one or more Warpgate servers via their HTTP admin API.

## Layout

- `src/wgman/` — the library (package `wgman`)
  - `models.py` — dataclasses + JSON mapping to/from the Warpgate admin API
  - `config.py` — YAML parsing, validation, `${ENV}` interpolation
  - `client.py` — synchronous httpx client for `/@warpgate/admin/api`
  - `reconcile.py` — diff + apply logic (create / update / prune)
  - `manager.py` — `WarpgateManager`, the high-level public API
  - `odoo.py` — Odoo as desired-state source (SSH targets from
    `maintenance.equipment.ssh_target`, via the `oerpc` lib)
  - `exceptions.py` — `WgmanError` hierarchy
  - `cli.py` — argparse CLI (`fetch`, `diff`, `apply`, `--prune`, `--server`,
    `--config`, `--odoo-url`/`--odoo-db`/`--odoo-user`)
- `tests/` — pytest suite (uses `respx` to mock the HTTP API; the Odoo
  source is tested with an injected fetcher, no oerpc mocking)
- `examples/wgman.yaml` — annotated example config

## Key facts about Warpgate (verified against a live warpgate 0.25.4 server)

- Targets, users, roles live ONLY in Warpgate's database, NOT in
  `warpgate.yaml`. The only programmatic interface is the admin API at
  `/@warpgate/admin/api`.
- Auth: header `X-Warpgate-Token: <token>`. Use an admin token
  (`--enable-admin-token` + `WARPGATE_ADMIN_TOKEN` on the server), or insert a
  row in the `api_tokens` table for an admin user.
- A target's `options` is a discriminated union on `kind`, flattened so `kind`
  sits at the top of the target object.
- Access control is role-based: a role is assigned to BOTH a user and a target;
  a user reaches a target if they share a role. Target-groups are purely
  organisational (a `group_id` + UI color), they grant no access.

### Wire-format gotchas (the API is NOT what the source serde-renames suggest)

The admin API uses **PascalCase** enum values on the wire (confirmed via the
live `openapi.json`). `models.py` accepts friendly lowercase in YAML and maps
to these at the API boundary:

- Target `kind`: `Ssh`, `Http`, `MySql`, `Postgres`, `Kubernetes`.
- SSH/DB `auth.kind`: `PublicKey`, `Password`, `IamRole`.
- Group `color` (`BootstrapThemeColor`): `Primary`, `Secondary`, `Success`,
  `Danger`, `Warning`, `Info`, `Light`, `Dark`.
- `http`/`mysql`/`postgres` options REQUIRE a `tls` block
  (`{mode: Disabled|Preferred|Required, verify: bool}`).
- `POST /users/{id}/roles/{role_id}` declares a JSON body (`AddUserRoleRequest`)
  and returns HTTP 415 if no JSON is sent — send `{}`. The equivalent target
  endpoint takes NO body.
- A role flagged `is_default` is auto-assigned to every user and CANNOT be
  removed (the DELETE returns 204 but the role re-attaches). The reconciler
  must treat default roles as implicit and never try to remove them, or it
  never converges.

## Conventions

- YAML config keys are **dash-cased** (`target-groups`, `api-key`,
  `external-host`), per the user's global rules.
- Synchronous API (httpx sync) — designed to be callable from Odoo / plain
  scripts.
- `apply` does create + update only; deletions require `--prune` / `prune=True`.

## Prune scope (`PruneConfig`)

- `--prune` is the master switch; *what* it deletes is governed by the
  optional `prune:` config section (parsed into `Config.prune`,
  `models.PruneConfig`).
- **Default (no `prune:` section): targets only.** `PruneConfig.default()`
  prunes targets and leaves target-groups / roles / users untouched — the
  safe choice for the Odoo workflow (Odoo sources targets, not users/roles;
  a naive full prune would wipe the admin user).
- Per-kind flags (`targets`/`target-groups`/`roles`/`users`, dash-cased in
  YAML) toggle deletion per kind; `keep-<kind>` lists protect individual
  names even when that kind is pruned (e.g. `keep-users: [admin]`).
- Reconciler mechanics: `reconcile()` takes `prune` (bool master switch) +
  `prune_config`; each `_reconcile_*` receives `prune and pc.prune_<kind>`
  plus the matching keep-set. `reconcile_config()` passes `config.prune`
  through (None → targets-only default).

## Odoo source (`odoo.py`)

- Desired state can come from an Odoo server instead of inline YAML: the
  config's `odoo:` section (`url`, `db`, `user`, optional `password`) or the
  CLI overrides `--odoo-url` / `--odoo-db` / `--odoo-user`.
- Mapping: `maintenance.equipment` records (Elabore `maintenance_server_data`
  module) with `ssh_target` set become SSH targets. `ssh_target` format:
  `[user@]DOMAIN_OR_IP[:PORT]` (defaults `root` / 22); the target label is the
  equipment `name`; auth is `publickey`. SSH targets only for now — no
  users/roles/groups from Odoo.
- Password: config value (with `${ENV}`) or interactive `getpass` prompt on a
  TTY; no CLI flag on purpose (would leak into `ps` / shell history).
- `fetch` prints the Odoo-sourced targets as a YAML `targets:` listing that
  round-trips as config; `diff`/`apply` merge them into `Config.root_targets`
  (name collisions with file targets are errors).
- `fetch` loads the config with `strict_env=False`: unset `${ENV}` refs used
  by other sections (e.g. the Warpgate `api-key`) don't block it; a kept
  `${...}` literal in the Odoo password is treated as unset.
- Transport is `oerpc` (JSON-RPC), a local project at `../oerpc`, wired as
  the `odoo` extra via `[tool.uv.sources]` until published. Tests inject a
  fake fetcher instead of mocking oerpc.

## Dev

```sh
uv sync --extra dev --extra odoo      # odoo extra needs ../oerpc checkout
uv run pytest
```
