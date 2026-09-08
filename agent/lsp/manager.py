"""Service-level orchestration for LSP clients.

:class:`LSPService` bridges the synchronous file_operations layer and the async
:class:`agent.lsp.client.LSPClient`: one asyncio loop in a background thread, one lazily
spawned client per ``(server_id, workspace_root)`` — servers flagged ``multi_root`` (pyright) get ONE
client per ``server_id`` and further roots (typically sibling git worktrees) are attached to the running
process via ``workspace/didChangeWorkspaceFolders`` — a **broken-set** of pairs that failed
to spawn/initialize (never retried for the life of the service), and a **delta baseline**
per file (``snapshot_baseline()`` runs BEFORE a write; the next ``get_diagnostics_sync()``
returns only diagnostics not in it).  Off unless config enables it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.lsp import eventlog
from agent.lsp.outcome import DiagnosticOutcome, normalized_text, failure_reason
from agent.lsp.client import DIAGNOSTICS_DOCUMENT_WAIT, LSPClient, _diagnostic_key as _diag_key
from agent.lsp.servers import ServerContext, ServerDef, find_server_for_file, language_id_for
from agent.lsp.workspace import clear_cache, resolve_workspace_for_file

logger = logging.getLogger("agent.lsp.manager")

DEFAULT_IDLE_TIMEOUT = 600  # seconds; servers idle for >10min get reaped
MIN_IDLE_TIMEOUT = 30  # floor for config values; must exceed any per-op wait budget

_Key = Tuple[str, str]
_Diags = List[Dict[str, Any]]


def _client_key(srv: ServerDef, root: str) -> _Key:
    """Cache key for the client serving ``root``: multi-root servers share one process per
    ``server_id``; everything else is keyed per resolved project root."""
    return (srv.server_id, "" if srv.multi_root else root)


class _BackgroundLoop:
    """A daemon thread owning one asyncio loop; :meth:`run` blocks on a coroutine."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_forever, name="hermes-lsp-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run_forever(self) -> None:
        loop = self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def run(self, coro, *, timeout: Optional[float] = None) -> Any:
        """Submit a coroutine to the loop and block for its result (or raise)."""
        from agent.async_utils import safe_schedule_threadsafe
        if self._loop is None:
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError("background loop not started")
        if (fut := safe_schedule_threadsafe(coro, self._loop)) is None:
            raise RuntimeError("background loop not running")
        try:
            return fut.result(timeout=timeout)
        except BaseException:
            fut.cancel()
            raise

    def stop(self) -> None:
        loop, self._loop = self._loop, None
        thread, self._thread = self._thread, None
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if thread is not None:
            thread.join(timeout=2.0)


class LSPService:
    """LSP lifecycle owner; normal writes use the singleton, explicit CLI checks own an instance."""

    def __init__(
        self, *, enabled: bool, wait_mode: str, wait_timeout: float, install_strategy: str,
        binary_overrides: Optional[Dict[str, List[str]]] = None,
        env_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        init_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        disabled_servers: Optional[List[str]] = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self._enabled = enabled
        self._wait_mode = wait_mode if wait_mode in {"document", "full"} else "document"
        self._wait_timeout = wait_timeout
        self._install_strategy = install_strategy
        self._binary_overrides = binary_overrides or {}
        self._env_overrides = env_overrides or {}
        self._init_overrides = init_overrides or {}
        self._disabled_servers = set(disabled_servers or [])
        self._idle_timeout = idle_timeout

        self._loop = _BackgroundLoop()
        if self._enabled:
            self._loop.start()

        # Per-(server_id, workspace_root) state
        self._clients: Dict[_Key, LSPClient] = {}
        self._broken: set = set()
        self._spawning: Dict[_Key, asyncio.Future] = {}
        self._starting: Dict[LSPClient, asyncio.Task] = {}
        self._last_used: Dict[_Key, float] = {}
        self._state_lock = threading.Lock()
        self._idle_reaper_task: Optional[asyncio.Task] = None
        # abs file path → diagnostics snapshot taken immediately before a write.
        self._delta_baseline: Dict[str, DiagnosticOutcome] = {}
        self._spawn_failures: Dict[_Key, str] = {}

        if self._enabled and self._idle_timeout > 0:
            self._loop.run(self._start_idle_reaper(), timeout=2.0)

    @classmethod
    def create_from_config(cls, *, install_strategy_override: Optional[str] = None) -> Optional["LSPService"]:
        """Build from profile config; an owned check may disable acquisition without editing config."""
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly()
        except Exception as e:  # noqa: BLE001
            logger.debug("LSP config load failed: %s", e)
            return None
        lsp_cfg = cfg.get("lsp") if isinstance(cfg, dict) else None
        lsp_cfg = lsp_cfg if isinstance(lsp_cfg, dict) else {}
        try:
            idle_timeout = float(lsp_cfg.get("idle_timeout", DEFAULT_IDLE_TIMEOUT))
        except (TypeError, ValueError):
            idle_timeout = DEFAULT_IDLE_TIMEOUT
        if 0 < idle_timeout < MIN_IDLE_TIMEOUT:
            # Below the per-op wait budget the reaper could kill a client mid-flight and the outer
            # timeout would then mark the pair broken for the process lifetime.  Clamp (0 still disables).
            idle_timeout = MIN_IDLE_TIMEOUT
        servers_cfg = lsp_cfg.get("servers") or {}
        servers = {n: c for n, c in servers_cfg.items() if isinstance(c, dict)} if isinstance(servers_cfg, dict) else {}
        return cls(
            enabled=bool(lsp_cfg.get("enabled", True)),
            wait_mode=lsp_cfg.get("wait_mode", "document"),
            wait_timeout=float(lsp_cfg.get("wait_timeout", DIAGNOSTICS_DOCUMENT_WAIT)),
            install_strategy=(install_strategy_override if install_strategy_override is not None
                              else lsp_cfg.get("install_strategy", "auto")),
            binary_overrides={n: c["command"] for n, c in servers.items()
                              if isinstance(c.get("command"), list) and c["command"]},
            env_overrides={n: {k: str(v) for k, v in c["env"].items()} for n, c in servers.items()
                           if isinstance(c.get("env"), dict)},
            init_overrides={n: c["initialization_options"] for n, c in servers.items()
                            if isinstance(c.get("initialization_options"), dict)},
            disabled_servers=[n for n, c in servers.items() if c.get("disabled")],
            idle_timeout=idle_timeout,
        )

    # ---- public API ----

    def is_active(self) -> bool:
        """Return True iff this service should be consulted at all."""
        return self._enabled

    def _broken_key(self, srv: ServerDef, file_path: str) -> Optional[_Key]:
        """``(server_id, per-server root)`` broken-set key, or ``None`` when the file isn't gated in.

        Falls back to the workspace root when the per-server resolver fails —
        the same key ``_get_or_spawn`` would have used when it failed.
        """
        ws_root, gated = resolve_workspace_for_file(file_path)
        if not (ws_root and gated):
            return None
        try:
            return (srv.server_id, srv.resolve_root(file_path, ws_root) or ws_root)
        except Exception:  # noqa: BLE001
            return (srv.server_id, ws_root)

    def enabled_for(self, file_path: str) -> bool:
        """True iff LSP should run for this file: registered non-disabled server, git workspace,
        and pair not broken (a failed server costs nothing until ``hermes lsp restart`` / exit)."""
        srv = find_server_for_file(file_path) if self._enabled else None
        if srv is None or srv.server_id in self._disabled_servers:
            return False
        key = self._broken_key(srv, file_path)
        return key is not None and key not in self._broken

    def _ineligible_outcome(self, file_path: str) -> Optional[DiagnosticOutcome]:
        if not self._enabled:
            return DiagnosticOutcome("not_checked", "disabled")
        srv = find_server_for_file(file_path)
        if srv is None:
            return DiagnosticOutcome("not_checked", "unsupported_file")
        if srv.server_id in self._disabled_servers:
            return DiagnosticOutcome("not_checked", "server_disabled")
        ws, gated = resolve_workspace_for_file(file_path)
        if not (ws and gated):
            return DiagnosticOutcome("not_checked", "no_project")
        root = srv.resolve_root(file_path, ws)
        if root is None:
            return DiagnosticOutcome("not_checked", "server_excluded")
        key = (srv.server_id, root)
        if key in self._broken:
            return DiagnosticOutcome("no_verdict", self._spawn_failures.get(key, "server_error"))
        return None

    def snapshot_baseline(self, file_path: str) -> None:
        """Best-effort pre-write evidence. Missing data is not a clean baseline."""
        try:
            result = self._ineligible_outcome(file_path)
            if result is None:
                result = self._loop.run(self._query_outcome_async(file_path, snapshot=True),
                                        timeout=max(8.0, self._wait_timeout + 3.0))
        except Exception as exc:
            self._mark_broken_for_file(file_path, exc)
            result = DiagnosticOutcome("no_verdict", failure_reason(exc))
        self._delta_baseline[os.path.abspath(file_path)] = result

    def get_diagnostics_sync(
        self, file_path: str, *, delta: bool = True, timeout: Optional[float] = None,
        line_shift: Optional[Callable[[int], Optional[int]]] = None,
    ) -> _Diags:
        """Compatibility list API; missing evidence still returns an empty list."""
        result = self.get_diagnostic_outcome_sync(file_path, delta=delta, timeout=timeout, line_shift=line_shift)
        return result.delta if result.delta is not None else result.diagnostics

    def get_diagnostic_outcome_sync(
        self, file_path: str, *, delta: bool = True, timeout: Optional[float] = None,
        line_shift: Optional[Callable[[int], Optional[int]]] = None,
        pre_content: Optional[str] = None, post_content: Optional[str] = None, read_only: bool = False,
    ) -> DiagnosticOutcome:
        """Return one source-attributed result. read_only requires supplied text and reports full counts."""
        if read_only:
            delta = False
            if post_content is None:
                return DiagnosticOutcome("not_checked", "invalid_input")
        baseline = self._delta_baseline.get(os.path.abspath(file_path)) if delta else None
        try:
            result = self._ineligible_outcome(file_path)
            if result is None:
                query = (self._query_outcome_async(file_path, read_only_text=normalized_text(post_content))
                         if read_only else self._query_outcome_async(file_path))
                result = self._loop.run(query,
                    timeout=timeout if timeout is not None else self._wait_timeout + 2.0)
        except Exception as exc:
            self._mark_broken_for_file(file_path, exc)
            result = DiagnosticOutcome("no_verdict", failure_reason(exc))
        known = baseline is not None and baseline.status == "fresh"
        if known and pre_content is not None and baseline.text != normalized_text(pre_content):
            known = False
        result.baseline = ("available" if known else "unavailable") if delta else "not_requested"
        if delta and not known:
            result.baseline_reason = (baseline.reason if baseline and baseline.status != "fresh" else
                                      "source_changed" if baseline else "no_fresh_diagnostics")
        if result.status != "fresh":
            return result
        if post_content is not None and result.text != normalized_text(post_content):
            return DiagnosticOutcome("no_verdict", "source_changed", baseline=result.baseline,
                                     baseline_reason=result.baseline_reason)
        if known:
            previous = baseline.diagnostics
            if line_shift is not None:
                from agent.lsp.range_shift import shift_baseline
                previous = shift_baseline(previous, line_shift)
            seen = {_diag_key(d) for d in previous}
            result.delta = [d for d in result.diagnostics if _diag_key(d) not in seen]
        # Roll forward the SAME full snapshot, including a fresh empty repair.
        if delta:
            self._delta_baseline[os.path.abspath(file_path)] = result
        if result.diagnostics:
            eventlog.log_diagnostic_outcome(result.server_id, file_path, len(result.diagnostics),
                                           len(result.delta) if result.delta is not None else None)
        else:
            eventlog.log_clean(result.server_id, file_path)
        return result

    def _mark_broken_for_file(self, file_path: str, exc: BaseException) -> None:
        """Mark the file's ``(server_id, root)`` pair broken after an outer timeout/error.
        The outer ``_loop.run`` timeout cancels the in-flight spawn before ``_get_or_spawn`` could record
        the failure; without this every later write would re-pay the full timeout.  Also kills any
        half-initialized client and logs the failure once."""
        srv = find_server_for_file(file_path)
        key = self._broken_key(srv, file_path) if srv is not None else None
        if key is None:
            return
        already_broken = key in self._broken
        self._broken.add(key)
        ckey = _client_key(srv, key[1])
        with self._state_lock:
            client = self._clients.pop(ckey, None)
            self._last_used.pop(ckey, None)
        if client is not None:
            try:
                # Fire-and-forget shutdown — we're already on a slow path.
                self._loop.run(client.shutdown(), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
        if not already_broken:
            eventlog.log_spawn_failed(key[0], key[1], exc)

    def shutdown(self) -> None:
        """Tear down all clients and stop the background loop."""
        if not self._enabled:
            return
        try:
            self._loop.run(self._shutdown_async(), timeout=10.0)
        except Exception as e:  # noqa: BLE001
            logger.debug("LSP shutdown error: %s", e)
        self._loop.stop()
        clear_cache()

    def get_status(self) -> Dict[str, Any]:
        """Return a snapshot of the service for ``hermes lsp status``."""
        with self._state_lock:
            clients = [
                {"server_id": c.server_id, "workspace_root": c.workspace_root,
                 "workspace_folders": list(c.workspace_folders), "state": c.state, "running": c.is_running}
                for c in self._clients.values()
            ]
            broken = list(self._broken)
        return {
            "enabled": self._enabled, "wait_mode": self._wait_mode, "wait_timeout": self._wait_timeout,
            "install_strategy": self._install_strategy, "clients": clients, "broken": broken,
            "disabled_servers": sorted(self._disabled_servers),
        }

    # ---- async internals ----

    async def _query_outcome_async(self, file_path: str, *, snapshot: bool = False,
                                   read_only_text: Optional[str] = None) -> DiagnosticOutcome:
        client = await self._get_or_spawn(file_path)
        if client is None:
            srv = find_server_for_file(file_path)
            key = self._broken_key(srv, file_path) if srv else None
            return DiagnosticOutcome("no_verdict", self._spawn_failures.get(key, "no_fresh_diagnostics"))
        connection = client._proc
        try:
            if read_only_text is None:
                version = await client.open_file(file_path, language_id=language_id_for(file_path))
            else:
                version = await client.open_file(file_path, language_id=language_id_for(file_path), text=read_only_text)
                if version == 0:
                    # A first unversioned push remains quarantined. Establish an
                    # edit generation for the existing bounded refresh mechanism.
                    version = await client.open_file(file_path, language_id=language_id_for(file_path), text=read_only_text)
            if not snapshot and read_only_text is None:
                await client.save_file(file_path)
            fresh = await client.wait_for_diagnostics(file_path, version, mode=self._wait_mode,
                timeout=None if snapshot else self._wait_timeout)
        except Exception:
            return DiagnosticOutcome("no_verdict", "server_error" if client.is_running else "disconnected")
        self._touch(client)
        if not fresh:
            return DiagnosticOutcome("no_verdict", "timeout" if client.is_running else "disconnected")
        # No await between capture and attribution: diagnostics and source share a generation.
        captured = client.diagnostic_snapshot(file_path)
        if captured is None or client._proc is not connection or captured["version"] < version:
            return DiagnosticOutcome("no_verdict", "no_fresh_diagnostics")
        diags = captured["diagnostics"]
        return DiagnosticOutcome("fresh", "diagnostics_present" if diags else "clean",
            diagnostics=diags, server_id=client.server_id,
            document_version=captured["version"], text=captured["text"])

    async def _get_or_spawn(self, file_path: str) -> Optional[LSPClient]:
        srv = find_server_for_file(file_path)
        if srv is None:
            return None
        if srv.server_id in self._disabled_servers:
            eventlog.log_disabled(srv.server_id, file_path, "disabled in config")
            return None
        ws_root, gated = resolve_workspace_for_file(file_path)
        if not (ws_root and gated):
            eventlog.log_no_project_root(srv.server_id, file_path)
            return None
        root = srv.resolve_root(file_path, ws_root)
        if root is None:
            eventlog.log_disabled(srv.server_id, file_path, "exclude marker hit (server gated off)")
            return None
        if (srv.server_id, root) in self._broken:
            return None
        key = _client_key(srv, root)
        with self._state_lock:
            client = self._clients.get(key)
            if client is not None and client.is_running:
                self._last_used[key] = time.time()
                eventlog.log_active(srv.server_id, root)
                return await self._attach_root(srv, client, root)
            spawning = self._spawning.get(key)
            owner = spawning is None
            if owner:
                spawning = self._spawning[key] = asyncio.get_running_loop().create_future()
        if not owner:
            try:
                client = await asyncio.shield(spawning)
            except Exception:  # noqa: BLE001
                return None
            return await self._attach_root(srv, client, root) if client is not None else None
        try:
            client = await self._spawn_client(srv, root)
            if client is None:
                self._broken.add((srv.server_id, root))
            else:
                with self._state_lock:
                    self._clients[key] = client
                    self._last_used[key] = time.time()
                eventlog.log_active(srv.server_id, root)
            if not spawning.done():
                spawning.set_result(client)
            return client
        except BaseException:
            if not spawning.done():
                spawning.cancel()
            raise
        finally:
            with self._state_lock:
                self._spawning.pop(key, None)

    @staticmethod
    async def _attach_root(srv: ServerDef, client: LSPClient, root: str) -> LSPClient:
        """Multi-root servers: announce ``root`` to the shared process instead of spawning another."""
        if srv.multi_root:
            await client.add_workspace_folder(root)
        return client

    async def _spawn_client(self, srv: ServerDef, root: str) -> Optional[LSPClient]:
        """Resolve the binary and start a client; ``None`` (after logging) when either fails."""
        ctx = ServerContext(
            workspace_root=root, install_strategy=self._install_strategy, binary_overrides=self._binary_overrides,
            env_overrides=self._env_overrides, init_overrides=self._init_overrides,
        )
        try:
            spec = srv.build_spawn(root, ctx)
        except ValueError as exc:
            eventlog.log_spawn_failed(srv.server_id, root, exc)
            self._spawn_failures[(srv.server_id, root)] = "server_unavailable"
            return None
        if spec is None:
            # Binary not locatable (auto-install off, manual-only, or install failed) — surface once.
            eventlog.log_server_unavailable(srv.server_id, srv.server_id)
            self._spawn_failures[(srv.server_id, root)] = "server_unavailable"
            return None
        client = LSPClient(
            server_id=srv.server_id, workspace_root=spec.workspace_root, command=spec.command, env=spec.env,
            cwd=spec.cwd, initialization_options=spec.initialization_options,
            seed_diagnostics_on_first_push=spec.seed_diagnostics_on_first_push or srv.seed_first_push,
        )
        self._starting[client] = asyncio.current_task()
        try:
            await client.start()
        except asyncio.CancelledError:
            # start() has not registered the client yet; retain ownership until
            # its process is reaped, including cancelled initialization.
            await client.shutdown()
            raise
        except Exception as e:  # noqa: BLE001
            eventlog.log_spawn_failed(srv.server_id, root, e)
            self._spawn_failures[(srv.server_id, root)] = "server_error"
            return None
        finally:
            self._starting.pop(client, None)
        return client

    def _touch(self, client: LSPClient) -> None:
        """Refresh last-used; guarded on membership so a client reaped mid-operation can't resurrect its entry."""
        with self._state_lock:
            for key, c in self._clients.items():
                if c is client:
                    self._last_used[key] = time.time()

    async def _start_idle_reaper(self) -> None:
        self._idle_reaper_task = asyncio.create_task(self._idle_reaper_loop())

    async def _idle_reaper_loop(self) -> None:
        interval = min(60.0, self._idle_timeout)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._reap_idle_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # A transient sweep error must not kill the reaper, or the accumulation leak it fixes comes back.
                logger.debug("LSP idle reaper sweep error: %s", e)

    async def _reap_idle_once(self) -> None:
        cutoff = time.time() - self._idle_timeout
        with self._state_lock:
            idle_keys = [key for key in self._clients if self._last_used.get(key, 0) < cutoff]
            clients = [self._clients.pop(key) for key in idle_keys]
            for key in idle_keys:
                self._last_used.pop(key, None)
        if clients:
            eventlog.log_reaped([(c.server_id, c.workspace_root) for c in clients], self._idle_timeout)
            await asyncio.gather(*(client.shutdown() for client in clients), return_exceptions=True)

    async def _shutdown_async(self) -> None:
        starting_tasks = set(self._starting.values())
        for task in starting_tasks:
            if not task.cancelling():
                task.cancel()
        if starting_tasks:
            await asyncio.gather(*starting_tasks, return_exceptions=True)
        if (reaper := self._idle_reaper_task) is not None:
            self._idle_reaper_task = None
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
        with self._state_lock:
            clients = list(self._clients.values())
            self._clients.clear()
            self._broken.clear()
            self._last_used.clear()
        await asyncio.gather(*(c.shutdown() for c in clients), return_exceptions=True)


__all__ = ["LSPService"]
