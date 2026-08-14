"""``--in`` accepts Git Bash / MSYS-style paths on Windows.

Under Git Bash, ``hermes chat --in ~`` reaches the native CLI as
``/c/Users/<user>`` — the shell expands ``~`` to an MSYS POSIX path and
MSYS2's automatic argument conversion is disabled for native executables.
The resolver must translate the MSYS/Cygwin/WSL drive-root spellings to
native form before the isdir check, or every Git Bash invocation dies with
"--in directory not found" (hit live by the Bot Mode agent-messaging flow,
whose SOUL protocol tells agents to deliver with ``--in ~``).

The translation itself (``_msys_to_windows_path``) has exhaustive unit
coverage in tests/tools/test_local_env_windows_msys.py; this pins the
``--in`` call site actually applying it.
"""

import os
from unittest import mock

from tools.environments.local import _msys_to_windows_path


def _resolve_in_dir(raw: str) -> str:
    """The exact resolution expression used by hermes_cli.main for --in."""
    return os.path.abspath(os.path.expanduser(_msys_to_windows_path(raw)))


class TestInDirMsysResolution:
    def test_git_bash_tilde_expansion_resolves_on_windows(self):
        with (
            mock.patch("tools.environments.local._IS_WINDOWS", True),
            mock.patch.dict(os.environ, {"USERPROFILE": r"C:\Users\alice"}),
        ):
            resolved = _resolve_in_dir("/c/Users/alice")
            assert resolved.lower().startswith("c:"), resolved
            assert "/c/" not in resolved

    def test_native_and_posix_forms_untouched(self):
        with mock.patch("tools.environments.local._IS_WINDOWS", True):
            assert _msys_to_windows_path("C:/Users/alice") == "C:/Users/alice"
            assert _msys_to_windows_path("~/projects") == "~/projects"

    def test_non_windows_never_translates(self):
        with mock.patch("tools.environments.local._IS_WINDOWS", False):
            assert _msys_to_windows_path("/c/Users/alice") == "/c/Users/alice"

    def test_main_call_site_uses_translation(self):
        """Guard: the --in resolution in hermes_cli.main must route through
        _msys_to_windows_path (a plain expanduser/abspath does not survive
        Git Bash). Source-level check keeps this honest without spawning
        the full CLI."""
        import inspect

        import hermes_cli.main as main_mod

        src = inspect.getsource(main_mod)
        idx = src.find('in_dir = getattr(args, "in_dir", None)')
        assert idx != -1, "--in resolution block moved; update this test"
        block = src[idx : idx + 800]
        assert "_msys_to_windows_path" in block, (
            "--in no longer translates MSYS paths; Git Bash `--in ~` will "
            "fail with '--in directory not found: /c/Users/...'"
        )
