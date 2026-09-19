"""Executive Agent (CHAIRMAN) — debate, attest, execute."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agents.analyst import is_idle_brief
from core.llm import API_QUOTA_VETO, API_TIMEOUT_VETO, QwenCortex
from core.memory import BoardMemory, daily_block_reason
from core.schemas import AnalystBrief, AttestationResult, BoardDecision, RiskReport
from core.ta import candle_veto, confluence_veto
from connectors.arbitrum import ArbitrumSepolia
from connectors.bitget_paper import BitgetPaperConnector

CALLSIGN = "CHAIRMAN"

_SYSTEM = """You are CHAIRMAN, the Executive Agent on Chronos-Nexus.
You listen to ORACLE (Analyst) and SENTINEL (Risk). You do not override a VETO.
You produce a single paper-trading decision for Bitget Demo.
You receive BOARD MEMORY of the last 5 paper cycles. Do not talk the desk into
repeating an identical failed side+symbol on the same news cluster.

Output JSON only with keys:
  action, consensus, reasoning
- action: EXECUTE | STAND_DOWN
- consensus: UNANIMOUS | MAJORITY | VETOED
Hard rules the Python chair will also enforce:
- VETO → STAND_DOWN / VETOED (including Illiquid Market / High Spread)
- TA confluence: BUY with RSI >= 70 → STAND_DOWN / VETOED ("VETO: RSI Overbought despite bullish news")
- TA confluence: SELL with RSI <= 30 → STAND_DOWN / VETOED ("VETO: RSI Oversold despite bearish news")
- Candle structure BREAK against the news side → STAND_DOWN / VETOED
- Occupied book (position already open) is never a new entry. SENTINEL/Python already stripped it.
- Daily hard stand-down (4 entries, 3 wins, any SL, chop/downtrend, 6% equity cap) → STAND_DOWN / VETOED
- ORACLE idle (primary_symbol=NONE, side=none, conviction=0, or Qwen timeout) → STAND_DOWN
- No live last price → STAND_DOWN / DEGRADED
- CLEAR or REDUCE → EXECUTE (REDUCE already cut size; it is not a veto)
- NEVER invent NVDA or a BUY when ORACLE stood down.
- The Demo symbol may be a proxy (e.g. BTC/USDT) when rTokens are not listed on Bitget Demo. That is a venue constraint, not a reason to stand down.
"""


class ExecutiveAgent:
    def __init__(self, cortex: QwenCortex, memory: BoardMemory | None = None) -> None:
        self.cortex = cortex
        self.memory = memory
        self.callsign = CALLSIGN

    def synthesize(
        self,
        brief: AnalystBrief,
        risk: RiskReport,
        tradable_symbol: str,
        last_price: float,
    ) -> BoardDecision:
        idle = is_idle_brief(brief)
        no_price = (not idle) and last_price <= 0
        ta_reason = confluence_veto(brief.side, risk.rsi) or candle_veto(
            brief.side,
            {
                "structure_break": str(risk.candle_structure or "").startswith("BREAK_"),
                "break_dir": (
                    "down"
                    if "DOWN" in str(risk.candle_structure or "")
                    else ("up" if "UP" in str(risk.candle_structure or "") else "")
                ),
                "structure": risk.candle_structure,
            },
        )
        day_reason = str(risk.daily_halt or "").strip()
        if not day_reason and self.memory is not None:
            day_reason = daily_block_reason(self.memory.daily_state())
        if idle or risk.verdict == "VETO" or no_price or ta_reason or day_reason:
            locked = "STAND_DOWN"
        else:
            locked = "EXECUTE"
        mem = self.memory.prompt_block() if self.memory is not None else "BOARD MEMORY: none."
        rsi_bit = f"{float(risk.rsi):.2f}" if risk.rsi is not None else "unmeasured"
        user = (
            f"{mem}\n\n"
            f"ORACLE brief:\n{brief.model_dump()}\n\n"
            f"SENTINEL report:\n{risk.model_dump()}\n\n"
            f"Tradable Demo symbol (proxy if rToken unlisted): {tradable_symbol}\n"
            f"Last price: {last_price}\n"
            f"TA confluence: RSI({risk.rsi_period or 14})={rsi_bit} "
            f"{risk.rsi_timeframe or ''} ta_verdict={risk.ta_verdict} "
            f"structure={risk.candle_structure or 'n/a'} "
            f"risk_score={risk.asset_risk_score} sl_margin={risk.sl_margin_frac}\n"
            f"Locked action: {locked} (SENTINEL verdict={risk.verdict}"
            f"{'; ORACLE idle STAND_DOWN' if idle else ''}"
            f"{'; no live last price' if no_price else ''}"
            f"{'; ' + ta_reason if ta_reason else ''}"
            f"{'; ' + day_reason if day_reason else ''})\n"
            "Write reasoning for the locked action. Do not change it."
        )
        fallback = {
            "action": locked,
            "consensus": (
                "DEGRADED" if idle or no_price else ("VETOED" if risk.verdict == "VETO" else "MAJORITY")
            ),
            "reasoning": (
                API_QUOTA_VETO
                if API_QUOTA_VETO in f"{brief.rationale} {risk.rationale}"
                else (
                    API_TIMEOUT_VETO
                    if API_TIMEOUT_VETO in (risk.rationale or "")
                    else "Deterministic chair: respect SENTINEL, keep paper clip inside the cap."
                )
            ),
        }
        if API_QUOTA_VETO in f"{brief.rationale} {risk.rationale}":
            print(f"[API ERROR] CHAIRMAN skipping Bitget Hackathon - Qwen 3.8 Max — {API_QUOTA_VETO}", flush=True)
            payload, degraded = fallback, True
        elif API_TIMEOUT_VETO in f"{brief.rationale} {risk.rationale}":
            print(f"[API ERROR] CHAIRMAN skipping Bitget Hackathon - Qwen 3.8 Max — {API_TIMEOUT_VETO}", flush=True)
            payload, degraded = fallback, True
        else:
            try:
                payload, degraded = self.cortex.generate_json(
                    _SYSTEM, user, temperature=0.1, fallback=fallback
                )
            except Exception as exc:
                print(f"[API ERROR] {str(exc)}", flush=True)
                payload, degraded = fallback, True

        # SENTINEL owns the veto. REDUCE is clearance at cut size, not a stand-down.
        # ORACLE idle (NONE / none / timeout) is a clean stand-down, not a risk veto.
        # TA confluence is a second hard rail: CHAIRMAN cannot buy overbought or sell oversold.
        if idle:
            action = "STAND_DOWN"
            consensus = "DEGRADED"
        elif risk.verdict == "VETO" or ta_reason or day_reason:
            action = "STAND_DOWN"
            consensus = "VETOED"
        elif no_price:
            action = "STAND_DOWN"
            consensus = "DEGRADED"
        else:
            action = "EXECUTE"
            consensus = "UNANIMOUS" if risk.verdict == "CLEAR" else "MAJORITY"

        notional = 0.0
        amount = 0.0
        if action == "EXECUTE":
            notional = max(1.0, float(risk.max_notional_usdt) * float(risk.size_multiplier))
            amount = notional / last_price

        reasoning = str(payload.get("reasoning") or fallback["reasoning"])
        if ta_reason and ta_reason not in reasoning:
            reasoning = f"{ta_reason} (RSI={rsi_bit} {risk.rsi_timeframe or ''}). {reasoning}"
        if day_reason and day_reason not in reasoning:
            reasoning = f"{day_reason}. {reasoning}"
        decision = BoardDecision(
            action=action,
            consensus=consensus,
            symbol=tradable_symbol,
            side=brief.side,
            amount=amount,
            notional_usdt=round(notional, 6),
            reasoning=reasoning,
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

        if decision.action == "EXECUTE" and self.memory is not None:
            allowed, block = self.memory.can_enter()
            if not allowed:
                decision.action = "STAND_DOWN"
                decision.consensus = "VETOED"
                decision.amount = 0.0
                decision.notional_usdt = 0.0
                if block and block not in (decision.reasoning or ""):
                    decision.reasoning = f"{block}. {decision.reasoning}"
                decision.reasoning_hash = hash_decision(brief, risk, decision)

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
                sl_price=risk.sl_price,
                tp_price=risk.tp_price,
                margin_usdt=risk.margin_usdt,
                sl_margin_frac=risk.sl_margin_frac,
            )
            if self.memory is not None and bool(order.get("ok")):
                margin = order.get("margin_usdt")
                if margin is None:
                    margin = risk.margin_usdt
                self.memory.note_entry(
                    symbol=str(order.get("symbol") or decision.symbol),
                    margin_usdt=float(margin or 0.0),
                    order_id=str(order.get("order_id") or ""),
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

        bundle = {
            "decision": decision,
            "attestation": attestation,
            "order": order,
        }
        self._remember(brief, risk, decision, order)
        return bundle

    def _remember(
        self,
        brief: AnalystBrief,
        risk: RiskReport,
        decision: BoardDecision,
        order: dict[str, Any] | None,
    ) -> None:
        if self.memory is None:
            return
        ticket = order or {}
        self.memory.record(
            news_context=list(brief.wire_headlines or []),
            decision={
                "action": decision.action,
                "consensus": decision.consensus,
                "symbol": decision.symbol,
                "side": decision.side,
                "verdict": risk.verdict,
                "rsi": risk.rsi,
                "ta_verdict": risk.ta_verdict,
                "risk_score": risk.asset_risk_score,
                "sl_margin_frac": risk.sl_margin_frac,
                "candle": risk.candle_structure,
                "thesis": brief.thesis,
                "model": decision.model,
            },
            result={
                "ok": bool(ticket.get("ok")),
                "status": ticket.get("status"),
                "order_id": ticket.get("order_id"),
                "error": ticket.get("error"),
            },
        )


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
