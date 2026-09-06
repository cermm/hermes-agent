"""T1-24: structured-output schema on delegate_task.

Per-task ``output_schema`` (JSON Schema object): the child receives the
schema as an explicit output contract, the parent validates the child's
final answer with jsonschema, and on failure sends exactly ONE bounded
retry turn carrying the validation errors. Result entries gain
``schema_valid`` / ``schema_errors`` / ``schema_retries`` ONLY when a
schema was requested — schema-less calls keep a byte-identical result
shape (wire-shape pinning).

Pattern from: github/copilot-cli ctx.agent(prompt, {schema}) — PATTERN
ONLY, zero code/prompt text copied (proprietary).
"""

import json
import threading
import pytest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _run_single_child,
    delegate_task,
)
from tools.delegation_output_schema import (
    append_output_contract,
    build_retry_message,
    coerce_output_schema,
    validate_output,
)

ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "zip": {"type": "string"},
    },
    "required": ["city"],
}


# ---------------------------------------------------------------------------
# Helper-module unit tests
# ---------------------------------------------------------------------------


class TestValidateOutput:
    def test_valid_json_matching_schema(self):
        ok, errors = validate_output('{"city": "Berlin"}', ADDRESS_SCHEMA)
        assert ok is True
        assert errors == []

    def test_json_violating_schema_reports_errors(self):
        ok, errors = validate_output('{"zip": "10115"}', ADDRESS_SCHEMA)
        assert ok is False
        assert errors
        assert any("city" in e for e in errors)

    def test_non_json_text_reports_parse_error(self):
        ok, errors = validate_output("I could not produce JSON, sorry.", ADDRESS_SCHEMA)
        assert ok is False
        assert errors

    def test_code_fenced_json_is_accepted(self):
        text = '```json\n{"city": "Oslo"}\n```'
        ok, errors = validate_output(text, ADDRESS_SCHEMA)
        assert ok is True
        assert errors == []

    def test_json_embedded_in_prose_is_extracted(self):
        text = 'Here is the result:\n{"city": "Lima"}\nHope that helps!'
        ok, _ = validate_output(text, ADDRESS_SCHEMA)
        assert ok is True

    def test_empty_text_is_invalid(self):
        ok, errors = validate_output("", ADDRESS_SCHEMA)
        assert ok is False
        assert errors


class TestCoerceOutputSchema:
    def test_valid_schema_passes(self):
        schema, err = coerce_output_schema(ADDRESS_SCHEMA)
        assert schema == ADDRESS_SCHEMA
        assert err is None

    def test_none_passes_through(self):
        schema, err = coerce_output_schema(None)
        assert schema is None
        assert err is None

    def test_non_dict_is_rejected(self):
        schema, err = coerce_output_schema("not a schema")
        assert schema is None
        assert err

    def test_invalid_json_schema_is_rejected(self):
        schema, err = coerce_output_schema({"type": 42})
        assert schema is None
        assert err


class TestPromptPlumbing:
    def test_contract_block_carries_schema(self):
        out = append_output_contract("base context", ADDRESS_SCHEMA)
        assert "base context" in out
        assert "OUTPUT CONTRACT" in out
        assert '"city"' in out

    def test_contract_block_without_prior_context(self):
        out = append_output_contract(None, ADDRESS_SCHEMA)
        assert "OUTPUT CONTRACT" in out

    def test_retry_message_carries_verbatim_errors(self):
        msg = build_retry_message(["'city' is a required property"])
        assert "'city' is a required property" in msg
        assert "JSON" in msg


# ---------------------------------------------------------------------------
# Tool-schema surface (one-time static field)
# ---------------------------------------------------------------------------


class TestToolSchemaSurface:
    def test_output_schema_on_task_items(self):
        item_props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"][
            "items"
        ]["properties"]
        assert "output_schema" in item_props
        assert item_props["output_schema"]["type"] == "object"
        # never required
        assert "output_schema" not in DELEGATE_TASK_SCHEMA["parameters"][
            "properties"
        ]["tasks"]["items"]["required"]

    def test_output_schema_advertised_per_task_only(self):
        """output_schema is advertised inside tasks[] items (the only spawn
        shape); the legacy top-level param stays handler-accepted but out
        of the schema."""
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        assert "output_schema" not in props
        task_props = props["tasks"]["items"]["properties"]
        assert task_props["output_schema"]["type"] == "object"


# ---------------------------------------------------------------------------
# _run_single_child validation + bounded retry
# ---------------------------------------------------------------------------


class _StubChild:
    """Minimal child agent double (mirrors test_delegate_kanban_isolation)."""

    tool_progress_callback = None
    _delegate_saved_tool_names: list = []
    _credential_pool = None
    _subagent_id = None  # skip registry
    _delegate_depth = 1
    _parent_subagent_id = None
    _delegate_output_schema: dict | None = None
    model = "test-model"
    session_prompt_tokens = 0
    session_completion_tokens = 0
    session_estimated_cost_usd = 0.0
    session_reasoning_tokens = 0

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []

    def get_activity_summary(self):
        return {"api_call_count": 1, "max_iterations": 5, "current_tool": None}

    def run_conversation(self, user_message, task_id=None, **_kwargs):
        self.calls.append(user_message)
        text = self.responses.pop(0)
        return {
            "final_response": text,
            "completed": True,
            "api_calls": 1,
            "messages": [],
        }

    def close(self):
        return None


class _StubParent:
    _current_task_id = None
    _delegate_depth = 0

    def _touch_activity(self, _desc):
        return None


def _run(child):
    return _run_single_child(0, "produce the address", child, _StubParent())


class TestRunSingleChildSchemaValidation:
    @pytest.mark.parametrize("escalate", [False, True])
    def test_every_schema_attempt_retains_child_authority(self, escalate):
        from agent.delegation_context import (
            delegated_child_subprocess_env,
            is_delegated_child_context,
            is_dispatcher_owned_worker_context,
        )
        from tools.terminal_tool import _get_approval_callback

        child = self.escalating_child(["bad", '{"city":"Prague"}'])
        child._delegate_escalate_on_validation_failure = escalate
        original = _StubChild.run_conversation
        seen = []

        def capture(**kwargs):
            callback = _get_approval_callback()
            seen.append((
                is_delegated_child_context(),
                is_dispatcher_owned_worker_context(),
                delegated_child_subprocess_env({"HERMES_KANBAN_TASK": "parent", "PATH": "/bin"}),
                callback("fixture command", "fixture approval") if callback else None,
            ))
            return original(child, **kwargs)

        child.run_conversation = capture
        with patch("tools.delegate_tool._load_config", return_value={}):
            entry = _run(child)
        assert entry["status"] == "completed"
        assert len(seen) == 2
        for delegated, dispatcher, environment, approval in seen:
            assert delegated and not dispatcher
            assert "HERMES_KANBAN_TASK" not in environment
            assert environment["PATH"] == "/bin"
            assert approval == "deny"
        assert not is_delegated_child_context()

    def test_schema_retry_is_inside_total_timeout_and_deferred_close(self):
        child = self.escalating_child(["bad", '{"city":"Prague"}'])
        retry_started = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        original = child.run_conversation

        def blocked(**kwargs):
            if child.calls:
                retry_started.set()
                assert release.wait(10)
            return original(**kwargs)

        child.run_conversation = blocked
        child.close = closed.set
        parent_result = []
        done = threading.Event()

        def run_parent():
            try:
                parent_result.append(_run(child))
            finally:
                done.set()

        with patch("tools.delegate_tool._get_child_timeout", return_value=2):
            parent = threading.Thread(target=run_parent, daemon=True)
            parent.start()
            try:
                assert retry_started.wait(5)
                assert done.wait(5), "the blocked retry escaped the configured timeout"
                assert parent_result[0]["status"] == "timeout"
                assert not closed.is_set(), "resources closed while retry still owns the child"
            finally:
                release.set()
                parent.join(10)
        assert closed.wait(5)

    def escalating_child(self, responses):
        child = _StubChild(responses)
        child._delegate_output_schema = ADDRESS_SCHEMA
        child._delegate_escalate_on_validation_failure = True
        child._fallback_chain = ["terra", "sol", "astra"]
        child.switched = []

        def activate():
            if len(child.switched) == len(child._fallback_chain):
                return False
            child.model = child._fallback_chain[len(child.switched)]
            child.switched.append(child.model)
            return True

        child._try_activate_fallback = activate
        original = child.run_conversation

        def run(*args, **kwargs):
            if child.calls:
                from agent.agent_runtime_helpers import restore_primary_runtime
                assert child._delegate_validation_retry_active is True
                assert restore_primary_runtime(child) is False
            return original(*args, **kwargs)

        child.run_conversation = run
        return child

    @pytest.mark.parametrize("escalate", [False, True])
    @pytest.mark.parametrize("terminal", ["completed", "failed", "interrupted"])
    def test_retry_result_describes_last_turn_and_total_work(self, escalate, terminal):
        child = self.escalating_child(["bad", '{"city":"Prague"}'])
        child._delegate_escalate_on_validation_failure = escalate
        original = _StubChild.run_conversation
        first_messages = [{"role": "assistant", "content": "bad"}]
        last_messages = [{"role": "assistant", "content": '{"city":"Prague"}'}]

        def run(**kwargs):
            result = original(child, **kwargs)
            result["completed"] = len(child.calls) > 1 and terminal == "completed"
            result["api_calls"] = len(child.calls)
            result["messages"] = first_messages if len(child.calls) == 1 else (
                (kwargs.get("conversation_history") or []) + last_messages
            )
            if len(child.calls) > 1 and terminal == "failed":
                result.update(failed=True, error="provider rejected retry", failure_reason="billing")
            if len(child.calls) > 1 and terminal == "interrupted":
                result["interrupted"] = True
            return result

        child.run_conversation = run
        entry = _run(child)
        assert entry["status"] == terminal
        assert entry["exit_reason"] == ("error" if terminal == "failed" else terminal)
        assert not entry["truncated"]
        assert entry["api_calls"] == 3
        if terminal == "failed":
            assert entry["failure_reason"] == "billing"
            assert entry["error"] == "provider rejected retry"

    @pytest.mark.parametrize("escalate", [False, True])
    @pytest.mark.parametrize("first_steer", [None, "accepted before repair"])
    def test_schema_retry_preserves_each_unconsumed_steer(self, escalate, first_steer):
        child = self.escalating_child(["bad", "bad", '{"city":"Prague"}'] if escalate else ["bad", '{"city":"Prague"}'])
        child._delegate_escalate_on_validation_failure = escalate
        original = _StubChild.run_conversation
        pending = [first_steer, "accepted during repair"]
        if escalate:
            pending.append("accepted during final repair")

        def run(**kwargs):
            result = original(child, **kwargs)
            result["pending_steer"] = pending[len(child.calls) - 1]
            return result

        child.run_conversation = run
        entry = _run(child)
        assert entry["status"] == "completed"
        assert entry["missed_steer"] == "\n".join(text for text in pending if text)

    @pytest.mark.parametrize("attempts", [0, 1, 2, 3])
    def test_schema_diagnostic_reports_executed_retries(self, attempts):
        child = self.escalating_child(["bad"] * (attempts + 1))
        child._fallback_chain = child._fallback_chain[:attempts]
        entry = _run(child)
        assert entry.get("schema_retries", 0) == attempts
        suffix = "retry" if attempts == 1 else "retries"
        assert entry["error"].endswith(f"(after {attempts} {suffix})." if attempts else "output_schema.")

    @pytest.mark.parametrize("escalate", [False, True])
    @pytest.mark.parametrize("outcome", ["exception", "invalid_result", "provider_failure"])
    def test_retry_failure_classification_survives_schema_failure(self, escalate, outcome):
        child = self.escalating_child(["bad", "still bad"])
        child._delegate_escalate_on_validation_failure = escalate
        original = _StubChild.run_conversation
        def run(**kwargs):
            if not child.calls:
                return original(child, **kwargs)
            if outcome == "exception":
                raise RuntimeError("retry transport failed")
            if outcome == "invalid_result":
                return None
            return {"completed": False, "failed": True, "error": "provider rejected retry",
                    "failure_reason": "billing", "final_response": "provider rejected retry"}
        child.run_conversation = run
        entry = _run(child)
        assert entry["status"] == "failed"
        assert entry["exit_reason"] == "error"
        assert entry["schema_retries"] == 1
        assert not entry["schema_valid"]
        assert not entry["truncated"]
        assert entry["failure_reason"] == {"exception": "schema_retry_error", "invalid_result": "invalid_child_result", "provider_failure": "billing"}[outcome]
        assert "output_schema" not in entry["error"]


    def test_validation_escalates_until_valid(self):
        child = self.escalating_child(["bad", "bad", '{"city":"Oslo"}'])
        entry = _run(child)
        assert entry["status"] == "completed"
        assert child.switched == ["terra", "sol"]
        assert entry["schema_retries"] == 2
        assert child._delegate_validation_retry_active is False

    def test_validation_stops_after_highest_tier(self):
        child = self.escalating_child(["bad"] * 4)
        entry = _run(child)
        assert entry["status"] == "failed"
        assert child.switched == ["terra", "sol", "astra"]
        assert len(child.calls) == 4
        assert entry["schema_retries"] == 3

    def test_validation_does_not_escalate_valid_result(self):
        child = self.escalating_child(['{"city":"Oslo"}'])
        assert _run(child)["status"] == "completed"
        assert child.switched == []

    def test_validation_does_not_escalate_interruption(self):
        child = self.escalating_child([])
        child.run_conversation = lambda **kwargs: {"interrupted": True, "final_response": "bad"}
        assert _run(child)["status"] == "interrupted"
        assert child.switched == []

    def test_valid_first_try_no_retry(self):
        child = _StubChild(['{"city": "Berlin"}'])
        child._delegate_output_schema = ADDRESS_SCHEMA
        entry = _run(child)
        assert entry["status"] == "completed"
        assert entry["schema_valid"] is True
        assert "schema_errors" not in entry
        assert len(child.calls) == 1

    def test_invalid_then_retry_then_valid(self):
        child = _StubChild(["not json at all", '{"city": "Oslo"}'])
        child._delegate_output_schema = ADDRESS_SCHEMA
        entry = _run(child)
        assert entry["schema_valid"] is True
        assert entry["schema_retries"] == 1
        # retry turn carried the validation errors
        assert len(child.calls) == 2
        assert "rejected" in child.calls[1] or "JSON" in child.calls[1]
        # final summary is the retried (valid) answer
        assert json.loads(entry["summary"])["city"] == "Oslo"

    def test_invalid_twice_surfaces_errors_and_stops(self):
        child = _StubChild(["nope", "still nope"])
        child._delegate_output_schema = ADDRESS_SCHEMA
        entry = _run(child)
        assert entry["schema_valid"] is False
        assert entry["schema_errors"]
        assert entry["schema_retries"] == 1
        # exactly ONE retry — bounded
        assert len(child.calls) == 2

    def test_retry_exception_degrades_to_invalid(self):
        child = _StubChild(["nope"])
        child._delegate_output_schema = ADDRESS_SCHEMA

        original = child.run_conversation

        def flaky(user_message, task_id=None, **kw):
            if child.calls:
                raise RuntimeError("child died on retry")
            return original(user_message, task_id=task_id, **kw)

        child.run_conversation = flaky
        entry = _run(child)
        assert entry["schema_valid"] is False
        assert entry["schema_errors"]

    def test_no_schema_keeps_legacy_result_shape(self):
        """Schema-less calls must not gain new keys (wire-shape pinning)."""
        child = _StubChild(['{"city": "Berlin"}'])
        entry = _run(child)
        assert "schema_valid" not in entry
        assert "schema_errors" not in entry
        assert "schema_retries" not in entry
        assert len(child.calls) == 1

    def test_failed_child_skips_validation(self):
        """A child with no output never gets a schema retry turn."""
        child = _StubChild([""])
        child._delegate_output_schema = ADDRESS_SCHEMA
        entry = _run(child)
        assert entry["status"] == "failed"
        assert len(child.calls) == 1
        assert entry.get("schema_valid") is False

    def test_schema_failure_reported_as_failed_not_completed(self):
        """Regression: a final answer that still violates the declared
        output contract after the bounded retry (here the classic empty
        ``{}`` fallback) must be reported status="failed", not
        "completed". Otherwise the batch report prints a ✓ and
        orchestrators that read only status/icon accept an empty verdict
        — schema_valid/schema_errors carry the detail, but status must
        agree with them."""
        child = _StubChild(["not json at all", "{}"])
        child._delegate_output_schema = ADDRESS_SCHEMA
        entry = _run(child)
        assert entry["schema_valid"] is False
        assert entry["schema_errors"]
        assert entry["status"] == "failed"
        # the failed entry names the schema violation, not the generic
        # "no response" error — the child DID respond, unusably
        assert "output_schema" in entry.get("error", "")
        # the invalid final text is still propagated for debugging
        assert entry["summary"] == "{}"

    def test_schema_failure_without_retry_reported_as_failed(self):
        """Same class, first-try path: retry turn raises, leaving the
        original non-JSON answer in place — status must still be failed."""
        child = _StubChild(["nope"])
        child._delegate_output_schema = ADDRESS_SCHEMA

        original = child.run_conversation

        def flaky(user_message, task_id=None, **kw):
            if child.calls:
                raise RuntimeError("child died on retry")
            return original(user_message, task_id=task_id, **kw)

        child.run_conversation = flaky
        entry = _run(child)
        assert entry["schema_valid"] is False
        assert entry["status"] == "failed"

    def test_schema_valid_entry_still_completed(self):
        """Guard: schema_valid=True keeps status="completed" untouched."""
        child = _StubChild(['{"city": "Berlin"}'])
        child._delegate_output_schema = ADDRESS_SCHEMA
        entry = _run(child)
        assert entry["status"] == "completed"
        assert "error" not in entry


# ---------------------------------------------------------------------------
# delegate_task dispatch-time schema handling
# ---------------------------------------------------------------------------


def _make_mock_parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    return parent


class TestDelegateTaskDispatch:
    def test_non_dict_output_schema_rejected(self):
        with (
            patch("tools.delegate_tool._load_config", return_value={}),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                return_value={
                    "provider": None,
                    "model": None,
                    "base_url": None,
                    "api_key": None,
                    "api_mode": None,
                },
            ),
        ):
            out = delegate_task(
                tasks=[
                    {"goal": "Summarize the release notes for module A", "output_schema": "not-a-dict"},
                    {"goal": "Summarize the release notes for module B"},
                ],
                parent_agent=_make_mock_parent(),
            )
        payload = json.loads(out)
        assert payload.get("error")
        assert "output_schema" in payload["error"]

    def test_invalid_json_schema_rejected_at_dispatch(self):
        with (
            patch("tools.delegate_tool._load_config", return_value={}),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                return_value={
                    "provider": None,
                    "model": None,
                    "base_url": None,
                    "api_key": None,
                    "api_mode": None,
                },
            ),
        ):
            out = delegate_task(
                tasks=[
                    {"goal": "Summarize the release notes for module A", "output_schema": {"type": 42}},
                    {"goal": "Summarize the release notes for module B"},
                ],
                parent_agent=_make_mock_parent(),
            )
        payload = json.loads(out)
        assert payload.get("error")
        assert "output_schema" in payload["error"]

    def test_child_receives_contract_and_schema_attr(self):
        """The built child carries the schema attr and its context gains
        the output-contract block."""
        captured = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            child = _StubChild(['{"city": "Rio"}'])
            return child

        with (
            patch("tools.delegate_tool._load_config", return_value={}),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                return_value={
                    "provider": None,
                    "model": None,
                    "base_url": None,
                    "api_key": None,
                    "api_mode": None,
                },
            ),
            patch(
                "tools.delegate_tool._build_child_preserving_parent_tools",
                side_effect=fake_build,
            ),
        ):
            out = delegate_task(
                goal="produce the address",
                context="base context",
                output_schema=ADDRESS_SCHEMA,
                parent_agent=_make_mock_parent(),
            )
        payload = json.loads(out)
        assert "OUTPUT CONTRACT" in (captured.get("context") or "")
        results = payload.get("results") or []
        assert results and results[0].get("schema_valid") is True
