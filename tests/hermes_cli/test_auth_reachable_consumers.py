"""Reachable auth consumer regressions for shared-auth authority (#380)."""
from __future__ import annotations

import importlib
import json
from pathlib import Path


def _profile_home(tmp_path, monkeypatch, name: str = "builder-high"):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    profile = root / "profiles" / name
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return root, profile


def test_photon_auth_uses_shared_authority_for_profile_writes(tmp_path, monkeypatch):
    root, profile = _profile_home(tmp_path, monkeypatch)

    photon_auth = importlib.import_module("plugins.platforms.photon.auth")

    assert photon_auth._auth_json_path() == root / "auth.json"
    photon_auth.store_photon_token("photon-test-token")

    assert (root / "auth.json").is_file()
    assert not (profile / "auth.json").exists()
    data = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    assert data["credential_pool"]["photon"][0]["access_token"] == "photon-test-token"


def test_managed_gateway_peek_reads_shared_authority_in_profile(tmp_path, monkeypatch):
    root, profile = _profile_home(tmp_path, monkeypatch)
    (root / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {"nous": {"access_token": "nous-test-token"}}}),
        encoding="utf-8",
    )

    managed = importlib.import_module("tools.managed_tool_gateway")

    assert managed.auth_json_path() == root / "auth.json"
    assert managed.peek_nous_access_token() == "nous-test-token"
    assert not (profile / "auth.json").exists()


def test_xai_availability_probe_reads_shared_authority_in_profile(tmp_path, monkeypatch):
    root, profile = _profile_home(tmp_path, monkeypatch)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    (root / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {
                    "xai-oauth": {
                        "tokens": {"access_token": "xai-test-token"}
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    xai_http = importlib.import_module("tools.xai_http")

    assert xai_http.has_xai_credentials() is True
    assert not (profile / "auth.json").exists()


def test_managed_gateway_authority_failure_does_not_fall_back_to_profile_local_auth(tmp_path, monkeypatch):
    import hermes_cli.auth_authority as auth_authority

    _root, profile = _profile_home(tmp_path, monkeypatch)
    (profile / "auth.json").write_text(
        json.dumps({"providers": {"nous": {"access_token": "profile-local-token"}}}),
        encoding="utf-8",
    )
    managed = importlib.import_module("tools.managed_tool_gateway")

    def blocked_authority():
        raise auth_authority.AuthAuthorityConfigError("incomplete migration")

    monkeypatch.setattr(auth_authority, "resolve_auth_authority", blocked_authority)
    monkeypatch.setattr(managed, "managed_nous_tools_enabled", lambda: True)

    assert managed.peek_nous_access_token() is None
    assert managed.is_managed_tool_gateway_ready("modal") is False


def test_photon_authority_failure_does_not_fall_back_to_profile_local_auth(tmp_path, monkeypatch):
    import hermes_cli.auth_authority as auth_authority

    _root, profile = _profile_home(tmp_path, monkeypatch)
    (profile / "auth.json").write_text(
        json.dumps({"credential_pool": {"photon": [{"access_token": "profile-local-token"}]}}),
        encoding="utf-8",
    )
    photon_auth = importlib.import_module("plugins.platforms.photon.auth")

    def blocked_authority():
        raise auth_authority.AuthAuthorityConfigError("incomplete migration")

    monkeypatch.setattr(auth_authority, "resolve_auth_authority", blocked_authority)

    import pytest

    with pytest.raises(auth_authority.AuthAuthorityConfigError):
        photon_auth._auth_json_path()


def test_auxiliary_nous_auth_reads_shared_singleton_in_profile(tmp_path, monkeypatch):
    root, profile = _profile_home(tmp_path, monkeypatch)
    (root / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {"nous": {"access_token": "nous-test-token"}}}),
        encoding="utf-8",
    )
    auxiliary = importlib.import_module("agent.auxiliary_client")

    provider = auxiliary._read_nous_auth()

    assert provider is not None
    assert provider["access_token"] == "nous-test-token"
    assert not (profile / "auth.json").exists()


def test_main_provider_configured_reads_shared_nous_in_profile(tmp_path, monkeypatch):
    root, profile = _profile_home(tmp_path, monkeypatch)
    (root / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {"nous": {"access_token": "nous-test-token"}}}),
        encoding="utf-8",
    )
    main = importlib.import_module("hermes_cli.main")
    import hermes_cli.auth as auth
    import hermes_cli.config as config

    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(auth, "PROVIDER_REGISTRY", {})
    monkeypatch.setattr(config, "DEFAULT_CONFIG", {"model": "default-model"})
    monkeypatch.setattr(config, "load_config", lambda: {"model": "default-model"})
    monkeypatch.setattr(auth, "get_auth_status", lambda provider_id: {"logged_in": provider_id == "nous"})

    assert main._has_any_provider_configured() is True
    assert not (profile / "auth.json").exists()
