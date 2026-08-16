"""Tests for GHSA-96vc-wcxf-jjff and GHSA-qg5c-hvr5-hjgr.

Two related ACP approval-flow issues:
- 96vc: ACP didn't set HERMES_EXEC_ASK, so `check_all_command_guards`
  took the non-interactive auto-approve path and never consulted the
  ACP-supplied callback.
- qg5c: `_approval_callback` was a module-global in terminal_tool;
  overlapping ACP sessions overwrote each other's callback slot.

Both fixed together by:
1. Setting HERMES_EXEC_ASK inside _run_agent (wraps the agent call).
2. Storing the callback in thread-local state so concurrent executor
   threads don't collide.
"""

import threading

import pytest


@pytest.fixture(autouse=True)
def _isolate_approval_state(monkeypatch):
    """Keep approval-isolation regressions independent of user config.

    The real developer profile may carry broad permanent/session allowlist
    entries. These tests assert routing behavior, so ambient approvals must not
    short-circuit the dangerous-command path before the callback/gateway owner
    logic under test runs.
    """
    from tools import approval

    permanent = set(approval._permanent_approved)
    session = {key: set(value) for key, value in approval._session_approved.items()}
    approval._permanent_approved.clear()
    approval._session_approved.clear()
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    try:
        yield
    finally:
        approval._permanent_approved.clear()
        approval._permanent_approved.update(permanent)
        approval._session_approved.clear()
        approval._session_approved.update(session)



class TestThreadLocalApprovalCallback:
    """GHSA-qg5c-hvr5-hjgr: set_approval_callback must be per-thread so
    concurrent ACP sessions don't stomp on each other's handlers."""

    def test_set_and_get_in_same_thread(self):
        from tools.terminal_tool import (
            set_approval_callback,
            _get_approval_callback,
        )

        cb1 = lambda cmd, desc: "once"  # noqa: E731
        set_approval_callback(cb1)
        assert _get_approval_callback() is cb1

    def test_callback_not_visible_in_different_thread(self):
        """Thread A's callback is NOT visible to Thread B."""
        from tools.terminal_tool import (
            set_approval_callback,
            _get_approval_callback,
        )

        cb_a = lambda cmd, desc: "thread_a"  # noqa: E731
        cb_b = lambda cmd, desc: "thread_b"  # noqa: E731

        seen_in_a = []
        seen_in_b = []

        def thread_a():
            set_approval_callback(cb_a)
            # Pause so thread B has time to set its own callback
            import time
            time.sleep(0.05)
            seen_in_a.append(_get_approval_callback())

        def thread_b():
            set_approval_callback(cb_b)
            import time
            time.sleep(0.05)
            seen_in_b.append(_get_approval_callback())

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join()
        tb.join()

        # Each thread must see ONLY its own callback — not the other's
        assert seen_in_a == [cb_a]
        assert seen_in_b == [cb_b]

    def test_main_thread_callback_not_leaked_to_worker(self):
        """A callback set in the main thread does NOT leak into a
        freshly-spawned worker thread."""
        from tools.terminal_tool import (
            set_approval_callback,
            _get_approval_callback,
        )

        cb_main = lambda cmd, desc: "main"  # noqa: E731
        set_approval_callback(cb_main)

        worker_saw = []

        def worker():
            worker_saw.append(_get_approval_callback())

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # Worker thread has no callback set — TLS is empty for it
        assert worker_saw == [None]
        # Main thread still has its callback
        assert _get_approval_callback() is cb_main

    def test_sudo_password_callback_also_thread_local(self):
        """Same protection applies to the sudo password callback."""
        from tools.terminal_tool import (
            set_sudo_password_callback,
            _get_sudo_password_callback,
        )

        cb_main = lambda: "main-password"  # noqa: E731
        set_sudo_password_callback(cb_main)

        worker_saw = []

        def worker():
            worker_saw.append(_get_sudo_password_callback())

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert worker_saw == [None]
        assert _get_sudo_password_callback() is cb_main

    def test_sudo_password_cache_does_not_leak_across_threads(self):
        """Interactive sudo cache must not bleed into another executor thread."""
        from tools.terminal_tool import (
            _get_cached_sudo_password,
            _reset_cached_sudo_passwords,
            _set_cached_sudo_password,
        )

        _reset_cached_sudo_passwords()
        _set_cached_sudo_password("main-thread-password")

        worker_saw = []

        def worker():
            worker_saw.append(_get_cached_sudo_password())

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert worker_saw == [""]
        assert _get_cached_sudo_password() == "main-thread-password"

    def test_sudo_password_cache_isolated_across_acp_sessions_on_same_pool_thread(self):
        """ACP's ThreadPoolExecutor reuses threads. Two ACP sessions that land
        on the same reused thread must not share the interactive sudo password
        cache. The fix wraps each session in contextvars.copy_context() and
        binds HERMES_SESSION_KEY per session, so the cache scope key differs
        across sessions even when the underlying thread is identical.
        """
        import contextvars
        from concurrent.futures import ThreadPoolExecutor

        from gateway.session_context import (
            clear_session_vars,
            set_session_vars,
        )
        from tools.terminal_tool import (
            _get_cached_sudo_password,
            _reset_cached_sudo_passwords,
            _set_cached_sudo_password,
        )

        _reset_cached_sudo_passwords()
        executor = ThreadPoolExecutor(max_workers=1)  # force thread reuse

        runs: list[tuple[str, str, str]] = []  # (session_id, before, after)

        def _simulate_acp_session(session_id: str, write_password: str) -> None:
            tokens = set_session_vars(session_key=session_id)
            try:
                observed_before = _get_cached_sudo_password()
                _set_cached_sudo_password(write_password)
                observed_after = _get_cached_sudo_password()
                runs.append((session_id, observed_before, observed_after))
            finally:
                clear_session_vars(tokens)

        def _run_in_fresh_context(session_id: str, pw: str) -> str:
            ctx = contextvars.copy_context()
            ctx.run(_simulate_acp_session, session_id, pw)
            return session_id

        try:
            executor.submit(_run_in_fresh_context, "acp-session-A", "alpha-secret").result()
            # Same thread. Without the fix B would see "alpha-secret".
            executor.submit(_run_in_fresh_context, "acp-session-B", "bravo-secret").result()
        finally:
            executor.shutdown(wait=True)
            _reset_cached_sudo_passwords()

        assert runs[0] == ("acp-session-A", "", "alpha-secret")
        # Core regression guard: B on the same reused thread must see an empty
        # cache, not A's password.
        assert runs[1] == ("acp-session-B", "", "bravo-secret")


class TestGatewayNotifierOwnership:
    def test_old_cleanup_preserves_replacement_notifier_and_queue(self):
        from tools import approval

        session_key = "replacement-notifier-session"
        old_cb = lambda _data: None
        replacement_cb = lambda _data: None
        old_token = approval.register_gateway_notify(session_key, old_cb)
        replacement_token = approval.register_gateway_notify(session_key, replacement_cb)
        old_entry = approval._ApprovalEntry({}, owner_token=old_token)
        replacement_entry = approval._ApprovalEntry({}, owner_token=replacement_token)
        approval._gateway_queues[session_key] = [old_entry, replacement_entry]

        try:
            approval.unregister_gateway_notify(session_key, old_token)

            assert approval._gateway_notify_cbs[session_key] is replacement_cb
            assert approval._gateway_queues[session_key] == [replacement_entry]
            assert old_entry.event.is_set() is True
            assert replacement_entry.event.is_set() is False
        finally:
            approval.unregister_gateway_notify(session_key, replacement_token)

    def test_owner_change_between_enqueue_and_notify_drops_stale_prompt(
        self, monkeypatch
    ):
        from tools import approval

        session_key = "stale-notify-before-user-prompt-session"
        old_notified = []
        replacement_notified = []
        replacement = {}

        def old_cb(data):
            old_notified.append(data)

        def replacement_cb(data):
            replacement_notified.append(data)

        old_token = approval.register_gateway_notify(session_key, old_cb)
        original_fire_hook = approval._fire_approval_hook

        def _replace_owner_on_pre_hook(event, **kwargs):
            result = original_fire_hook(event, **kwargs)
            if event == "pre_approval_request":
                approval.unregister_gateway_notify(session_key, old_token)
                replacement_token = approval.register_gateway_notify(
                    session_key, replacement_cb
                )
                replacement_entry = approval._ApprovalEntry(
                    {"command": "replacement command", "pattern_key": "replacement"},
                    owner_token=replacement_token,
                )
                approval._gateway_queues.setdefault(session_key, []).append(
                    replacement_entry
                )
                replacement["token"] = replacement_token
                replacement["entry"] = replacement_entry
            return result

        monkeypatch.setattr(approval, "_fire_approval_hook", _replace_owner_on_pre_hook)

        try:
            result = approval._await_gateway_decision(
                session_key,
                old_cb,
                {
                    "command": "OLD dangerous command",
                    "pattern_key": "old-danger",
                    "pattern_keys": ["old-danger"],
                    "description": "old approval prompt",
                },
                owner_token=old_token,
            )

            assert result["notify_failed"] is True
            assert result["stale_owner"] is True
            assert old_notified == []
            assert replacement_notified == []
            assert approval._gateway_queues[session_key] == [replacement["entry"]]

            assert approval.resolve_gateway_approval(session_key, "once") == 1
            assert replacement["entry"].result == "once"
        finally:
            approval.clear_session(session_key)
            if "token" in replacement:
                approval.unregister_gateway_notify(session_key, replacement["token"])

    def test_mcp_elicitation_cleanup_cannot_steal_replacement_approval(
        self, monkeypatch
    ):
        from tools import approval

        session_key = "replacement-mcp-elicitation-session"
        old_notified = threading.Event()
        replacement_notified = threading.Event()
        old_done = threading.Event()
        replacement_done = threading.Event()
        results = {}

        monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
        monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 1.0)

        def _request(name, done):
            token = approval.set_current_session_key(session_key)
            try:
                results[name] = approval.request_elicitation_consent(
                    f"{name} request",
                    f"{name} description",
                )
            finally:
                approval.reset_current_session_key(token)
                done.set()

        old_token = approval.register_gateway_notify(
            session_key, lambda _data: old_notified.set()
        )
        old_thread = threading.Thread(
            target=_request, args=("old", old_done), daemon=True
        )
        replacement_thread = None
        replacement_token = None
        try:
            old_thread.start()
            assert old_notified.wait(timeout=0.5)

            replacement_token = approval.register_gateway_notify(
                session_key, lambda _data: replacement_notified.set()
            )
            approval.unregister_gateway_notify(session_key, old_token)

            replacement_thread = threading.Thread(
                target=_request,
                args=("replacement", replacement_done),
                daemon=True,
            )
            replacement_thread.start()
            assert replacement_notified.wait(timeout=0.5)

            assert approval.resolve_gateway_approval(session_key, "once") == 1
            assert replacement_done.wait(timeout=0.5)
            assert old_done.wait(timeout=0.5)
            assert results == {"old": "decline", "replacement": "accept"}
        finally:
            approval.clear_session(session_key)
            approval.unregister_gateway_notify(session_key, replacement_token)
            old_thread.join(timeout=1.0)
            if replacement_thread is not None:
                replacement_thread.join(timeout=1.0)

    def test_mcp_elicitation_rejects_owner_unregistered_before_enqueue(
        self, monkeypatch
    ):
        from tools import approval

        session_key = "stale-before-enqueue-mcp-session"
        old_snapshot_taken = threading.Event()
        release_old_enqueue = threading.Event()
        replacement_notified = threading.Event()
        old_done = threading.Event()
        replacement_done = threading.Event()
        results = {}

        monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
        monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 1.0)
        original_await = approval._await_gateway_decision
        old_token = approval.register_gateway_notify(session_key, lambda _data: None)

        def _await_after_snapshot(
            key, notify_cb, approval_data, *, surface="gateway", owner_token=None
        ):
            if owner_token is old_token:
                old_snapshot_taken.set()
                assert release_old_enqueue.wait(timeout=1.0)
            return original_await(
                key,
                notify_cb,
                approval_data,
                surface=surface,
                owner_token=owner_token,
            )

        monkeypatch.setattr(approval, "_await_gateway_decision", _await_after_snapshot)

        def _request(name, done):
            token = approval.set_current_session_key(session_key)
            try:
                results[name] = approval.request_elicitation_consent(
                    f"{name} request", f"{name} description"
                )
            finally:
                approval.reset_current_session_key(token)
                done.set()

        old_thread = threading.Thread(
            target=_request, args=("old", old_done), daemon=True
        )
        replacement_thread = None
        replacement_token = None
        try:
            old_thread.start()
            assert old_snapshot_taken.wait(timeout=0.5)

            approval.unregister_gateway_notify(session_key, old_token)
            replacement_token = approval.register_gateway_notify(
                session_key, lambda _data: replacement_notified.set()
            )
            release_old_enqueue.set()

            assert old_done.wait(timeout=0.5)
            assert results["old"] == "decline"

            replacement_thread = threading.Thread(
                target=_request,
                args=("replacement", replacement_done),
                daemon=True,
            )
            replacement_thread.start()
            assert replacement_notified.wait(timeout=0.5)
            assert approval.resolve_gateway_approval(session_key, "once") == 1
            assert replacement_done.wait(timeout=0.5)
            assert results == {"old": "decline", "replacement": "accept"}
        finally:
            release_old_enqueue.set()
            approval.clear_session(session_key)
            approval.unregister_gateway_notify(session_key, replacement_token)
            old_thread.join(timeout=1.0)
            if replacement_thread is not None:
                replacement_thread.join(timeout=1.0)



class TestAcpExecAskGate:
    """GHSA-96vc-wcxf-jjff: ACP's _run_agent must set HERMES_INTERACTIVE so
    that tools.approval.check_all_command_guards takes the CLI-interactive
    path (consults the registered callback via prompt_dangerous_approval)
    instead of the non-interactive auto-approve shortcut.

    (HERMES_EXEC_ASK takes the gateway-queue path which requires a
    notify_cb registered in _gateway_notify_cbs — not applicable to ACP,
    which uses a direct callback shape.)"""

    def test_interactive_env_var_routes_to_callback(self, monkeypatch):
        """When HERMES_INTERACTIVE is set and an approval callback is
        registered, a dangerous command must route through the callback."""
        # Clean env
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from tools.approval import check_all_command_guards

        called_with = []

        def fake_cb(command, description, *, allow_permanent=True):
            called_with.append((command, description))
            return "once"

        # Without HERMES_INTERACTIVE: takes auto-approve path, callback NOT called
        result = check_all_command_guards(
            "rm -rf /tmp/test-exec-ask", "local", approval_callback=fake_cb,
        )
        assert result["approved"] is True
        assert called_with == [], (
            "without HERMES_INTERACTIVE the non-interactive auto-approve "
            "path should fire without consulting the callback"
        )

        # With HERMES_INTERACTIVE: callback IS called, approval flows through it
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        called_with.clear()
        result = check_all_command_guards(
            "rm -rf /tmp/test-exec-ask", "local", approval_callback=fake_cb,
        )
        assert called_with, (
            "with HERMES_INTERACTIVE the approval path should consult the "
            "registered callback — this was the ACP bypass in "
            "GHSA-96vc-wcxf-jjff"
        )
        assert result["approved"] is True

    def test_interactive_context_var_routes_to_callback_without_env(
        self, monkeypatch,
    ):
        """Context-local interactive flag must work without touching os.environ.

        Concurrent ACP sessions run on a shared ThreadPoolExecutor, so the
        interactive flag is now a contextvar instead of a process-global env
        var — one session can no longer clobber another's flag mid-run
        (GHSA-96vc-wcxf-jjff).
        """
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from tools.approval import (
            check_all_command_guards,
            reset_hermes_interactive_context,
            set_hermes_interactive_context,
        )

        called_with = []

        def fake_cb(command, description, *, allow_permanent=True):
            called_with.append((command, description))
            return "once"

        tok = set_hermes_interactive_context(True)
        try:
            result = check_all_command_guards(
                "rm -rf /tmp/test-context-interactive",
                "local",
                approval_callback=fake_cb,
            )
        finally:
            reset_hermes_interactive_context(tok)

        assert called_with, (
            "set_hermes_interactive_context(True) should route dangerous "
            "commands through the callback without HERMES_INTERACTIVE in env"
        )
        assert result["approved"] is True
