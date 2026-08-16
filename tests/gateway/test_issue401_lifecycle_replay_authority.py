"""Issue #401/#475 lifecycle replay and authority invariants."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult


class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str):
        return {}


def _adapter() -> _Adapter:
    return _Adapter(PlatformConfig(enabled=True, token="test-token"), Platform.TELEGRAM)


def test_stale_generation_replay_cannot_clear_current_post_delivery_owner():
    adapter = _adapter()
    session_key = "telegram:chat:thread"
    old_token = object()
    current_token = object()

    def old_callback():
        return "old"

    def current_callback():
        return "current"

    assert (
        adapter.register_post_delivery_callback(
            session_key, old_callback, generation=1, owner_token=old_token
        )
        is old_token
    )
    assert (
        adapter.register_post_delivery_callback(
            session_key, current_callback, generation=2, owner_token=current_token
        )
        is current_token
    )

    # Registering the newer generation retires the older callback. A replay from
    # generation 1 must be a no-op and must not remove the current owner slot.
    assert (
        adapter.pop_post_delivery_callback(
            session_key, generation=1, owner_token=old_token
        )
        is None
    )
    assert adapter._post_delivery_callbacks[session_key] == [
        {
            "generation": 2,
            "callback": current_callback,
            "owner_token": current_token,
        }
    ]

    # Same generation but wrong owner is also a no-op.
    assert (
        adapter.pop_post_delivery_callback(
            session_key, generation=2, owner_token=old_token
        )
        is None
    )
    assert adapter._post_delivery_callbacks[session_key][0]["callback"] is current_callback

    callback = adapter.pop_post_delivery_callback(
        session_key, generation=2, owner_token=current_token
    )
    assert callback is current_callback
    assert callable(callback)
    assert callback() == "current"
    assert session_key not in adapter._post_delivery_callbacks


def test_duplicate_lower_generation_lifecycle_replay_is_noop():
    adapter = _adapter()
    session_key = "telegram:chat:thread"
    current_token = object()

    def current_callback():
        return "current"

    def stale_callback():
        return "stale"

    adapter.register_post_delivery_callback(
        session_key, current_callback, generation=3, owner_token=current_token
    )

    assert (
        adapter.register_post_delivery_callback(
            session_key, stale_callback, generation=2, owner_token=object()
        )
        is None
    )
    assert adapter._post_delivery_callbacks[session_key] == [
        {
            "generation": 3,
            "callback": current_callback,
            "owner_token": current_token,
        }
    ]


@pytest.mark.parametrize("max_turns", [7, "9"])
def test_multiplex_runtime_env_reload_preserves_auth_authority_without_dotenv(
    tmp_path, monkeypatch, max_turns
):
    from gateway import run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        f"agent:\n  max_turns: {max_turns}\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "stale-dotenv-value")
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    def _forbidden_dotenv_reload(*args, **kwargs):
        raise AssertionError("multiplex gateway must not reload live .env secrets")

    with (
        patch("agent.secret_scope.is_multiplex_active", return_value=True),
        patch.object(gateway_run, "load_hermes_dotenv", _forbidden_dotenv_reload),
    ):
        gateway_run._reload_runtime_env_preserving_config_authority()

    assert gateway_run.os.environ["HERMES_MAX_ITERATIONS"] == str(max_turns)
