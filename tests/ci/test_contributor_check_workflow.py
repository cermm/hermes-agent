"""Executable regression coverage for the contributor attribution workflow."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "contributor-check.yml"
GITHUB_NOREPLY = "12345+test-user@users.noreply.github.com"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, email: str) -> str:
    marker = repo / "history.txt"
    previous = marker.read_text(encoding="utf-8") if marker.exists() else ""
    marker.write_text(f"{previous}{message}\n", encoding="utf-8")
    _git(repo, "add", "history.txt")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Contributor",
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "Contributor",
        "GIT_COMMITTER_EMAIL": email,
    }
    _git(repo, "commit", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "candidate")
    (repo / "contributors" / "emails").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "release.py").write_text("AUTHOR_MAP = {}\n", encoding="utf-8")
    root = _commit(repo, "root", GITHUB_NOREPLY)
    return repo, root


def _workflow_step() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["check-attribution"]["steps"][1]


def _run_check(
    repo: Path, tmp_path: Path, *, event_name: str, base_ref: str
) -> subprocess.CompletedProcess[str]:
    step = _workflow_step()
    output = tmp_path / "github-output"
    env = {
        **os.environ,
        "BASE_REF": base_ref,
        "EVENT_NAME": event_name,
        "GITHUB_OUTPUT": str(output),
    }
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_custom_base_pr_checks_only_commits_after_the_fetched_base(
    tmp_path: Path,
) -> None:
    repo, root = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/main", root)
    custom_base = _commit(
        repo, "upstream work before the PR", "unmapped-upstream@example.com"
    )
    _git(repo, "update-ref", "refs/remotes/origin/release", custom_base)
    _commit(repo, "candidate change", GITHUB_NOREPLY)

    result = _run_check(repo, tmp_path, event_name="pull_request", base_ref="release")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "All contributor emails are mapped" in result.stdout


def test_main_pr_still_checks_against_origin_main(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    main = _commit(repo, "main base", "unmapped-before-pr@example.com")
    _git(repo, "update-ref", "refs/remotes/origin/main", main)
    _commit(repo, "candidate change", GITHUB_NOREPLY)

    result = _run_check(repo, tmp_path, event_name="pull_request", base_ref="main")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "All contributor emails are mapped" in result.stdout


def test_main_push_preserves_the_origin_main_merge_base_range(tmp_path: Path) -> None:
    repo, root = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/main", root)
    _commit(repo, "older main history", "unmapped-history@example.com")
    _commit(repo, "pushed commit", GITHUB_NOREPLY)

    result = _run_check(repo, tmp_path, event_name="push", base_ref="")

    assert result.returncode != 0
    assert "unmapped-history@example.com" in result.stdout


def test_main_push_with_origin_main_at_parent_checks_the_new_commit(
    tmp_path: Path,
) -> None:
    repo, _ = _repo(tmp_path)
    previous_main = _commit(repo, "previous main", "unmapped-history@example.com")
    _git(repo, "update-ref", "refs/remotes/origin/main", previous_main)
    _commit(repo, "pushed commit", GITHUB_NOREPLY)

    result = _run_check(repo, tmp_path, event_name="push", base_ref="")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "All contributor emails are mapped" in result.stdout


def test_pull_request_without_a_base_ref_fails_closed(tmp_path: Path) -> None:
    repo, root = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/main", root)
    _commit(repo, "candidate change", GITHUB_NOREPLY)

    result = _run_check(repo, tmp_path, event_name="pull_request", base_ref="")

    assert result.returncode != 0
    assert "pull_request event did not provide a base ref" in result.stdout


def test_pull_request_with_an_unfetched_base_ref_fails_closed(tmp_path: Path) -> None:
    repo, root = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/main", root)
    _commit(repo, "candidate change", GITHUB_NOREPLY)

    result = _run_check(repo, tmp_path, event_name="pull_request", base_ref="missing")

    assert result.returncode != 0


def test_base_ref_is_not_reinterpreted_by_the_shell(tmp_path: Path) -> None:
    repo, root = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/main", root)
    _commit(repo, "candidate change", GITHUB_NOREPLY)
    marker = tmp_path / "injected"
    base_ref = f"$(touch${{IFS}}{marker})"

    result = _run_check(repo, tmp_path, event_name="pull_request", base_ref=base_ref)

    assert result.returncode != 0
    assert not marker.exists()


def test_base_ref_is_bound_through_the_step_environment() -> None:
    step = _workflow_step()

    assert step["env"]["BASE_REF"] == "${{ github.base_ref }}"
    assert step["env"]["EVENT_NAME"] == "${{ github.event_name }}"
