"""The one door an outside agent (Muse) may knock on — read-only unless the operator says so.

Beacon's finder already speaks HTTP, so the lazy move would be to point a tunnel straight
at it. Two reasons not to:

  * its keys fall back to strings written in the source (`signal-full` / `lab-read`), so a
    public URL would be a lock whose key is in the repo, and
  * the full key can bulk-exclude channels, edit the scraper's settings and stop it.

This gateway is what the tunnel points at instead:

  * its own long random key, made once and kept in storage/ (gitignored, never printed);
  * an allowlist of READ tools — a write tool is refused here, before the finder is asked;
  * it reaches the finder over 127.0.0.1 with the finder's READ key, so even a mistake in
    this file cannot write: the finder answers 403 (verified 2026-09-28);
  * every call is written to a log, so you can see exactly what the outside agent did;
  * it listens on localhost only. Nothing is public until you run a tunnel, and closing
    that tunnel closes the door. Beacon itself is untouched either way.

AGENT ACCESS (operator's choice, 2026-09-29: "the agent is Muse"): with
BEACON_GATEWAY_ACCESS=agent in the machine's settings, the door also offers everything Beacon's
own agent can do — run through the agent's own dispatcher (muse_powers.py), with every change
recorded with how to undo it. Without that setting it stays read-only.

It answers both shapes, so it fits whatever the outside agent speaks:

    GET  /health            is it alive, and what may it do
    GET  /tools             the tools it will accept, with their inputs
    GET  /guide             how an agent should use them (the rules Beacon's own agent follows)
    POST /tools/<name>      run one, body = the arguments
    POST /mcp               the same tools over MCP (initialize / tools/list / tools/call)

Run it:  python beacon_gateway.py        (Beacon or finder_service must be running)
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import beacon_host  # noqa: E402  — this machine's ports/paths before the constants below

beacon_host.load()
import index_stats  # noqa: E402  — the one shared count of the library (also used by the screens)
STORAGE = HERE / "storage"
TOKEN_FILE = STORAGE / "muse_gateway.token.json"   # *token*.json is gitignored
LOG_FILE = STORAGE / "muse_gateway.log"
FINDER_TOKENS = Path(os.getenv("FINDER_TOKEN_FILE") or (STORAGE / "finder.tokens.json"))

HOST = "127.0.0.1"                                  # never 0.0.0.0: the tunnel dials in
PORT = int(os.getenv("BEACON_GATEWAY_PORT", "8770"))
# Cloudflare's quick tunnel drops any answer slower than ~100s. Waiting longer here only
# leaves the caller hanging, so give up first and say why (measured 2026-09-28: db_stats
# ran 300s, and Muse got nothing back — twice).
CALL_TIMEOUT = int(os.getenv("BEACON_GATEWAY_TIMEOUT", "90"))

# db_stats counts every row of a 32 GB index — minutes, so it can never make it through
# the tunnel live. The index size changes by a few thousand a day, so a count taken in
# the background and served instantly is the honest answer, stamped with its age.
STATS_REFRESH_SECONDS = int(os.getenv("BEACON_GATEWAY_STATS_REFRESH", str(6 * 3600)))
STATS_TIMEOUT = 1800
CACHED_TOOLS = {"db_stats"}

CARETAKER_AGENT = "beacon-muse-link"                # muse_link.USER_AGENT

# "read" (default): look only. "agent": also everything Beacon's own agent may do.
ACCESS = os.getenv("BEACON_GATEWAY_ACCESS", "read").strip().lower()
CHANGES_FILE = STORAGE / "muse_changes.jsonl"
READ_GUIDE = ("Read-only connection to Beacon, a finder of faceless, replicable YouTube channel "
              "opportunities. For 'how many' use count_channels; report real numbers and names.")

# A runaway loop on the other side must not hammer a 32 GB index all night.
MAX_CALLS_PER_MINUTE = int(os.getenv("BEACON_GATEWAY_CALLS_PER_MIN", "60"))

# Belt and braces. The finder's READ key already refuses these, but naming them here means
# a write is refused on this side too — before it ever reaches the database.
WRITE_TOOLS = {
    "exclude_channels", "unexclude_channels", "exclude_by_filter", "run_junk_filter",
    "set_channel_topic", "reclassify_topics", "create_niche", "add_channel_to_niche",
    "seed_channels", "track_competitor", "sync_competitors", "crawl_niche",
    "start_scraper", "stop_scraper", "set_scraper_config", "add_watchlist_queries",
    "remove_watchlist_queries", "import_edge_bundles", "refresh_saved",
}


def log(line: str) -> None:
    """Every call is recorded: an outside agent's work should be reviewable afterwards."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        STORAGE.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except OSError:
        pass
    print(f"[{stamp}] {line}", flush=True)


def gateway_token() -> str:
    """This door's own key: made once, reused after that, never printed to the screen."""
    env = os.getenv("BEACON_GATEWAY_TOKEN", "").strip()
    if env:
        return env
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))["token"]
    except (OSError, ValueError, KeyError):
        token = secrets.token_urlsafe(32)
        STORAGE.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps({"token": token}, indent=2), encoding="utf-8")
        return token


def finder() -> tuple[str, str]:
    """(url, READ key) for the running finder — the read key, never the full one."""
    data = json.loads(FINDER_TOKENS.read_text(encoding="utf-8"))
    return data["url"].rstrip("/"), data["read"]


def build_powers():
    """Agent access: Beacon's own agent (headless — no window to drive) with the FULL key."""
    import engine_link  # noqa: F401 — engine path + library location before scripter imports
    import beacon_agent
    import sweep_control
    from muse_powers import MusePowers
    from scripter.intelligence.finder_client import FinderClient
    data = json.loads(FINDER_TOKENS.read_text(encoding="utf-8"))
    agent = beacon_agent.BeaconAgent(FinderClient(data["url"], data["full"]),
                                     sweep_control, ui=None)
    return MusePowers.for_beacon(agent, db_path=os.environ["YOUTUBE_INDEX_DB"],
                                 changes_path=CHANGES_FILE,
                                 read_config=sweep_control._read_watchlist_config,
                                 reviewer=os.getenv("BEACON_OUTSIDE_AGENT", "").strip().lower()
                                 or "outside-agent")


def _as_result(ok: bool, out) -> dict:
    """One shape for every answer: the one the finder itself sends back."""
    body = out if isinstance(out, dict) else {"result": out}
    reply = {"structuredContent": body,
             "content": [{"type": "text", "text": json.dumps(body, default=str)[:20000]}]}
    if not ok:
        reply["isError"] = True
        if "error" in body:                     # plain REST callers look at the top level
            reply["error"] = body["error"]
    return reply


def _post(url: str, token: str, body: dict | None, method: str = "POST",
          timeout: int | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout or CALL_TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:                      # the finder's own refusal
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:                                      # noqa: BLE001
            return exc.code, {"error": f"HTTP {exc.code}"}
    except TimeoutError:
        return 504, {"error": f"Beacon took longer than {timeout or CALL_TIMEOUT}s to answer. "
                              "Try a narrower question (a topic, a subscriber range)."}
    except Exception as exc:                                   # noqa: BLE001
        if "timed out" in str(exc):
            return 504, {"error": f"Beacon took longer than {timeout or CALL_TIMEOUT}s to "
                                  "answer. Try a narrower question."}
        return 502, {"error": f"{type(exc).__name__}: {exc}"}


class StatsCache:
    """The index size, counted in the background so asking for it is instant.

    The count itself lives in storage/index_stats.json (index_stats.py), shared with Beacon's
    screens: whoever counted last, everyone answers from it, and only one count runs at a time.
    So a restarted door answers at once instead of re-counting for six minutes.
    """

    def __init__(self) -> None:
        self.body: dict | None = None
        self.counted_at: float = 0.0
        self.error = ""
        self.counting = False

    def _adopt_shared(self) -> None:
        """Take the shared count when it is newer than the one held here."""
        snapshot = index_stats.read()
        if snapshot is not None and float(snapshot["counted_at"]) > self.counted_at:
            self.body = {"structuredContent": snapshot["stats"]}
            self.counted_at = float(snapshot["counted_at"])

    def _count(self) -> dict:
        url, token = finder()
        status, body = _post(f"{url}/tools/db_stats", token, {}, timeout=STATS_TIMEOUT)
        if status != 200:
            raise RuntimeError(str(body.get("error") or f"HTTP {status}"))
        return body.get("structuredContent") or {}

    def refresh(self) -> None:
        self.counting = True
        started = time.time()
        try:
            fresh = index_stats.count_now(self._count)
            if fresh is None:
                log("db_stats: another part of Beacon is counting — using its result")
            else:
                self.error = ""
                log(f"db_stats counted in the background in {time.time() - started:.0f}s")
        except Exception as exc:                               # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            log(f"db_stats background count failed: {self.error}")
        finally:
            self.counting = False
            self._adopt_shared()

    def loop(self) -> None:
        while True:
            self._adopt_shared()
            if self.body is None or time.time() - self.counted_at >= STATS_REFRESH_SECONDS:
                self.refresh()
            time.sleep(300 if self.body is None else 600)

    def answer(self) -> tuple[int, dict]:
        self._adopt_shared()
        if self.body is None:
            note = ("Beacon is counting its index right now (it takes a few minutes on "
                    "32 GB). Ask again in about five minutes." if self.counting or not self.error
                    else f"The last count failed ({self.error}); it retries automatically.")
            return 503, {"error": note, "retry_after_seconds": 300}
        answer = dict(self.body)
        minutes = int((time.time() - self.counted_at) / 60)
        answer["counted"] = (f"{minutes} minutes ago" if minutes else "just now")
        return 200, answer


STATS = StatsCache()


def read_tools() -> list[dict]:
    """What the finder offers to its READ key, minus anything that smells like a write."""
    url, token = finder()
    status, body = _post(f"{url}/tools", token, None, "GET")
    if status != 200:
        raise RuntimeError(f"the finder answered {status} — is Beacon running?")
    return [t for t in body.get("tools", []) if t.get("name") not in WRITE_TOOLS]


class Gateway(BaseHTTPRequestHandler):
    server_version = "BeaconGateway/1.0"
    token = ""
    tools: list[dict] = []
    powers = None                               # MusePowers when ACCESS == "agent"
    _hits: list[float] = []
    _lock = threading.Lock()

    def log_message(self, *_args) -> None:
        return                                  # our own log() is the record, not this

    # -- plumbing ---------------------------------------------------------------------
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.startswith("Bearer ") else ""
        # compare_digest: a plain == leaks the answer one character at a time.
        if supplied and hmac.compare_digest(supplied, self.token):
            return True
        log(f"REFUSED {self.command} {self.path} — no valid key")
        self._send(401, {"error": "a valid key is required in the Authorization header"})
        return False

    def _within_rate_limit(self) -> bool:
        now = time.time()
        with Gateway._lock:
            Gateway._hits = [t for t in Gateway._hits if now - t < 60]
            if len(Gateway._hits) >= MAX_CALLS_PER_MINUTE:
                log(f"REFUSED {self.path} — more than {MAX_CALLS_PER_MINUTE} calls a minute")
                self._send(429, {"error": f"limit is {MAX_CALLS_PER_MINUTE} calls a minute"})
                return False
            Gateway._hits.append(now)
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except ValueError:
            return {}

    # -- the tools ---------------------------------------------------------------------
    def _run(self, name: str, args: dict) -> tuple[int, dict]:
        """Refuse anything not on the allowlist, then let the finder do the work."""
        if self.powers is not None and name in self.powers.names:
            started = time.time()
            ok, out = self.powers.run(name, args)
            log(f"{name} {json.dumps(args, default=str)[:200]} -> {'ok' if ok else 'ERROR'} "
                f"in {time.time() - started:.1f}s (agent access)")
            return (200 if ok else 422), _as_result(ok, out)
        if name in WRITE_TOOLS or name not in {t["name"] for t in self.tools}:
            if self.powers is not None:
                log(f"REFUSED {name} — not offered on this connection")
                return 403, {"error": f"'{name}' is not available on this connection "
                                      "(see /tools for what is)"}
            log(f"REFUSED {name} — read-only gateway")
            return 403, {"error": f"'{name}' is not available: this connection is read-only"}
        if name in CACHED_TOOLS:
            status, body = STATS.answer()
            log(f"{name} -> {status} from the background count")
            return status, body
        url, token = finder()
        started = time.time()
        status, body = _post(f"{url}/tools/{name}", token, args)
        log(f"{name} {json.dumps(args, default=str)[:200]} -> {status} in "
            f"{time.time() - started:.1f}s")
        return status, body

    def do_GET(self) -> None:                                   # noqa: N802
        if not self._allowed():
            return
        if self.path == "/health":
            status, payload = 200, {"ok": True, "service": "beacon-gateway",
                                    "access": "agent" if self.powers is not None else "read-only",
                                    "tools": len(self.tools)}
        elif self.path == "/tools":
            status, payload = 200, {"tools": self.tools}
        elif self.path == "/guide":
            status, payload = 200, {"guide": self._guide()}
        else:
            status, payload = 404, {"error": "try /health, /tools, /guide, /tools/<name> or /mcp"}
        # The caretaker (muse_link.py) checks /health every 2 minutes; logging those buried
        # Muse's real visits under two routine lines per run. Everything else is recorded —
        # BEFORE answering, so the record is complete the moment the caller has its answer.
        if not (self.path == "/health"
                and self.headers.get("User-Agent", "") == CARETAKER_AGENT):
            log(f"GET {self.path} -> {status}")
        self._send(status, payload)

    def do_POST(self) -> None:                                  # noqa: N802
        if not self._allowed() or not self._within_rate_limit():
            return
        if self.path.startswith("/tools/"):
            status, body = self._run(self.path.split("/", 2)[-1], self._body())
            self._send(status, body)
        elif self.path == "/mcp":
            self._send(200, self._mcp(self._body()))
        else:
            self._send(404, {"error": "try /health, /tools, /tools/<name> or /mcp"})

    def _guide(self) -> str:
        if self.powers is not None:
            from muse_powers import GUIDE
            return GUIDE
        return READ_GUIDE

    def _mcp(self, payload: dict) -> dict:
        """The same tools for anything that speaks MCP over HTTP."""
        reply = {"jsonrpc": "2.0", "id": payload.get("id")}
        method = payload.get("method")
        if method == "initialize":
            reply["result"] = {"protocolVersion": "2025-03-26",
                               "serverInfo": {"name": "beacon-gateway", "version": "1.1"},
                               "capabilities": {"tools": {}},
                               "instructions": self._guide()}
        elif method == "tools/list":
            reply["result"] = {"tools": [
                {"name": t["name"], "description": t.get("description", ""),
                 "inputSchema": t.get("inputSchema") or t.get("schema") or {}}
                for t in self.tools]}
        elif method == "tools/call":
            params = payload.get("params") or {}
            status, body = self._run(params.get("name", ""), params.get("arguments") or {})
            if status == 200:
                reply["result"] = body
            else:
                reply["result"] = {"isError": True, "content": [
                    {"type": "text", "text": json.dumps(body, default=str)}]}
        else:
            reply["error"] = {"code": -32601, "message": f"unknown method: {method}"}
        return reply


def main() -> None:
    try:
        tools = read_tools()
    except Exception as exc:                                    # noqa: BLE001
        print(f"Cannot reach Beacon's finder: {exc}\n"
              f"Open Beacon (or start finder_service.py) and try again.", file=sys.stderr)
        raise SystemExit(1)
    Gateway.token = gateway_token()
    if ACCESS == "agent":
        Gateway.powers = build_powers()
        offered = Gateway.powers.names
        # The agent's own version of a tool wins (it applies Beacon's rules); read tools the
        # agent does not have stay available exactly as before.
        tools = [t for t in tools if t["name"] not in offered] + Gateway.powers.tools()
    Gateway.tools = tools
    threading.Thread(target=STATS.loop, name="stats-count", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Gateway)
    access = "agent access" if Gateway.powers is not None else "read-only"
    log(f"gateway up on http://{HOST}:{PORT} — {len(tools)} tools, {access}, "
        f"key in {TOKEN_FILE.name}")
    print("The key is NOT printed here. Read it from "
          f"{TOKEN_FILE} when you need to hand it over.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("gateway stopped — the outside door is closed")


if __name__ == "__main__":
    main()
