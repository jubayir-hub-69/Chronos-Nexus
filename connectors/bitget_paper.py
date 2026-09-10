"""Bitget Paper Trading connector.

Sandbox/Demo only. `set_sandbox_mode(True)` is the first call after construct.
Live trading is refused at init.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt

from core.config import PROJECT_ROOT, Settings

TRADE_LOG = PROJECT_ROOT / "data" / "logs" / "trades.json"
_LOG_LOCK = threading.Lock()

_SYMBOL_CANDIDATES = (
    "rNVDA/USDT",
    "rAAPL/USDT",
    "rTSLA/USDT",
    "NVDA/USDT",
    "AAPL/USDT",
    "TSLA/USDT",
    "NVDA/USDT:USDT",
    "AAPL/USDT:USDT",
    "TSLA/USDT:USDT",
    "BTC/USDT",
    "BTC/USDT:USDT",
)


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
                },
                "headers": {"PAPTRADING": "1"},
            }
        )
        # Must be the first call after construct — CCXT Demo / PAPTRADING=1.
        exchange.set_sandbox_mode(True)
        self.exchange = exchange

    def ping(self) -> dict[str, Any]:
        last_error: Exception | None = None
        markets: dict[str, Any] = {}
        for attempt in range(3):
            try:
                markets = self.exchange.load_markets(reload=attempt > 0)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                time.sleep(1.2 * (attempt + 1))
        if last_error is not None:
            raise RuntimeError(f"Bitget Demo load_markets failed: {last_error}") from last_error
        self.resolved_symbol = self._resolve_symbol(markets)
        market = markets.get(self.resolved_symbol) or {}
        if market.get("swap") or market.get("future"):
            self.exchange.options["defaultType"] = "swap"
        else:
            self.exchange.options["defaultType"] = "spot"
        return {
            "sandbox": True,
            "paptrading": "1",
            "markets": len(markets),
            "symbol": self.resolved_symbol,
            "market_type": "swap" if market.get("swap") else "spot",
            "id": self.exchange.id,
        }

    def fetch_demo_balance(self) -> dict[str, Any]:
        assets: dict[str, dict[str, float]] = {}
        errors: list[str] = []
        for account_type in ("spot", "swap"):
            try:
                raw = self.exchange.fetch_balance({"type": account_type})
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

    def fetch_ticker(self, symbol: str | None = None) -> dict[str, Any]:
        target = symbol or self.resolved_symbol or self.preferred_symbol
        try:
            ticker = self.exchange.fetch_ticker(target)
            return {
                "ok": True,
                "mocked": False,
                "symbol": ticker.get("symbol") or target,
                "last": ticker.get("last"),
                "bid": ticker.get("bid"),
                "ask": ticker.get("ask"),
                "percentage": ticker.get("percentage"),
                "quoteVolume": ticker.get("quoteVolume"),
                "datetime": ticker.get("datetime"),
            }
        except Exception as exc:
            return {
                "ok": False,
                "mocked": True,
                "symbol": target,
                "last": 100.0,
                "bid": 99.9,
                "ask": 100.1,
                "percentage": 0.0,
                "quoteVolume": 0.0,
                "datetime": datetime.now(timezone.utc).isoformat(),
                "error": str(exc)[:240],
            }

    def execute_paper_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        reasoning_hash: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.sandbox:
            raise RuntimeError("REFUSING live order — sandbox lock tripped")

        side_n = side.lower().strip()
        if side_n not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")

        ticker = self.fetch_ticker(symbol)
        last = float(ticker.get("last") or 0) or 1.0
        sized = self._size_amount(symbol, amount, last)

        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "venue": "bitget-demo",
            "sandbox": True,
            "live_trading": False,
            "symbol": symbol,
            "side": side_n,
            "amount": sized,
            "notional_usdt": round(sized * last, 6),
            "reasoning_hash": reasoning_hash,
            "ticker": {k: ticker.get(k) for k in ("last", "percentage", "mocked", "datetime")},
        }
        if extra:
            record["board"] = extra

        try:
            order = self._place(symbol, side_n, sized, last)
            record["ok"] = True
            record["status"] = str(order.get("status") or "submitted")
            record["order_id"] = order.get("id")
            record["raw_order"] = _slim_order(order)
        except Exception as exc:
            msg = str(exc)
            record["ok"] = False
            record["order_id"] = None
            record["raw_order"] = {}
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
            "reasoning_hash": extra.get("reasoning_hash"),
            "board": extra.get("board"),
        }
        log_path = _append_trade(record)
        record["log_path"] = str(log_path)
        return record

    def _place(self, symbol: str, side: str, amount: float, last: float) -> dict[str, Any]:
        market = {}
        try:
            market = self.exchange.market(symbol)
        except Exception:
            pass
        is_swap = bool(market.get("swap") or market.get("future") or ":USDT" in symbol)
        if is_swap:
            try:
                self.exchange.set_leverage(5, symbol)
            except Exception:
                pass
            params = {"marginMode": "crossed", "tradeSide": "open", "hedged": True}
            return self.exchange.create_order(symbol, "market", side, amount, None, params)

        try:
            return self.exchange.create_order(symbol, "market", side, amount)
        except Exception as first:
            if side == "buy" and hasattr(self.exchange, "create_market_buy_order_with_cost"):
                cost = max(amount * last, 10.0)
                try:
                    return self.exchange.create_market_buy_order_with_cost(symbol, cost)
                except Exception as second:
                    raise RuntimeError(f"{first} | fallback: {second}") from second
            raise

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
        wanted = [self.preferred_symbol, *_SYMBOL_CANDIDATES]
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
    TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK:
        if TRADE_LOG.exists():
            try:
                payload = json.loads(TRADE_LOG.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {"venue": "bitget-demo", "trades": []}
        else:
            payload = {"venue": "bitget-demo", "paper_trading": True, "trades": []}
        trades = payload.setdefault("trades", [])
        trades.append(record)
        tmp = TRADE_LOG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(TRADE_LOG)
    return TRADE_LOG
