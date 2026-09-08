"""Worker-side configuration readback and the connected, credential-free broker.

This module grants no execution authority. The trusted launcher must supply the
connected descriptor only inside worker_guard containment, and the host must
authenticate that launch before acknowledging the readback. No pathname socket,
endpoint, credential, budget policy, or publication command is accepted here.
"""
from __future__ import annotations

import hashlib
import json
import math
import socket
import struct
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace


MAX_FRAME = 1_048_576


class ControlDenied(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ControlDenied("duplicate_control_field")
        value[key] = item
    return value


def _read(connection, count):
    result = bytearray()
    while len(result) < count:
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise ControlDenied("control_channel_closed")
        result.extend(chunk)
    return bytes(result)


def receive_frame(connection):
    count = struct.unpack("!I", _read(connection, 4))[0]
    if not 0 < count <= MAX_FRAME:
        raise ControlDenied("invalid_control_frame_size")
    try:
        return json.loads(_read(connection, count).decode(), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ControlDenied("invalid_constant")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ControlDenied("invalid_control_frame") from exc


def send_frame(connection, value):
    data = canonical(value).encode()
    if not 0 < len(data) <= MAX_FRAME:
        raise ControlDenied("invalid_control_frame_size")
    connection.sendall(struct.pack("!I", len(data)) + data)


def source_readback(files):
    """Hash files named by the fixed, read-only bootstrap manifest.

    The host checks both the launch's mount manifest and these hashes; worker
    assertions alone are never sufficient to authenticate loaded code.
    """
    if not isinstance(files, dict) or not files:
        raise ControlDenied("missing_source_manifest")
    result = {}
    for name, expected in files.items():
        path = Path(name)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ControlDenied("invalid_source_path")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ControlDenied("source_drift")
        result[name] = actual
    return result


def candidate_sources(root):
    """Complete first-party Python inventory, matching deployment readiness."""
    root = Path(root).resolve(strict=True)
    excluded = {".git", ".venv", "venv", "node_modules", "__pycache__"}
    files = [p for p in root.rglob("*.py") if not excluded.intersection(p.relative_to(root).parts)]
    if any(p.is_symlink() for p in files):
        raise ControlDenied("symlinked_worker_source")
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def readback_payload(*, scope, configuration, source_files, challenge):
    """Independently read every source file, then attest its compact identity."""
    sources = source_readback(source_files)
    payload = {"schema": "worker-readback-v2", "scope": scope, "configuration": configuration,
               "source_manifest": {"sha256": fingerprint(sources), "file_count": len(sources)},
               "challenge": challenge}
    if not 0 < len(canonical(payload).encode()) <= MAX_FRAME:
        raise ControlDenied("invalid_control_frame_size")
    return payload


def effective_configuration(agent):
    """Inspect the initialized agent, including actual registered tool schemas."""
    tools = agent.tools
    if not isinstance(tools, list):
        raise ControlDenied("invalid_loaded_tools")
    names = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ControlDenied("unsupported_loaded_tool")
        name = tool.get("function", {}).get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ControlDenied("invalid_loaded_tool_name")
        names.append(name)
    if set(names) != set(agent.valid_tool_names):
        raise ControlDenied("tool_registry_drift")
    if agent.api_mode not in {"chat_completions", "codex_responses"}:
        raise ControlDenied("unsupported_worker_api")
    result = {
        "provider": agent.provider, "model": agent.model, "api_mode": agent.api_mode,
        "tools": json.loads(canonical(tools)),
        "max_iterations": agent.max_iterations,
        "max_tokens": agent.max_tokens,
    }
    if agent.api_mode == "codex_responses":
        if agent.provider != "openai-codex":
            raise ControlDenied("unsupported_responses_worker_provider")
        # Inspect the native transport's effective request settings, not just
        # raw config hints. This constructs no client request or tool effect.
        wire = agent._build_api_kwargs([{"role": "user", "content": "configuration-readback"}])
        result.update(wire_options={k: v for k, v in wire.items() if k not in
                                   {"input", "instructions", "prompt_cache_key", "timeout", "extra_headers"}},
                      wire_tools=wire.get("tools", []), wire_reasoning=wire.get("reasoning"),
                      reasoning_config=getattr(agent, "reasoning_config", None),
                      accounting_mode="request_quota", output_tokens_hard_capped=False)
    return result


class WorkerChannel:
    """One launch-bound channel; any unknown outcome permanently closes it.

    The broker retains the request reservation after a lost acknowledgement.
    Retrying an agent turn cannot turn that loss into a second paid request.
    """
    def __init__(self, connection: socket.socket, agent, *, scope: dict,
                 expected_configuration: dict, source_files: dict, challenge: str):
        if (connection.getsockopt(socket.SOL_SOCKET, socket.SO_DOMAIN) != socket.AF_UNIX
                or connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM):
            raise ControlDenied("connected_unix_stream_required")
        connection.getpeername()
        if not isinstance(scope, dict) or not scope or not isinstance(challenge, str) or len(challenge) < 32:
            raise ControlDenied("missing_launch_binding")
        if connection.gettimeout() is None:
            raise ControlDenied("bounded_channel_timeout_required")
        self.connection, self.agent = connection, agent
        self.configuration = json.loads(canonical(expected_configuration))
        self.sources = dict(source_files)
        self.scope = json.loads(canonical(scope))
        self.challenge = challenge
        self.ready = False
        self.closed = False
        self.lock = threading.Lock()

    def attest(self):
        with self.lock:
            if self.ready or self.closed:
                raise ControlDenied("channel_already_used")
            try:
                observed = effective_configuration(self.agent)
                if observed != self.configuration:
                    raise ControlDenied("effective_configuration_mismatch")
                hello = readback_payload(scope=self.scope, configuration=observed,
                                         source_files=self.sources, challenge=self.challenge)
                send_frame(self.connection, hello)
                ack = receive_frame(self.connection)
                if ack != {"schema": "worker-capability-v1", "readback_sha256": fingerprint(hello),
                           "challenge": self.challenge}:
                    raise ControlDenied("invalid_host_acknowledgement")
                self.ready = True
                return hello
            except BaseException:
                self.close()
                raise

    def request(self, kwargs):
        with self.lock:
            if not self.ready or self.closed:
                raise ControlDenied("worker_capabilities_unavailable")
            try:
                if effective_configuration(self.agent) != self.configuration:
                    raise ControlDenied("effective_configuration_drift")
                source_readback(self.sources)
                if self.configuration["api_mode"] == "codex_responses":
                    return self._request_responses(kwargs)
                allowed = {"model", "messages", "tools", "max_tokens", "max_completion_tokens", "stream", "timeout"}
                if not isinstance(kwargs, dict) or set(kwargs) - allowed or kwargs.get("stream", False) is not False:
                    raise ControlDenied("unsupported_broker_request")
                # Hermes' normal transport supplies a client timeout. This is
                # not remote routing or spending authority: bound our local
                # wait, while the host independently enforces its deadline.
                timeout = kwargs.get("timeout")
                if timeout is not None:
                    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
                        raise ControlDenied("invalid_client_timeout")
                    self.connection.settimeout(min(timeout, self.connection.gettimeout()))
                if kwargs.get("model") != self.configuration["model"]:
                    raise ControlDenied("request_model_drift")
                # Providers may omit tools on the final response call, but cannot add or change one.
                tools = kwargs.get("tools", [])
                if tools not in ([], self.configuration["tools"]):
                    raise ControlDenied("request_tool_drift")
                limits = [kwargs[k] for k in ("max_tokens", "max_completion_tokens") if k in kwargs]
                if len(limits) != 1 or type(limits[0]) is not int or not 0 < limits[0] <= self.configuration["max_tokens"]:
                    raise ControlDenied("invalid_request_output_limit")
                messages = kwargs.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise ControlDenied("invalid_request_messages")
                request_id = uuid.uuid4().hex
                send_frame(self.connection, {"request_id": request_id, "model": kwargs["model"],
                           "messages": messages, "tools": tools, "max_output_tokens": limits[0]})
                reply = receive_frame(self.connection)
                outcome = reply.get("outcome") if isinstance(reply, dict) and reply.get("ok") is True else None
                if not isinstance(outcome, dict) or outcome.get("request_id") != request_id or outcome.get("state") != "settled":
                    raise ControlDenied("paid_request_not_settled")
                response = outcome.get("result", {}).get("response")
                if not isinstance(response, dict):
                    raise ControlDenied("invalid_broker_response")
                from openai.types.chat import ChatCompletion
                return ChatCompletion.model_validate(response)
            except BaseException:
                self.close()
                raise

    def _request_responses(self, kwargs):
        allowed = {"model", "instructions", "input", "tools", "store", "reasoning", "include",
                   "prompt_cache_key", "timeout", "extra_headers", "tool_choice",
                   "parallel_tool_calls", "text", "service_tier"}
        if not isinstance(kwargs, dict) or set(kwargs) - allowed:
            raise ControlDenied("unsupported_responses_request")
        if kwargs.get("model") != self.configuration["model"] or kwargs.get("store") is not False:
            raise ControlDenied("responses_scope_mismatch")
        if kwargs.get("tools", []) not in ([], self.configuration["wire_tools"]):
            raise ControlDenied("responses_tool_drift")
        if kwargs.get("reasoning") != self.configuration["wire_reasoning"]:
            raise ControlDenied("responses_reasoning_drift")
        fixed = {k: v for k, v in kwargs.items() if k not in
                 {"input", "instructions", "prompt_cache_key", "timeout", "extra_headers"}}
        expected_fixed = dict(self.configuration["wire_options"])
        if not kwargs.get("tools", []):
            fixed.pop("tools", None); expected_fixed.pop("tools", None)
        if fixed != expected_fixed:
            raise ControlDenied("responses_option_drift")
        headers = kwargs.get("extra_headers", {})
        if not isinstance(headers, dict) or set(headers) - {"session_id", "x-client-request-id"}:
            raise ControlDenied("worker_cannot_select_provider_headers")
        timeout = kwargs.get("timeout")
        if timeout is not None:
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
                raise ControlDenied("invalid_client_timeout")
            self.connection.settimeout(min(timeout, self.connection.gettimeout()))
        request = json.loads(canonical({k: v for k, v in kwargs.items() if k not in {"timeout", "extra_headers"}}))
        request_id = uuid.uuid4().hex
        send_frame(self.connection, {"request_id": request_id, "request": request})
        reply = receive_frame(self.connection)
        outcome = reply.get("outcome") if isinstance(reply, dict) and reply.get("ok") is True else None
        if not isinstance(outcome, dict) or outcome.get("request_id") != request_id or outcome.get("state") != "settled":
            raise ControlDenied("controlled_request_not_settled")
        if not isinstance(outcome.get("response"), dict):
            raise ControlDenied("invalid_quota_response")
        from openai.types.responses import Response
        return Response.model_validate(outcome["response"])

    def finish(self):
        # Leave the guardian's duplicate alive until PID1 has exported outputs.
        self.closed = True
        self.ready = False
        self.connection.close()

    def close(self):
        self.closed = True
        self.ready = False
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


class BrokerClient:
    """OpenAI-shaped request facade used by the supported confined worker."""
    def __init__(self, channel):
        self.channel = channel
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        return self.channel.request(kwargs)

    def is_closed(self):
        return self.channel.closed

    def close(self):
        # Bootstrap owns the launch capability and its graceful export lifetime.
        pass
