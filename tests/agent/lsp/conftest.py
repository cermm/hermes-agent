"""Disposable real peer profile for explicit LSP checks."""
import json
from pathlib import Path
import subprocess
import sys
import pytest

@pytest.fixture
def check_project(tmp_path):
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    profile = tmp_path / "profile"
    profile.mkdir()
    peer = Path(__file__).with_name("_check_lsp_server.py")
    launcher = tmp_path / "check-server"
    launcher.write_text(f"#!{sys.executable}\nimport sys, runpy\nsys.path.insert(0, {str(peer.parent)!r})\nrunpy.run_path({str(peer)!r}, run_name='__main__')\n")
    launcher.chmod(0o755)
    sdk = tmp_path / "typescript"
    (sdk / "lib").mkdir(parents=True)
    (sdk / "lib/tsserver.js").write_text("// prerequisite; fixture peer does not execute it")
    (sdk / "package.json").write_text(json.dumps({"version": "6.0.3"}))
    wire = tmp_path / "wire.jsonl"
    (profile / "config.yaml").write_text(json.dumps({"lsp": {"enabled": True, "install_strategy": "auto", "wait_timeout": 5,
        "servers": {"typescript": {"command": [str(launcher), "--stdio"], "env": {"CHECK_LOG": str(wire)},
            "initialization_options": {"tsserver": {"path": str(sdk)}}}}}}))
    return project, profile, wire
