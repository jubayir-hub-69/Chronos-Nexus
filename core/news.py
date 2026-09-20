"""Wire NLP — source-weighted sentiment, recency, impact. No LLM required."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SOURCE_WEIGHT: dict[str, float] = {
    "CNBC": 1.00,
    "Bloomberg": 1.00,
    "Reuters": 0.98,
    "WSJ": 0.96,
    "Financial Times": 0.95,
    "Yahoo Finance": 0.88,
    "MarketWatch": 0.82,
    "CoinTelegraph": 0.52,
    "chronos-nexus": 0.20,
}

_BULL: tuple[tuple[str, float], ...] = (
    ("beats estimates", 18.0),
    ("beats expectations", 18.0),
    ("earnings beat", 16.0),
    ("raises guidance", 16.0),
    ("guidance raise", 14.0),
    ("record revenue", 12.0),
    ("upgrade", 10.0),
    ("price target raised", 10.0),
    ("all-time high", 8.0),
    ("breaks out", 8.0),
    ("partnership", 7.0),
    ("contract win", 8.0),
    ("buyback", 8.0),
    ("dividend hike", 7.0),
    ("fda approval", 16.0),
    ("approval", 6.0),
    ("launch", 5.0),
    ("surge", 6.0),
    ("rally", 5.0),
    ("bullish", 6.0),
    ("strong demand", 8.0),
    ("beat", 7.0),
    ("rate cut", 10.0),
    ("dovish", 9.0),
    ("soft landing", 8.0),
    ("risk-on", 6.0),
)

_BEAR: tuple[tuple[str, float], ...] = (
    ("earnings miss", 18.0),
    ("misses estimates", 18.0),
    ("missed estimates", 16.0),
    ("guidance cut", 16.0),
    ("profit warning", 16.0),
    ("downgrade", 12.0),
    ("lawsuit", 10.0),
    ("class action", 12.0),
    ("fraud", 20.0),
    ("scandal", 16.0),
    ("bankrupt", 22.0),
    ("insolv", 18.0),
    ("sec charge", 16.0),
    ("sec probe", 14.0),
    ("investigation", 10.0),
    ("layoff", 10.0),
    ("job cut", 10.0),
    ("recall", 12.0),
    ("trading halt", 16.0),
    ("delist", 18.0),
    ("plunge", 10.0),
    ("crash", 12.0),
    ("tumble", 8.0),
    ("selloff", 8.0),
    ("sell-off", 8.0),
    ("default", 16.0),
    ("restatement", 14.0),
    ("whistleblower", 12.0),
    ("hawkish", 9.0),
    ("rate hike", 10.0),
    ("hot cpi", 12.0),
    ("recession", 14.0),
    ("risk-off", 6.0),
)

_HIGH_IMPACT: tuple[str, ...] = (
    "earnings",
    "guidance",
    "fed ",
    "fomc",
    "cpi",
    "merger",
    "acquisition",
    "bankrupt",
    "war",
    "sanction",
    "fda",
    "sec ",
    "fomc",
    "payrolls",
    "nfp",
    "powell",
)
_RUMOR: tuple[str, ...] = ("rumor", "unconfirmed", "sources say", "might", "could", " reportedly")
_MACRO: tuple[str, ...] = (
    "fed ",
    "fomc",
    "powell",
    "cpi",
    "pce",
    "nfp",
    "payrolls",
    "treasury",
    "yield",
    "dxy",
    "oil ",
    "opec",
    "geopolit",
    "tariff",
    "sanction",
)

VETO_REASON_NEWS = "VETO: News sentiment too weak or conflicted"


def score_headline(headline: str, source: str = "", published: str = "", detail: str = "") -> dict[str, Any]:
    blob = f"{headline} {detail}".lower()
    bull = sum(w for needle, w in _BULL if needle in blob)
    bear = sum(w for needle, w in _BEAR if needle in blob)
    raw = 50.0 + bull - bear
    cred = SOURCE_WEIGHT.get(source, 0.60)
    if any(tok in blob for tok in _RUMOR):
        cred *= 0.55
    recency = _recency_weight(published)
    impact = "high" if any(tok in blob for tok in _HIGH_IMPACT) else ("medium" if bull + bear >= 10 else "low")
    macro = any(tok in blob for tok in _MACRO)
    if macro and impact != "high":
        impact = "high"
    # Log-odds style: source credibility and recency shrink the move toward 50.
    sentiment = max(0.0, min(100.0, 50.0 + (raw - 50.0) * cred * recency))
    direction = "bull" if sentiment >= 58 else ("bear" if sentiment <= 42 else "neutral")
    return {
        "sentiment": round(sentiment, 2),
        "credibility": round(cred, 3),
        "recency": round(recency, 3),
        "impact": impact,
        "macro": macro,
        "direction": direction,
        "source": source,
        "headline": headline,
    }


def score_wire(
    triggers: list[Any],
    *,
    ticker: str = "",
) -> dict[str, Any]:
    """Aggregate the live RSS tape. Neutral 50 if nothing scored."""
    rows: list[dict[str, Any]] = []
    root = (ticker or "").split(":")[0].split("/")[0].upper().lstrip("R")
    if root == "GOOGL":
        root = "GOOG"
    for item in triggers or []:
        if hasattr(item, "headline"):
            headline = str(item.headline or "")
            source = str(getattr(item, "source", "") or "")
            published = str(getattr(item, "published", "") or "")
            detail = str(getattr(item, "detail", "") or "")
            mapped = [str(x).upper() for x in (getattr(item, "rtoken_map", None) or [])]
        elif isinstance(item, dict):
            headline = str(item.get("headline") or item.get("title") or "")
            source = str(item.get("source") or "")
            published = str(item.get("published") or "")
            detail = str(item.get("detail") or "")
            mapped = [str(x).upper() for x in (item.get("rtoken_map") or [])]
        else:
            continue
        if not headline:
            continue
        row = score_headline(headline, source, published, detail)
        focused = bool(root) and (
            root in headline.upper()
            or root in detail.upper()
            or any(root in m for m in mapped)
        )
        row["focused"] = focused
        rows.append(row)
    if not rows:
        return {
            "scored": False,
            "sentiment": 50.0,
            "credibility": 0.5,
            "conflict": False,
            "n": 0,
            "direction": "neutral",
            "impact": "low",
            "macro": False,
            "conviction": 0,
            "headlines": [],
        }
    focused = [r for r in rows if r.get("focused")] or rows
    wsum = 0.0
    acc = 0.0
    cred_acc = 0.0
    for row in focused:
        w = max(0.05, float(row["credibility"]) * float(row["recency"]))
        if row.get("impact") == "high":
            w *= 1.25
        acc += float(row["sentiment"]) * w
        cred_acc += float(row["credibility"]) * w
        wsum += w
    sentiment = acc / wsum if wsum else 50.0
    cred = cred_acc / wsum if wsum else 0.5
    bulls = sum(1 for r in focused if r["direction"] == "bull")
    bears = sum(1 for r in focused if r["direction"] == "bear")
    conflict = bulls > 0 and bears > 0 and abs(bulls - bears) <= 1 and len(focused) >= 2
    impact = "high" if any(r["impact"] == "high" for r in focused) else (
        "medium" if any(r["impact"] == "medium" for r in focused) else "low"
    )
    direction = "bull" if sentiment >= 58 else ("bear" if sentiment <= 42 else "neutral")
    macro = any(bool(r.get("macro")) for r in focused)
    conv = conviction_score("buy" if direction == "bull" else ("sell" if direction == "bear" else "none"), {
        "scored": True,
        "sentiment": sentiment,
        "credibility": cred,
        "conflict": conflict,
        "impact": impact,
        "macro": macro,
        "recency": sum(float(r.get("recency") or 0.8) for r in focused) / max(len(focused), 1),
    })
    return {
        "scored": True,
        "sentiment": round(sentiment, 2),
        "credibility": round(cred, 3),
        "conflict": conflict,
        "n": len(focused),
        "direction": direction,
        "impact": impact,
        "macro": macro,
        "conviction": conv,
        "headlines": focused[:8],
    }


def news_veto(side: str, news: dict[str, Any] | None) -> str:
    payload = news if isinstance(news, dict) else {}
    if not payload.get("scored"):
        return ""
    side_n = (side or "").strip().lower()
    try:
        sentiment = float(payload.get("sentiment") or 50.0)
    except (TypeError, ValueError):
        sentiment = 50.0
    if payload.get("conflict") and float(payload.get("credibility") or 0) < 0.75:
        return VETO_REASON_NEWS
    if side_n == "buy" and sentiment < 58:
        return VETO_REASON_NEWS
    if side_n == "sell" and sentiment > 42:
        return VETO_REASON_NEWS
    return ""


def conviction_score(side: str, news: dict[str, Any] | None) -> int:
    """0–100 institutional conviction: |sentiment-50| × credibility × recency × impact × side-align.

    Neutral tape cannot print a high conviction. Conflicted tape is capped at 20.
    """
    payload = news if isinstance(news, dict) else {}
    if not payload.get("scored"):
        return 0
    try:
        sentiment = float(payload.get("sentiment") or 50.0)
    except (TypeError, ValueError):
        sentiment = 50.0
    try:
        cred = float(payload.get("credibility") or 0.5)
    except (TypeError, ValueError):
        cred = 0.5
    try:
        recency = float(payload.get("recency") or 0.8)
    except (TypeError, ValueError):
        recency = 0.8
    impact = str(payload.get("impact") or "low").lower()
    impact_w = {"high": 1.15, "medium": 1.0, "low": 0.72}.get(impact, 0.8)
    if payload.get("macro"):
        impact_w *= 1.08
    stretch = abs(sentiment - 50.0) / 50.0
    side_n = (side or "").strip().lower()
    aligned = (side_n == "buy" and sentiment >= 58) or (side_n == "sell" and sentiment <= 42)
    if payload.get("conflict"):
        return 20
    if side_n in {"buy", "sell"} and not aligned:
        return min(40, int(round(35.0 * cred)))
    raw = 100.0 * stretch * max(0.15, cred) * max(0.25, recency) * impact_w
    # Map a 0.5 stretch at full cred/recency/high-impact (~0.66) into the 70–95 band.
    score = 50.0 + raw * 0.9
    return int(max(0, min(100, round(score))))


def _recency_weight(published: str) -> float:
    raw = (published or "").strip()
    if not raw:
        return 0.85
    parsed = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        parsed = None
    if parsed is None:
        return 0.80
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600.0
    if age_h <= 3:
        return 1.0
    if age_h <= 12:
        return 0.9
    if age_h <= 24:
        return 0.75
    if age_h <= 48:
        return 0.5
    return 0.25
