"""The standalone FINDER service — the shared niche-finder tool, run as its own process.

It owns the discovery engine + the shared 8.8GB index, and exposes ~35 commands over
localhost HTTP with TWO keys:
  * FULL key  (FINDER_TOKEN)      -> read + write  — used by the Signal control room
  * READ key  (FINDER_READ_TOKEN) -> read only     — used by the Production Lab

Start it once and leave it running; both apps connect to it (see FinderClient). Keys are
written to a shared token file so the apps can pick them up automatically.

Run (from the Lab's venv, which has the deps):
    D:\\Youtube\\Scripter\\.venv\\Scripts\\python.exe D:\\Youtube\\Signal\\finder_service.py
"""

from __future__ import annotations

import os

import engine_link  # noqa: F401 — wires the shared engine onto sys.path + sets YOUTUBE_INDEX_DB

from scripter.intelligence.local_api import create_server

HOST = os.getenv("FINDER_HOST", "127.0.0.1")
PORT = int(os.getenv("FINDER_PORT", "8765"))
DB = os.environ["YOUTUBE_INDEX_DB"]                       # set by engine_link
FULL_TOKEN = os.getenv("FINDER_TOKEN", "signal-full")     # Signal uses this
READ_TOKEN = os.getenv("FINDER_READ_TOKEN", "lab-read")   # the Lab uses this
# shared token file both apps read so they don't have to be told the keys
TOKEN_FILE = os.getenv("FINDER_TOKEN_FILE") or os.path.join(
    os.path.dirname(DB), "finder.tokens.json")


def _publish_tokens() -> None:
    import json
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
            json.dump({"url": f"http://{HOST}:{PORT}",
                       "full": FULL_TOKEN, "read": READ_TOKEN}, fh)
    except Exception:  # noqa: BLE001 — best effort; apps can also read env
        pass


def main() -> None:
    _publish_tokens()
    server = create_server(host=HOST, port=PORT, db_path=DB,
                           token=FULL_TOKEN, read_token=READ_TOKEN)
    print(f"FINDER service up on http://{HOST}:{PORT}")
    print(f"  DB     : {DB}")
    print(f"  keys   : full=<{len(FULL_TOKEN)} chars>  read=<{len(READ_TOKEN)} chars>")
    print(f"  tokens : {TOKEN_FILE}")
    server.serve_forever()


if __name__ == "__main__":
    main()
