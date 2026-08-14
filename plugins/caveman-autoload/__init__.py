from __future__ import annotations

"""Worker-scoped Caveman output policy.

Bundled Hermes plugin. It injects a compact output directive only for
coding/implementation worker contexts. It fails closed for business, final
artifact, protected-action, approval, rollback, and safety prompts.
"""

import os
import re
from typing import Any

WORKER_PROFILES = {
    "planner",
    "builder",
    "builder-low",
    "builder-medium",
    "builder-high",
    "builder-mini",
    "reviewer",
    "reviewersol",
    "assurance",
    "researcher",
    "security",
    "semantic-reviewer",
}

CODING_TERMS = re.compile(
    r"\b(code|coding|implement|implementation|debug|test|tests|pytest|review|PR|pull request|"
    r"issue|kanban|worker|builder|reviewer|repair|fix|commit|diff|branch|merge|CI|lint|"
    r"typecheck|type-check|unit test|integration test|regression|stack trace|traceback)\b",
    re.I,
)

HARD_EXCLUSIONS = re.compile(
    r"\b(email|e-mail|linkedin|wiki final|final wiki|final report|presentation|homologation|"
    r"customer|business report|strategy|technical marketing|W&C|wire and cable|approval|approve|"
    r"authorize|authorization|permission|protected action|rollback|restore|irreversible|delete|"
    r"payment|secret|api key|password|legal|commercial|contract|normal mode|disable caveman|"
    r"full explanation|explain fully|polished|customer-facing|external-facing)\b",
    re.I,
)

DIRECTIVE = """Worker output compact. Preserve all technical substance. No filler.
Keep code, commands, paths, URLs, errors, SHAs, numbers, units exact.
Never drop not/never/no/only/except. Do not narrate tool calls.
Report: changed / proof / blocker / next.
Safety, approval, rollback, protected-action warnings stay explicit.
Persisted artifacts/docs/emails/reports stay normal prose unless user asks compression."""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _profile_name(context_profile: str | None = None) -> str:
    """Return the active Hermes profile name without trusting one env var."""
    if context_profile:
        return context_profile
    home = os.environ.get("HERMES_HOME") or ""
    normalized = home.rstrip("/")
    if "/profiles/" in normalized:
        return normalized.rsplit("/", 1)[-1]
    if normalized.endswith("/.hermes"):
        return "default"
    for key in ("HERMES_PROFILE", "HERMES_PROFILE_NAME"):
        value = os.environ.get(key)
        if value:
            return value
    return ""


def _is_dispatcher_worker() -> bool:
    """Trust Kanban worker env only when Hermes dispatcher context says so."""
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return False
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        return bool(is_dispatcher_owned_worker_context())
    except Exception:
        return False


def _is_delegated_child() -> bool:
    if _truthy(os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT")):
        return True
    try:
        from agent.delegation_context import is_delegated_child_context

        return bool(is_delegated_child_context())
    except Exception:
        return False


def _text_from_message(user_message: Any) -> str:
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts: list[str] = []
        for item in user_message:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _should_apply(
    user_message: Any = "",
    *,
    context_profile: str | None = None,
    **_: Any,
) -> bool:
    text = _text_from_message(user_message)

    if _truthy(os.environ.get("CAVEMAN_AUTOLOAD_OFF")):
        return False
    if HARD_EXCLUSIONS.search(text):
        return False

    coding_intent = bool(CODING_TERMS.search(text))
    if _truthy(os.environ.get("CAVEMAN_AUTOLOAD_FORCE")):
        return True
    if _is_dispatcher_worker() and coding_intent:
        return True
    if _is_delegated_child() and coding_intent:
        return True
    if _profile_name(context_profile) in WORKER_PROFILES and coding_intent:
        return True
    return False


def register(ctx: Any) -> None:
    profile_name = getattr(ctx, "profile_name", "")

    def on_pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
        try:
            if _should_apply(**kwargs, context_profile=profile_name):
                return {"context": DIRECTIVE}
        except Exception:
            return None
        return None

    ctx.register_hook("pre_llm_call", on_pre_llm_call)
