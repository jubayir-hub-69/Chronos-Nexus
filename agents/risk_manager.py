"""Risk Manager Agent (SENTINEL) — veto power over the Board."""

from __future__ import annotations

from typing import Any

from core.llm import GeminiCortex
from core.schemas import AnalystBrief, RiskReport

CALLSIGN = "SENTINEL"

_SYSTEM = """You are SENTINEL, the Risk Manager Agent on Chronos-Nexus.
You have VETO POWER. The Executive cannot override a VETO.

This is Bitget Demo / paper trading only. Still apply institutional risk logic
as if the book were real — the audit log is the product.

Evaluate:
1. Fake-news / rumor quality of the weekend wire (unverified leaks, single-source).
2. Black-swan flags (war-risk spikes, exchange halt, liquidity air-pockets on rTokens).
3. Weekend rToken liquidity vs Monday cash gap (spread, gap-through risk).
4. Concentration: one-name AI beta vs a basket.
5. Size: paper notional must stay tiny (default cap 15 USDT, never above 50).

Verdicts:
- CLEAR: trade may proceed at requested size
- REDUCE: trade may proceed at size_multiplier < 1
- VETO: Executive MUST stand down

Output JSON only with keys:
  verdict, fake_news_risk, black_swan_flags, max_notional_usdt, size_multiplier, rationale
- fake_news_risk: LOW | MEDIUM | HIGH
- size_multiplier: 0.0-1.0
"""


class RiskManagerAgent:
    def __init__(self, cortex: GeminiCortex) -> None:
        self.cortex = cortex
        self.callsign = CALLSIGN

    def evaluate(
        self,
        brief: AnalystBrief,
        ticker: dict[str, Any],
        paper_cap_usdt: float,
        tradable_symbol: str,
    ) -> RiskReport:
        user = (
            f"Analyst brief:\n{brief.model_dump()}\n\n"
            f"Ticker snapshot:\n{ticker}\n\n"
            f"Demo tradable symbol (executor proxy): {tradable_symbol}\n"
            "Do NOT veto just because the rToken name is unlisted on Bitget Demo. "
            "The executor will trade the Demo proxy above.\n"
            f"Paper notional cap USDT: {paper_cap_usdt}\n"
            "Produce the JSON risk report now."
        )
        fallback = {
            "verdict": "REDUCE",
            "fake_news_risk": "MEDIUM",
            "black_swan_flags": ["unverified_weekend_leak"],
            "max_notional_usdt": min(paper_cap_usdt, 15.0),
            "size_multiplier": 0.5,
            "rationale": (
                "Wire is scenario-grade, not confirmed primary-source. "
                "Allow a reduced paper clip to keep the event→decision→execution log alive, "
                "but cut size for rumor quality and weekend rToken spread risk."
            ),
        }
        payload, degraded = self.cortex.generate_json(
            _SYSTEM, user, temperature=0.15, fallback=fallback
        )
        verdict = _verdict(payload.get("verdict"))
        multiplier = _mult(payload.get("size_multiplier"), 0.5 if verdict == "REDUCE" else 1.0)
        if verdict == "VETO":
            multiplier = 0.0
        cap = _cap(payload.get("max_notional_usdt"), paper_cap_usdt)
        return RiskReport(
            verdict=verdict,
            fake_news_risk=_fake(payload.get("fake_news_risk")),
            black_swan_flags=_flags(payload.get("black_swan_flags")),
            max_notional_usdt=cap,
            size_multiplier=multiplier,
            rationale=str(payload.get("rationale") or fallback["rationale"]),
            llm_degraded=degraded,
            model=self.cortex.model_name,
        )


def _verdict(value: Any) -> str:
    raw = str(value or "VETO").upper()
    return raw if raw in {"CLEAR", "REDUCE", "VETO"} else "VETO"


def _fake(value: Any) -> str:
    raw = str(value or "MEDIUM").upper()
    return raw if raw in {"LOW", "MEDIUM", "HIGH"} else "MEDIUM"


def _mult(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _cap(value: Any, hard: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = hard
    return max(1.0, min(parsed, min(hard, 50.0)))


def _flags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value][:8]
