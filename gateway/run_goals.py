"""Goal/heartbeat continuation, post-turn hooks and loop-wakeup watcher methods for GatewayRunner.

Split out of ``gateway/run.py``; bound onto ``GatewayRunner`` via the MRO.
``gateway.run`` internals are imported lazily inside method bodies (import cycle),
so ``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from gateway.platforms.base import MessageEvent, MessageType

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401
    from gateway.run_turn_runner import TurnRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


from gateway.run_goal_control import GatewayGoalControlMixin
from gateway.session import SessionContext


class GatewayGoalsMixin(GatewayGoalControlMixin):
    """Goal/heartbeat continuation, post-turn hooks and loop-wakeup watcher methods for GatewayRunner."""

    # ── /goal — persistent cross-turn goals (Ralph-style loop) ──────────
    def _goal_max_turns_from_config(self) -> int:
        """Configured /goal turn budget. GatewayRunner.config is a GatewayConfig dataclass, so the
        top-level ``goals`` block is only reachable via hermes_cli.config.load_config()."""
        try:
            goals_cfg = (
                (self.config or {}).get("goals", {})
                if isinstance(self.config, dict)
                else getattr(self.config, "goals", {}) or {}
            )
            if not goals_cfg:
                from hermes_cli.config import load_config

                goals_cfg = (load_config() or {}).get("goals") or {}
            return int(goals_cfg.get("max_turns", 20) or 20)
        except Exception:
            return 20

    async def _warm_goals_session_db(self, label: str) -> None:
        """Warm the goals SessionDB cache off-loop (best-effort): a cold cache runs the state.db
        init on the loop thread and freezes the loop. The executor hop keeps the profile home
        override alive under multiplex; a failed warm-up is a bounded stall, never a crash."""
        try:
            from hermes_cli.goals import _get_session_db as _warm_goals_db

            await self._run_in_executor_with_context(_warm_goals_db)
        except Exception as exc:
            logger.warning("%s: session DB warm-up failed: %s", label, exc)

    async def _session_entry_for_manager(self, event: "MessageEvent", label: str):
        """Session entry for a /goal or /heartbeat manager, or None when lookup fails. Warms the
        SessionDB cache first (a cold cache drops the first write while the reply claims it was
        set). Internal events never touch activity (idle/daily reset clock)."""
        await self._warm_goals_session_db(label)
        try:
            session_entry = await self.async_session_store.get_or_create_session(
                event.source, touch_activity=not bool(getattr(event, "internal", False)),
            )
        except Exception as exc:
            logger.debug("%s: session lookup failed: %s", label, exc)
            return None
        return session_entry if getattr(session_entry, "session_id", None) else None

    async def _manager_for_event(self, event: "MessageEvent", kind: str, load):
        """``(manager, session_entry)`` for *kind* ("goal"/"heartbeat"), or ``(None, None)``.
        ``load()`` imports the manager class and returns a ``session_id -> manager`` factory."""
        try:
            factory = load()
        except Exception as exc:
            logger.debug("%s manager unavailable: %s", kind, exc)
            return None, None
        session_entry = await self._session_entry_for_manager(event, f"{kind} manager")
        if session_entry is None:
            return None, None
        return factory(session_entry.session_id), session_entry

    async def _get_goal_manager_for_event(self, event: "MessageEvent"):
        """Return ``(GoalManager, session_entry)`` for this event, or ``(None, None)``."""
        def _load():
            from hermes_cli.goals import GoalManager
            max_turns = self._goal_max_turns_from_config()
            return lambda sid: GoalManager(session_id=sid, default_max_turns=max_turns)
        return await self._manager_for_event(event, "goal", _load)

    async def _get_heartbeat_manager_for_event(self, event: "MessageEvent"):
        """Return ``(HeartbeatManager, session_entry)`` for this event, or ``(None, None)``."""
        def _load():
            from hermes_cli.heartbeat import HeartbeatManager
            return lambda sid: HeartbeatManager(session_id=sid)
        return await self._manager_for_event(event, "heartbeat", _load)

    @staticmethod
    def _synthetic_prompt_event(source: Any, text: str, *, internal: bool = False) -> MessageEvent:
        """Build the TEXT event used to inject a goal/heartbeat/loop prompt into a session."""
        return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, internal=internal)

    def _register_heartbeat_watch(self, quick_key: str, source: Any, session_id: str) -> None:
        """Track a session with an active heartbeat (``quick_key`` → ``(source, session_id)``) and
        start the poller. In-memory by design: heartbeat STATE survives restarts in SessionDB, but
        firing resumes only when the user touches /heartbeat again."""
        watch = getattr(self, "_heartbeat_watch", None)
        if watch is None:
            watch = self._heartbeat_watch = {}
        watch[quick_key] = (source, session_id)
        self._start_heartbeat_poller()

    def _unregister_heartbeat_watch(self, quick_key: str) -> None:
        (getattr(self, "_heartbeat_watch", None) or {}).pop(quick_key, None)

    async def _heartbeat_poll_once(self, watch: dict) -> None:
        """One heartbeat poll pass: enqueue every due prompt of a non-busy watched session."""
        # Off-loop warm-up covers the degraded path where /heartbeat's own warm-up failed.
        await self._warm_goals_session_db("heartbeat poll")
        for quick_key, (source, session_id) in list(watch.items()):
            try:
                if quick_key in self._running_agents:
                    continue  # busy sessions coalesce their tick to the next idle poll
                from hermes_cli.heartbeat import HeartbeatManager

                mgr = HeartbeatManager(session_id=session_id)
                if not mgr.has_heartbeat():
                    watch.pop(quick_key, None)
                    continue
                prompt = mgr.due_prompt()
                adapter = self._adapter_for_source(source) if prompt else None
                if adapter is not None:
                    self._enqueue_fifo(quick_key, self._synthetic_prompt_event(source, prompt), adapter)
            except Exception as exc:
                logger.debug("heartbeat poll for %s failed: %s", quick_key, exc)

    def _start_heartbeat_poller(self) -> None:
        """Start the single gateway-wide heartbeat poll task (idempotent)."""
        existing = getattr(self, "_heartbeat_poll_task", None)
        if existing is not None and not existing.done():
            return

        from hermes_cli.heartbeat import POLL_SECONDS

        async def _poll_loop():
            while True:
                await asyncio.sleep(POLL_SECONDS)
                watch = getattr(self, "_heartbeat_watch", None)
                if watch:
                    await self._heartbeat_poll_once(watch)

        try:
            task = self._heartbeat_poll_task = asyncio.create_task(_poll_loop())
            # PERMANENT once started (infinite loop) — tag it like a _spawn_supervised watcher so
            # _scale_to_zero_has_live_background_work() doesn't treat the gateway as busy forever.
            task._hermes_supervised_watcher = True  # type: ignore[attr-defined]
            _bg = getattr(self, "_background_tasks", None)
            if _bg is not None:
                _bg.add(task)
                task.add_done_callback(_bg.discard)
        except Exception:
            logger.debug("Failed to start heartbeat poller", exc_info=True)

    def _goal_notice_adapter(self, source: Any):
        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.debug("goal continuation: no adapter for %s", getattr(source, "platform", None))
        return adapter

    async def _send_goal_status_notice(self, source: Any, message: str) -> None:
        """Send a /goal judge status line back to the originating chat/thread."""
        adapter = self._goal_notice_adapter(source)
        if not adapter:
            return
        metadata = None
        with suppress(Exception):
            metadata = self._thread_metadata_for_source(source)
        result = await adapter.send(source.chat_id, message, metadata=metadata)
        if result is not None and not getattr(result, "success", True):
            logger.warning(
                "goal continuation: status send failed: %s", getattr(result, "error", "unknown error"),
            )

    async def _defer_goal_status_notice_after_delivery(
        self,
        source: Any,
        message: str,
        *,
        session_key: str = "",
        run_generation: int | None = None,
        control: dict | None = None,
    ) -> bool:
        """Send a /goal status line after the main response is delivered.

        The gateway message handler returns the agent response to the platform
        adapter, which sends it after this method's caller has returned. For a
        natural Discord/Telegram reading order, goal status belongs after that
        send. Platform adapters provide a one-shot post-delivery callback for
        exactly this boundary; when unavailable, fall back to direct awaited
        delivery rather than silently dropping the notice.

        When ``control`` is supplied, a deferred callback owns goal-run shutdown
        accounting until the callback fires or cancellation removes that exact
        owner token.
        """
        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.debug("goal continuation: no adapter for %s", getattr(source, "platform", None))
            return False

        deferred_owner = False
        effective_generation = run_generation
        if effective_generation is None and control is not None:
            effective_generation = int(control["generation"])

        async def _deliver() -> None:
            try:
                if control is not None and (
                    control["cancel"].is_set()
                    or not self._is_session_run_current(
                        session_key,
                        effective_generation or 0,
                    )
                ):
                    return
                await self._send_goal_status_notice(source, message)
            except Exception as exc:
                logger.warning("goal continuation: status send failed: %s", exc, exc_info=True)
            finally:
                if control is not None and deferred_owner:
                    self._finish_goal_run_control(session_key, control)

        if not session_key:
            try:
                session_key = self._session_key_for_source(source)
            except Exception:
                session_key = ""

        if session_key and hasattr(adapter, "register_post_delivery_callback"):
            try:
                generation = effective_generation
                if generation is None:
                    active = getattr(adapter, "_active_sessions", {}).get(session_key)
                    if active is not None:
                        generation = getattr(active, "_hermes_run_generation", None)

                lock, controls = self._goal_run_control_state()
                with lock:
                    if control is not None and (
                        control not in controls.get(session_key, ())
                        or control["cancel"].is_set()
                    ):
                        return False
                    owner_token = object() if control is not None else None
                    try:
                        adapter.register_post_delivery_callback(
                            session_key,
                            _deliver,
                            generation=generation,
                            owner_token=owner_token,
                            on_discard=(lambda: self._finish_goal_run_control(session_key, control))
                            if control is not None else None,
                        )
                    except TypeError:
                        owner_token = None
                        adapter.register_post_delivery_callback(
                            session_key,
                            _deliver,
                            generation=generation,
                        )

                    callbacks = getattr(adapter, "_post_delivery_callbacks", None)
                    entry = callbacks.get(session_key) if isinstance(callbacks, dict) else None
                    accepted = callbacks is None or entry is not None
                    if accepted and generation is not None and isinstance(entry, tuple):
                        accepted = int(entry[0]) == int(generation)
                    elif accepted and generation is not None and isinstance(entry, list):
                        accepted = any(
                            isinstance(item, dict)
                            and item.get("callback") is _deliver
                            and item.get("generation") is not None
                            and int(item.get("generation")) == int(generation)
                            and item.get("owner_token") is owner_token
                            for item in entry
                        )
                    if not accepted:
                        return False
                    if control is not None:
                        control["post_delivery_adapter"] = adapter
                        control["post_delivery_generation"] = generation
                        control["post_delivery_owner_token"] = owner_token
                        deferred_owner = True
                return True
            except Exception as exc:
                logger.debug("goal continuation: post-delivery callback registration failed: %s", exc)

        await _deliver()
        return False

    async def _post_turn_manager(self, session_entry: Any, label: str, module: str, load):
        """Shared head of the post-turn hooks: ``load()`` imports the manager module and returns a
        ``session_id -> manager`` factory; None when unavailable / no session id. Warms the
        SessionDB cache first — a cold cache at the turn boundary drops the read/write."""
        try:
            factory = load()
        except Exception as exc:
            logger.debug("%s: %s module unavailable: %s", label, module, exc)
            return None
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return None
        await self._warm_goals_session_db(label)
        return factory(sid)

    async def _post_turn_goal_continuation(self, *, session_entry, source, final_response, run_generation=None):
        with self._profile_scope_for_source(source):
            await self._post_turn_goal_continuation_scoped(
                session_entry=session_entry, source=source, final_response=final_response,
                run_generation=run_generation,
            )

    async def _post_turn_goal_continuation_scoped(
        self,
        *,
        session_entry: Any,
        source: Any,
        final_response: str,
        run_generation: int | None = None,
    ) -> None:
        """Run the goal judge after a gateway turn and, if still active,
        enqueue a continuation prompt for the same session.

        Goal evaluation and gate execution are blocking-capable, so they run in
        the gateway executor under session context. The async side owns a
        generation-scoped control record until every status/continuation side
        effect is complete or cancellation removes the exact owner.
        """
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return

        # Preserve the current off-loop cold SessionDB initialization before
        # GoalManager accesses the cached handle in its executor worker.
        await self._warm_goals_session_db(label="goal")
        max_turns = self._goal_max_turns_from_config()

        session_key = getattr(session_entry, "session_key", None) or ""
        if not session_key:
            try:
                session_key = self._session_key_for_source(source)
            except Exception:
                session_key = ""
        if run_generation is None:
            state = self._peek_session_state(session_key)
            run_generation = int(state.persistent.run_generation) if state is not None else 0
        control = self._begin_goal_run_control(session_key, run_generation)
        cancelled_result = object()

        def _goal_run_is_current() -> bool:
            return bool(
                not control["cancel"].is_set()
                and self._is_session_run_current(session_key, run_generation)
            )

        from tools.approval import register_gateway_notify, unregister_gateway_notify
        from tools.approval_context import (
            reset_deferred_command_session_authorization_required, reset_current_session_key,
            set_current_session_key, set_deferred_command_session_authorization_required,
        )

        adapter = self._adapter_for_source(source) if source is not None else None
        try:
            metadata = self._thread_metadata_for_source(source) if source is not None else None
        except Exception:
            metadata = None
        loop = asyncio.get_running_loop()

        def _approval_notify_for_goal(approval_data: dict) -> None:
            self._notify_goal_approval(adapter, source, metadata, loop, approval_data, _goal_run_is_current)

        def _evaluate_goal_sync_inner():
            from hermes_cli.goals import GoalManager, gather_background_processes

            mgr = GoalManager(session_id=sid, default_max_turns=max_turns)
            if not mgr.is_active():
                return None
            try:
                background_processes = gather_background_processes()
            except Exception:
                background_processes = None
            return mgr.evaluate_after_turn(
                final_response or "",
                user_initiated=True,
                background_processes=background_processes,
                is_current=_goal_run_is_current,
                write_lock=self._goal_run_control_state()[0],
            )

        def _evaluate_goal_sync():
            self._set_goal_run_thread(session_key, control)
            try:
                if control["cancel"].is_set():
                    return cancelled_result
                decision = _evaluate_goal_sync_inner()
                if control["cancel"].is_set():
                    return cancelled_result
                return decision
            finally:
                self._finish_goal_run_executor(session_key, control)

        session_context = SessionContext(
            source=source,
            connected_platforms=[],
            home_channels={},
            session_key=session_key,
            session_id=sid,
            created_at=getattr(session_entry, "created_at", None),
            updated_at=getattr(session_entry, "updated_at", None),
        )
        session_tokens = self._set_session_env(session_context)
        approval_token = set_current_session_key(session_key)
        deferred_authorization_token = (
            set_deferred_command_session_authorization_required(True)
        )
        notify_registered = bool(adapter is not None and session_key)
        notify_owner_token = None
        if notify_registered:
            notify_owner_token = register_gateway_notify(
                session_key, _approval_notify_for_goal
            )
            self._set_goal_run_notify_token(
                session_key, control, notify_owner_token
            )
        try:
            # A cancelled asyncio waiter must not cancel a queued executor
            # future before its worker can observe the fence and retire control.
            decision = await asyncio.shield(self._run_in_executor_with_context(_evaluate_goal_sync))
        except BaseException:
            with self._goal_run_control_state()[0]:
                control["cancel"].set()
                control["async_abandoned"].set()
            self._signal_goal_run_cancellation(session_key, [control])
            if control["executor_done"].is_set():
                self._finish_goal_run_control(session_key, control)
            raise
        finally:
            if notify_registered:
                unregister_gateway_notify(session_key, notify_owner_token)
            reset_deferred_command_session_authorization_required(
                deferred_authorization_token
            )
            reset_current_session_key(approval_token)
            self._clear_session_env(session_tokens)

        notice_deferred = False
        try:
            if (
                decision is cancelled_result
                or decision is None
                or not _goal_run_is_current()
            ):
                return
            msg = decision.get("message") or ""

            if msg and source is not None and _goal_run_is_current():
                notice_deferred = await self._defer_goal_status_notice_after_delivery(
                    source,
                    msg,
                    session_key=session_key,
                    run_generation=run_generation,
                    control=control,
                )

            if not _goal_run_is_current() or not decision.get("should_continue"):
                return

            prompt = decision.get("continuation_prompt") or ""
            if not prompt or source is None:
                return

            try:
                adapter = self._adapter_for_source(source)
                _quick_key = self._session_key_for_source(source)
                if adapter and _quick_key and _goal_run_is_current():
                    cont_event = self._synthetic_prompt_event(source, prompt)
                    self._enqueue_fifo(_quick_key, cont_event, adapter)
            except Exception as exc:
                logger.debug("goal continuation: enqueue failed: %s", exc)
        finally:
            if not notice_deferred:
                self._finish_goal_run_control(session_key, control)

    async def _run_post_turn_hooks(
        self, *, agent_result: Any, source: Any, is_internal: bool, event: Any = None, run_generation: int | None = None,
    ) -> None:
        """Run goal and loop bookkeeping after an agent turn returns."""
        final_text = self._final_text_for_post_turn_hooks(agent_result, event)
        try:
            session_entry = await self.async_session_store.get_or_create_session(
                source, touch_activity=not is_internal,
            )
        except Exception as exc:
            logger.debug("post-turn session resolution failed: %s", exc)
            return
        # Empty interrupted/errored responses must not drive /goal, but an in-flight /loop tick
        # still needs to be released and rescheduled.
        hooks = [("loop completion", self._post_turn_loop_completion)]
        if final_text.strip():
            hooks.insert(0, ("goal continuation", self._post_turn_goal_continuation))
        for label, hook in hooks:
            try:
                kwargs = {"run_generation": run_generation} if label == "goal continuation" else {}
                await hook(session_entry=session_entry, source=source, final_response=final_text, **kwargs)
            except Exception as exc:
                logger.debug("%s hook failed: %s", label, exc)

    @staticmethod
    def _final_text_for_post_turn_hooks(agent_result, event=None) -> str:
        """Text for /goal and /loop after a gateway turn. Streamed turns return None from
        _handle_message_with_agent (already_sent); the delivered reply is stashed on the event."""
        text = ""
        if isinstance(agent_result, dict):
            text = str(agent_result.get("final_response") or "")
        elif isinstance(agent_result, str):
            text = agent_result
        if text.strip():
            return text
        streamed = getattr(event, "_streamed_final_response", None)
        return streamed if isinstance(streamed, str) and streamed.strip() else text

    async def _post_turn_loop_completion(
        self, *, session_entry: Any, source: Any, final_response: str,
    ) -> None:
        """Complete a /loop wakeup tick after a gateway turn. No-op unless a tick is in flight
        (``awaiting_response``, set when the wakeup was injected); applies the LOOP_COMPLETE marker
        / --until judge / caps and schedules the next tick for the idle wakeup watcher."""
        def _load():
            from hermes_cli.loops import LoopManager
            return lambda sid: LoopManager(session_id=sid)

        mgr = await self._post_turn_manager(session_entry, "loop completion", "loops", _load)
        state = mgr.state if mgr is not None else None
        if state is None or not state.awaiting_response:
            return
        # The --until judge is a sync aux-LLM call — keep it off the event loop.
        decision = await asyncio.get_running_loop().run_in_executor(
            None, mgr.complete_tick, final_response or ""
        )
        msg = decision.get("message") or ""
        if msg and source is not None:
            await self._defer_goal_status_notice_after_delivery(source, msg)

    async def _loop_wakeup_fire_one(self, sid: str, state: Any, now: float, warned_no_route: set) -> None:
        """Inject one due /loop wakeup into its session, applying every deferral rule."""
        from hermes_cli.loops import LoopManager, goal_blocks_loop_tick

        if state.awaiting_response or now < state.next_due_at:
            return
        route = state.route or {}
        platform_name = route.get("platform", "")
        chat_id = route.get("chat_id", "")
        if not platform_name or not chat_id:
            return  # CLI / TUI-owned loop — their own schedulers drive it.
        adapter = next((a for p, a in self.adapters.items() if p.value == platform_name), None)
        if adapter is None:
            if sid not in warned_no_route:
                warned_no_route.add(sid)
                logger.debug(
                    "loop wakeup: no adapter for platform %r (session %s)", platform_name, sid,
                )
            return

        source = self._build_process_event_source({
            "session_key": "",
            "platform": platform_name,
            "chat_id": chat_id,
            **{k: route.get(k, "") for k in ("chat_type", "thread_id", "user_id", "user_name")},
        })
        if source is None:
            return
        session_key = None
        with suppress(Exception):
            session_key = self._session_key_for_source(source)
        if session_key and session_key in self._running_agents:
            return  # busy — stays due, next scan retries
        if goal_blocks_loop_tick(sid):
            return

        mgr = LoopManager(session_id=sid)
        if not mgr.is_due(now):
            return
        # fire_tick()/complete_tick() are writes (BEGIN IMMEDIATE) taking the SessionDB writer lock; a slow
        # writer elsewhere holding it while the loop thread blocked froze the gateway until the watchdog
        # fired. The context-preserving executor keeps the profile HERMES_HOME override under multiplex.
        wakeup = await self._run_in_executor_with_context(mgr.fire_tick)
        if not wakeup:
            return
        # #85957: after the parent turn's event.complete the CLIENT owns the next turn on this stateless
        # surface. Persist the completion as a durable delivery row — never self-post it as a new role=user
        # prompt.
        # #85957: same client-owns-the-turn rule as the raw-key branch above — persist the completion as a
        # delivery row, never self-post it as a new role=user prompt.
        try:
            logger.info(
                "loop wakeup #%s — injecting for %s chat=%s thread=%s",
                mgr.state.ticks_fired if mgr.state else "?",
                platform_name, source.chat_id, source.thread_id,
            )
            await adapter.handle_message(self._synthetic_prompt_event(source, wakeup, internal=True))
            # Slash-command loops dispatch through the command path and never hit the post-turn
            # completion hook — complete the tick immediately (caps + scheduling).
            if wakeup.lstrip().startswith("/"):
                await self._run_in_executor_with_context(mgr.complete_tick, "")
        except Exception as exc:
            logger.warning("loop wakeup injection failed for %s: %s", sid, exc)
            with suppress(Exception):
                mgr.abandon_tick()

    async def _loop_wakeup_watcher(self, interval: float = 15.0) -> None:
        """Fire due /loop wakeups for idle gateway sessions: a coarse ticker scans persisted loops
        (SessionDB ``loop:*`` rows) and injects each due prompt via the synthetic-message path.
        Deferrals: session running a turn (FIFO would race the live turn); active non-parked /goal
        (goal owns the idle boundary); no routing metadata (one-time warning)."""
        await asyncio.sleep(5)  # let platforms finish connecting
        warned_no_route: set = set()
        while self._running:
            try:
                from hermes_cli.loops import list_active_loops

                # Warm once per scan: the scan reads every persisted loop and a cold cache would
                # run the state.db init on the loop thread before the first read.
                await self._warm_goals_session_db("loop wakeup")
                # Off-loop too: the read is lock-free under WAL but convoys on the writer lock without it.
                active_loops = await self._run_in_executor_with_context(list_active_loops)
                now = time.time()
                for sid, state in active_loops:
                    await self._loop_wakeup_fire_one(sid, state, now, warned_no_route)
            except Exception as exc:
                logger.debug("loop wakeup watcher error: %s", exc)
            await asyncio.sleep(interval)
