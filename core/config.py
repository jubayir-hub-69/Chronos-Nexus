"""Environment loading + OpenRouter / Qwen cortex settings.

Paper trading is a hard lock. Live Bitget keys are refused.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(ENV_PATH, override=False)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_QWEN_MODEL = "qwen/qwen-2.5-72b-instruct"
CORTEX_BACKEND_LABEL = "OpenRouter - Qwen"


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return "(empty)"
    if len(value) <= keep * 2:
        return "****"
    return f"{value[:keep]}…{value[-keep:]}"


def normalize_model_name(name: str) -> str:
    """Keep the full OpenRouter slug (e.g. qwen/qwen-2.5-72b-instruct)."""
    return (name or "").strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    openrouter_api_key: str = ""
    qwen_model: str = DEFAULT_QWEN_MODEL

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

    resolved_qwen_model: str = ""
    qwen_source: str = ""

    @field_validator("bitget_paper_trading", mode="before")
    @classmethod
    def _coerce_paper(cls, value: object) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @field_validator("qwen_model", mode="before")
    @classmethod
    def _coerce_qwen_model(cls, value: object) -> str:
        raw = normalize_model_name(str(value or ""))
        if not raw or raw.lower() in {"auto", "default"}:
            return DEFAULT_QWEN_MODEL
        return raw

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
        requested = normalize_model_name(self.qwen_model)
        if not requested or requested.lower() in {"auto", "default"}:
            self.resolved_qwen_model = DEFAULT_QWEN_MODEL
            self.qwen_source = "default"
            return
        self.resolved_qwen_model = requested
        self.qwen_source = (
            "default" if requested == DEFAULT_QWEN_MODEL else "explicit"
        )

    def public_status(self) -> dict[str, str]:
        return {
            "cortex_backend": CORTEX_BACKEND_LABEL,
            "qwen_model_requested": self.qwen_model or DEFAULT_QWEN_MODEL,
            "qwen_model_resolved": self.resolved_qwen_model or "(pending)",
            "qwen_source": self.qwen_source or "(pending)",
            "openrouter_key": mask_secret(self.openrouter_api_key),
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
