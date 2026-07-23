from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "nix_auth_authority.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("nix_auth_authority", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_worker(home: str, source: str, start, results) -> None:
    module = _load_module()
    start.wait()
    results.put(module.seed_auth(Path(home), Path(source), force=False))


def test_seed_resolves_shared_authority_and_preserves_existing_destination(
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("auth:\n  authority: shared\n")
    destination = root / "auth.json"
    destination.write_text(json.dumps({"token": "current"}))
    source = tmp_path / "seed.json"
    source.write_text(json.dumps({"token": "seed"}))

    result = module.seed_auth(profile, source, force=False)

    assert result["status"] == "preserved"
    assert json.loads(destination.read_text()) == {"token": "current"}


def test_legacy_force_seed_cannot_replace_runtime_credentials(tmp_path: Path) -> None:
    module = _load_module()
    home = tmp_path / ".hermes"
    home.mkdir()
    destination = home / "auth.json"
    destination.write_text("old")
    source = tmp_path / "seed.json"
    source.write_text("new")

    result = module.seed_auth(home, source, force=True)

    assert result["status"] == "preserved"
    assert destination.read_text() == "old"


def test_concurrent_non_force_seed_has_one_writer_and_no_partial_json(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    sources = []
    for index in range(2):
        source = tmp_path / f"seed-{index}.json"
        source.write_text(json.dumps({"writer": index, "payload": "x" * 10000}))
        sources.append(source)

    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_seed_worker,
            args=(str(home), str(source), start, results),
        )
        for source in sources
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    statuses = sorted(results.get(timeout=2)["status"] for _ in workers)
    assert statuses == ["created", "preserved"]
    assert json.loads((home / "auth.json").read_text())["writer"] in {0, 1}
    assert not list(home.glob("auth.json.tmp.*"))


def test_seed_rejects_source_symlink(tmp_path: Path) -> None:
    module = _load_module()
    profile = tmp_path / ".hermes"
    profile.mkdir()
    actual = tmp_path / "seed.json"
    actual.write_text("{}", encoding="utf-8")
    linked = tmp_path / "linked.json"
    linked.symlink_to(actual)

    with pytest.raises(RuntimeError, match="source.*symlink"):
        module.seed_auth(profile, linked, force=False)


def test_seed_rejects_non_object_json(tmp_path: Path) -> None:
    module = _load_module()
    profile = tmp_path / ".hermes"
    seed = tmp_path / "seed.json"
    seed.write_text("[]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="JSON object"):
        module.seed_auth(profile, seed, force=False)
