---
sidebar_position: 8
title: Authentication store authority
---

# Authentication store authority

Hermes resolves every OAuth and credential-pool read, write, refresh, status check, and lock through one configured authority. The default is a single shared store at `~/.hermes/auth.json`, so named profiles can use the same login without copying tokens.

## Configuration

Set the authority in the active profile's `config.yaml`:

```yaml
auth:
  authority: shared  # shared | profile
```

- `shared` (default): `~/.hermes/auth.json`. Named profiles use the default Hermes root.
- `profile`: `<profile-home>/auth.json`. Use this for deliberate credential isolation.
Invalid authorities fail closed. Hermes does not silently switch to another credential store.

### Existing profiles

When `auth.authority` is absent and an existing named profile already has `auth.json`, Hermes temporarily selects that profile-local store in bounded legacy-compatibility mode. A fresh profile with no local store uses `shared`. Configure the authority explicitly or use the migration workflow below to remove ambiguity.

## Inspect the active authority

```bash
hermes auth status
hermes doctor
```

These commands show the selected mode, canonical store and lock paths, provenance, permissions, legacy-compatibility state, conflicting non-authoritative stores, and the latest migration phase. They list no tokens or complete credential payloads.

Provider status remains available by naming a provider:

```bash
hermes auth status nous
```

## Migrate profile stores to shared authority

Migration is dry-run first and requires an explicit profile scope:

```bash
hermes auth migrate-shared --profile coder --dry-run
hermes auth migrate-shared --all-profiles --dry-run
```

The dry-run prints a redacted manifest, `plan_id`, and `plan_digest`. It stores full precondition hashes only in a private mode-0600 artifact. Review the provider topology, then apply the exact plan:

```bash
hermes auth migrate-shared --profile coder --apply \
  --plan-id <id> --plan-digest <digest> --conflict-policy abort
```

Conflict policies are:

- `abort`: stop before committing a divergent provider entry.
- `prefer-shared`: preserve the shared entry.
- `prefer-profile`: replace a divergent shared entry with the selected profile's entry. With `--all-profiles`, profiles are merged by stable profile-name order.

Apply acquires all relevant auth locks in stable bytewise path order, validates that every source and config is unchanged since dry-run, creates private recovery backups and a journal, writes the shared store atomically, then changes selected profiles to `auth.authority: shared`. Legacy source `auth.json` files are preserved byte-for-byte as recovery material; they become non-authoritative rather than being deleted.

If a process is interrupted after backup but before commit, inspect the journal with `hermes auth status` and roll it back:

```bash
hermes auth migrate-recover --plan-id <id>
```

Recovery is idempotent. A committed migration is not implicitly rolled back.

To explicitly undo a committed migration, use `hermes auth migrate-shared --rollback --plan-id <id>`. Rollback is refused if the shared auth store or any migrated profile config changed after commit, so later credential rotations or configuration edits are never overwritten.

## Backups and restore

Auth is excluded from normal backups. To include it, encrypt it explicitly:

```bash
hermes backup --auth-mode include-encrypted --auth-passphrase-file /secure/passphrase
```

Restore requires an explicit destination:

```bash
hermes import backup.zip --auth-action restore-shared --auth-passphrase-file /secure/passphrase
hermes import backup.zip --auth-action restore-profile --auth-passphrase-file /secure/passphrase
```

Hermes validates the encrypted envelope, passphrase, topology, and gateway quiescence before extracting ordinary files. Auth and the active `config.yaml` are committed under the canonical auth lock and rolled back together on failure. Every live gateway resolving to the destination authority must be stopped first.

Quick snapshots record the resolved topology and copy the authoritative store under its canonical lock. Normal quick restore skips credentials. Programmatic restore must pass `include_auth=True` and an explicit `auth_action`; topology mismatches fail before non-auth files are changed.

## Profile lifecycle

- `hermes profile create NAME` creates an explicitly `shared` profile.
- `hermes profile create NAME --auth-mode profile` creates a profile-local authority. Cloning never copies credentials.
- Renaming quiesces gateways and Desktop/backend writers before moving profile-local authority state.
- Deleting a profile-local authority requires `--auth-action archive` or `--auth-action purge`. Archive runs only after all known profile writers stop. Shared credentials are never deleted with one profile.

## Docker and NixOS

Docker bootstrap and Nous session rebootstrap both resolve the same canonical authority and mutate it through the same lock-protected helper. Bootstrap is create-only. Rebootstrap replaces only a terminal or provably older Nous entry and never clobbers a healthy newer session.

On NixOS, `services.hermes-agent.authAuthority` emits the matching `auth.authority` setting. `authFile` is a one-time seed: activation atomically creates a missing target and never overwrites an existing store. If `services.hermes-agent.configFile` is supplied, the module merges `authAuthority` into the installed config so declared topology and seed target cannot diverge. There is intentionally no force-overwrite switch.

## First-party consumer manifest

All first-party auth-store consumers must resolve through `hermes_cli.auth_authority` (or the equivalent standalone Docker/Nix helper) and use the lock paired with the resolved data path for writes.

| Consumer | Module |
| --- | --- |
| CLI login/logout/status and provider setup | `hermes_cli.auth` |
| Setup wizard/provider readiness | `hermes_cli.main` |
| Dynamic model cache invalidation | `hermes_cli.models` |
| Credential pool refresh/account rotation | `agent.credential_pool` |
| Auxiliary models | `agent.auxiliary_client` |
| Gateway startup migration gate | `gateway.run` |
| Diagnostics | `hermes_cli.auth_commands`, `hermes_cli.doctor` |
| Backup and profile lifecycle | `hermes_cli.backup`, `hermes_cli.profiles` |
| Managed tool subprocesses | `tools.managed_tool_gateway` |
| xAI OAuth | `tools.xai_http` |
| Photon OAuth | `plugins.platforms.photon.auth` |
| Docker bootstrap/rebootstrap | `scripts/docker_auth_authority.py`, `scripts/docker_rebootstrap_nous_session.py` |
| NixOS activation seed | `scripts/nix_auth_authority.py` |

## Safe rollback before apply

Before applying, no live file is modified. Delete an unwanted private dry-run plan artifact or simply create a new plan. After an interrupted apply, use `migrate-recover`; do not copy token files manually while Hermes processes are running.
