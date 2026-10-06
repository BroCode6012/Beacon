"""Beacon's tools as an MCP server — so Claude Code (or any MCP client) can drive Beacon.

Two ways it runs:

  BRIDGE mode (automatic). When the Agent tab uses the "Claude Code" provider, Beacon
  starts `claude -p` with this server attached and a private ToolBridge address in the
  environment. Every tool call is forwarded back INTO the running Beacon app and executes
  through the same dispatcher as every other provider: the same finder connection, the
  same view switching, the same Autopilot restrictions. Standard library only, so it
  starts in a blink and any Python can run it.

  STANDALONE mode (you add it yourself). Register it once with Claude Code and you can
  manage Beacon from any Claude Code chat on your subscription:

      claude mcp add beacon -- "D:\\Youtube\\Beacon\\.venv\\Scripts\\python.exe" "D:\\Youtube\\Beacon\\beacon_mcp.py"

  It then connects to the running finder service itself (open Beacon, or start
  finder_service.py) and offers every tool except the ones that move Beacon's window
  around, which need the app.

Protocol: MCP over stdio — one JSON-RPC 2.0 message per line. stdout carries ONLY protocol
messages; anything a library prints is redirected to stderr so it cannot corrupt them.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
BRIDGE = os.getenv("BEACON_TOOL_BRIDGE", "")
BRIDGE_TOKEN = os.getenv("BEACON_TOOL_BRIDGE_TOKEN", "")

_out = sys.stdout.buffer          # captured before anything can redirect sys.stdout


def _write(message: dict) -> None:
    _out.write((json.dumps(message, default=str) + "\n").encode("utf-8"))
    _out.flush()


# -- bridge mode ---------------------------------------------------------------------------
def _bridge(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BRIDGE + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bearer {BRIDGE_TOKEN}",
                 "Content-Type": "application/json"})
    # Tools such as a niche crawl can run for minutes; the CLI enforces its own timeout.
    with urllib.request.urlopen(req, timeout=3600) as resp:
        return json.loads(resp.read().decode("utf-8"))


# -- standalone mode -------------------------------------------------------------------------
_local = None


def _standalone():
    """A headless agent dispatcher on the running finder service. Built on first use so
    `initialize` answers instantly; the engine import takes a few seconds."""
    global _local
    if _local is None:
        with contextlib.redirect_stdout(sys.stderr):
            here = os.path.dirname(os.path.abspath(__file__))
            if here not in sys.path:
                sys.path.insert(0, here)
            import engine_link  # noqa: F401 — shared engine path + database env
            import sweep_control
            from beacon_agent import TOOLS, _UI, BeaconAgent
            from beacon_engine.intelligence.finder_client import FinderClient

            db = os.environ["YOUTUBE_INDEX_DB"]
            url = (f"http://{os.getenv('FINDER_HOST', '127.0.0.1')}:"
                   f"{os.getenv('FINDER_PORT', '8765')}")
            token = os.getenv("FINDER_TOKEN", "signal-full")
            token_file = os.getenv("FINDER_TOKEN_FILE") or os.path.join(
                os.path.dirname(db), "finder.tokens.json")
            if os.path.exists(token_file):
                try:
                    with open(token_file, encoding="utf-8") as fh:
                        data = json.load(fh)
                    url, token = data.get("url", url), data.get("full", token)
                except (OSError, ValueError):
                    pass
            client = FinderClient(url, token)
            agent = BeaconAgent(client, sweep_control, ui=None)
            tools = [{"name": t["toolSpec"]["name"],
                      "description": t["toolSpec"]["description"],
                      "inputSchema": t["toolSpec"]["inputSchema"]["json"]}
                     for t in TOOLS if t["toolSpec"]["name"] not in _UI]
            _local = (client, agent, tools)
    return _local


def list_tools() -> list[dict]:
    if BRIDGE:
        return _bridge("GET", "/tools")["tools"]
    return _standalone()[2]


def call_tool(name: str, arguments: dict) -> tuple[object, bool]:
    if BRIDGE:
        res = _bridge("POST", "/call", {"name": name, "arguments": arguments})
        return res.get("output"), bool(res.get("is_error"))
    client, agent, _tools = _standalone()
    with contextlib.redirect_stdout(sys.stderr):
        if not client.is_up():
            return {"error": "Beacon's finder service is not running. Open Beacon (or "
                             "start finder_service.py) and try again."}, True
        return agent.execute(name, arguments)


# -- JSON-RPC loop -----------------------------------------------------------------------------
def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None                     # a notification (e.g. notifications/initialized)
    params = msg.get("params") or {}
    try:
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "beacon", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": list_tools()}
        elif method == "tools/call":
            output, is_error = call_tool(str(params.get("name") or ""),
                                         params.get("arguments") or {})
            text = output if isinstance(output, str) else json.dumps(output, default=str)
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"unknown method {method}"}}
    except Exception as exc:  # noqa: BLE001 — the client must always get an answer
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    sys.stdout = sys.stderr             # stray prints can never reach the protocol stream
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}})
            continue
        reply = handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            _write(reply)


if __name__ == "__main__":
    main()
