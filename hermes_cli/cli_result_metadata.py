"""Single-query metadata publication and transfer of the owned pipe endpoint."""

import sys
from typing import Any


class CLIResultMetadataMixin:
    def _publish_result_metadata(self, result: Any) -> None:
        """Publish requested query metadata or terminate on publication failure."""

        owner = getattr(self, "result_meta_fd", None)
        if owner is None:
            return
        from hermes_cli.result_metadata import (
            PUBLIC_ERROR_MESSAGE,
            ResultMetadataError,
            build_result_metadata,
            write_result_metadata_fd,
        )

        publication_failed = False
        try:
            metadata = build_result_metadata(result, max_iterations=self.max_turns)
            write_result_metadata_fd(owner, metadata)
        except ResultMetadataError:
            publication_failed = True
        try:
            self._close_result_metadata_fd()
        except ResultMetadataError:
            publication_failed = True
        if publication_failed:
            print(PUBLIC_ERROR_MESSAGE, file=sys.stderr)
            raise SystemExit(1) from None

    def _publish_abnormal_result_metadata(self, *, interrupted: bool) -> None:
        """Publish a closed failure frame without reflecting exception details."""

        from hermes_cli.result_metadata import MAX_API_CALLS

        api_calls = 0
        try:
            summary = self.agent.get_activity_summary()
            candidate = summary.get("api_call_count")
            upper_bound = min(self.max_turns + 1, MAX_API_CALLS)
            if type(candidate) is int and 0 <= candidate <= upper_bound:
                api_calls = candidate
        except Exception:
            pass
        self._publish_result_metadata(
            {
                "completed": False,
                "failed": not interrupted,
                "partial": False,
                "interrupted": interrupted,
                "api_calls": api_calls,
            }
        )

    def _close_result_metadata_fd(self) -> None:
        """Release the owned result-metadata descriptor exactly once."""

        owner = getattr(self, "result_meta_fd", None)
        self.result_meta_fd = None
        if owner is not None:
            owner.close()


class ResultMetadataFDOwnershipGuard:
    """Close a claimed result-metadata descriptor across every main() exit."""

    __slots__ = ("_pending_owner", "_cli_owner")

    def __init__(self, owner) -> None:
        self._pending_owner = owner
        self._cli_owner = None

    def transfer_to(self, cli) -> None:
        if self._pending_owner is None or self._cli_owner is not None:
            raise RuntimeError("result metadata descriptor ownership already transferred")
        if getattr(cli, "result_meta_fd", None) is not self._pending_owner:
            raise RuntimeError("result metadata descriptor ownership transfer mismatch")
        self._cli_owner = cli
        self._pending_owner = None

    def publish_fallback(self, *, interrupted: bool = False) -> bool:
        """Publish one safe terminal frame when a valid query stops early."""

        cli = self._cli_owner
        if cli is not None:
            owner = getattr(cli, "result_meta_fd", None)
            if owner is None or owner.closed:
                return False
            cli._publish_abnormal_result_metadata(interrupted=interrupted)
            return True

        owner = self._pending_owner
        if owner is None or owner.closed:
            return False
        from hermes_cli.result_metadata import (
            PUBLIC_ERROR_MESSAGE,
            ResultMetadataError,
            publish_abnormal_result_metadata_fd,
        )

        try:
            publish_abnormal_result_metadata_fd(owner, interrupted=interrupted)
        except ResultMetadataError:
            print(PUBLIC_ERROR_MESSAGE, file=sys.stderr)
            raise SystemExit(1) from None
        self._pending_owner = None
        return True

    def close(self) -> None:
        cli = self._cli_owner
        self._cli_owner = None
        if cli is not None:
            cli._close_result_metadata_fd()
            return

        owner = self._pending_owner
        self._pending_owner = None
        if owner is not None:
            owner.close()
