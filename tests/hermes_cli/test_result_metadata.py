from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("result", "max_iterations", "failure_class", "api_calls"),
    [
        pytest.param(
            {
                "completed": True,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "api_calls": 1,
                "turn_exit_reason": "max_iterations_reached(1/1)",
            },
            1,
            "unknown_failure",
            1,
            id="completed-max-iterations-is-contradictory",
        ),
        pytest.param(
            {
                "completed": True,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "api_calls": 1,
                "turn_exit_reason": "budget_exhausted",
            },
            1,
            "unknown_failure",
            1,
            id="completed-budget-exhausted-is-contradictory",
        ),
        pytest.param(
            {
                "completed": True,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "api_calls": 1,
                "turn_exit_reason": "all_retries_exhausted_no_response",
            },
            1,
            "unknown_failure",
            1,
            id="completed-retries-exhausted-is-contradictory",
        ),
        pytest.param(
            {
                "completed": True,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "api_calls": 0,
            },
            3,
            "none",
            0,
            id="clean-success-zero-api-calls",
        ),
        pytest.param(
            {
                "completed": True,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "api_calls": 4,
            },
            3,
            "none",
            4,
            id="clean-success-grace-call-boundary",
        ),
        pytest.param(
            {
                "completed": False,
                "failed": False,
                "partial": True,
                "interrupted": False,
                "api_calls": 2,
            },
            3,
            "max_turns_or_incomplete",
            2,
            id="valid-partial",
        ),
        pytest.param(
            {
                "completed": False,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "api_calls": 2,
            },
            3,
            "max_turns_or_incomplete",
            2,
            id="valid-incomplete",
        ),
        pytest.param(
            {
                "completed": False,
                "failed": True,
                "partial": False,
                "interrupted": True,
                "api_calls": 1,
                "error": "content_policy_blocked: private detail",
                "failure_reason": "rate_limit",
            },
            3,
            "interrupted",
            1,
            id="interrupted-precedence",
        ),
        pytest.param(
            {
                "completed": False,
                "failed": True,
                "partial": False,
                "interrupted": False,
                "api_calls": 1,
                "error": "content_policy_blocked: private detail",
                "failure_reason": "rate_limit",
            },
            3,
            "content_policy_blocked",
            1,
            id="content-policy-precedes-provider",
        ),
        pytest.param(
            {
                "completed": False,
                "failed": True,
                "partial": False,
                "interrupted": False,
                "api_calls": 1,
                "failure_reason": "rate_limit",
            },
            3,
            "provider_api_terminal",
            1,
            id="trusted-provider-failure",
        ),
        pytest.param(
            {
                "completed": True,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "api_calls": True,
            },
            3,
            "unknown_failure",
            0,
            id="boolean-api-calls-is-invalid",
        ),
        pytest.param(
            {
                "completed": True,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "api_calls": 5,
            },
            3,
            "unknown_failure",
            0,
            id="api-calls-over-grace-boundary-is-invalid",
        ),
    ],
)
def test_projection_matrix_is_closed_world_bounded_and_serializable(
    result, max_iterations, failure_class, api_calls
):
    from hermes_cli.result_metadata import (
        MAX_METADATA_BYTES,
        build_result_metadata,
        serialize_result_metadata,
    )

    raw_marker = "raw-marker-must-not-leak"
    result = {**result, "final_response": raw_marker, "tool_output": raw_marker}

    metadata = build_result_metadata(result, max_iterations=max_iterations)
    encoded = serialize_result_metadata(metadata)

    assert metadata["failure_class"] == failure_class
    assert metadata["api_calls"] == api_calls
    assert set(metadata) == {
        "schema_version",
        "completed",
        "failed",
        "partial",
        "interrupted",
        "api_calls",
        "failure_class",
    }
    assert json.loads(encoded) == metadata
    assert len(encoded) <= MAX_METADATA_BYTES
    assert raw_marker.encode() not in encoded


@pytest.mark.parametrize(
    ("result", "failure_class", "expected_statuses"),
    [
        (
            {"completed": False, "failed": False, "partial": False, "interrupted": True, "api_calls": 1},
            "interrupted",
            (False, False, False, True),
        ),
        (
            {
                "completed": False,
                "failed": True,
                "partial": False,
                "interrupted": False,
                "api_calls": 1,
                "error": "content_policy_blocked: private provider detail",
            },
            "content_policy_blocked",
            (False, True, False, False),
        ),
        (
            {
                "completed": False,
                "failed": True,
                "partial": False,
                "interrupted": False,
                "api_calls": 2,
                "failure_reason": "rate_limit",
            },
            "provider_api_terminal",
            (False, True, False, False),
        ),
        (
            {"completed": False, "failed": False, "partial": True, "interrupted": False, "api_calls": 2},
            "max_turns_or_incomplete",
            (False, False, True, False),
        ),
        (
            {
                "completed": False,
                "failed": False,
                "partial": False,
                "interrupted": False,
                "api_calls": 4,
                "turn_exit_reason": "max_iterations_reached(3/3)",
            },
            "max_turns_or_incomplete",
            (False, False, False, False),
        ),
        (
            {"completed": False, "failed": True, "partial": False, "interrupted": False, "api_calls": 1},
            "unknown_failure",
            (False, True, False, False),
        ),
        (
            {"completed": True, "failed": True, "partial": False, "interrupted": False, "api_calls": 1},
            "unknown_failure",
            (True, True, False, False),
        ),
    ],
)
def test_failure_class_precedence_and_status_invariants(result, failure_class, expected_statuses):
    from hermes_cli.result_metadata import build_result_metadata

    metadata = build_result_metadata(result, max_iterations=3)

    assert metadata["failure_class"] == failure_class
    assert (
        metadata["completed"],
        metadata["failed"],
        metadata["partial"],
        metadata["interrupted"],
    ) == expected_statuses


def test_interrupted_wins_failure_class_precedence():
    from hermes_cli.result_metadata import build_result_metadata

    metadata = build_result_metadata(
        {
            "completed": False,
            "failed": True,
            "partial": False,
            "interrupted": True,
            "api_calls": 1,
            "error": "content_policy_blocked: private detail",
            "failure_reason": "rate_limit",
        },
        max_iterations=3,
    )

    assert metadata["failure_class"] == "interrupted"
    assert metadata["interrupted"] is True
    assert metadata["failed"] is True


def test_canonical_turn_result_defaults_absent_negative_flags_to_false():
    from hermes_cli.result_metadata import build_result_metadata

    metadata = build_result_metadata(
        {"completed": True, "partial": False, "interrupted": False, "api_calls": 1},
        max_iterations=3,
    )

    assert metadata["failure_class"] == "none"
    assert metadata["failed"] is False


@pytest.mark.parametrize("api_calls", [True, -1, 5, "1", None])
def test_api_calls_must_be_a_bounded_non_boolean_integer(api_calls):
    from hermes_cli.result_metadata import build_result_metadata

    metadata = build_result_metadata(
        {
            "completed": True,
            "failed": False,
            "partial": False,
            "interrupted": False,
            "api_calls": api_calls,
        },
        max_iterations=3,
    )

    assert metadata["api_calls"] == 0
    assert metadata["failure_class"] == "unknown_failure"
    assert metadata["failed"] is False


def test_status_values_must_be_strict_booleans():
    from hermes_cli.result_metadata import build_result_metadata

    metadata = build_result_metadata(
        {"completed": 1, "failed": False, "partial": False, "interrupted": False, "api_calls": 0},
        max_iterations=3,
    )

    assert metadata["failure_class"] == "unknown_failure"
    assert metadata["failed"] is False


def test_provider_terminal_class_uses_only_structured_classifier_values():
    from agent.error_classifier import FailoverReason
    from hermes_cli.result_metadata import build_result_metadata

    base = {
        "completed": False,
        "failed": True,
        "partial": False,
        "interrupted": False,
        "api_calls": 1,
    }
    for reason in FailoverReason:
        metadata = build_result_metadata(
            {**base, "failure_reason": reason.value}, max_iterations=3
        )
        assert metadata["failure_class"] == "provider_api_terminal"

    metadata = build_result_metadata(
        {**base, "failure_reason": "human display text"}, max_iterations=3
    )
    assert metadata["failure_class"] == "unknown_failure"


def test_success_metadata_is_closed_world_and_canonical():
    from hermes_cli.result_metadata import (
        SCHEMA_VERSION,
        build_result_metadata,
        serialize_result_metadata,
    )

    result = {
        "completed": True,
        "failed": False,
        "partial": False,
        "interrupted": False,
        "api_calls": 1,
        "final_response": "secret response",
        "error": "secret error",
        "messages": [{"role": "user", "content": "secret prompt"}],
        "provider": "secret provider",
        "model": "secret model",
        "session_id": "secret session",
        "tool_output": "secret tool output",
        "path": "/secret/result/path",
        "hash": "secret hash",
        "exception": "secret exception",
    }

    metadata = build_result_metadata(result, max_iterations=3)
    encoded = serialize_result_metadata(metadata)

    assert metadata == {
        "schema_version": SCHEMA_VERSION,
        "completed": True,
        "failed": False,
        "partial": False,
        "interrupted": False,
        "api_calls": 1,
        "failure_class": "none",
    }
    assert encoded == (
        b'{"api_calls":1,"completed":true,"failed":false,'
        b'"failure_class":"none","interrupted":false,"partial":false,'
        b'"schema_version":"hermes-agent-result-meta-v1"}\n'
    )
    assert json.loads(encoded) == metadata
    for secret in (
        b"secret response",
        b"secret error",
        b"secret prompt",
        b"secret provider",
        b"secret model",
        b"secret session",
        b"secret tool output",
        b"/secret/result/path",
        b"secret hash",
        b"secret exception",
    ):
        assert secret not in encoded


@pytest.mark.parametrize(
    ("failure_class", "statuses"),
    [
        ("none", (False, False, False, False)),
        ("interrupted", (False, False, False, False)),
        ("content_policy_blocked", (False, False, False, False)),
        ("provider_api_terminal", (False, False, False, False)),
        ("max_turns_or_incomplete", (True, False, False, False)),
    ],
)
def test_serializer_rejects_failure_class_status_invariant_violations(
    failure_class, statuses
):
    from hermes_cli.result_metadata import (
        ResultMetadataError,
        SCHEMA_VERSION,
        serialize_result_metadata,
    )

    completed, failed, partial, interrupted = statuses
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "completed": completed,
        "failed": failed,
        "partial": partial,
        "interrupted": interrupted,
        "api_calls": 0,
        "failure_class": failure_class,
    }

    with pytest.raises(ResultMetadataError):
        serialize_result_metadata(metadata)


def _success_result() -> dict[str, object]:
    return {
        "completed": True,
        "failed": False,
        "partial": False,
        "interrupted": False,
        "api_calls": 1,
    }


def _success_metadata() -> dict[str, object]:
    from hermes_cli.result_metadata import build_result_metadata

    return build_result_metadata(_success_result(), max_iterations=3)


def _metadata_temps(directory):
    return [path for path in directory.iterdir() if path.name.endswith(".result-meta.tmp")]


@pytest.mark.parametrize("bad_path", ["relative.json", "/tmp/../tmp/result.json"])
def test_destination_requires_absolute_path_without_parent_traversal(bad_path):
    from hermes_cli.result_metadata import ResultMetadataError, validate_result_metadata_destination

    with pytest.raises(ResultMetadataError):
        validate_result_metadata_destination(bad_path)


def test_destination_rejects_missing_or_symlinked_parent_and_existing_leaf(tmp_path):
    from hermes_cli.result_metadata import ResultMetadataError, validate_result_metadata_destination

    with pytest.raises(ResultMetadataError):
        validate_result_metadata_destination(tmp_path / "missing" / "result.json")

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ResultMetadataError):
        validate_result_metadata_destination(linked_parent / "result.json")

    existing = real_parent / "result.json"
    existing.write_text("caller data", encoding="utf-8")
    with pytest.raises(ResultMetadataError):
        validate_result_metadata_destination(existing)


def test_write_result_metadata_is_atomic_private_and_leaves_no_temp(tmp_path):
    from hermes_cli.result_metadata import SCHEMA_VERSION, write_result_metadata

    destination = tmp_path / "result.json"
    metadata = write_result_metadata(destination, _success_metadata())

    assert json.loads(destination.read_bytes()) == metadata
    assert metadata["schema_version"] == SCHEMA_VERSION
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert _metadata_temps(tmp_path) == []


def test_write_result_metadata_handles_short_writes(monkeypatch, tmp_path):
    from hermes_cli.result_metadata import write_result_metadata

    real_write = os.write

    def short_write(fd, data):
        return real_write(fd, data[: max(1, len(data) // 2)])

    monkeypatch.setattr(os, "write", short_write)
    destination = tmp_path / "result.json"

    metadata = write_result_metadata(destination, _success_metadata())

    assert json.loads(destination.read_bytes()) == metadata


def test_publish_race_never_clobbers_destination(monkeypatch, tmp_path):
    from hermes_cli.result_metadata import ResultMetadataError, write_result_metadata

    destination = tmp_path / "result.json"
    real_link = os.link

    def racing_link(src, dst, **kwargs):
        destination.write_text("caller won", encoding="utf-8")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", racing_link)

    with pytest.raises(ResultMetadataError):
        write_result_metadata(destination, _success_metadata())

    assert destination.read_text(encoding="utf-8") == "caller won"
    assert _metadata_temps(tmp_path) == []


@pytest.mark.parametrize("fault", ["write", "file_fsync", "link"])
def test_publication_faults_leave_no_destination_or_temp(monkeypatch, tmp_path, fault):
    from hermes_cli.result_metadata import ResultMetadataError, write_result_metadata

    def raise_os_error(*_args, **_kwargs):
        raise OSError(fault)

    if fault == "write":
        monkeypatch.setattr(os, "write", raise_os_error)
    elif fault == "file_fsync":
        monkeypatch.setattr(os, "fsync", raise_os_error)
    else:
        monkeypatch.setattr(os, "link", raise_os_error)

    destination = tmp_path / "result.json"
    with pytest.raises(ResultMetadataError):
        write_result_metadata(destination, _success_metadata())

    assert not destination.exists()
    assert _metadata_temps(tmp_path) == []


def test_parent_fsync_failure_rolls_back_published_file(monkeypatch, tmp_path):
    from hermes_cli.result_metadata import ResultMetadataError, write_result_metadata

    real_fsync = os.fsync
    calls = 0

    def fail_parent_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_parent_fsync)
    destination = tmp_path / "result.json"

    with pytest.raises(ResultMetadataError):
        write_result_metadata(destination, _success_metadata())

    assert not destination.exists()
    assert _metadata_temps(tmp_path) == []


def test_temp_unlink_failure_rolls_back_and_retries_cleanup(monkeypatch, tmp_path):
    from hermes_cli.result_metadata import ResultMetadataError, write_result_metadata

    real_unlink = os.unlink
    calls = 0

    def fail_first_unlink(path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("unlink")
        return real_unlink(path, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_first_unlink)
    destination = tmp_path / "result.json"

    with pytest.raises(ResultMetadataError):
        write_result_metadata(destination, _success_metadata())

    assert calls >= 2
    assert not destination.exists()
    assert _metadata_temps(tmp_path) == []


def test_symlink_parent_swap_cannot_redirect_publication(monkeypatch, tmp_path):
    from hermes_cli.result_metadata import ResultMetadataError, write_result_metadata

    parent = tmp_path / "parent"
    parent.mkdir()
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    moved_parent = tmp_path / "moved-parent"
    real_link = os.link

    def swap_parent_then_link(src, dst, **kwargs):
        parent.rename(moved_parent)
        parent.symlink_to(attacker, target_is_directory=True)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", swap_parent_then_link)

    with pytest.raises(ResultMetadataError):
        write_result_metadata(parent / "result.json", _success_metadata())

    assert not (attacker / "result.json").exists()
    assert not (moved_parent / "result.json").exists()


@pytest.mark.parametrize("leaf_kind", ["symlink", "directory", "fifo"])
def test_destination_rejects_every_existing_leaf_type(tmp_path, leaf_kind):
    from hermes_cli.result_metadata import (
        ResultMetadataError,
        validate_result_metadata_destination,
        write_result_metadata,
    )

    destination = tmp_path / "special-result"
    if leaf_kind == "symlink":
        destination.symlink_to(tmp_path / "missing-target")
    elif leaf_kind == "directory":
        destination.mkdir()
    else:
        os.mkfifo(destination)

    with pytest.raises(ResultMetadataError):
        validate_result_metadata_destination(destination)
    with pytest.raises(ResultMetadataError):
        write_result_metadata(destination, _success_metadata())

    assert os.path.lexists(destination)


def test_destination_rejects_existing_device():
    from hermes_cli.result_metadata import ResultMetadataError, validate_result_metadata_destination

    if not os.path.exists("/dev/null"):
        pytest.skip("POSIX null device is unavailable")
    with pytest.raises(ResultMetadataError):
        validate_result_metadata_destination("/dev/null")


def test_temp_symlink_swap_never_publishes_or_unlinks_attacker_file(monkeypatch, tmp_path):
    from hermes_cli.result_metadata import ResultMetadataError, write_result_metadata

    attacker_file = tmp_path / "attacker.txt"
    attacker_file.write_text("caller data", encoding="utf-8")
    destination = tmp_path / "result.json"
    real_link = os.link
    swapped_temp = None

    def swap_temp_then_link(src, dst, **kwargs):
        nonlocal swapped_temp
        [swapped_temp] = _metadata_temps(tmp_path)
        swapped_temp.unlink()
        swapped_temp.symlink_to(attacker_file)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", swap_temp_then_link)

    with pytest.raises(ResultMetadataError):
        write_result_metadata(destination, _success_metadata())

    assert not destination.exists()
    assert attacker_file.read_text(encoding="utf-8") == "caller data"
    assert swapped_temp is not None and swapped_temp.is_symlink()


def test_claim_result_metadata_fd_accepts_blocking_fifo_writer_and_sets_cloexec():
    from hermes_cli.result_metadata import claim_result_metadata_fd

    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    owner = claim_result_metadata_fd(write_fd)
    try:
        assert owner.fileno() == write_fd
        assert os.get_inheritable(write_fd) is False
        assert os.fpathconf(write_fd, "PC_PIPE_BUF") >= 1024
        descendant = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import os,sys;\ntry: os.fstat({write_fd})\nexcept OSError: sys.exit(0)\nsys.exit(1)",
            ],
            close_fds=False,
            check=False,
        )
        assert descendant.returncode == 0
    finally:
        owner.close()
        os.close(read_fd)

    with pytest.raises(OSError):
        os.fstat(write_fd)


@pytest.mark.parametrize("invalid", [True, False, "3", 3.0, None, 0, 1, 2])
def test_claim_result_metadata_fd_rejects_noncanonical_values(invalid):
    from hermes_cli.result_metadata import ResultMetadataError, claim_result_metadata_fd

    with pytest.raises(ResultMetadataError):
        claim_result_metadata_fd(invalid)


def test_claim_result_metadata_fd_rejects_closed_read_end_and_regular_file(tmp_path):
    from hermes_cli.result_metadata import ResultMetadataError, claim_result_metadata_fd

    closed_read, closed_fd = os.pipe()
    os.close(closed_read)
    os.close(closed_fd)
    with pytest.raises(ResultMetadataError):
        claim_result_metadata_fd(closed_fd)

    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(ResultMetadataError):
            claim_result_metadata_fd(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    regular_fd = os.open(tmp_path / "regular", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        with pytest.raises(ResultMetadataError):
            claim_result_metadata_fd(regular_fd)
    finally:
        os.close(regular_fd)

    fifo = tmp_path / "duplex-fifo"
    os.mkfifo(fifo)
    duplex_fd = os.open(fifo, os.O_RDWR)
    try:
        with pytest.raises(ResultMetadataError):
            claim_result_metadata_fd(duplex_fd)
    finally:
        os.close(duplex_fd)


def test_claim_result_metadata_fd_rejects_nonblocking_or_small_pipe(monkeypatch):
    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)
        with pytest.raises(result_metadata.ResultMetadataError):
            result_metadata.claim_result_metadata_fd(write_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(result_metadata.os, "fpathconf", lambda *_args: 512)
    try:
        with pytest.raises(result_metadata.ResultMetadataError):
            result_metadata.claim_result_metadata_fd(write_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    ("failure_class", "statuses"),
    [
        ("none", (True, False, False, False)),
        ("interrupted", (False, False, False, True)),
        ("content_policy_blocked", (False, True, False, False)),
        ("provider_api_terminal", (False, True, False, False)),
        ("max_turns_or_incomplete", (False, False, True, False)),
        ("unknown_failure", (False, True, False, False)),
    ],
)
def test_write_result_metadata_fd_emits_one_bounded_atomic_frame(
    monkeypatch, failure_class, statuses
):
    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()
    owner = result_metadata.claim_result_metadata_fd(write_fd)
    real_write = os.write
    calls = []

    def counted_write(target_fd, payload):
        calls.append((target_fd, bytes(payload)))
        return real_write(target_fd, payload)

    monkeypatch.setattr(result_metadata.os, "write", counted_write)
    try:
        completed, failed, partial, interrupted = statuses
        metadata = {
            **_success_metadata(),
            "completed": completed,
            "failed": failed,
            "partial": partial,
            "interrupted": interrupted,
            "failure_class": failure_class,
        }
        result_metadata.write_result_metadata_fd(owner, metadata)
        expected = result_metadata.serialize_result_metadata(metadata)
        assert os.read(read_fd, 1024) == expected
        assert calls == [(write_fd, expected)]
        assert len(expected) <= 1024
    finally:
        owner.close()
        os.close(read_fd)


@pytest.mark.parametrize("fault", ["short", "epipe", "eagain"])
def test_write_result_metadata_fd_fails_closed_without_retry(monkeypatch, fault):
    import errno

    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()
    owner = result_metadata.claim_result_metadata_fd(write_fd)
    calls = 0

    def faulty_write(_fd, payload):
        nonlocal calls
        calls += 1
        if fault == "short":
            return len(payload) - 1
        error_number = errno.EPIPE if fault == "epipe" else errno.EAGAIN
        raise OSError(error_number, fault)

    monkeypatch.setattr(result_metadata.os, "write", faulty_write)
    try:
        with pytest.raises(result_metadata.ResultMetadataError):
            result_metadata.write_result_metadata_fd(owner, _success_metadata())
        assert calls == 1
    finally:
        owner.close()
        os.close(read_fd)
