from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.auth import _load_auth_store


@pytest.mark.parametrize(
    "payload",
    [
        {"providers": [], "credential_pool": {}},
        {"providers": {}, "credential_pool": []},
    ],
)
def test_load_auth_store_quarantines_non_mapping_sections(
    tmp_path: Path,
    payload: dict,
) -> None:
    auth_path = tmp_path / "auth.json"
    raw = json.dumps(payload).encode("utf-8")
    auth_path.write_bytes(raw)

    loaded = _load_auth_store(auth_path)

    assert loaded["providers"] == {}
    assert not loaded.get("credential_pool")
    assert auth_path.read_bytes() == raw
    assert auth_path.with_suffix(".json.corrupt").read_bytes() == raw