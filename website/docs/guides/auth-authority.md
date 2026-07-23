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

Quick snapshots record the resolved auth topology and copy the authoritative store under its canonical auth lock. Normal quick restore skips credentials. Programmatic restore must pass `include_auth=True`, and Hermes restores auth only when the snapshot topology matches the destination authority. This prevents a profile restore from overwriting a shared credential store accidentally.

## Safe rollback before apply

Before applying, no live file is modified. Delete an unwanted private dry-run plan artifact or simply create a new plan. After an interrupted apply, use `migrate-recover`; do not copy token files manually while Hermes processes are running.
