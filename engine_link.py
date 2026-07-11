"""Bridge from the standalone Signal app to the SHARED engine + database.

Signal lives in its own folder (D:\\Youtube\\Signal) so it can be worked on alone, but it
does NOT duplicate the heavy discovery/AI code or the 8.8 GB index. Instead it reuses:

  * the shared engine  -> the `scripter` package inside the sibling Lab repo
  * the shared database -> that repo's storage/youtube_index.sqlite3 (opened in place)

Import this FIRST in every Signal entry point (before importing anything from `scripter`).
When the engine is later moved to a neutral `engine/` folder, only this file changes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- where the shared engine + database live (override with env if you relocate them) ------
LAB_REPO = Path(os.getenv("FACELESS_ENGINE_DIR") or r"D:\Youtube\Scripter")
SHARED_DB = Path(os.getenv("YOUTUBE_INDEX_DB") or (LAB_REPO / "storage" / "youtube_index.sqlite3"))


def wire() -> None:
    """Put the shared engine on the import path and point the store at the shared DB."""
    if not LAB_REPO.exists():
        raise RuntimeError(
            f"Shared engine not found at {LAB_REPO}. Set FACELESS_ENGINE_DIR to the Lab repo.")
    engine_path = str(LAB_REPO)
    if engine_path not in sys.path:
        sys.path.insert(0, engine_path)          # so `import scripter...` resolves
    os.environ.setdefault("YOUTUBE_INDEX_DB", str(SHARED_DB))  # store reads this


# wire on import so `import engine_link` is enough
wire()
