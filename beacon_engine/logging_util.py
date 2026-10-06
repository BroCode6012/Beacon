"""Lightweight logging: errors/warnings go to a rotating file AND an in-memory buffer
the UI can show. Keeps a visible trail of relay failures, fallbacks, generation errors,
and any background-task exceptions — so problems aren't silently swallowed.
"""

from __future__ import annotations

import collections
import logging
from logging.handlers import RotatingFileHandler

from beacon_engine.config import settings

LOG_FILE = settings.log_file   # profile-aware (e.g. beacon_engine_work1.log); default beacon_engine.log
_BUFFER: collections.deque = collections.deque(maxlen=400)   # (level, formatted_message)


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _BUFFER.append((record.levelname, self.format(record)))
        except Exception:  # noqa: BLE001 — logging must never raise
            pass


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("beacon_engine")
    if logger.handlers:  # already configured
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")
    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:  # noqa: BLE001 — file logging is best-effort
        pass
    bh = _BufferHandler()
    bh.setFormatter(fmt)
    logger.addHandler(bh)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("beacon_engine")


def recent_logs(levels: set[str] | None = None) -> list[tuple[str, str]]:
    """Most-recent-last log lines, optionally filtered to certain levels."""
    if levels:
        return [(lvl, msg) for lvl, msg in _BUFFER if lvl in levels]
    return list(_BUFFER)


def counts() -> dict[str, int]:
    c = {"ERROR": 0, "WARNING": 0, "INFO": 0}
    for lvl, _ in _BUFFER:
        c[lvl] = c.get(lvl, 0) + 1
    return c


def clear_logs() -> None:
    _BUFFER.clear()


setup_logging()
