"""Risk Manager Agent (SENTINEL) — veto power over the Board."""

from __future__ import annotations

from typing import Any

from agents.analyst import session_clock
from core.llm import API_QUOTA_VETO, API_TIMEOUT_VETO, QwenCortex
from core.schemas import AnalystBrief, RiskReport
from connectors.bitget_paper import SWAP_LEVERAGE, protective_prices
from core.memory import (
    DAILY_MAX_ENTRIES,
    DAILY_RISK_PCT,
    clip_notional_to_daily_budget,
    daily_block_reason,
)
from core.ta import (
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_PERIOD,
    SCALE_OUT_PCT,
    SETUP_THRESHOLD,
    VETO_REASON_MARK,
    bbo_peg_price,
    bbo_sane_vs_mark,
    candle_veto,
    confluence_veto,
    extract_mark_price,
    score_asset_risk,
    setup_veto,
    severe_tape_halt,
    sl_margin_frac,
    snapshot_fundamentals,
    snapshot_ta,
    ta_verdict,
)

CALLSIGN = "SENTINEL"
SPREAD_VETO_PCT = 1.5
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
6. HARD RULE: if the live bid-ask spread is greater than 1.5%, you MUST VETO
   with reason exactly "Illiquid Market / High Spread". Python will enforce this.
   Size and last_price are the live MAINNET BBO (BUY=ask, SELL=bid), never a
   sandbox mid. Contracts/rToken perps that diverge from markPrice are a VETO.
   even if you return CLEAR. Missing/unmeasured spread is a fail-safe VETO.
7. TA CONFLUENCE (hard Python rail, applied after you return):
   - BUY only if RSI(14) < 70. RSI >= 70 → VETO "VETO: RSI Overbought despite bullish news"
   - SELL only if RSI(14) > 30. RSI <= 30 → VETO "VETO: RSI Oversold despite bearish news"
   Unmeasured RSI does not veto. Do not invent an RSI reading.
8. MULTI-FACTOR: combine news + fundamentals (mcap, supply, last, 24h volume)
   + live candle structure/volatility. A structure BREAK against the news side
   is a hard Python veto ("VETO: Candle structure contradicts news thesis").
9. Return asset_risk_score 0-100. Python maps it onto a margin stop:
   high risk → 50% of invested margin; low risk → up to 100% of invested margin.
10. DAILY CAPITAL (Python-enforced): max 4 entries per UTC day; 3 winning
    closes → hard stand-down; any stop-loss → hard stand-down; live OHLCV
    chop/downtrend → hard stand-down. Deployed margin across the day cannot
    exceed 6% of live fetch_balance equity (5–6% band). Do not use the whole book.
11. SETUP SCORE (Python, TA-weighted): 15m/1h/4h candles + volume + L2 walls
    + 24h high/low + VWAP. Composite must be >= 75 or VETO
    "VETO: Setup confidence below 75 — wait for a cleaner TA tape".
    News is context. TA is the primary edge. Opposing book walls still veto.

Verdicts:
- CLEAR: trade may proceed at requested size
- REDUCE: trade may proceed at size_multiplier < 1
- VETO: Executive MUST stand down

Output JSON only with keys:
  verdict, fake_news_risk, black_swan_flags, max_notional_usdt, size_multiplier,
  rationale, asset_risk_score
- fake_news_risk: LOW | MEDIUM | HIGH
- size_multiplier: 0.0-1.0
- asset_risk_score: 0-100
"""


class RiskManagerAgent:
    def __init__(self, cortex: QwenCortex) -> None:
        self.cortex = cortex
        self.callsign = CALLSIGN

    def evaluate(
        self,
        brief: AnalystBrief,
        ticker: dict[str, Any],
        paper_cap_usdt: float,
        tradable_symbol: str,
        order_book: dict[str, Any] | None = None,
        ta: dict[str, Any] | None = None,
        fundamentals: dict[str, Any] | None = None,
        daily: dict[str, Any] | None = None,
        equity_usdt: float | None = None,
    ) -> RiskReport:
        book = order_book or {}
        spread_pct = _spread_pct(book, ticker)
        illiquid = _is_illiquid(book, ticker, spread_pct)
        snapshot = _book_snapshot(book, ticker, spread_pct, illiquid)
        ta_snap = snapshot_ta(ta)
        fund = snapshot_fundamentals(fundamentals)
        rsi = ta_snap.get("rsi")
        rsi_tf = str(ta_snap.get("timeframe") or "")
        rsi_period = int(ta_snap.get("period") or RSI_PERIOD)
        bid_px = _px(book.get("best_bid"), ticker.get("bid"))
        ask_px = _px(book.get("best_ask"), ticker.get("ask"))
        peg_px = bbo_peg_price(brief.side, bid_px, ask_px)
        last_px = peg_px or _px(
            ticker.get("mark"),
            ticker.get("last"),
            fund.get("price"),
            ask_px,
            bid_px,
        )
        mark_px = extract_mark_price(ticker, book)

        user = (
            f"Analyst brief:\n{brief.model_dump()}\n\n"
            f"Ticker snapshot:\n{ticker}\n\n"
            f"L2 order book / spread (live):\n{snapshot}\n\n"
            f"TA confluence (RSI + candles + volatility):\n{ta_snap}\n\n"
            f"Fundamentals (CCXT):\n{fund}\n\n"
            f"Demo tradable symbol (executor proxy): {tradable_symbol}\n"
            "Do NOT veto just because the rToken name is unlisted on Bitget Demo. "
            "The executor will trade the Demo proxy above.\n"
            f"Paper notional cap USDT: {paper_cap_usdt}\n"
            f"HARD SPREAD RULE: veto when spread_pct > {SPREAD_VETO_PCT} "
            f"with reason '{VETO_REASON_SPREAD}'.\n"
            f"HARD RSI RULE: BUY only if RSI < {RSI_OVERBOUGHT:.0f}; "
            f"SELL only if RSI > {RSI_OVERSOLD:.0f}. "
            "HARD CANDLE RULE: structure BREAK against the news side is a VETO.\n"
            f"DAILY LIMITS: max {DAILY_MAX_ENTRIES} entries, win-streak 3, SL halt, "
            f"{DAILY_RISK_PCT:.0%} equity cap. Live equity USDT={equity_usdt} daily={daily}.\n"
            "Python enforces the exact VETO reason strings.\n"
            "Produce the JSON risk report now."
        )
        if API_TIMEOUT_VETO in f"{brief.thesis} {brief.rationale}" or API_QUOTA_VETO in f"{brief.thesis} {brief.rationale}":
            skip = API_QUOTA_VETO if API_QUOTA_VETO in f"{brief.thesis} {brief.rationale}" else API_TIMEOUT_VETO
            print(f"[API ERROR] SENTINEL skipping Bitget Hackathon - Qwen 3.8 Max — {skip}", flush=True)
            return _stamp_report(
                verdict="VETO",
                fake_news="HIGH",
                flags=[skip],
                cap=min(paper_cap_usdt, 15.0),
                multiplier=0.0,
                rationale=skip,
                spread_pct=spread_pct,
                ta_snap=ta_snap,
                fund=fund,
                last_px=last_px,
                brief=brief,
                paper_cap=paper_cap_usdt,
                degraded=True,
                model=self.cortex.model_name,
                daily=daily,
                equity_usdt=equity_usdt,
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

        is_contract = ":" in str(tradable_symbol or "") or bool(fund.get("is_swap"))
        if peg_px and not bbo_sane_vs_mark(peg=peg_px, mark=mark_px, is_contract=is_contract):
            verdict = "VETO"
            multiplier = 0.0
            if VETO_REASON_MARK not in flags:
                flags.append(VETO_REASON_MARK)
            rationale = (
                f"{VETO_REASON_MARK} (peg={peg_px} mark={mark_px}). {rationale}"
            )

        rsi_reason = confluence_veto(brief.side, rsi)
        if rsi_reason:
            verdict = "VETO"
            multiplier = 0.0
            if rsi_reason not in flags:
                flags.append(rsi_reason)
            rsi_bit = f"{float(rsi):.2f}" if rsi is not None else "n/a"
            rationale = f"{rsi_reason} (RSI={rsi_bit} {rsi_tf or 'ohlcv'}). {rationale}"
            print(
                f"[TA] {rsi_reason}  RSI={rsi_bit} {rsi_tf}  "
                f"{tradable_symbol}  side={brief.side}",
                flush=True,
            )
        elif rsi is not None:
            print(
                f"[TA] PASS  RSI={float(rsi):.2f} {rsi_tf}  "
                f"{tradable_symbol}  side={brief.side}  zone={ta_snap.get('zone')}",
                flush=True,
            )
        else:
            print(
                f"[TA] SKIPPED  RSI unmeasured  {tradable_symbol}  "
                f"side={brief.side}  ({ta_snap.get('error') or 'no OHLCV'})",
                flush=True,
            )

        candle_reason = candle_veto(brief.side, ta_snap)
        if candle_reason:
            verdict = "VETO"
            multiplier = 0.0
            if candle_reason not in flags:
                flags.append(candle_reason)
            rationale = (
                f"{candle_reason} ({ta_snap.get('structure') or 'break'} "
                f"{ta_snap.get('pattern') or ''}). {rationale}"
            )
            print(
                f"[TA] {candle_reason}  {ta_snap.get('structure')}  "
                f"{tradable_symbol}  side={brief.side}",
                flush=True,
            )
        else:
            print(
                f"[TA] CANDLES  structure={ta_snap.get('structure') or 'unmeasured'}  "
                f"pattern={ta_snap.get('pattern') or 'none'}  "
                f"vol={ta_snap.get('volatility') or 'n/a'}  {tradable_symbol}  "
                f"ohlcv=bitget.fetch_ohlcv",
                flush=True,
            )

        news_snap = {
            "scored": bool(brief.wire_headlines)
            or bool(brief.news_conflict)
            or abs(float(brief.sentiment_score or 50.0) - 50.0) > 0.5,
            "sentiment": brief.sentiment_score,
            "credibility": brief.news_credibility,
            "conflict": brief.news_conflict,
            "impact": brief.news_impact,
        }
        frames = ta_snap.get("frames") if isinstance(ta_snap.get("frames"), dict) else {}
        if not frames:
            frames = {"15m": dict(ta_snap)}
        try:
            session_name = str(session_clock().get("session") or "")
        except Exception:
            session_name = ""
        setup_reason, setup = setup_veto(
            side=brief.side,
            frames=frames,
            book=book,
            last=last_px,
            news=news_snap,
            conviction=int(brief.conviction or 0),
            high_24h=_px(ticker.get("high"), fund.get("high_24h")),
            low_24h=_px(ticker.get("low"), fund.get("low_24h")),
            volume_24h=fund.get("volume_24h_usdt"),
            session=session_name,
        )
        if setup_reason:
            already = verdict == "VETO"
            verdict = "VETO"
            multiplier = 0.0
            if setup_reason not in flags:
                flags.append(setup_reason)
            setup_bit = (
                f"{setup_reason} (setup={setup.get('score')}/{SETUP_THRESHOLD} "
                f"mtf={setup.get('align')} pullback={setup.get('pullback_ok')})"
            )
            # Keep the first hard rail (RSI / candle / spread) at the front of rationale.
            rationale = f"{rationale} {setup_bit}." if already else f"{setup_bit}. {rationale}"
            print(
                f"[SETUP] {setup_reason}  score={setup.get('score')}  "
                f"mtf={setup.get('align')}  {tradable_symbol}",
                flush=True,
            )
        else:
            if setup.get("measurable"):
                print(
                    f"[SETUP] PASS  score={setup.get('score')}  "
                    f"mtf={setup.get('align')}  pullback={setup.get('pullback_ok')}  "
                    f"{tradable_symbol}",
                    flush=True,
                )
            else:
                print(
                    f"[SETUP] SKIPPED  HTF unmeasured  "
                    f"score={setup.get('score')}  {tradable_symbol}  "
                    f"(75% rail waits for 1h/4h)",
                    flush=True,
                )

        chop = severe_tape_halt(ta_snap)
        if chop:
            verdict = "VETO"
            multiplier = 0.0
            if chop not in flags:
                flags.append(chop)
            rationale = f"{chop} (structure={ta_snap.get('structure')} vol={ta_snap.get('volatility')}). {rationale}"
            print(f"[DAILY] {chop}", flush=True)

        day_reason = daily_block_reason(daily)
        if day_reason:
            verdict = "VETO"
            multiplier = 0.0
            if day_reason not in flags:
                flags.append(day_reason)
            rationale = f"{day_reason}. {rationale}"
            print(f"[DAILY] {day_reason}", flush=True)

        if verdict == "VETO":
            multiplier = 0.0
        cap = _cap(payload.get("max_notional_usdt"), paper_cap_usdt)
        leverage = 1.0 if fund.get("is_swap") is False else SWAP_LEVERAGE
        if verdict != "VETO":
            requested = max(1.0, float(cap) * float(multiplier))
            sized, budget_reason = clip_notional_to_daily_budget(
                equity_usdt=equity_usdt,
                daily=daily,
                requested_notional=requested,
                leverage=leverage,
            )
            if budget_reason:
                verdict = "VETO"
                multiplier = 0.0
                if budget_reason not in flags:
                    flags.append(budget_reason)
                rationale = f"{budget_reason}. {rationale}"
                print(f"[DAILY] {budget_reason}", flush=True)
            else:
                cap = sized
                multiplier = 1.0
                print(
                    f"[DAILY] sized notional={cap} USDT  equity={equity_usdt}  "
                    f"cap={DAILY_RISK_PCT:.0%}  ohlcv=live",
                    flush=True,
                )
        return _stamp_report(
            verdict=verdict,
            fake_news=_fake(payload.get("fake_news_risk")),
            flags=flags,
            cap=cap,
            multiplier=multiplier,
            rationale=rationale,
            spread_pct=spread_pct,
            ta_snap=ta_snap,
            fund=fund,
            last_px=last_px,
            brief=brief,
            paper_cap=paper_cap_usdt,
            degraded=degraded,
            model=self.cortex.model_name,
            llm_score=payload.get("asset_risk_score"),
            daily=daily,
            equity_usdt=equity_usdt,
            daily_halt=chop or day_reason or "",
            setup=setup,
        )


def _stamp_report(
    *,
    verdict: str,
    fake_news: str,
    flags: list[str],
    cap: float,
    multiplier: float,
    rationale: str,
    spread_pct: float | None,
    ta_snap: dict[str, Any],
    fund: dict[str, Any],
    last_px: float | None,
    brief: AnalystBrief,
    paper_cap: float,
    degraded: bool,
    model: str,
    llm_score: Any = None,
    daily: dict[str, Any] | None = None,
    equity_usdt: float | None = None,
    daily_halt: str = "",
    setup: dict[str, Any] | None = None,
) -> RiskReport:
    rsi = ta_snap.get("rsi")
    py_score = score_asset_risk(
        side=brief.side,
        rsi=rsi,
        atr_pct=ta_snap.get("atr_pct"),
        candle_bias=str(ta_snap.get("bias") or ""),
        structure=str(ta_snap.get("structure") or ""),
        structure_break=bool(ta_snap.get("structure_break")),
        fake_news_risk=fake_news,
        spread_pct=spread_pct,
        volume_24h=fund.get("volume_24h_usdt"),
        notional_usdt=cap * max(multiplier, 0.0) if verdict != "VETO" else paper_cap,
        market_cap_usdt=fund.get("market_cap_usdt"),
        total_supply=fund.get("total_supply"),
    )
    try:
        llm = float(llm_score) if llm_score is not None and llm_score != "" else None
    except (TypeError, ValueError):
        llm = None
    if llm is not None:
        llm = max(0.0, min(100.0, llm))
        score = max(py_score, llm)
    else:
        score = py_score
    frac = sl_margin_frac(score)
    notional = 0.0 if verdict == "VETO" else max(1.0, float(cap) * float(multiplier))
    leverage = 1.0 if fund.get("is_swap") is False else SWAP_LEVERAGE
    margin = (notional / leverage) if notional > 0 else None
    sl_px = None
    tp_px = None
    if last_px and last_px > 0 and margin and margin > 0 and verdict != "VETO":
        qty = notional / last_px
        sl_px, tp_px = protective_prices(
            last_px,
            brief.side,
            margin_usdt=margin,
            qty=qty,
            sl_margin_frac=frac,
            leverage=leverage,
            take_profit_pct=SCALE_OUT_PCT,
        )
    print(
        f"[RISK] score={score:.1f}  sl_margin={frac:.0%}  "
        f"margin={margin if margin is not None else 'n/a'}  "
        f"mcap={fund.get('market_cap_usdt') or 'n/a'}  "
        f"structure={ta_snap.get('structure') or 'n/a'}",
        flush=True,
    )
    return RiskReport(
        verdict=verdict,  # type: ignore[arg-type]
        fake_news_risk=fake_news,  # type: ignore[arg-type]
        black_swan_flags=flags,
        max_notional_usdt=cap,
        size_multiplier=multiplier,
        rationale=rationale,
        spread_pct=spread_pct,
        rsi=rsi,
        rsi_timeframe=str(ta_snap.get("timeframe") or ""),
        rsi_period=int(ta_snap.get("period") or RSI_PERIOD),
        ta_verdict=ta_verdict(brief.side, rsi),  # type: ignore[arg-type]
        asset_risk_score=score,
        sl_margin_frac=frac,
        margin_usdt=None if margin is None else round(float(margin), 6),
        sl_price=sl_px,
        tp_price=tp_px,
        candle_structure=str(ta_snap.get("structure") or ""),
        candle_bias=str(ta_snap.get("bias") or ""),
        candle_pattern=str(ta_snap.get("pattern") or ""),
        volatility=str(ta_snap.get("volatility") or ""),
        market_cap_usdt=fund.get("market_cap_usdt"),
        total_supply=fund.get("total_supply"),
        last_price=last_px,
        fund_ok=bool(fund.get("ok")),
        daily_halt=str(daily_halt or daily_block_reason(daily) or ""),
        equity_usdt=equity_usdt,
        daily_budget_usdt=(
            round(float(equity_usdt) * DAILY_RISK_PCT, 6) if equity_usdt else None
        ),
        daily_deployed_usdt=(
            float((daily or {}).get("deployed_usdt") or 0.0) if daily else None
        ),
        daily_entries=int((daily or {}).get("entries") or 0) if daily else 0,
        setup_score=float((setup or {}).get("score") or 0.0),
        setup_threshold=float((setup or {}).get("threshold") or SETUP_THRESHOLD),
        mtf_align=str((setup or {}).get("align") or ta_snap.get("mtf") or ""),
        book_imbalance=(
            (setup or {}).get("book", {}).get("imbalance")
            if isinstance((setup or {}).get("book"), dict)
            else None
        ),
        rvol=ta_snap.get("rvol"),
        vwap_dev_pct=ta_snap.get("vwap_dev_pct"),
        pullback_ok=bool((setup or {}).get("pullback_ok")),
        sentiment_score=brief.sentiment_score,
        llm_degraded=degraded,
        model=model,
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
    if spread_pct is None:
        return True
    return spread_pct > SPREAD_VETO_PCT


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
