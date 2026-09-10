"""Risk Manager Agent (SENTINEL) — veto power over the Board."""

from __future__ import annotations

from typing import Any

from core.llm import API_TIMEOUT_VETO, GeminiCortex
from core.schemas import AnalystBrief, RiskReport

CALLSIGN = "SENTINEL"
SPREAD_VETO_PCT = 0.5
VETO_REASON_SPREAD = "Illiquid Market / High Spread"

_SYSTEM = """You are SENTINEL, the Risk Manager Agent on Chronos-Nexus.
You have VETO POWER. The Executive cannot override a VETO.

This is Bitget Demo / paper trading only. Still apply institutional risk logic
as if the book were real — the audit log is the product.

Evaluate:
1. Fake-news / rumor quality of the live RSS wire (unverified leaks, single-source).
2. Black-swan flags (war-risk spikes, exchange halt, liquidity air-pockets on rTokens).
3. Weekend rToken liquidity vs Monday cash gap (spread, gap-through risk).
4. Concentration: one-name AI beta vs a basket.
5. Size: paper notional must stay tiny (default cap 15 USDT, never above 50).
6. HARD RULE: if the live bid-ask spread is greater than 0.5%, you MUST VETO
   with reason exactly "Illiquid Market / High Spread". Python will enforce this
   even if you return CLEAR.

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
        order_book: dict[str, Any] | None = None,
    ) -> RiskReport:
        book = order_book or {}
        spread_pct = _spread_pct(book, ticker)
        illiquid = _is_illiquid(book, ticker, spread_pct)
        snapshot = _book_snapshot(book, ticker, spread_pct, illiquid)

        user = (
            f"Analyst brief:\n{brief.model_dump()}\n\n"
            f"Ticker snapshot:\n{ticker}\n\n"
            f"L2 order book / spread (live):\n{snapshot}\n\n"
            f"Demo tradable symbol (executor proxy): {tradable_symbol}\n"
            "Do NOT veto just because the rToken name is unlisted on Bitget Demo. "
            "The executor will trade the Demo proxy above.\n"
            f"Paper notional cap USDT: {paper_cap_usdt}\n"
            f"HARD SPREAD RULE: veto when spread_pct > {SPREAD_VETO_PCT} "
            f"with reason '{VETO_REASON_SPREAD}'.\n"
            "Produce the JSON risk report now."
        )
        if API_TIMEOUT_VETO in f"{brief.thesis} {brief.rationale}":
            print(f"[API ERROR] SENTINEL skipping Gemini — {API_TIMEOUT_VETO}", flush=True)
            return RiskReport(
                verdict="VETO",
                fake_news_risk="HIGH",
                black_swan_flags=[API_TIMEOUT_VETO],
                max_notional_usdt=min(paper_cap_usdt, 15.0),
                size_multiplier=0.0,
                rationale=API_TIMEOUT_VETO,
                spread_pct=spread_pct,
                llm_degraded=True,
                model=self.cortex.model_name,
            )

        fallback = {
            "verdict": "VETO",
            "fake_news_risk": "HIGH",
            "black_swan_flags": [API_TIMEOUT_VETO],
            "max_notional_usdt": min(paper_cap_usdt, 15.0),
            "size_multiplier": 0.0,
            "rationale": API_TIMEOUT_VETO,
        }
        try:
            payload, degraded = self.cortex.generate_json(
                _SYSTEM, user, temperature=0.15, fallback=fallback
            )
        except Exception as exc:
            print(f"[API ERROR] {str(exc)}", flush=True)
            payload, degraded = fallback, True
        verdict = _verdict(payload.get("verdict"))
        multiplier = _mult(payload.get("size_multiplier"), 0.5 if verdict == "REDUCE" else 1.0)
        flags = _flags(payload.get("black_swan_flags"))
        rationale = str(payload.get("rationale") or fallback["rationale"])

        if illiquid:
            verdict = "VETO"
            multiplier = 0.0
            if VETO_REASON_SPREAD not in flags:
                flags.append(VETO_REASON_SPREAD)
            spread_bit = f"{spread_pct:.4f}%" if spread_pct is not None else "unmeasured/crossed"
            rationale = (
                f"{VETO_REASON_SPREAD} (spread={spread_bit} > {SPREAD_VETO_PCT}%). {rationale}"
            )

        if verdict == "VETO":
            multiplier = 0.0
        cap = _cap(payload.get("max_notional_usdt"), paper_cap_usdt)
        return RiskReport(
            verdict=verdict,
            fake_news_risk=_fake(payload.get("fake_news_risk")),
            black_swan_flags=flags,
            max_notional_usdt=cap,
            size_multiplier=multiplier,
            rationale=rationale,
            spread_pct=spread_pct,
            llm_degraded=degraded,
            model=self.cortex.model_name,
        )


def _spread_pct(book: dict[str, Any], ticker: dict[str, Any]) -> float | None:
    raw = book.get("spread_pct")
    if isinstance(raw, (int, float)):
        return float(raw)
    bid = _px(book.get("best_bid"), ticker.get("bid"))
    ask = _px(book.get("best_ask"), ticker.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    if ask <= bid:
        return None
    mid = (bid + ask) / 2.0
    return ((ask - bid) / mid) * 100.0


def _is_illiquid(book: dict[str, Any], ticker: dict[str, Any], spread_pct: float | None) -> bool:
    if book.get("crossed"):
        return True
    bid = _px(book.get("best_bid"), ticker.get("bid"))
    ask = _px(book.get("best_ask"), ticker.get("ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask <= bid:
        return True
    return spread_pct is not None and spread_pct > SPREAD_VETO_PCT


def _book_snapshot(
    book: dict[str, Any],
    ticker: dict[str, Any],
    spread_pct: float | None,
    illiquid: bool,
) -> dict[str, Any]:
    return {
        "ok": book.get("ok"),
        "source": book.get("source"),
        "symbol": book.get("symbol") or ticker.get("symbol"),
        "best_bid": book.get("best_bid") if book.get("best_bid") is not None else ticker.get("bid"),
        "best_ask": book.get("best_ask") if book.get("best_ask") is not None else ticker.get("ask"),
        "bid_size": book.get("bid_size"),
        "ask_size": book.get("ask_size"),
        "bid_depth": book.get("bid_depth"),
        "ask_depth": book.get("ask_depth"),
        "spread_pct": spread_pct,
        "crossed": book.get("crossed"),
        "illiquid": illiquid,
        "error": book.get("error"),
    }


def _px(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


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
