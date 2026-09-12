"""Bitget Paper Trading connector.

Sandbox/Demo only. `set_sandbox_mode(True)` is the first call after construct.
Live trading is refused at init.
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

STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.05
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
                },
                "headers": {"PAPTRADING": "1"},
            }
        )
        # Must be the first call after construct — CCXT Demo / PAPTRADING=1.
        exchange.set_sandbox_mode(True)
        self.exchange = exchange

    def _ccxt(self, fn, *, label: str = "bitget"):
        return call_with_backoff(fn, attempts=3, label=label)

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
        return {
            "sandbox": True,
            "paptrading": "1",
            "markets": len(markets),
            "universe": len(self.universe),
            "universe_source": self.universe_source,
            "symbol": self.resolved_symbol,
            "market_type": "swap" if market.get("swap") else "spot",
            "id": self.exchange.id,
        }

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
        target = symbol or self.resolved_symbol or self.preferred_symbol
        try:
            ticker = self._ccxt(lambda: self.exchange.fetch_ticker(target), label="bitget.ticker")
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
                "mocked": False,
                "symbol": target,
                "last": None,
                "bid": None,
                "ask": None,
                "percentage": None,
                "quoteVolume": None,
                "datetime": datetime.now(timezone.utc).isoformat(),
                "error": str(exc)[:240],
            }

    def fetch_order_book(self, symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Live L2 book for the Demo symbol. Never invents bids/asks."""
        target = symbol or self.resolved_symbol or self.preferred_symbol
        try:
            book = self._ccxt(
                lambda: self.exchange.fetch_order_book(target, limit),
                label="bitget.order_book",
            )
            return _book_payload(
                target,
                list(book.get("bids") or []),
                list(book.get("asks") or []),
                ok=True,
                source="l2",
                error=None,
            )
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
            return _book_payload(
                target,
                bids,
                asks,
                ok=bool(bids and asks),
                source="ticker",
                error=str(exc)[:240],
            )

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

        ticker = self.fetch_ticker(symbol)
        last = coerce_price(ticker.get("last"), ticker.get("bid"), ticker.get("ask"))
        if last <= 0:
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
                "status": "NO_LIVE_PRICE",
                "order_id": None,
                "raw_order": {},
                "error": "No live last price — refusing to size a dummy ticket.",
                "ticker": {k: ticker.get(k) for k in ("last", "percentage", "mocked", "datetime")},
                "price": 0.0,
                "entry_price": 0.0,
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
            "ticker": {k: ticker.get(k) for k in ("last", "percentage", "mocked", "datetime")},
            "price": last,
            "entry_price": last,
            "sl_price": None,
            "tp_price": None,
            "sl_order": None,
            "tp_order": None,
        }
        if extra:
            record["board"] = extra

        balance_before = self._snapshot_usdt()
        try:
            order = self._place(symbol, side_n, sized, last)
            record["ok"] = True
            record["status"] = str(order.get("status") or "submitted")
            record["order_id"] = order.get("id")
            record["raw_order"] = _slim_order(order)
            entry = coerce_price(
                order.get("average"),
                order.get("price"),
                last,
            )
            record["entry_price"] = entry
            record["price"] = entry
            record["notional_usdt"] = round(sized * entry, 6) if entry > 0 else notional
            self._apply_balance_change(record, balance_before=balance_before, filled=True)
            if side_n == "buy" and entry > 0:
                try:
                    guards = self._place_sl_tp(symbol, sized, entry)
                    record.update(guards)
                    record["price"] = entry
                    record["entry_price"] = entry
                except Exception as guard_exc:
                    sl, tp = protective_prices(entry)
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

    def _place(self, symbol: str, side: str, amount: float, last: float) -> dict[str, Any]:
        is_swap = self._is_swap(symbol)
        if is_swap:
            try:
                self._ccxt(lambda: self.exchange.set_leverage(5, symbol), label="bitget.leverage")
            except Exception:
                pass
            params: dict[str, Any] = {"marginMode": "crossed", "tradeSide": "open", "hedged": True}
            if side == "buy":
                sl, tp = protective_prices(last)
                params["stopLossPrice"] = self._price(symbol, sl)
                params["takeProfitPrice"] = self._price(symbol, tp)
            try:
                return self._ccxt(
                    lambda: self.exchange.create_order(symbol, "market", side, amount, None, params),
                    label="bitget.create_order.swap",
                )
            except Exception:
                bare = {"marginMode": "crossed", "tradeSide": "open", "hedged": True}
                return self._ccxt(
                    lambda: self.exchange.create_order(symbol, "market", side, amount, None, bare),
                    label="bitget.create_order.swap.bare",
                )

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

    def _place_sl_tp(self, symbol: str, amount: float, entry: float) -> dict[str, Any]:
        """Place a -2% stop-loss and +5% take-profit immediately after a BUY fill."""
        sl, tp = protective_prices(entry)
        sl = self._price(symbol, sl)
        tp = self._price(symbol, tp)
        qty = self._size_amount(symbol, amount, entry)
        out: dict[str, Any] = {
            "sl_price": sl,
            "tp_price": tp,
            "sl_pct": -STOP_LOSS_PCT * 100.0,
            "tp_pct": TAKE_PROFIT_PCT * 100.0,
            "sl_order": None,
            "tp_order": None,
            "sl_error": None,
            "tp_error": None,
        }
        is_swap = self._is_swap(symbol)
        close = {
            "reduceOnly": True,
            "marginMode": "crossed",
            "tradeSide": "close",
            "hedged": True,
            "holdSide": "long",
        }

        sl_attempts: list[tuple[str, Any, dict[str, Any]]] = []
        if is_swap:
            sl_attempts = [
                ("market", None, {**close, "stopLossPrice": sl}),
                ("stop", sl, {**close, "stopPrice": sl}),
                ("stop_market", None, {**close, "stopPrice": sl}),
            ]
        else:
            sl_attempts = [
                ("stop_market", None, {"stopPrice": sl}),
                ("stop", sl, {"stopPrice": sl}),
                ("market", None, {"stopLossPrice": sl}),
            ]
        sl_order, sl_err = self._first_order(symbol, "sell", qty, sl_attempts)
        out["sl_order"] = _slim_order(sl_order) if sl_order else None
        out["sl_error"] = sl_err

        tp_attempts: list[tuple[str, Any, dict[str, Any]]] = []
        if is_swap:
            tp_attempts = [
                ("limit", tp, {**close, "takeProfitPrice": tp}),
                ("limit", tp, close),
            ]
        else:
            tp_attempts = [
                ("limit", tp, {"timeInForce": "GTC"}),
                ("limit", tp, {"takeProfitPrice": tp}),
            ]
        tp_order, tp_err = self._first_order(symbol, "sell", qty, tp_attempts)
        out["tp_order"] = _slim_order(tp_order) if tp_order else None
        out["tp_error"] = tp_err
        return out

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

    def _is_swap(self, symbol: str) -> bool:
        try:
            market = self.exchange.market(symbol)
            return bool(market.get("swap") or market.get("future"))
        except Exception:
            return ":USDT" in symbol

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


def protective_prices(entry: float) -> tuple[float, float]:
    px = float(entry)
    return px * (1.0 - STOP_LOSS_PCT), px * (1.0 + TAKE_PROFIT_PCT)


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
