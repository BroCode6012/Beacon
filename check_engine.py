"""Headless proof that the standalone Beacon app can reach the SHARED engine + database.

Run from the Lab's venv so the dependencies are available:
    D:\\Youtube\\Quill\\.venv\\Scripts\\python.exe D:\\Youtube\\Beacon\\check_engine.py
"""

from __future__ import annotations

import engine_link  # noqa: F401 — wires the shared engine onto sys.path + sets the DB path

from scripter.research.store import YouTubeStore, DEFAULT_DB_PATH


def main() -> None:
    print("Shared DB path  :", DEFAULT_DB_PATH)
    store = YouTubeStore()
    with store.connect() as conn:
        ch = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        vid = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    print(f"Channels in index: {ch:,}")
    print(f"Videos in index  : {vid:,}")
    print("OK — Beacon (separate folder) reads the shared engine + database in place.")


if __name__ == "__main__":
    main()
