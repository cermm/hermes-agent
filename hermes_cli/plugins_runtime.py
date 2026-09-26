"""Read-only code identity of already-registered, profile-scoped plugin hooks."""
from __future__ import annotations

from itertools import islice
import sys
import types
from typing import Callable

MAX_SNAPSHOT_HOOKS = 64
MAX_SNAPSHOT_CALLBACKS = 256


def _unavailable(reason: str) -> dict:
    return {"schema": 2, "status": "unavailable", "reason": reason,
            "manager_scope": None, "hooks": {}}


def _callback_identity(callback, fingerprint: Callable) -> dict:
    target = callback.__func__ if type(callback) is types.MethodType else callback
    if type(target) is not types.FunctionType:
        return {"supported": False, "reason": "unsupported_callable"}
    module = sys.modules.get(target.__module__)
    if type(module) is not types.ModuleType or target.__globals__ is not module.__dict__:
        return {"supported": False, "reason": "module_identity"}
    # Fingerprint the dispatch-list entry, never a fresh import or module attribute.
    return dict(fingerprint(callback), registered_module_identity=True)


def _capture_registry(registry: dict) -> dict | None:
    """Bound the entries actually copied, including concurrent append/registration."""
    callbacks = {}
    total = 0
    for index, (name, registered) in enumerate(registry.items()):
        if index >= MAX_SNAPSHOT_HOOKS:
            return None
        remaining = MAX_SNAPSHOT_CALLBACKS - total
        captured = tuple(islice(registered, remaining + 1))
        if len(captured) > remaining:
            return None
        if captured:
            callbacks[name] = captured
            total += len(captured)
    return callbacks


def snapshot_registered_hooks(fingerprint: Callable) -> dict:
    """Report a bounded snapshot without discovering plugins, creating a manager or invoking hooks.

    This witnesses dispatch registrations, not plugin ownership. The consumer selects the hook
    names and expected code identities appropriate to its installation. Unrelated opaque callable
    objects remain explicit unsupported records rather than preventing a useful complete snapshot.
    """
    plugins = sys.modules.get("hermes_cli.plugins")
    if plugins is None:
        return _unavailable("manager_module_missing")
    cache_lock = plugins._plugin_managers_lock
    if not cache_lock.acquire(blocking=False):
        return _unavailable("manager_cache_busy")
    try:
        home_key = plugins._plugin_home_key()
        manager = plugins._plugin_managers_by_home.get(home_key)
        if manager is None:
            return _unavailable("manager_missing")
        if manager.scope_key != str(home_key):
            return _unavailable("manager_scope_mismatch")
        if not manager._discovery_lock.acquire(blocking=False):
            return _unavailable("registration_busy")
        try:
            registry = manager._hooks
            try:
                callbacks = _capture_registry(registry)
            except RuntimeError:
                # Public registrations can mutate the dict outside the discovery lock.
                return _unavailable("registration_changed")
            if callbacks is None:
                return _unavailable("snapshot_limit")
            hooks = {name: [_callback_identity(callback, fingerprint) for callback in registered]
                     for name, registered in callbacks.items()}
            try:
                current = _capture_registry(registry)
            except RuntimeError:
                return _unavailable("registration_changed")
            if current is None:
                return _unavailable("snapshot_limit")
            if (manager._hooks is not registry or current.keys() != callbacks.keys()
                    or any(len(current[name]) != len(registered)
                           or any(left is not right for left, right in zip(current[name], registered))
                           for name, registered in callbacks.items())):
                return _unavailable("registration_changed")
            return {"schema": 2, "status": "ready", "reason": None,
                    "manager_scope": manager.scope_key, "hooks": hooks}
        finally:
            manager._discovery_lock.release()
    finally:
        cache_lock.release()
