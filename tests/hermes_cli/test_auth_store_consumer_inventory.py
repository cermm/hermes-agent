from __future__ import annotations

import json
import os
from pathlib import Path


def _shared_profile(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "hermes"
    profile = root / "profiles" / "consumer"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "auth:\n  authority: shared\n", encoding="utf-8"
    )
    shared_auth = root / "auth.json"
    shared_auth.write_text('{"providers": {}}', encoding="utf-8")
    return root, profile, shared_auth


def test_auth_store_consumers_resolve_one_shared_authority(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "hermes"
    profile = root / "profiles" / "consumer"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "auth:\n  authority: shared\n", encoding="utf-8"
    )
    expected = root / "auth.json"
    expected.write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "tokens": {"access_token": "test-access-token"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))

    from agent.auxiliary_client import _auth_json_path as auxiliary_auth_path
    from hermes_cli.auth import _auth_file_path as cli_auth_path
    from plugins.platforms.photon.auth import _auth_json_path as photon_auth_path
    from tools.managed_tool_gateway import auth_json_path as managed_tool_auth_path
    from tools.xai_http import has_xai_credentials

    consumers = {
        "CLI authentication": cli_auth_path(),
        "auxiliary model client": auxiliary_auth_path(),
        "managed tool gateway": managed_tool_auth_path(),
        "Photon platform": photon_auth_path(),
    }
    assert consumers == {name: expected for name in consumers}
    assert has_xai_credentials() is True


def test_model_cache_fingerprint_reads_shared_authority(tmp_path, monkeypatch) -> None:
    root, profile, shared_auth = _shared_profile(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli.models import _credential_fingerprint

    first = _credential_fingerprint("nous")
    stat = shared_auth.stat()
    os.utime(shared_auth, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = _credential_fingerprint("nous")

    assert first != second


def test_setup_readiness_uses_canonical_authority(monkeypatch, tmp_path) -> None:
    root, profile, _shared_auth = _shared_profile(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls: list[Path] = []

    import hermes_cli.auth_authority as authority

    real_resolver = authority.get_auth_store_path

    def tracked_resolver() -> Path:
        path = real_resolver()
        calls.append(path)
        return path

    monkeypatch.setattr(authority, "get_auth_store_path", tracked_resolver)

    import hermes_cli.auth as auth

    monkeypatch.setattr(auth, "get_auth_status", lambda _provider: {"logged_in": False})

    from hermes_cli.main import _has_any_provider_configured

    _has_any_provider_configured()
    assert calls == [root / "auth.json"]


def test_gateway_startup_gate_resolves_authority(monkeypatch, tmp_path) -> None:
    _root, profile, _shared_auth = _shared_profile(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = 0

    import hermes_cli.auth_authority as authority

    real_resolver = authority.resolve_auth_authority

    def tracked_resolver(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_resolver(*args, **kwargs)

    monkeypatch.setattr(authority, "resolve_auth_authority", tracked_resolver)

    from gateway.run import _auth_migration_startup_ready

    assert _auth_migration_startup_ready() is True
    assert calls == 1


def test_profile_authority_keeps_consumer_paths_profile_local(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "hermes"
    profile = root / "profiles" / "consumer"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "auth:\n  authority: profile\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))

    from agent.auxiliary_client import _auth_json_path as auxiliary_auth_path
    from hermes_cli.auth import _auth_file_path as cli_auth_path
    from plugins.platforms.photon.auth import _auth_json_path as photon_auth_path
    from tools.managed_tool_gateway import auth_json_path as managed_tool_auth_path

    expected = profile / "auth.json"
    assert cli_auth_path() == expected
    assert auxiliary_auth_path() == expected
    assert managed_tool_auth_path() == expected
    assert photon_auth_path() == expected
