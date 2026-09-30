"""Contracts for the outside door (beacon_gateway).

This is the only part of Beacon an outside agent can reach, so the tests that matter are
the refusals: no key, wrong key, a write tool, and a runaway loop. The finder is stubbed —
what is under test is what this gateway lets through, not what the finder does with it.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import beacon_gateway as gw

REAL_POST = gw._post          # kept before any test patches it

READ_TOOLS = [
    {"name": "count_channels", "description": "count", "inputSchema": {"type": "object"}},
    {"name": "find_channels", "description": "find", "inputSchema": {"type": "object"}},
]


class _Server:
    """The real handler on a real socket, with the finder stubbed out behind it."""

    def __init__(self, tools=None, token="test-key"):
        gw.Gateway.token = token
        gw.Gateway.tools = tools if tools is not None else list(READ_TOOLS)
        gw.Gateway._hits = []
        self.calls: list = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gw.Gateway)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


def request(server, path, token="test-key", body=None, method=None):
    """-> (status, payload). An HTTP error is a result here, not an exception."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        server.url(path), headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
        method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.server = _Server()
        self.addCleanup(self.server.stop)
        self.forwarded: list = []

        def fake_post(url, token, body, method="POST"):
            self.forwarded.append((url, token, body, method))
            return 200, {"structuredContent": {"count": 7}}

        patch = mock.patch.object(gw, "_post", fake_post)
        patch.start()
        self.addCleanup(patch.stop)
        finder = mock.patch.object(gw, "finder",
                                   lambda: ("http://127.0.0.1:8765", "the-read-key"))
        finder.start()
        self.addCleanup(finder.stop)
        log = mock.patch.object(gw, "log", lambda _line: None)
        log.start()
        self.addCleanup(log.stop)

    # -- refusals ----------------------------------------------------------------------
    def test_no_key_is_refused(self):
        status, body = request(self.server, "/health", token=None)
        self.assertEqual(status, 401)
        self.assertIn("key", body["error"])
        self.assertEqual(self.forwarded, [])          # the finder was never asked

    def test_a_wrong_key_is_refused(self):
        self.assertEqual(request(self.server, "/health", token="guess")[0], 401)
        self.assertEqual(request(self.server, "/tools", token="test-ke")[0], 401)
        self.assertEqual(self.forwarded, [])

    def test_a_write_tool_is_refused_before_the_database_is_touched(self):
        for tool in ("exclude_by_filter", "stop_scraper", "run_junk_filter"):
            status, body = request(self.server, f"/tools/{tool}", body={"max_subs": 1})
            self.assertEqual(status, 403, tool)
            self.assertIn("read-only", body["error"])
        self.assertEqual(self.forwarded, [])

    def test_a_tool_that_is_not_offered_is_refused(self):
        status, _ = request(self.server, "/tools/made_up_tool", body={})
        self.assertEqual(status, 403)
        self.assertEqual(self.forwarded, [])

    def test_a_runaway_loop_is_capped(self):
        with mock.patch.object(gw, "MAX_CALLS_PER_MINUTE", 3):
            codes = [request(self.server, "/tools/count_channels", body={})[0]
                     for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])

    # -- what it does allow --------------------------------------------------------------
    def test_a_read_tool_is_forwarded_with_the_finders_read_key(self):
        status, body = request(self.server, "/tools/count_channels",
                               body={"topic": "History"})
        self.assertEqual(status, 200)
        self.assertEqual(body["structuredContent"], {"count": 7})
        url, token, sent, _method = self.forwarded[0]
        self.assertTrue(url.endswith("/tools/count_channels"))
        self.assertEqual(token, "the-read-key")       # never the full key
        self.assertEqual(sent, {"topic": "History"})

    def test_health_and_tool_list_say_it_is_read_only(self):
        status, body = request(self.server, "/health")
        self.assertEqual((status, body["access"]), (200, "read-only"))
        names = {t["name"] for t in request(self.server, "/tools")[1]["tools"]}
        self.assertEqual(names, {"count_channels", "find_channels"})
        self.assertFalse(names & gw.WRITE_TOOLS)

    # -- the same tools over MCP ---------------------------------------------------------
    def test_mcp_lists_and_calls_the_same_read_tools(self):
        listed = request(self.server, "/mcp",
                         body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[1]
        self.assertEqual({t["name"] for t in listed["result"]["tools"]},
                         {"count_channels", "find_channels"})
        called = request(self.server, "/mcp", body={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "count_channels", "arguments": {"topic": "Space"}}})[1]
        self.assertEqual(called["result"]["structuredContent"], {"count": 7})
        self.assertEqual(self.forwarded[-1][2], {"topic": "Space"})

    def test_mcp_write_attempts_come_back_as_errors_not_results(self):
        reply = request(self.server, "/mcp", body={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "exclude_by_filter", "arguments": {"max_subs": 1}}})[1]
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(self.forwarded, [])


class TokenTests(unittest.TestCase):
    def test_the_key_is_made_once_kept_and_is_not_the_finders(self):
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in tmp.glob("*")] and None)
        with mock.patch.object(gw, "TOKEN_FILE", tmp / "muse_gateway.token.json"), \
                mock.patch.object(gw, "STORAGE", tmp), \
                mock.patch.dict("os.environ", {"BEACON_GATEWAY_TOKEN": ""}):
            first = gw.gateway_token()
            self.assertGreaterEqual(len(first), 32)       # not a guessable word
            self.assertEqual(gw.gateway_token(), first)   # reused, not regenerated
            self.assertNotIn(first, ("signal-full", "lab-read"))

    def test_the_write_list_covers_the_tools_that_change_the_index(self):
        for name in ("exclude_by_filter", "run_junk_filter", "set_channel_topic",
                     "stop_scraper", "set_scraper_config", "create_niche"):
            self.assertIn(name, gw.WRITE_TOOLS)


class SlowAnswerTests(unittest.TestCase):
    """2026-09-28: Muse asked db_stats twice; it ran 300s each time and Cloudflare's tunnel
    had already given up at ~100s, so Muse got nothing — and each try left a full-index
    count running on the laptop."""

    def setUp(self):
        self.server = _Server(tools=READ_TOOLS + [
            {"name": "db_stats", "description": "size", "inputSchema": {}}])
        self.addCleanup(self.server.stop)
        self.forwarded, self.lines = [], []
        for target, value in (
                ("_post", lambda *a, **k: self.forwarded.append(a) or (200, {"ok": 1})),
                ("finder", lambda: ("http://127.0.0.1:8765", "the-read-key")),
                ("log", self.lines.append)):
            p = mock.patch.object(gw, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.stats = gw.StatsCache()
        p = mock.patch.object(gw, "STATS", self.stats)
        p.start()
        self.addCleanup(p.stop)
        import shutil
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="gw-stats-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.snapshot = tmp / "index_stats.json"
        for name, value in (("SNAPSHOT", self.snapshot), ("LOCK", tmp / "index_stats.lock")):
            p = mock.patch.object(gw.index_stats, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_a_count_saved_by_the_screens_is_answered_at_once(self):
        gw.index_stats.write({"channels": 261857, "videos": 5040000}, 364.0)
        status, body = request(self.server, "/tools/db_stats", body={})
        self.assertEqual(status, 200)
        self.assertEqual(body["structuredContent"]["channels"], 261857)
        self.assertEqual(body["counted"], "just now")
        self.assertEqual(self.forwarded, [])

    def test_a_background_count_is_saved_for_everyone(self):
        with mock.patch.object(gw, "_post", lambda *a, **k: (200, {
                "structuredContent": {"channels": 7, "videos": 70}})):
            self.stats.refresh()
        self.assertEqual(gw.index_stats.read()["stats"]["channels"], 7)
        self.assertEqual(self.stats.body["structuredContent"]["videos"], 70)

    def test_the_index_size_is_served_instantly_from_the_background_count(self):
        import time
        self.stats.body = {"structuredContent": {"channels": 254108, "videos": 4600000}}
        self.stats.counted_at = time.time() - 600
        status, body = request(self.server, "/tools/db_stats", body={})
        self.assertEqual(status, 200)
        self.assertEqual(body["structuredContent"]["channels"], 254108)
        self.assertEqual(body["counted"], "10 minutes ago")      # its age is never hidden
        self.assertEqual(self.forwarded, [])                     # no new 5-minute scan

    def test_before_the_first_count_it_says_so_at_once_instead_of_hanging(self):
        self.stats.counting = True
        status, body = request(self.server, "/tools/db_stats", body={})
        self.assertEqual(status, 503)
        self.assertIn("counting", body["error"])
        self.assertEqual(body["retry_after_seconds"], 300)
        self.assertEqual(self.forwarded, [])

    def test_a_simple_check_is_logged_too(self):
        request(self.server, "/health")
        request(self.server, "/tools")
        self.assertIn("GET /health -> 200", self.lines)
        self.assertIn("GET /tools -> 200", self.lines)

    def test_it_gives_up_before_the_tunnel_does(self):
        self.assertLess(gw.CALL_TIMEOUT, 100)                    # Cloudflare cuts at ~100s

    def test_a_slow_answer_becomes_a_plain_message_not_a_crash(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            status, body = REAL_POST("http://127.0.0.1:9/x", "k", {})
        self.assertEqual(status, 504)
        self.assertIn("narrower", body["error"])


class CaretakerQuietTests(unittest.TestCase):
    """The caretaker's 2-minute /health checks buried Muse's real visits in the log."""

    def setUp(self):
        self.server = _Server()
        self.addCleanup(self.server.stop)
        self.lines = []
        p = mock.patch.object(gw, "log", self.lines.append)
        p.start()
        self.addCleanup(p.stop)

    def _get(self, path, agent):
        req = urllib.request.Request(self.server.url(path), headers={
            "Authorization": "Bearer test-key", "User-Agent": agent})
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status

    def test_the_caretakers_routine_check_is_not_logged(self):
        self.assertEqual(self._get("/health", gw.CARETAKER_AGENT), 200)
        self.assertEqual(self.lines, [])

    def test_everyone_elses_visits_still_are(self):
        self._get("/health", "Muse/1.0")
        self._get("/tools", gw.CARETAKER_AGENT)              # only /health is routine
        self.assertEqual(self.lines, ["GET /health -> 200", "GET /tools -> 200"])

    def test_the_caretaker_and_the_door_agree_on_the_name(self):
        import muse_link
        self.assertEqual(muse_link.USER_AGENT, gw.CARETAKER_AGENT)


class FakePowers:
    """Stands in for muse_powers.MusePowers: records what reached it."""

    names = {"exclude_channels", "find_channels", "recent_changes", "undo_change"}

    def __init__(self):
        self.calls = []

    def run(self, name, args):
        self.calls.append((name, args))
        if name == "exclude_channels" and not args.get("channel_ids"):
            return False, {"error": "no channel ids"}
        return True, {"done": name, "change_id": "abc123"}


class AgentAccessTests(unittest.TestCase):
    """BEACON_GATEWAY_ACCESS=agent: Muse may do what Beacon's own agent does."""

    def setUp(self):
        tools = list(READ_TOOLS) + [{"name": n, "description": n, "inputSchema": {}}
                                    for n in ("exclude_channels", "recent_changes")]
        self.server = _Server(tools=tools)
        self.addCleanup(self.server.stop)
        self.powers = FakePowers()
        gw.Gateway.powers = self.powers
        self.addCleanup(setattr, gw.Gateway, "powers", None)
        self.forwarded = []
        patches = [mock.patch.object(gw, "_post", lambda *a, **k: self.forwarded.append(a)
                                     or (200, {"structuredContent": {"count": 7}})),
                   mock.patch.object(gw, "finder", lambda: ("http://127.0.0.1:8765", "rk")),
                   mock.patch.object(gw, "log", lambda _line: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_change_goes_through_the_agents_tools_not_the_read_key(self):
        status, body = request(self.server, "/tools/exclude_channels",
                               body={"channel_ids": ["UCx"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["structuredContent"]["change_id"], "abc123")
        self.assertEqual(self.powers.calls, [("exclude_channels", {"channel_ids": ["UCx"]})])
        self.assertEqual(self.forwarded, [])

    def test_a_refused_change_is_an_error_not_a_success(self):
        status, body = request(self.server, "/tools/exclude_channels", body={})
        self.assertEqual(status, 422)
        self.assertTrue(body["isError"])
        self.assertEqual(body["error"], "no channel ids")     # readable at the top level

    def test_a_tool_that_is_not_offered_is_not_called_read_only_any_more(self):
        status, body = request(self.server, "/tools/open_view", body={"view": "Channels"})
        self.assertEqual(status, 403)
        self.assertNotIn("read-only", body["error"])
        self.assertEqual(self.powers.calls, [])

    def test_health_says_agent_and_the_guide_is_served(self):
        self.assertEqual(request(self.server, "/health")[1]["access"], "agent")
        guide = request(self.server, "/guide")[1]["guide"]
        self.assertIn("What I did", guide)

    def test_mcp_hands_over_the_rules_on_connect(self):
        reply = request(self.server, "/mcp", body={"jsonrpc": "2.0", "id": 1,
                                                    "method": "initialize"})[1]
        self.assertIn("undo_change", reply["result"]["instructions"])
        called = request(self.server, "/mcp", body={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "recent_changes", "arguments": {}}})[1]
        self.assertEqual(called["result"]["structuredContent"]["done"], "recent_changes")

    def test_still_no_key_no_entry(self):
        status, _ = request(self.server, "/tools/exclude_channels", token=None,
                            body={"channel_ids": ["UCx"]})
        self.assertEqual(status, 401)
        self.assertEqual(self.powers.calls, [])

    def test_the_runaway_limit_still_applies(self):
        with mock.patch.object(gw, "MAX_CALLS_PER_MINUTE", 2):
            codes = [request(self.server, "/tools/recent_changes", body={})[0]
                     for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])


if __name__ == "__main__":
    unittest.main()
