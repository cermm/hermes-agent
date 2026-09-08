"""Loaded-agent readback and actual connected-FD regressions, no paid API."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest

from hermes_cli.worker_control import (
    ControlDenied, WorkerChannel, effective_configuration, fingerprint,
    receive_frame, send_frame,
)


@pytest.fixture
def agent():
    return SimpleNamespace(provider="fixture", model="fixture-model", api_mode="chat_completions",
                           tools=[], valid_tool_names=set(), max_tokens=32, max_iterations=3)


def setup_channel(tmp_path, agent, mutate_ack=None, outcome="settled"):
    source = tmp_path / "pinned.py"
    source.write_text("# read-only fixture manifest\n")
    server, client = socket.socketpair()
    server.settimeout(2)
    client.settimeout(2)
    calls, errors = [], []

    def host():
        try:
            hello = receive_frame(server)
            ack = {"schema": "worker-capability-v1", "readback_sha256": fingerprint(hello),
                   "challenge": hello["challenge"]}
            if mutate_ack:
                mutate_ack(ack)
            send_frame(server, ack)
            while True:
                request = receive_frame(server)
                calls.append(request)
                response = {"id": "free-fixture", "object": "chat.completion", "created": 1,
                            "model": "fixture-model", "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "fixture reply"}}]}
                send_frame(server, {"ok": True, "outcome": {"request_id": request["request_id"],
                           "state": outcome, "result": {"response": response}}})
        except (ControlDenied, OSError):
            pass
        except BaseException as exc:
            errors.append(exc)
        finally:
            server.close()

    thread = threading.Thread(target=host, daemon=True)
    thread.start()
    channel = WorkerChannel(client, agent, scope={"board": "fixture", "run": "exact"},
                            expected_configuration=effective_configuration(agent),
                            source_files={str(source): hashlib.sha256(source.read_bytes()).hexdigest()},
                            challenge="a" * 64)
    return channel, calls, thread, errors, source


def request(agent):
    return {"model": agent.model, "messages": [{"role": "user", "content": "fixture"}],
            "max_tokens": 16, "tools": []}


def test_no_paid_request_before_host_acknowledges_actual_configuration(tmp_path, agent):
    channel, calls, thread, errors, _ = setup_channel(tmp_path, agent)
    try:
        with pytest.raises(ControlDenied, match="unavailable"):
            channel.request(request(agent))
        channel.attest()
        result = channel.request(request(agent))
        assert result.choices[0].message.content == "fixture reply"
        assert len(calls) == 1
        assert set(calls[0]) == {"request_id", "model", "messages", "tools", "max_output_tokens"}
    finally:
        channel.close()
        thread.join(2)
    assert not errors


@pytest.mark.parametrize("drift", ["model", "tool_registry", "source", "endpoint", "output_limit"])
def test_effective_drift_fails_before_broker_request(tmp_path, agent, drift):
    channel, calls, thread, errors, source = setup_channel(tmp_path, agent)
    try:
        channel.attest()
        kwargs = request(agent)
        if drift == "model":
            agent.model = "other"
        elif drift == "tool_registry":
            agent.valid_tool_names.add("production_publish")
        elif drift == "source":
            source.write_text("# source changed after acknowledgement\n")
        elif drift == "endpoint":
            kwargs["base_url"] = "https://must-not-contact.invalid"
        else:
            kwargs["max_tokens"] = True
        with pytest.raises(ControlDenied):
            channel.request(kwargs)
        assert channel.closed and not calls
    finally:
        channel.close()
        thread.join(2)
    assert not errors


def test_stale_challenge_and_unknown_effect_cannot_enable_retry(tmp_path, agent):
    channel, calls, thread, _, _ = setup_channel(tmp_path, agent, lambda ack: ack.update(challenge="old"))
    with pytest.raises(ControlDenied, match="acknowledgement"):
        channel.attest()
    thread.join(2)
    assert not calls
    channel, calls, thread, _, _ = setup_channel(tmp_path, agent, outcome="uncertain")
    try:
        channel.attest()
        with pytest.raises(ControlDenied, match="not_settled"):
            channel.request(request(agent))
        with pytest.raises(ControlDenied, match="unavailable"):
            channel.request(request(agent))
        assert len(calls) == 1
    finally:
        channel.close()
        thread.join(2)


def test_readback_uses_real_initialized_agent_and_tools(tmp_path, monkeypatch):
    # Exercise the actual initializer and registry; no AIAgent mock or provider
    # call. The canonical test harness supplies isolated credentials/config.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from run_agent import AIAgent
    actual = AIAgent(provider="openai", model="fixture-model", api_mode="chat_completions",
                     base_url="http://broker.invalid/v1", api_key="not-a-credential",
                     enabled_toolsets=["file"], max_tokens=32, max_iterations=3,
                     quiet_mode=True, skip_context_files=True, skip_memory=True,
                     skip_background_review=True, fallback_model={})
    try:
        config = effective_configuration(actual)
        assert config["model"] == "fixture-model"
        assert {tool["function"]["name"] for tool in config["tools"]} == {
            "read_file", "write_file", "patch", "search_files"}
        assert config["max_tokens"] == 32
        assert config["max_iterations"] == 3
        channel, calls, thread, errors, _ = setup_channel(tmp_path, actual)
        try:
            channel.attest()
            result = channel.request(actual._build_api_kwargs([{"role": "user", "content": "fixture"}]))
            assert result.choices[0].message.content == "fixture reply"
        finally:
            channel.close()
            thread.join(2)
        assert len(calls) == 1 and not errors
    finally:
        actual.close()


@pytest.mark.linux_only
def test_fixed_empty_bytecode_prefix_ignores_valid_but_wrong_cached_code(tmp_path):
    module = tmp_path / "readback_fixture.py"
    module.write_text("VALUE = 'bad-proof'\n")
    stamp = module.stat()
    subprocess.run(["/usr/bin/python3", "-I", "-c",
                    "import py_compile,sys; py_compile.compile(sys.argv[1],doraise=True)", str(module)],
                   check=True, timeout=5, env={"PATH": "/usr/bin:/bin"})
    module.write_text("VALUE = 'goodproof'\n")
    os.utime(module, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    source_hash = hashlib.sha256(module.read_bytes()).hexdigest()
    code = "import sys;sys.path.insert(0,sys.argv[1]);import readback_fixture;print(readback_fixture.VALUE)"
    def load(extra):
        return subprocess.check_output(["/usr/bin/python3", "-I", *extra, "-c", code, str(tmp_path)],
                                       timeout=5, env={"PATH": "/usr/bin:/bin"}, text=True).strip()
    assert load([]) == "bad-proof"  # The pre-fix source-only readback is insufficient.
    assert not Path("/__hermes_uncached__").exists()
    assert load(["-X", "pycache_prefix=/__hermes_uncached__"]) == "goodproof"
    assert hashlib.sha256(module.read_bytes()).hexdigest() == source_hash


def test_native_responses_control_hold_is_not_reported_as_provider_billing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from run_agent import AIAgent
    from agent.error_classifier import classify_api_error
    actual = AIAgent(provider="openai-codex", model="gpt-6-astra", api_mode="codex_responses",
                     base_url="https://chatgpt.com/backend-api/codex", api_key="not-a-credential",
                     enabled_toolsets=[], max_tokens=32, max_iterations=3,
                     quiet_mode=True, skip_context_files=True, skip_memory=True,
                     skip_background_review=True, fallback_model={})
    try:
        channel, calls, thread, errors, _ = setup_channel(tmp_path, actual, outcome="uncertain")
        try:
            channel.attest()
            with pytest.raises(ControlDenied, match="controlled_request_not_settled") as held:
                channel.request(actual._build_api_kwargs([{"role":"user","content":"fixture"}]))
            verdict = classify_api_error(held.value, provider=actual.provider, model=actual.model)
            assert verdict.reason.value != "billing"
            assert channel.closed and len(calls) == 1
            assert set(calls[0]) == {"request_id", "request"}
            assert "extra_headers" not in calls[0]["request"] and "timeout" not in calls[0]["request"]
            assert calls[0]["request"]["model"] == "gpt-6-astra"
            with pytest.raises(ControlDenied, match="unavailable"):
                channel.request(actual._build_api_kwargs([{"role":"user","content":"retry"}]))
            assert len(calls) == 1
        finally:
            channel.close(); thread.join(2)
        assert not errors
    finally:
        actual.close()


@pytest.mark.linux_only
def test_declared_unix_wrapper_cannot_hide_actual_tcp_descriptor(agent):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0)); listener.listen()
    peer = socket.create_connection(listener.getsockname())
    accepted, _ = listener.accept()
    disguised = socket.fromfd(accepted.fileno(), socket.AF_UNIX, socket.SOCK_STREAM)
    disguised.settimeout(1)
    try:
        assert disguised.family == socket.AF_UNIX
        with pytest.raises(ControlDenied, match="connected_unix_stream_required"):
            WorkerChannel(disguised, agent, scope={"fixture":True}, expected_configuration={},
                          source_files={}, challenge="a"*64)
    finally:
        disguised.close(); accepted.close(); peer.close(); listener.close()


def test_client_cleanup_preserves_launch_and_finish_preserves_guardian_export(tmp_path, agent):
    from hermes_cli.worker_control import BrokerClient
    channel, calls, thread, errors, _ = setup_channel(tmp_path, agent)
    try:
        channel.attest()
        facade = BrokerClient(channel)
        facade.close()  # Native SDK client cleanup is not launch revocation.
        assert not channel.closed
        assert channel.request(request(agent)).choices[0].message.content == "fixture reply"
        assert len(calls) == 1
    finally:
        channel.close(); thread.join(2)
    assert not errors
    host, worker = socket.socketpair()
    guardian_copy = socket.fromfd(worker.fileno(), socket.AF_UNIX, socket.SOCK_STREAM)
    host.settimeout(.05); worker.settimeout(1)
    control = WorkerChannel(worker, agent, scope={"fixture":True}, expected_configuration={},
                            source_files={}, challenge="a"*64)
    try:
        control.finish(); control.close()
        with pytest.raises(socket.timeout):
            host.recv(1)  # Trusted guardian still owns its copy through export.
        guardian_copy.close()
        assert host.recv(1) == b""
    finally:
        host.close(); worker.close(); guardian_copy.close()


@pytest.mark.parametrize("verdict,expected", [
    ('{"verdict":"done","reason":"fixture"}', "goal_evidence_complete"),
    ('{"verdict":"blocked","reason":"fixture"}', "blocked"),
    ('{"verdict":"continue","reason":"fixture"}', "blocked_turn_limit"),
    ('{"verdict":"wait","reason":"fixture","wait_seconds":1}', "blocked_wait_limit"),
    ('not valid verdict JSON', "blocked_observation"),
    (None, "blocked_observation"),
])
def test_controlled_goal_dispositions_never_create_acceptance(verdict, expected):
    from hermes_cli.worker_control_bootstrap import run_controlled_task
    calls = []
    class Agent:
        def run_conversation(self, prompt, **kwargs):
            calls.append(prompt)
            return {"final_response":"fixture", "messages":[], "completed":True}
    class Channel:
        def request(self, request):
            assert "tools" not in request
            assert request["model"] == "fixture-model"
            if verdict is None:
                raise ControlDenied("controlled_request_not_settled")
            return SimpleNamespace(output=[SimpleNamespace(type="message",content=[SimpleNamespace(type="output_text",text=verdict)])])
    contract = dict(task_id="task",run_id="run",title="Goal",body="Exact condition",goal_mode=True,
                    goal_max_turns=1,skills=[],model_override=None,provider_override=None,reasoning_effort=None)
    policy = {"prompt":"Work", "run_budget_seconds":10,"task_contract":contract,
              "scope":{"task_id":"task","run_id":"run","task_contract_sha256":fingerprint(contract)},
              "configuration":{"provider":"openai-codex","model":"fixture-model","api_mode":"codex_responses",
                  "wire_options":{"model":"fixture-model","store":False,"tools":[{"type":"function","name":"fixture"}]}}}
    result = run_controlled_task(Agent(), Channel(), policy)
    assert result["goal_outcome"]["status"] == expected
    assert result["completed"] is (expected == "goal_evidence_complete")
    assert result["failed"] is (expected != "goal_evidence_complete")
    assert result["goal_outcome"]["independently_accepted"] is False
    assert len(calls) == 1 and "Exact condition" in calls[0]


def test_pinned_native_task_skill_and_override_identity_before_worker_effect():
    from hermes_cli.worker_control_bootstrap import checked_task_contract
    skill = dict(name="static",path="/pinned/static.md",content="Approved static instructions")
    skill["sha256"] = hashlib.sha256(skill["content"].encode()).hexdigest()
    contract = dict(task_id="task",run_id="run",title="Goal",body="Body",goal_mode=True,goal_max_turns=2,
                    skills=[skill],model_override=None,provider_override=None,reasoning_effort=None)
    policy = {"task_contract":contract,"scope":{"task_id":"task","run_id":"run","task_contract_sha256":fingerprint(contract)},
              "configuration":{"provider":"openai-codex","model":"fixture","api_mode":"codex_responses"}}
    assert checked_task_contract(policy) == contract
    skill["content"] = "changed"
    with pytest.raises(ControlDenied, match="hash_mismatch"):
        checked_task_contract(policy)
    policy["scope"]["task_contract_sha256"] = fingerprint(contract)
    with pytest.raises(ControlDenied, match="skill_identity"):
        checked_task_contract(policy)
    skill["content"] = "Approved static instructions"
    contract["model_override"] = "not-the-effective-model"
    policy["scope"]["task_contract_sha256"] = fingerprint(contract)
    with pytest.raises(ControlDenied, match="override_not_effective"):
        checked_task_contract(policy)


@pytest.mark.parametrize("wait,budget,status", [
    ({"wait_for_seconds":1}, 10, "goal_evidence_complete"),
    ({"wait_for_seconds":20}, 10, "blocked_wait_deadline"),
    ({"wait_on_pid":1}, 10, "blocked_wait_identity"),
    ({"wait_on_session":"unbound-session"}, 10, "blocked_wait_identity"),
])
def test_controlled_goal_wait_respects_time_and_bound_identity(wait, budget, status):
    import time
    from hermes_cli.worker_control_bootstrap import run_controlled_task
    calls = []
    class Agent:
        def run_conversation(self, prompt, **kwargs):
            calls.append(time.monotonic())
            return {"final_response":"fixture", "messages":[], "completed":True}
    class Channel:
        def request(self, request):
            verdict = dict(verdict="wait", reason="wait for fixture", **wait) if len(calls)==1 else {"verdict":"done"}
            return SimpleNamespace(output=[SimpleNamespace(type="message",content=[SimpleNamespace(type="output_text",text=json.dumps(verdict))])])
    contract = dict(task_id="task",run_id="run",title="Goal",body="Body",goal_mode=True,
                    goal_max_turns=2,skills=[],model_override=None,provider_override=None,reasoning_effort=None)
    policy = {"prompt":"Work","run_budget_seconds":budget,"task_contract":contract,
              "scope":{"task_id":"task","run_id":"run","task_contract_sha256":fingerprint(contract)},
              "configuration":{"provider":"openai-codex","model":"fixture","api_mode":"codex_responses",
                                "wire_options":{"model":"fixture","store":False}}}
    result = run_controlled_task(Agent(), Channel(), policy)
    assert result["goal_outcome"]["status"] == status
    if status == "goal_evidence_complete":
        assert len(calls)==2 and calls[1]-calls[0] >= .95
    else:
        assert len(calls)==1 and not result["completed"]


def test_canonical_non_goal_task_preserves_unset_goal_budget():
    from hermes_cli.worker_control_bootstrap import checked_task_contract
    contract = dict(task_id="task",run_id="run",title="Task",body="Body",goal_mode=False,
                    goal_max_turns=None,skills=[],model_override=None,provider_override=None,reasoning_effort=None)
    policy = {"task_contract":contract,"scope":{"task_id":"task","run_id":"run","task_contract_sha256":fingerprint(contract)},
              "configuration":{"provider":"openai-codex","model":"fixture","api_mode":"codex_responses"}}
    assert checked_task_contract(policy)["goal_max_turns"] is None
    contract["goal_mode"] = True
    policy["scope"]["task_contract_sha256"] = fingerprint(contract)
    with pytest.raises(ControlDenied, match="turn_limit"):
        checked_task_contract(policy)


def parent_evidence_policy(goal_mode=False):
    artifact = dict(producer_task_id="maker", producer_run_id="3", path="/pinned/candidate.txt", content="Original failure: Slovak žluťoučký fixture")
    artifact["sha256"] = hashlib.sha256(artifact["content"].encode()).hexdigest()
    parent = dict(task_id="maker", run_id="3", status="done", ended_at=123, summary="Builder evidence", metadata={"finding":"still requires verification"}, artifacts=[artifact])
    contract = dict(task_id="reviewer",run_id="4",title="Review original condition",body="Inspect the pinned candidate",goal_mode=goal_mode,
                    goal_max_turns=2 if goal_mode else None,skills=[],model_override=None,provider_override=None,reasoning_effort=None,parent_handoffs=[parent])
    return {"prompt":"Review", "run_budget_seconds":10,"task_contract":contract,
            "scope":{"task_id":"reviewer","run_id":"4","task_contract_sha256":fingerprint(contract)},
            "configuration":{"provider":"openai-codex","model":"fixture","api_mode":"codex_responses","wire_options":{"model":"fixture","store":False}}}


def test_parent_artifact_bytes_reach_builder_judge_and_continuation():
    from hermes_cli.worker_control_bootstrap import run_controlled_task
    policy=parent_evidence_policy(goal_mode=True)
    artifact=policy["task_contract"]["parent_handoffs"][0]["artifacts"][0]
    prompts=[]; judges=[]
    class Agent:
        def run_conversation(self,prompt,**kwargs):
            prompts.append(prompt)
            return {"final_response":"Evidence inspected", "messages":[], "completed":True}
    class Channel:
        def request(self,request):
            judges.append(request)
            verdict={"verdict":"continue" if len(judges)==1 else "done", "reason":"fixture evidence"}
            return SimpleNamespace(output=[SimpleNamespace(type="message",content=[SimpleNamespace(type="output_text",text=json.dumps(verdict))])])
    result=run_controlled_task(Agent(),Channel(),policy)
    assert result["completed"] and not result["goal_outcome"]["independently_accepted"]
    assert len(prompts)==len(judges)==2
    assert all(artifact["content"] in prompt and artifact["sha256"] in prompt for prompt in prompts)
    assert all(artifact["content"] in request["input"][0]["content"] for request in judges)


@pytest.mark.parametrize("mutation,reason", [
    (lambda p:p["task_contract"]["parent_handoffs"][0]["artifacts"][0].update(content="tampered"), "hash_mismatch"),
    (lambda p:p["task_contract"]["parent_handoffs"][0].update(status="running"), "parent_identity"),
    (lambda p:p["task_contract"]["parent_handoffs"].append(p["task_contract"]["parent_handoffs"][0]), "parent_identity"),
    (lambda p:p["task_contract"]["parent_handoffs"][0]["artifacts"][0].update(path="relative.txt"), "artifact_identity"),
    (lambda p:p["task_contract"]["parent_handoffs"][0]["artifacts"][0].update(content="X"*131073), "artifact_identity"),
])
def test_parent_evidence_drift_scope_and_bounds_hold_before_worker(mutation,reason):
    from hermes_cli.worker_control_bootstrap import checked_task_contract
    policy=parent_evidence_policy()
    mutation(policy)
    if reason!="hash_mismatch":
        policy["scope"]["task_contract_sha256"]=fingerprint(policy["task_contract"])
    with pytest.raises(ControlDenied,match=reason): checked_task_contract(policy)


def test_large_source_inventory_uses_compact_verified_attestation(tmp_path, agent):
    from hermes_cli.worker_control import MAX_FRAME, canonical, readback_payload
    root = tmp_path / "long-source-inventory"; root.mkdir()
    source_files = {}
    for index in range(4000):
        path = root / (("x" * 200) + str(index) + ".py")
        path.write_bytes(b"# independently verified source\n")
        source_files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(canonical(source_files).encode()) > MAX_FRAME
    server, client = socket.socketpair(); server.settimeout(20); client.settimeout(20)
    scope, configuration, challenge = {"run":"large-manifest"}, effective_configuration(agent), "b"*64
    checked = []
    def host():
        try:
            expected = readback_payload(scope=scope, configuration=configuration, source_files=source_files, challenge=challenge)
            hello = receive_frame(server)
            assert hello == expected
            checked.append(hello)
            send_frame(server, {"schema":"worker-capability-v1","readback_sha256":fingerprint(hello),"challenge":challenge})
        finally: server.close()
    thread = threading.Thread(target=host); thread.start()
    channel = WorkerChannel(client, agent, scope=scope, expected_configuration=configuration, source_files=source_files, challenge=challenge)
    try:
        hello = channel.attest()
        assert hello["schema"] == "worker-readback-v2" and "sources" not in hello
        assert hello["source_manifest"] == {"sha256":fingerprint(source_files),"file_count":4000}
        assert len(canonical(hello).encode()) < 10000
        Path(next(reversed(source_files))).write_bytes(b"changed final source")
        with pytest.raises(ControlDenied,match="source_drift"):
            channel.request(request(agent))
    finally:
        channel.close(); thread.join(20)
    assert not thread.is_alive() and checked

def test_bootstrap_reads_utf8_policy_under_ascii_locale(tmp_path):
    import sys

    policy = tmp_path / "utf8-policy.json"
    policy.write_bytes('{"invalid":"\u010derstv\u00e9"}'.encode("utf-8"))
    code = """
import locale
import sys
sys.path.insert(0, sys.argv[1])
from hermes_cli.worker_control import ControlDenied
from hermes_cli.worker_control_bootstrap import run
assert locale.getpreferredencoding(False).lower() in {"ascii", "us-ascii", "ansi_x3.4-1968"}
try:
    run(sys.argv[2], -1)
except ControlDenied as error:
    assert str(error) == "invalid_worker_policy"
else:
    raise AssertionError("invalid policy was accepted")
"""
    environment = dict(os.environ, LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0")
    completed = subprocess.run(
        [sys.executable, "-I", "-X", "utf8=0", "-c", code,
         str(Path(__file__).resolve().parents[2]), str(policy)],
        env=environment, capture_output=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
