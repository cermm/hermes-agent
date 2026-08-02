from __future__ import annotations

import importlib
import json
from pathlib import Path


def test_photon_save_preserves_concurrent_unrelated_pool_update(
    tmp_path: Path, monkeypatch,
) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "providers": {},
                "credential_pool": {
                    "photon": [{"access_token": "old-photon"}],
                    "nous": [{"access_token": "old-nous"}],
                },
            }
        ),
        encoding="utf-8",
    )
    module = importlib.import_module("plugins.platforms.photon.auth")
    monkeypatch.setattr(module, "_auth_json_path", lambda: auth_path)

    stale = module._load_auth()
    stale["credential_pool"]["photon"] = [{"access_token": "new-photon"}]

    current = json.loads(auth_path.read_text(encoding="utf-8"))
    current["credential_pool"]["nous"] = [{"access_token": "rotated-nous"}]
    auth_path.write_text(json.dumps(current), encoding="utf-8")

    module._save_auth(stale)

    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    assert saved["credential_pool"]["photon"][0]["access_token"] == "new-photon"
    assert saved["credential_pool"]["nous"][0]["access_token"] == "rotated-nous"
