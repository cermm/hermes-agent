"""Real bundled delivery and profile-local loading; no model or language servers."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]
NAME = "semantic-code-intelligence"
RELATIVE = Path("software-development") / NAME / "SKILL.md"
ROLES = ("builder", "builder-mini", "builder-low", "builder-medium", "builder-high", "reviewer", "reviewersol")

PROBE = r"""
import hashlib,json
from pathlib import Path
from hermes_constants import get_hermes_home
from tools import skills_tool
from tools.registry import registry
from agent.prompt_builder import build_skills_system_prompt
from tools.mcp_tool_schema import mcp_prefixed_tool_name
name="semantic-code-intelligence"
listed=json.loads(registry.dispatch("skills_list",{}))
viewed=json.loads(registry.dispatch("skill_view",{"name":name},task_id="semantic-skill-delivery"))
index=build_skills_system_prompt(available_tools={"skill_view","skills_list","read_file","search_files"},available_toolsets={"skills","file"})
print(json.dumps({"home":str(get_hermes_home()),"listed":listed,"viewed":viewed,"index":index,
 "imports":{"skills_tool":skills_tool.__file__,"registry":__import__("tools.registry",fromlist=["x"]).__file__},
 "navigation_names":[mcp_prefixed_tool_name("serena_semantic_navigation",n) for n in
 ["get_symbols_overview","find_symbol","find_referencing_symbols","find_implementations","find_declaration","get_diagnostics_for_file"]]}))
"""


def _configure(tmp_path, monkeypatch, skills=None):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_PLATFORM", "cli")
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_BUNDLED_SKILLS", raising=False)
    monkeypatch.delenv("HERMES_OPTIONAL_SKILLS", raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    profile = home / ".hermes" / "profiles" / "fixture"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    config = {"model":{"default":"fixture-model","provider":"fixture-provider"},"skills":skills or {}}
    (profile / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    return profile


def _seed(profile):
    from hermes_cli.profiles import seed_profile_skills
    result = seed_profile_skills(profile, quiet=True)
    assert result is not None, "real bundled skill subprocess failed"
    return result


def _probe(profile):
    process = subprocess.run([sys.executable, "-B", "-c", PROBE], cwd=REPO,
                             env={**os.environ,"HERMES_HOME":str(profile)},
                             text=True,capture_output=True,timeout=45)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert Path(result["home"]) == profile
    assert all(Path(p).is_relative_to(REPO) for p in result["imports"].values())
    return result


@pytest.mark.parametrize("role", ROLES)
def test_bundled_skill_delivers_and_loads_in_each_worker_profile(tmp_path, monkeypatch, role):
    profile = _configure(tmp_path, monkeypatch)
    named = profile.with_name(role)
    profile.rename(named)
    profile = named
    monkeypatch.setenv("HERMES_HOME", str(profile))
    before = (profile / "config.yaml").read_bytes()
    source = REPO / "skills" / RELATIVE
    assert source.is_file(), "semantic workflow is missing from the shipped bundle"
    first = _seed(profile)
    assert NAME in first["copied"]
    copied = profile / "skills" / RELATIVE
    assert copied.read_bytes() == source.read_bytes()
    second = _seed(profile)
    assert NAME not in second["copied"] + second["updated"]
    loaded = _probe(profile)
    assert NAME in {x["name"] for x in loaded["listed"]["skills"]}
    assert NAME in loaded["index"]  # Also visible with native-only file/skills capabilities.
    assert loaded["viewed"]["success"] is True
    assert Path(loaded["viewed"]["skill_dir"]) == copied.parent
    assert loaded["viewed"]["content"] == source.read_text(encoding="utf-8")
    assert all(name in loaded["viewed"]["content"] for name in loaded["navigation_names"])
    assert (profile / "config.yaml").read_bytes() == before
    sibling = profile.parent / "unseeded-sibling"
    sibling.mkdir()
    (sibling / "config.yaml").write_bytes(before)
    assert NAME not in {x["name"] for x in _probe(sibling)["listed"]["skills"]}
    receipt = tmp_path / "skill-delivery-receipt.json"
    receipt.write_text(json.dumps({"role":role,"profile":str(profile),"source":str(source),
        "source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"first_sync":first,"second_sync":second,
        "loaded":loaded,"config_unchanged":True},indent=2),encoding="utf-8")
    print("SKILL_DELIVERY_RECEIPT " + str(receipt))


@pytest.mark.parametrize("policy", ["pristine-update","modified","deleted","local-collision","suppressed",
                                     "opt-out","external-shadow","disabled","platform-disabled"])
def test_skill_delivery_preserves_user_policy(tmp_path, monkeypatch, policy):
    profile = _configure(tmp_path, monkeypatch)
    bundle = tmp_path / "bundle"
    stock = bundle / RELATIVE
    stock.parent.mkdir(parents=True)
    stock.write_bytes((REPO / "skills" / RELATIVE).read_bytes())
    monkeypatch.setenv("HERMES_BUNDLED_SKILLS", str(bundle))
    local = profile / "skills" / RELATIVE
    config = json.loads((profile / "config.yaml").read_text())
    original = stock.read_bytes()
    custom = original + b"\nLocal workflow preference.\n"
    if policy in {"pristine-update","modified","deleted"}:
        assert NAME in _seed(profile)["copied"]
        stock.write_bytes(original + b"\nBundled workflow update.\n")
        if policy == "modified": local.write_bytes(custom)
        if policy == "deleted": shutil.rmtree(local.parent)
    elif policy == "local-collision":
        local.parent.mkdir(parents=True)
        local.write_bytes(custom)
    elif policy == "suppressed":
        (profile / "skills").mkdir()
        (profile / "skills" / ".curator_suppressed").write_text(NAME + "\n")
    elif policy == "opt-out":
        (profile / ".no-bundled-skills").write_text("User opted out.\n")
    elif policy == "external-shadow":
        external = tmp_path / "external"
        external_skill = external / RELATIVE
        external_skill.parent.mkdir(parents=True)
        external_skill.write_bytes(custom)
        config["skills"]["external_dirs"] = [str(external)]
    elif policy == "disabled":
        config["skills"]["disabled"] = [NAME]
    elif policy == "platform-disabled":
        config["skills"]["platform_disabled"] = {"cli":[NAME]}
    (profile / "config.yaml").write_text(json.dumps(config))
    before = (profile / "config.yaml").read_bytes()
    result = _seed(profile)
    loaded = _probe(profile)
    present = NAME in {x["name"] for x in loaded["listed"]["skills"]}
    visible = NAME in loaded["index"]
    if policy in {"deleted","suppressed","opt-out"}:
        assert not local.exists() and not present and not visible
        assert loaded["viewed"]["success"] is False
        assert NAME not in result["copied"] + result["updated"]
    elif policy in {"disabled","platform-disabled"}:
        assert local.read_bytes() == original
        assert not present and not visible and loaded["viewed"]["success"] is False
        assert "disabled" in loaded["viewed"]["error"]
    else:
        assert present and visible and loaded["viewed"]["success"] is True
        expected = stock.read_bytes() if policy == "pristine-update" else custom
        assert loaded["viewed"]["content"].encode() == expected
        if policy == "external-shadow":
            assert not local.exists() and NAME in result["shadowed_by_external"]
            assert Path(loaded["viewed"]["skill_dir"]) == external_skill.parent
        else:
            assert local.read_bytes() == expected
            assert Path(loaded["viewed"]["skill_dir"]) == local.parent
    if policy == "pristine-update": assert NAME in result["updated"]
    if policy == "modified": assert NAME in result["user_modified"]
    assert (profile / "config.yaml").read_bytes() == before
