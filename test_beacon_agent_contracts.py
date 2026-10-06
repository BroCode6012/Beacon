from __future__ import annotations

import copy
import inspect
import unittest
from unittest.mock import patch

import engine_link  # noqa: F401
from beacon_engine.intelligence.analyst_tools import IntelligenceAnalystTools

import beacon_ai
from beacon_agent import MAX_TOOL_ROUNDS, TOOLS, BeaconAgent


def _tool_spec(name: str) -> dict:
    return next(
        item["toolSpec"] for item in TOOLS if item["toolSpec"]["name"] == name)


def _properties(name: str) -> dict:
    return _tool_spec(name)["inputSchema"]["json"]["properties"]


class BeaconAgentSchemaContractTests(unittest.TestCase):
    def test_finder_tool_properties_exist_on_engine_methods(self) -> None:
        for name in ("find_channels", "count_channels", "recent_channels"):
            with self.subTest(name=name):
                method_args = set(inspect.signature(
                    getattr(IntelligenceAnalystTools, name)).parameters)
                self.assertLessEqual(set(_properties(name)), method_args)

    def test_niche_roles_match_workspace_contract(self) -> None:
        roles = _properties("add_channel_to_niche")["role"]["enum"]

        self.assertEqual(roles, ["competitor", "inspiration", "owned"])
        self.assertNotIn("member", roles)

    def test_find_and_show_channel_sorts_include_revenue(self) -> None:
        self.assertIn("revenue", _properties("find_channels")["sort"]["enum"])
        self.assertIn("revenue", _properties("show_channels")["sort"]["enum"])


class _Finder:
    def __init__(self, result) -> None:
        self.result = result

    def call(self, _name, **_args):
        return self.result


class _OneToolThenDoneBedrock:
    def __init__(self) -> None:
        self.calls = 0
        self.messages = []

    def converse(self, **kwargs):
        self.calls += 1
        self.messages.append(copy.deepcopy(kwargs["messages"]))
        if self.calls == 1:
            return {
                "stopReason": "tool_use",
                "output": {"message": {"role": "assistant", "content": [{
                    "toolUse": {
                        "toolUseId": "tool-1",
                        "name": "find_channels",
                        "input": {"limit": 1},
                    }
                }]}},
            }
        return {
            "stopReason": "end_turn",
            "output": {"message": {
                "role": "assistant", "content": [{"text": "Handled the failure."}]
            }},
        }


class _AlwaysToolBedrock:
    def __init__(self) -> None:
        self.calls = 0

    def converse(self, **_kwargs):
        self.calls += 1
        return {
            "stopReason": "tool_use",
            "output": {"message": {"role": "assistant", "content": [{
                "toolUse": {
                    "toolUseId": f"tool-{self.calls}",
                    "name": "find_channels",
                    "input": {"limit": 1},
                }
            }]}},
        }


class BeaconAgentToolResultTests(unittest.TestCase):
    @patch.dict("os.environ", {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
    })
    def test_error_payload_is_reported_as_error_tool_result(self) -> None:
        agent = BeaconAgent(_Finder({"error": "finder unavailable"}), sweep=object(),
                            model="test-model", provider="bedrock")
        bedrock = _OneToolThenDoneBedrock()
        agent._client = bedrock
        events = []

        result = agent.run("find one", on_event=events.append)

        tool_event = next(e for e in events if e["type"] == "tool_result")
        self.assertEqual(tool_event["status"], "error")
        tool_result = bedrock.messages[1][-1]["content"][0]["toolResult"]
        self.assertEqual(tool_result["status"], "error")
        self.assertEqual(result, "Handled the failure.")

    @patch.dict("os.environ", {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
    })
    def test_round_limit_returns_explicit_warning(self) -> None:
        agent = BeaconAgent(_Finder([]), sweep=object(), model="test-model", provider="bedrock")
        bedrock = _AlwaysToolBedrock()
        agent._client = bedrock

        result = agent.run("loop")

        self.assertEqual(bedrock.calls, MAX_TOOL_ROUNDS)
        self.assertIn(f"Stopped after {MAX_TOOL_ROUNDS} tool rounds", result)


class _ThrottleOnceBedrock:
    """Raises on the FIRST converse (simulating a Bedrock throttle), then succeeds."""

    def __init__(self) -> None:
        self.calls = 0

    def converse(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("ThrottlingException: slow down")
        return {
            "stopReason": "end_turn",
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        }


class BeaconAgentHistoryRollbackTests(unittest.TestCase):
    @patch.dict("os.environ", {
        "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"})
    def test_bedrock_error_rolls_back_the_incomplete_turn(self) -> None:
        # Regression: a failed turn must NOT leave a dangling user message, else the next
        # turn appends a 2nd user message → two consecutive user turns → Converse rejects
        # every later turn permanently.
        agent = BeaconAgent(_Finder([]), sweep=object(), model="test-model", provider="bedrock")
        agent._client = _ThrottleOnceBedrock()

        r1 = agent.run("first")
        self.assertTrue(r1.startswith("⚠️ Bedrock error"))
        # history rolled back to empty — no dangling user turn
        self.assertEqual(agent.messages, [])

        # the SAME agent must still work on the next turn (history not bricked)
        r2 = agent.run("second")
        self.assertEqual(r2, "ok")
        # exactly one user turn + one assistant reply survive; roles alternate correctly
        roles = [m["role"] for m in agent.messages]
        self.assertEqual(roles, ["user", "assistant"])

    @patch.dict("os.environ", {
        "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"})
    def test_round_limit_rolls_back_so_next_turn_is_valid(self) -> None:
        agent = BeaconAgent(_Finder([]), sweep=object(), model="test-model", provider="bedrock")
        agent._client = _AlwaysToolBedrock()

        agent.run("loop")   # exhausts MAX_TOOL_ROUNDS

        # the exhausted turn is rolled back — no dangling tool-result (user-role) tail
        self.assertEqual(agent.messages, [])


if __name__ == "__main__":
    unittest.main()


class _FoundryStub:
    """Stands in for an OpenAI-compatible /chat/completions surface (Foundry here).

    Replaces `BeaconAgent._http_post`, the single network seam for HTTP providers, so the
    whole loop (tool translation, tool_calls parsing, history shape, rollback) is
    exercised without a live endpoint.
    """

    def __init__(self, *replies) -> None:
        self.bodies: list[dict] = []
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, body, **_kwargs):
        self.bodies.append(body)
        self.calls += 1
        reply = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _msg(content=None, tool_calls=None):
    m = {"content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return {"choices": [{"message": m}]}


def _call(name, args="{}", cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": args}}


_FOUNDRY_ENV = {
    "BEACON_FOUNDRY_ENDPOINT": "https://example.invalid",
    "BEACON_FOUNDRY_API_KEY": "test",
    "BEACON_FOUNDRY_MODEL": "test-glm",
}


class _RecordingBedrock:
    """A Bedrock client that answers immediately and records what it was sent."""

    def __init__(self) -> None:
        self.messages: list = []

    def converse(self, **kwargs):
        self.messages.append(copy.deepcopy(kwargs["messages"]))
        return {
            "stopReason": "end_turn",
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        }


class FoundryBackendTests(unittest.TestCase):
    """The Foundry (GLM) path is the DEFAULT backend, so it carries the same contracts
    the Bedrock path is held to: errors surface as error tool-results, the round cap is
    explicit, and a turn that cannot finish is rolled back whole."""

    def _agent(self, finder, *replies):
        # A provider passed in directly: the test never depends on what is in .env.
        provider = beacon_ai.Provider(
            name="foundry-test", kind="openai", label="Foundry", model="test-glm",
            base_url="https://example.invalid", paths=("/chat/completions",),
            api_key="test")
        agent = BeaconAgent(finder, sweep=object(), provider=provider)
        stub = _FoundryStub(*replies)
        agent._http_post = stub
        return agent, stub

    def test_tools_are_translated_to_openai_function_shape(self) -> None:
        agent, stub = self._agent(_Finder([]), _msg("done"))
        agent.run("hi")
        tools = stub.bodies[0]["tools"]
        self.assertTrue(tools, "the agent must advertise its tools")
        for t in tools:
            self.assertEqual(t["type"], "function")
            self.assertIn("name", t["function"])
            self.assertIn("parameters", t["function"])

    def test_error_payload_is_reported_as_error_tool_result(self) -> None:
        agent, stub = self._agent(
            _Finder({"error": "finder unavailable"}),
            _msg(None, [_call("find_channels")]),
            _msg("Handled the failure."),
        )
        events = []

        result = agent.run("find one", on_event=events.append)

        tool_event = next(e for e in events if e["type"] == "tool_result")
        self.assertEqual(tool_event["status"], "error")
        self.assertEqual(result, "Handled the failure.")
        # the tool reply is kept on the `tool` role, keyed to the call id...
        tool_turn = next(m for m in agent.messages if m["role"] == "tool")
        self.assertEqual(tool_turn["results"][0]["id"], "c1")
        # ...and reaches the API in its own wire shape on the next round
        wire = next(m for m in stub.bodies[1]["messages"] if m["role"] == "tool")
        self.assertEqual(wire["tool_call_id"], "c1")

    def test_reasoning_scratchpad_is_stripped_from_the_answer(self) -> None:
        agent, _ = self._agent(_Finder([]),
                               _msg("<think>weighing options</think>Final answer."))
        self.assertEqual(agent.run("hi"), "Final answer.")

    def test_malformed_tool_arguments_do_not_break_the_turn(self) -> None:
        agent, _ = self._agent(
            _Finder([]),
            _msg(None, [_call("find_channels", args="{not json")]),
            _msg("recovered"),
        )
        self.assertEqual(agent.run("go"), "recovered")

    def test_round_limit_returns_explicit_warning_and_rolls_back(self) -> None:
        agent, stub = self._agent(_Finder([]), _msg(None, [_call("find_channels")]))

        result = agent.run("loop")

        self.assertEqual(stub.calls, MAX_TOOL_ROUNDS)
        self.assertIn(f"Stopped after {MAX_TOOL_ROUNDS} tool rounds", result)
        self.assertEqual(agent.messages, [])

    def test_backend_error_rolls_back_so_the_next_turn_is_valid(self) -> None:
        agent, _ = self._agent(_Finder([]), RuntimeError("gateway down"), _msg("ok"))

        r1 = agent.run("first")
        self.assertTrue(r1.startswith("⚠️ Foundry error"))
        self.assertEqual(agent.messages, [])

        r2 = agent.run("second")
        self.assertEqual(r2, "ok")
        self.assertEqual([m["role"] for m in agent.messages], ["user", "assistant"])

    def test_switching_provider_keeps_the_conversation(self) -> None:
        # The history is provider-neutral and each provider translates it, so moving from
        # an OpenAI-style provider to Bedrock mid-conversation keeps the thread. (The old
        # design had to wipe it: the two native transcripts are not interchangeable.)
        agent, _ = self._agent(_Finder([]), _msg("hi"))
        agent.run("first")

        bedrock = _RecordingBedrock()
        agent.provider = "bedrock"
        agent.model = "test-model"
        agent._client = bedrock
        self.assertEqual(agent.run("second"), "ok")

        sent = bedrock.messages[0]
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "user"])
        self.assertEqual(sent[0]["content"], [{"text": "first"}])
        self.assertEqual(sent[1]["content"], [{"text": "hi"}])
        self.assertEqual(sent[2]["content"], [{"text": "second"}])
        self.assertEqual([m["role"] for m in agent.messages],
                         ["user", "assistant", "user", "assistant"])
