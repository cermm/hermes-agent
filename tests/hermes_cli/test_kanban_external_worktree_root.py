"""Behavioral coverage for configured external Kanban worktree roots (#352)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "kanban@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Kanban Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
    )


def _set_remote_origin(
    repo: Path, remote: str = "git@github.com:acme/widgets.git"
) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", remote],
        check=True,
        capture_output=True,
        text=True,
    )


def test_configured_root_creates_namespaced_external_worktree(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _set_remote_origin(repo)
    worktree_root = tmp_path / "external-worktrees"
    monkeypatch.setattr(kb, "_configured_worktree_root", lambda: worktree_root)
    kb.create_board("external-wt", default_workdir=str(repo))

    with kb.connect(board="external-wt") as conn:
        tid = kb.create_task(
            conn,
            title="external",
            workspace_kind="worktree",
            workspace_path=str(repo),
            board="external-wt",
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        target = worktree_root / "acme-widgets" / tid
        assert task.workspace_path == str(target)
        assert not target.exists()
        resolved = kb.resolve_workspace(task, board="external-wt")

    assert resolved == target.resolve()
    assert (target / ".git").is_file()


@pytest.mark.parametrize(
    "unsafe_root", [Path("/tmp/worktrees"), Path("/var/tmp/worktrees")]
)
def test_configured_root_rejects_unsafe_locations(
    kanban_home, monkeypatch, unsafe_root
):
    from hermes_cli.config import load_config_readonly as real_load_config_readonly

    config = real_load_config_readonly()
    config.setdefault("kanban", {})["worktree_root"] = str(unsafe_root)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)

    with pytest.raises(ValueError, match="must not be under"):
        kb._configured_worktree_root()


@pytest.mark.parametrize(
    "unsafe_root", [Path("/mnt/c/worktrees"), Path("/mnt/d/worktrees")]
)
def test_configured_root_rejects_wsl_drive_mounts(
    kanban_home, monkeypatch, unsafe_root
):
    from hermes_cli.config import load_config_readonly as real_load_config_readonly

    config = real_load_config_readonly()
    config.setdefault("kanban", {})["worktree_root"] = str(unsafe_root)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr("hermes_constants.is_wsl", lambda: True)

    with pytest.raises(ValueError, match="WSL drive mount"):
        kb._configured_worktree_root()


def test_configured_root_allows_linux_mount_namespace_on_wsl(
    kanban_home, monkeypatch
):
    from hermes_cli.config import load_config_readonly as real_load_config_readonly

    safe_root = Path("/mnt/worktrees")
    config = real_load_config_readonly()
    config.setdefault("kanban", {})["worktree_root"] = str(safe_root)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr("hermes_constants.is_wsl", lambda: True)

    assert kb._configured_worktree_root() == safe_root


def test_create_persists_configured_path_before_dispatch(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _set_remote_origin(repo)
    worktree_root = tmp_path / "external-worktrees"
    monkeypatch.setattr(kb, "_configured_worktree_root", lambda: worktree_root)
    kb.create_board("persist-wt", default_workdir=str(repo))

    with kb.connect(board="persist-wt") as conn:
        tid = kb.create_task(
            conn,
            title="persist",
            workspace_kind="worktree",
            board="persist-wt",
        )
        task = kb.get_task(conn, tid)

    assert task is not None and task.workspace_path
    assert task.workspace_path == str(worktree_root / "acme-widgets" / tid)
    assert not Path(task.workspace_path).exists()


def test_cross_profile_project_child_materializes_without_project_lookup(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _set_remote_origin(repo)
    worktree_root = tmp_path / "external-worktrees"
    monkeypatch.setattr(kb, "_configured_worktree_root", lambda: worktree_root)

    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Widgets",
            folders=[str(repo)],
        )
        project = pdb.get_project(project_conn, project_id)
    assert project is not None

    with kb.connect() as conn:
        source_id = kb.create_task(
            conn,
            title="source",
            project_id=project.slug,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None and source.workspace_path
        assert source.worktree_source_path == str(repo)
        assert not Path(source.workspace_path).exists()

        monkeypatch.setattr(pdb, "get_project", lambda *_args, **_kwargs: None)
        child_id = kb.create_task(
            conn,
            title="child",
            project_id=source.project_id,
            project_source_task_id=source.id,
        )
        child = kb.get_task(conn, child_id)
        assert child is not None and child.workspace_path
        assert child.worktree_source_path == str(repo)
        assert not Path(child.workspace_path).exists()
        resolved = kb.resolve_workspace(child)

    assert child.project_id == source.project_id
    assert Path(child.workspace_path) == worktree_root / "acme-widgets" / child_id
    assert Path(child.workspace_path) != Path(source.workspace_path)
    assert resolved == Path(child.workspace_path)
    assert (resolved / ".git").is_file()


def test_idempotent_create_converges_pending_task_to_configured_root(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _set_remote_origin(repo)
    kb.create_board("idem-wt", default_workdir=str(repo))

    monkeypatch.setattr(kb, "_configured_worktree_root", lambda: None)
    with kb.connect(board="idem-wt") as conn:
        tid = kb.create_task(
            conn,
            title="legacy pending",
            workspace_kind="worktree",
            workspace_path=str(repo),
            idempotency_key="same-create",
            board="idem-wt",
        )
        legacy = kb.get_task(conn, tid)
        assert legacy is not None
        assert legacy.workspace_path == str(repo)

        worktree_root = tmp_path / "external-worktrees"
        monkeypatch.setattr(kb, "_configured_worktree_root", lambda: worktree_root)
        same_tid = kb.create_task(
            conn,
            title="ignored duplicate",
            workspace_kind="worktree",
            workspace_path=str(repo),
            idempotency_key="same-create",
            board="idem-wt",
        )
        converged = kb.get_task(conn, same_tid)

    assert same_tid == tid
    assert converged is not None
    assert converged.workspace_path == str(worktree_root / "acme-widgets" / tid)


@pytest.mark.parametrize("existing_kind", ["scratch", "dir"])
def test_idempotent_create_rejects_workspace_kind_change_to_worktree(
    kanban_home, tmp_path, monkeypatch, existing_kind
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _set_remote_origin(repo)
    original_path = repo if existing_kind == "dir" else None

    monkeypatch.setattr(kb, "_configured_worktree_root", lambda: None)
    with kb.connect(board="idem-kind-wt") as conn:
        tid = kb.create_task(
            conn,
            title=f"existing {existing_kind}",
            workspace_kind=existing_kind,
            workspace_path=str(original_path) if original_path else None,
            idempotency_key="same-create",
            board="idem-kind-wt",
        )

        worktree_root = tmp_path / "external-worktrees"
        target = worktree_root / "acme-widgets" / tid
        monkeypatch.setattr(kb, "_configured_worktree_root", lambda: worktree_root)
        with pytest.raises(ValueError, match="workspace_kind"):
            kb.create_task(
                conn,
                title="incompatible worktree retry",
                workspace_kind="worktree",
                workspace_path=str(repo),
                idempotency_key="same-create",
                board="idem-kind-wt",
            )
        unchanged = kb.get_task(conn, tid)

    assert unchanged is not None
    assert unchanged.workspace_kind == existing_kind
    assert unchanged.workspace_path == (str(original_path) if original_path else None)
    assert not target.exists()


def test_configured_root_rejects_root_inside_repo(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _set_remote_origin(repo)
    monkeypatch.setattr(kb, "_configured_worktree_root", lambda: repo / "nested-root")
    kb.create_board("unsafe-wt", default_workdir=str(repo))

    with kb.connect(board="unsafe-wt") as conn:
        with pytest.raises(ValueError, match="outside every git repository"):
            kb.create_task(
                conn,
                title="unsafe",
                workspace_kind="worktree",
                workspace_path=str(repo),
                board="unsafe-wt",
            )


def test_configured_root_rejects_repo_without_remote(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(
        kb, "_configured_worktree_root", lambda: tmp_path / "external-worktrees"
    )
    kb.create_board("no-remote", default_workdir=str(repo))

    with kb.connect(board="no-remote") as conn:
        with pytest.raises(ValueError, match="remote.origin.url"):
            kb.create_task(
                conn,
                title="no remote",
                workspace_kind="worktree",
                workspace_path=str(repo),
                board="no-remote",
            )


def test_configured_root_rejects_existing_non_worktree(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _set_remote_origin(repo)
    worktree_root = tmp_path / "external-worktrees"
    monkeypatch.setattr(kb, "_configured_worktree_root", lambda: worktree_root)
    kb.create_board("occupied-wt", default_workdir=str(repo))

    with kb.connect(board="occupied-wt") as conn:
        tid = kb.create_task(
            conn,
            title="occupied",
            workspace_kind="worktree",
            workspace_path=str(repo),
            board="occupied-wt",
        )
        task = kb.get_task(conn, tid)
        assert task is not None and task.workspace_path
        Path(task.workspace_path).mkdir(parents=True)

        with pytest.raises(ValueError, match="non-worktree directory"):
            kb.resolve_workspace(task, board="occupied-wt")


def test_configured_root_rejects_existing_wrong_branch(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _set_remote_origin(repo)
    worktree_root = tmp_path / "external-worktrees"
    monkeypatch.setattr(kb, "_configured_worktree_root", lambda: worktree_root)
    kb.create_board("wrong-branch", default_workdir=str(repo))

    with kb.connect(board="wrong-branch") as conn:
        tid = kb.create_task(
            conn,
            title="wrong branch",
            workspace_kind="worktree",
            workspace_path=str(repo),
            board="wrong-branch",
        )
        task = kb.get_task(conn, tid)
        assert task is not None and task.workspace_path
        target = Path(task.workspace_path)
        target.parent.mkdir(parents=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "-b",
                "wrong/branch",
                str(target),
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        with pytest.raises(ValueError, match="does not match expected"):
            kb.resolve_workspace(task, board="wrong-branch")


def test_unset_root_preserves_legacy_repo_local_layout(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(kb, "_configured_worktree_root", lambda: None)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="legacy",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.workspace_path == str(repo)
        resolved = kb.resolve_workspace(task)

    assert resolved == repo / ".worktrees" / tid


def test_configured_root_rejects_nested_namespace_target(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _set_remote_origin(repo)
    worktree_root = tmp_path / "external-worktrees"
    monkeypatch.setattr(kb, "_configured_worktree_root", lambda: worktree_root)
    kb.create_board("nested-wt", default_workdir=str(repo))

    with kb.connect(board="nested-wt") as conn:
        with pytest.raises(ValueError, match="direct child"):
            kb.create_task(
                conn,
                title="nested",
                workspace_kind="worktree",
                workspace_path=str(worktree_root / "acme-widgets" / "nested" / "task"),
                board="nested-wt",
            )


def test_configured_root_rejects_relative_explicit_target(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _set_remote_origin(repo)
    monkeypatch.setattr(
        kb, "_configured_worktree_root", lambda: tmp_path / "external-worktrees"
    )
    kb.create_board("relative-wt", default_workdir=str(repo))

    with kb.connect(board="relative-wt") as conn:
        with pytest.raises(ValueError, match="must be absolute"):
            kb.create_task(
                conn,
                title="relative",
                workspace_kind="worktree",
                workspace_path="relative/path",
                board="relative-wt",
            )
