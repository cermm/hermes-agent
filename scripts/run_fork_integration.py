"""Validate an exact affected-test manifest, then use the canonical runner."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def load_manifest(root: Path, path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = manifest["test_files"]
    if not files or len(set(files)) != len(files):
        raise ValueError("The test manifest must be nonempty and contain no duplicates")
    for name in files:
        target = root / name
        if (not name.startswith("tests/") or not name.endswith(".py")
                or target.resolve().is_relative_to(root) is False
                or not target.is_file() or not target.stat().st_size):
            raise ValueError(f"Missing, empty or out-of-tree test file: {name}")
    for nodeid in manifest["expected_linux_skips"]:
        if nodeid.split("::", 1)[0] not in files:
            raise ValueError(f"Expected skip outside manifest: {nodeid}")
    return manifest


def audit_results(manifest: dict, directory: Path) -> list[str]:
    failures = []
    expected = set(manifest["expected_linux_skips"])
    observed_expected = set()
    for name in manifest["test_files"]:
        path = directory / (name.replace("/", "__") + ".json")
        if not path.is_file():
            failures.append(f"No collection receipt: {name}")
            continue
        audit = json.loads(path.read_text(encoding="utf-8"))
        if audit["file"] != name or audit["platform"] != "linux":
            failures.append(f"Receipt identity/platform mismatch: {name}")
        if not audit["collected"] or audit["collection_skips"]:
            failures.append(f"Empty or skipped collection: {name}")
        if audit["exitstatus"]:
            failures.append(f"Pytest exited {audit['exitstatus']}: {name}")
        for skip in audit["skipped"]:
            nodeid = skip["nodeid"]
            if (nodeid not in expected or nodeid not in audit["windows_only"]
                    or skip["when"] != "setup" or skip["xfail"]):
                failures.append(f"Unexpected skip: {nodeid}: {skip['reason']}")
            else:
                observed_expected.add(nodeid)
        if not audit["passed"] and not set(audit["collected"]).issubset(observed_expected):
            failures.append(f"No tests actually passed: {name}")
    for missing in sorted(expected - observed_expected):
        failures.append(f"Expected Windows-only boundary was not collected/skipped: {missing}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("tests/fork_integration_manifest.json"))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    manifest_path = (root / args.manifest).resolve()
    try:
        manifest = load_manifest(root, manifest_path)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Validated {len(manifest['test_files'])} required test files", flush=True)
    if args.check_only:
        return 0
    if sys.platform != "linux":
        parser.error("This bounded fork lane requires Linux; native Windows coverage is separate")
    with tempfile.TemporaryDirectory(prefix="hermes-fork-ci-") as temporary:
        directory = Path(temporary)
        home = directory / "home"
        home.mkdir()
        reports = directory / "reports"
        env = os.environ.copy()
        env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"), HERMES_PYTHON=sys.executable)
        command = [
            "bash", "scripts/run_tests.sh", "-j", "2", "--file-retries", "0",
            "--file-timeout", "300", *manifest["test_files"], "--",
            "-p", "scripts.fork_integration_audit",
            "--fork-ci-report-dir", str(reports),
        ]
        result = subprocess.run(command, cwd=root, env=env, check=False)
        failures = audit_results(manifest, reports)
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        print(f"Audited {len(manifest['test_files'])} files; "
              f"{len(manifest['expected_linux_skips'])} declared native Windows skips; "
              f"{len(failures)} audit failures", flush=True)
        return 1 if result.returncode or failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
