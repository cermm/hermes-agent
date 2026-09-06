"""Descriptor ownership and eligibility before CLI startup or argument parsing."""

import sys

from hermes_cli.main_tui_launch import _resolve_use_tui


def _claim_result_metadata_args(args):
    """Claim chat result-metadata descriptor before startup side effects."""

    from hermes_cli import result_metadata

    owner = None
    raw_fd = getattr(args, "result_meta_fd", None)
    if raw_fd is None:
        return None
    if isinstance(raw_fd, result_metadata.ResultMetadataFD):
        return raw_fd
    try:
        owner = result_metadata.claim_result_metadata_fd(raw_fd)
    except result_metadata.ResultMetadataError:
        print(result_metadata.PUBLIC_ERROR_MESSAGE, file=sys.stderr)
        raise SystemExit(2) from None
    args.result_meta_fd = owner
    return owner


def _resolve_argv_option_action(token: str, actions: dict):
    """Resolve one raw argv option like argparse, including unique long abbrevs."""

    option = token.split("=", 1)[0]
    action = actions.get(option)
    attached = "=" in token
    attached_value = token.split("=", 1)[1] if attached else None
    ambiguous = False
    if action is None and option.startswith("--"):
        matches = []
        for candidate, candidate_action in actions.items():
            if candidate.startswith(option) and not any(
                match is candidate_action for match in matches
            ):
                matches.append(candidate_action)
        if len(matches) == 1:
            action = matches[0]
        elif len(matches) > 1:
            ambiguous = True
    if action is None and option.startswith("-") and not option.startswith("--"):
        action = actions.get(option[:2])
        attached = action is not None and len(option) > 2
        attached_value = option[2:] if attached else None
    if (
        action is not None
        and option.startswith("-")
        and not option.startswith("--")
        and len(option) > 2
        and action.nargs == 0
    ):
        for position in range(2, len(option)):
            clustered_action = actions.get(f"-{option[position]}")
            if clustered_action is None:
                return None, False, None, ambiguous
            if clustered_action.nargs != 0:
                action = clustered_action
                attached_value = option[position + 1 :]
                if not attached_value and "=" in token:
                    attached_value = token.split("=", 1)[1]
                    attached = True
                else:
                    attached = bool(attached_value)
                break
    return action, attached, attached_value, ambiguous


def _early_claim_result_metadata_argv(
    argv: list[str], *, include_parser_state: bool = False
):
    """Claim --result-meta-fd from raw argv before config/container startup."""

    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    top_actions = parser._option_string_actions
    chat_actions = chat_parser._option_string_actions
    command_index = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return None
        if token == "chat":
            command_index = index
            break
        if not token.startswith("-"):
            return None
        action, attached, _attached_value, ambiguous = _resolve_argv_option_action(
            token, top_actions
        )
        if action is None or ambiguous:
            return None
        if attached or action.nargs == 0:
            index += 1
            continue
        if action.nargs == "?":
            index += (
                2
                if index + 1 < len(argv) and not argv[index + 1].startswith("-")
                else 1
            )
            continue
        if action.nargs not in {None, 1} or index + 1 >= len(argv):
            return None
        if argv[index + 1].startswith("-"):
            return None
        index += 2

    if command_index is None:
        return None

    raw_values = []
    index = command_index + 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            break
        action, attached, attached_value, ambiguous = _resolve_argv_option_action(
            token, chat_actions
        )
        if ambiguous and any(
            candidate.startswith(token.split("=", 1)[0])
            and candidate_action.dest == "result_meta_fd"
            for candidate, candidate_action in chat_actions.items()
        ):
            from hermes_cli import result_metadata

            print(result_metadata.PUBLIC_ERROR_MESSAGE, file=sys.stderr)
            raise SystemExit(2)
        if action is None:
            index += 1
            continue
        if action.dest == "result_meta_fd":
            if attached:
                raw_values.append(attached_value)
                index += 1
                continue
            raw_values.append(argv[index + 1] if index + 1 < len(argv) else None)
            index += 2
            continue
        if attached or action.nargs == 0:
            index += 1
            continue
        if action.nargs == "?":
            index += (
                2
                if index + 1 < len(argv) and not argv[index + 1].startswith("-")
                else 1
            )
            continue
        if (
            action.nargs in {None, 1}
            and index + 1 < len(argv)
            and not argv[index + 1].startswith("-")
        ):
            index += 2
            continue
        index += 1

    if not raw_values:
        return None

    from hermes_cli import result_metadata

    if len(raw_values) > 1:
        print(result_metadata.PUBLIC_ERROR_MESSAGE, file=sys.stderr)
        raise SystemExit(2)

    raw_value = raw_values[0]
    if raw_value is None:
        print(result_metadata.PUBLIC_ERROR_MESSAGE, file=sys.stderr)
        raise SystemExit(2)

    from argparse import ArgumentTypeError

    from hermes_cli.result_metadata import parse_result_metadata_fd

    try:
        owner = result_metadata.claim_result_metadata_fd(parse_result_metadata_fd(raw_value))
        if include_parser_state:
            return owner, parser, chat_parser
        return owner
    except (ValueError, ArgumentTypeError, result_metadata.ResultMetadataError):
        print(result_metadata.PUBLIC_ERROR_MESSAGE, file=sys.stderr)
        raise SystemExit(2) from None


def _eligible_result_metadata_parse_failure_argv(
    argv: list[str], parser, chat_parser
) -> bool:
    """Recognize an unambiguous classic-CLI query after full parsing failed."""
    cli_requested = False
    tui_requested = False
    command_index = None
    top_actions = parser._option_string_actions
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return False
        if token == "chat":
            command_index = index
            break
        if not token.startswith("-"):
            return False
        action, attached, _attached_value, ambiguous = _resolve_argv_option_action(
            token, top_actions
        )
        if action is None or ambiguous:
            return False
        if action.dest == "cli":
            cli_requested = True
        elif action.dest == "tui":
            tui_requested = True
        if attached or action.nargs == 0:
            index += 1
            continue
        if action.nargs == "?":
            index += (
                2
                if index + 1 < len(argv) and not argv[index + 1].startswith("-")
                else 1
            )
            continue
        if action.nargs not in {None, 1} or index + 1 >= len(argv):
            return False
        if argv[index + 1].startswith("-"):
            return False
        index += 2

    if command_index is None:
        return False

    query_values = []
    chat_actions = chat_parser._option_string_actions
    index = command_index + 1
    while index < len(argv):
        token = argv[index]
        action, attached, attached_value, ambiguous = _resolve_argv_option_action(
            token, chat_actions
        )
        if action is None or ambiguous:
            return False
        if action.dest == "cli":
            cli_requested = True
        elif action.dest == "tui":
            tui_requested = True
        if action.dest == "query":
            if attached:
                if not attached_value:
                    return False
                query_values.append(attached_value)
                index += 1
                continue
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                return False
            if not argv[index + 1]:
                return False
            query_values.append(argv[index + 1])
            index += 2
            continue
        if attached or action.nargs == 0:
            index += 1
            continue
        if action.nargs == "?":
            index += (
                2
                if index + 1 < len(argv) and not argv[index + 1].startswith("-")
                else 1
            )
            continue
        if action.nargs not in {None, 1}:
            return False
        if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
            return False
        index += 2

    if len(query_values) != 1:
        return False

    from types import SimpleNamespace

    return not _resolve_use_tui(
        SimpleNamespace(cli=cli_requested, tui=tui_requested)
    )


def _eligible_result_metadata_owner_argv(
    argv: list[str], parser=None, chat_parser=None
) -> bool:
    """Best-effort validation for an already-claimed result-metadata owner."""

    if parser is None or chat_parser is None:
        return _eligible_result_metadata_owner_raw_argv(argv)
    return _eligible_result_metadata_parse_failure_argv(argv, parser, chat_parser)


def _eligible_result_metadata_owner_raw_argv(argv: list[str]) -> bool:
    """Validate an already-claimed owner without rebuilding parser state.

    This fallback is deliberately conservative.  It is used only after the
    result metadata FD has already been claimed from raw argv, so it needs to
    distinguish valid classic CLI query ownership from known no-publication
    cases such as empty queries or TUI routing when parser construction itself
    is no longer trustworthy.
    """

    cli_requested = False
    tui_requested = False
    command_index = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return False
        if token == "chat":
            command_index = index
            break
        if token in {"--cli", "--cl"}:
            cli_requested = True
        elif token in {"--tui", "--tu"}:
            tui_requested = True
        index += 1

    if command_index is None:
        return False

    query_values = []
    index = command_index + 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return False
        if token in {"--cli", "--cl"}:
            cli_requested = True
            index += 1
            continue
        if token in {"--tui", "--tu"}:
            tui_requested = True
            index += 1
            continue
        if token in {"--result-meta-fd", "--result-meta-f"}:
            if index + 1 >= len(argv):
                return False
            index += 2
            continue
        if token.startswith("--result-meta-fd=") or token.startswith(
            "--result-meta-f="
        ):
            index += 1
            continue
        if token in {
            "--image",
            "--model",
            "--provider",
            "--skills",
            "-s",
            "--toolsets",
            "--max-turns",
        }:
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                return False
            index += 2
            continue
        if token.startswith("--image=") or token.startswith("--skills="):
            if not token.split("=", 1)[1]:
                return False
            index += 1
            continue
        if token in {"--quiet", "-Q", "--verbose", "-v", "--safe-mode"}:
            index += 1
            continue
        if token in {"--query", "-q"}:
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                return False
            if not argv[index + 1]:
                return False
            query_values.append(argv[index + 1])
            index += 2
            continue
        if token.startswith("--query="):
            value = token.split("=", 1)[1]
            if not value:
                return False
            query_values.append(value)
            index += 1
            continue
        if token.startswith("-q") and token != "-q":
            value = token[2:]
            if not value:
                return False
            query_values.append(value)
            index += 1
            continue
        if token.startswith("-Qq"):
            value = token[3:]
            if not value:
                return False
            query_values.append(value)
            index += 1
            continue
        return False

    if len(query_values) != 1:
        return False

    from types import SimpleNamespace

    return not _resolve_use_tui(
        SimpleNamespace(cli=cli_requested, tui=tui_requested)
    )


def _publish_result_metadata_preparser_failure(
    owner, argv: list[str], exc: BaseException, parser=None, chat_parser=None, *, query_validated: bool = False
) -> None:
    """Publish/close a pre-parser owner for failures before cmd_chat owns it."""

    if owner is None:
        raise exc
    if (query_validated or _eligible_result_metadata_owner_argv(argv, parser, chat_parser)) and (
        _publish_unknown_result_metadata(
            owner, interrupted=isinstance(exc, KeyboardInterrupt)
        )
    ):
        raise SystemExit(0) from None
    if owner.closed and isinstance(exc, SystemExit):
        raise exc
    try:
        if not owner.closed:
            owner.close()
    except Exception:
        pass
    raise SystemExit(2) from None


def _publish_unknown_result_metadata(owner, *, interrupted: bool = False) -> bool:
    """Publish a terminal frame for a valid query that stopped early."""

    if owner is None or owner.closed:
        return False
    from hermes_cli import result_metadata

    try:
        result_metadata.publish_abnormal_result_metadata_fd(
            owner, interrupted=interrupted
        )
    except result_metadata.ResultMetadataError:
        print(result_metadata.PUBLIC_ERROR_MESSAGE, file=sys.stderr)
        raise SystemExit(1) from None
    return True


def _validate_result_metadata_args(args) -> bool:
    """Validate query/classic-CLI eligibility before startup side effects."""

    result_meta_fd = getattr(args, "result_meta_fd", None)
    if result_meta_fd is not None and not getattr(args, "query", None):
        print("Error: --result-meta-fd requires --query.", file=sys.stderr)
        raise SystemExit(2)

    use_tui = _resolve_use_tui(args)
    if result_meta_fd is not None and use_tui:
        print(
            "Error: --result-meta-fd is available only in the classic CLI.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if result_meta_fd is not None:
        args._result_meta_query_validated = True
    return use_tui
