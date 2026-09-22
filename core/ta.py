"""Technical-analysis and position-risk rails.

Pure functions — no CCXT, no LLM. SENTINEL / CHAIRMAN / the desk apply the
vetoes; the Bitget connector only supplies OHLCV, ticker, and fills.

Models
------
* Wilder RSI(14) on 15m (1h fallback). BUY veto ≥ 70; SELL veto ≤ 30.
  Unmeasured RSI is fail-open (does not veto).
* Multi-timeframe confluence: 15m entry, 1h confirmation, 4h regime.
  Frame direction = candle structure, else pattern bias, else SMA(20) slope.
  ALIGNED / MIXED / CONFLICT / UNMEASURED.
* Volume: relative volume vs 20-bar mean (rvol) + 20-bin volume profile
  (POC + 70% value area). Breaks outside VA on rvol < 1.1 are fakeouts.
* VWAP typical-price × volume; chase veto when |close-VWAP|/VWAP > 1.2%
  on an ALIGNED tape (wait for pullback).
* L2 imbalance (bid_vol − ask_vol) / total. Opposing wall = size ≥ 3×
  median within 0.8% of last.
* Setup ensemble (only when 1h/4h are measured). TA-weighted active desk:
    0.10·news + 0.22·HTF + 0.18·entry + 0.18·volume
    + 0.16·book + 0.10·extension + 0.06·conviction
  Score < 75 → VETO. Unmeasured HTF skips the 75% rail (same fail-open as RSI)
  so daily limits, RSI, spread, Spot chatbox, and Telegram buttons stay intact.
  Neutral wire or a quiet US cash session (pre-market, overnight, weekend,
  after-hours) with 24h quote volume ≥ 250k USDT may clear at 70 instead.
"""

from __future__ import annotations

from typing import Any, Sequence

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
RSI_TIMEFRAME = "15m"
RSI_FALLBACK_TIMEFRAME = "1h"
VETO_REASON_RSI_OVERBOUGHT = "VETO: RSI Overbought despite bullish news"
VETO_REASON_RSI_OVERSOLD = "VETO: RSI Oversold despite bearish news"
VETO_REASON_CANDLE = "VETO: Candle structure contradicts news thesis"
VETO_REASON_CHOP = "VETO: Choppy/downtrending tape — daily capital halt"
SCALE_OUT_PCT = 0.25  # first partial TP at +25% PnL (patience; 25–30% window)
SETUP_THRESHOLD = 75.0
# Neutral wire / pre-market: strong 24h volume + clean TA may trade at 70.
NEUTRAL_SETUP_THRESHOLD = 70.0
STRONG_24H_VOLUME_USDT = 250_000.0
MTF_FRAMES = ("15m", "1h", "4h")
VETO_REASON_SETUP = "VETO: Setup confidence below 75 — wait for a cleaner TA tape"
VETO_REASON_WALL = "VETO: Opposing order-book wall"
VETO_REASON_MTF = "VETO: Higher-timeframe trend disagrees"
VETO_REASON_EXTENSION = "VETO: Price extended — waiting for pullback"
VETO_REASON_FAKEOUT = "VETO: Breakout lacks volume confirmation"

SL_MARGIN_FRAC_MIN = 0.50
SL_MARGIN_FRAC_MAX = 1.00
TRAIL_LOCK_PCT = 0.01
ATR_PERIOD = 14
VOL_HIGH_ATR_PCT = 0.04
VOL_MED_ATR_PCT = 0.015
# Live mainnet BBO vs markPrice. Wider than the 1.5% L2 spread veto so a
# marketable ask can sit off mark without being a sandbox print anomaly.
BBO_MARK_DIVERGENCE_PCT = 2.0
VETO_REASON_MARK = "VETO: Live BBO diverges from markPrice"


def _pos_px(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def bbo_peg_price(side: str, bid: Any, ask: Any) -> float | None:
    """Best execution: BUY lifts the ask, SELL/CLOSE hits the bid. Never a mid."""
    side_n = (side or "").strip().lower()
    bid_f = _pos_px(bid)
    ask_f = _pos_px(ask)
    if side_n in {"buy", "long", "cover"}:
        return ask_f
    if side_n in {"sell", "short"}:
        return bid_f
    return None


def extract_mark_price(*sources: Any) -> float | None:
    """First positive mark/index from ticker dicts or scalars. Bitget uses markPrice."""
    for src in sources:
        if src is None or src == "":
            continue
        if isinstance(src, dict):
            info = src.get("info") if isinstance(src.get("info"), dict) else {}
            found = extract_mark_price(
                src.get("mark"),
                src.get("markPrice"),
                src.get("index"),
                src.get("indexPrice"),
                info.get("markPrice") if info else None,
                info.get("markPr") if info else None,
                info.get("indexPrice") if info else None,
            )
            if found:
                return found
            continue
        got = _pos_px(src)
        if got:
            return got
    return None


def mark_divergence_pct(px: Any, mark: Any) -> float | None:
    price = _pos_px(px)
    mark_f = _pos_px(mark)
    if price is None or mark_f is None:
        return None
    return abs(price - mark_f) / mark_f * 100.0


def bbo_sane_vs_mark(
    *,
    peg: Any,
    mark: Any,
    is_contract: bool,
    max_pct: float = BBO_MARK_DIVERGENCE_PCT,
) -> bool:
    """Spot: BBO is enough. Swap/rToken perps: refuse a BBO that has blown off mark."""
    if _pos_px(peg) is None:
        return False
    if not is_contract:
        return True
    mark_f = _pos_px(mark)
    if mark_f is None:
        return True
    div = mark_divergence_pct(peg, mark_f)
    if div is None:
        return True
    return div <= float(max_pct)


def compute_rsi(closes: Sequence[Any], period: int = RSI_PERIOD) -> float | None:
    """Wilder RSI. None when there are fewer than period+1 valid closes."""
    try:
        window = int(period)
    except (TypeError, ValueError):
        return None
    if window <= 0:
        return None
    prices: list[float] = []
    for raw in closes or []:
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            prices.append(px)
    if len(prices) < window + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(prices, prices[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    if len(gains) < window:
        return None

    avg_gain = sum(gains[:window]) / float(window)
    avg_loss = sum(losses[:window]) / float(window)
    for gain, loss in zip(gains[window:], losses[window:]):
        avg_gain = (avg_gain * (window - 1) + gain) / float(window)
        avg_loss = (avg_loss * (window - 1) + loss) / float(window)

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 4)


def compute_sma(closes: Sequence[Any], period: int = 20) -> float | None:
    """Simple moving average. None when the window is thin."""
    try:
        window = int(period)
    except (TypeError, ValueError):
        return None
    if window <= 0:
        return None
    prices: list[float] = []
    for raw in closes or []:
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            prices.append(px)
    if len(prices) < window:
        return None
    return sum(prices[-window:]) / float(window)


def compute_atr(rows: Sequence[Any], period: int = ATR_PERIOD) -> float | None:
    """True-range ATR from CCXT OHLCV rows [ts, o, h, l, c, v]."""
    try:
        window = int(period)
    except (TypeError, ValueError):
        return None
    if window <= 0 or not rows or len(rows) < 3:
        return None
    trs: list[float] = []
    for i in range(1, len(rows)):
        try:
            high = float(rows[i][2])
            low = float(rows[i][3])
            prev_close = float(rows[i - 1][4])
        except (TypeError, ValueError, IndexError):
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    sample = trs[-window:] if trs else []
    if not sample:
        return None
    return sum(sample) / float(len(sample))


def rsi_zone(rsi: float | None) -> str:
    if rsi is None:
        return "unmeasured"
    if rsi >= RSI_OVERBOUGHT:
        return "OVERBOUGHT"
    if rsi <= RSI_OVERSOLD:
        return "OVERSOLD"
    return "NEUTRAL"


def confluence_veto(side: str, rsi: float | None) -> str:
    """Exact SENTINEL reason, or empty when the tape may proceed / RSI is unknown.

    Unmeasured RSI does not veto: this filter is additive to the news thesis,
    unlike the L2 spread rail which fails closed on a dark book.
    """
    if rsi is None:
        return ""
    try:
        reading = float(rsi)
    except (TypeError, ValueError):
        return ""
    side_n = (side or "").strip().lower()
    if side_n == "buy" and reading >= RSI_OVERBOUGHT:
        return VETO_REASON_RSI_OVERBOUGHT
    if side_n == "sell" and reading <= RSI_OVERSOLD:
        return VETO_REASON_RSI_OVERSOLD
    return ""


def ta_verdict(side: str, rsi: float | None) -> str:
    if rsi is None:
        return "SKIPPED"
    if confluence_veto(side, rsi):
        return "VETO"
    return "PASS"


def snapshot_ta(ta: dict[str, Any] | None) -> dict[str, Any]:
    payload = ta if isinstance(ta, dict) else {}
    rsi_raw = payload.get("rsi")
    rsi: float | None
    try:
        rsi = float(rsi_raw) if rsi_raw is not None and rsi_raw != "" else None
    except (TypeError, ValueError):
        rsi = None
    if rsi is not None and rsi < 0:
        rsi = None
    timeframe = str(payload.get("timeframe") or RSI_TIMEFRAME)
    try:
        period = int(payload.get("period") or RSI_PERIOD)
    except (TypeError, ValueError):
        period = RSI_PERIOD
    out: dict[str, Any] = {
        "ok": bool(payload.get("ok")) and rsi is not None,
        "symbol": str(payload.get("symbol") or ""),
        "timeframe": timeframe,
        "period": period,
        "rsi": rsi,
        "zone": rsi_zone(rsi),
        "bars": payload.get("bars"),
        "source": payload.get("source") or "ohlcv",
        "error": payload.get("error"),
    }
    for key in (
        "structure",
        "bias",
        "pattern",
        "atr",
        "atr_pct",
        "volatility",
        "structure_break",
        "break_dir",
        "swing_high",
        "swing_low",
        "last_close",
        "last_open",
        "last_high",
        "last_low",
        "rvol",
        "vwap",
        "vwap_dev_pct",
        "sma20",
        "sma_bias",
        "high",
        "low",
        "high_24h",
        "low_24h",
        "poc",
        "vah",
        "val",
        "in_value_area",
        "mtf",
        "frames",
        "align",
        "pullback_ok",
    ):
        if key in payload:
            out[key] = payload[key]
    return out


def _bars(rows: Sequence[Any]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for row in rows or []:
        try:
            o = float(row[1])
            h = float(row[2])
            l = float(row[3])
            c = float(row[4])
        except (TypeError, ValueError, IndexError):
            continue
        if min(o, h, l, c) <= 0:
            continue
        out.append({"o": o, "h": h, "l": l, "c": c})
    return out


def _pattern(bars: list[dict[str, float]]) -> str:
    if len(bars) < 2:
        return "none"
    prev, last = bars[-2], bars[-1]
    last_body = last["c"] - last["o"]
    prev_body = prev["c"] - prev["o"]
    last_range = max(last["h"] - last["l"], 1e-12)
    abs_body = abs(last_body)
    upper = last["h"] - max(last["o"], last["c"])
    lower = min(last["o"], last["c"]) - last["l"]
    if abs_body <= 0.1 * last_range:
        return "doji"
    last_bull = last_body > 0
    prev_bull = prev_body > 0
    engulfed = abs_body >= abs(prev_body) * 0.99 and (
        (last_bull and not prev_bull and last["c"] >= prev["o"] and last["o"] <= prev["c"])
        or ((not last_bull) and prev_bull and last["c"] <= prev["o"] and last["o"] >= prev["c"])
    )
    if engulfed and last_bull:
        return "bullish_engulfing"
    if engulfed and not last_bull:
        return "bearish_engulfing"
    if lower >= 2.0 * abs_body and upper <= abs_body and last["c"] >= last["o"]:
        return "hammer"
    if upper >= 2.0 * abs_body and lower <= abs_body and last["c"] <= last["o"]:
        return "shooting_star"
    if abs_body >= 0.7 * last_range:
        return "bullish_marubozu" if last_bull else "bearish_marubozu"
    return "bullish_candle" if last_bull else "bearish_candle"


def analyze_candles(rows: Sequence[Any]) -> dict[str, Any]:
    """Live candle structure + ATR volatility. Safe on thin books (unmeasured)."""
    bars = _bars(rows)
    atr = compute_atr(rows)
    if len(bars) < 5:
        return {
            "ok": False,
            "structure": "unmeasured",
            "bias": "neutral",
            "pattern": "none",
            "atr": atr,
            "atr_pct": None,
            "volatility": "unmeasured",
            "structure_break": False,
            "break_dir": "",
            "swing_high": None,
            "swing_low": None,
            "last_close": bars[-1]["c"] if bars else None,
            "error": "insufficient OHLCV for candle structure",
        }
    last = bars[-1]
    lookback = bars[-11:-1] if len(bars) >= 12 else bars[:-1]
    swing_high = max(b["h"] for b in lookback) if lookback else last["h"]
    swing_low = min(b["l"] for b in lookback) if lookback else last["l"]
    structure_break = False
    break_dir = ""
    if last["c"] < swing_low:
        structure_break = True
        break_dir = "down"
        structure = "BREAK_DOWN"
    elif last["c"] > swing_high:
        structure_break = True
        break_dir = "up"
        structure = "BREAK_UP"
    elif last["c"] > bars[-3]["c"] and last["l"] >= min(b["l"] for b in bars[-6:-1]):
        structure = "UPTREND"
    elif last["c"] < bars[-3]["c"] and last["h"] <= max(b["h"] for b in bars[-6:-1]):
        structure = "DOWNTREND"
    else:
        structure = "RANGE"
    pattern = _pattern(bars)
    if pattern in {"bullish_engulfing", "hammer", "bullish_marubozu"} or structure in {
        "UPTREND",
        "BREAK_UP",
    }:
        bias = "bullish"
    elif pattern in {"bearish_engulfing", "shooting_star", "bearish_marubozu"} or structure in {
        "DOWNTREND",
        "BREAK_DOWN",
    }:
        bias = "bearish"
    else:
        bias = "neutral"
    atr_pct = (float(atr) / last["c"]) if atr and last["c"] > 0 else None
    if atr_pct is None:
        volatility = "unmeasured"
    elif atr_pct >= VOL_HIGH_ATR_PCT:
        volatility = "HIGH"
    elif atr_pct >= VOL_MED_ATR_PCT:
        volatility = "MEDIUM"
    else:
        volatility = "LOW"
    return {
        "ok": True,
        "structure": structure,
        "bias": bias,
        "pattern": pattern,
        "atr": atr,
        "atr_pct": None if atr_pct is None else round(atr_pct, 6),
        "volatility": volatility,
        "structure_break": structure_break,
        "break_dir": break_dir,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "last_close": last["c"],
        "last_open": last["o"],
        "last_high": last["h"],
        "last_low": last["l"],
        "error": None,
    }


def severe_tape_halt(ta: dict[str, Any] | None) -> str:
    """Halt new entries for the UTC day when live OHLCV is hostile.

    Uses Bitget fetch_ohlcv structure/volatility already sitting on the TA bundle.
    Unmeasured tape does not halt (same fail-open as RSI) unless volatility is HIGH.
    """
    payload = ta if isinstance(ta, dict) else {}
    structure = str(payload.get("structure") or "").upper()
    vol = str(payload.get("volatility") or "").upper()
    if structure in {"DOWNTREND", "BREAK_DOWN"}:
        return VETO_REASON_CHOP
    if vol == "HIGH" and structure in {"RANGE", "UNMEASURED", ""}:
        return VETO_REASON_CHOP
    return ""


def candle_veto(side: str, ta: dict[str, Any] | None) -> str:
    """Hard veto only on a structure break against the news side. Patterns score risk."""
    payload = ta if isinstance(ta, dict) else {}
    side_n = (side or "").strip().lower()
    if side_n not in {"buy", "sell"}:
        return ""
    brk = bool(payload.get("structure_break"))
    direction = str(payload.get("break_dir") or "").lower()
    structure = str(payload.get("structure") or "")
    if not brk and structure not in {"BREAK_DOWN", "BREAK_UP"}:
        return ""
    if side_n == "buy" and (direction == "down" or structure == "BREAK_DOWN"):
        return VETO_REASON_CANDLE
    if side_n == "sell" and (direction == "up" or structure == "BREAK_UP"):
        return VETO_REASON_CANDLE
    return ""


def structure_break_against(side: str, ta: dict[str, Any] | None) -> bool:
    return bool(candle_veto(side, ta))


def compute_vwap(rows: Sequence[Any]) -> dict[str, Any]:
    num = 0.0
    den = 0.0
    last_close = None
    for row in rows or []:
        try:
            high = float(row[2])
            low = float(row[3])
            close = float(row[4])
            vol = float(row[5]) if len(row) > 5 else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        if min(high, low, close) <= 0:
            continue
        typical = (high + low + close) / 3.0
        num += typical * max(vol, 0.0)
        den += max(vol, 0.0)
        last_close = close
    if den <= 0 or last_close is None:
        return {"ok": False, "vwap": None, "vwap_dev_pct": None}
    vwap = num / den
    return {
        "ok": True,
        "vwap": round(vwap, 8),
        "vwap_dev_pct": round((last_close - vwap) / vwap, 6) if vwap else None,
    }


def analyze_volume(rows: Sequence[Any]) -> dict[str, Any]:
    vols: list[float] = []
    for row in rows or []:
        try:
            vols.append(max(0.0, float(row[5])))
        except (TypeError, ValueError, IndexError):
            continue
    if len(vols) < 8:
        return {"ok": False, "rvol": None, "avg_vol": None, "last_vol": None}
    prior = vols[-21:-1] if len(vols) > 21 else vols[:-1]
    avg = sum(prior) / float(len(prior)) if prior else 0.0
    last = vols[-1]
    rvol = (last / avg) if avg > 0 else None
    return {
        "ok": True,
        "rvol": None if rvol is None else round(rvol, 4),
        "avg_vol": round(avg, 6),
        "last_vol": last,
    }


def analyze_volume_profile(rows: Sequence[Any], bins: int = 20) -> dict[str, Any]:
    """Session volume profile from OHLCV: POC + 70% value area.

    Fake breakouts print outside the value area on thin volume.
    """
    points: list[tuple[float, float]] = []
    last_close = None
    for row in rows or []:
        try:
            high = float(row[2])
            low = float(row[3])
            close = float(row[4])
            vol = float(row[5]) if len(row) > 5 else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        if min(high, low, close) <= 0:
            continue
        typical = (high + low + close) / 3.0
        points.append((typical, max(vol, 0.0)))
        last_close = close
    if len(points) < 8 or last_close is None:
        return {
            "ok": False,
            "poc": None,
            "vah": None,
            "val": None,
            "in_value_area": None,
            "last_vs_poc_pct": None,
        }
    lo = min(p for p, _ in points)
    hi = max(p for p, _ in points)
    if hi <= lo:
        return {
            "ok": False,
            "poc": lo,
            "vah": hi,
            "val": lo,
            "in_value_area": True,
            "last_vs_poc_pct": 0.0,
        }
    width = (hi - lo) / float(max(int(bins), 4))
    hist = [0.0] * max(int(bins), 4)
    for px, vol in points:
        idx = min(len(hist) - 1, max(0, int((px - lo) / width)))
        hist[idx] += vol
    poc_i = max(range(len(hist)), key=lambda i: hist[i])
    poc = lo + (poc_i + 0.5) * width
    total = sum(hist)
    target = total * 0.70
    lo_i = hi_i = poc_i
    covered = hist[poc_i]
    while covered < target and (lo_i > 0 or hi_i < len(hist) - 1):
        left = hist[lo_i - 1] if lo_i > 0 else -1.0
        right = hist[hi_i + 1] if hi_i < len(hist) - 1 else -1.0
        if right >= left:
            hi_i += 1
            covered += hist[hi_i]
        else:
            lo_i -= 1
            covered += hist[lo_i]
    val = lo + lo_i * width
    vah = lo + (hi_i + 1) * width
    in_va = val <= last_close <= vah
    return {
        "ok": True,
        "poc": round(poc, 8),
        "vah": round(vah, 8),
        "val": round(val, 8),
        "in_value_area": in_va,
        "last_vs_poc_pct": round((last_close - poc) / poc * 100.0, 4) if poc else None,
    }


def analyze_book(book: dict[str, Any] | None, *, side: str = "", last: float | None = None) -> dict[str, Any]:
    """L2 imbalance + opposing walls. Uses CCXT fetch_order_book levels."""
    payload = book if isinstance(book, dict) else {}
    bids = _levels(payload.get("bids"))
    asks = _levels(payload.get("asks"))
    bid_vol = sum(sz for _, sz in bids)
    ask_vol = sum(sz for _, sz in asks)
    total = bid_vol + ask_vol
    imbalance = ((bid_vol - ask_vol) / total) if total > 0 else 0.0
    try:
        px = float(last) if last not in (None, "") else float(payload.get("mid") or payload.get("best_bid") or 0)
    except (TypeError, ValueError):
        px = 0.0
    ask_wall = _wall(asks, px, above=True)
    bid_wall = _wall(bids, px, above=False)
    side_n = (side or "").strip().lower()
    veto = ""
    if side_n == "buy" and (ask_wall or imbalance <= -0.35):
        veto = VETO_REASON_WALL
    if side_n == "sell" and (bid_wall or imbalance >= 0.35):
        veto = VETO_REASON_WALL
    return {
        "ok": bool(bids or asks),
        "imbalance": round(imbalance, 4),
        "bid_vol": round(bid_vol, 6),
        "ask_vol": round(ask_vol, 6),
        "ask_wall": ask_wall,
        "bid_wall": bid_wall,
        "veto": veto,
    }


def _levels(raw: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return out
    for row in raw[:40]:
        try:
            px = float(row[0])
            sz = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px > 0 and sz > 0:
            out.append((px, sz))
    return out


def _wall(levels: list[tuple[float, float]], last: float, *, above: bool) -> dict[str, float] | None:
    if not levels or last <= 0:
        return None
    sizes = sorted(sz for _, sz in levels)
    median = sizes[len(sizes) // 2]
    if median <= 0:
        return None
    threshold = max(median * 3.0, median + 1e-9)
    for px, sz in levels:
        dist = (px - last) / last
        if above and dist < 0:
            continue
        if (not above) and dist > 0:
            continue
        if abs(dist) > 0.008:
            continue
        if sz >= threshold:
            return {"price": px, "size": sz, "dist_pct": round(dist * 100.0, 4)}
    return None


def enrich_ohlcv(rows: Sequence[Any], timeframe: str) -> dict[str, Any]:
    candles = analyze_candles(rows)
    closes: list[float] = []
    for row in rows or []:
        try:
            closes.append(float(row[4]))
        except (TypeError, ValueError, IndexError):
            continue
    vwap = compute_vwap(rows)
    volume = analyze_volume(rows)
    profile = analyze_volume_profile(rows)
    candles["rsi"] = compute_rsi(closes)
    candles["timeframe"] = timeframe
    sma20 = compute_sma(closes, 20)
    last_close = closes[-1] if closes else None
    sma_bias = "neutral"
    if sma20 and last_close:
        if last_close > sma20 * 1.001:
            sma_bias = "bullish"
        elif last_close < sma20 * 0.999:
            sma_bias = "bearish"
    candles["sma20"] = None if sma20 is None else round(float(sma20), 8)
    candles["sma_bias"] = sma_bias
    candles["vwap"] = vwap.get("vwap")
    candles["vwap_dev_pct"] = vwap.get("vwap_dev_pct")
    candles["rvol"] = volume.get("rvol")
    candles["avg_vol"] = volume.get("avg_vol")
    candles["last_vol"] = volume.get("last_vol")
    candles["poc"] = profile.get("poc")
    candles["vah"] = profile.get("vah")
    candles["val"] = profile.get("val")
    candles["in_value_area"] = profile.get("in_value_area")
    candles["bars"] = len(closes)
    candles["ok"] = bool(candles.get("ok") or vwap.get("ok") or volume.get("ok") or profile.get("ok"))
    return candles


def mtf_alignment(frames: dict[str, Any] | None, side: str) -> str:
    """ALIGNED | MIXED | CONFLICT | UNMEASURED vs the news side."""
    payload = frames if isinstance(frames, dict) else {}
    dirs: list[int] = []
    for tf in ("1h", "4h"):
        frame = payload.get(tf) if isinstance(payload.get(tf), dict) else {}
        dirs.append(_frame_dir(frame))
    if not dirs or all(d == 0 for d in dirs):
        return "UNMEASURED"
    side_n = (side or "").strip().lower()
    want = 1 if side_n == "buy" else (-1 if side_n == "sell" else 0)
    if want == 0:
        return "UNMEASURED"
    if any(d == -want for d in dirs if d != 0):
        return "CONFLICT"
    if all(d == want for d in dirs):
        return "ALIGNED"
    return "MIXED"


def _frame_dir(frame: dict[str, Any]) -> int:
    structure = str(frame.get("structure") or "").upper()
    bias = str(frame.get("bias") or "").lower()
    sma_bias = str(frame.get("sma_bias") or "").lower()
    if structure in {"UPTREND", "BREAK_UP"} or bias == "bullish":
        return 1
    if structure in {"DOWNTREND", "BREAK_DOWN"} or bias == "bearish":
        return -1
    if sma_bias == "bullish":
        return 1
    if sma_bias == "bearish":
        return -1
    return 0


def detect_fakeout(entry: dict[str, Any] | None) -> bool:
    """Break without volume, or a break that prints outside the value area on thin tape."""
    frame = entry if isinstance(entry, dict) else {}
    if not frame.get("structure_break"):
        return False
    rvol = frame.get("rvol")
    try:
        rv = float(rvol) if rvol is not None else None
    except (TypeError, ValueError):
        rv = None
    if rv is not None and rv < 0.85:
        return True
    if frame.get("in_value_area") is False and (rv is None or rv < 1.1):
        return True
    return False


def detect_pullback(side: str, entry: dict[str, Any] | None, align: str) -> bool:
    """True when HTF agrees and 15m is not a chase of the spike."""
    if align != "ALIGNED":
        return False
    frame = entry if isinstance(entry, dict) else {}
    rsi = frame.get("rsi")
    dev = frame.get("vwap_dev_pct")
    side_n = (side or "").strip().lower()
    try:
        rsi_f = float(rsi) if rsi is not None else None
    except (TypeError, ValueError):
        rsi_f = None
    try:
        dev_f = float(dev) if dev is not None else None
    except (TypeError, ValueError):
        dev_f = None
    if side_n == "buy":
        if rsi_f is not None and rsi_f >= 68:
            return False
        if dev_f is not None and dev_f > 0.012:
            return False
        return str(frame.get("structure") or "") != "BREAK_DOWN"
    if side_n == "sell":
        if rsi_f is not None and rsi_f <= 32:
            return False
        if dev_f is not None and dev_f < -0.012:
            return False
        return str(frame.get("structure") or "") != "BREAK_UP"
    return False


def cash_session_is_quiet(session: str | None) -> bool:
    """US cash is not in the regular 09:30–16:00 ET session."""
    blob = (session or "").upper()
    return any(
        token in blob
        for token in ("PRE-MARKET", "OVERNIGHT", "WEEKEND", "AFTER-HOURS")
    )


def news_is_neutral(news: dict[str, Any] | None) -> bool:
    """Unscored or mid-band wire. A conflicted tape is not neutral."""
    payload = news if isinstance(news, dict) else {}
    if payload.get("conflict"):
        return False
    if not payload.get("scored"):
        return True
    try:
        sentiment = float(payload.get("sentiment") or 50.0)
    except (TypeError, ValueError):
        sentiment = 50.0
    return 42.0 < sentiment < 58.0


def strong_session_volume(volume_24h: Any) -> bool:
    """True when the live ticker printed a real 24h quote volume above the floor."""
    if volume_24h is None or volume_24h == "":
        return False
    try:
        vol = float(volume_24h)
    except (TypeError, ValueError):
        return False
    return vol >= STRONG_24H_VOLUME_USDT


def neutral_lane_allows(
    *,
    score: Any,
    volume_24h: Any,
    news: dict[str, Any] | None,
    session: str | None,
) -> bool:
    """70% TA is enough when 24h volume is strong and the session or wire is quiet.

    Hard vetoes (MTF conflict, wall, fakeout, news fight) are decided before this
    lane. Cash-session directional tapes keep the 75 rail.
    """
    try:
        scored = float(score)
    except (TypeError, ValueError):
        return False
    if scored < NEUTRAL_SETUP_THRESHOLD:
        return False
    if not strong_session_volume(volume_24h):
        return False
    return news_is_neutral(news) or cash_session_is_quiet(session)


def setup_veto(
    *,
    side: str,
    frames: dict[str, Any] | None = None,
    book: dict[str, Any] | None = None,
    last: float | None = None,
    news: dict[str, Any] | None = None,
    conviction: int = 0,
    high_24h: float | None = None,
    low_24h: float | None = None,
    volume_24h: float | None = None,
    session: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Hard rails + composite 0–100. Caller vetoes when reason is set."""
    from core.news import news_veto

    payload = frames if isinstance(frames, dict) else {}
    entry = payload.get("15m") if isinstance(payload.get("15m"), dict) else payload
    if not isinstance(entry, dict):
        entry = {}
    else:
        entry = dict(entry)
    if high_24h is not None:
        entry["high_24h"] = high_24h
    if low_24h is not None:
        entry["low_24h"] = low_24h
    if last is not None:
        entry.setdefault("last_close", last)
    align = mtf_alignment(payload, side)
    fakeout = detect_fakeout(entry)
    pullback = detect_pullback(side, entry, align)
    book_snap = analyze_book(book, side=side, last=last)
    n_veto = news_veto(side, news)
    reason = ""
    if n_veto:
        reason = n_veto
    elif align == "CONFLICT":
        reason = VETO_REASON_MTF
    elif fakeout:
        reason = VETO_REASON_FAKEOUT
    elif book_snap.get("veto"):
        reason = str(book_snap["veto"])
    scored = score_setup(
        side=side,
        news=news,
        frames=payload,
        book_snap=book_snap,
        conviction=conviction,
        align=align,
        pullback=pullback,
        fakeout=fakeout,
        entry=entry,
    )
    measurable = align in {"ALIGNED", "MIXED", "CONFLICT"}
    lane = False
    if not reason and measurable and scored["score"] < SETUP_THRESHOLD:
        lane = neutral_lane_allows(
            score=scored["score"],
            volume_24h=volume_24h,
            news=news,
            session=session,
        )
        if not lane:
            reason = VETO_REASON_SETUP
    scored.update(
        {
            "align": align,
            "pullback_ok": pullback,
            "fakeout": fakeout,
            "book": book_snap,
            "veto": reason,
            "threshold": NEUTRAL_SETUP_THRESHOLD if lane else SETUP_THRESHOLD,
            "measurable": measurable,
            "neutral_lane": lane,
            "volume_24h": volume_24h,
        }
    )
    return reason, scored


def score_setup(
    *,
    side: str,
    news: dict[str, Any] | None,
    frames: dict[str, Any],
    book_snap: dict[str, Any],
    conviction: int,
    align: str,
    pullback: bool,
    fakeout: bool,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Weighted ensemble. Active desk sizes when this clears 75. TA dominates news."""
    news_p = _news_part(side, news)
    htf_p = {"ALIGNED": 100.0, "MIXED": 70.0, "UNMEASURED": 20.0, "CONFLICT": 0.0}.get(align, 20.0)
    entry_p = _entry_part(side, entry)
    vol_p = _volume_part(entry, fakeout)
    book_p = _book_part(side, book_snap)
    ext_p = 92.0 if pullback else (55.0 if align == "ALIGNED" else 65.0)
    try:
        conv = max(0.0, min(100.0, float(conviction)))
    except (TypeError, ValueError):
        conv = 0.0
    parts = {
        "news": news_p,
        "htf": htf_p,
        "entry": entry_p,
        "volume": vol_p,
        "book": book_p,
        "extension": ext_p,
        "conviction": conv,
    }
    weights = {
        "news": 0.10,
        "htf": 0.22,
        "entry": 0.18,
        "volume": 0.18,
        "book": 0.16,
        "extension": 0.10,
        "conviction": 0.06,
    }
    score = sum(parts[k] * weights[k] for k in weights)
    return {"score": round(score, 2), "parts": parts}


def _news_part(side: str, news: dict[str, Any] | None) -> float:
    payload = news if isinstance(news, dict) else {}
    if not payload.get("scored"):
        return 50.0
    try:
        sentiment = float(payload.get("sentiment") or 50.0)
    except (TypeError, ValueError):
        sentiment = 50.0
    cred = float(payload.get("credibility") or 0.5)
    side_n = (side or "").strip().lower()
    aligned = (side_n == "buy" and sentiment >= 58) or (side_n == "sell" and sentiment <= 42)
    if payload.get("conflict"):
        return 25.0
    if not aligned:
        return max(0.0, 40.0 * cred)
    stretch = abs(sentiment - 50.0) / 50.0
    return min(100.0, 70.0 + 30.0 * stretch * cred)


def _entry_part(side: str, entry: dict[str, Any]) -> float:
    bias = str(entry.get("bias") or "").lower()
    structure = str(entry.get("structure") or "").upper()
    side_n = (side or "").strip().lower()
    score = 50.0
    if side_n == "buy" and (bias == "bullish" or structure in {"UPTREND", "RANGE"}):
        score = 82.0
    elif side_n == "sell" and (bias == "bearish" or structure in {"DOWNTREND", "RANGE"}):
        score = 82.0
    if structure in {"BREAK_UP"} and side_n == "buy":
        score = 70.0
    if structure in {"BREAK_DOWN"} and side_n == "sell":
        score = 70.0
    rsi = entry.get("rsi")
    try:
        rsi_f = float(rsi) if rsi is not None else None
    except (TypeError, ValueError):
        rsi_f = None
    if rsi_f is not None:
        if side_n == "buy" and 42 <= rsi_f <= 62:
            score = min(100.0, score + 12)
        if side_n == "sell" and 38 <= rsi_f <= 58:
            score = min(100.0, score + 12)
    score = min(100.0, max(0.0, score + _range_adj(side_n, entry)))
    return score


def _range_adj(side_n: str, entry: dict[str, Any]) -> float:
    """+12 near the supportive 24h extreme, −12 chasing the far extreme."""
    high = _pos_px(entry.get("high_24h")) or _pos_px(entry.get("high")) or _pos_px(entry.get("swing_high"))
    low = _pos_px(entry.get("low_24h")) or _pos_px(entry.get("low")) or _pos_px(entry.get("swing_low"))
    last = _pos_px(entry.get("last_close")) or _pos_px(entry.get("last"))
    if high is None or low is None or last is None or high <= low:
        return 0.0
    loc = (last - low) / (high - low)
    if side_n == "buy":
        if loc <= 0.35:
            return 12.0
        if loc >= 0.80:
            return -12.0
        return 0.0
    if side_n == "sell":
        if loc >= 0.65:
            return 12.0
        if loc <= 0.20:
            return -12.0
        return 0.0
    return 0.0


def _volume_part(entry: dict[str, Any], fakeout: bool) -> float:
    if fakeout:
        return 5.0
    rvol = entry.get("rvol")
    try:
        rv = float(rvol) if rvol is not None else None
    except (TypeError, ValueError):
        rv = None
    if rv is None:
        return 45.0
    if rv >= 1.5:
        return 95.0
    if rv >= 1.1:
        return 80.0
    if rv >= 0.8:
        return 60.0
    return 30.0


def _book_part(side: str, book_snap: dict[str, Any]) -> float:
    if book_snap.get("veto"):
        return 0.0
    try:
        imb = float(book_snap.get("imbalance") or 0.0)
    except (TypeError, ValueError):
        imb = 0.0
    side_n = (side or "").strip().lower()
    if side_n == "buy":
        if imb >= 0.25:
            return 100.0
        if imb >= 0.08:
            return 85.0
        if imb >= 0:
            return 60.0
        return 25.0
    if side_n == "sell":
        if imb <= -0.25:
            return 100.0
        if imb <= -0.08:
            return 85.0
        if imb <= 0:
            return 60.0
        return 25.0
    return 50.0


def sl_margin_frac(risk_score: float) -> float:
    """Map 0 (low risk) → 100% of margin SL, 100 (high risk) → 50% of margin SL."""
    try:
        score = max(0.0, min(100.0, float(risk_score)))
    except (TypeError, ValueError):
        score = 50.0
    return round(SL_MARGIN_FRAC_MAX - (SL_MARGIN_FRAC_MAX - SL_MARGIN_FRAC_MIN) * (score / 100.0), 4)


def score_asset_risk(
    *,
    side: str = "none",
    rsi: float | None = None,
    atr_pct: float | None = None,
    candle_bias: str = "",
    structure: str = "",
    structure_break: bool = False,
    fake_news_risk: str = "MEDIUM",
    spread_pct: float | None = None,
    volume_24h: float | None = None,
    notional_usdt: float | None = None,
    market_cap_usdt: float | None = None,
    total_supply: float | None = None,
) -> float:
    """Deterministic 0–100 asset-risk score. Higher → tighter margin stop."""
    score = 40.0
    fake = str(fake_news_risk or "MEDIUM").upper()
    if fake == "HIGH":
        score += 22.0
    elif fake == "MEDIUM":
        score += 8.0
    else:
        score -= 4.0
    if atr_pct is not None:
        if atr_pct >= VOL_HIGH_ATR_PCT:
            score += 18.0
        elif atr_pct >= VOL_MED_ATR_PCT:
            score += 6.0
        else:
            score -= 4.0
    side_n = (side or "").strip().lower()
    bias = (candle_bias or "").strip().lower()
    if structure_break or str(structure).startswith("BREAK_"):
        score += 16.0
    if (side_n == "buy" and bias == "bearish") or (side_n == "sell" and bias == "bullish"):
        score += 12.0
    elif (side_n == "buy" and bias == "bullish") or (side_n == "sell" and bias == "bearish"):
        score -= 6.0
    if rsi is not None:
        if side_n == "buy" and rsi >= 65:
            score += 8.0
        elif side_n == "sell" and rsi <= 35:
            score += 8.0
    if spread_pct is not None and spread_pct > 0.6:
        score += 10.0
    if volume_24h is not None and notional_usdt is not None and notional_usdt > 0:
        if volume_24h < notional_usdt * 20.0:
            score += 14.0
        elif volume_24h < notional_usdt * 100.0:
            score += 6.0
    if market_cap_usdt is not None:
        if market_cap_usdt < 1.0e7:
            score += 16.0
        elif market_cap_usdt < 1.0e8:
            score += 8.0
        elif market_cap_usdt > 1.0e10:
            score -= 6.0
    elif total_supply is not None and total_supply > 1.0e12:
        score += 8.0
    return round(max(0.0, min(100.0, score)), 2)


def sl_tp_from_margin(
    entry: float,
    side: str,
    margin_usdt: float,
    sl_frac: float,
    *,
    qty: float | None = None,
    leverage: float = 5.0,
    take_profit_pct: float = 0.05,
) -> tuple[float, float]:
    """Stop from invested margin. High-risk 50% of margin; low-risk 100%.

    Example: $10 margin, 5x, entry 100, sl_frac 0.50 → qty 0.5, max loss $5, SL 90.
    """
    px = float(entry)
    try:
        frac = max(SL_MARGIN_FRAC_MIN, min(SL_MARGIN_FRAC_MAX, float(sl_frac)))
    except (TypeError, ValueError):
        frac = 0.75
    try:
        margin = abs(float(margin_usdt))
    except (TypeError, ValueError):
        margin = 0.0
    try:
        lev = max(1.0, float(leverage or 1.0))
    except (TypeError, ValueError):
        lev = 5.0
    size = 0.0
    if qty is not None:
        try:
            size = abs(float(qty))
        except (TypeError, ValueError):
            size = 0.0
    if size <= 0 and px > 0 and margin > 0:
        size = (margin * lev) / px
    if px <= 0 or size <= 0 or margin <= 0:
        return 0.0, 0.0
    loss = frac * margin
    delta = max(loss / size, px * 0.005)
    tp_pct = float(take_profit_pct)
    short = (side or "").strip().lower() in {"sell", "short"}
    if short:
        sl = px + delta
        tp = px * (1.0 - tp_pct)
    else:
        sl = max(px * 0.01, px - delta)
        tp = px * (1.0 + tp_pct)
    return sl, tp


def ratchet_trail_sl(
    side: str,
    entry: float,
    mark: float,
    current_trail: float | None,
    *,
    lock_pct: float = TRAIL_LOCK_PCT,
) -> float:
    """After a scaled TP, trail the runner. Longs only ratchet up; shorts only down.

    Never gives back past breakeven (entry) once the 50% scale-out has fired.
    """
    try:
        px_entry = float(entry)
        px_mark = float(mark)
    except (TypeError, ValueError):
        return float(current_trail or 0.0)
    if px_entry <= 0 or px_mark <= 0:
        return float(current_trail or 0.0)
    pct = max(0.002, float(lock_pct or TRAIL_LOCK_PCT))
    short = (side or "").strip().lower() in {"sell", "short"}
    try:
        held = float(current_trail or 0.0)
    except (TypeError, ValueError):
        held = 0.0
    if short:
        candidate = min(px_entry, px_mark * (1.0 + pct))
        if held <= 0:
            return candidate
        return min(held, candidate)
    candidate = max(px_entry, px_mark * (1.0 - pct))
    if held <= 0:
        return candidate
    return max(held, candidate)


def snapshot_fundamentals(raw: dict[str, Any] | None) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}

    def _num(key: str) -> float | None:
        value = payload.get(key)
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    return {
        "ok": bool(payload.get("ok")),
        "symbol": str(payload.get("symbol") or ""),
        "price": _num("price") or _num("last"),
        "market_cap_usdt": _num("market_cap_usdt"),
        "total_supply": _num("total_supply"),
        "volume_24h_usdt": _num("volume_24h_usdt") or _num("quoteVolume"),
        "is_swap": bool(payload.get("is_swap")),
        "source": str(payload.get("source") or "ccxt"),
        "error": payload.get("error"),
    }
