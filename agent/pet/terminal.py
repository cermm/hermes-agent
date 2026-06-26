from __future__ import annotations
import os
from dataclasses import dataclass, field


RENDER_MODES = ("auto", "kitty", "iterm", "sixel", "unicode", "off")

@dataclass(frozen=True)
class TerminalProfile:
    """Declarative description of a terminal's detection fingerprint."""
    mode: str  # one of RENDER_MODES
    # Any of these env vars being set (non-empty) is a match
    env_vars: tuple[str, ...] = ()
    # $TERM_PROGRAM must equal one of these (case-insensitive)
    term_program_vals: tuple[str, ...] = ()
    # Any of these strings appearing in $TERM is a match
    term_substrings: tuple[str, ...] = ()

    def matches(self) -> bool:
        if any(os.environ.get(v) for v in self.env_vars):
            return True
        tp = os.environ.get("TERM_PROGRAM", "").lower()
        if tp and tp in self.term_program_vals:
            return True
        term = os.environ.get("TERM", "").lower()
        if any(s in term for s in self.term_substrings):
            return True
        return False


# ── Registry (priority order — first match wins) ───────────────────────────
#
# VS Code / Cursor override comes first: their embedded xterm.js leaks
# KITTY_WINDOW_ID / ITERM_SESSION_ID from the parent shell, so we must
# short-circuit before any of the real-graphics entries.
#
_TERMINAL_PROFILES: tuple[TerminalProfile, ...] = (
    TerminalProfile(
        mode="unicode",
        term_program_vals=("vscode",),
    ),

    TerminalProfile(
        mode="kitty",
        env_vars=("KITTY_WINDOW_ID","GHOSTTY_RESOURCES_DIR","WEZTERM_PANE",),
        term_program_vals=("kitty", "ghostty",),
        term_substrings=("kitty","ghostty","wezterm", ),
    ),
    TerminalProfile(
        mode="iterm",
        env_vars=("ITERM_SESSION_ID",),
        term_program_vals=("iterm.app",),
    ),
    TerminalProfile(
        mode="sixel",
        env_vars=("WT_SESSION",),
        term_substrings=("mlterm","contour","sixel",),
        term_program_vals=("mintty","foot","contour",),
    ),
)


def detect_terminal_graphics() -> str:
    """Return the richest graphics mode available in the current terminal.

    Non-blocking — no DA1/terminal query is issued, as those can break pipes
    in strange ways.
    Walks ``_TERMINAL_PROFILES` in priority order; returns the first match's mode, or ``"unicode"`` as the
    safe universal fallback.
    """
    for profile in _TERMINAL_PROFILES:
        if profile.matches():
            return profile.mode
    return "unicode"