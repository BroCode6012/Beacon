"""Every AI provider Beacon can talk to, in ONE list.

To add a provider: add a ``Provider(...)`` to ``PROVIDERS`` below and put its key in
.env. That is the whole job — the engine menu, the startup health check, the cost meter,
image routing and fallbacks all read this list; nothing else needs editing.

Two kinds of provider:
  "openai"   any OpenAI-compatible ``/chat/completions`` endpoint — relays, gateways,
             Kaggle's model proxy. Fully described by the fields below.
  "foundry"  the Microsoft Foundry managed-compute deployment. It keeps its own tiered
             budgets, reasoning switch and text/vision split in beacon_engine/config.py.

Keys that expire (Kaggle mints a new one every ~2 hours) are handled by a ``login``
object instead of a fixed key: it hands out a cached key and renews it before expiry.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import requests

from beacon_engine.config import MODELS, RELAY_PRICING, Tier, deployment_display_name, settings
from beacon_engine.logging_util import get_logger

log = get_logger()


@dataclass(frozen=True)
class Credentials:
    """Where to send a request and the key to send with it."""

    base_url: str        # everything before /chat/completions, e.g. https://host/v1
    api_key: str


class Login(Protocol):
    """A provider whose key is minted on demand rather than pasted into .env."""

    def configured(self) -> bool: ...
    def credentials(self) -> Credentials: ...
    def invalidate(self) -> None: ...


@dataclass(frozen=True)
class Provider:
    id: str                                   # stable; saved as the user's engine pick
    label: str                                # what the engine menu shows
    kind: str = "openai"                      # "openai" | "foundry"
    base_url: str = ""                        # fixed base URL, or...
    base_url_env: str = ""                    # ...the .env name that holds it
    key_env: tuple[str, ...] = ()             # .env names tried in order for the key
    login: Login | None = None                # instead of a key: mints one (Kaggle)
    model: str = ""                           # model id sent with every request...
    tier_models: dict = field(default_factory=dict)   # ...unless a tier overrides it
    task_models: dict[str, str] = field(default_factory=dict)
    vision: bool = True                       # can it read images (thumbnails)?
    price_in: float = 0.0                     # $ per 1M input tokens, for the cost meter
    price_out: float = 0.0                    # $ per 1M output tokens
    max_output_tokens: int = 32_000           # requests are capped here
    # Requests are raised to at least this. A model that thinks before answering spends
    # that thinking out of the SAME budget, so a small ask comes back cut off or empty.
    min_output_tokens: int = 0
    retries: int = 1                          # attempts before giving up / falling back
    timeout: int = 300                        # seconds per attempt
    fallback: tuple[str, ...] = ()            # provider ids tried, in order, if this fails

    def model_for(self, tier: Tier, *, task: str | None = None, images: bool = False) -> str:
        if self.task_models:
            # Images always use a vision-capable combo, even in a normally text task.
            if images:
                return self.task_models["vision"]
            if task is not None:
                if task not in self.task_models:
                    raise ValueError(f"Unknown {self.id} task: {task}")
                return self.task_models[task]
        return self.tier_models.get(tier) or self.model

    def configured(self) -> bool:
        """Is it set up at all (keys present)? Says nothing about whether it answers."""
        if self.kind == "foundry":
            return bool(settings.azure_foundry_endpoint and settings.azure_foundry_api_key)
        if self.login is not None:
            return self.login.configured()
        return bool(self._base_url() and self._key())

    def credentials(self) -> Credentials:
        if self.login is not None:
            return self.login.credentials()
        return Credentials(self._base_url(), self._key())

    def _base_url(self) -> str:
        return (self.base_url or os.getenv(self.base_url_env, "")).strip().rstrip("/")

    def _key(self) -> str:
        return next((v for name in self.key_env if (v := os.getenv(name, "").strip())), "")


class KaggleModelProxy:
    """Kaggle's free model credit ($10/day, $100/month) — the key behind `kaggle benchmarks auth`.

    The account token (KGAT_..., from kaggle.com/settings → API Tokens) lives in
    KAGGLE_API_TOKEN or ~/.kaggle/access_token, where Kaggle's own CLI keeps it. From it
    we mint a model key that lasts ~2 hours, cache it, and mint again 5 minutes before
    it runs out. Kaggle intends this credit for building benchmarks; see the provider
    entry below before relying on it for production work.
    """

    API = "https://api.kaggle.com/v1/models.ModelProxyApiService/"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: Credentials | None = None
        self._expires_at = 0.0

    @staticmethod
    def account_token() -> str:
        token = os.getenv("KAGGLE_API_TOKEN", "").strip()
        if token:
            return token
        path = Path.home() / ".kaggle" / "access_token"
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def configured(self) -> bool:
        return bool(self.account_token())

    def invalidate(self) -> None:
        with self._lock:
            self._cached, self._expires_at = None, 0.0

    def credentials(self) -> Credentials:
        with self._lock:
            if self._cached and time.time() < self._expires_at - 300:
                return self._cached
            data = self._call("CreateDefaultModelProxyToken")
            base = (data.get("baseUri") or "").rstrip("/")
            self._cached = Credentials(f"{base}/openapi", data.get("token", ""))
            self._expires_at = _parse_expiry(data.get("expiryTime")) or time.time() + 3600
            log.info("kaggle: minted a model key valid until %s",
                     datetime.fromtimestamp(self._expires_at).strftime("%H:%M"))
            return self._cached

    def quota(self) -> list[dict]:
        """Today's and this month's credit: [{period, used, allowed, refills_at}]."""
        return [{"period": q.get("refillPeriod", "").lower(),
                 "used": float(q.get("quotaUsed") or 0.0),
                 "allowed": float(q.get("totalQuotaAllowed") or 0.0),
                 "refills_at": q.get("refillTime", "")}
                for q in self._call("GetModelProxyQuotas").get("quotaBalances") or []]

    def _call(self, method: str) -> dict:
        token = self.account_token()
        if not token:
            raise RuntimeError("no Kaggle account token (KAGGLE_API_TOKEN or "
                               "~/.kaggle/access_token)")
        r = requests.post(self.API + method, json={}, timeout=30,
                          headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 403:
            raise RuntimeError("Kaggle refused the account token (403) — it was revoked, "
                               "or the account needs phone/identity verification")
        r.raise_for_status()
        return r.json()


def _parse_expiry(stamp: str | None) -> float:
    """Kaggle sends nanosecond ISO stamps ('...02.418037600Z') Python 3.10 cannot parse."""
    if not stamp:
        return 0.0
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?", stamp)
    if not m:
        return 0.0
    when = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
    return when.timestamp() + float(f"0.{m.group(2) or 0}")


KAGGLE = KaggleModelProxy()

# The menu lists these in this order, and the startup check falls back down it. A
# provider without its keys simply does not appear.
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="aplink",
        label="APlink — Claude Opus 4.8",
        base_url_env="APLINK_BASE_URL",
        key_env=("APLINK_API_KEY",),
        model="claude-opus-4-8",
        price_in=RELAY_PRICING[Tier.OPUS][0],
        price_out=RELAY_PRICING[Tier.OPUS][1],
        retries=3,                 # real Opus via a reverse proxy: 500s when busy
        timeout=150,
        fallback=("foundry",),
    ),
    Provider(
        # Free, but Kaggle meant it for building benchmarks, not running apps: the
        # account could be cut off. Kaggle reserves each call's worst-case cost up front,
        # so the output cap is kept modest to leave room in the $10/day. Sonnet 5 thinks
        # by default and bills that thinking as output: a 3,000-token fact sheet came
        # back with ~150 visible tokens (2026-10-02), hence the floor.
        id="kaggle",
        label="Kaggle — Claude Sonnet 5 (free $10/day)",
        login=KAGGLE,
        model="anthropic/claude-sonnet-5@default",
        price_in=2.0,
        price_out=10.0,
        max_output_tokens=16_000,
        min_output_tokens=8_000,
        retries=2,
    ),
    Provider(
        id="foundry",
        label=f"Foundry — {deployment_display_name(MODELS[Tier.SONNET].model_id)}",
        kind="foundry",
    ),
    Provider(
        # The original single relay (RELAY_* in .env). Hidden unless configured.
        # NOTE: on iiitoken 'claude-opus-4-6' is FAKE (substitutes Sonnet); opus-4-8 is real.
        id="relay",
        label="Relay — RELAY_* in .env",
        base_url_env="RELAY_BASE_URL",
        key_env=("RELAY_API_KEY",),
        model=os.getenv("RELAY_MODEL_SONNET", "claude-sonnet-4-6"),
        tier_models={Tier.OPUS: os.getenv("RELAY_MODEL_OPUS", "claude-opus-4-8"),
                     Tier.HAIKU: os.getenv("RELAY_MODEL_HAIKU", "claude-haiku-4-5")},
        price_in=RELAY_PRICING[Tier.SONNET][0],
        price_out=RELAY_PRICING[Tier.SONNET][1],
        retries=int(os.getenv("RELAY_RETRIES", "3")),
        timeout=150,
        fallback=("foundry",),
    ),
    Provider(
        id="hub",
        label="Hub — task combos (automatic failover)",
        base_url_env="HUB_BASE_URL",
        key_env=("HUB_API_KEY",),
        model="quill-research",
        tier_models={Tier.OPUS: "quill-review", Tier.HAIKU: "quill-fast"},
        task_models={task: f"quill-{task}" for task in (
            "analysis", "ideas", "strategy", "write", "review", "research",
            "packaging", "vision", "fast",
        )},
        max_output_tokens=16_000,
        min_output_tokens=8_000,
        # Hub already retries accounts and providers. No second client retry chain.
        retries=1,
        # Default Hub policy can try six accounts for up to 120s each.
        timeout=900,
    ),
)

_BY_ID = {p.id: p for p in PROVIDERS}


def get(provider_id: str | None) -> Provider | None:
    return _BY_ID.get(provider_id or "")


def available() -> list[Provider]:
    """Providers whose keys are present, in menu order."""
    return [p for p in PROVIDERS if p.configured()]


def default_id() -> str:
    """QUILL_PROVIDER from .env if it is set up, else the first provider that is."""
    wanted = os.getenv("QUILL_PROVIDER", "").strip().lower()
    if (p := get(wanted)) and p.configured():
        return p.id
    return next((p.id for p in available()), "foundry")
