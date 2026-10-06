"""Client for the standalone Finder service (the shared niche-finder tool).

The Finder runs as its OWN process (beacon_engine.intelligence.local_api) exposing ~35 commands
over localhost HTTP, with a read-only vs full-access switch. Both apps talk to it through
this one client — the Lab with a read-only key, Signal with a full key:

    finder = FinderClient()                 # reads FINDER_URL / FINDER_TOKEN from env
    finder.health()                         # {'ok': True, 'writes_enabled': ...}
    finder.db_stats()                       # -> {'channels': ..., 'videos': ...}
    finder.call('get_niche_outliers', niche_id=3, limit=20)

It never imports the discovery engine directly — that's the point: the finder is an external
tool, and this is the only doorway to it. Raises FinderError on transport/permission failures
so callers can fall back gracefully.
"""

from __future__ import annotations

import os

import requests

DEFAULT_URL = os.getenv("FINDER_URL", "http://127.0.0.1:8765")


class FinderError(RuntimeError):
    """The Finder service was unreachable, unauthorized, or refused the call."""


class FinderClient:
    def __init__(self, base_url: str = "", token: str = "", *, timeout: int = 600) -> None:
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self.token = token or os.getenv("FINDER_TOKEN", "")
        self.timeout = timeout

    # -- low level ----------------------------------------------------------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def health(self) -> dict:
        try:
            r = requests.get(f"{self.base_url}/health", headers=self._headers(), timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as ex:  # noqa: BLE001
            raise FinderError(f"Finder service not reachable at {self.base_url}: {ex}") from ex

    def is_up(self) -> bool:
        try:
            return bool(self.health().get("ok"))
        except FinderError:
            return False

    def list_tools(self) -> list[dict]:
        r = requests.get(f"{self.base_url}/tools", headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("tools", [])

    def call(self, tool: str, **args):
        """Run a Finder command by name. Returns its structured result (dict/list)."""
        try:
            r = requests.post(f"{self.base_url}/tools/{tool}", json=args,
                              headers=self._headers(), timeout=self.timeout)
        except Exception as ex:  # noqa: BLE001
            raise FinderError(f"Finder call '{tool}' failed to reach {self.base_url}: {ex}") from ex
        if r.status_code == 403:
            raise FinderError(f"Finder refused '{tool}': this client is read-only "
                              "(that command needs the full-access key).")
        if r.status_code == 401:
            raise FinderError("Finder rejected the access key (401).")
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("error", "")
            except Exception:  # noqa: BLE001
                detail = r.text[:200]
            raise FinderError(f"Finder call '{tool}' failed ({r.status_code}): {detail}")
        data = r.json()
        # tool results come back as {content:[...], structuredContent: <result>}
        return data.get("structuredContent", data)

    # -- convenience --------------------------------------------------------
    def db_stats(self) -> dict:
        return self.call("db_stats")
