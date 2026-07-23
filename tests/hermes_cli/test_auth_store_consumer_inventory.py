from __future__ import annotations

import json
from pathlib import Path


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
