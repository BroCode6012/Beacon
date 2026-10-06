"""Localhost JSON and MCP-style HTTP surface for intelligence tools."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from beacon_engine.intelligence.analyst_tools import IntelligenceAnalystTools, TOOL_SCHEMAS
from beacon_engine.research.store import YouTubeStore, resolve_index_path


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    db_path: str | None = None,
    token: str = "",
    read_token: str = "",
    allow_writes: bool = False,
) -> ThreadingHTTPServer:
    tools = IntelligenceAnalystTools(
        YouTubeStore(db_path or resolve_index_path())
    )

    def _access_for(header: str):
        """One service, two keys: `token` grants FULL (read+write) access, `read_token`
        grants READ-ONLY. Returns 'full' | 'read' | None(unauthorized). When neither key is
        configured (legacy calls), the level falls back to the `allow_writes` flag."""
        supplied = header[7:].strip() if header.startswith("Bearer ") else ""
        if token or read_token:
            if token and supplied == token:
                return "full"
            if read_token and supplied == read_token:
                return "read"
            return None
        return "full" if allow_writes else "read"

    class Handler(BaseHTTPRequestHandler):
        server_version = "FacelessIntelligence/1.0"

        def log_message(self, *_args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            if self.path == "/health":
                self._json(200, {
                    "ok": True,
                    "service": "youtube-intelligence",
                    "writes_enabled": self.access == "full",
                    "durable_channel_saves": tools.store.backend == "postgresql",
                })
                return
            if self.path == "/tools":
                self._json(200, {"tools": _tool_list(self.access == "full")})
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            payload = self._body()
            if payload is None:
                return
            if self.path == "/mcp":
                self._mcp(payload)
                return
            if self.path.startswith("/tools/"):
                name = self.path.split("/", 2)[-1]
                self._call_tool(name, payload)
                return
            self._json(404, {"error": "not_found"})

        def _mcp(self, payload: dict) -> None:
            request_id = payload.get("id")
            method = payload.get("method")
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "serverInfo": {
                        "name": "faceless-youtube-intelligence",
                        "version": "1.0",
                    },
                    "capabilities": {"tools": {}},
                }
            elif method == "tools/list":
                result = {"tools": _tool_list(self.access == "full")}
            elif method == "tools/call":
                params = payload.get("params") or {}
                name = params.get("name", "")
                args = params.get("arguments") or {}
                try:
                    result = self._tool_result(name, args)
                except Exception as exc:  # noqa: BLE001
                    self._json(200, {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": str(exc)},
                    })
                    return
            else:
                self._json(200, {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                })
                return
            self._json(200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            })

        def _call_tool(self, name: str, args: dict) -> None:
            try:
                self._json(200, self._tool_result(name, args))
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
            except PermissionError as exc:
                self._json(403, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})

        def _tool_result(self, name: str, args: dict) -> dict:
            schema = TOOL_SCHEMAS.get(name)
            if not schema:
                raise KeyError(f"Unknown intelligence tool: {name}")
            if schema.get("write") and self.access != "full":
                raise PermissionError("Mutating tools need the full-access key (read-only client).")
            result = tools.dispatch(name, args)
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=True, default=str),
                }],
                "structuredContent": result,
            }

        def _authorized(self) -> bool:
            self.access = _access_for(self.headers.get("Authorization", ""))
            if self.access is None:
                self._json(401, {"error": "unauthorized"})
                return False
            return True

        def _body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
                return data
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "invalid_json"})
                return None

        def _json(self, status: int, data) -> None:
            body = json.dumps(data, ensure_ascii=True, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, int(port)), Handler)


def serve_in_thread(**kwargs) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = create_server(**kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _tool_list(allow_writes: bool) -> list[dict]:
    return [
        {
            "name": name,
            "description": schema["description"],
            "inputSchema": {"type": "object", "additionalProperties": True},
        }
        for name, schema in TOOL_SCHEMAS.items()
        if allow_writes or not schema.get("write")
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(resolve_index_path()))
    parser.add_argument("--token", default="", help="FULL access key (read+write)")
    parser.add_argument("--read-token", default="", help="READ-ONLY access key (for the Lab)")
    parser.add_argument("--allow-writes", action="store_true")
    args = parser.parse_args()
    server = create_server(
        host=args.host,
        port=args.port,
        db_path=args.db,
        token=args.token,
        read_token=args.read_token,
        allow_writes=args.allow_writes,
    )
    print(f"Intelligence API listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
