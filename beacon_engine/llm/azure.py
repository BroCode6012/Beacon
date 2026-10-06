"""The model client used by every agent in the pipeline.

WHICH provider answers is decided by beacon_engine/llm/providers.py — one list of every
provider Beacon can use (Foundry, APlink, Kaggle, ...). This file only knows the two ways
of talking to them: any OpenAI-compatible /chat/completions endpoint (`_openai_call`),
and Foundry managed compute (`_azure_complete`), whose quirks are below. A provider that
fails falls back to the ones its entry names, never to one a probe found refused.

The Foundry path is a MANAGED-COMPUTE deployment — an open-weights model on a rented
GPU. Three things differ from the old Claude-on-Bedrock/Anthropic path, and the code
exists mostly to absorb them:
  * No prompt caching. The reused research "ammunition" is prepended to the system
    message and re-sent every call, because managed compute has nowhere to cache it.
  * Reasoning is billed against the ANSWER's token budget. Ask a thinking model for
    220 tokens and it spends all 220 reasoning and returns an empty string, so every
    request is raised to a floor (a bigger one when the call thinks) and retried at
    the ceiling if the answer still comes back empty. See MODELS in beacon_engine/config.py.
  * Reasoning may arrive inline in the content and has to be stripped off the answer.
    DeepSeek V4 is tidier — it returns the scratchpad in a separate `reasoning` field —
    but the strip stays for models that do not separate them.

Tier no longer selects a different model (one deployment serves all three); it selects
how much room the request gets.

THINKING IS PER REQUEST. Hybrid models answer straight out unless the call carries
`reasoning_effort`, so Beacon sends it according to the stage's own thinking/effort flags
(FOUNDRY_REASONING=off|auto|low|medium|high). A deployment with no thinking switch
rejects the argument once, is remembered, and is asked plainly from then on — so the
model in .env can be swapped for one of either kind without touching this file.

TEXT AND VISION CAN BE TWO DIFFERENT DEPLOYMENTS, on two different Foundry resources.
The writer (DeepSeek V4 Flash) cannot read images; the model that reads thumbnails
(Qwen3-VL) writes worse. So calls carrying images route to the vision deployment and
everything else to the text one — see `_target`. Configure the pair with
FOUNDRY_VISION_ENDPOINT / FOUNDRY_VISION_API_KEY / FOUNDRY_VISION_DEPLOYMENT; leave the
endpoint and key unset when both deployments live on the SAME resource (today's setup),
and all three unset when one multimodal deployment serves both jobs.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field

import requests

from beacon_engine.config import (
    MODELS, PRICING, REASONING_MIN_OUTPUT_TOKENS, Tier, is_managed_compute, settings,
    vision_target,
)
from beacon_engine.llm import providers
from beacon_engine.llm.providers import Provider
from beacon_engine.logging_util import get_logger

log = get_logger()


def _billed_usd(usage: dict) -> float | None:
    """The provider's own charge for a call, when it reports one (Kaggle: nanodollars)."""
    cost = usage.get("cost")
    if not isinstance(cost, dict):
        return None
    nano = sum(float(v or 0) for k, v in cost.items() if k.endswith("_nanodollars"))
    return nano / 1e9


# Reasoning models emit their scratchpad inline before the answer. Ornith/Gemma-4 use
# `<|channel>thought ... <channel|>`; most other open weights use `<think>...</think>`.
# Anything still open at the end of the string is an unterminated thought, which means
# the answer never arrived — dropping it lets the caller retry with a bigger budget.
_REASONING_BLOCKS = (
    re.compile(r"<\|?channel\|?>\s*thought\b.*?(?:<\|?/?channel\|?>|\Z)", re.S | re.I),
    re.compile(r"<\s*(think|thought|reasoning)\s*>.*?(?:<\s*/\s*\1\s*>|\Z)", re.S | re.I),
)

_REASONING_LEVELS = ("low", "medium", "high")


def _strip_reasoning(text: str) -> str:
    """Return just the final answer, with any reasoning scratchpad removed."""
    for pattern in _REASONING_BLOCKS:
        text = pattern.sub("", text)
    return text.strip()


def _reasoning_effort(thinking: bool, effort: str, images) -> str | None:
    """How hard this call should think, or None to have the model answer straight out.

    The pipeline already declares, per stage, whether judgment is wanted (``thinking``)
    and how much (``effort``) — 'auto' simply honours that. Images never think: they go
    to the vision deployment, which is there to read a thumbnail, not deliberate on it.
    """
    mode = settings.foundry_reasoning
    if images or mode == "off":
        return None
    if mode in _REASONING_LEVELS:
        return mode
    if not thinking:                       # mechanical stages: parsing, cleanup, formatting
        return None
    return effort if effort in _REASONING_LEVELS else "high"


@dataclass
class _OpenAIUsage:
    """Usage in the shape ``Usage.add`` expects (no cache fields on this surface)."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Usage:
    """Cumulative token usage and (approximate) cost across a run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    # Per-token providers (everything in providers.PROVIDERS except Foundry)
    api_input_tokens: int = 0
    api_output_tokens: int = 0
    api_cost_usd: float = 0.0
    api_calls: int = 0
    fallback_calls: int = 0                # a provider failed -> its fallback answered
    unpriced_calls: int = 0                # Hub's routed model has no fixed list price
    by_provider: dict = field(default_factory=dict)   # provider id -> calls

    def add(self, tier: Tier, usage) -> None:
        in_price, out_price = PRICING[tier]
        i = getattr(usage, "input_tokens", 0) or 0
        o = getattr(usage, "output_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        self.input_tokens += i
        self.output_tokens += o
        self.cache_write_tokens += cw
        self.cache_read_tokens += cr
        # cache writes ~1.25x input price, cache reads ~0.1x input price
        self.cost_usd += (
            i * in_price
            + cw * in_price * 1.25
            + cr * in_price * 0.10
            + o * out_price
        ) / 1_000_000

    def add_api(self, provider: Provider, prompt_tokens: int, completion_tokens: int,
                exact_cost_usd: float | None = None) -> None:
        """One call to a per-token provider. Uses the provider's own bill when it sends
        one (Kaggle does, in nanodollars), else its list price."""
        self.api_input_tokens += prompt_tokens
        self.api_output_tokens += completion_tokens
        self.api_calls += 1
        self.by_provider[provider.id] = self.by_provider.get(provider.id, 0) + 1
        if provider.id == "hub" and exact_cost_usd is None:
            self.unpriced_calls += 1
        self.api_cost_usd += (exact_cost_usd if exact_cost_usd is not None else
                              (prompt_tokens * provider.price_in
                               + completion_tokens * provider.price_out) / 1_000_000)

    @property
    def total_cost_usd(self) -> float:
        """Known/estimated cost; excludes Hub calls without reported billing."""
        return self.cost_usd + self.api_cost_usd

    @property
    def cost_label(self) -> str:
        if self.unpriced_calls:
            return (f"${self.total_cost_usd:.4f} + Hub" if self.total_cost_usd
                    else "See Hub Activity")
        return f"${self.total_cost_usd:.4f}"


@dataclass
class LLMResult:
    text: str
    tier: Tier
    stop_reason: str | None = None
    via: str = "foundry"   # id of the provider that actually answered
    requested_model: str = ""
    model: str = ""        # provider-reported model, not proof of underlying weights
    upstream_provider: str = ""


def _openai_headers(api_key: str | None) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Some relays sit behind Cloudflare bot-protection (error 1010) that 403s requests
        # with no/default User-Agent. A normal browser UA gets through.
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    }


# A definite "no": key rejected, account banned or out of credit, model/deployment gone.
# Anything else (timeout, 429, 5xx) is "unknown" — a cold managed-compute GPU can take
# minutes to answer and must never be mistaken for a dead one.
_REFUSED_STATUSES = (400, 401, 402, 403, 404)


@dataclass
class EngineHealth:
    state: str           # "ok" | "refused" | "unknown"
    detail: str = ""

    @property
    def refused(self) -> bool:
        return self.state == "refused"


def _error_text(r) -> str:
    """The provider's own reason for a refusal ('insufficient_user_quota: ...'), shortened."""
    try:
        err = r.json().get("error") or {}
        if isinstance(err, dict):
            return ": ".join(x for x in (err.get("code"), err.get("message")) if x)[:120]
        return str(err)[:120]
    except Exception:  # noqa: BLE001 -- a non-JSON error page
        return ""


# A probe asks for room to write a real answer, not 16 tokens. Prepaid relays reserve the
# whole max_tokens up front: APlink with ¥0.004 left (2026-10-01) passed a 16-token probe
# and then 403'd every real call. Only the few tokens of "OK" are actually billed.
PROBE_MAX_TOKENS = 4_000


def probe_provider(provider_id: str | None, timeout: float = 20.0) -> EngineHealth:
    """Will this provider answer a real-sized call? One short request, for the startup check.

    It answers only "is the door open", not "is the model any good": any 200 is "ok",
    even an empty reply. ``cli models`` is the full Foundry round-trip check.
    """
    provider = providers.get(provider_id or providers.default_id())
    if provider is None:
        return EngineHealth("refused", f"unknown provider '{provider_id}'")
    if not provider.configured():
        return EngineHealth("refused", f"{provider.label}: key not set")
    if provider.kind == "foundry":
        root, key = settings.azure_foundry_endpoint, settings.azure_foundry_api_key
        model = MODELS[Tier.SONNET].model_id
        attempts = [(url, LLM._foundry_headers(key), model)
                    for url in LLM._endpoint_urls(root.rstrip("/"), model)]
    else:
        try:
            creds = provider.credentials()
        except (requests.ConnectionError, requests.Timeout) as ex:
            return EngineHealth("unknown", f"login unreachable: {str(ex)[:120]}")
        except Exception as ex:  # noqa: BLE001 -- a login that cannot mint is a definite no
            return EngineHealth("refused", f"login failed: {str(ex)[:120]}")
        model = provider.model_for(Tier.SONNET)
        attempts = [(creds.base_url + "/chat/completions", _openai_headers(creds.api_key),
                     model)]
    detail = "no answer"
    for url, headers, model in attempts:
        body = {"model": model, "max_tokens": PROBE_MAX_TOKENS, "stream": False,
                "messages": [{"role": "user", "content": "Reply with just: OK"}]}
        try:
            r = requests.post(url, json=body, headers=headers, timeout=timeout)
        except Exception as ex:  # noqa: BLE001
            return EngineHealth("unknown", f"{type(ex).__name__}: {str(ex)[:120]}")
        if r.ok:
            return EngineHealth("ok", f"{r.status_code} from {model}")
        why = _error_text(r)
        detail = (f"{r.status_code} {r.reason or ''}".strip() + f" from {model}"
                  + (f" ({why})" if why else ""))
        if r.status_code in (404, 405):              # wrong serving path: try the next one
            continue
        return EngineHealth("refused" if r.status_code in _REFUSED_STATUSES else "unknown",
                            detail)
    return EngineHealth("refused", detail)


@dataclass
class LLM:
    """Entry point for model calls. One instance per run accumulates usage."""

    # deployment id -> the serving path it answered on. Keyed per deployment because
    # the text and vision models may live on different resources.
    _foundry_route: dict = field(default_factory=dict)
    # Deployments that answered "unrecognized argument: reasoning_effort" — they have no
    # thinking switch, so we stop asking them to think for the rest of the session.
    _no_reasoning: set = field(default_factory=set)
    usage: Usage = field(default_factory=Usage)
    # Which entry of providers.PROVIDERS answers; None = providers.default_id(). The
    # engine menu sets it for the session.
    provider: str | None = None
    # Circuit breaker: after this many CONSECUTIVE failures, a provider with a fallback
    # is skipped for a cooldown instead of paying its failure latency on every call.
    _fails: dict = field(default_factory=dict)              # provider id -> failures
    _circuit_threshold: int = 3
    _blocked_until: dict = field(default_factory=dict)      # provider id -> epoch
    _cooldown_secs: int = 180
    # Providers a probe got a definite "no" from (e.g. Foundry 401 after the account was
    # banned): id -> reason. Never used as a fallback — that would only swap the real
    # error for the refusal.
    refused: dict = field(default_factory=dict)

    def _target(self, tier: Tier, images) -> tuple[str, str, str]:
        """Which deployment serves this call: (resource_root, api_key, model_id).

        Text and vision can be two different models on two different Foundry resources
        — the strongest writer available may not read images at all. Calls carrying
        images go to the vision deployment; everything else to the text one. With no
        vision settings configured both resolve to the same place.
        """
        if images:
            root, key, model = vision_target(tier)
            what = "vision"
        else:
            root, key, model = (settings.azure_foundry_endpoint,
                                settings.azure_foundry_api_key,
                                MODELS[tier].model_id)
            what = "text"
        if not root or not key:
            raise RuntimeError(
                f"The {what} deployment is not configured. Set AZURE_FOUNDRY_ENDPOINT "
                "(the resource root, e.g. https://<resource>.services.ai.azure.com) and "
                "AZURE_FOUNDRY_API_KEY"
                + ("; for a vision model on another resource also set "
                   "FOUNDRY_VISION_ENDPOINT, FOUNDRY_VISION_API_KEY and "
                   "FOUNDRY_VISION_DEPLOYMENT." if images else ".")
            )
        return root.rstrip("/"), key, model

    @staticmethod
    def _endpoint_urls(root: str, model_id: str) -> list[str]:
        """Candidate chat-completions URLs for this deployment, best first.

        Foundry lists several URLs per deployment. The resource-wide OpenAI router works
        for most deployments; managed compute also answers on its own per-deployment
        path. We try the router first and remember whichever one answers.
        """
        return [
            f"{root}/openai/v1/chat/completions",
            f"{root}/managed-deployments/{model_id}/v1/chat/completions",
        ]

    @staticmethod
    def _foundry_headers(key: str) -> dict:
        """Foundry accepts the Azure `api-key` header; bearer auth is sent alongside it
        so the same client works against a plain OpenAI-compatible gateway too."""
        return {
            "api-key": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def complete(
        self,
        *,
        tier: Tier,
        instructions: str,
        prompt: str,
        cached_context: str | None = None,
        thinking: bool = True,
        effort: str = "high",
        max_tokens: int = 8_000,
        images: list[tuple[bytes, str]] | None = None,
        task: str | None = None,
    ) -> LLMResult:
        """Run a single completion on the selected provider.

        The provider is ``self.provider`` (the engine menu) or ``providers.default_id()``.
        Calls carrying images go to a provider that can read them. If the provider fails
        after its retries, its ``fallback`` providers are tried in order — except any a
        probe found refused, so a dead fallback never replaces the real error.
        """
        # Every call carries the global creator profile (faceless-creator rules etc.)
        # so the WHOLE system knows who it works for — no per-feature re-teaching.
        from beacon_engine.creator_profile import compose_instructions
        instructions = compose_instructions(instructions)

        chosen = self._chosen(images)
        chain = [chosen] + [p for pid in chosen.fallback
                            if (p := providers.get(pid)) and p.configured()
                            and pid not in self.refused]
        failures: list[tuple[Provider, Exception]] = []
        for i, provider in enumerate(chain):
            has_next = i + 1 < len(chain)
            if has_next and self._breaker_open(provider.id):
                continue
            try:
                if provider.kind == "foundry":
                    res = self._azure_complete(tier, instructions, prompt, cached_context,
                                               thinking, effort, max_tokens, images)
                else:
                    res = self._openai_complete(provider, tier, instructions, prompt,
                                                cached_context, max_tokens, images,
                                                effort, thinking, task)
            except Exception as ex:  # noqa: BLE001
                failures.append((provider, ex))
                self._note_failure(provider.id)
                if has_next:
                    self.usage.fallback_calls += 1
                    log.warning("%s failed: %s -> %s", provider.label, str(ex)[:160],
                                chain[i + 1].label)
                continue
            self._fails[provider.id] = 0
            return res
        # Lead with the chosen provider's OWN error: reporting only the last fallback's
        # error showed Foundry's 401 when APlink had in fact run out of credit (2026-10-01).
        parts = [f"{p.label} failed: {str(ex)[:200]}" for p, ex in failures]
        skipped = [f"{providers.get(pid).label} ({self.refused[pid]})"
                   for pid in chosen.fallback if pid in self.refused and providers.get(pid)]
        raise RuntimeError(
            "; then ".join(parts or [f"{chosen.label} was not tried"])
            + (f" (fallback skipped — {'; '.join(skipped)})" if skipped else "")
        ) from (failures[0][1] if failures else None)

    def _chosen(self, images) -> Provider:
        """The provider for this call: the selected one, or — for images it cannot read —
        the first configured provider that can, Foundry's vision deployment included."""
        chosen = providers.get(self.provider or providers.default_id()) \
            or providers.get("foundry")
        if images and not chosen.vision:
            chosen = next((p for p in providers.available()
                           if p.vision and p.id not in self.refused), chosen)
        return chosen

    def _breaker_open(self, provider_id: str) -> bool:
        until = self._blocked_until.get(provider_id, 0)
        if until and time.time() >= until:                    # cooldown over: try again
            self._blocked_until.pop(provider_id, None)
            self._fails[provider_id] = 0
            log.info("%s cooldown over — trying it again", provider_id)
            return False
        return bool(until)

    def _note_failure(self, provider_id: str) -> None:
        fails = self._fails.get(provider_id, 0) + 1
        self._fails[provider_id] = fails
        if fails >= self._circuit_threshold:
            self._blocked_until[provider_id] = time.time() + self._cooldown_secs

    # -- Foundry managed compute (default / fallback) -----------------------

    def _azure_complete(
        self, tier, instructions, prompt, cached_context, thinking, effort,
        max_tokens, images,
    ) -> LLMResult:
        """One OpenAI-shaped chat completion against the Foundry deployment.

        There is no prompt caching and no Anthropic thinking block here: the deployment
        is an open-weights model behind /chat/completions, so the cached context is just
        prepended to the system message.
        """
        spec = MODELS[tier]
        root, key, model_id = self._target(tier, images)
        cap = spec.max_output_tokens
        reasoning = _reasoning_effort(thinking, effort, images)
        # Reasoning eats the same budget as the answer, so never ask for less than the
        # deployment's floor — a small ask comes back empty, not short. A thinking call
        # needs the bigger floor: the scratchpad is spent before the answer starts.
        floor = max(spec.min_output_tokens,
                    REASONING_MIN_OUTPUT_TOKENS if reasoning else 0)
        budget = max(min(max_tokens, cap), min(floor, cap))

        system_text = "\n\n".join(x for x in (cached_context, instructions) if x)
        if images:
            content: list[dict] = [{"type": "text", "text": prompt}]
            for data, media in images:
                b64 = base64.b64encode(data).decode("ascii")
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:{media};base64,{b64}"}})
            user_msg: dict = {"role": "user", "content": content}
        else:
            user_msg = {"role": "user", "content": prompt}

        messages = [{"role": "system", "content": system_text}, user_msg]

        # Truncation/starvation guard: if the model stopped on length or spent the whole
        # budget reasoning and returned nothing, redo once at the deployment ceiling.
        text, finish = "", None
        for _ in range(2):
            text, finish, usage = self._foundry_call(root, key, model_id, messages,
                                                     budget, reasoning)
            self.usage.add(tier, usage)
            if text and finish != "length":
                break
            if budget >= cap:
                break
            log.info("foundry: %s at %d tokens -> retrying at %d",
                     "empty answer (all budget spent reasoning)" if not text
                     else "hit the length limit", budget, cap)
            budget = cap

        if not text:
            raise RuntimeError(
                f"{model_id} returned no answer within {budget} tokens — it spent "
                "the whole budget reasoning. Raise max_output_tokens for this tier."
            )
        return LLMResult(text=text, tier=tier, stop_reason=finish, via="foundry")

    def _foundry_call(self, root: str, key: str, model_id: str,
                      messages: list[dict], budget: int, reasoning: str | None = None):
        """POST one completion; return (answer_text, finish_reason, usage-like)."""
        body = {
            "model": model_id,
            "messages": messages,
            "max_tokens": budget,
            "stream": False,
        }
        # Hybrid models (DeepSeek V4) think only when asked. Deployments without the
        # switch reject the argument once; after that we stop sending it to them.
        if reasoning and model_id not in self._no_reasoning:
            body["reasoning_effort"] = reasoning
        headers = self._foundry_headers(key)
        # Routes are cached per deployment: the text and vision models can sit on
        # different resources and answer on different paths.
        cached = self._foundry_route.get(model_id) if self._foundry_route else None
        urls = [cached] if cached else self._endpoint_urls(root, model_id)
        last: Exception | None = None
        for url in urls:
            try:
                r = requests.post(url, json=body, headers=headers,
                                  timeout=settings.foundry_timeout_secs)
            except Exception as ex:  # noqa: BLE001 — try the next serving path
                last = ex
                continue
            # Only a wrong PATH is worth retrying elsewhere; a 401 or a 500 means we
            # found the right door and should surface the real error.
            if r.status_code in (404, 405) and url is not urls[-1]:
                last = RuntimeError(f"{r.status_code} at {url}")
                continue
            # "Unrecognized request argument supplied: reasoning_effort" — this model has
            # no thinking switch. Remember that and ask it again plainly, so swapping the
            # deployment in .env never needs a code change.
            if (r.status_code == 400 and "reasoning_effort" in body
                    and "reasoning_effort" in (getattr(r, "text", "") or "")):
                log.info("%s has no thinking switch — asking it plainly from now on",
                         model_id)
                self._no_reasoning.add(model_id)
                return self._foundry_call(root, key, model_id, messages, budget)
            r.raise_for_status()
            r.encoding = "utf-8"
            self._foundry_route[model_id] = url
            return self._parse_completion(r.json())
        raise last or RuntimeError("no Foundry endpoint answered")

    @staticmethod
    def _parse_completion(data: dict):
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"foundry returned no choices: {str(data)[:200]}")
        msg = choices[0].get("message", {}) or {}
        text = _strip_reasoning(msg.get("content") or "")
        raw = data.get("usage", {}) or {}
        usage = _OpenAIUsage(
            input_tokens=int(raw.get("prompt_tokens", 0) or 0),
            output_tokens=int(raw.get("completion_tokens", 0) or 0),
        )
        return text, choices[0].get("finish_reason"), usage

    # -- OpenAI-compatible providers (relays, Kaggle, gateways) ---------------

    def _openai_complete(self, provider: Provider, tier, instructions, prompt,
                         cached_context, max_tokens, images=None, effort="high",
                         thinking=True, task=None) -> LLMResult:
        """One OpenAI /chat/completions call, retried ``provider.retries`` times."""
        last_ex: Exception | None = None
        for attempt in range(max(1, provider.retries)):
            try:
                return self._openai_call(provider, tier, instructions, prompt,
                                         cached_context, max_tokens, images, effort,
                                         thinking, task)
            except requests.HTTPError as ex:
                # Keep the provider's own reason ('insufficient_user_quota: ...') — the
                # bare "403 Forbidden" does not say whether the key or the credit failed.
                why = _error_text(ex.response) if ex.response is not None else ""
                last_ex = RuntimeError(f"{ex} ({why})" if why else str(ex))
                last_ex.__cause__ = ex
                # A minted key can be revoked before it expires: mint again once.
                if (provider.login is not None and ex.response is not None
                        and ex.response.status_code in (401, 403)):
                    provider.login.invalidate()
            except Exception as ex:  # noqa: BLE001
                last_ex = ex
            if attempt + 1 < provider.retries:
                log.info("%s attempt %d/%d failed (%s) — retrying", provider.id,
                         attempt + 1, provider.retries, str(last_ex)[:90])
        raise last_ex or RuntimeError(f"{provider.label} did not answer")

    def _openai_call(self, provider: Provider, tier, instructions, prompt, cached_context,
                     max_tokens, images, effort, thinking, task=None) -> LLMResult:
        creds = provider.credentials()
        system_text = "\n\n".join(x for x in (cached_context, instructions) if x)
        if images:
            content: list[dict] = [{"type": "text", "text": prompt}]
            for data, media in images:
                b64 = base64.b64encode(data).decode("ascii")
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:{media};base64,{b64}"}})
            user_msg: dict = {"role": "user", "content": content}
        else:
            user_msg = {"role": "user", "content": prompt}
        requested = provider.model_for(tier, task=task, images=bool(images))
        cap = provider.max_output_tokens
        body = {
            "model": requested,
            "messages": [{"role": "system", "content": system_text}, user_msg],
            # Never below the provider's floor: a model that thinks first spends that
            # thinking out of this same budget (see Provider.min_output_tokens).
            "max_tokens": min(max(max_tokens, provider.min_output_tokens), cap),
            "stream": False,
        }
        # Reasoning models (GPT-5.x / o-series) accept `reasoning_effort` — make the
        # high-judgment stages think harder, light/mechanical stages think less. Only sent
        # for reasoning-model names so other models don't reject it.
        if any(k in requested.lower() for k in ("gpt-5", "o1", "o3", "o4", "reason")):
            body["reasoning_effort"] = (
                "low" if not thinking else "high" if effort == "high" else "medium")
        # An answer cut off by the token limit (or eaten entirely by thinking) is redone
        # once at the provider's ceiling — a truncated answer must never pass as complete.
        while True:
            r = requests.post(creds.base_url + "/chat/completions", json=body,
                              headers=_openai_headers(creds.api_key), timeout=provider.timeout)
            r.raise_for_status()
            # Relays often omit the charset header → requests guesses latin-1 and mangles
            # UTF-8 punctuation (— ' " …) into mojibake (â□□). Force UTF-8.
            r.encoding = "utf-8"
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(f"{provider.id} returned no choices: {str(data)[:200]}")
            msg = choices[0].get("message", {}) or {}
            text = _strip_reasoning(msg.get("content") or "")
            finish = choices[0].get("finish_reason")
            usage = data.get("usage", {}) or {}
            pin = int(usage.get("prompt_tokens", 0) or 0)
            pout = int(usage.get("completion_tokens", 0) or 0)
            self.usage.add_api(provider, pin, pout, _billed_usd(usage))
            if (text and finish != "length") or body["max_tokens"] >= cap:
                break
            log.info("%s: %s at %d tokens -> retrying at %d", provider.id,
                     "empty answer" if not text else "cut off by the length limit",
                     body["max_tokens"], cap)
            body["max_tokens"] = cap
        if not text:
            raise RuntimeError(f"{provider.id} returned empty content")
        if finish == "length":
            raise RuntimeError(f"{provider.id} answer was truncated at its output limit; "
                               "shorten the task or choose a larger output budget")

        # Substitution check: what model did the provider SAY it used vs what we asked?
        route_headers = r.headers if provider.id == "hub" else {}
        served = (route_headers.get("x-hub-model") or data.get("model") or "").strip()
        families = ("opus", "sonnet", "haiku", "gpt", "deepseek", "gemini", "qwen", "glm")
        core = next((f for f in families if f in requested.lower()), "")
        if core and served and core not in served.lower():
            log.warning("MODEL MISMATCH: asked '%s' but %s served '%s' "
                        "(possible substitution!)", requested, provider.id, served)
        else:
            log.info("%s served '%s' (asked '%s') | in=%d out=%d", provider.id,
                     served or "?", requested, pin, pout)
        return LLMResult(text=text, tier=tier, stop_reason=finish, via=provider.id,
                         requested_model=requested, model=served,
                         upstream_provider=route_headers.get("x-hub-provider", ""))

    # -- reporting ----------------------------------------------------------

    def cost_summary(self) -> str:
        u = self.usage
        # On managed compute the GPU is rented by the hour, so a per-token dollar figure
        # would be fiction. Report the tokens and say where the money actually goes.
        lines = []
        if u.input_tokens or u.output_tokens:
            # Managed compute rents the GPU by the hour, so a per-token dollar figure
            # would be fiction: report the tokens and say where the money goes.
            lines.append(
                f"Foundry: tokens in={u.input_tokens:,} out={u.output_tokens:,} "
                + ("| billed by GPU-hour, not per token" if is_managed_compute()
                   else f"| ${u.cost_usd:.4f}"))
        if u.api_calls or u.fallback_calls:
            who = ", ".join(f"{pid} {n}" for pid, n in sorted(u.by_provider.items()))
            lines.append(f"providers: {u.api_calls} calls ({who}) in={u.api_input_tokens:,} "
                         f"out={u.api_output_tokens:,} ${u.api_cost_usd:.4f}"
                         f" | fallbacks: {u.fallback_calls}")
        if u.unpriced_calls:
            lines.append(f"Known cost ${u.total_cost_usd:.4f}; {u.unpriced_calls} Hub calls "
                         "have unknown cost here — see Hub Activity for routed usage.")
        else:
            lines.append(f"TOTAL real cost ${u.total_cost_usd:.4f}")
        return "\n".join(lines)
