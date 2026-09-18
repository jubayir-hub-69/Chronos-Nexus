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
        self._order_lock = threading.RLock()

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
        with self._order_lock:
            return self._execute_paper_order(symbol, side, amount, reasoning_hash, extra)

    def _execute_paper_order(
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
            if entry > 0:
                try:
                    guards = self._place_sl_tp(symbol, sized, entry, side=side_n)
                    record.update(guards)
                    record["price"] = entry
                    record["entry_price"] = entry
                except Exception as guard_exc:
                    sl, tp = protective_prices(entry, side_n)
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
            sl, tp = protective_prices(last, side, atr=self.fetch_atr(symbol))
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
    ) -> dict[str, Any]:
        """Attach SL/TP immediately after a fill. Longs sell-to-close; shorts buy-to-close."""
        side_n = (side or "buy").lower().strip()
        short = side_n in {"sell", "short"}
        sl, tp = protective_prices(entry, side_n, atr=self.fetch_atr(symbol))
        sl = self._price(symbol, sl)
        tp = self._price(symbol, tp)
        qty = self._size_amount(symbol, amount, entry)
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
        tp_order, tp_err = self._first_order(symbol, close_side, qty, tp_attempts)
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

    def fetch_atr(self, symbol: str, timeframe: str = "1h", period: int = 14) -> float | None:
        """True-range ATR from live Demo OHLCV. None if the venue has no candles."""
        try:
            rows = self._ccxt(
                lambda: self.exchange.fetch_ohlcv(symbol, timeframe, limit=max(period + 2, 16)),
                label="bitget.ohlcv.atr",
            )
        except Exception:
            return None
        if not isinstance(rows, list) or len(rows) < 3:
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
        window = trs[-period:] if trs else []
        if not window:
            return None
        return sum(window) / float(len(window))

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
            book.append(snap)
        if not book:
            for symbol in list(self.universe or [])[:40]:
                try:
                    spot = self._spot_base_holding(symbol)
                except Exception:
                    continue
                if not spot.get("open"):
                    continue
                mark = coerce_price((self.fetch_ticker(symbol) or {}).get("last"))
                qty = float(spot.get("contracts") or 0.0)
                entry = coerce_price(spot.get("entry_price"), mark)
                pnl_usdt, pnl_pct = unrealized_pnl(entry, mark, qty, "buy")
                book.append(
                    {
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
                )
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
        qty = float(existing.get("contracts") or 0.0) * max(0.0, min(1.0, float(fraction)))
        entry = coerce_price(existing.get("entry_price"), (existing.get("raw") or {}).get("entryPrice"))
        ticker = self.fetch_ticker(symbol)
        mark = coerce_price(ticker.get("last"), ticker.get("bid"), ticker.get("ask"), entry)
        if qty <= 0:
            return {
                "ok": False,
                "status": "FLAT",
                "symbol": symbol,
                "error": "Quantity is zero",
                "pnl_usdt": 0.0,
                "pnl_pct": 0.0,
            }
        qty = self._size_amount(symbol, qty, mark or entry or 1.0)
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
            fill = coerce_price(order.get("average"), order.get("price"), mark)
            pnl_usdt, pnl_pct = unrealized_pnl(entry, fill, qty, pos_side)
            record.update(
                {
                    "ok": True,
                    "status": "CLOSED" if fraction >= 0.999 else "PARTIAL",
                    "order_id": order.get("id"),
                    "raw_order": _slim_order(order),
                    "price": fill,
                    "pnl_usdt": round(pnl_usdt, 6),
                    "pnl_pct": round(pnl_pct, 4),
                    "notional_usdt": round(abs(fill * qty), 6),
                }
            )
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


def protective_prices(
    entry: float,
    side: str = "buy",
    atr: float | None = None,
) -> tuple[float, float]:
    """SL/TP prices. Longs: SL below / TP above. Shorts: SL above / TP below.

    When ATR is available, distance is 1.5× ATR (floored/capped) and TP is 2.5× SL.
    """
    px = float(entry)
    sl_pct = STOP_LOSS_PCT
    tp_pct = TAKE_PROFIT_PCT
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
