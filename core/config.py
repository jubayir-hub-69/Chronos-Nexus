"""Environment loading + Gemini auto-selection.

Paper trading is a hard lock. Live Bitget keys are refused.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.retry import call_with_backoff

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(ENV_PATH, override=False)

# Fastest *stable text* models first. Flash beats Pro. Skip image/live/tts.
_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
)

_SKIP_TOKENS = (
    "embed",
    "image",
    "imagen",
    "veo",
    "lyria",
    "tts",
    "live",
    "audio",
    "omni",
    "computer",
    "robotics",
    "transcribe",
    "deep-research",
    "antigravity",
    "nano-banana",
)


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return "(empty)"
    if len(value) <= keep * 2:
        return "****"
    return f"{value[:keep]}…{value[-keep:]}"


def _normalize_model_name(name: str) -> str:
    return name.split("/")[-1].strip()


def _is_text_chat_model(name: str) -> bool:
    n = name.lower()
    if "gemini" not in n:
        return False
    return not any(tok in n for tok in _SKIP_TOKENS)


def _speed_score(name: str) -> tuple[float, float, float]:
    """Higher is better: stable flash (newest) > lite > pro > preview."""
    n = name.lower()
    stable = 1.0 if ("preview" not in n and "exp" not in n) else 0.0
    if "flash-lite" in n:
        family = 2.0
    elif "flash" in n:
        family = 3.0
    elif "pro" in n:
        family = 1.0
    else:
        family = 0.0
    match = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
    version = float(match.group(1)) if match else 0.0
    return (stable, family, version)


def _list_models_legacy(api_key: str) -> list[str]:
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    names: list[str] = []
    for model in genai.list_models():
        methods = list(getattr(model, "supported_generation_methods", []) or [])
        if "generateContent" in methods:
            names.append(_normalize_model_name(getattr(model, "name", "")))
    return [n for n in names if n]


def _list_models_new(api_key: str) -> list[str]:
    from google import genai
    from google.genai import types

    try:
        http_opts = types.HttpOptions(
            timeout=15_000,
            retry_options=types.HttpRetryOptions(attempts=1),
        )
    except Exception:
        http_opts = {"timeout": 15_000}
    client = genai.Client(api_key=api_key, http_options=http_opts)
    names: list[str] = []
    for model in client.models.list():
        actions = list(getattr(model, "supported_actions", []) or [])
        if actions and "generateContent" not in actions:
            continue
        names.append(_normalize_model_name(getattr(model, "name", "") or getattr(model, "model", "")))
    return [n for n in names if n]


def list_gemini_models(api_key: str) -> list[str]:
    errors: list[str] = []
    # Current SDK first — google-generativeai is end-of-life and emits FutureWarning.
    for loader in (_list_models_new, _list_models_legacy):
        try:
            return call_with_backoff(lambda fn=loader: fn(api_key), label=f"gemini.{loader.__name__}")
        except ImportError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
    raise RuntimeError("Gemini list_models failed: " + " | ".join(errors[:2]))


def resolve_gemini_model(api_key: str, requested: str = "auto") -> tuple[str, list[str], str]:
    """Return (resolved_name, discovered_names, source).

    `GEMINI_MODEL=auto` queries the live catalog and picks the fastest stable
    text model (Flash over Pro, newest over oldest). Explicit names are kept.
    """
    requested = (requested or "auto").strip()
    discovered: list[str] = []
    if not api_key:
        return _FALLBACK_MODELS[0], discovered, "no_api_key_fallback"

    if requested.lower() != "auto":
        return _normalize_model_name(requested), discovered, "explicit"

    try:
        discovered = list_gemini_models(api_key)
    except Exception:
        return _FALLBACK_MODELS[0], discovered, "catalog_error_fallback"

    chat = [n for n in discovered if _is_text_chat_model(n)]
    if not chat:
        return _FALLBACK_MODELS[0], discovered, "empty_catalog_fallback"

    chat.sort(key=_speed_score, reverse=True)
    return chat[0], discovered, "auto"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    gemini_api_key: str = ""
    gemini_model: str = "auto"

    bitget_api_key: str = ""
    bitget_api_secret: str = ""
    bitget_passphrase: str = ""
    bitget_paper_trading: bool = True
    bitget_symbol: str = "rNVDA/USDT"

    arbitrum_sepolia_rpc: str = "https://sepolia-rollup.arbitrum.io/rpc"
    arbitrum_sepolia_chain_id: int = 421614
    arbitrum_private_key: str = ""

    paper_notional_usdt: float = Field(default=15.0, ge=1.0, le=100.0)

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    resolved_gemini_model: str = ""
    gemini_discovery_source: str = ""
    gemini_catalog: list[str] = Field(default_factory=list)

    @field_validator("bitget_paper_trading", mode="before")
    @classmethod
    def _coerce_paper(cls, value: object) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @field_validator("arbitrum_private_key", mode="before")
    @classmethod
    def _normalize_pk(cls, value: object) -> str:
        raw = str(value or "").strip()
        if raw and not raw.startswith("0x"):
            return "0x" + raw
        return raw

    def assert_paper_trading(self) -> None:
        flag = os.getenv("BITGET_PAPER_TRADING", "true")
        if not self.bitget_paper_trading or str(flag).strip().lower() in {"0", "false", "no", "off"}:
            raise RuntimeError(
                "REFUSING TO BOOT: BITGET_PAPER_TRADING must be true. "
                "Chronos-Nexus is Demo/paper only for Bitget AI Base Camp Hackathon S2."
            )

    def resolve_cortex(self) -> None:
        name, catalog, source = resolve_gemini_model(self.gemini_api_key, self.gemini_model)
        self.resolved_gemini_model = name
        self.gemini_catalog = catalog
        self.gemini_discovery_source = source

    def public_status(self) -> dict[str, str]:
        return {
            "gemini_model_requested": self.gemini_model,
            "gemini_model_resolved": self.resolved_gemini_model or "(pending)",
            "gemini_source": self.gemini_discovery_source or "(pending)",
            "gemini_key": mask_secret(self.gemini_api_key),
            "bitget_key": mask_secret(self.bitget_api_key),
            "bitget_paper": str(self.bitget_paper_trading),
            "arb_chain": str(self.arbitrum_sepolia_chain_id),
            "arb_rpc": self.arbitrum_sepolia_rpc,
            "arb_key": mask_secret(self.arbitrum_private_key),
            "telegram": "armed" if (self.telegram_bot_token and self.telegram_chat_id) else "disarmed",
            "telegram_token": mask_secret(self.telegram_bot_token),
        }


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    settings = Settings()
    settings.assert_paper_trading()
    settings.resolve_cortex()
    return settings
