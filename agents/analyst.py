"""Analyst Agent (ORACLE) — weekend / off-hours macro intelligence."""

from __future__ import annotations

from typing import Any

from core.llm import GeminiCortex
from core.schemas import AnalystBrief, WeekendTrigger

CALLSIGN = "ORACLE"

WEEKEND_WIRE: tuple[WeekendTrigger, ...] = (
    WeekendTrigger(
        id="GEO-01",
        category="geopolitical",
        headline="Overnight naval incident lifts Hormuz transit-risk premium",
        detail=(
            "Energy and defense names typically gap at the cash open. "
            "rTokens (rXOM, rCVX, rBA) reprice immediately while NYSE is dark."
        ),
        rtoken_map=["rXOM/USDT", "rCVX/USDT", "rBA/USDT"],
    ),
    WeekendTrigger(
        id="SUPPLY-02",
        category="supply-chain",
        headline="TSMC aftershock chatter delays CoWoS expansion window",
        detail=(
            "AI GPU supply tightness is the highest-beta Monday-gap channel. "
            "rNVDA, rAVGO, rTSM trade 24/7 and will gap the cash tape at the open."
        ),
        rtoken_map=["rNVDA/USDT", "rAVGO/USDT", "rTSM/USDT"],
    ),
    WeekendTrigger(
        id="TECH-03",
        category="tech-shift",
        headline="Hyperscaler weekend leak: incremental AI capex into 2H",
        detail=(
            "A credible capex add reprices the Mag-7 complex on rToken rails first. "
            "Primary vector: rNVDA, with rMSFT / rGOOGL / rAMZN as secondary."
        ),
        rtoken_map=["rNVDA/USDT", "rMSFT/USDT", "rGOOGL/USDT", "rAMZN/USDT"],
    ),
)

_SYSTEM = """You are ORACLE, the Analyst Agent on Chronos-Nexus.
You sit a 24/7 rToken desk. Cash US equities are closed (weekend / overnight).
Tokenized US stocks (Bitget rTokens) keep trading. Your job is to translate
weekend macro into a Monday cash-session gap thesis and a SINGLE paper trade
on Bitget Demo.

Rules:
- Reason about gap risk, not long-term fundamentals.
- Prefer liquid mega-cap rTokens (rNVDA, rAAPL, rTSLA, rMSFT).
- If the preferred rToken pair is unavailable, still name it; the executor will fall back.
- Never claim a live news fact you were not given. Treat the wire as desk scenarios.
- Output JSON only with keys:
  thesis, monday_gap_bias, primary_symbol, side, conviction, horizon, rationale, affected_tickers
- monday_gap_bias: GAP_UP | GAP_DOWN | MIXED | FADE
- side: buy | sell
- conviction: integer 0-100
- primary_symbol must look like rNVDA/USDT
"""


class AnalystAgent:
    def __init__(self, cortex: GeminiCortex) -> None:
        self.cortex = cortex
        self.callsign = CALLSIGN

    def ingest_weekend_wire(self) -> list[WeekendTrigger]:
        return list(WEEKEND_WIRE)

    def brief(self, triggers: list[WeekendTrigger], preferred_symbol: str) -> AnalystBrief:
        wire = [t.model_dump() for t in triggers]
        user = (
            "Weekend / off-hours desk wire:\n"
            f"{wire}\n\n"
            f"Executor preferred symbol if listed: {preferred_symbol}\n"
            "Produce the JSON brief now."
        )
        fallback = {
            "thesis": "AI-supply + capex wire dominates the weekend tape; fade energy noise.",
            "monday_gap_bias": "GAP_UP",
            "primary_symbol": preferred_symbol or "rNVDA/USDT",
            "side": "buy",
            "conviction": 62,
            "horizon": "weekend_to_monday_open",
            "rationale": (
                "CoWoS delay + hyperscaler capex leak both push semiconductor beta higher "
                "into the cash open. rNVDA is the cleanest 24/7 expression."
            ),
            "affected_tickers": ["NVDA", "TSM", "AVGO"],
        }
        payload, degraded = self.cortex.generate_json(
            _SYSTEM, user, temperature=0.35, fallback=fallback
        )
        return AnalystBrief(
            thesis=str(payload.get("thesis") or fallback["thesis"]),
            monday_gap_bias=_gap(payload.get("monday_gap_bias")),
            primary_symbol=str(payload.get("primary_symbol") or preferred_symbol),
            side=_side(payload.get("side")),
            conviction=_clamp_int(payload.get("conviction"), 62),
            horizon=str(payload.get("horizon") or "weekend_to_monday_open"),
            rationale=str(payload.get("rationale") or fallback["rationale"]),
            affected_tickers=_str_list(payload.get("affected_tickers")),
            llm_degraded=degraded,
            model=self.cortex.model_name,
        )


def _gap(value: Any) -> str:
    raw = str(value or "MIXED").upper().replace(" ", "_")
    return raw if raw in {"GAP_UP", "GAP_DOWN", "MIXED", "FADE"} else "MIXED"


def _side(value: Any) -> str:
    raw = str(value or "buy").lower()
    return raw if raw in {"buy", "sell"} else "buy"


def _clamp_int(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value][:8]
