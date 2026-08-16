from __future__ import annotations

import errno
import json
import os
import tempfile

import pytest


def _success_result() -> dict[str, object]:
    return {
        "completed": True,
        "failed": False,
        "partial": False,
        "interrupted": False,
        "api_calls": 1,
        "final_response": "secret response marker",
        "error": "secret error marker",
        "messages": [{"role": "user", "content": "secret prompt marker"}],
        "provider": "secret provider marker",
        "model": "secret model marker",
        "tool_output": "secret tool-output marker",
    }


def test_result_metadata_shape_is_closed_world_bounded_and_secret_free():
    from hermes_cli.result_metadata import (
        MAX_METADATA_BYTES,
        SCHEMA_VERSION,
        build_result_metadata,
        serialize_result_metadata,
    )

    metadata = build_result_metadata(_success_result(), max_iterations=3)
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
    assert json.loads(encoded) == metadata
    assert len(encoded) <= MAX_METADATA_BYTES
    for marker in (
        b"secret response marker",
        b"secret error marker",
        b"secret prompt marker",
        b"secret provider marker",
        b"secret model marker",
        b"secret tool-output marker",
    ):
        assert marker not in encoded


def test_result_metadata_fd_claim_accepts_only_blocking_fifo_writer():
    from hermes_cli.result_metadata import (
        ResultMetadataError,
        claim_result_metadata_fd,
    )

    read_fd, write_fd = os.pipe()
    owner = claim_result_metadata_fd(write_fd)
    try:
        assert owner.fileno() == write_fd
        assert os.get_inheritable(write_fd) is False
    finally:
        owner.close()
        os.close(read_fd)

    with pytest.raises(OSError):
        os.fstat(write_fd)

    with pytest.raises(ResultMetadataError):
        claim_result_metadata_fd(10**100)

    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(ResultMetadataError):
            claim_result_metadata_fd(read_fd)
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)


def test_result_metadata_fd_claim_rejects_unsupported_platform(monkeypatch):
    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(result_metadata, "fcntl", None)
    try:
        with pytest.raises(result_metadata.ResultMetadataError):
            result_metadata.claim_result_metadata_fd(write_fd)
        with pytest.raises(OSError):
            os.fstat(write_fd)
    finally:
        os.close(read_fd)


def test_result_metadata_fd_claim_closes_when_isolation_fails(monkeypatch):
    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()

    def fail_set_inheritable(_fd, _inheritable):
        raise OSError(errno.EIO, "secret isolation detail")

    monkeypatch.setattr(result_metadata.os, "set_inheritable", fail_set_inheritable)
    try:
        with pytest.raises(result_metadata.ResultMetadataError):
            result_metadata.claim_result_metadata_fd(write_fd)
        with pytest.raises(OSError):
            os.fstat(write_fd)
    finally:
        os.close(read_fd)


def test_result_metadata_fd_claim_rejects_named_fifo():
    from hermes_cli import result_metadata

    with tempfile.TemporaryDirectory() as tmpdir:
        fifo_path = os.path.join(tmpdir, "result-meta")
        os.mkfifo(fifo_path)
        read_fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        write_fd = os.open(fifo_path, os.O_WRONLY)
        try:
            with pytest.raises(result_metadata.ResultMetadataError):
                result_metadata.claim_result_metadata_fd(write_fd)
            with pytest.raises(OSError):
                os.fstat(write_fd)
        finally:
            os.close(read_fd)


def test_result_metadata_fd_claim_uses_dev_fd_when_proc_fd_missing(monkeypatch):
    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()
    real_readlink = os.readlink

    def fake_readlink(path):
        if path.startswith("/proc/self/fd/"):
            raise OSError("procfs unavailable")
        return real_readlink(path)

    monkeypatch.setattr(result_metadata.os, "readlink", fake_readlink)
    owner = result_metadata.claim_result_metadata_fd(write_fd)
    try:
        assert owner.fileno() == write_fd
    finally:
        owner.close()
        os.close(read_fd)


def test_result_metadata_fd_owner_cannot_be_directly_constructed():
    from hermes_cli.result_metadata import ResultMetadataError, ResultMetadataFD

    with pytest.raises(ResultMetadataError):
        ResultMetadataFD(3)


@pytest.mark.parametrize("invalid", [True, False, "3", 3.0, None, 0, 1, 2])
def test_result_metadata_fd_claim_rejects_noncanonical_values(invalid):
    from hermes_cli.result_metadata import ResultMetadataError, claim_result_metadata_fd

    with pytest.raises(ResultMetadataError):
        claim_result_metadata_fd(invalid)


def test_result_metadata_fd_writes_one_exact_frame_and_closes():
    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()
    owner = result_metadata.claim_result_metadata_fd(write_fd)
    metadata = result_metadata.build_result_metadata(
        _success_result(), max_iterations=3
    )
    try:
        result_metadata.write_result_metadata_fd(owner, metadata)
        assert os.read(read_fd, result_metadata.MAX_METADATA_BYTES) == (
            result_metadata.serialize_result_metadata(metadata)
        )
    finally:
        owner.close()
        os.close(read_fd)


def test_result_metadata_fd_close_fault_is_fail_closed(monkeypatch):
    from hermes_cli import result_metadata

    read_fd, write_fd = os.pipe()
    owner = result_metadata.claim_result_metadata_fd(write_fd)
    real_close = os.close

    def fail_close(_fd):
        raise OSError(errno.EIO, "secret close detail")

    monkeypatch.setattr(result_metadata.os, "close", fail_close)
    try:
        with pytest.raises(result_metadata.ResultMetadataError):
            owner.close()
        assert owner.closed is True
    finally:
        real_close(write_fd)
        real_close(read_fd)


@pytest.mark.parametrize("fault", ["short", "epipe", "eagain"])
def test_result_metadata_fd_write_fails_closed_without_retry(monkeypatch, fault):
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
            result_metadata.write_result_metadata_fd(
                owner,
                result_metadata.build_result_metadata(
                    _success_result(), max_iterations=3
                ),
            )
        assert calls == 1
    finally:
        owner.close()
        os.close(read_fd)


def test_result_metadata_api_calls_match_consumer_bound():
    from hermes_cli import result_metadata

    accepted = result_metadata.build_result_metadata(
        {**_success_result(), "api_calls": result_metadata.MAX_API_CALLS},
        max_iterations=90,
    )
    rejected = result_metadata.build_result_metadata(
        {**_success_result(), "api_calls": result_metadata.MAX_API_CALLS + 1},
        max_iterations=90,
    )

    assert accepted["api_calls"] == result_metadata.MAX_API_CALLS
    assert accepted["failure_class"] == "none"
    assert rejected["api_calls"] == 0
    assert rejected["failure_class"] == "unknown_failure"
    with pytest.raises(result_metadata.ResultMetadataError):
        result_metadata.serialize_result_metadata(
            {**accepted, "api_calls": result_metadata.MAX_API_CALLS + 1}
        )


def test_result_metadata_contradictory_statuses_fail_to_unknown_failure():
    from hermes_cli import result_metadata

    metadata = result_metadata.build_result_metadata(
        {
            "completed": False,
            "failed": True,
            "partial": False,
            "interrupted": True,
            "api_calls": 1,
        },
        max_iterations=90,
    )

    assert metadata["failed"] is True
    assert metadata["interrupted"] is True
    assert metadata["failure_class"] == "unknown_failure"
    with pytest.raises(result_metadata.ResultMetadataError):
        result_metadata.serialize_result_metadata(
            {**metadata, "failure_class": "interrupted"}
        )
    with pytest.raises(result_metadata.ResultMetadataError):
        result_metadata.serialize_result_metadata(
            {**metadata, "failure_class": "provider_api_terminal"}
        )
