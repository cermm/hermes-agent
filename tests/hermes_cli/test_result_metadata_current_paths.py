"""Integration seams introduced by the current CLI decomposition and TTY query mode."""

import os
import types

import pytest

from hermes_cli import result_metadata
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
from hermes_cli.cli_result_metadata import CLIResultMetadataMixin


@pytest.mark.parametrize("metadata_requested", [False, True])
def test_metadata_query_exits_after_one_turn_even_on_tty(monkeypatch, metadata_requested):
    import cli

    calls = []
    reader, writer = os.pipe()
    owner = result_metadata.claim_result_metadata_fd(writer)
    fake = types.SimpleNamespace(
        result_meta_fd=owner if metadata_requested else None,
        console=types.SimpleNamespace(print=lambda *a, **k: None),
        _claim_active_session=lambda *a, **k: True,
        _show_security_advisories=lambda: None,
        chat=lambda *a, **k: calls.append("query"),
        run=lambda: calls.append("interactive"),
        _print_exit_summary=lambda **k: calls.append("summary"),
    )
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a: True)
    monkeypatch.setattr(cli, "_collect_query_images", lambda q, i: (q, []))
    monkeypatch.setattr(cli, "_collect_kanban_task_images", lambda images: [])
    monkeypatch.setattr(cli, "_finalize_single_query", lambda c: calls.append("finalize"))
    try:
        cli._run_single_query_mode(fake, "hello", None, False, False)
        assert calls == (["query", "summary", "finalize"] if metadata_requested else ["interactive"])
    finally:
        owner.close()
        os.close(reader)


@pytest.mark.parametrize("result", [
    {"completed": True, "failed": False, "partial": False, "interrupted": False, "api_calls": 1},
    {"completed": False, "failed": True, "partial": False, "interrupted": False, "api_calls": 1},
    {"completed": False, "failed": False, "partial": False, "interrupted": True, "api_calls": 1},
])
def test_current_chat_turn_publishes_once_before_rendering(monkeypatch, result):
    import cli

    reader, writer = os.pipe()
    owner = result_metadata.claim_result_metadata_fd(writer)

    class Query(CLIChatTurnMixin, CLIResultMetadataMixin):
        result_meta_fd = owner
        max_turns = 10
        agent = object()
        _secret_capture_callback = None
        _active_agent_route_signature = "same"

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, message):
            return {"signature": "same", "model": "test", "runtime": {}}

        def _init_agent(self, **kwargs):
            return True

        def _chat_route_images(self, message, images):
            return message

        def _chat_expand_context_references(self, message):
            return message, None

        def _chat_stage_user_message(self, *args):
            pass

        def _reset_stream_state(self):
            pass

        def _chat_setup_turn_audio(self, *args):
            pass

        def _chat_run_agent(self, turn, message):
            turn.result = result

        def _chat_monitor_agent_thread(self, turn, worker):
            worker.join(timeout=2)
            assert not worker.is_alive()

        def _chat_settle_turn(self, turn):
            pass

        def _chat_render_turn(self, *args):
            assert owner.closed
            return "rendered"

        def _chat_release_turn_audio(self, turn):
            pass

    monkeypatch.setattr(cli, "set_secret_capture_callback", lambda callback: None)
    try:
        query = Query()
        assert query.chat("hello") == "rendered"
        assert os.read(reader, result_metadata.MAX_METADATA_BYTES) == result_metadata.serialize_result_metadata(
            result_metadata.build_result_metadata(result, max_iterations=query.max_turns)
        )
        assert os.read(reader, 1) == b""
    finally:
        owner.close()
        os.close(reader)
