# warpgate-man — agent notes

Declarative manager for Warpgate bastion configuration. Reads a YAML file
describing desired state (targets, target-groups, users, roles) and reconciles
one or more Warpgate servers via their HTTP admin API.

## Layout

- `src/wgman/` — the library (package `wgman`)
  - `models.py` — dataclasses + JSON mapping to/from the Warpgate admin API
  - `config.py` — YAML parsing, validation, `${ENV}` interpolation
  - `client.py` — synchronous httpx clients: `_Transport` (shared connection
    + error translation), `WarpgateClient` (`/@warpgate/admin/api`),
    `WarpgateUserClient` (`/@warpgate/api`)
  - `reconcile.py` — diff + apply logic (create / update / prune)
  - `manager.py` — `WarpgateManager`, the high-level public API
  - `sshconfig.py` — pure renderer: user-API payloads → `ssh_config(5)`
  - `odoo.py` — Odoo as desired-state source (SSH targets from
    `maintenance.equipment.ssh_target`, via the `oerpc` lib)
  - `exceptions.py` — `WgmanError` hierarchy
  - `cli.py` — argparse CLI (`fetch`, `diff`, `apply`, `ssh-config`,
    `--prune`, `--server`, `--config`,
    `--odoo-url`/`--odoo-db`/`--odoo-user`)
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

## User API + `ssh-config` (verified against warpgate @ bb88fff)

Warpgate has a SECOND API at `/@warpgate/api` (no `admin/`), for what one
authenticated *user* may see. Same `X-Warpgate-Token` header — the middleware
tries the global admin token first, then the per-user `api_tokens` table.
`ssh-config` is built on it; admins are users too, so there is one code path.

- `GET /targets` is **already filtered server-side** by role intersection.
  Do NOT re-implement access logic. It returns
  `{id, name, description, kind, external_host, group, default_database_name}`
  and deliberately NOT the backend `host`/`port`/`username`.
- A per-user token whose user holds an admin role is filtered like any other
  user's — there is no admin-role bypass in that handler.
- The **global** admin token (`--enable-admin-token`) is bound to no user:
  upstream has an explicit `RequestAuthorization::AdminToken => targets.clear()`
  branch, and `/info` returns `username: null`. So it yields an EMPTY list, not
  the whole fleet. `BastionInfo.from_info` refuses it on the null username
  rather than emitting a silently empty config.
- `GET /info` gives `username`, `external_hosts.<proto>`, `ports.<proto>`.
  Always take the SSH host/port from there: it honours reverse-proxy / NAT
  config, unlike the 2222 default.
- SSH addressing: the client only ever reaches the BASTION, and the target is
  selected through the username, `<user>:<target>` (`:` is what Warpgate's own
  UI generates; `#` also parses). The backend account must never be emitted —
  Warpgate dials the backend itself. `known_hosts` pins the bastion, so never
  emit `StrictHostKeyChecking no`.
- **ssh_config(5) quoting**: target names may contain spaces; an unquoted
  `User a:b c` is rejected with "extra arguments at end of line" and that
  invalidates the WHOLE file, not just the stanza. `selector_for()` quotes
  such values. `tests/test_sshconfig.py` verifies this with the real `ssh -G`
  binary — string assertions alone cannot catch it.
- SSO cannot be driven headlessly (browser-redirect only; PKCE verifier lives
  server-side, device-code endpoint unset). SSO users mint a personal API
  token in the web UI. Do not promise CLI SSO.

## Conventions

- YAML config keys are **dash-cased** (`target-groups`, `api-key`,
  `external-host`), per the user's global rules.
- `api-key` is *the caller's* token, not necessarily an admin one: the persona
  lives in the token, not in the config schema. Hence one `servers:` section
  for both admin and end-user commands.
- API errors are translated at the client boundary into the `ApiError`
  subclasses (`AuthenticationError` 401, `AuthorizationError` 403,
  `UnsupportedApiError` 404-on-user-API) with messages stating cause and
  remedy; the raw status/body stay on the exception.
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
