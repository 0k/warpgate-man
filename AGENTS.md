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

## Why `ssh-config --from-odoo` exists (verified against warpgate @ c7d2841)

The user-API path above needs a *personal* token, so every end user must
log into Warpgate's web UI at least once — even though Odoo already
created them, already pushed their SSH key, and gives them no other
reason to go there. The obvious workaround does not exist:

- **An admin CANNOT mint a token for another user.** There is no
  `POST /users/{id}/credentials/api-tokens` on the admin API. API tokens
  live only at the *user* API's `/profile/api-tokens`, reachable solely
  by their owner (`warpgate-protocol-http/src/api/api_tokens.rs`). Admin
  credential endpoints cover passwords, public keys, OTP, SSO and
  certificates — never API tokens.
- So Warpgate cannot be made to do the filtering on a user's behalf; it
  is recomputed from the desired state (`access.py`).

`authorized_target_ids` (`warpgate-core/src/config_providers/db.rs`) is
just `UserRoleAssignment ⨝ TargetRoleAssignment ON role_id`, restricted
to assignments neither revoked nor expired. No target kind is excluded
and there is no hidden built-in target. Two consequences:

- **`is_default` is NOT an authorization-time special case.** A default
  role is *materialised* as a real join row by `grant_default_roles` at
  user creation. So do not union "all default roles" into a user's roles
  when reading LIVE state — but DO treat a declared `default: true` role
  as held by everyone when reading DESIRED state, since that is what
  Warpgate will have materialised.
- **`GET /users/{id}/roles` also returns revoked/expired assignments**,
  flagged by `is_active`. A live-state reader must filter on it or it
  reports access the user no longer has.

`GET /info` still yields `ports` / `external_hosts` for the global admin
token (gated on being authenticated, not on being a user); only
`username` is null. `--from-odoo` sidesteps this anyway by taking the
address from `--bastion` or the config's `ssh-host`/`ssh-port`.

## Odoo reality check (elabore.coop, 2026-09)

The live `odoo:` section sources **machines only**: ~40
`maintenance.equipment` targets, no `targets.roles`, no `users:`
selections. A literal role intersection would return the empty set for
everyone and emit empty ssh configs. Hence the rule in `access.py`: when
NOTHING declares a role, all targets are visible; one role declared
anywhere reinstates the intersection. Do not "simplify" this away — it is
what makes the feature work against the real data, and
`tests/test_access.py` pins both directions.

Note `ssh_target` is the **backend** address (`root@ceres.swiss:22`). It
must never reach a client config — the client only ever dials the
bastion. `tests/test_cli_sshconfig_odoo.py` asserts those hostnames are
absent from the output.

## Conventions

- YAML config keys are **dash-cased** (`target-groups`, `api-key`,
  `external-host`), per the user's global rules.
- `api-key` is *the caller's* token, not necessarily an admin one: the persona
  lives in the token, not in the config schema. Hence one `servers:` section
  for both admin and end-user commands.
- `api-key` is **optional in the schema** and required only at the point of
  authentication, via `ServerConfig.require_api_key()`. It authenticates
  *toward the bastion*; it does not identify a server. An end user's config
  legitimately declares `name`/`url`/`ssh-host` with no token — that is the
  premise of `ssh-config --from-odoo`, and a mandatory field forced a lying
  `api-key: ""`. Both authenticating call sites go through `require_api_key()`
  (`manager.from_server`, and the `WarpgateUserClient` in
  `cli._run_ssh_config`); it raises `ConfigError`, which the CLI maps to exit
  2 (usage), not 1. Never read `server.api_key` directly at a call site.
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
  safe choice when the desired state is partial: a full prune would wipe the
  admin user the moment (say) an Odoo user selection matches nothing.
- Per-kind flags (`targets`/`target-groups`/`roles`/`users`, dash-cased in
  YAML) toggle deletion per kind; `keep-<kind>` lists protect individual
  names even when that kind is pruned (e.g. `keep-users: [admin]`).
- Reconciler mechanics: `reconcile()` takes `prune` (bool master switch) +
  `prune_config`; each `_reconcile_*` receives `prune and pc.prune_<kind>`
  plus the matching keep-set. `reconcile_config()` passes `config.prune`
  through (None → targets-only default).

## Odoo source (`odoo.py`)

- Desired state can come from an Odoo server alongside inline YAML: the
  config's `odoo:` section (`url`, `db`, `user`, optional `password`,
  `verify-tls`, plus the `targets:` / `users:` selections) or the CLI
  overrides `--odoo-url` / `--odoo-db` / `--odoo-user`.
- Selection is raw Odoo *domains* passed verbatim to `search_read`
  (`normalize_domain` only validates shape) — no wgman-side query DSL.
  `odoo.targets.domain` defaults to `[["ssh_target", "!=", false]]`.
- Mapping (all three entity kinds, not targets only):
  - **targets**: `maintenance.equipment` records (Elabore
    `maintenance_server_data` module) matching `targets.domain`.
    `ssh_target` format `[user@]DOMAIN_OR_IP[:PORT]` (defaults `root` / 22);
    the target label is the equipment `name`; auth is `publickey`; roles come
    from `targets.roles`. SSH kind only — Odoo describes machines.
  - **users**: `res.users` matching each `users[].domain`; Warpgate username
    = Odoo `login`; roles accumulate across matching selections.
  - **public keys**: `ssh.key` records (Elabore addon: `user_id` + `key`) of
    the selected users, one batched query. `normalize_public_key` unwraps
    hard-wrapped base64 and raises on anything not OpenSSH-shaped rather than
    pushing garbage credentials.
  - **roles**: every role named in `targets.roles` / `users[].roles` is
    auto-declared, so access wiring needs no separate `roles:` listing.
- **`db` is optional** (`odoo.resolve_database`). Odoo publishes its database
  list on `/web/database/list`, unauthenticated — the same endpoint its login
  page uses — so it is resolved BEFORE the oerpc login that needs it. Exactly
  one database is adopted silently; several, none, or a server with
  `list_db = False` raise, because guessing would work against the wrong
  data. A declared `db` short-circuits the lookup (no HTTP at all).
  - The redirect hop is **re-POSTed by hand**, not delegated to httpx's
    `follow_redirects`: that implements browser semantics, where 301/302
    downgrade POST to GET, and this JSON-RPC endpoint does not answer GET.
    `tests/test_odoo.py::TestResolveDatabase` pins all four codes.
  - Login errors name the *resolved* database, not `config.db`, which is
    `None` whenever it was discovered.
- Password: config value (with `${ENV}`) or interactive `getpass` prompt on a
  TTY; no CLI flag on purpose (would leak into `ps` / shell history).
- `fetch` prints the Odoo-sourced state as a YAML `targets:` / `roles:` /
  `users:` listing that round-trips as config; `diff`/`apply` merge it via
  `Config.merge_targets` / `merge_users` / `merge_roles` (name collisions
  with file-defined targets or users are errors; roles are deduped silently).
- `fetch` loads the config with `strict_env=False`: unset `${ENV}` refs used
  by other sections (e.g. the Warpgate `api-key`) don't block it; a kept
  `${...}` literal in the Odoo password is treated as unset.
- Transport is `oerpc` (JSON-RPC), pinned as the `odoo` extra to a PEP 508
  direct reference: `oerpc @ git+https://github.com/0k/oerpc.git@0.0.2`.
  Tests inject a fake fetcher instead of mocking oerpc.
- **Never** point that dependency at a local checkout (`[tool.uv.sources]`
  with `path = "../oerpc"`). It resolves only on the author's machine, so a
  fresh clone dies with `Distribution not found`, and `pip` — which ignores
  `[tool.uv.sources]` entirely — looks for a nonexistent `oerpc` on PyPI.
  `tests/test_packaging.py` guards against reintroducing it. To work against
  a local oerpc, override it in your own environment
  (`uv pip install -e ../oerpc` after the sync), never in `pyproject.toml`.

## `scripts/odoo-get-ssh-config`

A bash bootstrapper for the end user: ensures `uv`/`git`, prompts once for
the four values (Odoo url/db/login + bastion), writes
`~/.config/wgman/config.yaml`, then delegates to the PUBLISHED package via
`uvx --from 'warpgate-man[odoo] @ git+https://…'`. Never runs from the
checkout — it is meant for machines with no clone.

- **stdout is the ssh config and nothing else**; all prompts/progress go to
  stderr, and prompts are read from `/dev/tty` so `odoo-get-ssh-config >
  ~/.ssh/config.d/warpgate` stays interactive. `tests/sh/streams` pins this.
- The generated config declares no `api-key` (see the convention above) and
  never stores the password — it emits `password: ${ODOO_PASSWORD}` and the
  script supplies the variable for that run only. Written `0600`.
- `has_tty` tests by *opening* `/dev/tty`, not by `[ -r ]`: in a container
  the node exists but opening it fails with ENXIO, and a stat-based check
  would print a prompt nobody can answer.

## Dev

```sh
uv sync --extra dev --extra odoo      # oerpc comes from its public git tag
uv run pytest                         # the library
bin/test-sh                           # the bash script (sunit)
```

`bin/test-sh` needs `sunit` and `shellcheck`. The sunit fixtures put the
sandbox on a **closed** PATH (`tests/sh/helpers.sh:ogsc_path`) with the
needed coreutils symlinked in: keeping `/usr/bin` would leave the real
`git` visible, so a "missing tool" test would silently exercise the
present-tool path and pass for the wrong reason. Test files also need
`prefix_cmd` to source the helpers, because `try` runs in a fresh
`bash -c` where they are otherwise out of scope.
