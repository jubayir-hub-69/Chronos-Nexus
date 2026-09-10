"""Executive Agent (CHAIRMAN) — debate, attest, execute."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.llm import GeminiCortex
from core.schemas import AnalystBrief, AttestationResult, BoardDecision, RiskReport
from connectors.arbitrum import ArbitrumSepolia
from connectors.bitget_paper import BitgetPaperConnector

CALLSIGN = "CHAIRMAN"

_SYSTEM = """You are CHAIRMAN, the Executive Agent on Chronos-Nexus.
You listen to ORACLE (Analyst) and SENTINEL (Risk). You do not override a VETO.
You produce a single paper-trading decision for Bitget Demo.

Output JSON only with keys:
  action, consensus, reasoning
- action: EXECUTE | STAND_DOWN
- consensus: UNANIMOUS | MAJORITY | VETOED
Hard rules the Python chair will also enforce:
- VETO → STAND_DOWN / VETOED
- CLEAR or REDUCE → EXECUTE (REDUCE already cut size; it is not a veto)
- The Demo symbol may be a proxy (e.g. BTC/USDT) when rTokens are not listed on Bitget Demo. That is a venue constraint, not a reason to stand down.
"""


class ExecutiveAgent:
    def __init__(self, cortex: GeminiCortex) -> None:
        self.cortex = cortex
        self.callsign = CALLSIGN

    def synthesize(
        self,
        brief: AnalystBrief,
        risk: RiskReport,
        tradable_symbol: str,
        last_price: float,
    ) -> BoardDecision:
        locked = "STAND_DOWN" if risk.verdict == "VETO" else "EXECUTE"
        user = (
            f"ORACLE brief:\n{brief.model_dump()}\n\n"
            f"SENTINEL report:\n{risk.model_dump()}\n\n"
            f"Tradable Demo symbol (proxy if rToken unlisted): {tradable_symbol}\n"
            f"Last price: {last_price}\n"
            f"Locked action: {locked} (SENTINEL verdict={risk.verdict})\n"
            "Write reasoning for the locked action. Do not change it."
        )
        fallback = {
            "action": "STAND_DOWN" if risk.verdict == "VETO" else "EXECUTE",
            "consensus": "VETOED" if risk.verdict == "VETO" else "MAJORITY",
            "reasoning": "Deterministic chair: respect SENTINEL, keep paper clip inside the cap.",
        }
        payload, degraded = self.cortex.generate_json(
            _SYSTEM, user, temperature=0.1, fallback=fallback
        )

        # SENTINEL owns the veto. REDUCE is clearance at cut size, not a stand-down.
        if risk.verdict == "VETO":
            action = "STAND_DOWN"
            consensus = "VETOED"
        else:
            action = "EXECUTE"
            consensus = "UNANIMOUS" if risk.verdict == "CLEAR" else "MAJORITY"

        notional = 0.0
        amount = 0.0
        if action == "EXECUTE":
            notional = max(1.0, float(risk.max_notional_usdt) * float(risk.size_multiplier))
            amount = notional / max(last_price, 1e-9)

        decision = BoardDecision(
            action=action,
            consensus=consensus,
            symbol=tradable_symbol,
            side=brief.side,
            amount=amount,
            notional_usdt=round(notional, 6),
            reasoning=str(payload.get("reasoning") or fallback["reasoning"]),
            llm_degraded=degraded or brief.llm_degraded or risk.llm_degraded,
            model=self.cortex.model_name,
        )
        decision.reasoning_hash = hash_decision(brief, risk, decision)
        return decision

    def convene(
        self,
        brief: AnalystBrief,
        risk: RiskReport,
        bitget: BitgetPaperConnector,
        arbitrum: ArbitrumSepolia,
        tradable_symbol: str,
        last_price: float,
    ) -> dict[str, Any]:
        decision = self.synthesize(brief, risk, tradable_symbol, last_price)
        attestation: AttestationResult | None = None
        order: dict[str, Any] | None = None

        if decision.action == "EXECUTE":
            attestation = arbitrum.log_proof_of_thought(decision.reasoning_hash)
            extra = {
                "analyst": brief.model_dump(),
                "risk": risk.model_dump(),
                "executive": decision.model_dump(),
                "attestation": attestation.model_dump(),
            }
            order = bitget.execute_paper_order(
                symbol=decision.symbol,
                side=decision.side,
                amount=decision.amount,
                reasoning_hash=decision.reasoning_hash,
                extra=extra,
            )
        else:
            extra = {
                "analyst": brief.model_dump(),
                "risk": risk.model_dump(),
                "executive": decision.model_dump(),
            }
            order = bitget.log_stand_down(
                {
                    "symbol": decision.symbol,
                    "side": decision.side,
                    "reasoning_hash": decision.reasoning_hash,
                    "board": extra,
                }
            )

        return {
            "decision": decision,
            "attestation": attestation,
            "order": order,
        }


def hash_decision(brief: AnalystBrief, risk: RiskReport, decision: BoardDecision) -> str:
    blob = {
        "analyst": brief.model_dump(),
        "risk": risk.model_dump(),
        "action": decision.action,
        "symbol": decision.symbol,
        "side": decision.side,
        "amount": round(decision.amount, 8),
        "notional_usdt": decision.notional_usdt,
        "reasoning": decision.reasoning,
    }
    canonical = json.dumps(blob, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _action(value: Any) -> str:
    raw = str(value or "STAND_DOWN").upper().replace(" ", "_")
    return raw if raw in {"EXECUTE", "STAND_DOWN"} else "STAND_DOWN"


def _consensus(value: Any, verdict: str) -> str:
    if verdict == "VETO":
        return "VETOED"
    raw = str(value or "MAJORITY").upper()
    return raw if raw in {"UNANIMOUS", "MAJORITY", "VETOED", "DEGRADED"} else "MAJORITY"
