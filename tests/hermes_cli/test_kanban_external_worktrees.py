"""External worktree placement, repository identity and publication boundaries."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import projects_db as pdb

pytestmark = pytest.mark.linux_only


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repository(path):
    path.mkdir()
    git(path, 'init', '-b', 'main')
    git(path, 'config', 'user.email', 'fixture@example.invalid')
    git(path, 'config', 'user.name', 'Fixture')
    git(path, 'config', 'commit.gpgsign', 'false')
    (path / 'README.md').write_text('original source\n')
    git(path, 'add', 'README.md')
    git(path, 'commit', '-m', 'initial fixture')
    git(path, 'remote', 'add', 'origin', 'git@github.com:acme/widgets.git')
    return path


@pytest.fixture
def external_board(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    source = repository(tmp_path / 'source')
    # /tmp and the Windows mount are deliberately forbidden policy roots.
    # Use another disposable Linux directory to exercise the real config loader.
    with tempfile.TemporaryDirectory(prefix='hermes-worktree-policy-', dir='/var/tmp') as raw:
        root = Path(raw)
        config = home / 'config.yaml'
        config.write_text(json.dumps({'kanban': {'worktree_root': str(root)}}))
        kb.create_board('external', default_workdir=str(source))
        with kbc.connect(board='external') as conn:
            yield conn, source, root, config


def create(conn, **kwargs):
    return kb.create_task(conn, title='fixture', workspace_kind='worktree', board='external', **kwargs)


def test_external_worktree_publication_reuse_and_sibling_isolation(external_board, monkeypatch):
    conn, source, root, _ = external_board
    tid = create(conn, idempotency_key='same-task', assignee='default')
    expected = root / 'acme-widgets' / tid
    task = kb.get_task(conn, tid)
    assert task.workspace_path == str(expected)
    assert not expected.exists()
    assert create(conn, idempotency_key='same-task') == tid
    workspace, branch = kbw._resolve_worktree_workspace(task, board='external')
    assert workspace == expected and branch == f'wt/{tid}'
    assert kbw._resolve_worktree_workspace(task, board='external') == (workspace, branch)
    assert git(source, 'worktree', 'list', '--porcelain').count(f'worktree {expected}\n') == 1
    assert not (source / '.worktrees').exists()
    child = create(conn, workspace_path=str(expected))
    child_workspace, child_branch = kbw._resolve_worktree_workspace(kb.get_task(conn, child), board='external')
    assert child_workspace == root / 'acme-widgets' / child
    assert child_branch == f'wt/{child}'
    assert git(expected, 'branch', '--show-current') == branch
    spawned = []
    monkeypatch.setattr(kbd, '_memory_pressure_level', lambda: 'ok')
    result = kbd.dispatch_once(conn, board='external', max_spawn=1, reconcile_orphans=False,
        spawn_fn=lambda task, path, board=None: spawned.append((task.id, path, board)))
    assert spawned == [(tid, str(expected), 'external')]
    assert result.spawned == [(tid, 'default', str(expected))]
    dispatched = kb.get_task(conn, tid)
    assert dispatched.claim_lock and dispatched.branch_name == branch
    with kbc.connect(board='external') as peer:
        assert kb.claim_task(peer, tid) is None


@pytest.mark.parametrize('escape', ['repo_local', 'lookalike', 'nested', 'traversal', 'target_symlink', 'namespace_symlink'])
def test_external_policy_rejects_escaped_targets_without_publication(external_board, tmp_path, escape):
    conn, source, root, _ = external_board
    namespace = root / 'acme-widgets'
    outside = tmp_path / 'outside'
    outside.mkdir()
    paths = {
        'repo_local': source / '.worktrees/card',
        'lookalike': Path(str(root) + '-other') / 'acme-widgets/card',
        'nested': namespace / 'nested/card',
        'traversal': namespace / '../escape',
        'target_symlink': namespace / 'escape',
        'namespace_symlink': namespace / 'card',
    }
    if escape == 'namespace_symlink':
        namespace.symlink_to(outside, target_is_directory=True)
    elif escape == 'target_symlink':
        namespace.mkdir()
        paths[escape].symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='namespace|direct child'):
        create(conn, workspace_path=str(paths[escape]))
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert list(outside.iterdir()) == []
    assert not (source / '.worktrees').exists()


@pytest.mark.parametrize('invalid', ['relative', 'temporary', 'windows_mount', 'inside_source', 'inside_other_repo'])
def test_invalid_policy_root_is_rejected_before_task_creation(external_board, tmp_path, invalid):
    conn, source, _, config = external_board
    other = repository(tmp_path / 'other')
    roots = {'relative': 'relative/worktrees', 'temporary': '/tmp/worktrees',
             'windows_mount': '/mnt/c/worktrees', 'inside_source': str(source / 'worktrees'),
             'inside_other_repo': str(other / 'worktrees')}
    config.write_text(json.dumps({'kanban': {'worktree_root': roots[invalid]}}))
    with pytest.raises(ValueError, match='worktree_root'):
        create(conn)
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_existing_same_remote_clone_cannot_supply_source_identity(external_board, tmp_path, monkeypatch):
    conn, source, root, _ = external_board
    tid = create(conn)
    task = kb.get_task(conn, tid)
    target = root / 'acme-widgets' / tid
    wrong = repository(tmp_path / 'other-clone')
    target.parent.mkdir()
    git(wrong, 'worktree', 'add', '-b', f'wt/{tid}', str(target), 'HEAD')
    original = git(target, 'rev-parse', '--git-common-dir')
    with pytest.raises(ValueError, match='different git repository'):
        kbw.resolve_workspace(task, board='external')
    assert git(target, 'rev-parse', '--git-common-dir') == original
    assert kb.get_task(conn, tid).workspace_path == task.workspace_path
    assert not (source / '.worktrees').exists()
    kb.assign_task(conn, tid, 'default')
    spawned = []
    monkeypatch.setattr(kbd, '_memory_pressure_level', lambda: 'ok')
    result = kbd.dispatch_once(conn, board='external', max_spawn=1, failure_limit=1, reconcile_orphans=False,
        spawn_fn=lambda *args, **kwargs: spawned.append(args))
    assert not spawned and not result.spawned
    assert result.auto_blocked == [tid]
    assert kb.get_task(conn, tid).claim_lock is None


def test_idempotent_policy_convergence_cannot_overwrite_a_new_claim(external_board, monkeypatch):
    conn, source, root, config = external_board
    config.write_text('{}')
    tid = create(conn, idempotency_key='legacy-card')
    assert kb.get_task(conn, tid).workspace_path == str(source)
    config.write_text(json.dumps({'kanban': {'worktree_root': str(root)}}))
    original = kbw._configured_create_worktree_target

    def claim_after_planning(**kwargs):
        target = original(**kwargs)
        with kbc.connect(board='external') as peer:
            assert kb.claim_task(peer, tid) is not None
        return target

    with monkeypatch.context() as race:
        race.setattr(kbw, '_configured_create_worktree_target', claim_after_planning)
        with pytest.raises(RuntimeError, match='changed|claimed'):
            create(conn, idempotency_key='legacy-card')
    current = kb.get_task(conn, tid)
    assert current.claim_lock and current.workspace_path == str(source)
    assert not (root / 'acme-widgets').exists()
    # A separate unclaimed legacy task still converges without creating files.
    config.write_text('{}')
    other = create(conn, idempotency_key='other-card')
    config.write_text(json.dumps({'kanban': {'worktree_root': str(root)}}))
    assert create(conn, idempotency_key='other-card') == other
    assert kb.get_task(conn, other).workspace_path == str(root / 'acme-widgets' / other)


def test_materialization_rechecks_branch_before_dispatch(external_board, monkeypatch):
    conn, source, _, _ = external_board
    tid = create(conn, assignee='default')
    target = Path(kb.get_task(conn, tid).workspace_path)
    ensure = kbw._ensure_git_worktree

    def occupy_before_git_add(repo, destination, branch):
        destination.parent.mkdir(parents=True, exist_ok=True)
        git(repo, 'worktree', 'add', '-b', 'other-worker', str(destination), 'HEAD')
        # This is the real existing-target fast path after a concurrent writer
        # occupied the destination between planning and materialization.
        ensure(repo, destination, branch)

    monkeypatch.setattr(kbw, '_ensure_git_worktree', occupy_before_git_add)
    monkeypatch.setattr(kbd, '_memory_pressure_level', lambda: 'ok')
    spawned = []
    result = kbd.dispatch_once(conn, board='external', max_spawn=1, failure_limit=1, reconcile_orphans=False,
        spawn_fn=lambda *args, **kwargs: spawned.append(args))
    assert not spawned and result.auto_blocked == [tid]
    assert git(target, 'branch', '--show-current') == 'other-worker'


@pytest.mark.parametrize('path_kind', ['missing', 'not_git', 'different_valid_repo'])
def test_explicit_project_authority_cannot_fall_back_to_board(external_board, tmp_path, path_kind):
    conn, board_source, _, _ = external_board
    primary = tmp_path / 'project-primary'
    if path_kind == 'not_git':
        primary.mkdir()
    elif path_kind == 'different_valid_repo':
        repository(primary)
    with pdb.connect_closing() as projects:
        project_id = pdb.create_project(projects, name='Explicit Project', primary_path=str(primary))
    if path_kind == 'different_valid_repo':
        tid = create(conn, project_id=project_id)
        target = kbw.resolve_workspace(kb.get_task(conn, tid), board='external')
        assert git(target, 'rev-parse', '--git-common-dir') == str(primary / '.git')
        assert git(target, 'rev-parse', '--git-common-dir') != str(board_source / '.git')
        return
    with pytest.raises(ValueError, match='project.*repository'):
        create(conn, project_id=project_id)
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


@pytest.mark.parametrize('source_state', ['valid', 'wrong_clone', 'wrong_branch', 'unmaterialized'])
def test_cross_profile_project_link_requires_real_source_evidence(external_board, tmp_path, monkeypatch, source_state):
    conn, source, root, config = external_board
    with pdb.connect_closing() as projects:
        project_id = pdb.create_project(projects, name='Wire Project', primary_path=str(source))
        project = pdb.get_project(projects, project_id)
    source_id = create(conn, project_id=project_id)
    source_task = kb.get_task(conn, source_id)
    source_path = Path(source_task.workspace_path)
    if source_state == 'valid':
        kbw.resolve_workspace(source_task, board='external')
    elif source_state in ('wrong_clone', 'wrong_branch'):
        actual_repo = repository(tmp_path / 'wrong') if source_state == 'wrong_clone' else source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        branch = 'unrelated-branch' if source_state == 'wrong_branch' else source_task.branch_name
        git(actual_repo, 'worktree', 'add', '-b', branch, str(source_path), 'HEAD')
    worker = tmp_path / 'worker-profile'
    worker.mkdir()
    (worker / 'config.yaml').write_bytes(config.read_bytes())
    monkeypatch.setenv('HERMES_HOME', str(worker))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(config.parent))
    with pdb.connect_closing() as projects:
        assert pdb.get_project(projects, project_id) is None
    kwargs = dict(title='Follow up', project_id=project_id,
                  project_source_task_id=source_id, board='external')
    if source_state != 'valid':
        with pytest.raises(ValueError, match='source|branch|materialized'):
            kb.create_task(conn, **kwargs)
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
        return
    child_id = kb.create_task(conn, **kwargs)
    child = kb.get_task(conn, child_id)
    assert child.project_id == project_id and child.workspace_kind == 'worktree'
    assert child.branch_name == f'{project.slug}/{child_id}-follow-up'
    assert child.workspace_path == str(root / 'acme-widgets' / child_id)
    assert kbw.resolve_workspace(child, board='external') != source_path
    assert git(Path(child.workspace_path), 'rev-parse', '--git-common-dir') == git(source_path, 'rev-parse', '--git-common-dir')
