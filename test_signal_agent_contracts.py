from __future__ import annotations

import copy
import inspect
import unittest
from unittest.mock import patch

import engine_link  # noqa: F401
from scripter.intelligence.analyst_tools import IntelligenceAnalystTools

from signal_agent import MAX_TOOL_ROUNDS, TOOLS, SignalAgent


def _tool_spec(name: str) -> dict:
    return next(
        item["toolSpec"] for item in TOOLS if item["toolSpec"]["name"] == name)


def _properties(name: str) -> dict:
    return _tool_spec(name)["inputSchema"]["json"]["properties"]


class SignalAgentSchemaContractTests(unittest.TestCase):
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


class SignalAgentToolResultTests(unittest.TestCase):
    @patch.dict("os.environ", {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
    })
    def test_error_payload_is_reported_as_error_tool_result(self) -> None:
        agent = SignalAgent(_Finder({"error": "finder unavailable"}), sweep=object(),
                            model="test-model")
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
        agent = SignalAgent(_Finder([]), sweep=object(), model="test-model")
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


class SignalAgentHistoryRollbackTests(unittest.TestCase):
    @patch.dict("os.environ", {
        "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"})
    def test_bedrock_error_rolls_back_the_incomplete_turn(self) -> None:
        # Regression: a failed turn must NOT leave a dangling user message, else the next
        # turn appends a 2nd user message → two consecutive user turns → Converse rejects
        # every later turn permanently.
        agent = SignalAgent(_Finder([]), sweep=object(), model="test-model")
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
        agent = SignalAgent(_Finder([]), sweep=object(), model="test-model")
        agent._client = _AlwaysToolBedrock()

        agent.run("loop")   # exhausts MAX_TOOL_ROUNDS

        # the exhausted turn is rolled back — no dangling tool-result (user-role) tail
        self.assertEqual(agent.messages, [])


if __name__ == "__main__":
    unittest.main()
