"""Analyst Agent (ORACLE) — live off-hours macro intelligence."""

from __future__ import annotations

import html
import re
from calendar import timegm
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

from core.llm import API_TIMEOUT_VETO, GeminiCortex
from core.memory import BoardMemory
from core.retry import call_with_backoff
from core.schemas import AnalystBrief, WeekendTrigger

CALLSIGN = "ORACLE"

RSS_FEEDS: tuple[tuple[str, str], ...] = (
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
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
    (("openai", "chatgpt"), "MSFT"),
    (("ev ", "electric vehicle", "electric car"), "TSLA"),
    (("artificial intelligence", "generative ai", "large language model"), "NVDA"),
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
You sit a 24/7 rToken desk. Cash US equities are closed (weekend / overnight).
Tokenized US stocks (Bitget rTokens / stock perps) keep trading. Your job is to
translate LIVE financial headlines into a Monday cash-session gap thesis and a
SINGLE paper trade on Bitget Demo.

Rules:
- Reason about gap risk, not long-term fundamentals.
- Headlines are LIVE RSS items (Yahoo Finance / CoinTelegraph). Never invent facts
  that are not in the wire. Never reuse a canned weekend scenario.
- You receive BOARD MEMORY of the last 5 paper cycles. Do not blindly repeat a
  side+symbol that just failed or was vetoed for the same news cluster unless the
  live wire has materially changed.
- You are given a UNIVERSE of tradable Demo symbols. primary_symbol MUST be
  copied EXACTLY from that list. Never invent rNVDA/USDT or any name outside it.
- Pick the SINGLE most relevant name for THIS tape:
  · iPhone / Apple hardware / App Store / Tim Cook → the AAPL listing
  · AI chips / GPUs / CUDA / general AI beta / NVIDIA → the NVDA listing
  · Azure / Office / OpenAI partnership / Satya → the MSFT listing
  · Search / YouTube / Android / Alphabet / Google → the GOOG listing
  · EVs / autonomy / Tesla / Musk automotive → the TSLA listing
- If several names hit, pick the highest-beta expression of the dominant headline.
- Output JSON only with keys:
  thesis, monday_gap_bias, primary_symbol, side, conviction, horizon, rationale, affected_tickers
- monday_gap_bias: GAP_UP | GAP_DOWN | MIXED | FADE
- side: buy | sell
- conviction: integer 0-100
"""


class AnalystAgent:
    def __init__(self, cortex: GeminiCortex, memory: BoardMemory | None = None) -> None:
        self.cortex = cortex
        self.memory = memory
        self.callsign = CALLSIGN

    def ingest_weekend_wire(self) -> list[WeekendTrigger]:
        """Fetch the top 3 live financial RSS headlines. Never returns canned macro."""
        return fetch_live_wire(limit=3)

    def brief(
        self,
        triggers: list[WeekendTrigger],
        universe: list[str] | tuple[str, ...] | str,
        preferred_symbol: str | None = None,
    ) -> AnalystBrief:
        symbols = _normalize_universe(universe, preferred_symbol)
        news_pick = pick_symbol_from_news(triggers, symbols)
        default_symbol = news_pick or (preferred_symbol if preferred_symbol in symbols else symbols[0])
        wire = [t.model_dump() for t in triggers]
        headlines = [t.headline for t in triggers if t.headline]
        mem = self.memory.prompt_block() if self.memory is not None else "BOARD MEMORY: none."
        listed = "\n".join(f"  - {s}" for s in symbols)
        user = (
            f"{mem}\n\n"
            "Live financial RSS wire (newest first). These are real headlines, not desk fiction:\n"
            f"{wire}\n\n"
            "UNIVERSE of Bitget Demo symbols. primary_symbol MUST be one of these exact strings:\n"
            f"{listed}\n\n"
            "Select the single most relevant symbol for this tape "
            "(iPhone→AAPL, AI/GPU→NVDA, Azure/OpenAI→MSFT, Google/Alphabet→GOOG, EV/Tesla→TSLA).\n"
            "Produce the JSON brief now. Do not invent catalysts absent from the wire."
        )
        fallback = {
            "thesis": API_TIMEOUT_VETO,
            "monday_gap_bias": "MIXED",
            "primary_symbol": default_symbol,
            "side": "buy",
            "conviction": 0,
            "horizon": "weekend_to_monday_open",
            "rationale": API_TIMEOUT_VETO,
            "affected_tickers": [],
        }
        try:
            payload, degraded = self.cortex.generate_json(
                _SYSTEM, user, temperature=0.35, fallback=fallback
            )
        except Exception as exc:
            print(f"[API ERROR] {str(exc)}", flush=True)
            payload, degraded = fallback, True
        if degraded:
            err = self.cortex.last_error or "unknown Gemini fault"
            print(f"[API ERROR] ORACLE degraded: {err}", flush=True)
            payload = {
                **fallback,
                **{k: payload.get(k, fallback[k]) for k in fallback},
                "thesis": str(payload.get("thesis") or API_TIMEOUT_VETO),
                "rationale": str(payload.get("rationale") or API_TIMEOUT_VETO),
                "conviction": _clamp_int(payload.get("conviction"), 0),
            }
        return AnalystBrief(
            thesis=str(payload.get("thesis") or fallback["thesis"]),
            monday_gap_bias=_gap(payload.get("monday_gap_bias")),
            primary_symbol=snap_to_universe(
                str(payload.get("primary_symbol") or default_symbol),
                symbols,
                headlines,
            ),
            side=_side(payload.get("side")),
            conviction=_clamp_int(payload.get("conviction"), 0 if degraded else 28),
            horizon=str(payload.get("horizon") or "weekend_to_monday_open"),
            rationale=str(payload.get("rationale") or fallback["rationale"]),
            affected_tickers=_str_list(payload.get("affected_tickers")),
            wire_headlines=headlines,
            llm_degraded=degraded,
            model=self.cortex.model_name,
        )


def fetch_live_wire(limit: int = 3) -> list[WeekendTrigger]:
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
    return raw or ["NVDA/USDT:USDT"]


def _base_ticker(symbol: str) -> str:
    raw = (symbol or "").strip()
    if not raw:
        return ""
    base = raw.split(":")[0].split("/")[0].upper()
    if base.startswith("R") and len(base) > 2 and base[1:].isalpha():
        rest = base[1:]
        if rest in {"NVDA", "AAPL", "TSLA", "MSFT", "GOOG", "GOOGL", "META", "AMZN", "AVGO", "TSM"}:
            base = rest
    if base == "GOOGL":
        return "GOOG"
    return base


def snap_to_universe(
    raw: str,
    universe: list[str] | tuple[str, ...],
    headlines: list[str] | None = None,
) -> str:
    """Clamp an LLM / alias symbol onto the Demo universe. Never returns off-list."""
    symbols = [str(s).strip() for s in universe if str(s).strip()]
    if not symbols:
        return raw or "NVDA/USDT:USDT"
    wanted = (raw or "").strip()
    if wanted in symbols:
        return wanted
    by_base = {_base_ticker(s): s for s in symbols}
    base = _base_ticker(wanted)
    if base and base in by_base:
        return by_base[base]
    news_pick = pick_symbol_from_news(headlines or [], symbols)
    if news_pick:
        return news_pick
    return symbols[0]


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
