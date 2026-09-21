"""Phase 12: 45113 max-order-value close is sliced until the book is flat."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from connectors.bitget_paper import (
    CLOSE_CHUNK_QTY_DEFAULT,
    BitgetPaperConnector,
    _is_max_order_fault,
)
from core.retry import is_retryable


class MaxOrderFaultTests(unittest.TestCase):
    def test_45113_is_detected_and_not_retried(self) -> None:
        err = RuntimeError("bitget 45113 Maximum order value limit triggered")
        self.assertTrue(_is_max_order_fault(err))
        self.assertFalse(is_retryable(err))
        self.assertTrue(_is_max_order_fault("45112 more than the maximum order quantity"))
        self.assertFalse(_is_max_order_fault("insufficient margin"))


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


if __name__ == "__main__":
    unittest.main()
