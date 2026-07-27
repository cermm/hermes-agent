from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tool_search_livetest.py"
_SPEC = importlib.util.spec_from_file_location("tool_search_livetest_auth", _SCRIPT)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def test_isolated_home_refuses_credential_copy_without_explicit_opt_in(
    tmp_path: Path, monkeypatch,
) -> None:
    source_home = tmp_path / "source"
    source_home.mkdir()
    source_auth = source_home / "auth.json"
    source_auth.write_text(json.dumps({"providers": {"nous": {"token": "secret"}}}))
    (source_home / ".env").write_text("SECRET=value\n", encoding="utf-8")
    monkeypatch.setattr(_MOD, "ORIGINAL_AUTH", source_auth)
    monkeypatch.setattr(Path, "home", lambda: source_home.parent)
    monkeypatch.delenv("HERMES_TOOL_SEARCH_LIVETEST_ALLOW_CREDENTIAL_COPY", raising=False)

    isolated = _MOD.setup_isolated_home(enabled=True)

    assert not (isolated / "auth.json").exists()
    assert not (isolated / ".env").exists()
    config = (isolated / "config.yaml").read_text(encoding="utf-8")
    assert "authority: profile" in config


def test_isolated_home_opt_in_copies_credentials_privately(
    tmp_path: Path, monkeypatch,
) -> None:
    real_home = tmp_path / "real"
    source_root = real_home / ".hermes"
    source_root.mkdir(parents=True)
    source_auth = source_root / "auth.json"
    source_auth.write_text(json.dumps({"providers": {"nous": {"token": "secret"}}}))
    (source_root / ".env").write_text("SECRET=value\n", encoding="utf-8")
    monkeypatch.setattr(_MOD, "ORIGINAL_AUTH", source_auth)
    monkeypatch.setattr(Path, "home", lambda: real_home)
    monkeypatch.setenv("HERMES_TOOL_SEARCH_LIVETEST_ALLOW_CREDENTIAL_COPY", "1")

    isolated = _MOD.setup_isolated_home(enabled=False)

    for copied in (isolated / "auth.json", isolated / ".env"):
        assert copied.is_file()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
