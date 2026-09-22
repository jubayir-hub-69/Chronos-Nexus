"""Analyst Agent (ORACLE) — live off-hours macro intelligence."""

from __future__ import annotations

import html
import json
import re
from calendar import timegm
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

from core.llm import API_QUOTA_VETO, API_TIMEOUT_VETO, QwenCortex, is_quota_fault
from core.memory import BoardMemory
from core.retry import call_with_backoff
from core.news import score_wire
from core.schemas import AnalystBrief, WeekendTrigger

CALLSIGN = "ORACLE"
STAND_DOWN_SYMBOL = "NONE"

RSS_FEEDS: tuple[tuple[str, str], ...] = (
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; Chronos-Nexus/0.3; "
        "+https://github.com/chronos-nexus; paper-trading research)"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}

_TAG_RE = re.compile(r"<[^>]+>")

_NEWS_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("iphone", "apple", "aapl", "tim cook", "app store", "ipad", "macbook"), "AAPL"),
    (("tesla", "tsla", "cybertruck", "gigafactory", "optimus", "dojo"), "TSLA"),
    (("microsoft", "msft", "azure", "copilot", "satya"), "MSFT"),
    (("google", "alphabet", "googl", "youtube", "android", "waymo"), "GOOG"),
    (("nvidia", "nvda", "cuda", "gpu", "blackwell", "hopper", "jensen"), "NVDA"),
    (("amazon", "amzn", "aws ", "bezos"), "AMZN"),
    (("meta", "facebook", "instagram", "whatsapp", "llama"), "META"),
    (("netflix", "nflx"), "NFLX"),
    (("broadcom", "avgo"), "AVGO"),
    (("amd ", "advanced micro"), "AMD"),
    (("intel", "intc"), "INTC"),
    (("openai", "chatgpt"), "MSFT"),
    (("ev ", "electric vehicle", "electric car"), "TSLA"),
    (("artificial intelligence", "generative ai", "large language model"), "NVDA"),
)

_BAD_NEWS_NEEDLES: tuple[str, ...] = (
    "scandal",
    "fraud",
    "indict",
    "lawsuit",
    "class action",
    "bankrupt",
    "insolv",
    "crash",
    "plunge",
    "tumble",
    "selloff",
    "sell-off",
    "earnings miss",
    "misses estimates",
    "missed estimates",
    "guidance cut",
    "profit warning",
    "downgrade",
    "recall",
    "sec charge",
    "sec probe",
    "investigation",
    "lay off",
    "layoff",
    "job cut",
    "default",
    "trading halt",
    "delist",
    "restatement",
    "whistleblower",
    "accounting probe",
)

_RTOKEN_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("nvidia", "nvda"), "rNVDA/USDT"),
    (("apple", "aapl"), "rAAPL/USDT"),
    (("tesla", "tsla"), "rTSLA/USDT"),
    (("microsoft", "msft"), "rMSFT/USDT"),
    (("alphabet", "google", "googl"), "rGOOGL/USDT"),
    (("amazon", "amzn"), "rAMZN/USDT"),
    (("broadcom", "avgo"), "rAVGO/USDT"),
    (("tsmc", "taiwan semi"), "rTSM/USDT"),
    (("meta", "facebook"), "rMETA/USDT"),
    (("bitcoin", "btc"), "BTC/USDT"),
    (("ethereum", "eth"), "ETH/USDT"),
)

_SYSTEM = """You are ORACLE, the Analyst Agent on Chronos-Nexus.
You are an ACTIVE DAY TRADER on a 24/7 rToken / stock-perp desk. Cash sessions
open and close; tokenized names keep trading. Your job is to pick ONE listed
Demo name with the cleanest short-horizon setup, or STAND DOWN.

A SESSION CLOCK is injected every cycle (UTC timestamp, weekday, US cash
session). Use it. Friday close is not Monday open. Weekend news is gap risk.

Primary edge is TECHNICAL, not a news essay:
- Volume (rvol), 24h high/low location, L2 spread and book imbalance, candle
  structure (engulfing / hammer / marubozu / break), 15m entry vs 1h/4h regime.
- News is CONTEXT. A slightly mixed wire is not a reason to sit out if a
  listed name maps cleanly. Toxic tape (fraud, crash, lawsuit) is stay_away.
- SENTINEL's Python rail requires a 75+ TA setup (volume + candles + book +
  MTF) during the regular US cash session. On a NEUTRAL wire, or when the
  session clock says PRE-MARKET, OVERNIGHT, WEEKEND, or AFTER-HOURS, a listed
  name with strong 24h volume and a clean TA score of 70+ is a valid paper
  trade. Do not emit NONE for the whole quiet session when that tape exists.
  You pick the name; SENTINEL confirms the tape.

Rules:
- Headlines are LIVE RSS. Never invent facts that are not in the wire.
- BOARD MEMORY of the last 5 cycles: do not blindly repeat a failed side+symbol
  on the same news cluster unless the live tape has changed.
- LIVE UNIVERSE from Bitget Demo load_markets(). primary_symbol MUST be copied
  EXACTLY from that list, OR emit primary_symbol="NONE", side="none", conviction=0.
- Pricing is live Bitget MAINNET BBO (BUY=ask, SELL=bid). You do not invent a last.
- NEVER default to NVDA. NEVER assume BUY. Empty universe or unmapped tape → NONE.
- OCCUPIED names already have a live Demo position. Never pick them for a NEW entry.
- Scan ANY equity/sector on the wire. If several listed names hit, pick the
  single best expression of THIS tape and explain why in selection_reason.
- Think like a meticulous quant. Use the full reasoning budget. Do not rush.
- Output JSON only with keys:
  thesis, monday_gap_bias, primary_symbol, side, conviction, horizon,
  rationale, affected_tickers, news_good, news_bad, stay_away, selection_reason
- monday_gap_bias: GAP_UP | GAP_DOWN | MIXED | FADE
- side: buy | sell | none
- conviction: integer 0-100
- stay_away: array of short strings ("NFLX — earnings miss / guidance cut")
"""


class AnalystAgent:
    def __init__(self, cortex: QwenCortex, memory: BoardMemory | None = None) -> None:
        self.cortex = cortex
        self.memory = memory
        self.callsign = CALLSIGN

    def ingest_weekend_wire(self) -> list[WeekendTrigger]:
        """Fetch the newest live financial RSS headlines. Never returns canned macro."""
        return fetch_live_wire(limit=8)

    def brief(
        self,
        triggers: list[WeekendTrigger],
        universe: list[str] | tuple[str, ...] | str,
        preferred_symbol: str | None = None,
        occupied: list[str] | tuple[str, ...] | None = None,
    ) -> AnalystBrief:
        occupied_list = [str(s).strip() for s in (occupied or []) if str(s).strip()]
        blocked = {_base_ticker(s) for s in occupied_list if _base_ticker(s)}
        symbols = [
            s
            for s in _normalize_universe(universe, preferred_symbol)
            if _base_ticker(s) not in blocked
        ]
        clock = session_clock()
        wire = [t.model_dump() for t in triggers]
        headlines = [t.headline for t in triggers if t.headline]
        mem = self.memory.prompt_block() if self.memory is not None else "BOARD MEMORY: none."
        listed = _universe_prompt_block(symbols)
        heuristic_avoid = detect_stay_away(triggers, symbols)
        wire_score = score_wire(triggers)
        occ_block = ""
        if occupied_list:
            occ_block = (
                "OCCUPIED BOOK — a Demo position is already open. Do NOT pick these "
                "for a NEW entry. Do not stack. Dedicated desk monitoring handles them.\n"
                f"Occupied: {', '.join(occupied_list)}\n\n"
            )
        user = (
            f"{mem}\n\n"
            f"SESSION CLOCK (authoritative — do not guess the day or session):\n{clock['prompt']}\n\n"
            f"{occ_block}"
            "Live financial RSS wire (newest first). These are real headlines, not desk fiction:\n"
            f"{_wire_for_prompt(wire)}\n\n"
            "LIVE UNIVERSE from Bitget Demo load_markets() (equity / stock perps / rTokens).\n"
            "Fills are simulated at live Bitget MAINNET BBO (BUY=ask, SELL=bid), "
            "never at a sandbox mid. primary_symbol MUST be one of these exact strings, or NONE:\n"
            f"{listed}\n\n"
            "If the wire names no listed equity, or the tape is not actionable, emit "
            'primary_symbol="NONE", side="none", conviction=0. NEVER default to NVDA. NEVER assume BUY.\n'
            "Summarize news_good / news_bad. Fill stay_away for toxic names. "
            "selection_reason must explain why this name beat the other listed candidates.\n"
            f"PYTHON WIRE SENTIMENT (source-weighted, 0-100): {wire_score.get('sentiment')} "
            f"cred={wire_score.get('credibility')} conflict={wire_score.get('conflict')} "
            f"impact={wire_score.get('impact')} n={wire_score.get('n')}. "
            "Neutral (~50) is not an automatic stand-down — SENTINEL will score TA at 75+. "
            "Only stand down if the wire is toxic/conflicted AND no listed name maps.\n"
            "Act as an active day trader. Produce the JSON brief now. Do not invent catalysts."
        )
        # Static stand-down object only. Never splice raw RSS into thesis/rationale —
        # unescaped quotes in headlines previously exploded fallback JSON parsing.
        fallback = {
            "thesis": API_TIMEOUT_VETO,
            "monday_gap_bias": "MIXED",
            "primary_symbol": STAND_DOWN_SYMBOL,
            "side": "none",
            "conviction": 0,
            "horizon": "weekend_to_monday_open",
            "rationale": API_TIMEOUT_VETO,
            "affected_tickers": [],
            "news_good": "",
            "news_bad": "Bitget Hackathon - Qwen 3.8 Max unavailable — no tape color without a live model.",
            "stay_away": [],
            "selection_reason": "STAND_DOWN: API timeout / no actionable news. No blind fallback.",
        }
        try:
            payload, degraded = self.cortex.generate_json(
                _SYSTEM, user, temperature=0.35, fallback=fallback
            )
        except Exception as exc:
            print(f"[API ERROR] {_safe_text(exc)}", flush=True)
            payload, degraded = dict(fallback), True
        if degraded:
            err = self.cortex.last_error or "unknown Bitget Hackathon - Qwen 3.8 Max fault"
            print(f"[API ERROR] ORACLE degraded: {_safe_text(err)}", flush=True)
            quota = is_quota_fault(err)
            reason = API_QUOTA_VETO if quota else API_TIMEOUT_VETO
            payload = dict(fallback)
            payload["thesis"] = reason
            payload["rationale"] = reason
            payload["primary_symbol"] = STAND_DOWN_SYMBOL
            payload["side"] = "none"
            payload["conviction"] = 0
            payload["stay_away"] = []
            payload["news_good"] = ""
            payload["news_bad"] = reason if quota else "Bitget Hackathon - Qwen 3.8 Max unavailable — no tape color without a live model."
            payload["selection_reason"] = (
                API_QUOTA_VETO
                if quota
                else fallback["selection_reason"]
            )
        picked = snap_to_universe(
            _safe_text(payload.get("primary_symbol") or STAND_DOWN_SYMBOL),
            symbols,
        )
        if degraded:
            picked = STAND_DOWN_SYMBOL
        stay = _str_list(payload.get("stay_away")) if not degraded else []
        for item in heuristic_avoid:
            if item not in stay:
                stay.append(item)
        side = "none" if degraded else _side(payload.get("side"))
        conviction = _clamp_int(payload.get("conviction"), 0)
        if picked == STAND_DOWN_SYMBOL:
            side = "none"
            conviction = 0
        focused = score_wire(triggers, ticker=picked) if picked != STAND_DOWN_SYMBOL else wire_score
        nlp_conv = int(focused.get("conviction") or 0)
        if side in {"buy", "sell"} and focused.get("scored"):
            conviction = min(conviction, max(nlp_conv, 1))
            if (side == "buy" and float(focused.get("sentiment") or 50) < 55) or (
                side == "sell" and float(focused.get("sentiment") or 50) > 45
            ):
                conviction = min(conviction, 45)
            if focused.get("conflict"):
                conviction = min(conviction, 20)
        return AnalystBrief(
            thesis=_safe_text(payload.get("thesis"), fallback["thesis"]),
            monday_gap_bias=_gap(payload.get("monday_gap_bias")),
            primary_symbol=picked,
            side=side,
            conviction=conviction,
            horizon=_safe_text(payload.get("horizon"), "weekend_to_monday_open"),
            rationale=_safe_text(payload.get("rationale"), fallback["rationale"]),
            affected_tickers=_str_list(payload.get("affected_tickers")),
            wire_headlines=headlines,
            news_good=_safe_text(payload.get("news_good")),
            news_bad=_safe_text(payload.get("news_bad")),
            stay_away=stay[:12],
            selection_reason=_safe_text(payload.get("selection_reason")),
            sentiment_score=float(focused.get("sentiment") or 50.0),
            news_credibility=float(focused.get("credibility") or 0.5),
            news_conflict=bool(focused.get("conflict")),
            news_impact=str(focused.get("impact") or "low"),
            llm_degraded=degraded,
            model=self.cortex.model_name,
        )


def fetch_live_wire(limit: int = 8) -> list[WeekendTrigger]:
    items: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(RSS_FEEDS) or 1) as pool:
        futs = [pool.submit(_fetch_feed, name, url) for name, url in RSS_FEEDS]
        for fut in as_completed(futs):
            try:
                items.extend(fut.result() or [])
            except Exception:
                continue
    triggers = _to_triggers(items, limit=limit)
    if triggers:
        return triggers
    return [
        WeekendTrigger(
            id="WIRE-FAIL",
            category="outage",
            headline="Live financial RSS unavailable",
            detail=(
                "Yahoo Finance and CoinTelegraph RSS returned no items. "
                "ORACLE will not invent weekend macro."
            ),
            rtoken_map=[],
            source="chronos-nexus",
        )
    ]


def _fetch_feed(source: str, url: str) -> list[dict[str, Any]]:
    content = _http_get(url)
    if not content:
        return []
    parsed = _parse_with_feedparser(content, source)
    if parsed:
        return parsed
    return _parse_with_xml(content, source)


def _http_get(url: str) -> bytes | None:
    def _requests() -> bytes:
        import requests

        resp = requests.get(url, headers=_HEADERS, timeout=8)
        if resp.status_code == 429:
            raise TimeoutError(f"429 rate limit: {url}")
        resp.raise_for_status()
        return resp.content

    try:
        return call_with_backoff(_requests, attempts=3, label=f"rss:{url}")
    except Exception:
        pass
    try:
        from urllib.request import Request, urlopen

        req = Request(url, headers=_HEADERS)
        with urlopen(req, timeout=8) as resp:  # noqa: S310 — public RSS only
            return resp.read()
    except Exception:
        return None


def _parse_with_feedparser(content: bytes, source: str) -> list[dict[str, Any]]:
    try:
        import feedparser
    except ImportError:
        return []
    feed = feedparser.parse(content)
    entries = list(getattr(feed, "entries", None) or [])
    out: list[dict[str, Any]] = []
    for entry in entries:
        title = _plain(getattr(entry, "title", "") or "")
        if not title:
            continue
        summary = _plain(
            getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
        )
        link = str(getattr(entry, "link", "") or "")
        parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        ts = 0.0
        if parsed:
            try:
                ts = float(timegm(parsed))
            except Exception:
                ts = 0.0
        published = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if ts
            else str(getattr(entry, "published", "") or getattr(entry, "updated", "") or "")
        )
        out.append(
            {
                "source": source,
                "title": title,
                "summary": summary,
                "link": link,
                "published": published,
                "ts": ts,
            }
        )
    return out


def _parse_with_xml(content: bytes, source: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    out: list[dict[str, Any]] = []
    for el in root.iter():
        if _local(el.tag) not in {"item", "entry"}:
            continue
        title = _plain(_child_text(el, "title"))
        if not title:
            continue
        summary = _plain(_child_text(el, "description") or _child_text(el, "summary"))
        link = _child_text(el, "link") or _atom_href(el)
        pub = (
            _child_text(el, "pubDate")
            or _child_text(el, "published")
            or _child_text(el, "updated")
        )
        ts = _parse_ts(pub)
        published = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else pub
        )
        out.append(
            {
                "source": source,
                "title": title,
                "summary": summary,
                "link": link,
                "published": published,
                "ts": ts,
            }
        )
    return out


def _to_triggers(items: list[dict[str, Any]], limit: int) -> list[WeekendTrigger]:
    seen: set[str] = set()
    ranked = sorted(items, key=lambda row: float(row.get("ts") or 0), reverse=True)
    triggers: list[WeekendTrigger] = []
    for item in ranked:
        title = str(item.get("title") or "").strip()
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        blob = f"{title} {item.get('summary') or ''}".lower()
        n = len(triggers) + 1
        triggers.append(
            WeekendTrigger(
                id=f"RSS-{n:02d}",
                category=_classify(blob),
                headline=title,
                detail=_detail(item),
                rtoken_map=_map_rtokens(blob),
                source=str(item.get("source") or ""),
                link=str(item.get("link") or ""),
                published=str(item.get("published") or ""),
            )
        )
        if len(triggers) >= limit:
            break
    return triggers


def _detail(item: dict[str, Any]) -> str:
    summary = _plain(str(item.get("summary") or ""))[:500]
    link = str(item.get("link") or "").strip()
    published = str(item.get("published") or "").strip()
    source = str(item.get("source") or "").strip()
    bits = [bit for bit in (summary, f"source={source}" if source else "", published, link) if bit]
    return " | ".join(bits) or title_or_empty(item)


def title_or_empty(item: dict[str, Any]) -> str:
    return str(item.get("title") or "")


def _classify(blob: str) -> str:
    if any(w in blob for w in ("war", "sanction", "geopolit", "hormuz", "military", "attack")):
        return "geopolitical"
    if any(w in blob for w in ("supply", "chip", "tsmc", "semiconductor", "foundry")):
        return "supply-chain"
    if any(w in blob for w in ("fed", "inflation", "cpi", "treasury", "rate cut", "rate hike")):
        return "macro"
    if any(w in blob for w in ("bitcoin", "ethereum", "crypto", "token")):
        return "crypto"
    if any(w in blob for w in ("ai", "nvidia", "apple", "tesla", "tech", "gpu")):
        return "tech-shift"
    return "live-wire"


def _map_rtokens(blob: str) -> list[str]:
    mapped: list[str] = []
    for needles, symbol in _RTOKEN_HINTS:
        if any(n in blob for n in needles):
            mapped.append(symbol)
    return mapped[:6]


def _normalize_universe(
    universe: list[str] | tuple[str, ...] | str | None,
    preferred: str | None = None,
) -> list[str]:
    if isinstance(universe, str):
        raw = [universe]
    else:
        raw = [str(s).strip() for s in (universe or []) if str(s).strip()]
    if preferred and preferred not in raw:
        raw.append(preferred)
    return raw


def is_none_symbol(symbol: str | None) -> bool:
    raw = (symbol or "").strip().upper()
    return raw in {"", "NONE", "NULL", "N/A", "NA", "-", "FLAT"}


def is_idle_brief(brief: AnalystBrief) -> bool:
    """True when ORACLE refused a trade: NONE / none / 0 / Qwen timeout."""
    if brief.llm_degraded:
        return True
    if (brief.side or "none").lower() == "none":
        return True
    if int(brief.conviction or 0) <= 0:
        return True
    return is_none_symbol(brief.primary_symbol)


def session_clock(now: datetime | None = None) -> dict[str, str]:
    """Exact UTC timestamp, weekday, and US cash session for ORACLE's prompt."""
    utc = now or datetime.now(timezone.utc)
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    else:
        utc = utc.astimezone(timezone.utc)
    et = _eastern(utc)
    weekday = utc.strftime("%A")
    session = _us_cash_session(et)
    stamp = utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    et_stamp = et.strftime("%Y-%m-%d %H:%M %Z") if et.tzinfo else et.strftime("%Y-%m-%d %H:%M ET")
    prompt = (
        f"  utc: {stamp}\n"
        f"  weekday: {weekday}\n"
        f"  us_eastern: {et_stamp}\n"
        f"  us_cash_session: {session}\n"
        "  rTokens / stock perps: trade 24/7 on Bitget Demo regardless of cash hours."
    )
    return {
        "utc": stamp,
        "weekday": weekday,
        "session": session,
        "eastern": et_stamp,
        "prompt": prompt,
        "line": f"{stamp} · {weekday} · {session}",
    }


def detect_stay_away(
    triggers: list[WeekendTrigger] | list[Any],
    universe: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Heuristic toxic-tape flags so stay-away alerts still fire if Qwen is dark."""
    symbols = [str(s).strip() for s in (universe or []) if str(s).strip()]
    flags: list[str] = []
    seen: set[str] = set()
    for item in triggers:
        if isinstance(item, WeekendTrigger):
            headline = item.headline
            blob = f"{item.headline} {item.detail}".lower()
        else:
            headline = str(item or "")
            blob = headline.lower()
        if not blob.strip():
            continue
        hits = [n for n in _BAD_NEWS_NEEDLES if n in blob]
        if not hits:
            continue
        mapped = pick_symbol_from_news([item], symbols) if symbols else None
        name = _base_ticker(mapped) if mapped else "TAPE"
        key = f"{name}|{hits[0]}"
        if key in seen:
            continue
        seen.add(key)
        flags.append(f"{name} — {hits[0]}")
    return flags[:8]


def _universe_prompt_block(symbols: list[str]) -> str:
    if not symbols:
        return "  (empty — no Demo equity/rToken listings; you MUST emit NONE)"
    if len(symbols) <= 280:
        return "\n".join(f"  - {s}" for s in symbols)
    bases = sorted({_base_ticker(s) for s in symbols if _base_ticker(s)})
    head = "\n".join(f"  - {s}" for s in symbols[:80])
    return (
        f"  {len(symbols)} listed Demo names. Ticker roots: {', '.join(bases)}\n"
        "  Exact-symbol sample (copy one of these, or NONE):\n"
        f"{head}\n"
        "  … (catalog truncated; Python will snap a ticker root onto the live book)"
    )


def _eastern(utc: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        pass
    dst = _us_eastern_dst(utc)
    offset = timedelta(hours=4 if dst else 5)
    tz = timezone(timedelta(hours=-4 if dst else -5), name="EDT" if dst else "EST")
    naive = utc.astimezone(timezone.utc).replace(tzinfo=None) - offset
    return naive.replace(tzinfo=tz)


def _us_eastern_dst(utc: datetime) -> bool:
    """US DST: 2nd Sunday March 07:00 UTC → 1st Sunday November 06:00 UTC."""
    year = utc.year
    start = _nth_weekday(year, 3, 6, 2).replace(hour=7)
    end = _nth_weekday(year, 11, 6, 1).replace(hour=6)
    return start <= utc.astimezone(timezone.utc) < end


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    delta = (weekday - first.weekday()) % 7
    day = 1 + delta + (n - 1) * 7
    return datetime(year, month, day, tzinfo=timezone.utc)


def _us_cash_session(et: datetime) -> str:
    if et.weekday() >= 5:
        return "WEEKEND — US cash CLOSED; rTokens trade 24/7"
    minutes = et.hour * 60 + et.minute
    if minutes < 4 * 60:
        return "OVERNIGHT — US cash CLOSED"
    if minutes < 9 * 60 + 30:
        return "PRE-MARKET — US cash not yet open"
    if minutes < 16 * 60:
        return "US CASH OPEN (regular session 09:30–16:00 ET)"
    if minutes < 20 * 60:
        return "AFTER-HOURS — US cash closed, extended tape"
    return "OVERNIGHT — US cash CLOSED"


def _base_ticker(symbol: str) -> str:
    raw = (symbol or "").strip()
    if not raw or is_none_symbol(raw):
        return ""
    base = raw.split(":")[0].split("/")[0].upper()
    if base.startswith("R") and len(base) > 2 and base[1:].isalpha():
        base = base[1:]
    if base == "GOOGL":
        return "GOOG"
    return base


def snap_to_universe(
    raw: str,
    universe: list[str] | tuple[str, ...],
    headlines: list[str] | None = None,
) -> str:
    """Clamp an LLM / alias symbol onto the Demo universe. Idle tape → NONE, never NVDA."""
    del headlines  # news-keyword fallback removed — no blind AAPL/NVDA default
    symbols = [str(s).strip() for s in universe if str(s).strip()]
    wanted = (raw or "").strip()
    if is_none_symbol(wanted):
        return STAND_DOWN_SYMBOL
    if wanted in symbols:
        return wanted
    by_base = {_base_ticker(s): s for s in symbols if _base_ticker(s)}
    base = _base_ticker(wanted)
    if base and base in by_base:
        return by_base[base]
    return STAND_DOWN_SYMBOL


def pick_symbol_from_news(
    triggers_or_headlines: list[Any],
    universe: list[str] | tuple[str, ...],
) -> str | None:
    symbols = [str(s).strip() for s in universe if str(s).strip()]
    by_base = {_base_ticker(s): s for s in symbols}
    if not by_base:
        return None
    chunks: list[str] = []
    for item in triggers_or_headlines:
        if isinstance(item, WeekendTrigger):
            chunks.append(f"{item.headline} {item.detail}")
        else:
            chunks.append(str(item or ""))
    blob = " ".join(chunks).lower()
    if not blob.strip():
        return None
    for needles, ticker in _NEWS_HINTS:
        if ticker in by_base and any(n in blob for n in needles):
            return by_base[ticker]
    return None


def _plain(text: str) -> str:
    return " ".join(_TAG_RE.sub(" ", html.unescape(text or "")).split())


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


def _child_text(el: ET.Element, name: str) -> str:
    want = name.lower()
    for child in list(el):
        if _local(child.tag) != want:
            continue
        if child.text and child.text.strip():
            return child.text
        href = child.attrib.get("href")
        if href:
            return href
    return ""


def _atom_href(el: ET.Element) -> str:
    for child in list(el):
        if _local(child.tag) == "link":
            href = child.attrib.get("href") or (child.text or "")
            if href.strip():
                return href.strip()
    return ""


def _parse_ts(value: str) -> float:
    raw = (value or "").strip()
    if not raw:
        return 0.0
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _gap(value: Any) -> str:
    raw = str(value or "MIXED").upper().replace(" ", "_")
    return raw if raw in {"GAP_UP", "GAP_DOWN", "MIXED", "FADE"} else "MIXED"


def _side(value: Any) -> str:
    raw = str(value or "none").lower().strip()
    return raw if raw in {"buy", "sell", "none"} else "none"


def _clamp_int(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def _wire_for_prompt(wire: list[dict[str, Any]]) -> str:
    """JSON-safe wire dump so quotes in headlines cannot break a later parse."""
    try:
        return json.dumps(wire, ensure_ascii=False, default=str)
    except Exception:
        return "[]"


def _safe_text(value: Any, default: str = "") -> str:
    if value is None:
        text = default
    else:
        text = str(value)
    return " ".join(text.replace("\x00", "").split())


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:12]:
        if isinstance(item, dict):
            ticker = str(item.get("ticker") or item.get("symbol") or "").strip()
            reason = str(item.get("reason") or item.get("rationale") or "").strip()
            if ticker and reason:
                out.append(f"{ticker} — {reason}")
            elif ticker or reason:
                out.append(ticker or reason)
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out[:8]
