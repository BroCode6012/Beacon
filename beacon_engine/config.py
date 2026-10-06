"""Configuration: settings, model tiers, and per-tier capabilities.

Cost note: a 20-agent pipeline on pure Opus burns through credits fast (~$5/$25 per
1M tokens). We tier models so only the high-judgment stages (hooks, the line-level
weapons check) pay for Opus, while research synthesis runs on Sonnet and mechanical
work (transcript cleanup, formatting) runs on Haiku. Prompt caching on the reused
research "ammunition" context cuts cost further.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum

from dotenv import load_dotenv
from pathlib import Path

# override=True because .env IS this app's configuration, and a stale machine-level
# variable must not silently win over it. Without this, editing .env appears to do
# nothing whenever the same name is also set in the OS environment — which cost an
# afternoon once: a leftover AZURE_FOUNDRY_API_KEY in the Windows user environment
# shadowed the correct key in .env, and every call 401'd while the file looked right.
# Nothing in .env is a per-process control variable (APP_PROFILE, YOUTUBE_INDEX_DB and
# friends are set by launchers and never live in the file), so nothing legitimate is
# clobbered by preferring the file.
load_dotenv(Path(os.getenv("BEACON_ENV_FILE") or Path(__file__).resolve().parents[1] / ".env"), override=False)


class Tier(str, Enum):
    """Model tier. Map each agent to the cheapest tier that meets its quality bar."""

    OPUS = "opus"      # hardest creative judgment: hooks, weapons check
    SONNET = "sonnet"  # body writing, research synthesis
    HAIKU = "haiku"    # parsing, formatting, transcript cleanup


@dataclass(frozen=True)
class ModelSpec:
    """A Foundry managed-compute deployment plus the request features it supports.

    The deployment is an open-weights model served over the OpenAI-compatible
    ``/chat/completions`` surface, so there is no prompt caching and no Anthropic
    ``thinking``/``effort`` block. Thinking, where the model has it at all, is asked for
    per request instead — see ``FOUNDRY_REASONING`` below. What matters either way is
    that reasoning is billed against the SAME output budget as the answer: ask for 220
    tokens and a thinking model spends all 220 thinking and returns an empty string.
    ``min_output_tokens`` is the floor every request is raised to so the model always
    has room to finish.
    """

    model_id: str
    supports_thinking: bool
    supports_effort: bool
    max_output_tokens: int
    min_output_tokens: int = 1_024


def _first_env(*names: str) -> str:
    """Return the first configured environment variable from ``names``."""
    return next((value for name in names if (value := os.getenv(name, "").strip())), "")


# Suffixes a copied Foundry endpoint may carry. We want the bare resource root so the
# client can build either serving path itself (see beacon_engine/llm/azure.py).
_ENDPOINT_SUFFIXES = ("/anthropic", "/openai/v1", "/openai", "/v1", "/chat/completions")
# The portal also hands out a PROJECT url — https://<resource>.../api/projects/<project>
# — which is not a serving root at all. Everything from /api/projects onward has to go,
# including the project name after it, or the client builds
# ".../api/projects/apiser/openai/v1/chat/completions" and 404s.
_PROJECT_PATH = "/api/projects/"


def _resource_root(endpoint: str) -> str:
    """Trim a pasted Foundry URL back to the bare resource root.

    The console shows several different URLs per resource and it is never obvious which
    one to copy, so accept any of them rather than making that the user's problem.
    """
    endpoint = (endpoint or "").strip().rstrip("/")
    lowered = endpoint.lower()
    if _PROJECT_PATH in lowered:
        endpoint = endpoint[: lowered.index(_PROJECT_PATH)].rstrip("/")
    changed = True
    while endpoint and changed:
        changed = False
        for suffix in _ENDPOINT_SUFFIXES:
            if endpoint.lower().endswith(suffix):
                endpoint = endpoint[: -len(suffix)].rstrip("/")
                changed = True
    return endpoint


def _foundry_endpoint() -> str:
    """The Foundry resource root, e.g. ``https://<resource>.services.ai.azure.com``.

    Accepts whatever shape was pasted out of the portal (the console shows several
    per-deployment URLs) and trims it back to the root.
    """
    return _resource_root(_first_env(
        "AZURE_FOUNDRY_ENDPOINT",
        "CLAUDE_BASE_URL",
        "AZURE_OPENAI_ENDPOINT",
    ))


def _vision_endpoint() -> str:
    """Resource root for the vision deployment; may be a different resource."""
    return _resource_root(_first_env("FOUNDRY_VISION_ENDPOINT"))


# The TEXT deployment, used for every tier — tier no longer picks a different model, only
# how much room the request gets. In practice .env points this at DeepSeek V4 Flash (it
# replaced GLM-5.2, which failed to deploy); the fallback below is Qwen3-VL, the one of
# the pair that also reads images, so a machine with no FOUNDRY_DEPLOYMENT_NAME set still
# gets a model that can do both jobs.
#
# Nothing here is model-specific: swap the name in .env and the whole app follows,
# including the dropdown label and the optional thinking switch below.
#
# Images do NOT come here — see vision_target() and FOUNDRY_VISION_* below.
_DEFAULT_DEPLOYMENT = _first_env(
    "FOUNDRY_DEPLOYMENT_NAME",
    "CLAUDE_DEPLOYMENT_NAME",
    "AZURE_OPENAI_DEPLOYMENT",
) or "qwen--qwen3-vl-30b-a3b-instruct"

_DEFAULT_IDS = {
    Tier.OPUS: _DEFAULT_DEPLOYMENT,
    Tier.SONNET: _DEFAULT_DEPLOYMENT,
    Tier.HAIKU: _DEFAULT_DEPLOYMENT,
}

_ENV_OVERRIDE = {
    Tier.OPUS: "AZURE_FOUNDRY_MODEL_OPUS",
    Tier.SONNET: "AZURE_FOUNDRY_MODEL_SONNET",
    Tier.HAIKU: "AZURE_FOUNDRY_MODEL_HAIKU",
}

# min_output_tokens is the floor every request is raised to. It exists because a
# REASONING deployment charges its thinking to the answer's own budget: measured in the
# Foundry playground, Ornith spent all 4,096 tokens reasoning about a 150-word script and
# returned "Token limit reached" with no text, while 32,000 answered cleanly. Beacon has
# calls budgeted as low as 220 tokens, so without a floor those come back empty.
#
# These floors are the NOT-thinking floors: a model answering straight out needs room
# only for the answer. DeepSeek V4 Flash is a hybrid — it thinks only when the request
# asks it to (see FOUNDRY_REASONING) — and a thinking call is raised to the separate,
# much larger REASONING_MIN_OUTPUT_TOKENS floor instead.
# The empty-answer retry in beacon_engine/llm/azure.py is the backstop either way: it redoes
# the call at max_output_tokens rather than returning nothing.
MODELS: dict[Tier, ModelSpec] = {
    Tier.OPUS: ModelSpec(
        model_id=os.getenv(_ENV_OVERRIDE[Tier.OPUS], _DEFAULT_IDS[Tier.OPUS]),
        supports_thinking=False,
        supports_effort=False,
        max_output_tokens=32_000,
        min_output_tokens=1_024,
    ),
    Tier.SONNET: ModelSpec(
        model_id=os.getenv(_ENV_OVERRIDE[Tier.SONNET], _DEFAULT_IDS[Tier.SONNET]),
        supports_thinking=False,
        supports_effort=False,
        max_output_tokens=32_000,
        min_output_tokens=1_024,
    ),
    Tier.HAIKU: ModelSpec(
        model_id=os.getenv(_ENV_OVERRIDE[Tier.HAIKU], _DEFAULT_IDS[Tier.HAIKU]),
        supports_thinking=False,
        supports_effort=False,
        max_output_tokens=16_000,
        min_output_tokens=512,
    ),
}

# A thinking call spends part of its budget on the scratchpad before a word of the answer
# exists, so it cannot run on the answer-sized floors above. Measured on DeepSeek V4 Flash:
# ~1,000-2,100 completion tokens for a five-constraint logic puzzle, reasoning included.
# 8,000 leaves room for that plus a long answer; the retry at max_output_tokens still
# covers anything that overruns.
REASONING_MIN_OUTPUT_TOKENS = int(os.getenv("FOUNDRY_REASONING_FLOOR", "8000"))

# How much the TEXT deployment is asked to think, sent as OpenAI's `reasoning_effort`:
#   off              never ask — the model answers straight out (fastest)
#   auto (default)   follow the stage: mechanical stages (thinking=False) stay off, the
#                    rest think at the effort the pipeline already declares
#   low|medium|high  force one level on every text call
# A deployment with no thinking switch (Qwen-*-Instruct, GLM) rejects the argument once
# and is then remembered and asked plainly — see beacon_engine/llm/azure.py — so this is safe
# to leave on whatever model .env points at.
# NEVER send "none": measured against DeepSeek V4 Flash on Foundry, `reasoning_effort:
# "none"` hung the request past 10 minutes instead of answering, so it maps to off here.
_REASONING_LEVELS = ("low", "medium", "high")


def _reasoning_mode(raw: str) -> str:
    """Normalise FOUNDRY_REASONING to 'off', 'auto', or one of the levels."""
    value = (raw or "").strip().lower()
    if value in _REASONING_LEVELS or value == "auto":
        return value
    if value in ("off", "none", "no", "false", "0", ""):
        return "off"
    return "auto"


def deployment_display_name(deployment: str) -> str:
    """A Foundry deployment id as a human would say it, for labels and tooltips.

    ``deepseek-ai--deepseek-v4-flash-0731`` -> ``DeepSeek V4 Flash``. The publisher
    prefix, the serving-precision suffix and the training date are noise on a chip, but
    anything unrecognised is kept rather than mangled, so a model nobody anticipated
    still shows a sane name.
    """
    name = (deployment or "").split("--")[-1]
    name = re.sub(r"-(?:fp8|bf16|int[48]|awq|gptq)\b", "", name, flags=re.I)
    name = re.sub(r"-\d{4,8}$", "", name)                    # trailing training date
    words = []
    for word in name.split("-"):
        if not word:
            continue
        special = {"vl": "VL", "ai": "AI", "moe": "MoE", "glm": "GLM", "llm": "LLM",
                   "deepseek": "DeepSeek", "qwen": "Qwen", "openai": "OpenAI"}
        if word.lower() in special:
            words.append(special[word.lower()])
        elif re.fullmatch(r"(?:\d|[a-z]\d)[\w.]*", word, flags=re.I):  # v4, 30b, a3b, 8x7b
            words.append(word.upper())
        else:
            words.append(word.capitalize())                            # qwen3, flash
    return " ".join(words) or deployment


# Managed-compute deployments rent a GPU by the hour; tokens are not billed per token.
# Any model that is not a per-token Claude deployment therefore reports 0.0 rather than
# a made-up number, and cost_summary() says so instead of showing a fake total.
MANAGED_COMPUTE_PRICING = (0.0, 0.0)


def _model_pricing(model_id: str) -> tuple[float, float]:
    """Approx pricing ($ per 1M tokens); 0.0 for hourly-billed managed compute."""
    name = model_id.lower()
    if "claude" not in name:
        return MANAGED_COMPUTE_PRICING
    if "opus" in name:
        return 5.0, 25.0
    if "haiku" in name:
        return 1.0, 5.0
    return 3.0, 15.0


def is_managed_compute() -> bool:
    """True when the configured deployment is billed by GPU-hour, not by token."""
    return _model_pricing(MODELS[Tier.SONNET].model_id) == MANAGED_COMPUTE_PRICING


PRICING: dict[Tier, tuple[float, float]] = {
    tier: _model_pricing(spec.model_id) for tier, spec in MODELS.items()
}

# Approx cheap-relay pricing (real USD per 1M) for the cost-savings meter. Defaults are
# iiitoken-ish; override per tier via env if your relay differs. Only used for reporting.
RELAY_PRICING: dict[Tier, tuple[float, float]] = {
    Tier.OPUS: (
        float(os.getenv("RELAY_PRICE_OPUS_IN", "0.23")),
        float(os.getenv("RELAY_PRICE_OPUS_OUT", "1.31")),
    ),
    Tier.SONNET: (
        float(os.getenv("RELAY_PRICE_SONNET_IN", "0.175")),
        float(os.getenv("RELAY_PRICE_SONNET_OUT", "0.875")),
    ),
    Tier.HAIKU: (
        float(os.getenv("RELAY_PRICE_HAIKU_IN", "0.10")),
        float(os.getenv("RELAY_PRICE_HAIKU_OUT", "0.44")),
    ),
}


@dataclass(frozen=True)
class Settings:
    azure_foundry_endpoint: str = _foundry_endpoint()
    azure_foundry_api_key: str | None = _first_env(
        "AZURE_FOUNDRY_API_KEY",
        "CLAUDE_API_KEY",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "AZURE_OPENAI_API_KEY",
    ) or None

    # -- The vision half of the pair --------------------------------------------
    # The best writer available and the best thumbnail-reader available are not the
    # same model: a strong text deployment (GLM-5.2) cannot see images, and the
    # vision deployment (Qwen3-VL) writes worse. So Beacon talks to both — text calls
    # go to the deployment above, calls carrying images go here — and the two can live
    # on DIFFERENT Foundry resources, each with its own key.
    #
    # Leave these unset and everything collapses back to one deployment doing both,
    # which is correct when the text model is itself multimodal.
    vision_endpoint: str = _vision_endpoint()
    vision_api_key: str | None = _first_env("FOUNDRY_VISION_API_KEY") or None
    vision_deployment: str = _first_env("FOUNDRY_VISION_DEPLOYMENT")
    # How hard the TEXT deployment thinks before answering: off / auto / low / medium /
    # high (see _reasoning_mode). Images never carry it — the vision model only reads.
    foundry_reasoning: str = _reasoning_mode(os.getenv("FOUNDRY_REASONING", "auto"))
    # A reasoning model on rented GPUs answers in minutes, not seconds — the old
    # relay-sized timeout would abort a healthy call mid-thought.
    foundry_timeout_secs: int = int(os.getenv("FOUNDRY_TIMEOUT_SECS", "900"))
    youtube_api_key: str | None = os.getenv("YOUTUBE_API_KEY") or None

    # Analytics feedback loop (your OWN channel — needs a Google OAuth desktop client).

    # Instance profile — lets you run multiple windows isolated from each other (separate
    # sessions folder + log). Set via APP_PROFILE env (run.bat passes its first argument).
    # Empty = the shared default. ONLY affects file paths; nothing about model behaviour.
    app_profile: str = os.getenv("APP_PROFILE", "").strip()

    # Saved sessions — one folder, one .json per analyzed channel (save & resume later).
    # Computed in __post_init__ so the profile can suffix it (e.g. sessions_work1/).
    sessions_dir: str = os.getenv("SESSIONS_DIR", "")
    log_file: str = os.getenv("LOG_FILE", "")

    # Thumbnail Studio — image generation via a relay (default: GrsAI, its own /generate
    # endpoint). Ships inert until IMAGE_API_KEY is set. nano-banana-pro adapts best from
    # reference thumbnails; gpt-image-2.5 is cheapest. Both billed per image on GrsAI.

    # Voiceover Studio — MiniMax TTS via a relay (default: Aura Studio). Ships inert until
    # TTS_API_KEY is set. POST {base}/api/v1/tts -> {"status":"ok","audio":"<mp3 url>"};
    # voice cloning at {base}/api/v1/voice-clone -> {"voice_id", "demo_audio_url"}.
    # NOTE: use model names WITHOUT the "minimax-" prefix (e.g. speech-2.8-turbo) — the
    # prefixed form in their docs errors. Audio is on Tencent COS (slow abroad → long timeout).

    # Text providers other than Foundry (APlink, Kaggle, RELAY_*, ...) are configured in
    # beacon_engine/llm/providers.py, which reads their keys from .env itself.

    # Web search for the agent (fact verification / current info). Keyless by default
    # (DuckDuckGo HTML); set SERPER_API_KEY (serper.dev, Google results) for higher quality.
    serper_api_key: str | None = os.getenv("SERPER_API_KEY") or None

    # Faceless length rule from the reference method: words ~= spoken minutes,
    # roughly 150 wpm. Used to translate a target runtime into a word budget.

    # Studio — the shared folder where every channel and every video lives.
    # `studio_channel` is the channel folder finished work lands in. Leave it
    # empty and Beacon saves exactly where it always has, and nowhere else.

    def __post_init__(self):
        # frozen dataclass: set via object.__setattr__
        # Per-instance file isolation (file paths only — no effect on model behaviour).
        suffix = f"_{self.app_profile}" if self.app_profile else ""
        if not self.sessions_dir:
            object.__setattr__(self, "sessions_dir", f"sessions{suffix}")
        if not self.log_file:
            object.__setattr__(self, "log_file", f"beacon_engine{suffix}.log")


settings = Settings()


def vision_target(tier: Tier = Tier.HAIKU) -> tuple[str, str | None, str]:
    """Where an image-carrying call should go: (resource_root, api_key, deployment).

    Falls back field-by-field to the text deployment, so a half-configured pair still
    works: set only FOUNDRY_VISION_DEPLOYMENT when the vision model lives on the SAME
    resource, or all three when it lives on another one (its key is per-resource).
    """
    root = settings.vision_endpoint or settings.azure_foundry_endpoint
    key = (settings.vision_api_key if settings.vision_endpoint
           else None) or settings.azure_foundry_api_key
    model = settings.vision_deployment or MODELS[tier].model_id
    return root, key, model


def has_split_vision() -> bool:
    """True when images go to a different deployment than text."""
    return bool(settings.vision_deployment or settings.vision_endpoint)
