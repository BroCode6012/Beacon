"""Contracts for Beacon's AI switchboard (beacon_ai) and its MCP tool server (beacon_mcp).

What matters is that a provider is a SETTING: definitions load without code changes, keys
never live in the settings file, every adapter speaks the same neutral conversation, and
the Claude Code path reaches Beacon's tools through the bridge. Nothing here calls a real
AI service; the Claude Code path is exercised end to end with a stand-in CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import engine_link  # noqa: F401 — shared engine on the path (beacon_agent needs it)
import beacon_ai as ai
import beacon_mcp
from beacon_agent import BeaconAgent

HERE = Path(__file__).resolve().parent


def _tmpdir(test: unittest.TestCase) -> Path:
    path = Path(tempfile.mkdtemp(prefix="beacon-ai-test-"))
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


class SettingsTests(unittest.TestCase):
    def test_env_references_chain_and_default(self) -> None:
        with mock.patch.dict(os.environ, {"B_SET": "b-value"}, clear=False):
            os.environ.pop("A_UNSET", None)
            self.assertEqual(ai.expand("${A_UNSET|B_SET}"), "b-value")
            self.assertEqual(ai.expand("${A_UNSET:-fallback}"), "fallback")
            self.assertEqual(ai.expand("https://${A_UNSET:-us}-x.com"), "https://us-x.com")
            self.assertEqual(ai.expand("${A_UNSET}"), "")

    def test_a_key_written_into_the_settings_file_is_refused(self) -> None:
        # ai_providers.json can be committed; a literal key there is how keys leak.
        p = ai._build("leaky", {"kind": "openai", "base_url": "https://x", "model": "m",
                                "api_key": "sk-live-123"})
        ok, why = ai.check(p)
        self.assertFalse(ok)
        self.assertIn(".env", why)
        self.assertEqual(p.api_key, "")

    def test_setup_messages_name_the_exact_env_line_to_add(self) -> None:
        with mock.patch.dict(os.environ, {"T_MODEL": "m"}):
            os.environ.pop("T_KEY", None)
            p = ai._build("t", {"kind": "openai", "base_url": "https://x",
                                "api_key": "${T_KEY}", "model": "${T_MODEL}"})
            self.assertIn("T_KEY in Beacon/.env", ai.check(p)[1])

    def test_a_half_filled_address_is_not_ready(self) -> None:
        # A Vertex AI URL without its project id still looks like a URL.
        with mock.patch.dict(os.environ, {"T_KEY": "k"}):
            os.environ.pop("T_PROJECT", None)
            p = ai._build("v", {"kind": "openai", "api_key": "${T_KEY}", "model": "m",
                                "base_url": "https://h/projects/${T_PROJECT}/x"})
            self.assertIn("T_PROJECT", ai.check(p)[1])

    def test_builtins_exist_without_any_settings_file(self) -> None:
        providers, use = ai.load(_tmpdir(self) / "missing.json")
        for name in ("foundry", "foundry-vision", "bedrock", "claude-code"):
            self.assertIn(name, providers)
        self.assertEqual(use, {})

    def test_foundry_keeps_its_resource_root_and_both_url_shapes(self) -> None:
        with mock.patch.dict(os.environ, {
                "BEACON_FOUNDRY_ENDPOINT": "https://res.services.ai.azure.com/openai/v1/x"}):
            p = ai.load(_tmpdir(self) / "none.json")[0]["foundry"]
        self.assertEqual(p.base_url, "https://res.services.ai.azure.com")
        self.assertEqual(len(p.paths), 2)

    def test_the_shipped_settings_file_loads_and_holds_no_secrets(self) -> None:
        raw = json.loads((HERE / "ai_providers.json").read_text(encoding="utf-8"))
        providers, _ = ai.load(HERE / "ai_providers.json")
        for name, spec in raw["providers"].items():
            with self.subTest(provider=name):
                self.assertIn(spec["kind"], ai.KINDS)
                self.assertFalse(providers[name].problem, providers[name].problem)
                if "api_key" in spec:
                    self.assertTrue(spec["api_key"].startswith("${"))


class ChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _tmpdir(self)
        self.choice = self.dir / "_ai_choice.json"
        self.providers = {n: ai.Provider(name=n, kind="openai") for n in ("a", "b", "c", "foundry")}
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("BEACON_AI_AGENT", "BEACON_AI_AUTOPILOT", "BEACON_AGENT_PROVIDER"):
            os.environ.pop(name, None)

    def choose(self, role="agent", use=None):
        return ai.choose(role, providers=self.providers, use=use or {},
                         choice_path=self.choice)

    def test_the_agent_tab_pick_beats_env_and_file(self) -> None:
        os.environ["BEACON_AI_AGENT"] = "b"
        ai.save_choice("agent", "a", path=self.choice)
        self.assertEqual(self.choose(use={"agent": "c"}), "a")

    def test_env_beats_the_file(self) -> None:
        os.environ["BEACON_AI_AGENT"] = "b"
        self.assertEqual(self.choose(use={"agent": "c"}), "b")

    def test_autopilot_follows_the_agent_unless_pinned(self) -> None:
        ai.save_choice("agent", "a", path=self.choice)
        self.assertEqual(self.choose("autopilot"), "a")
        self.assertEqual(self.choose("autopilot", use={"autopilot": "c"}), "c")

    def test_an_unknown_name_is_ignored_and_the_old_setting_still_works(self) -> None:
        ai.save_choice("agent", "deleted-provider", path=self.choice)
        os.environ["BEACON_AGENT_PROVIDER"] = "C"
        self.assertEqual(self.choose(), "c")


_TOOLS = [{"name": "count_channels", "description": "Count.",
           "schema": {"type": "object", "properties": {"topic": {"type": "string"}}}}]

_HISTORY = [
    {"role": "user", "text": "how many?"},
    {"role": "assistant", "text": "", "tool_calls": [
        {"id": "t1", "name": "count_channels", "input": {"topic": "History"}}]},
    {"role": "tool", "results": [
        {"id": "t1", "name": "count_channels", "content": '{"count": 2290}', "is_error": False}]},
]


class _Post:
    def __init__(self, reply) -> None:
        self.reply, self.bodies = reply, []

    def __call__(self, body, **_kw):
        self.bodies.append(json.loads(json.dumps(body)))
        return self.reply


class AdapterTests(unittest.TestCase):
    def test_openai_translates_tools_calls_and_results(self) -> None:
        p = ai.Provider(name="o", kind="openai", model="m", base_url="https://x",
                        paths=("/chat/completions",), api_key="k")
        post = _Post({"choices": [{"message": {"content": "<think>hm</think>2,290 channels"}}]})
        reply = ai.OpenAIChat(p, post).chat("sys", _HISTORY, _TOOLS)
        sent = post.bodies[0]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "tool"])
        self.assertEqual(json.loads(sent[2]["tool_calls"][0]["function"]["arguments"]),
                         {"topic": "History"})
        self.assertEqual(sent[3]["tool_call_id"], "t1")
        self.assertEqual(post.bodies[0]["tools"][0]["function"]["name"], "count_channels")
        self.assertEqual(reply["text"], "2,290 channels")

    def test_openai_keeps_provider_extras_but_only_for_that_provider(self) -> None:
        # Gemini attaches a thought signature to tool calls that must come back unchanged.
        call = {"type": "function", "function": {"name": "count_channels", "arguments": "{}"},
                "extra_content": {"google": {"thought_signature": "sig"}}}
        gem = ai.Provider(name="gemini", kind="openai", model="m", base_url="https://g",
                          paths=("/c",), api_key="k")
        reply = ai.OpenAIChat(gem, _Post({"choices": [{"message": {
            "content": None, "tool_calls": [call]}}]})).chat("s", [], _TOOLS)
        made = reply["tool_calls"][0]
        self.assertEqual(made["id"], "call_1")           # generated when the API omits it
        history = [{"role": "user", "text": "q"},
                   {"role": "assistant", "text": "", "tool_calls": [made]}]

        same = _Post({"choices": [{"message": {"content": "ok"}}]})
        ai.OpenAIChat(gem, same).chat("s", history, _TOOLS)
        self.assertIn("extra_content", same.bodies[0]["messages"][2]["tool_calls"][0])

        other = ai.Provider(name="openrouter", kind="openai", model="m",
                            base_url="https://o", paths=("/c",), api_key="k")
        post = _Post({"choices": [{"message": {"content": "ok"}}]})
        ai.OpenAIChat(other, post).chat("s", history, _TOOLS)
        self.assertNotIn("extra_content", post.bodies[0]["messages"][2]["tool_calls"][0])

    def test_malformed_arguments_become_empty_input(self) -> None:
        p = ai.Provider(name="o", kind="openai", model="m", base_url="https://x",
                        paths=("/c",), api_key="k")
        post = _Post({"choices": [{"message": {"tool_calls": [
            {"id": "c", "function": {"name": "db_stats", "arguments": "{nope"}}]}}]})
        self.assertEqual(ai.OpenAIChat(p, post).chat("s", [], _TOOLS)["tool_calls"][0]["input"], {})

    def test_anthropic_alternates_turns_and_reports_tool_errors(self) -> None:
        p = ai.Provider(name="c", kind="anthropic", model="claude-x", api_key="k")
        history = _HISTORY[:2] + [{"role": "tool", "results": [
            {"id": "t1", "name": "count_channels", "content": '{"error": "x"}', "is_error": True}]}]
        post = _Post({"content": [{"type": "text", "text": "Sorry."}]})
        reply = ai.AnthropicChat(p, post).chat("sys", history, _TOOLS)
        body = post.bodies[0]
        self.assertEqual(body["system"], "sys")
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "assistant", "user"])
        self.assertEqual(body["messages"][1]["content"][0]["type"], "tool_use")
        result = body["messages"][2]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"], result["is_error"]),
                         ("tool_result", "t1", True))
        self.assertEqual(body["tools"][0]["input_schema"], _TOOLS[0]["schema"])
        self.assertEqual(reply["text"], "Sorry.")

    def test_bedrock_marks_errors_and_never_sends_blank_text(self) -> None:
        class Client:
            def converse(self, **kw):
                self.kw = kw
                return {"stopReason": "end_turn",
                        "output": {"message": {"content": [{"text": "done"}]}}}

        client = Client()
        p = ai.Provider(name="b", kind="bedrock", model="m")
        history = _HISTORY[:2] + [{"role": "tool", "results": [
            {"id": "t1", "name": "count_channels", "content": "{}", "is_error": True}]}]
        ai.BedrockChat(p, lambda: client).chat("sys", history, _TOOLS)
        sent = client.kw["messages"]
        self.assertEqual(sent[1]["content"], [{"toolUse": {
            "toolUseId": "t1", "name": "count_channels", "input": {"topic": "History"}}}])
        self.assertEqual(sent[2]["content"][0]["toolResult"]["status"], "error")

    def test_a_history_from_one_kind_is_valid_for_another(self) -> None:
        # Built by an OpenAI-style provider, continued by Claude: no OpenAI wire leaks in.
        p = ai.Provider(name="c", kind="anthropic", model="m", api_key="k")
        history = [{"role": "user", "text": "q"},
                   {"role": "assistant", "text": "", "tool_calls": [{
                       "id": "call_1", "name": "db_stats", "input": {},
                       "wire": {"gemini": {"extra_content": {}}}}]},
                   {"role": "tool", "results": [{"id": "call_1", "name": "db_stats",
                                                 "content": "{}", "is_error": False}]}]
        post = _Post({"content": [{"type": "text", "text": "ok"}]})
        ai.AnthropicChat(p, post).chat("s", history, [])
        self.assertNotIn("extra_content", json.dumps(post.bodies[0]))


class ConnectionMessageTests(unittest.TestCase):
    def test_an_endpoint_that_does_not_exist_is_explained_in_plain_words(self) -> None:
        import requests
        failure = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='fkdopre.openai.azure.com', port=443): Max retries "
            "exceeded (Caused by NameResolutionError(\"Failed to resolve "
            "'fkdopre.openai.azure.com' ([Errno 11001] getaddrinfo failed)\"))")
        with mock.patch("requests.post", side_effect=failure) as post,                 mock.patch.object(ai, "_connect_sleep"):
            with self.assertRaises(ai.ProviderError) as ctx:
                ai.post_json({}, urls=["https://fkdopre.openai.azure.com/openai/v1/chat/completions"],
                             headers={}, timeout=5)
        self.assertIn("could not find the address fkdopre.openai.azure.com", str(ctx.exception))
        self.assertIn("Keys and Endpoint", str(ctx.exception))
        self.assertEqual(post.call_count, ai.CONNECT_TRIES)

    def test_a_brief_network_blip_is_retried_not_fatal(self) -> None:
        import requests
        ok = mock.MagicMock(status_code=200)
        ok.json.return_value = {"choices": [{"message": {"content": "READY"}}]}
        blip = requests.exceptions.ConnectionError("Failed to resolve host (getaddrinfo failed)")
        with mock.patch("requests.post", side_effect=[blip, ok]) as post,                 mock.patch.object(ai, "_connect_sleep"):
            data = ai.post_json({}, urls=["https://x.example/v1/chat/completions"],
                                headers={}, timeout=5)
        self.assertEqual(data["choices"][0]["message"]["content"], "READY")
        self.assertEqual(post.call_count, 2)

    def test_thinking_mode_is_only_requested_when_configured(self) -> None:
        base = dict(kind="openai", model="m", base_url="https://x", paths=("/c",), api_key="k")
        normal = _Post({"choices": [{"message": {"content": "ok"}}]})
        ai.OpenAIChat(ai.Provider(name="n", **base), normal).chat("s", [], [])
        self.assertNotIn("reasoning_effort", normal.bodies[0])
        thinking = _Post({"choices": [{"message": {"content": "ok"}}]})
        ai.OpenAIChat(ai.Provider(name="t", reasoning_effort="high", **base), thinking).chat("s", [], [])
        self.assertEqual(thinking.bodies[0]["reasoning_effort"], "high")

    def test_a_slow_service_says_it_timed_out(self) -> None:
        import requests
        with mock.patch("requests.post", side_effect=requests.exceptions.ReadTimeout("slow")):
            with self.assertRaises(ai.ProviderError) as ctx:
                ai.post_json({}, urls=["https://x.example/v1/chat/completions"], headers={},
                             timeout=7)
        self.assertIn("did not answer within 7 seconds", str(ctx.exception))


class BridgeAndMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list = []

        def call_tool(name, args):
            self.calls.append((name, args))
            return {"count": 2290}, False

        self.bridge = ai.ToolBridge(lambda: [{"name": "count_channels", "description": "d",
                                              "inputSchema": {"type": "object"}}], call_tool)
        self.addCleanup(self.bridge.close)

    def test_the_bridge_refuses_callers_without_the_token(self) -> None:
        req = urllib.request.Request(self.bridge.url + "/tools",
                                     headers={"Authorization": "Bearer wrong"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(self.calls, [])

    def test_mcp_server_over_real_stdio_reaches_beacon_through_the_bridge(self) -> None:
        env = {**os.environ, "BEACON_TOOL_BRIDGE": self.bridge.url,
               "BEACON_TOOL_BRIDGE_TOKEN": self.bridge.token}
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "count_channels", "arguments": {"topic": "History"}}},
        ]
        proc = subprocess.run([sys.executable, str(HERE / "beacon_mcp.py")],
                              input="\n".join(json.dumps(m) for m in lines) + "\n",
                              capture_output=True, text=True, env=env, timeout=60)
        replies = {r["id"]: r for r in map(json.loads, proc.stdout.strip().splitlines())}
        self.assertEqual(sorted(replies), [1, 2, 3])        # the notification gets no reply
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "beacon")
        self.assertEqual(replies[2]["result"]["tools"][0]["name"], "count_channels")
        call = replies[3]["result"]
        self.assertFalse(call["isError"])
        self.assertEqual(json.loads(call["content"][0]["text"]), {"count": 2290})
        self.assertEqual(self.calls, [("count_channels", {"topic": "History"})])

    def test_notifications_get_no_reply_and_unknown_methods_an_error(self) -> None:
        self.assertIsNone(beacon_mcp.handle({"jsonrpc": "2.0", "method": "notifications/x"}))
        err = beacon_mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "nope"})
        self.assertEqual(err["error"]["code"], -32601)


# A stand-in for the `claude` CLI: reads the same flags Beacon passes, starts the MCP
# server from the config exactly as Claude Code would, calls one tool, prints a result.
_FAKE_CLI = textwrap.dedent('''
    import json, os, subprocess, sys
    args = sys.argv[1:]
    def flag(name):
        return args[args.index(name) + 1] if name in args else None
    assert "-p" in args and flag("--output-format") == "json"
    assert flag("--tools") == "", "built-in tools must be switched off"
    assert "--strict-mcp-config" in args and flag("--allowedTools") == "mcp__beacon"
    assert not any(k.startswith("CLAUDE") and k != "CLAUDE_CODE_GIT_BASH_PATH"
                   for k in os.environ), "host session variables leaked"
    system = open(flag("--system-prompt-file"), encoding="utf-8").read()
    prompt = sys.stdin.read()
    server = json.load(open(flag("--mcp-config"), encoding="utf-8"))["mcpServers"]["beacon"]
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "count_channels", "arguments": {"topic": "History"}}}]
    out = subprocess.run([server["command"], *server["args"]],
                         input="\\n".join(map(json.dumps, msgs)) + "\\n",
                         capture_output=True, text=True,
                         env={**os.environ, **server["env"]}, timeout=60).stdout
    replies = {r["id"]: r for r in map(json.loads, out.strip().splitlines())}
    names = [t["name"] for t in replies[2]["result"]["tools"]]
    tool = replies[3]["result"]
    print(json.dumps({"type": "result", "is_error": False, "session_id": "s-1",
                      "result": json.dumps({"prompt": prompt, "tools": names,
                                            "system_ok": "Beacon" in system,
                                            "tool_text": tool["content"][0]["text"],
                                            "tool_error": tool["isError"]})}))
''')


class _Finder:
    def __init__(self) -> None:
        self.calls: list = []

    def call(self, name, **args):
        self.calls.append((name, args))
        return {"count": 2290}


class ClaudeCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        d = _tmpdir(self)
        (d / "fake_claude.py").write_text(_FAKE_CLI, encoding="utf-8")
        if os.name == "nt":
            cli = d / "fake_claude.cmd"
            cli.write_text(f'@"{sys.executable}" "{d / "fake_claude.py"}" %*\n',
                           encoding="utf-8")
        else:
            cli = d / "fake_claude"
            cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{d / "fake_claude.py"}" "$@"\n')
            cli.chmod(0o755)
        self.provider = ai.Provider(name="cc", kind="claude_code", label="Claude Code",
                                    command=str(cli), timeout=120)
        env = mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "x",
                                           "ANTHROPIC_BASE_URL": "http://gateway"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_turn_runs_beacon_tools_inside_beacon(self) -> None:
        finder = _Finder()
        agent = BeaconAgent(finder, sweep=object(), provider=self.provider)
        events: list = []
        reply = json.loads(agent.run("How many History channels?", on_event=events.append))

        self.assertEqual(reply["prompt"], "How many History channels?")
        self.assertTrue(reply["system_ok"])
        self.assertFalse(reply["tool_error"])
        self.assertEqual(json.loads(reply["tool_text"]), {"count": 2290})
        # It went through Beacon's real dispatcher, which adds the opportunity gate.
        [(name, args)] = finder.calls
        self.assertEqual((name, args["topic"], args["faceless_only"]),
                         ("count_channels", "History", True))
        self.assertIn("tool", [e["type"] for e in events])
        # headless: tools that move Beacon's window are not offered
        self.assertNotIn("show_channels", reply["tools"])
        self.assertEqual([m["role"] for m in agent.messages], ["user", "assistant"])

    def test_restricted_mode_still_holds_through_the_cli(self) -> None:
        finder = _Finder()
        agent = BeaconAgent(finder, sweep=object(), provider=self.provider,
                            allowed_tools={"db_stats"})
        reply = json.loads(agent.run("count them"))
        self.assertEqual(reply["tools"], ["db_stats"])
        self.assertTrue(reply["tool_error"])
        self.assertEqual(finder.calls, [])

    def test_the_computers_own_cli_settings_cannot_hijack_a_turn(self) -> None:
        # The operator's ~/.claude/settings.json held model deepreinforce-ai--ornith-10-397b,
        # which the subscription cannot use, and the first real turn failed on it. Beacon
        # names its own model and loads no user settings, hooks or status line.
        runner = ai.ClaudeCodeRunner(self.provider)
        args = runner._command(Path("system.txt"), Path("mcp.json"))
        self.assertEqual(args[args.index("--setting-sources") + 1], "project")
        providers, _ = ai.load(HERE / "ai_providers.json")
        self.assertEqual(providers["claude-code"].model, "sonnet")
        with mock.patch.dict(os.environ, {"BEACON_CLAUDE_CODE_MODEL": "opus"}):
            providers, _ = ai.load(HERE / "ai_providers.json")
        self.assertEqual(providers["claude-code"].model, "opus")

    def test_not_signed_in_is_explained_in_plain_words(self) -> None:
        msg = ai.ClaudeCodeRunner._explain("Not logged in · Please run /login")
        self.assertIn("run `claude`", msg)


if __name__ == "__main__":
    unittest.main()
