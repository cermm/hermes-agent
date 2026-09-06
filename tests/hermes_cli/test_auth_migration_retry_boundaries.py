"""Crash recovery preserves migration authority and refuses reuse of stale sources."""

import json

import pytest

from hermes_cli import auth_migration as migration
from hermes_cli.auth_authority import AuthAuthorityConfigError, resolve_auth_authority


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    profile = root / 'profiles' / 'worker'
    profile.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(root))
    (root / 'auth.json').write_text(json.dumps({'providers': {'nous': {'access_token': 'fake-root'}}}))
    (profile / 'auth.json').write_text(json.dumps({'providers': {'openai-codex': {'access_token': 'fake-local'}}}))
    (profile / 'config.yaml').write_text('auth:\n  authority: profile\n')
    original = {path: path.read_bytes() for path in (root / 'auth.json', profile / 'config.yaml')}
    plan = migration.plan_shared_migration(profile='worker')
    arguments = dict(plan_id=plan.plan_id, plan_digest=plan.plan_digest, conflict_policy='abort')
    migration.apply_shared_migration(**arguments)
    journal = migration._state_dir() / 'journals' / f'{plan.plan_id}.json'
    return root, profile, original, arguments, journal


@pytest.mark.parametrize('recover', [False, True])
@pytest.mark.parametrize('after_write', [False, True])
def test_interrupted_explicit_rollback_blocks_auth_and_resumes(migrated, monkeypatch, recover, after_write):
    root, profile, original, arguments, journal = migrated
    real_write = migration._private_bytes_write
    def interrupted(path, raw):
        if path == profile / 'config.yaml':
            if after_write:
                real_write(path, raw)
            raise OSError('injected config restoration failure')
        real_write(path, raw)
    with monkeypatch.context() as patch:
        patch.setattr(migration, '_private_bytes_write', interrupted)
        with pytest.raises(OSError, match='restoration failure'):
            migration.rollback_shared_migration(plan_id=arguments['plan_id'])
    assert json.loads(journal.read_text())['phase'] == 'rollback_pending'
    with pytest.raises(AuthAuthorityConfigError, match='incomplete migration'):
        resolve_auth_authority(profile_home=profile, shared_root=root)
    operation = migration.recover_shared_migration if recover else migration.rollback_shared_migration
    assert operation(plan_id=arguments['plan_id']) == 'rolled_back'
    assert all(path.read_bytes() == raw for path, raw in original.items())
    assert resolve_auth_authority(profile_home=profile, shared_root=root).effective_mode == 'profile'


def test_repeated_apply_preserves_committed_journal_and_backup(migrated):
    root, profile, original, arguments, journal = migrated
    before = journal.read_bytes()
    backups = {path: path.read_bytes() for path in migration._state_dir().joinpath('backups').rglob('*') if path.is_file()}
    with pytest.raises(migration.AuthMigrationError, match='already used'):
        migration.apply_shared_migration(**arguments)
    assert journal.read_bytes() == before
    assert all(path.read_bytes() == raw for path, raw in backups.items())
    assert migration.rollback_shared_migration(plan_id=arguments['plan_id']) == 'rolled_back'
    assert all(path.read_bytes() == raw for path, raw in original.items())


def test_retained_local_backup_cannot_reenter_migration(migrated):
    root, profile, original, arguments, journal = migrated
    target = root / 'auth.json'
    store = json.loads(target.read_text())
    store['providers']['openai-codex']['access_token'] = 'fake-refreshed'
    target.write_text(json.dumps(store))
    plan = migration.plan_shared_migration(all_profiles=True)
    assert plan.manifest['sources'] == []
    migration.apply_shared_migration(plan_id=plan.plan_id, plan_digest=plan.plan_digest, conflict_policy='prefer-profile')
    assert json.loads(target.read_text())['providers']['openai-codex']['access_token'] == 'fake-refreshed'
    with pytest.raises(migration.AuthMigrationError, match='already uses shared'):
        migration.plan_shared_migration(profile='worker')
    assert (profile / 'auth.json').exists(), 'rollback source must be preserved'


def test_partial_rollback_rejects_external_edits_without_overwriting(migrated, monkeypatch):
    root, profile, original, arguments, journal = migrated
    real_write = migration._private_bytes_write
    def interrupted(path, raw):
        if path == profile / 'config.yaml':
            raise OSError('injected restoration failure')
        real_write(path, raw)
    with monkeypatch.context() as patch:
        patch.setattr(migration, '_private_bytes_write', interrupted)
        with pytest.raises(OSError):
            migration.rollback_shared_migration(plan_id=arguments['plan_id'])
    target = root / 'auth.json'
    target.write_text('{"providers":{"nous":{"access_token":"fake-external-update"}}}')
    external = target.read_bytes()
    with pytest.raises(migration.AuthMigrationError, match='changed'):
        migration.recover_shared_migration(plan_id=arguments['plan_id'])
    assert target.read_bytes() == external
    with pytest.raises(AuthAuthorityConfigError, match='incomplete migration'):
        resolve_auth_authority(profile_home=profile, shared_root=root)


@pytest.mark.parametrize('terminal_write', [False, True])
def test_interrupted_automatic_recovery_resumes_restored_files(migrated, monkeypatch, terminal_write):
    root, profile, original, arguments, journal = migrated
    # Recreate the durable phase from an apply interrupted immediately before
    # its final commit marker. Target and every profile already contain the
    # recorded migration postconditions.
    record = json.loads(journal.read_text())
    record['phase'] = 'profiles_configured'
    migration._private_json_write(journal, record)
    real_write = migration._private_bytes_write
    real_journal_write = migration._private_json_write
    def interrupted(path, raw):
        if path == profile / 'config.yaml':
            raise OSError('interrupted automatic restore')
        real_write(path, raw)
    def interrupted_terminal(path, data):
        if path == journal and data.get('phase') == 'rolled_back':
            raise OSError('interrupted terminal journal write')
        real_journal_write(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(migration, '_private_json_write' if terminal_write else '_private_bytes_write',
                      interrupted_terminal if terminal_write else interrupted)
        with pytest.raises(OSError):
            migration.recover_shared_migration(plan_id=arguments['plan_id'])
    with pytest.raises(AuthAuthorityConfigError, match='incomplete migration'):
        resolve_auth_authority(profile_home=profile, shared_root=root)
    assert migration.recover_shared_migration(plan_id=arguments['plan_id']) == 'rolled_back'
    assert all(path.read_bytes() == raw for path, raw in original.items())
