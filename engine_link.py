"""Bridge from the standalone Signal app to the SHARED engine + database.

Signal lives in its own folder (D:\\Youtube\\Signal) so it can be worked on alone, but it
does NOT duplicate the heavy discovery/AI code. Instead it reuses:

  * the shared engine  -> the `scripter` package inside the sibling Lab repo
  * the shared database -> Signal's storage/youtube_index.sqlite3 (opened in place)

Import this FIRST in every Signal entry point (before importing anything from `scripter`).
When the engine is later moved to a neutral `engine/` folder, only this file changes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- where the shared engine + database live (override with env if you relocate them) ------
SIGNAL_ROOT = Path(__file__).resolve().parent
LAB_REPO = Path(os.getenv("FACELESS_ENGINE_DIR") or r"D:\Youtube\Scripter")
SIGNAL_DB = SIGNAL_ROOT / "storage" / "youtube_index.sqlite3"


def resolve_shared_db() -> Path:
    """Return Signal's shared index, unless an explicit override is configured."""
    configured = os.getenv("SIGNAL_INDEX_DB") or os.getenv("YOUTUBE_INDEX_DB")
    if configured:
        return Path(configured)
    return SIGNAL_DB


SHARED_DB = resolve_shared_db()


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


# wire on import so `import engine_link` is enough
wire()
