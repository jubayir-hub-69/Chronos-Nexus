"""Paper entry when ORACLE is idle on a neutral wire or a quiet cash session.

The 75% rail stays in force for a directional cash-session tape. This scan
only runs after a non-degraded stand-down, and only promotes a listed name
whose live 24h volume clears the floor and whose TA score clears 70 with no
hard veto (conflict, wall, fakeout, news fight).
"""

from __future__ import annotations

from typing import Any

from core.ta import (
    NEUTRAL_SETUP_THRESHOLD,
    cash_session_is_quiet,
    news_is_neutral,
    setup_veto,
    strong_session_volume,
)


def infer_tape_side(ta: dict[str, Any] | None) -> str:
    """15m bias / structure → buy or sell. A flat range is not a side."""
    payload = ta if isinstance(ta, dict) else {}
    frames = payload.get("frames") if isinstance(payload.get("frames"), dict) else {}
    entry = frames.get("15m") if isinstance(frames.get("15m"), dict) else payload
    if not isinstance(entry, dict):
        return ""
    bias = str(entry.get("bias") or "").lower()
    structure = str(entry.get("structure") or "").upper()
    if bias == "bullish" or structure in {"UPTREND", "BREAK_UP"}:
        return "buy"
    if bias == "bearish" or structure in {"DOWNTREND", "BREAK_DOWN"}:
        return "sell"
    return ""


def _stay_blocks(symbol: str, stay_away: list[str]) -> bool:
    base = (symbol or "").split(":")[0].split("/")[0].upper()
    root = base[1:] if base.startswith("R") and len(base) > 2 and base[1:].isalpha() else base
    tokens: set[str] = set()
    for item in stay_away:
        for part in str(item).upper().replace("—", " ").replace("-", " ").split():
            tokens.add(part.strip(".,:;"))
    if not tokens:
        return False
    return root in tokens or base in tokens


def _as_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def seek_neutral_candidate(
    bitget: Any,
    symbols: list[str] | tuple[str, ...],
    *,
    news: dict[str, Any] | None,
    session: str | None,
    stay_away: list[str] | None = None,
) -> dict[str, Any] | None:
    """Best unoccupied name for a paper trade, or None.

    Probes at most 8 listings for 24h volume, then scores the top 3 tapes.
    """
    if bitget is None:
        return None
    if not (news_is_neutral(news) or cash_session_is_quiet(session)):
        return None
    blocked = [str(item) for item in (stay_away or []) if str(item).strip()]
    names = [str(item).strip() for item in (symbols or []) if str(item).strip()]
    ranked: list[tuple[float, str]] = []
    for symbol in names[:8]:
        if _stay_blocks(symbol, blocked):
            continue
        try:
            fund = bitget.fetch_fundamentals(symbol) or {}
        except Exception:
            continue
        vol = fund.get("volume_24h_usdt")
        if not strong_session_volume(vol):
            continue
        try:
            ranked.append((float(vol), symbol))
        except (TypeError, ValueError):
            continue
    ranked.sort(key=lambda row: row[0], reverse=True)
    best: dict[str, Any] | None = None
    for vol, symbol in ranked[:3]:
        try:
            ta = bitget.fetch_mtf_bundle(symbol) or {}
            book = bitget.fetch_order_book(symbol) or {}
            ticker = bitget.fetch_ticker(symbol) or {}
        except Exception:
            continue
        side = infer_tape_side(ta)
        if side not in {"buy", "sell"}:
            continue
        frames = ta.get("frames") if isinstance(ta.get("frames"), dict) else {}
        if not frames:
            frames = {"15m": ta}
        last = _as_float(ticker.get("last")) or _as_float(ticker.get("mark"))
        reason, scored = setup_veto(
            side=side,
            frames=frames,
            book=book,
            last=last,
            news=news,
            conviction=0,
            high_24h=_as_float(ticker.get("high")),
            low_24h=_as_float(ticker.get("low")),
            volume_24h=vol,
            session=session,
        )
        if reason:
            continue
        try:
            score = float(scored.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        if score < NEUTRAL_SETUP_THRESHOLD:
            continue
        conviction = max(55, min(80, int(round(score))))
        session_bit = session or "neutral wire"
        row = {
            "symbol": symbol,
            "side": side,
            "score": score,
            "volume_24h": vol,
            "conviction": conviction,
            "thesis": (
                f"Neutral/off-hours paper: {symbol} {side} "
                f"TA {score:.1f} with 24h volume {vol:.0f} USDT."
            ),
            "reason": (
                f"NEUTRAL LANE: {session_bit} — {symbol} {side} setup {score:.1f} "
                f"(24h volume {vol:.0f} USDT, threshold {NEUTRAL_SETUP_THRESHOLD:.0f})."
            ),
        }
        if best is None or row["score"] > best["score"]:
            best = row
    return best
