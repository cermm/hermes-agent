from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest


def _install_fake_cli_dependencies(monkeypatch, fake_main):
    fake_cli = types.ModuleType("cli")
    fake_cli.main = fake_main
    monkeypatch.setitem(sys.modules, "cli", fake_cli)


def _install_direct_api_fake_cli(monkeypatch):
    import cli as cli_mod

    real_cli = cli_mod.HermesCLI

    class FakeCLI(real_cli):
        def __init__(self, **kwargs):
            self.result_meta_fd = kwargs.get("result_meta_fd")
            self.max_turns = kwargs.get("max_turns") or 90
            self.session_id = "session"
            self.system_prompt = ""
            self.preloaded_skills = []

        def show_banner(self):
            pass

        def show_tools(self):
            pass

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    return cli_mod


def _expected_abnormal_metadata(result_metadata, *, interrupted: bool, api_calls: int = 0):
    return result_metadata.serialize_result_metadata(
        result_metadata.build_result_metadata(
            {
                "completed": False,
                "failed": not interrupted,
                "partial": False,
                "interrupted": interrupted,
                "api_calls": api_calls,
            },
            max_iterations=90,
        )
    )


def test_parser_result_meta_fd_is_opt_in_and_canonical():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()

    absent = parser.parse_args(["chat", "--query", "hello"])
    present = parser.parse_args(
        ["chat", "--query", "hello", "--result-meta-fd", "9"]
    )
    abbreviated = parser.parse_args(
        ["chat", "--query", "hello", "--result-meta-f=9"]
    )

    assert absent.result_meta_fd is None
    assert present.result_meta_fd == 9
    assert abbreviated.result_meta_fd == 9

    for value in ("03", "+3", "-3", "3.0", "true", "2"):
        with pytest.raises(SystemExit) as raised:
            parser.parse_args(
                ["chat", "--query", "hello", "--result-meta-fd", value]
            )
        assert raised.value.code == 2


@pytest.mark.parametrize(
    "duplicate_args",
    [
        ["--result-meta-fd", "7", "--result-meta-fd", "9"],
        ["--result-meta-fd=7", "--result-meta-fd=9"],
        ["--result-meta-fd", "7", "--result-meta-fd=9"],
    ],
)
def test_parser_rejects_duplicate_result_meta_fd(duplicate_args):
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["chat", "--query", "hello", *duplicate_args])

    assert raised.value.code == 2


def test_cmd_chat_claims_forwards_and_closes_result_meta_fd(monkeypatch):
    import hermes_cli.main as main_mod
    from hermes_cli._parser import build_top_level_parser

    read_fd, write_fd = os.pipe()
    captured = {}

    def fake_main(**kwargs):
        owner = kwargs["result_meta_fd"]
        captured["fd"] = owner.fileno()
        assert os.get_inheritable(write_fd) is False

    _install_fake_cli_dependencies(monkeypatch, fake_main)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: None)

    parser, _subparsers, chat = build_top_level_parser()
    chat.set_defaults(func=main_mod.cmd_chat)
    main_mod.cmd_chat(
        parser.parse_args(
            ["chat", "--query", "hello", "--result-meta-fd", str(write_fd)]
        )
    )

    assert captured["fd"] == write_fd
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_cmd_chat_real_bridge_publishes_metadata(monkeypatch, capsys):
    import signal

    import cli as cli_mod
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata
    from hermes_cli._parser import build_top_level_parser

    result = {
        "completed": True,
        "failed": False,
        "partial": False,
        "interrupted": False,
        "api_calls": 1,
        "final_response": "exact response",
        "messages": [],
    }
    real_cli = cli_mod.HermesCLI

    class FakeAgent:
        session_id = "session"
        quiet_mode = False
        suppress_status_output = False
        stream_delta_callback = object()
        tool_gen_callback = object()

        def run_conversation(self, *_args, **_kwargs):
            return dict(result)

    class FakeCLI(real_cli):
        def __init__(self, **kwargs):
            self.result_meta_fd = kwargs.get("result_meta_fd")
            self.max_turns = kwargs.get("max_turns") or 90
            self.agent = FakeAgent()
            self.session_id = "session"
            self.conversation_history = []
            self._active_agent_route_signature = "same"

        def _claim_active_session(self, *_args, **_kwargs):
            return True

        def _release_active_session(self):
            pass

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, _query):
            return {
                "signature": "same",
                "model": None,
                "runtime": None,
                "request_overrides": None,
            }

        def _init_agent(self, **_kwargs):
            return True

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: None)

    parser, _subparsers, chat = build_top_level_parser()
    chat.set_defaults(func=main_mod.cmd_chat)
    read_fd, write_fd = os.pipe()
    with pytest.raises(SystemExit) as raised:
        main_mod.cmd_chat(
            parser.parse_args(
                [
                    "chat",
                    "--query",
                    "hello",
                    "--quiet",
                    "--toolsets",
                    "safe",
                    "--result-meta-fd",
                    str(write_fd),
                ]
            )
        )
    captured = capsys.readouterr()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)
    os.close(read_fd)

    assert raised.value.code == 0
    assert captured.out == "exact response\n"
    assert captured.err == "\nsession_id: session\n"
    assert result_metadata.serialize_result_metadata(
        result_metadata.build_result_metadata(result, max_iterations=90)
    ) == payload


def test_invalid_result_meta_fd_fails_before_config_or_provider(monkeypatch, capsys):
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata
    from hermes_cli._parser import build_top_level_parser

    read_fd, closed_fd = os.pipe()
    os.close(closed_fd)
    monkeypatch.setattr(
        main_mod,
        "_resolve_use_tui",
        lambda _args: (_ for _ in ()).throw(AssertionError("config resolution ran")),
    )
    monkeypatch.setattr(
        main_mod,
        "_has_any_provider_configured",
        lambda: (_ for _ in ()).throw(AssertionError("provider check ran")),
    )
    parser, _subparsers, chat = build_top_level_parser()
    chat.set_defaults(func=main_mod.cmd_chat)

    with pytest.raises(SystemExit) as raised:
        main_mod.cmd_chat(
            parser.parse_args(
                ["chat", "--query", "hello", "--result-meta-fd", str(closed_fd)]
            )
        )

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert captured.err == result_metadata.PUBLIC_ERROR_MESSAGE + "\n"
    os.close(read_fd)


@pytest.mark.parametrize("raw_fd", ["999999", "03"])
def test_main_invalid_result_meta_fd_fails_before_startup(monkeypatch, capsys, raw_fd):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata

    events = []

    def record_event(name):
        def _inner(*_args, **_kwargs):
            events.append(name)
            raise AssertionError(f"{name} must not run before fd claim")
        return _inner

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", record_event("cleanup_quarantined_exes"))
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", record_event("recover_from_interrupted_install"))
    monkeypatch.setattr(config_mod, "get_container_exec_info", record_event("get_container_exec_info"))
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", record_event("prepare_agent_startup"))
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "chat", "--query", "hello", "--result-meta-fd", raw_fd],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert events == []
    assert captured.out == ""
    assert captured.err == result_metadata.PUBLIC_ERROR_MESSAGE + "\n"


@pytest.mark.parametrize(
    ("startup_error", "interrupted"),
    [
        (RuntimeError("pre-turn startup failed"), False),
        (KeyboardInterrupt(), True),
    ],
)
def test_main_startup_error_publishes_abnormal_metadata(
    monkeypatch, startup_error, interrupted
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    monkeypatch.setattr(
        main_mod,
        "_prepare_agent_startup",
        lambda _args: (_ for _ in ()).throw(startup_error),
    )
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            "--query",
            "hello",
            "--result-meta-fd",
            str(write_fd),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)

    assert raised.value.code == 0
    assert payload == _expected_abnormal_metadata(
        result_metadata, interrupted=interrupted
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    ("startup_hook", "raised_error", "interrupted"),
    [
        ("_cleanup_quarantined_exes", RuntimeError("cleanup failed"), False),
        ("_cleanup_quarantined_exes", KeyboardInterrupt(), True),
        ("_recover_from_interrupted_install", RuntimeError("recovery failed"), False),
        ("_recover_from_interrupted_install", KeyboardInterrupt(), True),
        ("build_model_parser", RuntimeError("subparser failed"), False),
        ("build_model_parser", KeyboardInterrupt(), True),
        ("build_gateway_parser", RuntimeError("gateway subparser failed"), False),
        ("build_gateway_parser", KeyboardInterrupt(), True),
    ],
)
def test_main_pre_parser_result_metadata_owner_publishes_abnormal_metadata(
    monkeypatch, startup_hook, raised_error, interrupted
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata

    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda _args: None)
    if startup_hook != "_cleanup_quarantined_exes":
        monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    if startup_hook != "_recover_from_interrupted_install":
        monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)

    def fail(*_args, **_kwargs):
        raise raised_error

    monkeypatch.setattr(main_mod, startup_hook, fail)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            "--query",
            "hello",
            "--result-meta-fd",
            str(write_fd),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)

    assert raised.value.code == 0
    assert payload == _expected_abnormal_metadata(
        result_metadata, interrupted=interrupted
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    "terminator_tail",
    [
        ["--"],
        ["--", "unexpected-positional"],
    ],
)
def test_main_pre_parser_result_metadata_owner_rejects_option_terminator(
    monkeypatch, terminator_tail
):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(
        main_mod,
        "_cleanup_quarantined_exes",
        lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            "--query",
            "hello",
            "--result-meta-fd",
            str(write_fd),
            *terminator_tail,
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    os.set_blocking(read_fd, False)
    try:
        payload = os.read(read_fd, 1)
    except BlockingIOError:
        payload = b""

    assert raised.value.code == 2
    assert payload == b""
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    ("raised_error", "interrupted"),
    [
        (RuntimeError("top-level parser rebuild failed"), False),
        (KeyboardInterrupt(), True),
    ],
)
@pytest.mark.parametrize(
    "query_args",
    [
        ["--query", "hello"],
        ["--query", "hello", "--accept-hooks"],
        ["--query", "hello", "--checkpoints"],
        ["--query", "hello", "--cli"],
        ["--query", "hello", "--continue"],
        ["--query", "hello", "--continue", "session title"],
        ["--query", "hello", "--dev"],
        ["--query", "hello", "--ignore-rules"],
        ["--query", "hello", "--ignore-user-config"],
        ["--query", "hello", "--image", "image.png"],
        ["--query", "hello", "--max-turns", "3"],
        ["--query", "hello", "--model", "model-name"],
        ["--query", "hello", "--no-restore-cwd"],
        ["--query", "hello", "--pass-session-id"],
        ["--query", "hello", "--provider", "provider-name"],
        ["--query", "hello", "--quiet"],
        ["--query", "hello", "--reasoning", "low"],
        ["--query", "hello", "--resume", "session-id"],
        ["--query", "hello", "--skills", "python"],
        ["--query", "hello", "--source", "tool"],
        ["--query", "hello", "--toolsets", "safe"],
        ["--query", "hello", "--verbose"],
        ["--query", "hello", "--worktree"],
        ["--query", "hello", "--yolo"],
        ["--query", "hello", "--safe-mode"],
        ["-Qqhello"],
        ["-m", "model-name", "-qhello"],
        ["-qhello", "-r", "session-id"],
        ["-qhello", "-s", "python"],
        ["-qhello", "-t", "safe"],
        ["-qhello", "-v"],
        ["-qhello", "-w"],
    ],
)
def test_main_second_top_level_parser_failure_publishes_abnormal_metadata(
    monkeypatch, raised_error, interrupted, query_args
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import _parser as parser_mod
    from hermes_cli import result_metadata

    real_build_top_level_parser = parser_mod.build_top_level_parser
    calls = 0

    def build_top_level_parser_once_then_fail():
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_build_top_level_parser()
        raise raised_error

    monkeypatch.setattr(
        parser_mod, "build_top_level_parser", build_top_level_parser_once_then_fail
    )
    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            *query_args,
            "--result-meta-fd",
            str(write_fd),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)

    assert raised.value.code == 0
    assert calls == 2
    assert payload == _expected_abnormal_metadata(
        result_metadata, interrupted=interrupted
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    ("container_error", "interrupted"),
    [
        (RuntimeError("container lookup failed"), False),
        (KeyboardInterrupt(), True),
    ],
)
def test_main_container_lookup_failure_publishes_abnormal_metadata(
    monkeypatch, container_error, interrupted
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)

    def fail_container_lookup():
        raise container_error

    monkeypatch.setattr(config_mod, "get_container_exec_info", fail_container_lookup)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            "--query",
            "hello",
            "--result-meta-fd",
            str(write_fd),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)

    assert raised.value.code == 0
    assert payload == _expected_abnormal_metadata(
        result_metadata, interrupted=interrupted
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize("builder_hook", ["second_top_level_parser", "build_gateway_parser"])
@pytest.mark.parametrize(
    "query_args",
    [
        ["--query", ""],
        ["--query"],
        ["-q", ""],
        ["-q"],
    ],
)
def test_main_empty_query_builder_failure_closes_without_frame(
    monkeypatch, builder_hook, query_args
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import _parser as parser_mod

    if builder_hook == "second_top_level_parser":
        real_build_top_level_parser = parser_mod.build_top_level_parser
        calls = 0

        def build_top_level_parser_once_then_fail():
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_build_top_level_parser()
            raise RuntimeError("top-level parser rebuild failed")

        monkeypatch.setattr(
            parser_mod, "build_top_level_parser", build_top_level_parser_once_then_fail
        )
    else:
        monkeypatch.setattr(
            main_mod,
            "build_gateway_parser",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("gateway subparser failed")
            ),
        )

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            *query_args,
            "--result-meta-fd",
            str(write_fd),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    os.set_blocking(read_fd, False)
    try:
        payload = os.read(read_fd, 1)
    except BlockingIOError:
        payload = b""

    assert raised.value.code == 2
    assert payload == b""
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    "failure_hook",
    [
        "second_top_level_parser",
        "build_gateway_parser",
        "container_lookup",
        "routing_prep",
    ],
)
@pytest.mark.parametrize(
    "invalid_args",
    [
        ["--definitely-unknown-option", "--query", "hello"],
        ["--query", "hello", "--definitely-unknown-option"],
        ["--query", "hello", "--image"],
        ["--model", "--query", "hello"],
        ["--query", "hello", "--model"],
        ["--query", "hello", "--skills"],
        ["--query", "hello", "unexpected-positional"],
    ],
)
def test_main_invalid_query_builder_failure_closes_without_frame(
    monkeypatch, failure_hook, invalid_args
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import _parser as parser_mod

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)

    if failure_hook == "second_top_level_parser":
        real_build_top_level_parser = parser_mod.build_top_level_parser
        calls = 0

        def build_top_level_parser_once_then_fail():
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_build_top_level_parser()
            raise RuntimeError("top-level parser rebuild failed")

        monkeypatch.setattr(
            parser_mod, "build_top_level_parser", build_top_level_parser_once_then_fail
        )
        monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    elif failure_hook == "build_gateway_parser":
        monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
        monkeypatch.setattr(
            main_mod,
            "build_gateway_parser",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("gateway subparser failed")
            ),
        )
    elif failure_hook == "container_lookup":
        monkeypatch.setattr(
            config_mod,
            "get_container_exec_info",
            lambda: (_ for _ in ()).throw(RuntimeError("container lookup failed")),
        )
    else:
        monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)

        class ExplodingChoices:
            def keys(self):
                raise RuntimeError("routing prep failed")

        class ExplodingSubparsers:
            choices = ExplodingChoices()

        real_build_top_level_parser = parser_mod.build_top_level_parser

        def build_top_level_parser_with_exploding_subparsers():
            parser, _subparsers, chat_parser = real_build_top_level_parser()
            return parser, ExplodingSubparsers(), chat_parser

        monkeypatch.setattr(
            parser_mod,
            "build_top_level_parser",
            build_top_level_parser_with_exploding_subparsers,
        )

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "chat", *invalid_args, "--result-meta-fd", str(write_fd)],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    os.set_blocking(read_fd, False)
    try:
        payload = os.read(read_fd, 1)
    except BlockingIOError:
        payload = b""

    assert raised.value.code == 2
    assert payload == b""
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_main_tui_result_metadata_rejects_before_startup_without_frame(
    monkeypatch, capsys
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod

    startup_calls = []
    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)

    def fail_startup(_args):
        startup_calls.append(True)
        raise RuntimeError("TUI startup must not run")

    monkeypatch.setattr(
        main_mod,
        "_prepare_agent_startup",
        fail_startup,
    )
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            "--tui",
            "--query",
            "hello",
            "--result-meta-fd",
            str(write_fd),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, 1)

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert startup_calls == []
    assert payload == b""
    assert captured.err.endswith(
        "Error: --result-meta-fd is available only in the classic CLI.\n"
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    "invalid_args",
    [
        ["--max-turns", "not-an-int"],
    ],
)
def test_main_eligible_parse_error_publishes_one_unknown_failure_frame(
    monkeypatch, invalid_args
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            "--query",
            "hello",
            "--result-meta-fd",
            str(write_fd),
            *invalid_args,
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)

    assert raised.value.code == 0
    assert payload == result_metadata.serialize_result_metadata(
        result_metadata.build_result_metadata(
            {
                "completed": False,
                "failed": True,
                "partial": False,
                "interrupted": False,
                "api_calls": 0,
            },
            max_iterations=0,
        )
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_main_abbreviated_result_meta_fd_parse_error_publishes_one_unknown_failure_frame(
    monkeypatch,
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            "--query",
            "hello",
            f"--result-meta-f={write_fd}",
            "--max-turns",
            "not-an-int",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)

    assert raised.value.code == 0
    assert payload == result_metadata.serialize_result_metadata(
        result_metadata.build_result_metadata(
            {
                "completed": False,
                "failed": True,
                "partial": False,
                "interrupted": False,
                "api_calls": 0,
            },
            max_iterations=0,
        )
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_main_chat_option_terminator_excludes_result_meta_fd(
    monkeypatch,
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            "-q",
            "hello",
            "--",
            "--result-meta-fd",
            str(write_fd),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    os.set_blocking(read_fd, False)
    try:
        payload = os.read(read_fd, 1)
    except BlockingIOError:
        payload = b""

    assert raised.value.code == 2
    assert payload == b""
    os.fstat(write_fd)
    os.close(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    "query_args",
    [
        ["--query", ""],
        ["--query="],
        ["-q", ""],
        ["-q"],
    ],
)
def test_main_empty_query_parse_error_closes_without_frame(monkeypatch, query_args):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            *query_args,
            "--result-meta-fd",
            str(write_fd),
            "--max-turns",
            "not-an-int",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    os.set_blocking(read_fd, False)
    try:
        payload = os.read(read_fd, 1)
    except BlockingIOError:
        payload = b""

    assert raised.value.code == 2
    assert payload == b""
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_main_combined_short_query_parse_error_publishes_one_unknown_failure_frame(
    monkeypatch,
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            "-Qqhello",
            "--result-meta-fd",
            str(write_fd),
            "--max-turns",
            "not-an-int",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)

    assert raised.value.code == 0
    assert payload == result_metadata.serialize_result_metadata(
        result_metadata.build_result_metadata(
            {
                "completed": False,
                "failed": True,
                "partial": False,
                "interrupted": False,
                "api_calls": 0,
            },
            max_iterations=0,
        )
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    "route_args",
    [
        ["--tui", "--cli"],
        ["--tui", "--cl"],
        ["--tu", "--cl"],
    ],
)
def test_main_explicit_cli_parse_error_publishes_one_unknown_failure_frame(
    monkeypatch, route_args
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            *route_args,
            "--query",
            "hello",
            "--result-meta-fd",
            str(write_fd),
            "--max-turns",
            "not-an-int",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)

    assert raised.value.code == 0
    assert payload == result_metadata.serialize_result_metadata(
        result_metadata.build_result_metadata(
            {
                "completed": False,
                "failed": True,
                "partial": False,
                "interrupted": False,
                "api_calls": 0,
            },
            max_iterations=0,
        )
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize("ambient_tui", ["env", "config"])
def test_main_ambient_tui_parse_error_closes_without_frame(monkeypatch, ambient_tui):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("HERMES_TUI", raising=False)
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"display": {"interface": "tui" if ambient_tui == "config" else "cli"}},
    )
    if ambient_tui == "env":
        monkeypatch.setenv("HERMES_TUI", "1")

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "chat",
            "--query",
            "hello",
            "--result-meta-fd",
            str(write_fd),
            "--max-turns",
            "not-an-int",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, 1)

    assert raised.value.code == 2
    assert payload == b""
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    "excluded_args",
    [
        ["chat", "--definitely-unknown-option"],
        ["chat", "--tui", "--query", "hello", "--definitely-unknown-option"],
        ["chat", "--tu", "--query", "hello", "--definitely-unknown-option"],
        ["chat", "--definitely-unknown-option", "--query", "hello"],
        ["chat", "--model", "--query", "hello"],
        ["chat", "--query", "one", "--query", "two", "--definitely-unknown-option"],
        ["chat", "--query", "one", "--que", "two", "--definitely-unknown-option"],
    ],
)
def test_main_excluded_parse_error_closes_without_frame(monkeypatch, excluded_args):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod

    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(config_mod, "get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", *excluded_args, "--result-meta-fd", str(write_fd)],
    )

    with pytest.raises(SystemExit) as raised:
        main_mod.main()
    payload = os.read(read_fd, 1)

    assert raised.value.code == 2
    assert payload == b""
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_main_duplicate_result_meta_fd_rejects_before_claim_or_startup(
    monkeypatch, capsys
):
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata

    events = []

    def record_event(name):
        def _inner(*_args, **_kwargs):
            events.append(name)
            raise AssertionError(f"{name} must not run for duplicate result metadata FDs")

        return _inner

    monkeypatch.setattr(
        result_metadata,
        "claim_result_metadata_fd",
        record_event("claim_result_metadata_fd"),
    )
    monkeypatch.setattr(
        main_mod,
        "_cleanup_quarantined_exes",
        record_event("cleanup_quarantined_exes"),
    )
    monkeypatch.setattr(
        main_mod,
        "_recover_from_interrupted_install",
        record_event("recover_from_interrupted_install"),
    )
    monkeypatch.setattr(
        config_mod,
        "get_container_exec_info",
        record_event("get_container_exec_info"),
    )
    monkeypatch.setattr(
        main_mod,
        "_exec_in_container",
        record_event("exec_in_container"),
    )
    monkeypatch.setattr(
        main_mod,
        "_prepare_agent_startup",
        record_event("prepare_agent_startup"),
    )
    monkeypatch.setattr(
        main_mod,
        "_try_termux_fast_cli_launch",
        record_event("termux_fast_cli_launch"),
    )
    monkeypatch.setattr(
        main_mod,
        "_try_termux_fast_tui_launch",
        record_event("termux_fast_tui_launch"),
    )

    read_fds = []
    write_fds = []
    try:
        for _ in range(2):
            read_fd, write_fd = os.pipe()
            os.set_inheritable(write_fd, True)
            read_fds.append(read_fd)
            write_fds.append(write_fd)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hermes",
                "chat",
                "--query",
                "hello",
                "--result-meta-fd",
                str(write_fds[0]),
                f"--result-meta-fd={write_fds[1]}",
            ],
        )

        with pytest.raises(SystemExit) as raised:
            main_mod.main()

        captured = capsys.readouterr()
        assert raised.value.code == 2
        assert events == []
        assert captured.out == ""
        assert captured.err == result_metadata.PUBLIC_ERROR_MESSAGE + "\n"
        for write_fd in write_fds:
            os.fstat(write_fd)
            assert os.get_inheritable(write_fd) is True
    finally:
        for fd in [*write_fds, *read_fds]:
            os.close(fd)


def test_nonquiet_query_interrupt_publishes_result_metadata(monkeypatch, capsys):
    import cli as cli_mod
    from hermes_cli import result_metadata

    real_cli = cli_mod.HermesCLI

    class FakeConsole:
        def print(self, *_args, **_kwargs):
            pass

    class FakeAgent:
        def get_activity_summary(self):
            return {"api_call_count": 1}

    class FakeCLI(real_cli):
        def __init__(self, **kwargs):
            self.result_meta_fd = kwargs.get("result_meta_fd")
            self.max_turns = kwargs.get("max_turns") or 90
            self.session_id = "session"
            self.console = FakeConsole()
            self.agent = FakeAgent()
            self.conversation_history = []
            self._active_agent_route_signature = "same"

        def _claim_active_session(self, *_args, **_kwargs):
            return True

        def _release_active_session(self):
            pass

        def _show_security_advisories(self):
            pass

        def chat(self, *_args, **_kwargs):
            raise KeyboardInterrupt()

        def _print_exit_summary(self, clear_screen=False):
            print(f"\nsession_id: {self.session_id}", file=sys.stderr)

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)
    read_fd, write_fd = os.pipe()
    with pytest.raises(SystemExit) as raised:
        cli_mod.main(query="hello", quiet=False, result_meta_fd=write_fd)

    captured = capsys.readouterr()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)
    os.close(read_fd)
    metadata = result_metadata.build_result_metadata(
        {
            "completed": False,
            "failed": False,
            "partial": False,
            "interrupted": True,
            "api_calls": 1,
        },
        max_iterations=90,
    )

    assert raised.value.code == 0
    assert captured.err == "\nsession_id: session\n"
    assert result_metadata.serialize_result_metadata(metadata) == payload


@pytest.mark.parametrize("extra_args", [[], ["--tui", "--query", "hello"]])
def test_cmd_chat_rejects_result_meta_fd_without_query_or_with_tui(
    monkeypatch, extra_args
):
    import hermes_cli.main as main_mod
    from hermes_cli._parser import build_top_level_parser

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        main_mod,
        "_has_any_provider_configured",
        lambda: (_ for _ in ()).throw(AssertionError("provider check ran")),
    )
    parser, _subparsers, chat = build_top_level_parser()
    chat.set_defaults(func=main_mod.cmd_chat)

    with pytest.raises(SystemExit) as raised:
        main_mod.cmd_chat(
            parser.parse_args(
                ["chat", *extra_args, "--result-meta-fd", str(write_fd)]
            )
        )

    assert raised.value.code == 2
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_publish_result_metadata_fd_is_silent_and_closes_owner(capsys):
    from cli import HermesCLI
    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()
    cli = HermesCLI.__new__(HermesCLI)
    cli.result_meta_fd = result_metadata.claim_result_metadata_fd(write_fd)
    cli.max_turns = 7

    cli._publish_result_metadata(
        {
            "completed": True,
            "failed": False,
            "partial": False,
            "interrupted": False,
            "api_calls": 2,
            "final_response": "raw response must not leak",
        }
    )

    captured = capsys.readouterr()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)
    assert captured.out == captured.err == ""
    assert b'"api_calls":2' in payload
    assert b"raw response must not leak" not in payload
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_publish_result_metadata_fd_write_failure_is_fixed_and_secret_free(
    monkeypatch, capsys
):
    from cli import HermesCLI
    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()
    cli = HermesCLI.__new__(HermesCLI)
    cli.result_meta_fd = result_metadata.claim_result_metadata_fd(write_fd)
    cli.max_turns = 7

    def fail_write(_fd, _payload):
        raise OSError("secret provider detail")

    monkeypatch.setattr(result_metadata.os, "write", fail_write)

    with pytest.raises(SystemExit) as raised:
        cli._publish_result_metadata(
            {
                "completed": True,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "api_calls": 1,
                "final_response": "raw response must not leak",
            }
        )

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert captured.out == ""
    assert captured.err == result_metadata.PUBLIC_ERROR_MESSAGE + "\n"
    assert "secret provider detail" not in captured.err
    assert "raw response must not leak" not in captured.err
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    ("raised_error", "interrupted"),
    [
        (RuntimeError("secret exception detail /private/path"), False),
        (KeyboardInterrupt(), True),
    ],
)
def test_direct_api_publishes_metadata_on_pre_transfer_construction_error(
    monkeypatch, raised_error, interrupted
):
    import cli as cli_mod
    from hermes_cli import result_metadata

    class FakeCLI:
        def __init__(self, **_kwargs):
            raise raised_error

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    read_fd, write_fd = os.pipe()

    with pytest.raises(SystemExit) as raised:
        cli_mod.main(
            query="x",
            quiet=True,
            toolsets="safe",
            result_meta_fd=write_fd,
        )

    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)
    assert raised.value.code == 0
    assert payload == _expected_abnormal_metadata(
        result_metadata, interrupted=interrupted
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    ("raised_error", "interrupted"),
    [
        (RuntimeError("secret exception detail /private/path"), False),
        (KeyboardInterrupt(), True),
    ],
)
def test_direct_api_publishes_metadata_on_post_transfer_init_error(
    monkeypatch, raised_error, interrupted
):
    import signal

    import cli as cli_mod
    from hermes_cli import result_metadata

    real_cli = cli_mod.HermesCLI

    class FakeCLI(real_cli):
        def __init__(self, **kwargs):
            self.result_meta_fd = kwargs.get("result_meta_fd")
            self.max_turns = kwargs.get("max_turns") or 90
            self.agent = None
            self.session_id = "session"
            self.conversation_history = []
            self._active_agent_route_signature = "same"

        def _claim_active_session(self, *_args, **_kwargs):
            return True

        def _release_active_session(self):
            pass

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, _query):
            return {
                "signature": "same",
                "model": None,
                "runtime": None,
                "request_overrides": None,
            }

        def _init_agent(self, **_kwargs):
            raise raised_error

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    read_fd, write_fd = os.pipe()

    with pytest.raises(SystemExit) as raised:
        cli_mod.main(
            query="hello",
            quiet=True,
            toolsets="safe",
            result_meta_fd=write_fd,
        )
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)

    assert raised.value.code == 0
    assert payload == _expected_abnormal_metadata(
        result_metadata, interrupted=interrupted
    )
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires procfs descriptor paths")
def test_proc_fd_console_startup_exit_publishes_metadata_without_model(tmp_path):
    from hermes_cli import result_metadata

    source_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    consumer = tmp_path / "consumer"
    home.mkdir()
    consumer.mkdir()
    executable = tmp_path / "hermes"
    executable.write_text(
        f"#!{sys.executable}\nfrom hermes_cli.main import main\nmain()\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)

    executable_fd = os.open(executable, os.O_RDONLY | os.O_CLOEXEC)
    consumer_fd = os.open(consumer, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["HERMES_HOME"] = str(home / ".hermes")
    env["PYTHONPATH"] = str(source_root)
    try:
        completed = subprocess.run(
            [
                f"/proc/self/fd/{executable_fd}",
                "--profile",
                "default",
                "chat",
                "--continue",
                "__missing_result_metadata_session__",
                "--query",
                "result-metadata-startup-exit-probe",
                "--quiet",
                "--result-meta-fd",
                str(write_fd),
            ],
            cwd=f"/proc/self/fd/{consumer_fd}",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(write_fd, executable_fd, consumer_fd),
            close_fds=True,
            env=env,
            timeout=30,
        )
        os.close(write_fd)
        write_fd = -1
        payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)
    finally:
        for fd in (write_fd, read_fd, executable_fd, consumer_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    assert completed.returncode == 0
    assert payload == result_metadata.serialize_result_metadata(
        result_metadata.build_result_metadata(
            {
                "completed": False,
                "failed": True,
                "partial": False,
                "interrupted": False,
                "api_calls": 0,
            },
            max_iterations=90,
        )
    )


def test_direct_api_closes_result_meta_fd_on_pre_query_exit(monkeypatch):
    cli_mod = _install_direct_api_fake_cli(monkeypatch)
    read_fd, write_fd = os.pipe()

    with pytest.raises(SystemExit) as raised:
        cli_mod.main(query="x", list_tools=True, result_meta_fd=write_fd)

    assert raised.value.code == 0
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_direct_api_accepts_claimed_result_meta_owner(monkeypatch):
    import cli as cli_mod
    from hermes_cli import result_metadata

    _install_direct_api_fake_cli(monkeypatch)
    read_fd, write_fd = os.pipe()
    owner = result_metadata.claim_result_metadata_fd(write_fd)

    with pytest.raises(SystemExit) as raised:
        cli_mod.main(query="x", list_tools=True, result_meta_fd=owner)

    assert raised.value.code == 0
    assert owner.closed is True
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize("failed", [False, True])
def test_quiet_query_preserves_legacy_exit_without_metadata_and_uses_frame_exit_contract(
    monkeypatch, capsys, failed
):
    import signal

    import cli as cli_mod
    from hermes_cli import result_metadata

    result = {
        "completed": not failed,
        "failed": failed,
        "partial": False,
        "interrupted": False,
        "api_calls": 1,
        "final_response": "" if failed else "exact response",
        "error": "secret failure detail" if failed else "",
        "messages": [],
    }
    real_cli = cli_mod.HermesCLI

    class FakeAgent:
        session_id = "session"
        quiet_mode = False
        suppress_status_output = False
        stream_delta_callback = object()
        tool_gen_callback = object()

        def run_conversation(self, *_args, **_kwargs):
            return dict(result)

        def get_activity_summary(self):
            return {"api_call_count": 1}

    class FakeCLI(real_cli):
        def __init__(self, **kwargs):
            self.result_meta_fd = kwargs.get("result_meta_fd")
            self.max_turns = kwargs.get("max_turns") or 90
            self.agent = FakeAgent()
            self.session_id = "session"
            self.conversation_history = []
            self._active_agent_route_signature = "same"

        def _claim_active_session(self, *_args, **_kwargs):
            return True

        def _release_active_session(self):
            pass

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, _query):
            return {
                "signature": "same",
                "model": None,
                "runtime": None,
                "request_overrides": None,
            }

        def _init_agent(self, **_kwargs):
            return True

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)

    with pytest.raises(SystemExit) as baseline_exit:
        cli_mod.main(query="hello", quiet=True, toolsets="safe")
    baseline = capsys.readouterr()

    read_fd, write_fd = os.pipe()
    with pytest.raises(SystemExit) as metadata_exit:
        cli_mod.main(
            query="hello",
            quiet=True,
            toolsets="safe",
            result_meta_fd=write_fd,
        )
    with_metadata = capsys.readouterr()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)
    os.close(read_fd)

    assert baseline_exit.value.code == (1 if failed else 0)
    assert metadata_exit.value.code == 0
    expected_stdout = "" if failed else "exact response\n"
    assert baseline.out == with_metadata.out == expected_stdout
    expected_stderr = (
        "Error: secret failure detail\n\nsession_id: session\n"
        if failed
        else "\nsession_id: session\n"
    )
    assert baseline.err == with_metadata.err == expected_stderr
    assert result_metadata.serialize_result_metadata(
        result_metadata.build_result_metadata(result, max_iterations=90)
    ) == payload


@pytest.mark.parametrize(
    ("raised_error", "legacy_error", "failure_class", "expected_statuses"),
    [
        (
            KeyboardInterrupt(),
            SystemExit,
            "interrupted",
            (False, False, False, True),
        ),
        (
            RuntimeError("secret exception detail /private/path"),
            RuntimeError,
            "unknown_failure",
            (False, True, False, False),
        ),
    ],
)
def test_quiet_query_abnormal_exit_publishes_closed_failure_frame(
    monkeypatch,
    capsys,
    raised_error,
    legacy_error,
    failure_class,
    expected_statuses,
):
    import json
    import signal

    import cli as cli_mod
    from hermes_cli import result_metadata

    real_cli = cli_mod.HermesCLI

    class FakeAgent:
        session_id = "session"
        quiet_mode = False
        suppress_status_output = False
        stream_delta_callback = object()
        tool_gen_callback = object()

        def run_conversation(self, *_args, **_kwargs):
            raise raised_error

        def get_activity_summary(self):
            return {"api_call_count": 3, "private": "/private/path"}

    class FakeCLI(real_cli):
        def __init__(self, **kwargs):
            self.result_meta_fd = kwargs.get("result_meta_fd")
            self.max_turns = kwargs.get("max_turns") or 90
            self.agent = FakeAgent()
            self.session_id = "session"
            self.conversation_history = []
            self._active_agent_route_signature = "same"

        def _claim_active_session(self, *_args, **_kwargs):
            return True

        def _release_active_session(self):
            pass

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, _query):
            return {
                "signature": "same",
                "model": None,
                "runtime": None,
                "request_overrides": None,
            }

        def _init_agent(self, **_kwargs):
            return True

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)

    with pytest.raises(legacy_error) as baseline_error:
        cli_mod.main(query="hello", quiet=True, toolsets="safe")
    baseline = capsys.readouterr()
    if isinstance(raised_error, KeyboardInterrupt):
        assert baseline_error.value.code == 130

    read_fd, write_fd = os.pipe()
    with pytest.raises(SystemExit) as metadata_exit:
        cli_mod.main(
            query="hello",
            quiet=True,
            toolsets="safe",
            result_meta_fd=write_fd,
        )
    captured = capsys.readouterr()
    payload = os.read(read_fd, result_metadata.MAX_METADATA_BYTES)
    os.close(read_fd)

    decoded = json.loads(payload)
    assert metadata_exit.value.code == 0
    assert decoded["failure_class"] == failure_class
    assert tuple(
        decoded[key] for key in ("completed", "failed", "partial", "interrupted")
    ) == expected_statuses
    assert decoded["api_calls"] == 3
    assert len(payload) <= result_metadata.MAX_METADATA_BYTES
    assert set(decoded) == {
        "schema_version",
        "completed",
        "failed",
        "partial",
        "interrupted",
        "api_calls",
        "failure_class",
    }
    for secret in ("secret exception detail", "/private/path"):
        assert secret not in payload.decode("utf-8")
        assert secret not in captured.out
        assert secret not in captured.err
