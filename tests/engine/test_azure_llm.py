"""The Foundry managed-compute path: OpenAI /chat/completions, not Anthropic Messages.

The behaviour worth pinning down is what a REASONING deployment does to a request:
it charges its thinking to the answer's own token budget, so a small ask comes back
empty rather than short. These tests cover the floor, the retry, the reasoning strip,
and the two serving paths a Foundry deployment can answer on.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from beacon_engine.config import MODELS, REASONING_MIN_OUTPUT_TOKENS, Tier
from beacon_engine.llm import LLM


class _Response:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, payload: dict | None = None, status_code: int = 200,
                 text: str = ""):
        self.payload = payload or {}
        self.status_code = status_code
        self.encoding = "utf-8"
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self.payload


def _answer(text: str, finish: str = "stop", prompt_tokens: int = 12,
            completion_tokens: int = 4) -> _Response:
    return _Response({
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    })


class _Recorder:
    """Captures each POST and replays a queued response."""

    def __init__(self, *responses: _Response):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        self.calls.append({"url": url, "body": json, "headers": headers,
                           "timeout": timeout})
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    @property
    def budgets(self) -> list[int]:
        return [c["body"]["max_tokens"] for c in self.calls]


def _reasoning(mode: str):
    """Patch the thinking switch, so these tests don't depend on the machine's .env."""
    import dataclasses

    from beacon_engine import config

    return patch("beacon_engine.llm.azure.settings",
                 dataclasses.replace(config.settings, foundry_reasoning=mode))


class FoundryCompletionTests(unittest.TestCase):
    """Budget and routing behaviour, with thinking off so the floors are the plain ones."""

    def _run(self, recorder, *, tier=Tier.SONNET, **kwargs):
        llm = LLM(provider="foundry")
        with _reasoning("off"), patch("beacon_engine.llm.azure.requests.post", recorder):
            return llm, llm.complete(tier=tier, instructions="Be concise.",
                                     prompt="Say hello.", **kwargs)

    def test_sends_an_openai_chat_completion_for_the_configured_deployment(self) -> None:
        rec = _Recorder(_answer("Foundry works"))

        llm, result = self._run(rec, max_tokens=32)

        self.assertEqual(result.text, "Foundry works")
        self.assertEqual(result.via, "foundry")
        body = rec.calls[0]["body"]
        self.assertEqual(body["model"], MODELS[Tier.SONNET].model_id)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertTrue(rec.calls[0]["url"].endswith("/chat/completions"))
        self.assertEqual(llm.usage.input_tokens, 12)
        self.assertEqual(llm.usage.output_tokens, 4)

    def test_a_small_ask_is_raised_to_the_tier_floor(self) -> None:
        """220 tokens would be spent entirely on reasoning, so it must not be sent."""
        rec = _Recorder(_answer("Fine."))

        self._run(rec, tier=Tier.HAIKU, max_tokens=220)

        self.assertEqual(rec.budgets, [MODELS[Tier.HAIKU].min_output_tokens])

    def test_a_large_ask_is_still_capped_at_the_ceiling(self) -> None:
        rec = _Recorder(_answer("Fine."))

        self._run(rec, tier=Tier.HAIKU, max_tokens=999_999)

        self.assertEqual(rec.budgets, [MODELS[Tier.HAIKU].max_output_tokens])

    def test_an_answerless_reply_is_retried_at_the_ceiling(self) -> None:
        """All budget spent thinking -> one retry with the model's full room."""
        spec = MODELS[Tier.SONNET]
        rec = _Recorder(_answer("<think>still reasoning"), _answer("Got there."))

        _llm, result = self._run(rec, max_tokens=100)

        self.assertEqual(result.text, "Got there.")
        self.assertEqual(rec.budgets, [spec.min_output_tokens, spec.max_output_tokens])

    def test_a_length_stop_is_retried_at_the_ceiling(self) -> None:
        spec = MODELS[Tier.SONNET]
        rec = _Recorder(_answer("cut off mid-", finish="length"), _answer("Complete."))

        _llm, result = self._run(rec, max_tokens=100)

        self.assertEqual(result.text, "Complete.")
        self.assertEqual(rec.budgets, [spec.min_output_tokens, spec.max_output_tokens])

    def test_reasoning_is_stripped_off_the_answer(self) -> None:
        rec = _Recorder(_answer("<|channel>thought\nplan it<channel|>The hook line."))

        _llm, result = self._run(rec, max_tokens=32)

        self.assertEqual(result.text, "The hook line.")

    def test_still_answerless_after_the_retry_raises(self) -> None:
        rec = _Recorder(_answer("<think>thinking forever"))

        with self.assertRaises(RuntimeError) as ctx:
            self._run(rec, max_tokens=100)

        self.assertIn("reasoning", str(ctx.exception))

    def test_a_404_falls_through_to_the_managed_deployment_path(self) -> None:
        rec = _Recorder(_Response(status_code=404), _answer("Served here."))

        llm, result = self._run(rec, max_tokens=32)

        self.assertEqual(result.text, "Served here.")
        self.assertIn("/openai/v1/", rec.calls[0]["url"])
        self.assertIn("/managed-deployments/", rec.calls[1]["url"])
        # The working path is remembered per deployment so later calls skip the dead one.
        self.assertEqual(llm._foundry_route[MODELS[Tier.SONNET].model_id],
                         rec.calls[1]["url"])

    def test_images_are_sent_as_openai_image_urls(self) -> None:
        rec = _Recorder(_answer("A thumbnail."))
        llm = LLM(provider="foundry")

        with patch("beacon_engine.llm.azure.requests.post", rec):
            llm.complete(tier=Tier.SONNET, instructions="Look.", prompt="What is it?",
                         max_tokens=32, images=[(b"\x89PNG", "image/png")])

        content = rec.calls[0]["body"]["messages"][1]["content"]
        kinds = [part["type"] for part in content]
        self.assertIn("image_url", kinds)
        self.assertTrue(
            next(p for p in content if p["type"] == "image_url")
            ["image_url"]["url"].startswith("data:image/png;base64,"))


class SplitVisionTests(unittest.TestCase):
    """Text and vision can be two deployments on two different Foundry resources.

    The strongest writer available may not read images at all, so an image-carrying
    call has to leave for a different resource, with a different key and model, while
    plain text keeps going to the writer.
    """

    TEXT_ROOT = "https://writer-resource.services.ai.azure.com"
    VISION_ROOT = "https://vision-resource.services.ai.azure.com"

    def _split(self, **over):
        """Settings is frozen, so swap in a replaced copy rather than setting fields.

        Both module globals are patched: azure.py reads `settings` directly, and
        config.vision_target() reads its own module-level one.
        """
        import dataclasses

        from beacon_engine import config

        base = {"azure_foundry_endpoint": self.TEXT_ROOT,
                "azure_foundry_api_key": "text-key",
                "vision_endpoint": self.VISION_ROOT,
                "vision_api_key": "vision-key",
                "vision_deployment": "qwen--vision"}
        base.update(over)
        swapped = dataclasses.replace(config.settings, **base)
        return (patch("beacon_engine.llm.azure.settings", swapped),
                patch("beacon_engine.config.settings", swapped))

    def _call(self, rec, *, images=None):
        text_p, cfg_p = self._split()
        llm = LLM(provider="foundry")
        with text_p, cfg_p, patch("beacon_engine.llm.azure.requests.post", rec):
            return llm.complete(tier=Tier.HAIKU, instructions="Go.", prompt="Hi",
                                max_tokens=64, images=images)

    def test_text_goes_to_the_writer_deployment(self) -> None:
        rec = _Recorder(_answer("Written."))

        self._call(rec)

        self.assertIn(self.TEXT_ROOT, rec.calls[0]["url"])
        self.assertEqual(rec.calls[0]["body"]["model"], MODELS[Tier.HAIKU].model_id)
        self.assertEqual(rec.calls[0]["headers"]["api-key"], "text-key")

    def test_images_go_to_the_vision_deployment_on_its_own_resource(self) -> None:
        rec = _Recorder(_answer("faceless"))

        self._call(rec, images=[(b"\x89PNG", "image/png")])

        self.assertIn(self.VISION_ROOT, rec.calls[0]["url"])
        self.assertEqual(rec.calls[0]["body"]["model"], "qwen--vision")
        self.assertEqual(rec.calls[0]["headers"]["api-key"], "vision-key")

    def test_without_vision_settings_one_deployment_serves_both(self) -> None:
        rec = _Recorder(_answer("both"))
        blank = {"vision_endpoint": "", "vision_api_key": None, "vision_deployment": ""}
        text_p, cfg_p = self._split(**blank)
        llm = LLM(provider="foundry")
        with text_p, cfg_p, patch("beacon_engine.llm.azure.requests.post", rec):
            llm.complete(tier=Tier.HAIKU, instructions="Go.", prompt="Hi",
                         max_tokens=64, images=[(b"\x89PNG", "image/png")])

        self.assertIn(self.TEXT_ROOT, rec.calls[0]["url"])
        self.assertEqual(rec.calls[0]["body"]["model"], MODELS[Tier.HAIKU].model_id)
        self.assertEqual(rec.calls[0]["headers"]["api-key"], "text-key")

    def test_routes_are_cached_per_deployment_not_globally(self) -> None:
        """A path learned for the writer must not be reused against the vision host."""
        rec = _Recorder(_answer("one"))
        text_p, cfg_p = self._split()
        llm = LLM(provider="foundry")
        with text_p, cfg_p, patch("beacon_engine.llm.azure.requests.post", rec):
            llm.complete(tier=Tier.HAIKU, instructions="Go.", prompt="Hi", max_tokens=64)
            llm.complete(tier=Tier.HAIKU, instructions="Go.", prompt="Hi", max_tokens=64,
                         images=[(b"\x89PNG", "image/png")])

        self.assertIn(self.TEXT_ROOT, rec.calls[0]["url"])
        self.assertIn(self.VISION_ROOT, rec.calls[1]["url"])
        self.assertEqual(set(llm._foundry_route),
                         {MODELS[Tier.HAIKU].model_id, "qwen--vision"})


class ThinkingSwitchTests(unittest.TestCase):
    """Hybrid deployments (DeepSeek V4) think only when the request asks them to.

    The point of the switch is that the model in .env can be swapped for one of either
    kind without a code change: a deployment that has no thinking switch says so once
    and is asked plainly from then on.
    """

    def _call(self, rec, mode, **kwargs):
        llm = LLM(provider="foundry")
        with _reasoning(mode), patch("beacon_engine.llm.azure.requests.post", rec):
            llm.complete(tier=Tier.HAIKU, instructions="Go.", prompt="Hi", **kwargs)
        return llm

    def test_auto_follows_the_stage_that_asked_for_judgment(self) -> None:
        rec = _Recorder(_answer("Thought about it."))

        self._call(rec, "auto", thinking=True, effort="medium", max_tokens=200)

        self.assertEqual(rec.calls[0]["body"]["reasoning_effort"], "medium")

    def test_auto_leaves_mechanical_stages_answering_straight_out(self) -> None:
        """Transcript cleanup and formatting gain nothing from deliberation."""
        rec = _Recorder(_answer("cleaned"))

        self._call(rec, "auto", thinking=False, max_tokens=200)

        self.assertNotIn("reasoning_effort", rec.calls[0]["body"])

    def test_a_forced_level_applies_to_every_text_call(self) -> None:
        rec = _Recorder(_answer("done"))

        self._call(rec, "high", thinking=False, effort="low", max_tokens=200)

        self.assertEqual(rec.calls[0]["body"]["reasoning_effort"], "high")

    def test_off_never_asks_for_thinking(self) -> None:
        rec = _Recorder(_answer("done"))

        self._call(rec, "off", thinking=True, effort="high", max_tokens=200)

        self.assertNotIn("reasoning_effort", rec.calls[0]["body"])

    def test_images_never_think(self) -> None:
        """Images go to the vision model to be read, not deliberated over."""
        rec = _Recorder(_answer("a thumbnail"))

        self._call(rec, "high", max_tokens=200, images=[(b"\x89PNG", "image/png")])

        self.assertNotIn("reasoning_effort", rec.calls[0]["body"])

    def test_a_thinking_call_gets_the_bigger_floor(self) -> None:
        """The scratchpad is spent before the answer starts, so a small ask is raised."""
        rec = _Recorder(_answer("done"))

        self._call(rec, "auto", thinking=True, max_tokens=220)

        self.assertEqual(rec.budgets, [REASONING_MIN_OUTPUT_TOKENS])

    def test_a_deployment_without_a_thinking_switch_is_asked_plainly_after_one_refusal(
            self) -> None:
        refusal = _Response(
            {"error": {"code": "unrecognized_request_argument",
                       "message": "Unrecognized request argument supplied: reasoning_effort"}},
            status_code=400,
            text='{"error":{"message":"Unrecognized request argument supplied: '
                 'reasoning_effort"}}')
        rec = _Recorder(refusal, _answer("plain answer"))
        llm = LLM(provider="foundry")

        with _reasoning("high"), patch("beacon_engine.llm.azure.requests.post", rec):
            first = llm.complete(tier=Tier.HAIKU, instructions="Go.", prompt="Hi",
                                 max_tokens=200)
            llm.complete(tier=Tier.HAIKU, instructions="Go.", prompt="Hi", max_tokens=200)

        self.assertEqual(first.text, "plain answer")
        self.assertEqual(rec.calls[0]["body"]["reasoning_effort"], "high")
        # the retry, and every call after it, drop the argument
        self.assertNotIn("reasoning_effort", rec.calls[1]["body"])
        self.assertNotIn("reasoning_effort", rec.calls[2]["body"])
        self.assertIn(MODELS[Tier.HAIKU].model_id, llm._no_reasoning)


class ProviderFallbackTests(unittest.TestCase):
    """A provider that fails hands the call to the fallbacks its entry names."""

    def _relay(self, **fields):
        from beacon_engine.llm.providers import Provider
        return Provider(id="test-relay", label="Test relay", base_url="https://relay.invalid/v1",
                        key_env=("TEST_RELAY_KEY",), model="unavailable-model", **fields)

    def _run(self, relay, rec):
        import dataclasses

        from beacon_engine import config
        from beacon_engine.llm import providers
        foundry_ready = dataclasses.replace(config.settings,
                                            azure_foundry_endpoint="https://res.services.ai.azure.com",
                                            azure_foundry_api_key="key")
        llm = LLM(provider="test-relay")
        with patch.dict(providers._BY_ID, {"test-relay": relay}),                 patch.dict("os.environ", {"TEST_RELAY_KEY": "k"}),                 patch("beacon_engine.llm.providers.settings", foundry_ready),                 patch("beacon_engine.llm.azure.settings", foundry_ready),                 patch.object(llm, "_openai_call", side_effect=RuntimeError("offline")),                 patch("beacon_engine.llm.azure.requests.post", rec):
            return llm, llm.complete(tier=Tier.HAIKU, instructions="Be concise.",
                                     prompt="Say hello.", max_tokens=32)

    def test_failed_provider_falls_back_to_foundry(self) -> None:
        rec = _Recorder(_answer("Foundry caught it"))
        llm, result = self._run(self._relay(fallback=("foundry",)), rec)
        self.assertEqual(result.via, "foundry")
        self.assertEqual(llm.usage.fallback_calls, 1)
        self.assertEqual(rec.calls[0]["body"]["model"], MODELS[Tier.HAIKU].model_id)

    def test_no_fallback_named_means_the_real_error_surfaces(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._relay(), _Recorder(_answer("unused")))
        self.assertIn("offline", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class EndpointTrimmingTests(unittest.TestCase):
    """The portal shows several URLs per resource; any of them must work.

    Picking the wrong one is the user's most likely mistake, and the failure it causes
    (a 404 from a URL built out of a project path) looks nothing like "wrong endpoint".
    """

    ROOT = "https://apiser-resource.services.ai.azure.com"

    def test_every_url_the_portal_hands_out_trims_to_the_resource_root(self) -> None:
        from beacon_engine.config import _resource_root

        for pasted in (
            self.ROOT,
            self.ROOT + "/",
            self.ROOT + "/api/projects/apiser",       # the project url
            self.ROOT + "/api/projects/apiser/",
            self.ROOT + "/openai/v1/chat/completions",
            self.ROOT + "/openai/v1",
            self.ROOT + "/anthropic",
            self.ROOT + "/v1",
        ):
            with self.subTest(pasted=pasted):
                self.assertEqual(_resource_root(pasted), self.ROOT)

    def test_a_blank_endpoint_stays_blank(self) -> None:
        from beacon_engine.config import _resource_root

        self.assertEqual(_resource_root(""), "")
        self.assertEqual(_resource_root(None), "")
