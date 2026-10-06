"""Initialize Beacon's own engine and index; never load another app's code."""
from __future__ import annotations
import os
from pathlib import Path
import beacon_host

beacon_host.load()
SIGNAL_ROOT = Path(__file__).resolve().parent
ENGINE_ROOT = SIGNAL_ROOT
SIGNAL_DB = SIGNAL_ROOT / "storage" / "youtube_index.sqlite3"

def resolve_shared_db() -> Path:
    configured = os.getenv("BEACON_INDEX_DB") or os.getenv("SIGNAL_INDEX_DB") or os.getenv("YOUTUBE_INDEX_DB")
    return Path(configured) if configured else SIGNAL_DB

SHARED_DB = resolve_shared_db()

def wire() -> None:
    if not (ENGINE_ROOT / "beacon_engine" / "__init__.py").is_file():
        raise RuntimeError("Beacon's engine is missing; use a complete Beacon checkout.")
    os.environ.setdefault("SIGNAL_INDEX_DB", str(SHARED_DB))
    os.environ.setdefault("YOUTUBE_INDEX_DB", str(SHARED_DB))
    from dotenv import load_dotenv
    load_dotenv(Path(os.getenv("BEACON_ENV_FILE") or SIGNAL_ROOT / ".env"), override=False)

wire()
