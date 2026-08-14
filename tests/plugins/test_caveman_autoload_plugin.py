"""Tests for the bundled caveman-autoload plugin."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "caveman-autoload"


def _load_plugin_init():
    module_name = "hermes_plugins_test.caveman_autoload"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_DIR / "__init__.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestManifest:
    def test_plugin_directory_exists(self):
        assert PLUGIN_DIR.is_dir()
        assert (PLUGIN_DIR / "plugin.yaml").exists()
        assert (PLUGIN_DIR / "__init__.py").exists()

    def test_manifest_fields(self):
        data = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
        assert data["name"] == "caveman-autoload"
        assert data["kind"] == "standalone"
        assert data["provides_hooks"] == ["pre_llm_call"]


class TestPredicate:
    def test_default_profile_coding_prompt_is_not_enough(self, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        assert mod._should_apply("implement the pytest repair") is False

    def test_worker_profile_with_coding_intent_applies(self, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        worker_home = tmp_path / ".hermes" / "profiles" / "builder"
        monkeypatch.setenv("HERMES_HOME", str(worker_home))
        assert mod._should_apply("implement the pytest repair") is True

    def test_non_coding_worker_prompt_does_not_apply(self, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        worker_home = tmp_path / ".hermes" / "profiles" / "builder"
        monkeypatch.setenv("HERMES_HOME", str(worker_home))
        assert mod._should_apply("write a polished customer-facing report") is False

    def test_hard_exclusion_beats_force(self, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "builder"))
        monkeypatch.setenv("CAVEMAN_AUTOLOAD_FORCE", "1")
        assert mod._should_apply("approve protected action rollback for this code") is False

    def test_off_switch_beats_worker_coding(self, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "builder"))
        monkeypatch.setenv("CAVEMAN_AUTOLOAD_OFF", "true")
        assert mod._should_apply("implement the pytest repair") is False

    def test_multimodal_text_is_extracted(self, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "reviewer"))
        message = [
            {"type": "text", "text": "please review this diff"},
            {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
        ]
        assert mod._should_apply(message) is True

    def test_context_profile_from_plugin_context_applies(self, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        assert mod._should_apply("debug test failure", context_profile="builder-high") is True

    def test_dispatcher_helper_controls_kanban_env_trust(self, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
        assert mod._is_dispatcher_worker() is True

        from agent.delegation_context import non_dispatcher_owned_context

        with non_dispatcher_owned_context():
            assert mod._is_dispatcher_worker() is False

    def test_dispatcher_helper_missing_fails_closed(self, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
        agent_mod = ModuleType("agent")
        monkeypatch.setitem(sys.modules, "agent", agent_mod)
        monkeypatch.setitem(sys.modules, "agent.delegation_context", ModuleType("agent.delegation_context"))
        assert mod._is_dispatcher_worker() is False


class TestPluginDiscovery:
    def test_loads_when_enabled_and_injects_context(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes" / "profiles" / "builder"
        hermes_home.mkdir(parents=True)
        (hermes_home / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["caveman-autoload"]}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        for key in list(sys.modules):
            if key.startswith(("hermes_plugins", "hermes_cli.plugins")):
                del sys.modules[key]

        from hermes_cli.plugins import _ensure_plugins_discovered

        mgr = _ensure_plugins_discovered(force=True)
        loaded = mgr._plugins.get("caveman-autoload")
        assert loaded is not None
        assert loaded.enabled is True
        assert loaded.module is not None

        results = mgr.invoke_hook("pre_llm_call", user_message="implement pytest fix")
        assert results == [{"context": loaded.module.DIRECTIVE}]

    def test_discovered_but_not_loaded_by_default(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        from hermes_cli import plugins as plugins_mod

        mgr = plugins_mod.PluginManager()
        mgr.discover_and_load(force=True)
        loaded = mgr._plugins.get("caveman-autoload")
        assert loaded is not None
        assert loaded.enabled is False
        assert "not enabled" in (loaded.error or "").lower()
