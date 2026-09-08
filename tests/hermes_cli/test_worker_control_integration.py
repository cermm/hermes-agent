"""Cross-repository actual Linux containment + agent + spend-boundary replay.

Set pytest's pythonpath option to the verified W&C candidate for this explicit
integration group. No real providers, workers, production state or transports.
"""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from hermes_cli.worker_control import effective_configuration, candidate_sources, fingerprint
from hermes_cli.worker_guard import GuardSpec, ProcessIdentity


@pytest.mark.linux_only
@pytest.mark.parametrize("route", ["chat", "codex", "codex-goal"])
def test_real_confined_agent_reaches_durable_fake_provider(tmp_path, monkeypatch, route):
    pytest.importorskip("programming_flow_infra", reason="cross-repository candidate path not supplied")
    from programming_flow_infra.event_action.worker_activation import ControlledSession
    from programming_flow_infra.event_action.spend_broker import Pricing, ProviderResult, SpendBroker, SpendGrant, SpendStore
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "host-fixture-home"))
    from run_agent import AIAgent
    actual = AIAgent(provider="openai-codex" if route != "chat" else "openai",
                     model="gpt-6-astra" if route != "chat" else "fixture-model",
                     api_mode="codex_responses" if route != "chat" else "chat_completions",
                     base_url="https://chatgpt.com/backend-api/codex" if route != "chat" else "http://broker.invalid/v1", api_key="not-a-credential",
                     enabled_toolsets=[], max_tokens=32, max_iterations=3,
                     quiet_mode=True, skip_context_files=True, skip_memory=True,
                     skip_background_review=True, fallback_model={})
    configuration = effective_configuration(actual)
    native_request = actual._build_api_kwargs([{"role": "user", "content": "fixture"}])
    actual.close()
    assert configuration["tools"] == []
    root = Path(__file__).resolve().parents[2]
    sources = candidate_sources(root)
    scope = {"board": "disposable", "task_id": "fixture", "run_id": "run-exact", "fence": 1,
             "incarnation": "fixture-incarnation", "generation": 7, "profile": "fixture-profile"}
    policy = {"schema": "confined-worker-policy-v1", "scope": scope, "configuration": configuration,
              "source_files": sources, "challenge": "d" * 64, "enabled_toolsets": [],
              "prompt": "Reply with fixture-complete. Do not call any tools.",
              "run_budget_seconds": 60, "channel_timeout_seconds": 30}
    if route == "codex-goal":
        skill = {"name":"fixture-skill", "path":"/pinned/fixture-skill.md", "content":"Keep the fixture local and bounded."}
        skill["sha256"] = hashlib.sha256(skill["content"].encode()).hexdigest()
        contract = dict(task_id=scope["task_id"], run_id=scope["run_id"], title="Complete fixture",
                        body="Produce the fixture result.", goal_mode=True, goal_max_turns=2,
                        skills=[skill], model_override=None, provider_override=None, reasoning_effort=None)
        policy["task_contract"] = contract
        scope["task_contract_sha256"] = fingerprint(contract)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    price = Pricing("openai", "fixture-model", "fixture-not-a-rate", "TEST", "unit", 1, 1, 1)
    store = SpendStore(private / "spend.db")
    store.configure("fixture-campaign", "fixture-objective", price, 1_000_000, 1_000_000)

    class Provider:
        pricing = price
        calls = []

        def validate(self, request):
            return len(json.dumps(request).encode())

        def send(self, request_id, request, *, deadline_monotonic):
            assert time.monotonic() < deadline_monotonic
            assert store.totals("fixture-campaign")["reserved"] > 0
            self.calls.append(request)
            return ProviderResult({"id": "fixture-free", "object": "chat.completion", "created": 1,
                "model": "fixture-model", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "fixture-complete"}}]}, self.validate(request), 8, "fixture-receipt")

        def reconcile(self, request_id):
            return None

    provider = Provider()
    lock = threading.RLock()
    broker = SpendBroker(store, provider, lambda grant: True, effect_guard=lambda _: lock)
    broker.register(SpendGrant("fixture-grant", "fixture-campaign", "fixture-objective", price,
                              time.time() + 90, 100_000, 32, 500_000, scope))
    if route != "chat":
        from programming_flow_infra.event_action.request_quota import RequestQuotaStore, RequestQuotaBroker, QuotaGrant
        from programming_flow_infra.event_action.codex_provider import CodexProvider, CODEX_BASE_URL
        store = RequestQuotaStore(private / "quota.db")
        store.configure("fixture-campaign", "fixture-objective", campaign_limit=10,
                        objective_deadline=time.time()+90, campaign_deadline=time.time()+120)
        class Client:
            def __init__(self, **kwargs):
                assert kwargs["max_retries"] == 0 and kwargs["api_key"] == "inert-test-only"
                self.responses = self
            def create(self, **kwargs):
                assert store.totals("fixture-campaign")["reserved"] == 1
                (private / "provider-request.json").write_text(json.dumps(kwargs))
                response_text = "fixture-complete"
                if kwargs["instructions"].startswith("You are a strict judge"):
                    assert not kwargs.get("tools")
                    response_text = json.dumps({"verdict":"done" if store.totals("fixture-campaign")["consumed"] >= 4 else "continue",
                                                "reason":"independent fixture follow-up"})
                class Stream:
                    def __init__(self, items): self.items = items
                    def __iter__(self): return iter(self.items)
                    def close(self): pass
                return Stream([{"type":"response.output_item.done","output_index":0,"item":
                    {"id":"msg_fixture","type":"message","role":"assistant","status":"completed",
                     "content":[{"type":"output_text","text":response_text,"annotations":[]}] }},
                    {"type":"response.completed","response":{"id":"resp_fixture","object":"response",
                    "created_at":1,"model":"gpt-6-astra","status":"completed","output":[],
                    "parallel_tool_calls":False,"tool_choice":"auto","tools":[],
                    "usage":{"input_tokens":7,"output_tokens":3,"total_tokens":10,
                    "input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":0}}}}])
            def close(self):
                pass
        provider = CodexProvider(expected_request={k:v for k,v in native_request.items() if k not in {"timeout", "extra_headers"}},
            credential_resolver=lambda:{"api_key":"inert-test-only", "base_url":CODEX_BASE_URL}, client_factory=Client)
        original_request = RequestQuotaBroker.request
        def diagnostic_request(self, *args):
            try:
                value = original_request(self, *args)
                (private / "broker-outcome.json").write_text(json.dumps(value))
                return value
            except Exception as error:
                (private / "broker-outcome.json").write_text(json.dumps({"error": str(error)}))
                raise
        monkeypatch.setattr(RequestQuotaBroker, "request", diagnostic_request)
        broker = RequestQuotaBroker(store, provider, lambda grant: True, effect_guard=lambda _: lock)
        broker.register(QuotaGrant("fixture-grant", "fixture-campaign", "fixture-objective", "openai-codex", "gpt-6-astra",
                                  time.time()+90, scope, max_request_seconds=30))
    runtime = Path(sys.base_prefix).resolve()
    venv = Path(sys.prefix).resolve()
    spec = GuardSpec("fixture-permit", scope,
        (str(venv / "bin/python"), "-I", "-X", "pycache_prefix=/__hermes_uncached__",
         str(root / "hermes_cli/worker_control_bootstrap.py"), "--policy", str(policy_path)),
        tuple(dict.fromkeys(("/usr/bin", "/usr/lib/x86_64-linux-gnu", "/usr/lib/python3.12", "/usr/lib64",
                             str(runtime.parent), str(venv), str(root), str(policy_path)))),
        str(workspace), str(private), str(private / "lease.json"), ProcessIdentity.capture(os.getpid()),
        time.monotonic() + 60, environment={"HERMES_HOME": "/tmp/hermes-home", "HOME": "/tmp/hermes-home"},
        memory_bytes=512 * 1024 * 1024, process_limit=16)
    session = ControlledSession(spec, policy, broker, "fixture-grant", lambda _: True)
    try:
        try:
            session.activate()
        except Exception as exc:
            raise AssertionError(session.handle.receipt_path.with_name("stderr.log").read_text()
                                 + session.handle.receipt_path.read_text()) from exc
        outcome = session.wait(timeout=65)
        if route != "chat" and (private / "broker-outcome.json").exists():
            from openai.types.responses import Response
            observed_broker = json.loads((private / "broker-outcome.json").read_text())
            if observed_broker.get("state") == "settled":
                Response.model_validate(observed_broker["response"])
        assert outcome["guardian"]["artifacts_exported"], json.dumps(outcome) + session.handle.receipt_path.with_name("stderr.log").read_text()
        evidence = Path(outcome["guardian"]["artifacts_path"]) / "worker-result.json"
        exported = next(row for row in outcome["guardian"]["artifacts_manifest"] if row["path"] == "worker-result.json")
        assert hashlib.sha256(evidence.read_bytes()).hexdigest() == exported["sha256"]
        assert not (workspace / "worker-result.json").exists()
        assert evidence.exists(), session.handle.receipt_path.with_name("stderr.log").read_text() + json.dumps(outcome) + ((private / "broker-outcome.json").read_text() if (private / "broker-outcome.json").exists() else "no broker outcome")
        assert json.loads(evidence.read_text())["scope"] == scope
        if route != "chat":
            assert (private / "provider-request.json").exists(), evidence.read_text() + session.handle.receipt_path.with_name("stderr.log").read_text() + (private / "broker-outcome.json").read_text()
            assert store.totals("fixture-campaign")["settled"] == (4 if route == "codex-goal" else 1)
            assert store.totals("fixture-campaign")["credit_usage"] is None
            if route == "codex-goal":
                result = json.loads(evidence.read_text())["result"]
                assert result["completed"] and not result["failed"]
                assert result["goal_outcome"]["turns_used"] == 2
                assert result["goal_outcome"]["independently_accepted"] is False
                assert [j["verdict"] for j in result["goal_outcome"]["judgements"]] == ["continue", "done"]
        else:
            assert provider.calls
            assert store.totals("fixture-campaign")["observed"] > 0
        assert outcome["guardian"]["exit_verified"]
        assert outcome["effects_reconciled"] is False
    finally:
        session.stop()
        if session.handle.process.poll() is None:
            session.wait(timeout=10)
