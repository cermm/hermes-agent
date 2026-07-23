from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_auth_store_consumers.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_auth_store_consumers", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _inventory(path: Path, consumers: dict[str, str]) -> Path:
    inventory = path / "inventory.json"
    inventory.write_text(
        json.dumps({"version": 1, "consumers": consumers}), encoding="utf-8"
    )
    return inventory


def test_audit_rejects_unclassified_python_path_construction(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "new_consumer.py"
    source.write_text(
        'from pathlib import Path\nAUTH = Path.home() / ".hermes" / "auth.json"\n',
        encoding="utf-8",
    )

    unclassified, stale = module.audit(tmp_path, _inventory(tmp_path, {}))

    assert [(item.path, item.line, item.kind) for item in unclassified] == [
        ("new_consumer.py", 2, "path_division")
    ]
    assert stale == []


def test_audit_accepts_reviewed_category_and_rejects_stale_entries(
    tmp_path: Path,
) -> None:
    module = _load_module()
    source = tmp_path / "adapter.py"
    source.write_text(
        'from pathlib import Path\nAUTH = Path("root").joinpath("auth.json")\n',
        encoding="utf-8",
    )
    inventory = _inventory(
        tmp_path,
        {
            "adapter.py": "whole_store_deployment_adapter",
            "removed.py": "canonical_authority_owner",
        },
    )

    unclassified, stale = module.audit(tmp_path, inventory)

    assert unclassified == []
    assert stale == ["removed.py"]


def test_scan_ignores_tests_but_covers_non_python_deployment_adapters(
    tmp_path: Path,
) -> None:
    module = _load_module()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fixture.py").write_text(
        'AUTH = Path("root") / "auth.json"\n', encoding="utf-8"
    )
    hook = tmp_path / "stage2-hook.sh"
    hook.write_text('target="$HERMES_HOME/auth.json"\n', encoding="utf-8")

    findings = module.scan_repository(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("stage2-hook.sh", 1, "text_reference")
    ]
