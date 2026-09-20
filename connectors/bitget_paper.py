"""Bitget Paper Trading connector.

Sandbox/Demo only for private calls (orders, balances, positions).
`set_sandbox_mode(True)` is the first call after construct on the execution
client. Live trading is refused at init.

Public market data (ticker, L2, OHLCV, markPrice) is fetched from Bitget
MAINNET via a second CCXT client with no keys and no sandbox flag.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt

from core.config import PROJECT_ROOT, Settings
from core.retry import call_with_backoff
from core.ta import (
    BBO_MARK_DIVERGENCE_PCT,
    MTF_FRAMES,
    RSI_FALLBACK_TIMEFRAME,
    RSI_PERIOD,
    RSI_TIMEFRAME,
    analyze_candles,
    bbo_peg_price,
    bbo_sane_vs_mark,
    compute_atr,
    compute_rsi,
    enrich_ohlcv,
    extract_mark_price,
    mtf_alignment,
    sl_tp_from_margin,
)

STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.05
SCALE_TP_FRACTION = 0.5
ATR_SL_MULT = 1.5
ATR_TP_MULT = 2.5
ATR_SL_PCT_FLOOR = 0.012
ATR_SL_PCT_CAP = 0.05
ATR_TP_PCT_FLOOR = 0.03
ATR_TP_PCT_CAP = 0.12
POSITION_OPEN_MSG = "Position already open"
# Bitget USDT-M taker + crossed-margin estimate when the Demo wallet delta is unavailable.
TAKER_FEE_RATE = 0.0006
SWAP_LEVERAGE = 5.0

TRADE_LOG = PROJECT_ROOT / "data" / "logs" / "trades.json"
_LOG_LOCK = threading.Lock()

_SYMBOL_CANDIDATES = (
    "rNVDA/USDT",
    "rAAPL/USDT",
    "rTSLA/USDT",
    "rMSFT/USDT",
    "rGOOGL/USDT",
    "NVDA/USDT",
    "AAPL/USDT",
    "TSLA/USDT",
    "MSFT/USDT",
    "GOOG/USDT",
    "NVDA/USDT:USDT",
    "AAPL/USDT:USDT",
    "TSLA/USDT:USDT",
    "MSFT/USDT:USDT",
    "GOOG/USDT:USDT",
    "BTC/USDT",
    "BTC/USDT:USDT",
)


def _looks_contract(symbol: str) -> bool:
    u = (symbol or "").upper()
    return ":USDT" in u or ":USDC" in u or ":USD" in u or u.endswith(":USDT")


def public_symbol_candidates(symbol: str) -> list[str]:
    """Demo/sandbox symbol → live Bitget unified-symbol tries (spot + swap)."""
    raw = (symbol or "").strip()
    if not raw:
        return []
    out: list[str] = []

    def add(item: str) -> None:
        text = (item or "").strip()
        if text and text not in out:
            out.append(text)

    add(raw)
    u = raw.upper().replace("SUSDT", "USDT").replace("SUSDC", "USDC")
    add(u)
    base_quote = u.split(":")[0]
    add(base_quote)
    if ":" not in u:
        add(f"{base_quote}:USDT")
    else:
        add(u)
    parts = base_quote.split("/")
    base = parts[0] if parts else u
    quote = parts[1] if len(parts) > 1 else "USDT"
    if quote not in {"USDT", "USDC", "USD"}:
        quote = "USDT"
    if base.startswith("S") and base[1:] in _CRYPTO_DENY:
        add(f"{base[1:]}/{quote}")
        add(f"{base[1:]}/{quote}:USDT")
    if base.startswith("R") and len(base) > 2 and base[1:].isalpha():
        add(f"{base[1:]}/{quote}")
        add(f"{base[1:]}/{quote}:USDT")
        add(f"{base}/{quote}")
        add(f"{base}/{quote}:USDT")
    else:
        add(f"{base}/{quote}")
        add(f"{base}/{quote}:USDT")
        add(f"r{base}/{quote}")
        add(f"r{base}/{quote}:USDT")
    return out


def simulate_account_balance_change(
    *,
    side: str,
    notional_usdt: float,
    filled: bool,
    is_swap: bool = True,
) -> float:
    """USDT wallet delta for GitBook `account_balance_change`.

    Swap opens lock initial margin (notional / leverage) plus taker fee.
    Spot buy spends notional + fee; spot sell credits notional − fee.
    Non-fills (veto, stand-down, error) are a zero change.
    """
    if not filled:
        return 0.0
    try:
        notional = abs(float(notional_usdt or 0.0))
    except (TypeError, ValueError):
        notional = 0.0
    if notional <= 0:
        return 0.0
    fee = notional * TAKER_FEE_RATE
    if is_swap:
        margin = notional / SWAP_LEVERAGE
        return round(-(margin + fee), 8)
    side_n = (side or "").lower().strip()
    if side_n == "buy":
        return round(-(notional + fee), 8)
    if side_n == "sell":
        return round(notional - fee, 8)
    return 0.0


def _coin_amt(value: Any) -> float:
    """Wallet free/total cell → finite float ≥ 0. None-safe."""
    if value is None or value == "":
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return 0.0
    return max(0.0, parsed)


def coerce_price(*candidates: Any) -> float:
    """First positive float among scalars or ticker/order dicts. Never returns None."""
    for value in candidates:
        if value is None or value == "":
            continue
        if isinstance(value, dict):
            nested = coerce_price(
                value.get("last"),
                value.get("price"),
                value.get("average"),
                value.get("entry_price"),
            )
            if nested > 0:
                return nested
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0.0


def _stamp_bbo(
    record: dict[str, Any],
    quote: dict[str, Any] | None,
    *,
    as_entry: bool = True,
) -> dict[str, Any]:
    """Paper log / receipts always show the live mainnet BBO peg, not a sandbox mid.

    Opens stamp entry+price. Closes stamp the exit `price` only (keep original entry).
    """
    payload = quote if isinstance(quote, dict) else {}
    peg = coerce_price(payload.get("peg"))
    if peg > 0:
        record["price"] = peg
        if as_entry:
            record["entry_price"] = peg
        record["price_source"] = payload.get("source") or "mainnet_bbo"
    if payload.get("best_bid") is not None:
        record["best_bid"] = payload.get("best_bid")
    if payload.get("best_ask") is not None:
        record["best_ask"] = payload.get("best_ask")
    mark = payload.get("mark")
    if mark not in (None, "", 0, 0.0):
        record["mark_price"] = mark
    if payload.get("public_symbol"):
        record["public_symbol"] = payload.get("public_symbol")
    return record


def stamp_gitbook_log(record: dict[str, Any]) -> dict[str, Any]:
    """Guarantee GitBook paper-log columns on every row. Never leave price/Δ as null."""
    ts = record.get("timestamp") or record.get("ts") or datetime.now(timezone.utc).isoformat()
    instrument = str(record.get("instrument") or record.get("symbol") or "")
    direction = str(record.get("direction") or record.get("side") or "none")
    try:
        if record.get("quantity") is not None:
            quantity = float(record["quantity"])
        else:
            quantity = float(record.get("amount") or 0.0)
    except (TypeError, ValueError):
        quantity = 0.0
    raw_order = record.get("raw_order") if isinstance(record.get("raw_order"), dict) else {}
    price = coerce_price(
        record.get("price"),
        record.get("entry_price"),
        record.get("ticker"),
        raw_order.get("price") if raw_order else None,
        raw_order.get("average") if raw_order else None,
    )
    try:
        notional = float(record.get("notional_usdt") or 0.0)
    except (TypeError, ValueError):
        notional = 0.0
    if notional <= 0 and price > 0 and quantity > 0:
        notional = round(price * quantity, 6)
        record["notional_usdt"] = notional
    filled = bool(record.get("ok"))
    change = record.get("account_balance_change")
    if change is None or change == "":
        symbol = str(record.get("symbol") or instrument)
        change_f = simulate_account_balance_change(
            side=direction,
            notional_usdt=notional,
            filled=filled,
            is_swap=":USDT" in symbol or bool(record.get("is_swap")),
        )
    else:
        try:
            change_f = float(change)
        except (TypeError, ValueError):
            change_f = 0.0

    record["timestamp"] = ts
    record["ts"] = record.get("ts") or ts
    record["instrument"] = instrument
    record["symbol"] = record.get("symbol") or instrument
    record["direction"] = direction
    record["side"] = record.get("side") or direction
    record["quantity"] = quantity
    if record.get("amount") is None:
        record["amount"] = quantity
    record["price"] = price
    entry = coerce_price(record.get("entry_price"), price)
    record["entry_price"] = entry if entry > 0 else price
    record["account_balance_change"] = change_f
    return record


class BitgetPaperConnector:
    def __init__(self, settings: Settings) -> None:
        if not settings.bitget_paper_trading:
            raise RuntimeError("REFUSING: Bitget connector requires BITGET_PAPER_TRADING=true")
        if not settings.bitget_api_key or not settings.bitget_api_secret:
            raise RuntimeError("BITGET_API_KEY / BITGET_API_SECRET missing")

        self.settings = settings
        self.sandbox = True
        self.preferred_symbol = settings.bitget_symbol
        self.resolved_symbol: str | None = None
        self.universe: list[str] = []
        self.universe_source: str = "unloaded"

        exchange = ccxt.bitget(
            {
                "apiKey": settings.bitget_api_key,
                "secret": settings.bitget_api_secret,
                "password": settings.bitget_passphrase,
                "enableRateLimit": True,
                "timeout": 20000,
                "options": {
                    "defaultType": "spot",
                    "sandboxMode": True,
                    # Bitget native-token fee: pay in BGB (20% discount) when the
                    # account switch-deduct endpoint is armed. Unknown order-body
                    # keys are NOT sent on create_order — Bitget rejects them.
                    "deduct": "on",
                },
                "headers": {"PAPTRADING": "1"},
            }
        )
        # Must be the first call after construct — CCXT Demo / PAPTRADING=1.
        exchange.set_sandbox_mode(True)
        self.exchange = exchange
        self._order_lock = threading.RLock()
        self.bgb_fee_deduct: dict[str, Any] = {"ok": False, "deduct": "off", "via": None}
        # Public mainnet feed — no keys, never sandbox. Spot + swap catalogs.
        self.public = ccxt.bitget(
            {
                "enableRateLimit": True,
                "timeout": 20000,
                "options": {
                    "defaultType": "swap",
                    "fetchMarkets": ["spot", "swap"],
                },
            }
        )
        self._public_lock = threading.Lock()
        self._public_ready = False
        self.price_feed = "bitget.mainnet"

    def _ccxt(self, fn, *, label: str = "bitget"):
        return call_with_backoff(fn, attempts=3, label=label)

    def _feed(self):
        """Mainnet public client. Unit tests that skip __init__ fall back to sandbox."""
        return getattr(self, "public", None) or self.exchange

    def _ensure_public_markets(self) -> None:
        client = getattr(self, "public", None)
        if client is None or getattr(self, "_public_ready", False):
            return
        lock = getattr(self, "_public_lock", None)
        if lock is None:
            self._load_public_markets(client)
            return
        with lock:
            if self._public_ready:
                return
            self._load_public_markets(client)

    def _load_public_markets(self, client: Any) -> None:
        prev = (client.options or {}).get("defaultType")
        try:
            # CCXT bitget fetchMarkets defaults to both spot and swap.
            client.options["defaultType"] = "swap"
            self._ccxt(lambda: client.load_markets(reload=False), label="bitget.mainnet.load_markets")
            self._public_ready = True
            n = len(client.markets or {})
            print(f"[BITGET] mainnet price feed armed  markets={n}  spot+swap", flush=True)
        except Exception as exc:
            self._public_ready = False
            print(f"[BITGET] mainnet price feed failed — {str(exc)[:160]}", flush=True)
        finally:
            client.options["defaultType"] = prev or "swap"

    def _resolve_public_symbol(self, symbol: str) -> str:
        raw = (symbol or "").strip()
        if not raw:
            return raw
        client = self._feed()
        markets = getattr(client, "markets", None) or {}
        if not markets and client is getattr(self, "public", None):
            try:
                self._ensure_public_markets()
                markets = client.markets or {}
            except Exception:
                markets = {}
        for cand in public_symbol_candidates(raw):
            if cand in markets:
                return cand
        root = raw.upper().split(":")[0].split("/")[0]
        if root.startswith("R") and len(root) > 2:
            root = root[1:]
        hits: list[str] = []
        for name, market in (markets or {}).items():
            row = market if isinstance(market, dict) else {}
            base = str(row.get("base") or str(name).split("/")[0] or "").upper()
            base_root = base[1:] if base.startswith("R") and len(base) > 2 else base
            if base == root or base_root == root:
                hits.append(str(name))
        if not hits:
            return raw
        hits.sort(
            key=lambda n: (
                0 if n.endswith(":USDT") else 1,
                0 if n.endswith("/USDT:USDT") or n.endswith("/USDT") else 1,
                n,
            )
        )
        return hits[0]

    def _feed_type(self, symbol: str) -> str:
        if _looks_contract(symbol):
            return "swap"
        client = self._feed()
        try:
            market = client.market(symbol)
            if market.get("swap") or market.get("future"):
                return "swap"
        except Exception:
            pass
        return "spot"

    def ping(self) -> dict[str, Any]:
        try:
            markets = self._ccxt(lambda: self.exchange.load_markets(reload=False), label="bitget.load_markets")
        except Exception:
            markets = self._ccxt(lambda: self.exchange.load_markets(reload=True), label="bitget.load_markets.reload")
        self.universe = self.discover_equity_universe(markets)
        self.resolved_symbol = self._resolve_symbol(markets)
        market = markets.get(self.resolved_symbol) or {}
        if market.get("swap") or market.get("future"):
            self.exchange.options["defaultType"] = "swap"
        else:
            self.exchange.options["defaultType"] = "spot"
        self.bgb_fee_deduct = self._enable_bgb_fee_deduct()
        self._ensure_public_markets()
        public_n = len((getattr(self, "public", None).markets or {}) if getattr(self, "public", None) else {})
        return {
            "sandbox": True,
            "paptrading": "1",
            "markets": len(markets),
            "universe": len(self.universe),
            "universe_source": self.universe_source,
            "symbol": self.resolved_symbol,
            "market_type": "swap" if market.get("swap") else "spot",
            "id": self.exchange.id,
            "bgb_fee_deduct": self.bgb_fee_deduct.get("deduct") or "off",
            "bgb_fee_deduct_via": self.bgb_fee_deduct.get("via"),
            "price_feed": getattr(self, "price_feed", "bitget.mainnet"),
            "public_markets": public_n,
        }

    def _enable_bgb_fee_deduct(self) -> dict[str, Any]:
        """Pay trading fees in BGB (Bitget native token) via the official switch.

        Account-level, not a per-order header: POST /api/v3/account/switch-deduct
        (UTA) and POST /api/v2/spot/account/switch-deduct (classic spot).
        Fail-open — Demo / missing UTA must never block the rail.
        """
        payload = {"deduct": "on"}
        errors: list[str] = []
        uta = getattr(self.exchange, "private_uta_post_v3_account_switch_deduct", None)
        if callable(uta):
            try:
                uta(payload)
                print("[BITGET] fee deduct in BGB armed (uta v3 switch-deduct)", flush=True)
                return {"ok": True, "deduct": "on", "via": "uta_v3"}
            except Exception as exc:
                errors.append(f"uta_v3:{str(exc)[:120]}")
        try:
            self.exchange.request(
                "v2/spot/account/switch-deduct",
                ["private", "spot"],
                "POST",
                payload,
            )
            print("[BITGET] fee deduct in BGB armed (spot v2 switch-deduct)", flush=True)
            return {"ok": True, "deduct": "on", "via": "spot_v2"}
        except Exception as exc:
            errors.append(f"spot_v2:{str(exc)[:120]}")
        note = " | ".join(errors)[:240] if errors else "unsupported"
        print(f"[BITGET] fee deduct in BGB skipped — {note}", flush=True)
        return {"ok": False, "deduct": "off", "via": None, "error": note}

    def discover_equity_universe(self, markets: dict[str, Any] | None = None) -> list[str]:
        """Live CCXT catalog of Demo equity / stock-perp / rToken listings. No hardcoded cap."""
        book = markets
        if not book:
            try:
                book = self._ccxt(
                    lambda: self.exchange.load_markets(reload=False),
                    label="bitget.load_markets.universe",
                )
            except Exception:
                book = self.exchange.markets or {}
        equity = _select_equity_universe(book or {})
        if equity:
            self.universe_source = "equity"
            self.universe = equity
            return list(equity)
        fallback = _usdt_fallback_universe(book or {})
        self.universe_source = "usdt-fallback" if fallback else "empty"
        self.universe = fallback
        return list(fallback)

    def fetch_equity_universe(self, reload: bool = False) -> list[str]:
        if reload or not (self.exchange.markets or {}):
            try:
                markets = self._ccxt(
                    lambda: self.exchange.load_markets(reload=bool(reload)),
                    label="bitget.load_markets.universe.reload",
                )
            except Exception:
                markets = self.exchange.markets or {}
            return self.discover_equity_universe(markets)
        if self.universe:
            return list(self.universe)
        return self.discover_equity_universe(self.exchange.markets or {})

    def fetch_demo_balance(self) -> dict[str, Any]:
        assets: dict[str, dict[str, float]] = {}
        errors: list[str] = []
        for account_type in ("spot", "swap"):
            try:
                raw = self._ccxt(
                    lambda t=account_type: self.exchange.fetch_balance({"type": t}),
                    label=f"bitget.balance.{account_type}",
                )
            except Exception as exc:
                errors.append(f"{account_type}:{str(exc)[:120]}")
                continue
            totals = raw.get("total") or {}
            frees = raw.get("free") or {}
            for coin, total in totals.items():
                try:
                    total_f = float(total or 0)
                except (TypeError, ValueError):
                    continue
                try:
                    free_f = float(frees.get(coin) or 0)
                except (TypeError, ValueError):
                    free_f = 0.0
                if coin in {"USDT", "USDC"} or total_f > 0:
                    prev = assets.get(str(coin), {"free": 0.0, "total": 0.0})
                    assets[str(coin)] = {
                        "free": prev["free"] + free_f,
                        "total": prev["total"] + total_f,
                    }
        return {
            "ok": bool(assets) or not errors,
            "sandbox": True,
            "assets": assets,
            "error": " | ".join(errors)[:240] if errors and not assets else None,
        }

    def resolve_spot_symbol(self, query: str) -> str | None:
        """Any live Bitget Spot listing matching the query. Never returns a swap/perp."""
        raw = (query or "").strip()
        if not raw:
            return None
        try:
            markets = self.exchange.markets or self._ccxt(
                lambda: self.exchange.load_markets(reload=False),
                label="bitget.load_markets.spot",
            )
        except Exception:
            markets = self.exchange.markets or {}
        q = raw.upper().replace("USDT:USDT", "USDT")
        if q.endswith(":USDT"):
            q = q[: -len(":USDT")]
        candidates: list[str] = []
        if "/" in q:
            candidates.append(q)
            base = q.split("/")[0]
            quote = q.split("/")[1] if "/" in q else "USDT"
            if quote not in {"USDT", "USDC", "USD"}:
                candidates.append(f"{base}/USDT")
        else:
            candidates.extend([f"{q}/USDT", f"{q}/USDC", f"{q}/USD"])
        for cand in candidates:
            market = markets.get(cand) if isinstance(markets, dict) else None
            if _is_spot_market(cand, market if isinstance(market, dict) else {}):
                return cand
        root = q.split("/")[0]
        alt = root[1:] if root.startswith("R") and len(root) > 2 and root[1:].isalpha() else root
        if alt == "GOOGL":
            alt = "GOOG"
        hits: list[str] = []
        for name, market in (markets or {}).items():
            row = market if isinstance(market, dict) else {}
            if not _is_spot_market(str(name), row):
                continue
            base = str(row.get("base") or str(name).split("/")[0] or "").upper()
            base_root = base[1:] if base.startswith("R") and len(base) > 2 else base
            if base_root == "GOOGL":
                base_root = "GOOG"
            if base == root or base_root == alt or base == alt:
                hits.append(str(name))
        hits.sort(key=lambda n: (0 if str(n).endswith("/USDT") else 1, n))
        return hits[0] if hits else None

    def fetch_spot_usdt_free(self) -> float:
        """Spot wallet free USDT via fetch_balance(type=spot)."""
        try:
            raw = self._ccxt(
                lambda: self.exchange.fetch_balance({"type": "spot"}),
                label="bitget.balance.spot.chat",
            )
        except Exception:
            return 0.0
        frees = raw.get("free") or {}
        try:
            return max(0.0, float(frees.get("USDT") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def fetch_spot_quote(self, symbol: str, side: str = "buy") -> dict[str, Any]:
        """Live mainnet Spot BBO. Isolated from the swap defaultType used by the AI desk."""
        resolved = self.resolve_spot_symbol(symbol) or symbol
        quote = self.fetch_bbo(resolved, side=side)
        last = coerce_price(
            quote.get("peg"),
            quote.get("last"),
            quote.get("best_ask"),
            quote.get("best_bid"),
        )
        return {
            "ok": last > 0,
            "symbol": str(quote.get("public_symbol") or quote.get("symbol") or resolved),
            "last": last,
            "bid": quote.get("best_bid"),
            "ask": quote.get("best_ask"),
            "mark": quote.get("mark"),
            "peg": coerce_price(quote.get("peg"), last) if last > 0 else None,
            "price_source": quote.get("source") or "mainnet_bbo",
            "error": None if last > 0 else (quote.get("error") or "No live Bitget mainnet price."),
        }

    def fetch_live_price(self, symbol: str) -> dict[str, Any]:
        """Mainnet last/bid/ask for /price. Spot first, then any public listing."""
        resolved = self.resolve_spot_symbol(symbol) or (symbol or "").strip()
        quote = self.fetch_bbo(resolved, side="buy")
        last = coerce_price(
            quote.get("last"),
            quote.get("peg"),
            quote.get("best_ask"),
            quote.get("best_bid"),
            quote.get("mark"),
        )
        return {
            "ok": last > 0,
            "symbol": str(quote.get("public_symbol") or quote.get("symbol") or resolved),
            "last": last if last > 0 else None,
            "bid": quote.get("best_bid"),
            "ask": quote.get("best_ask"),
            "mark": quote.get("mark"),
            "source": quote.get("source") or "bitget.mainnet",
            "error": None if last > 0 else (quote.get("error") or f"No live mainnet price for {symbol}."),
        }

    def fetch_spot_wallet(self, coins: tuple[str, ...] = ("USDT", "BGB")) -> dict[str, Any]:
        """Spot wallet free/total for named coins. None-safe; missing coins are 0."""
        try:
            raw = self._ccxt(
                lambda: self.exchange.fetch_balance({"type": "spot"}),
                label="bitget.balance.spot.wallet",
            )
        except Exception as exc:
            return {"ok": False, "assets": {}, "error": str(exc)[:240]}
        frees = raw.get("free") if isinstance(raw.get("free"), dict) else {}
        totals = raw.get("total") if isinstance(raw.get("total"), dict) else {}
        assets: dict[str, dict[str, float]] = {}
        for coin in coins:
            key = str(coin or "").upper()
            if not key:
                continue
            try:
                free_f = float(frees.get(key) if frees.get(key) is not None else 0.0)
            except (TypeError, ValueError):
                free_f = 0.0
            try:
                total_f = float(totals.get(key) if totals.get(key) is not None else 0.0)
            except (TypeError, ValueError):
                total_f = 0.0
            assets[key] = {"free": max(0.0, free_f), "total": max(0.0, total_f)}
        return {"ok": True, "assets": assets, "error": None}

    def fetch_asset_balance(self, coin: str) -> dict[str, Any]:
        """Free/total for one asset across Spot + swap. Any CCXT coin key, case-insensitive."""
        wanted = str(coin or "").strip()
        if not wanted:
            return {
                "ok": False,
                "coin": "",
                "free": 0.0,
                "total": 0.0,
                "found": False,
                "error": "coin required",
            }
        wanted_u = wanted.upper()
        free_sum = 0.0
        total_sum = 0.0
        found = False
        matched = wanted_u
        errors: list[str] = []
        for account_type in ("spot", "swap"):
            try:
                raw = self._ccxt(
                    lambda t=account_type: self.exchange.fetch_balance({"type": t}),
                    label=f"bitget.balance.{account_type}.asset",
                )
            except Exception as exc:
                errors.append(f"{account_type}:{str(exc)[:120]}")
                continue
            frees = raw.get("free") if isinstance(raw.get("free"), dict) else {}
            totals = raw.get("total") if isinstance(raw.get("total"), dict) else {}
            keys = {str(k) for k in list(frees.keys()) + list(totals.keys())}
            for key in keys:
                if key.upper() != wanted_u:
                    continue
                found = True
                matched = key
                free_sum += _coin_amt(frees.get(key))
                total_sum += _coin_amt(totals.get(key))
        return {
            "ok": True if found or not errors else False,
            "coin": matched,
            "free": max(0.0, free_sum),
            "total": max(0.0, total_sum),
            "found": found,
            "error": " | ".join(errors)[:240] if errors and not found else None,
        }

    def execute_spot_market(
        self,
        symbol: str,
        side: str,
        quote_usdt: float,
    ) -> dict[str, Any]:
        """Manual Telegram fill. Spot market only. Never leverage, never SL/TP, never swap."""
        if not self.sandbox:
            raise RuntimeError("REFUSING live spot order — sandbox lock tripped")
        side_n = (side or "").lower().strip()
        if side_n not in {"buy", "sell"}:
            return {"ok": False, "status": "ERROR", "error": "side must be buy or sell", "symbol": symbol}
        try:
            cost = float(quote_usdt)
        except (TypeError, ValueError):
            cost = 0.0
        if cost <= 0:
            return {"ok": False, "status": "ERROR", "error": "USDT amount must be positive", "symbol": symbol}
        resolved = self.resolve_spot_symbol(symbol)
        if not resolved:
            return {
                "ok": False,
                "status": "NOT_SPOT",
                "error": f"{symbol} is not listed on Bitget Spot. Manual chat trades are SPOT only.",
                "symbol": symbol,
            }
        try:
            market = self.exchange.market(resolved)
        except Exception:
            market = {}
        if not _is_spot_market(resolved, market if isinstance(market, dict) else {}):
            return {
                "ok": False,
                "status": "NOT_SPOT",
                "error": f"{resolved} is not a Spot market. Manual chat trades cannot use futures/margin.",
                "symbol": resolved,
            }

        prev = (self.exchange.options or {}).get("defaultType")
        balance_before: float | None = None
        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "venue": "bitget-demo",
            "sandbox": True,
            "live_trading": False,
            "source": "telegram_spot_manual",
            "market_type": "spot",
            "symbol": resolved,
            "side": side_n,
            "notional_usdt": round(cost, 6),
        }
        try:
            self.exchange.options["defaultType"] = "spot"
            quote = self.fetch_spot_quote(resolved, side=side_n)
            last = coerce_price(quote.get("peg"), quote.get("last"))
            if last <= 0:
                record.update(
                    {
                        "ok": False,
                        "status": "NO_LIVE_PRICE",
                        "error": "No live Bitget mainnet Spot BBO.",
                        "amount": 0.0,
                        "price": 0.0,
                    }
                )
                log_path = _append_trade(record)
                record["log_path"] = str(log_path)
                return record
            qty = self._size_amount(resolved, cost / last, last)
            record["amount"] = qty
            record["quantity"] = qty
            record["price"] = last
            record["entry_price"] = last
            _stamp_bbo(record, quote)
            free = self.fetch_spot_usdt_free()
            if side_n == "buy" and free + 1e-9 < cost:
                record.update(
                    {
                        "ok": False,
                        "status": "INSUFFICIENT_MARGIN",
                        "error": f"Spot USDT free {free:.4f} < {cost:.4f} requested.",
                    }
                )
                log_path = _append_trade(record)
                record["log_path"] = str(log_path)
                return record
            balance_before = self._snapshot_usdt()
            order = self._place_spot_market(resolved, side_n, qty, last, cost)
            demo_fill = coerce_price(order.get("average"), order.get("price"))
            fill = last if last > 0 else demo_fill
            filled_qty = coerce_price(order.get("filled"), order.get("amount"), qty)
            record.update(
                {
                    "ok": True,
                    "status": str(order.get("status") or "closed"),
                    "order_id": order.get("id"),
                    "raw_order": _slim_order(order),
                    "price": fill,
                    "entry_price": fill,
                    "demo_fill_price": demo_fill if demo_fill > 0 else None,
                    "amount": filled_qty,
                    "quantity": filled_qty,
                    "notional_usdt": round(abs(fill * filled_qty), 6) if fill and filled_qty else round(cost, 6),
                }
            )
            _stamp_bbo(record, quote)
            self._apply_balance_change(record, balance_before=balance_before, filled=True)
        except Exception as exc:
            record.update(
                {
                    "ok": False,
                    "status": "ERROR",
                    "error": str(exc)[:400],
                    "order_id": None,
                    "raw_order": {},
                }
            )
            self._apply_balance_change(record, balance_before=None, filled=False)
        finally:
            self.exchange.options["defaultType"] = prev
        log_path = _append_trade(record)
        record["log_path"] = str(log_path)
        return record

    def _place_spot_market(
        self,
        symbol: str,
        side: str,
        amount: float,
        last: float,
        cost: float,
    ) -> dict[str, Any]:
        params = {"type": "spot"}
        if side == "buy" and hasattr(self.exchange, "create_market_buy_order_with_cost"):
            try:
                return self._ccxt(
                    lambda: self.exchange.create_market_buy_order_with_cost(symbol, cost, params),
                    label="bitget.spot.buy_cost",
                )
            except TypeError:
                try:
                    return self._ccxt(
                        lambda: self.exchange.create_market_buy_order_with_cost(symbol, cost),
                        label="bitget.spot.buy_cost.noparams",
                    )
                except Exception:
                    pass
            except Exception:
                pass
        return self._ccxt(
            lambda: self.exchange.create_order(symbol, "market", side, amount, None, params),
            label="bitget.spot.create_order",
        )

    def fetch_account_equity(self) -> dict[str, Any]:
        """Live USDT equity via CCXT fetch_balance. Caps the 5–6% daily risk budget."""
        payload = self.fetch_demo_balance()
        usdt = (payload.get("assets") or {}).get("USDT") or {}
        equity = 0.0
        for key in ("total", "free"):
            try:
                value = float(usdt.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            if value > equity:
                equity = value
        return {
            "ok": bool(payload.get("ok")) and equity > 0,
            "equity_usdt": round(equity, 6) if equity > 0 else None,
            "source": "ccxt.fetch_balance",
            "error": payload.get("error"),
        }

    def _snapshot_usdt(self) -> float | None:
        """Live Demo USDT total. None if the wallet call fails — caller simulates Δ."""
        try:
            payload = self.fetch_demo_balance()
            usdt = (payload.get("assets") or {}).get("USDT") or {}
            for key in ("total", "free"):
                try:
                    value = float(usdt.get(key))
                except (TypeError, ValueError):
                    continue
                if value >= 0:
                    return value
        except Exception:
            return None
        return None

    def _apply_balance_change(
        self,
        record: dict[str, Any],
        *,
        balance_before: float | None,
        filled: bool,
    ) -> None:
        """Set account_balance_change from wallet delta, else margin+fee simulation."""
        record["account_balance_before"] = balance_before
        if filled:
            after = self._snapshot_usdt()
            record["account_balance_after"] = after
            if balance_before is not None and after is not None:
                record["account_balance_change"] = round(after - balance_before, 8)
                record["account_balance_change_source"] = "demo_wallet"
                return
        else:
            record["account_balance_after"] = balance_before
        try:
            notional = float(record.get("notional_usdt") or 0.0)
        except (TypeError, ValueError):
            notional = 0.0
        symbol = str(record.get("symbol") or "")
        try:
            is_swap = self._is_swap(symbol) if symbol else True
        except Exception:
            is_swap = ":USDT" in symbol
        change = simulate_account_balance_change(
            side=str(record.get("side") or ""),
            notional_usdt=notional,
            filled=filled,
            is_swap=is_swap,
        )
        record["account_balance_change"] = change
        record["account_balance_change_source"] = "simulated_margin_fee" if filled else "none"
        if filled and balance_before is not None and record.get("account_balance_after") is None:
            record["account_balance_after"] = round(balance_before + change, 8)

    def fetch_ticker(self, symbol: str | None = None) -> dict[str, Any]:
        """Live Bitget MAINNET ticker (spot or swap). Never a sandbox print."""
        target = symbol or self.resolved_symbol or self.preferred_symbol
        client = self._feed()
        live = self._resolve_public_symbol(target)
        dtype = self._feed_type(live)
        prev = (client.options or {}).get("defaultType")
        try:
            client.options["defaultType"] = dtype
            ticker = self._ccxt(lambda: client.fetch_ticker(live), label="bitget.mainnet.ticker")
            mark = extract_mark_price(ticker)
            return {
                "ok": True,
                "mocked": False,
                "symbol": ticker.get("symbol") or live or target,
                "public_symbol": live,
                "last": ticker.get("last"),
                "bid": ticker.get("bid"),
                "ask": ticker.get("ask"),
                "mark": mark,
                "index": extract_mark_price(ticker.get("index"), ticker.get("indexPrice")) if isinstance(ticker, dict) else None,
                "percentage": ticker.get("percentage"),
                "quoteVolume": ticker.get("quoteVolume"),
                "datetime": ticker.get("datetime"),
                "source": "bitget.mainnet",
                "market_type": dtype,
            }
        except Exception as exc:
            return {
                "ok": False,
                "mocked": False,
                "symbol": target,
                "public_symbol": live,
                "last": None,
                "bid": None,
                "ask": None,
                "mark": None,
                "percentage": None,
                "quoteVolume": None,
                "datetime": datetime.now(timezone.utc).isoformat(),
                "source": "bitget.mainnet",
                "error": str(exc)[:240],
            }
        finally:
            client.options["defaultType"] = prev

    def fetch_order_book(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Live MAINNET L2 book. Never invents bids/asks. Never uses the sandbox book."""
        target = symbol or self.resolved_symbol or self.preferred_symbol
        client = self._feed()
        live = self._resolve_public_symbol(target)
        dtype = self._feed_type(live)
        prev = (client.options or {}).get("defaultType")
        try:
            client.options["defaultType"] = dtype
            book = self._ccxt(
                lambda: client.fetch_order_book(live, limit),
                label="bitget.mainnet.order_book",
            )
            payload = _book_payload(
                live or target,
                list(book.get("bids") or []),
                list(book.get("asks") or []),
                ok=True,
                source="mainnet_l2",
                error=None,
            )
            payload["public_symbol"] = live
            payload["market_type"] = dtype
            return payload
        except Exception as exc:
            ticker = self.fetch_ticker(target)
            bid = ticker.get("bid")
            ask = ticker.get("ask")
            bids: list[list[float]] = []
            asks: list[list[float]] = []
            try:
                bid_f = float(bid) if bid is not None else 0.0
                ask_f = float(ask) if ask is not None else 0.0
            except (TypeError, ValueError):
                bid_f = ask_f = 0.0
            if bid_f > 0 and ask_f > 0:
                bids = [[bid_f, 0.0]]
                asks = [[ask_f, 0.0]]
            payload = _book_payload(
                live or target,
                bids,
                asks,
                ok=bool(bids and asks),
                source="mainnet_ticker",
                error=str(exc)[:240],
            )
            payload["public_symbol"] = live
            payload["market_type"] = dtype
            return payload
        finally:
            client.options["defaultType"] = prev

    def fetch_bbo(self, symbol: str, side: str = "") -> dict[str, Any]:
        """Mainnet best bid/offer peg. BUY=ask, SELL/CLOSE=bid. Contracts vs markPrice."""
        target = symbol or self.resolved_symbol or self.preferred_symbol or ""
        book = self.fetch_order_book(target)
        ticker = self.fetch_ticker(target)
        bid = coerce_price(book.get("best_bid"), ticker.get("bid"))
        ask = coerce_price(book.get("best_ask"), ticker.get("ask"))
        mark = coerce_price(extract_mark_price(ticker, ticker.get("mark"), book))
        last = coerce_price(ticker.get("last"), mark, ask, bid)
        peg = bbo_peg_price(side, bid if bid > 0 else None, ask if ask > 0 else None)
        peg_f = coerce_price(peg)
        if peg_f <= 0:
            peg_f = last
            peg = peg_f if peg_f > 0 else None
        live = str(ticker.get("public_symbol") or book.get("public_symbol") or target)
        is_contract = _looks_contract(live) or _looks_contract(target)
        try:
            is_contract = is_contract or self._is_swap(target)
        except Exception:
            pass
        error = None
        if last <= 0 and peg_f <= 0:
            error = "NO_LIVE_BBO"
        elif peg_f > 0 and not bbo_sane_vs_mark(peg=peg_f, mark=mark if mark > 0 else None, is_contract=is_contract):
            error = "MARK_DIVERGENCE"
        div = None
        if peg_f > 0 and mark > 0:
            div = round(abs(peg_f - mark) / mark * 100.0, 4)
        return {
            "ok": error is None,
            "source": "mainnet_bbo",
            "symbol": target,
            "public_symbol": live,
            "side": (side or "").lower().strip(),
            "best_bid": bid if bid > 0 else None,
            "best_ask": ask if ask > 0 else None,
            "mark": mark if mark > 0 else None,
            "last": last if last > 0 else None,
            "peg": peg_f if peg_f > 0 else None,
            "is_contract": is_contract,
            "spread_pct": book.get("spread_pct"),
            "mark_divergence_pct": div,
            "error": error,
        }

    def fetch_open_position(self, symbol: str | None = None) -> dict[str, Any]:
        """Live CCXT positions for the target. Never invents an open book."""
        target = symbol or self.resolved_symbol or self.preferred_symbol
        errors: list[str] = []
        positions: list[dict[str, Any]] = []
        try:
            raw = self._ccxt(
                lambda: self.exchange.fetch_positions([target]),
                label="bitget.fetch_positions",
            )
            positions = list(raw or [])
        except Exception as exc:
            errors.append(str(exc)[:160])
            try:
                raw = self._ccxt(lambda: self.exchange.fetch_positions(), label="bitget.fetch_positions.all")
                positions = list(raw or [])
            except Exception as exc2:
                errors.append(str(exc2)[:160])

        for pos in positions:
            if not _position_is_open(pos, target):
                continue
            return {
                "open": True,
                "source": "positions",
                "symbol": target,
                "side": pos.get("side"),
                "contracts": _position_contracts(pos),
                "entry_price": pos.get("entryPrice") or pos.get("markPrice"),
                "raw": _slim_position(pos),
                "error": None,
            }

        spot = self._spot_base_holding(target)
        if spot.get("open"):
            return spot
        return {
            "open": False,
            "source": "positions",
            "symbol": target,
            "side": None,
            "contracts": 0.0,
            "entry_price": None,
            "raw": {},
            "error": " | ".join(errors)[:240] if errors else None,
        }

    def execute_paper_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        reasoning_hash: str,
        extra: dict[str, Any] | None = None,
        sl_price: float | None = None,
        tp_price: float | None = None,
        margin_usdt: float | None = None,
        sl_margin_frac: float | None = None,
    ) -> dict[str, Any]:
        with self._order_lock:
            return self._execute_paper_order(
                symbol,
                side,
                amount,
                reasoning_hash,
                extra,
                sl_price=sl_price,
                tp_price=tp_price,
                margin_usdt=margin_usdt,
                sl_margin_frac=sl_margin_frac,
            )

    def _execute_paper_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        reasoning_hash: str,
        extra: dict[str, Any] | None = None,
        sl_price: float | None = None,
        tp_price: float | None = None,
        margin_usdt: float | None = None,
        sl_margin_frac: float | None = None,
    ) -> dict[str, Any]:
        if not self.sandbox:
            raise RuntimeError("REFUSING live order — sandbox lock tripped")

        side_n = side.lower().strip()
        if side_n not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")

        try:
            existing = self.fetch_open_position(symbol)
        except Exception as exc:
            existing = {"open": False, "error": str(exc)[:160]}
        if existing.get("open"):
            pos_px = coerce_price(
                existing.get("entry_price"),
                existing.get("markPrice"),
                (existing.get("raw") or {}).get("entryPrice")
                if isinstance(existing.get("raw"), dict)
                else None,
            )
            record = {
                "id": str(uuid.uuid4()),
                "ts": datetime.now(timezone.utc).isoformat(),
                "venue": "bitget-demo",
                "sandbox": True,
                "live_trading": False,
                "symbol": symbol,
                "side": side_n,
                "amount": 0.0,
                "notional_usdt": 0.0,
                "reasoning_hash": reasoning_hash,
                "ok": False,
                "status": "POSITION_ALREADY_OPEN",
                "order_id": None,
                "raw_order": {},
                "error": POSITION_OPEN_MSG,
                "position": existing,
                "price": pos_px,
                "entry_price": pos_px,
                "account_balance_change": 0.0,
                "account_balance_change_source": "none",
                "sl_price": None,
                "tp_price": None,
            }
            if extra:
                record["board"] = extra
            log_path = _append_trade(record)
            record["log_path"] = str(log_path)
            return record

        quote = self.fetch_bbo(symbol, side=side_n)
        last = coerce_price(quote.get("peg"), quote.get("mark"), quote.get("last"))
        if last <= 0 or not quote.get("ok"):
            status = str(quote.get("error") or "NO_LIVE_PRICE")
            if status == "MARK_DIVERGENCE":
                err = (
                    f"Live BBO diverges from markPrice "
                    f"(peg={quote.get('peg')} mark={quote.get('mark')} "
                    f"div={quote.get('mark_divergence_pct')}% > {BBO_MARK_DIVERGENCE_PCT}%)."
                )
            else:
                err = "No live mainnet BBO — refusing to size a dummy or sandbox ticket."
            record = {
                "id": str(uuid.uuid4()),
                "ts": datetime.now(timezone.utc).isoformat(),
                "venue": "bitget-demo",
                "sandbox": True,
                "live_trading": False,
                "symbol": symbol,
                "side": side_n,
                "amount": 0.0,
                "notional_usdt": 0.0,
                "reasoning_hash": reasoning_hash,
                "ok": False,
                "status": status if status in {"NO_LIVE_PRICE", "NO_LIVE_BBO", "MARK_DIVERGENCE"} else "NO_LIVE_PRICE",
                "order_id": None,
                "raw_order": {},
                "error": err,
                "ticker": {
                    k: quote.get(k)
                    for k in ("last", "best_bid", "best_ask", "mark", "peg", "source")
                },
                "price": 0.0,
                "entry_price": 0.0,
                "account_balance_change": 0.0,
                "account_balance_change_source": "none",
                "sl_price": None,
                "tp_price": None,
            }
            _stamp_bbo(record, quote)
            if extra:
                record["board"] = extra
            log_path = _append_trade(record)
            record["log_path"] = str(log_path)
            return record
        sized = self._size_amount(symbol, amount, last)
        notional = round(sized * last, 6)

        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "venue": "bitget-demo",
            "sandbox": True,
            "live_trading": False,
            "symbol": symbol,
            "side": side_n,
            "amount": sized,
            "notional_usdt": notional,
            "reasoning_hash": reasoning_hash,
            "ticker": {
                k: quote.get(k)
                for k in ("last", "best_bid", "best_ask", "mark", "peg", "source", "public_symbol")
            },
            "price": last,
            "entry_price": last,
            "sl_price": None,
            "tp_price": None,
            "sl_order": None,
            "tp_order": None,
            "margin_usdt": margin_usdt,
            "sl_margin_frac": sl_margin_frac,
        }
        _stamp_bbo(record, quote)
        if extra:
            record["board"] = extra

        balance_before = self._snapshot_usdt()
        try:
            order = self._place(
                symbol,
                side_n,
                sized,
                last,
                sl_price=sl_price,
                tp_price=tp_price,
                margin_usdt=margin_usdt,
                sl_margin_frac=sl_margin_frac,
            )
            record["ok"] = True
            record["status"] = str(order.get("status") or "submitted")
            record["order_id"] = order.get("id")
            record["raw_order"] = _slim_order(order)
            demo_fill = coerce_price(order.get("average"), order.get("price"))
            entry = last if last > 0 else demo_fill
            record["demo_fill_price"] = demo_fill if demo_fill > 0 else None
            record["entry_price"] = entry
            record["price"] = entry
            record["notional_usdt"] = round(sized * entry, 6) if entry > 0 else notional
            _stamp_bbo(record, quote)
            if margin_usdt is None and record["notional_usdt"]:
                try:
                    lev = SWAP_LEVERAGE if self._is_swap(symbol) else 1.0
                except Exception:
                    lev = SWAP_LEVERAGE
                record["margin_usdt"] = round(float(record["notional_usdt"]) / lev, 6)
            self._apply_balance_change(record, balance_before=balance_before, filled=True)
            if entry > 0:
                try:
                    guards = self._place_sl_tp(
                        symbol,
                        sized,
                        entry,
                        side=side_n,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        margin_usdt=record.get("margin_usdt"),
                        sl_margin_frac=sl_margin_frac,
                    )
                    record.update(guards)
                    record["price"] = entry
                    record["entry_price"] = entry
                except Exception as guard_exc:
                    sl, tp = protective_prices(
                        entry,
                        side_n,
                        margin_usdt=record.get("margin_usdt"),
                        qty=sized,
                        sl_margin_frac=sl_margin_frac,
                    )
                    record["sl_price"] = sl
                    record["tp_price"] = tp
                    record["sl_error"] = str(guard_exc)[:240]
                    record["tp_error"] = str(guard_exc)[:240]
        except Exception as exc:
            msg = str(exc)
            record["ok"] = False
            record["order_id"] = None
            record["raw_order"] = {}
            record["price"] = last
            record["entry_price"] = last
            self._apply_balance_change(record, balance_before=balance_before, filled=False)
            if _is_margin_error(msg):
                record["status"] = "INSUFFICIENT_MARGIN"
                record["error"] = (
                    "Bitget Demo wallet has no virtual USDT. "
                    "Open Bitget → Demo Trading and claim demo funds, then re-run."
                )
            else:
                record["status"] = "ERROR"
                record["error"] = msg[:400]

        log_path = _append_trade(record)
        record["log_path"] = str(log_path)
        return record

    def log_stand_down(self, extra: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "venue": "bitget-demo",
            "sandbox": True,
            "live_trading": False,
            "ok": False,
            "status": "VETOED",
            "symbol": extra.get("symbol"),
            "side": extra.get("side"),
            "amount": 0.0,
            "quantity": 0.0,
            "price": 0.0,
            "entry_price": 0.0,
            "notional_usdt": 0.0,
            "account_balance_change": 0.0,
            "account_balance_change_source": "none",
            "reasoning_hash": extra.get("reasoning_hash"),
            "board": extra.get("board"),
        }
        log_path = _append_trade(record)
        record["log_path"] = str(log_path)
        return record

    def _place(
        self,
        symbol: str,
        side: str,
        amount: float,
        last: float,
        sl_price: float | None = None,
        tp_price: float | None = None,
        margin_usdt: float | None = None,
        sl_margin_frac: float | None = None,
    ) -> dict[str, Any]:
        is_swap = self._is_swap(symbol)
        if is_swap:
            try:
                self._ccxt(lambda: self.exchange.set_leverage(5, symbol), label="bitget.leverage")
            except Exception:
                pass
            sl, tp = protective_prices(
                last,
                side,
                atr=None if sl_price else self.fetch_atr(symbol),
                margin_usdt=margin_usdt,
                qty=amount,
                sl_margin_frac=sl_margin_frac,
                leverage=SWAP_LEVERAGE,
            )
            if sl_price:
                sl = float(sl_price)
            if tp_price:
                tp = float(tp_price)
            sl_px = self._price(symbol, sl)
            tp_px = self._price(symbol, tp)
            param_sets: list[dict[str, Any]] = [
                {
                    "marginMode": "crossed",
                    "tradeSide": "open",
                    "hedged": True,
                    "stopLossPrice": sl_px,
                    "takeProfitPrice": tp_px,
                    "presetStopLossPrice": sl_px,
                    "presetTakeProfitPrice": tp_px,
                },
                {
                    "marginMode": "crossed",
                    "tradeSide": "open",
                    "hedged": True,
                    "stopLossPrice": sl_px,
                    "takeProfitPrice": tp_px,
                },
                {"marginMode": "crossed", "tradeSide": "open", "hedged": True},
            ]
            last_exc: Exception | None = None
            for params in param_sets:
                try:
                    return self._ccxt(
                        lambda p=params: self.exchange.create_order(
                            symbol, "market", side, amount, None, p
                        ),
                        label="bitget.create_order.swap",
                    )
                except Exception as exc:
                    last_exc = exc
                    continue
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("swap create_order failed")

        try:
            return self._ccxt(
                lambda: self.exchange.create_order(symbol, "market", side, amount),
                label="bitget.create_order.spot",
            )
        except Exception as first:
            if side == "buy" and hasattr(self.exchange, "create_market_buy_order_with_cost"):
                cost = max(amount * last, 10.0)
                try:
                    return self._ccxt(
                        lambda: self.exchange.create_market_buy_order_with_cost(symbol, cost),
                        label="bitget.market_buy_cost",
                    )
                except Exception as second:
                    raise RuntimeError(f"{first} | fallback: {second}") from second
            raise

    def _place_sl_tp(
        self,
        symbol: str,
        amount: float,
        entry: float,
        side: str = "buy",
        sl_price: float | None = None,
        tp_price: float | None = None,
        margin_usdt: float | None = None,
        sl_margin_frac: float | None = None,
    ) -> dict[str, Any]:
        """Attach SL/TP immediately after a fill. Longs sell-to-close; shorts buy-to-close."""
        side_n = (side or "buy").lower().strip()
        short = side_n in {"sell", "short"}
        sl, tp = protective_prices(
            entry,
            side_n,
            atr=None if sl_price else self.fetch_atr(symbol),
            margin_usdt=margin_usdt,
            qty=amount,
            sl_margin_frac=sl_margin_frac,
            leverage=SWAP_LEVERAGE if self._is_swap(symbol) else 1.0,
        )
        if sl_price:
            sl = float(sl_price)
        if tp_price:
            tp = float(tp_price)
        sl = self._price(symbol, sl)
        tp = self._price(symbol, tp)
        qty = self._size_amount(symbol, amount, entry)
        tp_qty = self._size_amount(symbol, float(amount) * SCALE_TP_FRACTION, entry)
        try:
            market = self.exchange.market(symbol)
            min_amt = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0)
        except Exception:
            min_amt = 0.0
        if min_amt and tp_qty < min_amt:
            tp_qty = qty
        sl_pct = ((sl / entry) - 1.0) * 100.0 if entry else 0.0
        tp_pct = ((tp / entry) - 1.0) * 100.0 if entry else 0.0
        out: dict[str, Any] = {
            "sl_price": sl,
            "tp_price": tp,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "sl_order": None,
            "tp_order": None,
            "sl_error": None,
            "tp_error": None,
        }
        is_swap = self._is_swap(symbol)
        close_side = "buy" if short else "sell"
        hold = "short" if short else "long"
        close = {
            "reduceOnly": True,
            "marginMode": "crossed",
            "tradeSide": "close",
            "hedged": True,
            "holdSide": hold,
        }

        sl_attempts: list[tuple[str, Any, dict[str, Any]]] = []
        if is_swap:
            sl_attempts = [
                ("market", None, {**close, "stopLossPrice": sl, "presetStopLossPrice": sl}),
                ("stop", sl, {**close, "stopPrice": sl, "triggerPrice": sl}),
                ("stop_market", None, {**close, "stopPrice": sl, "triggerPrice": sl}),
                (
                    "market",
                    None,
                    {**close, "triggerPrice": sl, "planType": "loss_plan", "triggerType": "mark_price"},
                ),
                (
                    "market",
                    None,
                    {**close, "triggerPrice": sl, "planType": "pos_loss", "triggerType": "mark_price"},
                ),
            ]
        else:
            sl_attempts = [
                ("stop_market", None, {"stopPrice": sl, "triggerPrice": sl}),
                ("stop", sl, {"stopPrice": sl, "triggerPrice": sl}),
                ("market", None, {"stopLossPrice": sl}),
            ]
        sl_order, sl_err = self._first_order(symbol, close_side, qty, sl_attempts)
        out["sl_order"] = _slim_order(sl_order) if sl_order else None
        out["sl_error"] = sl_err

        tp_attempts: list[tuple[str, Any, dict[str, Any]]] = []
        if is_swap:
            tp_attempts = [
                ("limit", tp, {**close, "takeProfitPrice": tp, "presetTakeProfitPrice": tp}),
                ("limit", tp, close),
                (
                    "market",
                    None,
                    {**close, "triggerPrice": tp, "planType": "profit_plan", "triggerType": "mark_price"},
                ),
                (
                    "market",
                    None,
                    {**close, "triggerPrice": tp, "planType": "pos_profit", "triggerType": "mark_price"},
                ),
            ]
        else:
            tp_attempts = [
                ("limit", tp, {"timeInForce": "GTC"}),
                ("limit", tp, {"takeProfitPrice": tp}),
            ]
        tp_order, tp_err = self._first_order(symbol, close_side, tp_qty, tp_attempts)
        out["tp_order"] = _slim_order(tp_order) if tp_order else None
        out["tp_error"] = tp_err
        out["tp_qty"] = tp_qty
        return out

    def update_stop_loss(
        self,
        symbol: str,
        side: str,
        amount: float,
        sl_price: float,
        old_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace a reduce-only stop. Never opens a new book. Safe no-op on venue faults."""
        if not self.sandbox:
            raise RuntimeError("REFUSING live stop amend — sandbox lock tripped")
        side_n = _norm_side(side)
        short = side_n in {"sell", "short"}
        sl = self._price(symbol, float(sl_price))
        qty = self._size_amount(symbol, amount, sl)
        if qty <= 0 or sl <= 0:
            return {"ok": False, "sl_price": sl, "sl_order": None, "error": "invalid stop size"}
        if old_order_id:
            try:
                self._ccxt(
                    lambda: self.exchange.cancel_order(str(old_order_id), symbol),
                    label="bitget.cancel_stop",
                )
            except Exception:
                pass
        close_side = "buy" if short else "sell"
        hold = "short" if short else "long"
        is_swap = self._is_swap(symbol)
        close = {
            "reduceOnly": True,
            "marginMode": "crossed",
            "tradeSide": "close",
            "hedged": True,
            "holdSide": hold,
        }
        attempts: list[tuple[str, Any, dict[str, Any]]] = []
        if is_swap:
            attempts = [
                ("market", None, {**close, "stopLossPrice": sl, "presetStopLossPrice": sl}),
                ("stop", sl, {**close, "stopPrice": sl, "triggerPrice": sl}),
                ("stop_market", None, {**close, "stopPrice": sl, "triggerPrice": sl}),
            ]
        else:
            attempts = [
                ("stop_market", None, {"stopPrice": sl, "triggerPrice": sl}),
                ("stop", sl, {"stopPrice": sl, "triggerPrice": sl}),
            ]
        order, err = self._first_order(symbol, close_side, qty, attempts)
        return {
            "ok": order is not None,
            "sl_price": sl,
            "sl_order": _slim_order(order) if order else None,
            "sl_order_id": (order or {}).get("id") if order else None,
            "error": err,
            "qty": qty,
        }

    def _partial_close_qty(
        self,
        symbol: str,
        contracts: float,
        fraction: float,
        mark: float,
    ) -> tuple[float, str]:
        """Size a reduce-only close. Never exceeds the live book. Skips dust partials."""
        try:
            frac = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            frac = 1.0
        try:
            total = abs(float(contracts or 0.0))
        except (TypeError, ValueError):
            total = 0.0
        if total <= 0:
            return 0.0, "empty"
        raw = total if frac >= 0.999 else total * frac
        min_amt = 0.0
        try:
            market = self.exchange.market(symbol)
            min_amt = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0)
        except Exception:
            min_amt = 0.0
        if frac < 0.999 and min_amt and raw < min_amt:
            return 0.0, "below_min"
        qty = self._size_amount(symbol, raw, mark or 1.0)
        if qty > total:
            qty = self._size_amount(symbol, total, mark or 1.0)
        return qty, "ok"

    def _first_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        attempts: list[tuple[str, Any, dict[str, Any]]],
    ) -> tuple[dict[str, Any] | None, str | None]:
        last_err: str | None = None
        for order_type, price, params in attempts:
            try:
                order = self._ccxt(
                    lambda t=order_type, p=price, par=params: self.exchange.create_order(
                        symbol, t, side, amount, p, par
                    ),
                    label=f"bitget.{order_type}.{side}",
                )
                return order, None
            except Exception as exc:
                last_err = str(exc)[:240]
                continue
        return None, last_err

    def _spot_base_holding(self, symbol: str) -> dict[str, Any]:
        try:
            market = self.exchange.market(symbol)
        except Exception:
            return {"open": False, "source": "spot", "symbol": symbol}
        if market.get("swap") or market.get("future"):
            return {"open": False, "source": "spot", "symbol": symbol}
        base = str(market.get("base") or "")
        if not base or base.upper() in {"USDT", "USDC"}:
            return {"open": False, "source": "spot", "symbol": symbol}
        try:
            raw = self._ccxt(
                lambda: self.exchange.fetch_balance({"type": "spot"}),
                label="bitget.balance.spot.position",
            )
        except Exception as exc:
            return {"open": False, "source": "spot", "symbol": symbol, "error": str(exc)[:160]}
        totals = raw.get("total") or {}
        try:
            held = float(totals.get(base) or 0)
        except (TypeError, ValueError):
            held = 0.0
        min_amt = 0.0
        try:
            min_amt = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0)
        except (TypeError, ValueError):
            min_amt = 0.0
        dust = max(min_amt, 1e-8)
        if held > dust:
            return {
                "open": True,
                "source": "spot",
                "symbol": symbol,
                "side": "long",
                "contracts": held,
                "entry_price": None,
                "raw": {"base": base, "total": held},
                "error": None,
            }
        return {"open": False, "source": "spot", "symbol": symbol, "contracts": held}

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = RSI_TIMEFRAME,
        limit: int = 64,
    ) -> list[list[Any]]:
        """Live MAINNET candles. Empty list if the venue has no OHLCV for this symbol."""
        client = self._feed()
        live = self._resolve_public_symbol(symbol)
        dtype = self._feed_type(live)
        prev = (client.options or {}).get("defaultType")
        try:
            client.options["defaultType"] = dtype
            rows = self._ccxt(
                lambda: client.fetch_ohlcv(live, timeframe, limit=int(limit)),
                label=f"bitget.mainnet.ohlcv.{timeframe}",
            )
        except Exception:
            return []
        finally:
            client.options["defaultType"] = prev
        return list(rows) if isinstance(rows, list) else []

    def _ohlcv_with_fallback(
        self,
        symbol: str,
        timeframe: str = RSI_TIMEFRAME,
        period: int = RSI_PERIOD,
    ) -> tuple[str, list[list[Any]]]:
        used = timeframe or RSI_TIMEFRAME
        need = max(int(period) * 4, 50)
        rows = self.fetch_ohlcv(symbol, used, limit=need)
        if len(rows) < int(period) + 1:
            used = RSI_FALLBACK_TIMEFRAME
            rows = self.fetch_ohlcv(symbol, used, limit=need)
        return used, rows

    def fetch_rsi(
        self,
        symbol: str,
        timeframe: str = RSI_TIMEFRAME,
        period: int = RSI_PERIOD,
    ) -> dict[str, Any]:
        """RSI(14) from live OHLCV. Prefers 15m; falls back to 1h if the tape is thin."""
        used, rows = self._ohlcv_with_fallback(symbol, timeframe, period)
        closes: list[float] = []
        for row in rows:
            try:
                closes.append(float(row[4]))
            except (TypeError, ValueError, IndexError):
                continue
        rsi = compute_rsi(closes, period)
        return {
            "ok": rsi is not None,
            "mocked": False,
            "symbol": symbol,
            "timeframe": used,
            "period": int(period),
            "rsi": rsi,
            "bars": len(closes),
            "source": "ohlcv",
            "error": None if rsi is not None else "insufficient OHLCV for RSI(14)",
        }

    def fetch_ta_bundle(
        self,
        symbol: str,
        timeframe: str = RSI_TIMEFRAME,
        period: int = RSI_PERIOD,
    ) -> dict[str, Any]:
        """RSI + candle structure + ATR from one OHLCV pull."""
        used, rows = self._ohlcv_with_fallback(symbol, timeframe, period)
        closes: list[float] = []
        for row in rows:
            try:
                closes.append(float(row[4]))
            except (TypeError, ValueError, IndexError):
                continue
        rsi = compute_rsi(closes, period)
        candles = analyze_candles(rows)
        atr = candles.get("atr")
        if atr is None:
            atr = compute_atr(rows)
        payload: dict[str, Any] = {
            "ok": bool(rsi is not None or candles.get("ok")),
            "mocked": False,
            "symbol": symbol,
            "timeframe": used,
            "period": int(period),
            "rsi": rsi,
            "bars": len(closes),
            "source": "ohlcv",
            "atr": atr,
            "error": None,
        }
        payload.update(candles)
        if rsi is None and not candles.get("ok"):
            payload["error"] = "insufficient OHLCV for TA bundle"
        return payload

    def fetch_mtf_bundle(self, symbol: str) -> dict[str, Any]:
        """15m entry + 1h/4h confirmation from live Bitget fetch_ohlcv."""
        frames: dict[str, Any] = {}
        for tf in MTF_FRAMES:
            rows = self.fetch_ohlcv(symbol, tf, limit=80)
            if not rows:
                continue
            frames[tf] = enrich_ohlcv(rows, tf)
        entry = frames.get("15m") if isinstance(frames.get("15m"), dict) else {}
        payload: dict[str, Any] = {
            "ok": bool(frames),
            "mocked": False,
            "symbol": symbol,
            "source": "bitget.fetch_ohlcv.mtf",
            "frames": frames,
            "mtf": mtf_alignment(frames, "buy"),
            "error": None if frames else "no OHLCV on 15m/1h/4h",
        }
        payload.update(entry)
        if not payload.get("timeframe"):
            payload["timeframe"] = "15m"
        return payload

    def fetch_fundamentals(self, symbol: str) -> dict[str, Any]:
        """Market cap / supply / last via CCXT ticker + market.info. Never invents a cap."""
        ticker = self.fetch_ticker(symbol)
        info: dict[str, Any] = {}
        try:
            market = self.exchange.market(symbol) or {}
            raw_info = market.get("info") if isinstance(market.get("info"), dict) else {}
            info.update(raw_info)
        except Exception:
            market = {}
        tinfo = ticker.get("info") if isinstance(ticker.get("info"), dict) else {}
        if tinfo:
            info.update(tinfo)
        price = coerce_price(ticker.get("last"), ticker.get("bid"), ticker.get("ask"))
        supply = _first_positive(
            info.get("circulatingSupply"),
            info.get("circulating_supply"),
            info.get("totalSupply"),
            info.get("total_supply"),
            info.get("maxSupply"),
            info.get("supply"),
            market.get("supply") if isinstance(market, dict) else None,
        )
        mcap = _first_positive(
            info.get("marketCap"),
            info.get("market_cap"),
            info.get("marketCapUsd"),
        )
        if mcap is None and price > 0 and supply is not None:
            mcap = price * supply
        volume = _first_positive(
            ticker.get("quoteVolume"),
            info.get("quoteVolume"),
            info.get("baseVolume"),
        )
        try:
            is_swap = self._is_swap(symbol)
        except Exception:
            is_swap = ":USDT" in symbol
        return {
            "ok": price > 0,
            "mocked": False,
            "symbol": symbol,
            "price": price if price > 0 else None,
            "last": price if price > 0 else ticker.get("last"),
            "market_cap_usdt": mcap,
            "total_supply": supply,
            "volume_24h_usdt": volume,
            "is_swap": is_swap,
            "source": "ccxt",
            "error": ticker.get("error"),
        }

    def fetch_atr(self, symbol: str, timeframe: str = "1h", period: int = 14) -> float | None:
        """True-range ATR from live Demo OHLCV. None if the venue has no candles."""
        rows = self.fetch_ohlcv(symbol, timeframe, limit=max(period + 2, 16))
        return compute_atr(rows, period)

    def fetch_open_book(self) -> list[dict[str, Any]]:
        """Every live Demo position with mark, entry, and unrealized PnL."""
        raw: list[Any] = []
        errors: list[str] = []
        try:
            raw = list(
                self._ccxt(lambda: self.exchange.fetch_positions(), label="bitget.fetch_positions.book")
                or []
            )
        except Exception as exc:
            errors.append(str(exc)[:160])
        book: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pos in raw:
            snap = normalize_position(pos)
            if not snap or not snap.get("open"):
                continue
            key = f"{snap['symbol']}|{snap['side']}"
            if key in seen:
                continue
            seen.add(key)
            self._overlay_live_mark(snap)
            book.append(snap)
        if not book:
            for symbol in list(self.universe or [])[:40]:
                try:
                    spot = self._spot_base_holding(symbol)
                except Exception:
                    continue
                if not spot.get("open"):
                    continue
                ticker = self.fetch_ticker(symbol) or {}
                mark = coerce_price(ticker.get("mark"), ticker.get("last"), ticker.get("bid"), ticker.get("ask"))
                qty = float(spot.get("contracts") or 0.0)
                entry = coerce_price(spot.get("entry_price"), mark)
                pnl_usdt, pnl_pct = unrealized_pnl(entry, mark, qty, "buy")
                row = {
                    "open": True,
                    "source": "spot",
                    "symbol": symbol,
                    "side": "buy",
                    "contracts": qty,
                    "entry_price": entry,
                    "mark_price": mark,
                    "pnl_usdt": pnl_usdt,
                    "pnl_pct": pnl_pct,
                    "raw": spot.get("raw") or {},
                    "error": None,
                }
                self._overlay_live_mark(row)
                book.append(row)
        if errors and not book:
            return [{"open": False, "error": " | ".join(errors), "symbol": "", "side": "none"}]
        return book

    def close_market(
        self,
        symbol: str,
        *,
        fraction: float = 1.0,
        reason: str = "manual",
        side: str | None = None,
    ) -> dict[str, Any]:
        """Reduce-only market close. Never opens a new book."""
        with self._order_lock:
            return self._close_market(symbol, fraction=fraction, reason=reason, side=side)

    def _close_market(
        self,
        symbol: str,
        *,
        fraction: float = 1.0,
        reason: str = "manual",
        side: str | None = None,
    ) -> dict[str, Any]:
        if not self.sandbox:
            raise RuntimeError("REFUSING live close — sandbox lock tripped")
        existing = self.fetch_open_position(symbol)
        if side:
            wanted = _norm_side(side)
            got = _norm_side(str(existing.get("side") or ""))
            if got and wanted and got != wanted and existing.get("open"):
                # still close the live book on this symbol
                pass
        if not existing.get("open"):
            return {
                "ok": False,
                "status": "FLAT",
                "symbol": symbol,
                "error": "No open position",
                "pnl_usdt": 0.0,
                "pnl_pct": 0.0,
            }
        pos_side = _norm_side(str(existing.get("side") or side or "buy"))
        entry = coerce_price(existing.get("entry_price"), (existing.get("raw") or {}).get("entryPrice"))
        close_side_guess = "buy" if pos_side in {"sell", "short"} else "sell"
        quote = self.fetch_bbo(symbol, side=close_side_guess)
        mark = coerce_price(quote.get("peg"), quote.get("mark"), quote.get("last"), entry)
        qty, qty_why = self._partial_close_qty(
            symbol,
            float(existing.get("contracts") or 0.0),
            fraction,
            mark or entry or 1.0,
        )
        if qty <= 0:
            return {
                "ok": False,
                "status": "SKIP_PARTIAL" if qty_why == "below_min" else "FLAT",
                "symbol": symbol,
                "error": (
                    "Partial size below Bitget min amount — holding runner"
                    if qty_why == "below_min"
                    else "Quantity is zero"
                ),
                "pnl_usdt": 0.0,
                "pnl_pct": 0.0,
            }
        close_side = "buy" if pos_side in {"sell", "short"} else "sell"
        is_swap = self._is_swap(symbol)
        hold = "short" if close_side == "buy" else "long"
        param_sets: list[dict[str, Any]] = [{}]
        if is_swap:
            param_sets = [
                {
                    "reduceOnly": True,
                    "marginMode": "crossed",
                    "tradeSide": "close",
                    "hedged": True,
                    "holdSide": hold,
                },
                {
                    "reduceOnly": True,
                    "marginMode": "crossed",
                    "tradeSide": "close",
                    "holdSide": hold,
                },
                {"reduceOnly": True, "holdSide": hold},
                {"reduceOnly": True},
            ]
        balance_before = self._snapshot_usdt()
        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "venue": "bitget-demo",
            "sandbox": True,
            "live_trading": False,
            "symbol": symbol,
            "side": close_side,
            "position_side": pos_side,
            "amount": qty,
            "reason": reason,
            "entry_price": entry,
            "price": mark,
            "mark_price": mark,
        }
        _stamp_bbo(record, quote, as_entry=False)
        try:
            order = None
            last_exc: Exception | None = None
            for params in param_sets:
                try:
                    order = self._ccxt(
                        lambda p=params: self.exchange.create_order(
                            symbol, "market", close_side, qty, None, p
                        ),
                        label="bitget.close_market",
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    continue
            if order is None:
                raise last_exc or RuntimeError("close_market failed")
            demo_fill = coerce_price(order.get("average"), order.get("price"))
            fill = mark if mark > 0 else demo_fill
            pnl_usdt, pnl_pct = unrealized_pnl(entry, fill, qty, pos_side)
            record.update(
                {
                    "ok": True,
                    "status": "CLOSED" if fraction >= 0.999 else "PARTIAL",
                    "order_id": order.get("id"),
                    "raw_order": _slim_order(order),
                    "price": fill,
                    "demo_fill_price": demo_fill if demo_fill > 0 else None,
                    "pnl_usdt": round(pnl_usdt, 6),
                    "pnl_pct": round(pnl_pct, 4),
                    "notional_usdt": round(abs(fill * qty), 6),
                }
            )
            _stamp_bbo(record, quote, as_entry=False)
            self._apply_balance_change(record, balance_before=balance_before, filled=True)
        except Exception as exc:
            pnl_usdt, pnl_pct = unrealized_pnl(entry, mark, qty, pos_side)
            record.update(
                {
                    "ok": False,
                    "status": "ERROR",
                    "error": str(exc)[:400],
                    "pnl_usdt": round(pnl_usdt, 6),
                    "pnl_pct": round(pnl_pct, 4),
                    "notional_usdt": round(abs((mark or 0.0) * qty), 6),
                }
            )
            self._apply_balance_change(record, balance_before=balance_before, filled=False)
        log_path = _append_trade(record)
        record["log_path"] = str(log_path)
        return record

    def close_all(self, *, reason: str = "closeall") -> list[dict[str, Any]]:
        with self._order_lock:
            return self._close_all(reason=reason)

    def _close_all(self, *, reason: str = "closeall") -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for pos in self.fetch_open_book():
            if not pos.get("open"):
                continue
            try:
                results.append(
                    self.close_market(
                        str(pos.get("symbol")),
                        fraction=1.0,
                        reason=reason,
                        side=str(pos.get("side") or ""),
                    )
                )
            except Exception as exc:
                results.append(
                    {
                        "ok": False,
                        "status": "ERROR",
                        "symbol": pos.get("symbol"),
                        "error": str(exc)[:240],
                        "pnl_usdt": 0.0,
                        "pnl_pct": 0.0,
                    }
                )
        return results

    def _overlay_live_mark(self, snap: dict[str, Any]) -> None:
        """Reprice an open Demo book to live mainnet mark / BBO for realistic PnL."""
        symbol = str(snap.get("symbol") or "")
        if not symbol:
            return
        try:
            ticker = self.fetch_ticker(symbol)
        except Exception:
            return
        mark = coerce_price(
            ticker.get("mark"),
            ticker.get("last"),
            ticker.get("bid"),
            ticker.get("ask"),
            snap.get("mark_price"),
        )
        if mark <= 0:
            return
        entry = coerce_price(snap.get("entry_price"), mark)
        qty = float(snap.get("contracts") or 0.0)
        side = str(snap.get("side") or "buy")
        pnl_usdt, pnl_pct = unrealized_pnl(entry, mark, qty, side)
        snap["mark_price"] = mark
        snap["pnl_usdt"] = pnl_usdt
        snap["pnl_pct"] = pnl_pct
        snap["mark_source"] = ticker.get("source") or "bitget.mainnet"

    def _is_swap(self, symbol: str) -> bool:
        try:
            market = self.exchange.market(symbol)
            return bool(market.get("swap") or market.get("future"))
        except Exception:
            return ":USDT" in symbol or _looks_contract(symbol)

    def _price(self, symbol: str, value: float) -> float:
        try:
            return float(self.exchange.price_to_precision(symbol, value))
        except Exception:
            return float(f"{value:.6f}")

    def _size_amount(self, symbol: str, amount: float, last: float) -> float:
        qty = float(amount)
        if qty <= 0:
            notional = float(self.settings.paper_notional_usdt)
            qty = notional / max(last, 1e-9)
        try:
            market = self.exchange.market(symbol)
            min_amt = ((market.get("limits") or {}).get("amount") or {}).get("min")
            if min_amt and qty < float(min_amt):
                qty = float(min_amt)
        except Exception:
            pass
        try:
            return float(self.exchange.amount_to_precision(symbol, qty))
        except Exception:
            return float(f"{qty:.6f}")

    def _resolve_symbol(self, markets: dict[str, Any]) -> str:
        wanted = [self.preferred_symbol, *self.universe, *_SYMBOL_CANDIDATES]
        seen: set[str] = set()
        for symbol in wanted:
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            if symbol in markets:
                return symbol
        # last resort: any USDT spot
        for symbol, market in markets.items():
            if market.get("spot") and str(symbol).endswith("/USDT"):
                return str(symbol)
        raise RuntimeError("No tradable Demo market found on Bitget sandbox")


_CRYPTO_DENY = {
    "BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "AVAX", "DOT", "LINK", "MATIC",
    "LTC", "BCH", "UNI", "ATOM", "FIL", "APT", "ARB", "OP", "SUI", "TIA",
    "NEAR", "INJ", "SEI", "PEPE", "WIF", "BONK", "SHIB", "TRX", "TON", "BNB",
    "USDT", "USDC", "USD", "EUR", "DAI", "BUSD",
}


def _select_equity_universe(markets: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for symbol, market in (markets or {}).items():
        name = str(symbol or "").strip()
        if not name or name in seen:
            continue
        if not _is_equity_market(name, market if isinstance(market, dict) else {}):
            continue
        seen.add(name)
        out.append(name)
    out.sort(key=_universe_sort_key)
    return out


def _usdt_fallback_universe(markets: dict[str, Any]) -> list[str]:
    """When Demo lists no tagged stock/rToken markets, keep a live USDT book rather than a hardcoded five."""
    out: list[str] = []
    seen: set[str] = set()
    for symbol, market in (markets or {}).items():
        name = str(symbol or "").strip()
        if not name or name in seen:
            continue
        row = market if isinstance(market, dict) else {}
        quote = str(row.get("quote") or "").upper()
        if quote not in {"USDT", "USD", "USDC"} and "USDT" not in name.upper():
            continue
        if not (row.get("swap") or row.get("future") or row.get("spot") or ":USDT" in name or name.endswith("/USDT")):
            continue
        seen.add(name)
        out.append(name)
    out.sort(key=_universe_sort_key)
    return out


def _is_spot_market(symbol: str, market: dict[str, Any] | None = None) -> bool:
    """True only for a cash spot listing. Swap / future / :USDT perps are rejected."""
    name = str(symbol or "")
    if ":USDT" in name or name.endswith(":USDT"):
        return False
    row = market or {}
    if not row:
        return False
    if row.get("swap") or row.get("future") or row.get("option"):
        return False
    if row.get("spot") is True:
        return True
    if row.get("spot") is False:
        return False
    return "/" in name and ":" not in name


def _is_equity_market(symbol: str, market: dict[str, Any] | None = None) -> bool:
    """True for Bitget stock perps, rTokens, and US-equity-like Demo listings."""
    market = market or {}
    info = market.get("info") if isinstance(market.get("info"), dict) else {}
    tags = " ".join(
        str(info.get(key) or "")
        for key in (
            "productType",
            "category",
            "symbolType",
            "symbolName",
            "groupName",
            "subType",
            "businessType",
            "symbol",
        )
    ).upper()
    if any(tok in tags for tok in ("STOCK", "EQUITY", "RTOKEN", "SUSDT", "SHARE")):
        return True
    base = str(market.get("base") or symbol.split(":")[0].split("/")[0] or "").strip()
    if not base:
        return False
    if base[0] in {"r", "R"} and len(base) > 2 and base[1:].replace("-", "").isalpha():
        return True
    root = base[1:].upper() if base[0] in {"r", "R"} and len(base) > 2 and base[1:].isalpha() else base.upper()
    if root in _CRYPTO_DENY:
        return False
    quote = str(market.get("quote") or "").upper()
    usdtish = quote in {"USDT", "USD", "USDC", ""} or "USDT" in symbol.upper()
    if root.isalpha() and 1 <= len(root) <= 6 and usdtish:
        if market.get("swap") or market.get("future") or ":USDT" in symbol:
            return True
    return False


def _universe_sort_key(symbol: str) -> tuple[int, str]:
    raw = str(symbol)
    base = raw.split(":")[0].split("/")[0].upper()
    rtoken = 0 if (base.startswith("R") and len(base) > 2) else 1
    swap = 0 if ":USDT" in raw else 1
    return (rtoken, swap, raw)


def protective_prices(
    entry: float,
    side: str = "buy",
    atr: float | None = None,
    *,
    margin_usdt: float | None = None,
    qty: float | None = None,
    sl_margin_frac: float | None = None,
    leverage: float | None = None,
    take_profit_pct: float | None = None,
) -> tuple[float, float]:
    """SL/TP prices. Longs: SL below / TP above. Shorts: SL above / TP below.

    When margin_usdt + sl_margin_frac are set, the stop is strictly the fraction
    of invested margin (50% high-risk → 100% low-risk). ATR/2%/5% remains the
    fallback for callers that do not pass a margin clip.
    """
    px = float(entry)
    tp_override = TAKE_PROFIT_PCT
    if take_profit_pct is not None:
        try:
            parsed_tp = float(take_profit_pct)
        except (TypeError, ValueError):
            parsed_tp = TAKE_PROFIT_PCT
        if parsed_tp > 0:
            tp_override = parsed_tp
    if margin_usdt and sl_margin_frac and px > 0:
        sl, tp = sl_tp_from_margin(
            px,
            side,
            float(margin_usdt),
            float(sl_margin_frac),
            qty=qty,
            leverage=float(leverage or SWAP_LEVERAGE),
            take_profit_pct=tp_override,
        )
        if sl > 0 and tp > 0:
            return sl, tp
    sl_pct = STOP_LOSS_PCT
    tp_pct = tp_override
    if atr is not None and px > 0:
        try:
            atr_pct = abs(float(atr)) / px
        except (TypeError, ValueError):
            atr_pct = 0.0
        if atr_pct > 0:
            sl_pct = min(ATR_SL_PCT_CAP, max(ATR_SL_PCT_FLOOR, atr_pct * ATR_SL_MULT))
            tp_pct = min(ATR_TP_PCT_CAP, max(ATR_TP_PCT_FLOOR, sl_pct * ATR_TP_MULT))
    if _norm_side(side) in {"sell", "short"}:
        return px * (1.0 + sl_pct), px * (1.0 - tp_pct)
    return px * (1.0 - sl_pct), px * (1.0 + tp_pct)


def _first_positive(*values: Any) -> float | None:
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


def unrealized_pnl(entry: float, mark: float, qty: float, side: str) -> tuple[float, float]:
    """Return (pnl_usdt, pnl_pct). Shorts profit when mark falls."""
    try:
        entry_f = float(entry or 0.0)
        mark_f = float(mark or 0.0)
        qty_f = abs(float(qty or 0.0))
    except (TypeError, ValueError):
        return 0.0, 0.0
    if entry_f <= 0 or qty_f <= 0 or mark_f <= 0:
        return 0.0, 0.0
    if _norm_side(side) in {"sell", "short"}:
        usdt = (entry_f - mark_f) * qty_f
        pct = ((entry_f - mark_f) / entry_f) * 100.0
    else:
        usdt = (mark_f - entry_f) * qty_f
        pct = ((mark_f - entry_f) / entry_f) * 100.0
    return round(usdt, 8), round(pct, 6)


def _norm_side(side: str) -> str:
    raw = (side or "").strip().lower()
    if raw in {"sell", "short"}:
        return "sell"
    if raw in {"buy", "long"}:
        return "buy"
    return raw


def normalize_position(pos: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(pos, dict):
        return None
    symbol = str(pos.get("symbol") or "")
    qty = _position_contracts(pos)
    info = pos.get("info") if isinstance(pos.get("info"), dict) else {}
    side = _norm_side(str(pos.get("side") or info.get("holdSide") or info.get("posSide") or ""))
    if not symbol or qty <= 0 or side in {"flat", "none", "closed", ""}:
        return None
    entry = coerce_price(pos.get("entryPrice"), info.get("openPriceAvg"), pos.get("markPrice"))
    mark = coerce_price(pos.get("markPrice"), pos.get("entryPrice"), entry)
    pnl_usdt = pos.get("unrealizedPnl")
    try:
        pnl_usdt_f = float(pnl_usdt) if pnl_usdt not in (None, "") else None
    except (TypeError, ValueError):
        pnl_usdt_f = None
    pnl_pct = pos.get("percentage")
    try:
        pnl_pct_f = float(pnl_pct) if pnl_pct not in (None, "") else None
    except (TypeError, ValueError):
        pnl_pct_f = None
    if pnl_usdt_f is None or pnl_pct_f is None:
        calc_usdt, calc_pct = unrealized_pnl(entry, mark, qty, side)
        if pnl_usdt_f is None:
            pnl_usdt_f = calc_usdt
        if pnl_pct_f is None:
            pnl_pct_f = calc_pct
    return {
        "open": True,
        "source": "positions",
        "symbol": symbol,
        "side": side,
        "contracts": qty,
        "entry_price": entry,
        "mark_price": mark,
        "pnl_usdt": round(float(pnl_usdt_f or 0.0), 6),
        "pnl_pct": round(float(pnl_pct_f or 0.0), 4),
        "raw": _slim_position(pos),
        "error": None,
    }


def _fill_price(order: dict[str, Any], last: float) -> float:
    for key in ("average", "price", "stopPrice"):
        try:
            value = float(order.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return float(last)


def _position_contracts(pos: dict[str, Any]) -> float:
    for key in ("contracts", "contractSize", "notional"):
        try:
            value = abs(float(pos.get(key) or 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    info = pos.get("info") if isinstance(pos.get("info"), dict) else {}
    for key in ("total", "available", "openSizeQty"):
        try:
            value = abs(float(info.get(key) or 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _position_is_open(pos: dict[str, Any], symbol: str) -> bool:
    if not isinstance(pos, dict):
        return False
    psym = str(pos.get("symbol") or "")
    if not psym:
        return False
    wanted = symbol.split(":")[0]
    got = psym.split(":")[0]
    if psym != symbol and wanted != got:
        return False
    if _position_contracts(pos) <= 0:
        return False
    side = str(pos.get("side") or "").lower()
    if side in {"flat", "none", "closed"}:
        return False
    return True


def _slim_position(pos: dict[str, Any]) -> dict[str, Any]:
    keys = ("symbol", "side", "contracts", "contractSize", "entryPrice", "markPrice", "notional", "unrealizedPnl")
    return {k: pos.get(k) for k in keys}


def _book_payload(
    symbol: str,
    bids: list[Any],
    asks: list[Any],
    *,
    ok: bool,
    source: str,
    error: str | None,
) -> dict[str, Any]:
    best_bid = _level_px(bids, 0)
    best_ask = _level_px(asks, 0)
    bid_size = _level_sz(bids, 0)
    ask_size = _level_sz(asks, 0)
    crossed = bool(best_bid and best_ask and best_ask <= best_bid)
    spread_pct: float | None = None
    mid: float | None = None
    if best_bid and best_ask and best_bid > 0 and best_ask > 0 and not crossed:
        mid = (best_bid + best_ask) / 2.0
        spread_pct = ((best_ask - best_bid) / mid) * 100.0
    return {
        "ok": ok and best_bid is not None and best_ask is not None,
        "mocked": False,
        "source": source,
        "symbol": symbol,
        "bids": bids[:10],
        "asks": asks[:10],
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "mid": mid,
        "spread_pct": spread_pct,
        "crossed": crossed,
        "bid_depth": len(bids),
        "ask_depth": len(asks),
        "error": error,
    }


def _level_px(levels: list[Any], index: int) -> float | None:
    if index >= len(levels):
        return None
    row = levels[index]
    try:
        px = float(row[0])
    except (TypeError, ValueError, IndexError):
        return None
    return px if px > 0 else None


def _level_sz(levels: list[Any], index: int) -> float | None:
    if index >= len(levels):
        return None
    row = levels[index]
    try:
        return float(row[1])
    except (TypeError, ValueError, IndexError):
        return None


def _is_margin_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in ("25203", "25202", "insufficient margin", "insufficient balance")
    )


def _slim_order(order: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "symbol", "type", "side", "amount", "price", "status", "datetime", "filled")
    return {k: order.get(k) for k in keys}


def _append_trade(record: dict[str, Any]) -> Path:
    stamp_gitbook_log(record)
    TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK:
        if TRADE_LOG.exists():
            try:
                payload = json.loads(TRADE_LOG.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {"venue": "bitget-demo", "trades": []}
        else:
            payload = {"venue": "bitget-demo", "paper_trading": True, "trades": []}
        payload["paper_trading"] = True
        payload["gitbook_columns"] = [
            "timestamp",
            "instrument",
            "direction",
            "quantity",
            "price",
            "account_balance_change",
        ]
        trades = payload.setdefault("trades", [])
        trades.append(record)
        tmp = TRADE_LOG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(TRADE_LOG)
    return TRADE_LOG
