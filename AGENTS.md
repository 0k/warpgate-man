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
  - `exceptions.py` — `WgmanError` hierarchy
  - `cli.py` — argparse CLI (`diff`, `apply`, `--prune`, `--server`, `--config`)
- `tests/` — pytest suite (uses `respx` to mock the HTTP API)
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

## Dev

```sh
uv sync --extra dev      # or: uv pip install -e '.[dev]'
uv run pytest
```
