"""Generation and registration ownership for deferred goal evaluation workers."""

import asyncio
import logging
import threading

from agent.async_utils import safe_schedule_threadsafe

logger = logging.getLogger("gateway.run")


class GatewayGoalControlMixin:
    def _active_goal_run_count(self) -> int:
        """Count generation-owned goal workers still evaluating or gating."""
        lock, controls = self._goal_run_control_state()
        with lock:
            return sum(
                not control["done"].is_set()
                for session_controls in controls.values()
                for control in session_controls
            )

    def _goal_run_control_state(self):
        """Return lazily-created generation-owned goal worker state."""
        lock = getattr(self, "_goal_run_controls_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._goal_run_controls_lock = lock
        controls = getattr(self, "_goal_run_controls", None)
        if controls is None:
            controls = {}
            self._goal_run_controls = controls
        return lock, controls

    def _begin_goal_run_control(self, session_key: str, generation: int) -> dict:
        """Install a cancellation fence for one goal-evaluation generation."""
        control = {
            "generation": int(generation),
            "cancel": threading.Event(),
            "done": threading.Event(),
            "executor_done": threading.Event(),
            "async_abandoned": threading.Event(),
            "thread_id": None,
            "notify_token": None,
            "post_delivery_adapter": None,
            "post_delivery_generation": None,
            "post_delivery_owner_token": None,
        }
        lock, controls = self._goal_run_control_state()
        with lock:
            state = self._peek_session_state(session_key)
            active_generation = int(state.persistent.run_generation) if state is not None else 0
            if session_key and active_generation and int(generation) < active_generation:
                control["cancel"].set()
                control["done"].set()
                return control
            prior_controls = [
                prior
                for prior in controls.get(session_key, ())
                if (
                    not prior["done"].is_set()
                    and int(prior.get("generation", 0)) <= int(generation)
                )
            ]
            for prior in prior_controls:
                prior["cancel"].set()
            controls.setdefault(session_key, []).append(control)
        self._signal_goal_run_cancellation(session_key, prior_controls)
        return control

    def _set_goal_run_thread(self, session_key: str, control: dict) -> None:
        """Bind the worker thread and honor cancellation that raced startup."""
        thread_id = threading.get_ident()
        lock, controls = self._goal_run_control_state()
        with lock:
            if control not in controls.get(session_key, ()):
                return
            control["thread_id"] = thread_id
            cancelled = control["cancel"].is_set()
            if cancelled:
                from tools.interrupt import set_interrupt

                set_interrupt(True, thread_id=thread_id)

    def _finish_goal_run_executor(self, session_key: str, control: dict) -> None:
        """Atomically release a control's reusable executor-thread ownership."""
        from tools.interrupt import clear_current_thread_interrupt

        lock, _controls = self._goal_run_control_state()
        with lock:
            clear_current_thread_interrupt()
            control["thread_id"] = None
            control["executor_done"].set()
            async_abandoned = control["async_abandoned"].is_set()
        if async_abandoned:
            self._finish_goal_run_control(session_key, control)

    def _set_goal_run_notify_token(
        self, session_key: str, control: dict, notify_token: object
    ) -> None:
        lock, controls = self._goal_run_control_state()
        with lock:
            if control in controls.get(session_key, ()):
                control["notify_token"] = notify_token

    def _signal_goal_run_cancellation(
        self, session_key: str, controls: list[dict]
    ) -> None:
        """Release approval waits and interrupt workers already marked cancelled."""
        notify_tokens = []
        retired_callbacks = []
        lock, active_controls = self._goal_run_control_state()
        with lock:
            for control in controls:
                if (
                    control not in active_controls.get(session_key, ())
                    or control["done"].is_set()
                ):
                    continue
                notify_token = control.get("notify_token")
                if notify_token is not None:
                    notify_tokens.append(notify_token)

                callback_adapter = control.get("post_delivery_adapter")
                callback_generation = control.get("post_delivery_generation")
                callback_owner_token = control.get("post_delivery_owner_token")
                if callback_adapter is not None and hasattr(
                    callback_adapter, "pop_post_delivery_callback"
                ):
                    try:
                        callback = callback_adapter.pop_post_delivery_callback(
                            session_key,
                            generation=callback_generation,
                            owner_token=callback_owner_token,
                        )
                    except TypeError:
                        callback = callback_adapter.pop_post_delivery_callback(
                            session_key,
                            generation=callback_generation,
                        )
                    if callback is not None:
                        control["post_delivery_adapter"] = None
                        control["post_delivery_generation"] = None
                        control["post_delivery_owner_token"] = None
                        retired_callbacks.append(control)

                thread_id = control.get("thread_id")
                if thread_id is not None and not control["executor_done"].is_set():
                    from tools.interrupt import set_interrupt

                    set_interrupt(True, thread_id=thread_id)

        for notify_token in notify_tokens:
            from tools.approval import unregister_gateway_notify

            unregister_gateway_notify(session_key, notify_token)
        for control in retired_callbacks:
            self._finish_goal_run_control(session_key, control)

    def _cancel_goal_run(self, session_key: str) -> bool:
        """Cancel every unfinished goal generation for one session."""
        lock, controls = self._goal_run_control_state()
        with lock:
            session_controls = [
                control
                for control in controls.get(session_key, ())
                if not control["done"].is_set()
            ]
            if not session_controls:
                return False
            for control in session_controls:
                control["cancel"].set()

        self._signal_goal_run_cancellation(session_key, session_controls)
        return True

    def _cancel_all_goal_runs(self) -> None:
        lock, controls = self._goal_run_control_state()
        with lock:
            active_by_session = {
                session_key: [
                    control
                    for control in session_controls
                    if not control["done"].is_set()
                ]
                for session_key, session_controls in controls.items()
            }
            active_by_session = {
                session_key: session_controls
                for session_key, session_controls in active_by_session.items()
                if session_controls
            }
            for session_controls in active_by_session.values():
                for control in session_controls:
                    control["cancel"].set()
        for session_key, session_controls in active_by_session.items():
            self._signal_goal_run_cancellation(session_key, session_controls)

    def _finish_goal_run_control(self, session_key: str, control: dict) -> None:
        """Retire only the control owned by the finishing worker generation."""
        control["done"].set()
        lock, controls = self._goal_run_control_state()
        with lock:
            session_controls = controls.get(session_key, [])
            if control in session_controls:
                session_controls.remove(control)
            if not session_controls:
                controls.pop(session_key, None)

    def _has_active_goal_run(self, session_key: str) -> bool:
        lock, controls = self._goal_run_control_state()
        with lock:
            return any(
                not control["done"].is_set()
                for control in controls.get(session_key, ())
            )

    def _notify_goal_approval(self, adapter, source, metadata, loop, approval_data, is_current) -> None:
        """Deliver the deferred gate prompt on the current owner's control lane."""
        from gateway.run import _format_exec_approval_fallback, _interim_metadata, _redact_approval_command
        if adapter is None or not is_current():
            raise RuntimeError("Goal approval owner is no longer active")
        if hasattr(adapter, "pause_typing_for_chat"):
            adapter.pause_typing_for_chat(source.chat_id)
        command = _redact_approval_command(approval_data.get("command", ""))
        description = approval_data.get("description", "dangerous command")
        prefix = getattr(adapter, "typed_command_prefix", "/")
        if approval_data.get("requires_session"):
            preview = command[:200] + ("..." if len(command) > 200 else "")
            message = ("⚠️ **Deferred goal command requires session approval:**\n"
                       f"```\n{preview}\n```\nReason: {description}\n\n"
                       f"Reply `{prefix}approve session` to authorize this exact command "
                       f"for this session, or `{prefix}deny` to cancel.")
        else:
            message = _format_exec_approval_fallback(
                command, description, prefix,
                allow_session=approval_data.get("allow_session", True),
                allow_permanent=approval_data.get("allow_permanent", True),
                smart_denied=approval_data.get("smart_denied", False),
            )
        async def send_current():
            if not is_current():
                raise RuntimeError("Goal approval owner changed before delivery")
            return await adapter.send(source.chat_id, message, metadata=_interim_metadata(
                {**(metadata or {}), "is_approval_prompt": True}))
        future = safe_schedule_threadsafe(send_current(), loop, logger=logger,
                                         log_message="Goal approval text-send scheduling error")
        if future is None:
            raise RuntimeError("Goal approval event loop is unavailable")
        result = future.result(timeout=15)
        if getattr(result, "success", True) is False:
            raise RuntimeError("Goal approval delivery failed")
