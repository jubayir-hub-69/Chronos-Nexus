"""Phase 12: 45113 max-order-value close is sliced until the book is flat."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from utils.commands import _telegram_fault
from connectors.bitget_paper import (
    CLOSE_CHUNK_QTY_DEFAULT,
    BitgetPaperConnector,
    _confirmed_fill_qty,
    _is_max_order_fault,
    _is_min_notional_fault,
    normalize_position,
)
from core.retry import is_retryable


class MaxOrderFaultTests(unittest.TestCase):
    def test_45113_is_detected_and_not_retried(self) -> None:
        err = RuntimeError("bitget 45113 Maximum order value limit triggered")
        self.assertTrue(_is_max_order_fault(err))
        self.assertFalse(is_retryable(err))
        self.assertTrue(_is_max_order_fault("45112 more than the maximum order quantity"))
        self.assertFalse(_is_max_order_fault("insufficient margin"))
        dust = RuntimeError('bitget {"code":"45110","msg":"less than the minimum amount 1 USDT"}')
        self.assertTrue(_is_min_notional_fault(dust))
        self.assertFalse(is_retryable(dust))
        self.assertFalse(_is_min_notional_fault("45113 Maximum order value"))


class ChunkQtyTests(unittest.TestCase):
    def test_thousand_lot_slices_to_one_hundred(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.exchange = MagicMock()
        conn.exchange.market.return_value = {
            "limits": {"amount": {"min": 1.0, "max": 100000.0}, "cost": {"max": 1_000_000.0}}
        }
        conn.exchange.amount_to_precision = lambda _s, q: float(q)
        chunk = conn._close_chunk_qty("PRESPCX/USDT:USDT", 1000.0, 1.0)
        self.assertEqual(chunk, CLOSE_CHUNK_QTY_DEFAULT)
        self.assertLess(chunk, 1000.0)

    def test_sub_dollar_crumb_is_not_a_slice(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.exchange = MagicMock()
        conn.exchange.market.return_value = {
            "limits": {"amount": {"min": 0.001, "max": 100000.0}, "cost": {"min": 1.0, "max": 1_000_000.0}}
        }
        conn.exchange.amount_to_precision = lambda _s, q: float(q)
        self.assertEqual(conn._close_chunk_qty("X/USDT:USDT", 0.4, 1.0), 0.0)

    def test_dust_tail_is_absorbed_into_the_chunk(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.exchange = MagicMock()
        conn.exchange.market.return_value = {
            "limits": {"amount": {"min": 0.001, "max": 100000.0}, "cost": {"max": 1_000_000.0}}
        }
        conn.exchange.amount_to_precision = lambda _s, q: float(q)
        chunk = conn._close_chunk_qty("X/USDT:USDT", 100.4, 1.0)
        self.assertAlmostEqual(chunk, 100.4)


class ReduceUntilFlatTests(unittest.TestCase):
    def test_45113_halves_then_fills_in_slices(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.sandbox = True
        conn.exchange = MagicMock()
        conn.exchange.market.return_value = {
            "limits": {"amount": {"min": 1.0, "max": 100000.0}, "cost": {"max": 1_000_000.0}}
        }
        conn.exchange.amount_to_precision = lambda _s, q: float(q)
        calls: list[float] = []

        def _ccxt(fn, label=""):
            del label
            return fn()

        def create_order(_symbol, _typ, _side, qty, _price, _params):
            calls.append(float(qty))
            if float(qty) > 100:
                raise RuntimeError("45113 Maximum order value limit triggered")
            return {"id": f"ord-{len(calls)}", "average": 1.0}

        conn._ccxt = _ccxt  # type: ignore[method-assign]
        conn.exchange.create_order = create_order
        remaining = {"qty": 250.0}

        def fetch_open(symbol):
            del symbol
            if remaining["qty"] <= 1e-9:
                return {"open": False, "contracts": 0.0}
            return {"open": True, "contracts": remaining["qty"], "side": "buy"}

        def size_amount(_symbol, amount, _last):
            return float(amount)

        conn.fetch_open_position = fetch_open  # type: ignore[method-assign]
        conn._size_amount = size_amount  # type: ignore[method-assign]
        conn._close_chunk_qty = lambda _s, rem, _m: min(float(rem), 100.0)  # type: ignore[method-assign]

        original_submit = conn._submit_reduce_order

        def submit(symbol, close_side, qty, param_sets):
            order = original_submit(symbol, close_side, qty, param_sets)
            remaining["qty"] = max(0.0, remaining["qty"] - float(qty))
            return order

        conn._submit_reduce_order = submit  # type: ignore[method-assign]

        with patch("connectors.bitget_paper.time.sleep", return_value=None):
            order, filled, loops = conn._reduce_until_flat(
                "PRESPCX/USDT:USDT",
                "sell",
                250.0,
                1.0,
                [{"reduceOnly": True}],
                fraction=1.0,
            )
        self.assertIsNotNone(order)
        self.assertAlmostEqual(filled, 250.0)
        self.assertGreaterEqual(loops, 3)
        self.assertTrue(all(q <= 100.0 + 1e-9 for q in calls))
        self.assertFalse(fetch_open("PRESPCX/USDT:USDT").get("open"))

    def test_45110_dust_is_dropped_without_raising(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.sandbox = True
        conn.exchange = MagicMock()
        conn.exchange.market.return_value = {
            "limits": {"amount": {"min": 0.001, "max": 100000.0}, "cost": {"min": 1.0}}
        }
        conn.exchange.amount_to_precision = lambda _s, q: float(q)
        calls: list[float] = []

        def _ccxt(fn, label=""):
            del label
            return fn()

        def create_order(_symbol, _typ, _side, qty, _price, _params):
            calls.append(float(qty))
            raise RuntimeError('{"code":"45110","msg":"less than the minimum amount 1 USDT"}')

        conn._ccxt = _ccxt  # type: ignore[method-assign]
        conn.exchange.create_order = create_order
        conn._size_amount = lambda _s, amount, _last: float(amount)  # type: ignore[method-assign]
        conn._close_chunk_qty = lambda _s, rem, _m: float(rem)  # type: ignore[method-assign]

        with patch("connectors.bitget_paper.time.sleep", return_value=None):
            order, filled, _loops = conn._reduce_until_flat(
                "DUST/USDT:USDT",
                "sell",
                0.4,
                1.0,
                [{"reduceOnly": True}],
                fraction=1.0,
            )
        self.assertEqual(calls, [])
        self.assertAlmostEqual(filled, 0.0)
        self.assertTrue(isinstance(order, dict) and order.get("dust"))

        with patch("connectors.bitget_paper.time.sleep", return_value=None):
            order, filled, _loops = conn._reduce_until_flat(
                "DUST/USDT:USDT",
                "sell",
                1.0,
                1.0,
                [{"reduceOnly": True}],
                fraction=1.0,
            )
        self.assertEqual(calls, [1.0])
        self.assertAlmostEqual(filled, 0.0)
        self.assertTrue(isinstance(order, dict) and order.get("dust"))

    def test_id_only_ack_is_not_a_fill(self) -> None:
        ghost = {
            "id": "1486178786162884608",
            "symbol": "PRESPCX/USDT",
            "filled": None,
            "status": None,
            "amount": None,
            "price": None,
        }
        self.assertEqual(_confirmed_fill_qty(ghost, 52.6), 0.0)
        self.assertEqual(
            _confirmed_fill_qty({"id": "1", "status": "closed", "filled": 52.6}, 100.0),
            52.6,
        )

    def test_id_only_close_does_not_report_flat_when_qty_unchanged(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.sandbox = True
        conn.exchange = MagicMock()
        conn.exchange.market.return_value = {
            "limits": {"amount": {"min": 0.001, "max": 100000.0}, "cost": {"min": 1.0}}
        }
        conn.exchange.amount_to_precision = lambda _s, q: float(q)

        def _ccxt(fn, label=""):
            del label
            return fn()

        def create_order(_symbol, _typ, _side, qty, _price, _params):
            return {"id": "ghost", "symbol": "PRESPCX/USDT", "filled": None, "status": None}

        conn._ccxt = _ccxt  # type: ignore[method-assign]
        conn.exchange.create_order = create_order
        conn._size_amount = lambda _s, amount, _last: float(amount)  # type: ignore[method-assign]
        conn._close_chunk_qty = lambda _s, rem, _m: min(float(rem), 50.0)  # type: ignore[method-assign]
        conn.fetch_open_position = lambda _symbol: {  # type: ignore[method-assign]
            "open": True,
            "contracts": 1000.0,
            "side": "long",
            "error": None,
        }

        with patch("connectors.bitget_paper.time.sleep", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                conn._reduce_until_flat(
                    "PRESPCX/USDT",
                    "sell",
                    1000.0,
                    151.61,
                    [{"reduceOnly": True, "hedged": False}],
                    fraction=1.0,
                )
        self.assertIn("did not change", str(caught.exception))


class PositionMarkTests(unittest.TestCase):
    def test_mark_is_not_copied_into_the_entry(self) -> None:
        snap = normalize_position(
            {
                "symbol": "PRESPCX/USDT",
                "side": "long",
                "contracts": 1000.0,
                "entryPrice": None,
                "markPrice": 151.61,
                "unrealizedPnl": 0,
                "percentage": 0,
                "info": {"markPrice": "151.61", "total": "1000"},
            }
        )
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertIsNone(snap["entry_price"])
        self.assertEqual(snap["mark_price"], 151.61)
        self.assertIsNone(snap["pnl_pct"])

    def test_live_ticker_replaces_a_stuck_exchange_mark(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.fetch_ticker = lambda symbol: {  # type: ignore[method-assign]
            "ok": True,
            "last": 148.2,
            "bid": 148.1,
            "ask": 148.3,
            "mark": 148.22,
            "source": "bitget.mainnet",
        }
        snap = {
            "symbol": "PRESPCX/USDT",
            "side": "buy",
            "contracts": 1000.0,
            "entry_price": 153.63,
            "mark_price": 153.63,
            "pnl_pct": 0.0,
            "pnl_usdt": 0.0,
        }
        conn._overlay_live_mark(snap)
        self.assertAlmostEqual(snap["mark_price"], 148.22)
        self.assertAlmostEqual(snap["entry_price"], 153.63)
        self.assertLess(snap["pnl_pct"], 0.0)
        self.assertEqual(snap["mark_source"], "bitget.mainnet")


class TelegramTimeoutTests(unittest.TestCase):
    def test_timeout_line_hides_the_bot_token(self) -> None:
        raw = (
            "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
            "with url: /bot123456:ABCDEF/getUpdates (Caused by ConnectTimeoutError"
            "('Connection to api.telegram.org timed out. (connect timeout=30)'))"
        )
        line = _telegram_fault(RuntimeError(raw))
        self.assertEqual(line, "connection to api.telegram.org timed out")
        self.assertNotIn("ABCDEF", line)
        self.assertNotIn("getUpdates", line)


if __name__ == "__main__":
    unittest.main()
