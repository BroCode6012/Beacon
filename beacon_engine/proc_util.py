"""Run child processes without flashing a console window on Windows.

The desktop app is started by ``pythonw.exe`` (see tools/install_shortcut.ps1), which
has no console of its own. Every plain ``subprocess.run`` of a console tool -- ffmpeg,
ffprobe, ssh -- therefore makes Windows open a brand new terminal for the child and
tear it down a moment later. On Windows 11 that terminal is Windows Terminal, so what
the user sees is a full black window appearing over the app and stealing focus.

CREATE_NO_WINDOW gives the child a console it never shows. ``run`` is a drop-in for
``subprocess.run``; off Windows it is a straight pass-through.
"""

from __future__ import annotations

import subprocess
import sys

# Only defined on Windows builds of the stdlib, so keep the literal as a fallback.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def no_window_flags(extra: int = 0) -> int:
    """creationflags that keep a child process off screen (0 off Windows)."""
    return (extra | _CREATE_NO_WINDOW) if sys.platform == "win32" else extra


def run(cmd, **kwargs):
    """``subprocess.run``, but the child never pops a console window."""
    if sys.platform == "win32":
        kwargs["creationflags"] = no_window_flags(kwargs.get("creationflags", 0))
    return subprocess.run(cmd, **kwargs)
