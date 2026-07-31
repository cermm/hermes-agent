from __future__ import annotations

import json
import os
import sys
import types

import pytest


def _install_fake_cli_dependencies(monkeypatch, fake_main):
    fake_cli = types.ModuleType("cli")
    fake_cli.main = fake_main
    fake_banner = types.ModuleType("hermes_cli.banner")
    fake_banner.prefetch_update_check = lambda: None
    fake_skills_sync = types.ModuleType("tools.skills_sync")
    fake_skills_sync.sync_skills = lambda quiet=True: None
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.banner", fake_banner)
    monkeypatch.setitem(sys.modules, "tools.skills_sync", fake_skills_sync)


def _install_direct_api_fake_cli(monkeypatch):
    import cli as cli_mod

    real_cli = cli_mod.HermesCLI

    class FakeCLI(real_cli):
        def __init__(self, **kwargs):
            self.result_meta_file = kwargs.get("result_meta_file")
            self.result_meta_fd = kwargs.get("result_meta_fd")
            self.session_id = "session"
            self.system_prompt = ""
            self.preloaded_skills = []

        def show_banner(self):
            pass

        def show_tools(self):
            pass

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    return cli_mod


def test_parser_accepts_result_meta_file_for_chat(tmp_path):
    from hermes_cli._parser import build_top_level_parser

    destination = tmp_path / "result.json"
    parser, _subparsers, _chat = build_top_level_parser()
    args = parser.parse_args(
        [
            "chat",
            "--quiet",
            "--toolsets",
            "safe",
            "--max-turns",
            "1",
            "--source",
            "observer-test",
            "--query",
            "hello",
            "--result-meta-file",
            str(destination),
        ]
    )

    assert args.result_meta_file == str(destination)
    assert args.quiet is True
    assert args.toolsets == "safe"
    assert args.max_turns == 1
    assert args.source == "observer-test"
    assert args.query == "hello"


def test_parser_accepts_result_meta_fd_for_chat():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()
    args = parser.parse_args(["chat", "--query", "hello", "--result-meta-fd", "9"])

    assert args.result_meta_fd == 9


@pytest.mark.parametrize("value", ["03", "+3", "-3", "3.0", "true", "2"])
def test_parser_rejects_noncanonical_result_meta_fd(value):
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["chat", "--query", "hello", "--result-meta-fd", value])

    assert raised.value.code == 2


def test_parser_rejects_both_result_metadata_transports(tmp_path):
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(
            [
                "chat",
                "--query",
                "hello",
                "--result-meta-file",
                str(tmp_path / "result.json"),
                "--result-meta-fd",
                "9",
            ]
        )

    assert raised.value.code == 2


def test_cmd_chat_forwards_result_meta_file(monkeypatch, tmp_path):
    import hermes_cli.main as main_mod
    from hermes_cli._parser import build_top_level_parser

    destination = tmp_path / "result.json"
    captured = {}
    _install_fake_cli_dependencies(monkeypatch, lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)

    parser, _subparsers, chat = build_top_level_parser()
    chat.set_defaults(func=main_mod.cmd_chat)
    args = parser.parse_args(
        ["chat", "--query", "hello", "--result-meta-file", str(destination)]
    )
    main_mod.cmd_chat(args)

    assert captured["result_meta_file"] == str(destination)


def test_cmd_chat_claims_forwards_and_closes_result_meta_fd(monkeypatch):
    import hermes_cli.main as main_mod
    from hermes_cli._parser import build_top_level_parser

    read_fd, fd = os.pipe()
    captured = {}

    def fake_main(**kwargs):
        owner = kwargs["result_meta_fd"]
        captured["fd"] = owner.fileno()
        assert os.get_inheritable(fd) is False

    _install_fake_cli_dependencies(monkeypatch, fake_main)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)

    parser, _subparsers, chat = build_top_level_parser()
    chat.set_defaults(func=main_mod.cmd_chat)
    main_mod.cmd_chat(
        parser.parse_args(["chat", "--query", "hello", "--result-meta-fd", str(fd)])
    )

    assert captured["fd"] == fd
    with pytest.raises(OSError):
        os.fstat(fd)
    os.close(read_fd)


def test_invalid_result_meta_fd_fails_before_config_or_provider(monkeypatch, capsys):
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata
    from hermes_cli._parser import build_top_level_parser

    closed_fd = os.open(os.devnull, os.O_WRONLY)
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


@pytest.mark.parametrize("failure", [RuntimeError("startup"), KeyboardInterrupt()])
def test_cmd_chat_closes_result_meta_fd_on_startup_error_or_interrupt(
    monkeypatch, failure
):
    import hermes_cli.main as main_mod
    from hermes_cli._parser import build_top_level_parser

    read_fd, fd = os.pipe()

    def fail_main(**_kwargs):
        raise failure

    _install_fake_cli_dependencies(monkeypatch, fail_main)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)
    parser, _subparsers, chat = build_top_level_parser()
    chat.set_defaults(func=main_mod.cmd_chat)

    with pytest.raises(type(failure)):
        main_mod.cmd_chat(
            parser.parse_args(["chat", "--query", "hello", "--result-meta-fd", str(fd)])
        )

    with pytest.raises(OSError):
        os.fstat(fd)
    os.close(read_fd)


def test_direct_api_closes_result_meta_fd_on_post_construction_skill_error(monkeypatch):
    cli_mod = _install_direct_api_fake_cli(monkeypatch)
    read_fd, write_fd = os.pipe()

    with pytest.raises(ValueError, match=r"Unknown skill\(s\): __review_missing_skill__"):
        cli_mod.main(
            query="x",
            quiet=True,
            toolsets="safe",
            skills="__review_missing_skill__",
            result_meta_fd=write_fd,
        )

    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_direct_api_closes_result_meta_fd_on_pre_query_list_tools_exit(monkeypatch):
    cli_mod = _install_direct_api_fake_cli(monkeypatch)
    read_fd, write_fd = os.pipe()

    with pytest.raises(SystemExit) as raised:
        cli_mod.main(query="x", list_tools=True, result_meta_fd=write_fd)

    assert raised.value.code == 0
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


@pytest.mark.parametrize(
    "argv",
    [
        ["chat", "--result-meta-file", "/tmp/result.json"],
        ["chat", "--tui", "--query", "hello", "--result-meta-file", "/tmp/result.json"],
    ],
)
def test_cmd_chat_rejects_non_query_or_tui_mode_before_provider(monkeypatch, argv):
    import hermes_cli.main as main_mod
    from hermes_cli._parser import build_top_level_parser

    monkeypatch.setattr(
        main_mod,
        "_has_any_provider_configured",
        lambda: (_ for _ in ()).throw(AssertionError("provider check ran")),
    )
    parser, _subparsers, chat = build_top_level_parser()
    chat.set_defaults(func=main_mod.cmd_chat)

    with pytest.raises(SystemExit) as raised:
        main_mod.cmd_chat(parser.parse_args(argv))

    assert raised.value.code == 2


def test_cmd_chat_rejects_existing_destination_before_provider(monkeypatch, tmp_path):
    import hermes_cli.main as main_mod
    from hermes_cli._parser import build_top_level_parser

    destination = tmp_path / "result.json"
    destination.write_text("keep", encoding="utf-8")
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
                ["chat", "--query", "hello", "--result-meta-file", str(destination)]
            )
        )

    assert raised.value.code == 2
    assert destination.read_text(encoding="utf-8") == "keep"


def test_publish_result_metadata_is_silent_and_uses_effective_max_turns(tmp_path, capsys):
    from cli import HermesCLI

    destination = tmp_path / "result.json"
    cli = HermesCLI.__new__(HermesCLI)
    cli.result_meta_file = str(destination)
    cli.max_turns = 7

    cli._publish_result_metadata(
        {
            "completed": True,
            "failed": False,
            "partial": False,
            "interrupted": False,
            "api_calls": 2,
        }
    )

    assert capsys.readouterr().out == ""
    assert '"api_calls":2' in destination.read_text(encoding="utf-8")


def test_publish_failure_has_fixed_diagnostic_and_nonzero_exit(
    monkeypatch, tmp_path, capsys
):
    from cli import HermesCLI
    from hermes_cli import result_metadata

    destination = tmp_path / "result.json"
    cli = HermesCLI.__new__(HermesCLI)
    cli.result_meta_file = str(destination)
    cli.max_turns = 7

    def fail(*_args, **_kwargs):
        raise result_metadata.ResultMetadataError(f"secret path: {destination}")

    monkeypatch.setattr(result_metadata, "write_result_metadata", fail)

    with pytest.raises(SystemExit) as raised:
        cli._publish_result_metadata({"completed": True, "api_calls": 1})

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert captured.out == ""
    assert captured.err == result_metadata.PUBLIC_ERROR_MESSAGE + "\n"
    assert str(destination) not in captured.err


def test_publish_result_metadata_fd_is_silent_and_closes_owner(capsys):
    from cli import HermesCLI
    from hermes_cli import result_metadata

    read_fd, fd = os.pipe()
    cli = HermesCLI.__new__(HermesCLI)
    cli.result_meta_file = None
    cli.result_meta_fd = result_metadata.claim_result_metadata_fd(fd)
    cli.max_turns = 7

    cli._publish_result_metadata(
        {
            "completed": True,
            "failed": False,
            "partial": False,
            "interrupted": False,
            "api_calls": 2,
        }
    )

    assert capsys.readouterr().out == ""
    assert b'"api_calls":2' in os.read(read_fd, 1024)
    with pytest.raises(OSError):
        os.fstat(fd)
    os.close(read_fd)


@pytest.mark.parametrize("fault", ["short", "epipe", "eagain"])
def test_publish_result_metadata_fd_closes_owner_on_failure(monkeypatch, capsys, fault):
    import errno

    from cli import HermesCLI
    from hermes_cli import result_metadata

    read_fd, fd = os.pipe()
    cli = HermesCLI.__new__(HermesCLI)
    cli.result_meta_file = None
    cli.result_meta_fd = result_metadata.claim_result_metadata_fd(fd)
    cli.max_turns = 7
    def fail_write(_fd, payload):
        if fault == "short":
            return len(payload) - 1
        error_number = errno.EPIPE if fault == "epipe" else errno.EAGAIN
        raise OSError(error_number, fault)

    monkeypatch.setattr(result_metadata.os, "write", fail_write)
    monkeypatch.setattr(
        result_metadata,
        "write_result_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("file fallback attempted")
        ),
    )

    with pytest.raises(SystemExit) as raised:
        cli._publish_result_metadata({"completed": True, "api_calls": 1})

    assert raised.value.code == 1
    assert capsys.readouterr().err == result_metadata.PUBLIC_ERROR_MESSAGE + "\n"
    with pytest.raises(OSError):
        os.fstat(fd)
    os.close(read_fd)


def test_quiet_query_stdout_is_byte_identical_and_publishes_once(monkeypatch, tmp_path, capsys):
    import cli as cli_mod
    import signal
    from hermes_cli import result_metadata

    result = {
        "completed": True,
        "failed": False,
        "partial": False,
        "interrupted": False,
        "api_calls": 1,
        "final_response": "exact response",
        "messages": [],
    }
    write_calls = 0
    real_write = result_metadata.write_result_metadata

    def counted_write(*args, **kwargs):
        nonlocal write_calls
        write_calls += 1
        return real_write(*args, **kwargs)

    monkeypatch.setattr(result_metadata, "write_result_metadata", counted_write)
    RealHermesCLI = cli_mod.HermesCLI

    class FakeAgent:
        session_id = "session"
        quiet_mode = False
        _stream_callback = object()

        def run_conversation(self, *_args, **_kwargs):
            return dict(result)

    class FakeCLI(RealHermesCLI):
        def __init__(self, **kwargs):
            self.result_meta_file = kwargs.get("result_meta_file")
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
            return {"signature": "same", "model": None, "runtime": None}

        def _init_agent(self, **_kwargs):
            return True


    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)

    with pytest.raises(SystemExit) as baseline_exit:
        cli_mod.main(query="hello", quiet=True, toolsets="safe")
    baseline = capsys.readouterr()

    destination = tmp_path / "result.json"
    with pytest.raises(SystemExit) as metadata_exit:
        cli_mod.main(
            query="hello",
            quiet=True,
            toolsets="safe",
            result_meta_file=str(destination),
        )
    with_metadata = capsys.readouterr()

    read_fd, write_fd = os.pipe()
    with pytest.raises(SystemExit) as fd_exit:
        cli_mod.main(
            query="hello",
            quiet=True,
            toolsets="safe",
            result_meta_fd=write_fd,
        )
    with_fd = capsys.readouterr()
    fd_metadata = json.loads(os.read(read_fd, 1024))
    os.close(read_fd)

    assert baseline_exit.value.code == metadata_exit.value.code == fd_exit.value.code == 0
    assert baseline.out == with_metadata.out == with_fd.out == "exact response\n"
    assert baseline.err == with_metadata.err == with_fd.err == "\nsession_id: session\n"
    assert write_calls == 1
    expected = result_metadata.build_result_metadata(result, max_iterations=90)
    assert expected == json.loads(destination.read_bytes()) == fd_metadata


def test_native_windows_result_meta_fd_fails_before_config_or_provider(monkeypatch, capsys):
    import hermes_cli.main as main_mod
    from hermes_cli import result_metadata
    from hermes_cli._parser import build_top_level_parser

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(result_metadata.os, "name", "nt")
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
                ["chat", "--query", "hello", "--result-meta-fd", str(write_fd)]
            )
        )

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert captured.err == result_metadata.PUBLIC_ERROR_MESSAGE + "\n"
    os.close(read_fd)
    os.close(write_fd)


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
    argv = ["chat", *extra_args, "--result-meta-fd", str(write_fd)]

    with pytest.raises(SystemExit) as raised:
        main_mod.cmd_chat(parser.parse_args(argv))

    assert raised.value.code == 2
    with pytest.raises(OSError):
        os.fstat(write_fd)
    os.close(read_fd)


def test_publish_result_metadata_fd_close_fault_is_publication_failure(monkeypatch, capsys):
    from cli import HermesCLI
    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()
    cli = HermesCLI.__new__(HermesCLI)
    cli.result_meta_file = None
    cli.result_meta_fd = result_metadata.claim_result_metadata_fd(write_fd)
    cli.max_turns = 7
    real_close = result_metadata.os.close

    def fail_target_close(fd):
        if fd == write_fd:
            raise OSError("close fault")
        return real_close(fd)

    monkeypatch.setattr(result_metadata.os, "close", fail_target_close)
    with pytest.raises(SystemExit) as raised:
        cli._publish_result_metadata({"completed": True, "api_calls": 1})

    assert raised.value.code == 1
    assert capsys.readouterr().err == result_metadata.PUBLIC_ERROR_MESSAGE + "\n"
    monkeypatch.setattr(result_metadata.os, "close", real_close)
    real_close(write_fd)
    real_close(read_fd)
