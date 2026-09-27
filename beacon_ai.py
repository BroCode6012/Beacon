"""Beacon's AI switchboard — every model Beacon can talk to, behind one small interface.

WHY THIS EXISTS
The agent used to have two hard-wired backends (GLM on Azure Foundry, Claude on AWS
Bedrock). When the Azure account was lost the agent went dark, and moving to anything
else meant editing code. Now the model is a SETTING: providers are listed in
`ai_providers.json`, their secrets live in Beacon/.env, and the Agent tab picks which
one answers. Adding a provider is a few lines of JSON; switching is one click.

FOUR KINDS COVER ALMOST EVERY AI SERVICE
Nearly every provider speaks one of four "languages", so four adapters are enough:

  openai       OpenAI-compatible /chat/completions — Azure AI Foundry, Azure OpenAI,
               OpenAI, Google Gemini (its OpenAI endpoint), Google Vertex AI, OpenRouter,
               Groq, DeepSeek, Mistral, and local Ollama / LM Studio.
  anthropic    Anthropic's Messages API — Claude with an API key.
  bedrock      AWS Bedrock Converse — any Bedrock model, any AWS account or profile.
  claude_code  The Claude Code CLI on this machine — Claude on a SUBSCRIPTION, no API
               key. It runs its own loop and reaches Beacon's tools through
               beacon_mcp.py, which calls back into this process (ToolBridge below).

ONE CONVERSATION FORMAT
The agent keeps history in a neutral shape and each adapter translates it on every call:

    {"role": "user", "text": "..."}
    {"role": "assistant", "text": "...", "tool_calls": [{"id", "name", "input"}]}
    {"role": "tool", "results": [{"id", "name", "content": "<json>", "is_error": bool}]}

So switching provider mid-conversation keeps the context. The old design had to wipe
it, because a Bedrock transcript and an OpenAI transcript are not interchangeable.
Anything a provider needs echoed back exactly (Claude's content blocks, Gemini's
thought signatures on tool calls) rides along under "wire", keyed by provider name, and
is only ever replayed to that same provider.

SECRETS never go in ai_providers.json, which can be committed. Values are written as
${NAME}: read from the environment / Beacon/.env. ${A|B} means "A, else B" and
${A:-text} supplies a plain default. Beacon/.env is re-read when it changes, so a new key
works after pressing Test — no restart.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "ai_providers.json"
CHOICE_PATH = HERE / "_ai_choice.json"      # what the Agent tab picked (per machine)
ENV_PATH = HERE / ".env"
MCP_SCRIPT = HERE / "beacon_mcp.py"
KINDS = ("openai", "anthropic", "bedrock", "claude_code")
ROLES = ("agent", "autopilot", "vision")

# Reasoning models emit their scratchpad inline before the answer. An unterminated block
# means the answer never arrived, so consume to end-of-string rather than leaving noise.
_REASONING_BLOCKS = (
    re.compile(r"<\|?channel\|?>\s*thought\b.*?(?:<\|?/?channel\|?>|\Z)", re.S | re.I),
    re.compile(r"<\s*(think|thought|reasoning)\s*>.*?(?:<\s*/\s*\1\s*>|\Z)", re.S | re.I),
)


def strip_reasoning(text: str) -> str:
    """Return just the final answer, with any inline reasoning scratchpad removed."""
    for pattern in _REASONING_BLOCKS:
        text = pattern.sub("", text or "")
    return text.strip()


# -- environment ----------------------------------------------------------------------
_env_stamp: float | None = None


def refresh_env() -> None:
    """Re-read Beacon/.env when it has changed since the last read.

    override=True for Beacon's own file only: it holds nothing but AI and AWS settings,
    and a key the operator just corrected must beat the stale copy loaded at start-up.
    """
    global _env_stamp
    try:
        stamp = ENV_PATH.stat().st_mtime
    except OSError:
        return
    if stamp == _env_stamp:
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH, override=True)
    except Exception:  # noqa: BLE001 — a missing dotenv must not stop the app
        return
    _env_stamp = stamp


_REF = re.compile(r"\$\{([^}]*)\}")


def expand(value):
    """${A|B:-default} -> the first non-empty env var, else the default, else ""."""
    if not isinstance(value, str):
        return value

    def sub(m: re.Match) -> str:
        names, _, default = m.group(1).partition(":-")
        for name in names.split("|"):
            v = (os.getenv(name.strip()) or "").strip() if name.strip() else ""
            if v:
                return v
        return default

    return _REF.sub(sub, value).strip()


def _unset_refs(value) -> list[str]:
    """.env names a value needs that are empty and have no default, e.g. a Vertex AI
    address whose project id is missing still LOOKS like a URL, so check every part."""
    missing = []
    for m in _REF.finditer(value if isinstance(value, str) else ""):
        names, has_default, _ = m.group(1).partition(":-")
        chain = [n.strip() for n in names.split("|") if n.strip()]
        if chain and not has_default and not any((os.getenv(n) or "").strip() for n in chain):
            missing.append(chain[0])
    return missing


def _first_ref(value) -> str:
    """The env var a ${...} value would read first — named in 'needs X' messages."""
    m = _REF.search(value) if isinstance(value, str) else None
    return m.group(1).partition(":-")[0].split("|")[0].strip() if m else ""


# -- provider definitions ---------------------------------------------------------------
# Always available, built from the .env names Beacon already used, so an older setup with
# no ai_providers.json keeps working exactly as before. ai_providers.json may add to these
# or override one by using the same name.
BUILTIN: dict[str, dict] = {
    "foundry": {
        "kind": "openai", "label": "Foundry",
        "notes": ("Azure AI Foundry / Azure OpenAI text model named in BEACON_FOUNDRY_MODEL "
                  "(DeepSeek V4 Flash since 2026-09-17; GLM-5.2 before)."),
        "base_url": "${BEACON_FOUNDRY_ENDPOINT|AZURE_FOUNDRY_ENDPOINT}",
        "trim_to_resource": True,
        # Foundry publishes several URLs per deployment: the resource-wide router works
        # for most, managed compute also answers on its own path. Tried in order.
        "paths": ["/openai/v1/chat/completions",
                  "/managed-deployments/{model}/v1/chat/completions"],
        "auth": "both",
        "api_key": "${BEACON_FOUNDRY_API_KEY|AZURE_FOUNDRY_API_KEY}",
        "model": "${BEACON_FOUNDRY_MODEL|FOUNDRY_DEPLOYMENT_NAME}",
        # Reasoning is billed against the answer's budget: too low and the model thinks
        # until it runs out and returns an empty string.
        "max_tokens": "${BEACON_FOUNDRY_MAX_TOKENS:-4000}",
        "timeout": "${BEACON_FOUNDRY_TIMEOUT_SECS:-300}",
        # Prepaid Azure credits: maintenance passes are free, so keep Autopilot on.
        # BEACON_FOUNDRY_AUTOPILOT=manual hands the Off/On switch back to the operator.
        "autopilot": "${BEACON_FOUNDRY_AUTOPILOT:-auto}",
        # Empty = the model's normal mode. "low" / "medium" / "high" asks a reasoning model
        # to think first (see the "Foundry · thinking" entry in ai_providers.json).
        "reasoning_effort": "${BEACON_FOUNDRY_REASONING_EFFORT}",
    },
    "foundry-vision": {
        "kind": "openai", "label": "Foundry vision", "vision": True,
        "notes": "Azure AI Foundry image model (the original Qwen3-VL setup).",
        "base_url": ("${BEACON_FOUNDRY_VISION_ENDPOINT|FOUNDRY_VISION_ENDPOINT"
                     "|BEACON_FOUNDRY_ENDPOINT|AZURE_FOUNDRY_ENDPOINT}"),
        "trim_to_resource": True,
        "paths": ["/openai/v1/chat/completions",
                  "/managed-deployments/{model}/v1/chat/completions"],
        "auth": "both",
        "api_key": ("${BEACON_FOUNDRY_VISION_API_KEY|FOUNDRY_VISION_API_KEY"
                    "|BEACON_FOUNDRY_API_KEY|AZURE_FOUNDRY_API_KEY}"),
        "model": "${BEACON_FOUNDRY_VISION_MODEL|FOUNDRY_VISION_DEPLOYMENT}",
        "max_tokens": "${BEACON_FOUNDRY_MAX_TOKENS:-4000}",
    },
    "bedrock": {
        "kind": "bedrock", "label": "Bedrock",
        "notes": "AWS Bedrock with the AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env.",
        "model": "${SIGNAL_BEDROCK_MODEL}",
        "region": "${AWS_REGION|AWS_DEFAULT_REGION:-us-east-1}",
        "max_tokens": 2000,
    },
    "claude-code": {
        "kind": "claude_code", "label": "Claude Code",
        "notes": ("Claude on your subscription through the claude CLI on this computer. "
                  "No API key; sign in once by running `claude auth login`. Set "
                  "BEACON_CLAUDE_CODE_MODEL=opus in .env for the strongest model."),
        # Always named explicitly: the CLI's own settings file can hold a model this
        # subscription cannot use, which failed with "you may not have access to it".
        "model": "${BEACON_CLAUDE_CODE_MODEL:-sonnet}",
        "timeout": 900,
    },
}

_DEFAULT_PATHS = {"openai": ("/chat/completions",)}


@dataclass
class Provider:
    name: str
    kind: str
    label: str = ""
    notes: str = ""
    model: str = ""
    base_url: str = ""
    paths: tuple = ()
    auth: str = "bearer"            # bearer | api-key | both | none
    api_key: str = ""               # the resolved secret: never shown, logged, or saved
    api_key_ref: str = ""           # where it comes from, e.g. BEACON_GEMINI_API_KEY
    api_key_command: str = ""       # e.g. "gcloud auth print-access-token" (Vertex AI)
    extra_headers: dict = field(default_factory=dict)
    region: str = ""
    aws_profile: str = ""
    max_tokens: int = 4000
    max_tokens_field: str = "max_tokens"
    temperature: float | None = 0.0
    reasoning_effort: str = ""      # openai kind: "low" | "medium" | "high"; empty = not sent
    # "auto" = keep Autopilot running while this provider is selected; "manual" = the
    # operator's Off/On stands. Azure credits are prepaid, so background maintenance there
    # costs nothing extra; a Claude subscription has usage limits, so it stays manual.
    autopilot: str = "manual"
    timeout: int = 300
    vision: bool = False
    command: str = "claude"
    use_subscription: bool = True   # claude_code: ignore ANTHROPIC_API_KEY in the env
    problem: str = ""               # a definition error found while loading
    unset: dict = field(default_factory=dict)  # setting -> .env names it needs but lacks

    @property
    def title(self) -> str:
        return self.label or self.name


def _resource_root(url: str) -> str:
    """Foundry hands out several URL shapes per deployment; keep only the resource root."""
    url = (url or "").strip().rstrip("/")
    for marker in ("/openai", "/managed-deployments", "/models"):
        cut = url.find(marker)
        if cut > 0:
            return url[:cut]
    return url


def _int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _build(name: str, raw: dict) -> Provider:
    kind = str(raw.get("kind") or "").strip().lower()
    p = Provider(name=name, kind=kind)
    if kind not in KINDS:
        p.problem = f"unknown kind '{kind}' (use one of: {', '.join(KINDS)})"
        return p
    p.unset = {k: _unset_refs(raw.get(k)) for k in ("base_url", "model", "region",
                                                    "aws_profile") if _unset_refs(raw.get(k))}
    p.label = str(raw.get("label") or "")
    p.notes = str(raw.get("notes") or raw.get("_help") or "")
    p.model = expand(raw.get("model") or "")
    base = expand(raw.get("base_url") or "")
    p.base_url = _resource_root(base) if raw.get("trim_to_resource") else base.rstrip("/")
    p.paths = tuple(raw.get("paths") or _DEFAULT_PATHS.get(kind, ()))
    p.auth = str(raw.get("auth") or "bearer").lower()
    key_template = raw.get("api_key") or ""
    if key_template and not _first_ref(key_template):
        # A literal key in a file that can be committed is how keys leak. Refuse it and
        # say where it belongs instead of quietly using it.
        p.problem = ("the api_key is written into ai_providers.json — move it to "
                     "Beacon/.env and write ${YOUR_KEY_NAME} here instead")
    else:
        p.api_key = expand(key_template)
        p.api_key_ref = _first_ref(key_template)
    p.api_key_command = str(raw.get("api_key_command") or "")
    p.extra_headers = {k: expand(v) for k, v in (raw.get("extra_headers") or {}).items()}
    p.region = expand(raw.get("region") or "")
    p.aws_profile = expand(raw.get("aws_profile") or "")
    p.max_tokens = _int(expand(raw.get("max_tokens", 4000)), 4000)
    p.max_tokens_field = str(raw.get("max_tokens_field") or "max_tokens")
    temp = raw.get("temperature", 0.0)
    p.temperature = None if temp is None else float(temp)
    p.reasoning_effort = expand(raw.get("reasoning_effort") or "").lower()
    p.autopilot = (expand(raw.get("autopilot") or "manual").lower() or "manual")
    p.timeout = _int(expand(raw.get("timeout", 300)), 300)
    p.vision = bool(raw.get("vision", False))
    p.command = str(raw.get("command") or "claude")
    p.use_subscription = bool(raw.get("use_subscription", True))
    return p


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        return {"_error": f"{path.name} is not valid JSON: {exc}"}
    return data if isinstance(data, dict) else {}


def load(config_path: Path | None = None) -> tuple[dict[str, Provider], dict[str, str]]:
    """(providers by name, the file's "use" section). Cheap enough to call per render."""
    refresh_env()
    data = _read_json(config_path or CONFIG_PATH)
    defs = {**BUILTIN, **{k: v for k, v in (data.get("providers") or {}).items()
                          if isinstance(v, dict) and not k.startswith("_")}}
    providers = {name: _build(name, raw) for name, raw in defs.items()}
    use = {k: str(v) for k, v in (data.get("use") or {}).items() if v}
    return providers, use


def save_choice(role: str, name: str, path: Path | None = None) -> None:
    """Remember which provider answers a role (what the Agent tab's picker writes)."""
    path = path or CHOICE_PATH
    data = _read_json(path)
    data.pop("_error", None)
    data[role] = name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def choose(role: str = "agent", *, providers: dict | None = None,
           use: dict | None = None, choice_path: Path | None = None) -> str:
    """Which provider answers a role. Most recent explicit decision wins:

    the Agent tab's pick -> BEACON_AI_<ROLE> env -> ai_providers.json "use" ->
    (autopilot follows the agent) -> the old BEACON_AGENT_PROVIDER -> the defaults.
    """
    if providers is None or use is None:
        providers, use = load()
    picked = _read_json(choice_path or CHOICE_PATH)
    for candidate in (picked.get(role), os.getenv(f"BEACON_AI_{role.upper()}"),
                      use.get(role)):
        if candidate and str(candidate).strip() in providers:
            return str(candidate).strip()
    if role == "vision":
        return "foundry-vision"
    if role == "autopilot":
        return choose("agent", providers=providers, use=use, choice_path=choice_path)
    legacy = (os.getenv("BEACON_AGENT_PROVIDER") or "").strip().lower()
    return legacy if legacy in providers else "foundry"


def autopilot_provider(providers: dict | None = None, use: dict | None = None,
                       choice_path: Path | None = None) -> Provider:
    """The provider Autopilot would run on (its own slot, or the agent's)."""
    if providers is None or use is None:
        providers, use = load()
    name = choose("autopilot", providers=providers, use=use, choice_path=choice_path)
    return get_provider(name, providers)


def get_provider(name: str, providers: dict | None = None) -> Provider:
    if providers is None:
        providers, _ = load()
    p = providers.get(name)
    if p is None:
        return Provider(name=name, kind="", problem=f"no provider named '{name}' — add it "
                                                    "to ai_providers.json")
    return p


def check(p: Provider, *, injected_client: bool = False) -> tuple[bool, str]:
    """(ok, reason) — is this provider fully set up? Says exactly what is missing."""
    if p.problem:
        return False, p.problem

    def needs(what: str, ref: str = "") -> str:
        ref = ref or (p.unset.get(what) or [""])[0]
        return (f"{p.title} needs {ref} in Beacon/.env" if ref
                else f"{p.title} needs a {what} in ai_providers.json")

    if p.kind == "claude_code":
        if not shutil.which(p.command):
            return False, (f"the '{p.command}' command is not installed on this computer "
                           "(install Claude Code, then sign in once)")
        return True, "ready"
    if p.kind == "bedrock":
        if not p.model or p.unset.get("model"):
            return False, needs("model")
        creds = (injected_client or p.aws_profile or os.getenv("AWS_PROFILE")
                 or (os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")))
        if not creds:
            return False, f"{p.title} needs AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY in Beacon/.env"
        return True, "ready"
    if p.kind == "openai" and (not p.base_url or p.unset.get("base_url")):
        return False, needs("base_url")
    if p.auth != "none" and not (p.api_key or p.api_key_command):
        return False, needs("api_key", p.api_key_ref)
    if not p.model or p.unset.get("model"):
        return False, needs("model")
    return True, "ready"


# -- HTTP ---------------------------------------------------------------------------------
class ProviderError(RuntimeError):
    """A provider answered with an error — message is safe to show (no secrets)."""


_WORKING_URL: dict[tuple, str] = {}
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


CONNECT_TRIES = 3
_connect_sleep = time.sleep      # swapped out by tests
_NO_SUCH_HOST = ("nameresolutionerror", "failed to resolve", "getaddrinfo failed",
                 "name or service not known", "nodename nor servname")


def _connection_problem(host: str, exc: Exception) -> str:
    """Plain words for a failed connection. The raw text ("NameResolutionError ...
    getaddrinfo failed") is what the Test button used to show when an endpoint had a typo."""
    if any(marker in str(exc).lower() for marker in _NO_SUCH_HOST):
        # Said carefully: a wrong endpoint and a brief Wi-Fi/DNS blip look identical here.
        return (f"could not find the address {host} — check the endpoint in Beacon/.env "
                "(Azure portal: the resource's Keys and Endpoint page), or your internet "
                "connection")
    return f"could not connect to {host}: {str(exc)[:200]}"


def post_json(body: dict, *, urls: list[str], headers: dict, timeout: int) -> dict:
    """POST to the first URL that exists (404 falls through), remembering which answered."""
    import requests
    key = tuple(urls)
    order = ([_WORKING_URL[key]] + [u for u in urls if u != _WORKING_URL[key]]
             if key in _WORKING_URL else list(urls))
    for i, url in enumerate(order):
        host = url.split("/")[2] if "//" in url else url
        for attempt in range(1, CONNECT_TRIES + 1):
            try:
                r = requests.post(url, json=body, headers=headers, timeout=timeout)
                break
            except requests.exceptions.ConnectionError as exc:
                # A momentary network or DNS blip killed a whole agent turn on 2026-09-17,
                # between two successful calls. Retrying is safe: a chat request has no side
                # effects on the provider, and Beacon's tools run locally, not there.
                if attempt == CONNECT_TRIES:
                    raise ProviderError(_connection_problem(host, exc)) from exc
                _connect_sleep(attempt * 1.5)
            except requests.exceptions.Timeout as exc:
                raise ProviderError(f"{host} did not answer within {timeout} seconds") from exc
        if r.status_code == 404 and i < len(order) - 1:
            continue
        if r.status_code >= 400:
            raise ProviderError(f"HTTP {r.status_code} from {host}: {r.text[:400]}")
        _WORKING_URL[key] = url
        return r.json()
    raise ProviderError("no endpoint answered")


def _command_token(command: str, ttl: int = 3000) -> str:
    """Run a token command (e.g. gcloud) and cache its answer for most of an hour."""
    cached = _TOKEN_CACHE.get(command)
    if cached and time.time() - cached[1] < ttl:
        return cached[0]
    out = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    token = (out.stdout or "").strip().splitlines()[-1:] or [""]
    if out.returncode != 0 or not token[0]:
        raise ProviderError(f"api_key_command failed: {(out.stderr or '').strip()[:200]}")
    _TOKEN_CACHE[command] = (token[0], time.time())
    return token[0]


def _secret(p: Provider) -> str:
    return p.api_key or (_command_token(p.api_key_command) if p.api_key_command else "")


Post = Callable[..., dict]


# -- adapters -----------------------------------------------------------------------------
class OpenAIChat:
    """Any OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, p: Provider, post: Post | None = None) -> None:
        self.p = p
        self.post = post or post_json

    def _urls(self) -> list[str]:
        return [self.p.base_url + path.format(model=self.p.model) for path in self.p.paths]

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", **self.p.extra_headers}
        if self.p.auth != "none":
            key = _secret(self.p)
            if self.p.auth in ("bearer", "both"):
                h["Authorization"] = f"Bearer {key}"
            if self.p.auth in ("api-key", "both"):
                h["api-key"] = key
        return h

    def _wire(self, history: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in history:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                turn: dict = {"role": "assistant", "content": m.get("text") or None}
                calls = []
                for c in m.get("tool_calls") or []:
                    raw = (c.get("wire") or {}).get(self.p.name)
                    calls.append(raw or {"id": c["id"], "type": "function", "function": {
                        "name": c["name"], "arguments": json.dumps(c.get("input") or {})}})
                if calls:
                    turn["tool_calls"] = calls
                out.append(turn)
            elif m["role"] == "tool":
                out.extend({"role": "tool", "tool_call_id": r["id"], "content": r["content"]}
                           for r in m["results"])
        return out

    def _send(self, body: dict) -> dict:
        body[self.p.max_tokens_field] = self.p.max_tokens
        if self.p.temperature is not None:
            body["temperature"] = self.p.temperature
        if self.p.reasoning_effort:
            # Only sent when asked for: models without a thinking mode reject the field.
            body["reasoning_effort"] = self.p.reasoning_effort
        return self.post(body, urls=self._urls(), headers=self._headers(),
                         timeout=self.p.timeout)

    @staticmethod
    def _text(content) -> str:
        if isinstance(content, list):          # some servers return content parts
            content = "".join(part.get("text", "") for part in content
                              if isinstance(part, dict))
        return strip_reasoning(content or "")

    def chat(self, system: str, history: list[dict], tools: list[dict]) -> dict:
        body: dict = {"model": self.p.model,
                      "messages": [{"role": "system", "content": system}] + self._wire(history)}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["schema"]}} for t in tools]
            body["tool_choice"] = "auto"
        data = self._send(body)
        msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function") or {}
            # `arguments` is a JSON *string*, and a model can emit "" or malformed JSON.
            # Treat anything unparsable as "no arguments" rather than failing the turn —
            # the tool's own validation is the real gate.
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            cid = c.get("id") or f"call_{i + 1}"
            calls.append({"id": cid, "name": fn.get("name") or "",
                          "input": args if isinstance(args, dict) else {},
                          "wire": {self.p.name: {**c, "id": cid}}})
        return {"text": self._text(msg.get("content")), "tool_calls": calls}

    def describe_image(self, prompt: str, image_bytes: bytes, media_type: str) -> str:
        import base64
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data = self._send({"model": self.p.model, "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
        ]}]})
        msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        return self._text(msg.get("content"))


def _merge(out: list[dict], role: str, blocks: list) -> None:
    """Append blocks, folding consecutive same-role turns (both APIs demand alternation)."""
    if out and out[-1]["role"] == role:
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": list(blocks)})


class AnthropicChat:
    """Claude through Anthropic's Messages API with an API key."""

    VERSION = "2023-06-01"

    def __init__(self, p: Provider, post: Post | None = None) -> None:
        self.p = p
        self.post = post or post_json

    def _wire(self, history: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in history:
            if m["role"] == "user":
                _merge(out, "user", [{"type": "text", "text": m["text"]}])
            elif m["role"] == "assistant":
                raw = (m.get("wire") or {}).get(self.p.name)
                blocks = raw or (
                    ([{"type": "text", "text": m["text"]}] if m.get("text") else [])
                    + [{"type": "tool_use", "id": c["id"], "name": c["name"],
                        "input": c.get("input") or {}} for c in m.get("tool_calls") or []])
                _merge(out, "assistant", blocks or [{"type": "text", "text": "(no reply)"}])
            elif m["role"] == "tool":
                _merge(out, "user", [{"type": "tool_result", "tool_use_id": r["id"],
                                      "content": r["content"], "is_error": r["is_error"]}
                                     for r in m["results"]])
        return out

    def _send(self, body: dict) -> dict:
        body.setdefault("max_tokens", self.p.max_tokens)
        if self.p.temperature is not None:
            body["temperature"] = self.p.temperature
        url = (self.p.base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        headers = {"x-api-key": _secret(self.p), "anthropic-version": self.VERSION,
                   "content-type": "application/json", **self.p.extra_headers}
        return self.post(body, urls=[url], headers=headers, timeout=self.p.timeout)

    def chat(self, system: str, history: list[dict], tools: list[dict]) -> dict:
        body: dict = {"model": self.p.model, "system": system, "messages": self._wire(history)}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"],
                              "input_schema": t["schema"]} for t in tools]
        data = self._send(body)
        blocks = data.get("content") or []
        return {
            "text": "\n".join(b.get("text", "") for b in blocks
                              if b.get("type") == "text").strip(),
            "tool_calls": [{"id": b["id"], "name": b["name"], "input": b.get("input") or {}}
                           for b in blocks if b.get("type") == "tool_use"],
            "wire": {self.p.name: blocks},
        }

    def describe_image(self, prompt: str, image_bytes: bytes, media_type: str) -> str:
        import base64
        data = self._send({"model": self.p.model, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(image_bytes).decode()}},
            {"type": "text", "text": prompt},
        ]}]})
        return "\n".join(b.get("text", "") for b in data.get("content") or []
                         if b.get("type") == "text").strip()


class BedrockChat:
    """AWS Bedrock Converse — works for every Bedrock model, in any account or profile."""

    def __init__(self, p: Provider, client: Callable[[], object] | None = None) -> None:
        self.p = p
        self._injected = client
        self._client = None

    def client(self):
        injected = self._injected() if self._injected else None
        if injected is not None:
            return injected
        if self._client is None:
            import boto3
            session = (boto3.Session(profile_name=self.p.aws_profile) if self.p.aws_profile
                       else boto3.Session())
            self._client = session.client("bedrock-runtime",
                                          region_name=self.p.region or "us-east-1")
        return self._client

    def _wire(self, history: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in history:
            if m["role"] == "user":
                _merge(out, "user", [{"text": m["text"]}])
            elif m["role"] == "assistant":
                raw = (m.get("wire") or {}).get(self.p.name)
                # Bedrock rejects blank text blocks, so an empty reply sends none.
                blocks = raw or (
                    ([{"text": m["text"]}] if m.get("text") else [])
                    + [{"toolUse": {"toolUseId": c["id"], "name": c["name"],
                                    "input": c.get("input") or {}}}
                       for c in m.get("tool_calls") or []])
                _merge(out, "assistant", blocks or [{"text": "(no reply)"}])
            elif m["role"] == "tool":
                _merge(out, "user", [{"toolResult": {
                    "toolUseId": r["id"], "content": [{"text": r["content"]}],
                    "status": "error" if r["is_error"] else "success"}}
                    for r in m["results"]])
        return out

    def _inference(self) -> dict:
        cfg: dict = {"maxTokens": self.p.max_tokens}
        if self.p.temperature is not None:
            cfg["temperature"] = self.p.temperature
        return cfg

    def chat(self, system: str, history: list[dict], tools: list[dict]) -> dict:
        kwargs: dict = {"modelId": self.p.model, "system": [{"text": system}],
                        "messages": self._wire(history), "inferenceConfig": self._inference()}
        if tools:
            kwargs["toolConfig"] = {"tools": [{"toolSpec": {
                "name": t["name"], "description": t["description"],
                "inputSchema": {"json": t["schema"]}}} for t in tools]}
        resp = self.client().converse(**kwargs)
        blocks = (resp.get("output", {}).get("message", {}) or {}).get("content") or []
        calls = [{"id": b["toolUse"]["toolUseId"], "name": b["toolUse"]["name"],
                  "input": b["toolUse"].get("input") or {}}
                 for b in blocks if "toolUse" in b]
        return {
            "text": "\n".join(b["text"].strip() for b in blocks
                              if b.get("text", "").strip()),
            # A tool request only counts when Bedrock says the turn stopped for it.
            "tool_calls": calls if resp.get("stopReason") == "tool_use" else [],
            "wire": {self.p.name: blocks},
        }

    def describe_image(self, prompt: str, image_bytes: bytes, media_type: str) -> str:
        fmt = media_type.split("/")[-1].replace("jpg", "jpeg")
        resp = self.client().converse(
            modelId=self.p.model, inferenceConfig=self._inference(),
            messages=[{"role": "user", "content": [
                {"image": {"format": fmt, "source": {"bytes": image_bytes}}},
                {"text": prompt}]}])
        blocks = (resp.get("output", {}).get("message", {}) or {}).get("content") or []
        return "\n".join(b["text"] for b in blocks if b.get("text"))


def make_chat(p: Provider, *, post: Post | None = None,
              bedrock_client: Callable[[], object] | None = None):
    if p.kind == "openai":
        return OpenAIChat(p, post)
    if p.kind == "anthropic":
        return AnthropicChat(p, post)
    if p.kind == "bedrock":
        return BedrockChat(p, bedrock_client)
    raise ProviderError(f"{p.title} ({p.kind}) cannot be used as a chat model here")


# -- Claude Code (subscription) ---------------------------------------------------------
class ToolBridge:
    """A private localhost door through which beacon_mcp.py runs tools INSIDE Beacon.

    The Claude Code CLI is a separate process with its own agent loop. Rather than give
    it a second copy of Beacon (a second finder client, no app to switch views, no cache
    invalidation, no Autopilot restrictions), every tool call comes back here and runs
    through the same dispatcher as every other provider. Bound to 127.0.0.1, guarded by
    a random per-turn token, and closed as soon as the turn ends.
    """

    def __init__(self, list_tools: Callable[[], list[dict]],
                 call_tool: Callable[[str, dict], tuple[object, bool]]) -> None:
        self.token = secrets.token_urlsafe(32)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def _send(self, code: int, obj) -> None:
                data = json.dumps(obj, default=str).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                given = self.headers.get("Authorization", "")
                return hmac.compare_digest(given.encode(), f"Bearer {bridge.token}".encode())

            def do_GET(self) -> None:  # noqa: N802 — http.server naming
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                if self.path != "/tools":
                    return self._send(404, {"error": "not found"})
                self._send(200, {"tools": list_tools()})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                if self.path != "/call":
                    return self._send(404, {"error": "not found"})
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    req = json.loads(self.rfile.read(length) or b"{}")
                    output, is_error = call_tool(str(req.get("name") or ""),
                                                 req.get("arguments") or {})
                except Exception as exc:  # noqa: BLE001 — reported to the model as a result
                    output, is_error = {"error": f"{type(exc).__name__}: {exc}"}, True
                self._send(200, {"output": output, "is_error": bool(is_error)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _child_python() -> str:
    """An interpreter with a console, for the MCP server Claude Code launches.

    pythonw has no usable stdio, and stdio IS the MCP transport. In bridge mode the
    server only needs the standard library, so any interpreter will do.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


_NOT_SIGNED_IN = ("not logged in", "please run /login", "invalid api key", "authentication",
                  "oauth", "credit balance")


class ClaudeCodeRunner:
    """One agent turn through `claude -p`, keeping the CLI's session between turns."""

    # A private, otherwise-empty working folder: the CLI must not pick up this project's
    # CLAUDE.md or settings, which would cost tokens and change its behaviour. It is kept
    # rather than deleted per turn: on Windows the CLI can still hold the folder for a
    # moment after it exits, and Python 3.10's temporary-folder cleanup crashes on that.
    WORK_DIR = Path(tempfile.gettempdir()) / "beacon-claude-code"

    def __init__(self, p: Provider) -> None:
        self.p = p
        self.session_id: str | None = None

    # Settings a person deliberately sets for the CLI; everything else CLAUDE* is session
    # plumbing from whatever launched Beacon.
    _KEEP_ENV = {"CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CONFIG_DIR"}

    def _env(self) -> dict:
        # If Beacon was started from inside another Claude session (a terminal in an IDE,
        # the desktop app), that session's variables are inherited: they mark the CLI as a
        # child session and point it at the parent's private gateway, where it is "not
        # logged in". Strip them so it uses this computer's own sign-in.
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith("CLAUDE") or k.upper() in self._KEEP_ENV}
        if self.p.use_subscription:
            # An API key, token or custom endpoint silently takes the CLI off the
            # subscription (pay-per-token, or someone else's gateway).
            env = {k: v for k, v in env.items() if not k.upper().startswith("ANTHROPIC_")}
        env["MCP_TOOL_TIMEOUT"] = str(self.p.timeout * 1000)
        env["MCP_TIMEOUT"] = "60000"
        return env

    @staticmethod
    def _transcript(history: list[dict]) -> str:
        """Earlier turns from another provider, so a switch keeps the thread."""
        lines = [f"{'Operator' if m['role'] == 'user' else 'Beacon'}: {m.get('text')}"
                 for m in history[-12:] if m["role"] in ("user", "assistant") and m.get("text")]
        if not lines:
            return ""
        return ("Earlier in this conversation (answered by another model):\n"
                + "\n".join(lines) + "\n\nNow the operator asks:\n")

    def _command(self, system_file: Path, mcp_file: Path | None) -> list[str]:
        exe = shutil.which(self.p.command) or self.p.command
        args = [exe, "-p", "--output-format", "json",
                "--system-prompt-file", str(system_file),
                # No built-in tools (files, shell, web): Beacon's own tools only.
                "--tools", "",
                # Beacon's turn must not inherit this computer's CLI settings — model,
                # hooks, status line, permissions. The work folder has no settings of its
                # own, so this leaves the CLI with exactly what Beacon passes.
                "--setting-sources", "project"]
        if mcp_file is not None:
            args += ["--mcp-config", str(mcp_file), "--strict-mcp-config",
                     "--allowedTools", "mcp__beacon"]
        if self.p.model:
            args += ["--model", self.p.model]
        if self.session_id:
            args += ["--resume", self.session_id]
        return args

    def run_turn(self, *, system: str, user_text: str, history: list[dict],
                 list_tools: Callable[[], list[dict]] | None = None,
                 call_tool: Callable[[str, dict], tuple[object, bool]] | None = None) -> str:
        bridge = ToolBridge(list_tools, call_tool) if list_tools and call_tool else None
        prompt = user_text if self.session_id else self._transcript(history) + user_text
        self.WORK_DIR.mkdir(parents=True, exist_ok=True)
        run_id = secrets.token_hex(6)       # own files per run: agent + Autopilot can overlap
        system_file = self.WORK_DIR / f"system-{run_id}.txt"
        mcp_file = self.WORK_DIR / f"mcp-{run_id}.json" if bridge is not None else None
        try:
            system_file.write_text(system, encoding="utf-8")
            if mcp_file is not None:
                mcp_file.write_text(json.dumps({"mcpServers": {"beacon": {
                    "command": _child_python(), "args": [str(MCP_SCRIPT)],
                    "env": {"BEACON_TOOL_BRIDGE": bridge.url,
                            "BEACON_TOOL_BRIDGE_TOKEN": bridge.token},
                }}}), encoding="utf-8")
            for attempt in (1, 2):
                proc = subprocess.run(
                    self._command(system_file, mcp_file), input=prompt,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    cwd=self.WORK_DIR, env=self._env(), timeout=self.p.timeout,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                result = self._parse(proc)
                if (result is None and self.session_id and attempt == 1
                        and "no conversation found" in (proc.stderr + proc.stdout).lower()):
                    self.session_id = None          # the CLI forgot the session; start over
                    prompt = self._transcript(history) + user_text
                    continue
                break
        finally:
            if bridge is not None:
                bridge.close()
            for leftover in (system_file, mcp_file):
                if leftover is not None:
                    try:
                        leftover.unlink()
                    except OSError:
                        pass            # still held for a moment; the next run overwrites nothing
        if result is None:
            detail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise ProviderError(self._explain(detail) or f"exit code {proc.returncode}")
        if result.get("is_error"):
            raise ProviderError(self._explain(str(result.get("result") or ""))
                                or str(result.get("subtype") or "failed"))
        self.session_id = result.get("session_id") or self.session_id
        return strip_reasoning(str(result.get("result") or ""))

    @staticmethod
    def _parse(proc: subprocess.CompletedProcess) -> dict | None:
        for line in reversed((proc.stdout or "").strip().splitlines()):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("type") == "result":
                return data
        return None

    @staticmethod
    def _explain(detail: str) -> str:
        if any(marker in detail.lower() for marker in _NOT_SIGNED_IN):
            return ("Claude Code is not signed in on this computer. Open a terminal, run "
                    "`claude`, sign in with your subscription, then press Test again. "
                    f"({detail[:160]})")
        return detail


# -- quick connection test ----------------------------------------------------------------
def try_provider(p: Provider) -> tuple[bool, str]:
    """Send one tiny message; (ok, what happened) in words fit for the Agent tab."""
    ok, why = check(p)
    if not ok:
        return False, why
    started = time.time()
    ask = "Reply with exactly the word READY and nothing else."
    try:
        if p.kind == "claude_code":
            text = ClaudeCodeRunner(p).run_turn(system="You are a connection test.",
                                                user_text=ask, history=[])
        else:
            text = make_chat(p).chat("You are a connection test.",
                                     [{"role": "user", "text": ask}], [])["text"]
    except Exception as exc:  # noqa: BLE001 — the whole point is to report it
        return False, f"{p.title} did not answer: {type(exc).__name__}: {str(exc)[:300]}"
    secs = time.time() - started
    if not text.strip():
        return False, f"{p.title} answered with an empty reply after {secs:.1f}s"
    return True, f"{p.title} answered in {secs:.1f}s: “{text.strip()[:60]}”"


def fingerprint(p: Provider) -> str:
    """Stable identity of a provider's settings, so a changed setting rebuilds its client."""
    raw = json.dumps(dataclasses.asdict(p), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
