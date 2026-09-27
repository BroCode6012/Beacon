"""Bridge from the standalone Beacon app to the SHARED engine + database.

Beacon lives in its own folder (D:\\Youtube\\Beacon) so it can be worked on alone, but it
does NOT duplicate the heavy discovery/AI code. Instead it reuses:

  * the shared engine  -> the `scripter` package inside the sibling Lab repo
  * the shared database -> Beacon's storage/youtube_index.sqlite3 (opened in place)

Import this FIRST in every Beacon entry point (before importing anything from `scripter`).
When the engine is later moved to a neutral `engine/` folder, only this file changes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- where the shared engine + database live (override with env if you relocate them) ------
SIGNAL_ROOT = Path(__file__).resolve().parent
LAB_REPO = Path(os.getenv("FACELESS_ENGINE_DIR") or r"D:\Youtube\Quill")
SIGNAL_DB = SIGNAL_ROOT / "storage" / "youtube_index.sqlite3"


def resolve_shared_db() -> Path:
    """Return Beacon's shared index, unless an explicit override is configured."""
    configured = os.getenv("SIGNAL_INDEX_DB") or os.getenv("YOUTUBE_INDEX_DB")
    if configured:
        return Path(configured)
    return SIGNAL_DB


SHARED_DB = resolve_shared_db()


def _load_engine_env() -> None:
    """Load the ENGINE's .env, which Beacon's process would otherwise never see.

    `scripter.config` calls `load_dotenv()` with no path, and that searches upward from
    the CURRENT WORKING DIRECTORY. Started from D:\\Youtube\\Beacon it therefore never
    finds D:\\Youtube\\Quill\\.env, so engine services that need credentials — the
    YouTube key pool above all — come up empty inside Beacon while working perfectly in
    the radar worker (which runs with the Lab as its cwd). That asymmetry is invisible
    until something in Beacon tries to call YouTube directly.

    override=False on purpose: values already in the environment WIN. The DB path is set
    just above, and a stale copy in the engine's .env must never quietly repoint Beacon
    at a different database.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:          # dotenv is optional for bare DB-only entry points
        return
    for candidate in (LAB_REPO / "storage" / ".env", LAB_REPO / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)


def wire() -> None:
    """Put the shared engine on the import path and point the store at the shared DB."""
    if not LAB_REPO.exists():
        raise RuntimeError(
            f"Shared engine not found at {LAB_REPO}. Set FACELESS_ENGINE_DIR to the Lab repo.")
    engine_path = str(LAB_REPO)
    if engine_path not in sys.path:
        sys.path.insert(0, engine_path)          # so `import scripter...` resolves
    os.environ.setdefault("SIGNAL_INDEX_DB", str(SHARED_DB))
    os.environ.setdefault("YOUTUBE_INDEX_DB", str(SHARED_DB))  # store reads this
    _load_engine_env()                           # after the DB vars, so they always win


# wire on import so `import engine_link` is enough
wire()
