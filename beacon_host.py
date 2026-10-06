"""Where Beacon lives is a SETTING — like which AI answers.

The operator's rule (2026-09-29): "let's test Oracle — when we find better options we move,
so make the app flexible." A laptop, the Oracle machine, or any other Linux box is described
by ONE file, `beacon_host.env`, next to this module (or wherever BEACON_HOST_FILE points):
folders, ports, time zone, keys. No code names a particular machine.

Values already in the environment WIN — a systemd unit, a test, a one-off override — so the
file only fills the gaps. Without the file every default is the laptop's original layout, and
the desktop app behaves exactly as it always has.

Standard library only: this runs before anything else is importable.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_FILE = Path(os.getenv("BEACON_HOST_FILE") or (HERE / "beacon_host.env"))

# The engine is part of this checkout, on every host.


def parse(text: str) -> dict[str, str]:
    """KEY=value lines; blank lines and # comments ignored; surrounding quotes removed."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if " #" in value and not value.startswith(("'", '"')):
            value = value.split(" #", 1)[0].rstrip()     # trailing comment
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load(path: Path | None = None) -> dict[str, str]:
    """Apply this machine's settings to the environment; returns what was applied.

    Never overrides a variable that is already set. Applying TZ re-reads the time zone, so
    a server in UTC shows the operator's own clock (seen in the Oracle test: 11:30 UTC for a
    Casablanca operator).
    """
    path = path or HOST_FILE
    try:
        values = parse(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    applied = {k: v for k, v in values.items() if k not in os.environ}
    os.environ.update(applied)
    if "TZ" in applied and hasattr(time, "tzset"):
        time.tzset()                                     # POSIX only; Windows has none
    return applied


def default_engine_dir(root: Path = HERE) -> Path:
    """Beacon includes its own engine; sibling repositories are never consulted."""
    return Path(root)
