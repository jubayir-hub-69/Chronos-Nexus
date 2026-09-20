"""Phase 8: live mainnet BBO peg across spot, swap, and rTokens."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from connectors.bitget_paper import (
    BitgetPaperConnector,
    _looks_contract,
    _stamp_bbo,
    public_symbol_candidates,
)
from core.ta import (
    BBO_MARK_DIVERGENCE_PCT,
    bbo_peg_price,
    bbo_sane_vs_mark,
    extract_mark_price,
    mark_divergence_pct,
)


class BboMathTests(unittest.TestCase):
    def test_buy_lifts_ask_sell_hits_bid(self) -> None:
        self.assertEqual(bbo_peg_price("buy", 100.0, 101.0), 101.0)
        self.assertEqual(bbo_peg_price("long", 100.0, 101.0), 101.0)
        self.assertEqual(bbo_peg_price("sell", 100.0, 101.0), 100.0)
        self.assertEqual(bbo_peg_price("short", 100.0, 101.0), 100.0)
        self.assertIsNone(bbo_peg_price("buy", 100.0, None))
        self.assertIsNone(bbo_peg_price("none", 100.0, 101.0))

    def test_never_falls_back_to_mid(self) -> None:
        self.assertIsNone(bbo_peg_price("buy", 100.0, 0))
        self.assertIsNone(bbo_peg_price("", 100.0, 101.0))

    def test_mark_from_bitget_ticker_info(self) -> None:
        self.assertEqual(
            extract_mark_price({"last": 10, "info": {"markPrice": "12.5"}}),
            12.5,
        )
        self.assertEqual(extract_mark_price({"mark": 9.1}), 9.1)

    def test_contract_bbo_must_track_mark(self) -> None:
        self.assertTrue(bbo_sane_vs_mark(peg=100.5, mark=100.0, is_contract=True))
        blown = 100.0 * (1.0 + (BBO_MARK_DIVERGENCE_PCT + 1.0) / 100.0)
        self.assertFalse(bbo_sane_vs_mark(peg=blown, mark=100.0, is_contract=True))
        self.assertTrue(bbo_sane_vs_mark(peg=blown, mark=100.0, is_contract=False))
        self.assertAlmostEqual(mark_divergence_pct(102.0, 100.0), 2.0)


class PublicSymbolMapTests(unittest.TestCase):
    def test_rtoken_and_perp_variants(self) -> None:
        cands = public_symbol_candidates("rNVDA/USDT")
        self.assertIn("rNVDA/USDT", cands)
        self.assertIn("NVDA/USDT:USDT", cands)
        self.assertIn("NVDA/USDT", cands)

    def test_demo_seth_maps_to_eth_swap(self) -> None:
        cands = public_symbol_candidates("SETH/SUSDT:SUSDT")
        self.assertTrue(any(c.startswith("ETH/") for c in cands))
        self.assertIn("ETH/USDT:USDT", cands)

    def test_contract_shape(self) -> None:
        self.assertTrue(_looks_contract("NVDA/USDT:USDT"))
        self.assertFalse(_looks_contract("SOL/USDT"))


class FetchBboTests(unittest.TestCase):
    def _conn(self) -> BitgetPaperConnector:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.public = MagicMock()
        conn.exchange = MagicMock()
        conn.exchange.options = {"defaultType": "swap"}
        conn.public.options = {"defaultType": "swap"}
        conn.public.markets = {
            "NVDA/USDT:USDT": {"swap": True, "spot": False, "base": "NVDA", "quote": "USDT"},
            "SOL/USDT": {"swap": False, "spot": True, "base": "SOL", "quote": "USDT"},
        }
        conn.resolved_symbol = "NVDA/USDT:USDT"
        conn.preferred_symbol = "NVDA/USDT:USDT"
        conn._public_ready = True
        conn._ccxt = lambda fn, label="": fn()  # type: ignore[method-assign]
        return conn

    def test_buy_pegs_mainnet_ask(self) -> None:
        conn = self._conn()
        conn.public.fetch_order_book = MagicMock(
            return_value={"bids": [[174.0, 2.0]], "asks": [[174.4, 1.5]]}
        )
        conn.public.fetch_ticker = MagicMock(
            return_value={
                "symbol": "NVDA/USDT:USDT",
                "last": 174.2,
                "bid": 174.0,
                "ask": 174.4,
                "info": {"markPrice": "174.15"},
            }
        )
        conn.public.market = lambda s: conn.public.markets[s]
        quote = conn.fetch_bbo("NVDA/USDT:USDT", side="buy")
        self.assertTrue(quote["ok"])
        self.assertEqual(quote["source"], "mainnet_bbo")
        self.assertEqual(quote["peg"], 174.4)
        self.assertEqual(quote["best_ask"], 174.4)
        self.assertEqual(quote["best_bid"], 174.0)
        self.assertAlmostEqual(float(quote["mark"]), 174.15)

    def test_sell_pegs_mainnet_bid(self) -> None:
        conn = self._conn()
        conn.public.fetch_order_book = MagicMock(
            return_value={"bids": [[174.0, 2.0]], "asks": [[174.4, 1.5]]}
        )
        conn.public.fetch_ticker = MagicMock(
            return_value={
                "symbol": "NVDA/USDT:USDT",
                "last": 174.2,
                "bid": 174.0,
                "ask": 174.4,
                "info": {"markPrice": "174.15"},
            }
        )
        conn.public.market = lambda s: conn.public.markets[s]
        quote = conn.fetch_bbo("NVDA/USDT:USDT", side="sell")
        self.assertEqual(quote["peg"], 174.0)

    def test_spot_without_mark_does_not_crash(self) -> None:
        """BGB Spot has no markPrice — preview must still peg the ask."""
        conn = self._conn()
        conn.public.markets["BGB/USDT"] = {
            "swap": False,
            "spot": True,
            "base": "BGB",
            "quote": "USDT",
        }
        conn.exchange.markets = dict(conn.public.markets)
        conn.public.fetch_order_book = MagicMock(
            return_value={"bids": [[4.20, 10.0]], "asks": [[4.22, 8.0]]}
        )
        conn.public.fetch_ticker = MagicMock(
            return_value={
                "symbol": "BGB/USDT",
                "last": 4.21,
                "bid": 4.20,
                "ask": 4.22,
            }
        )
        conn.public.market = lambda s: conn.public.markets[s]
        quote = conn.fetch_bbo("BGB/USDT", side="buy")
        self.assertTrue(quote["ok"])
        self.assertEqual(quote["peg"], 4.22)
        self.assertIsNone(quote["mark"])
        spot = conn.fetch_spot_quote("BGB", side="buy")
        self.assertTrue(spot["ok"])
        self.assertGreater(float(spot["last"]), 0)

    def test_mark_divergence_refuses_entry(self) -> None:
        conn = self._conn()
        conn.public.fetch_order_book = MagicMock(
            return_value={"bids": [[100.0, 1.0]], "asks": [[120.0, 1.0]]}
        )
        conn.public.fetch_ticker = MagicMock(
            return_value={
                "symbol": "NVDA/USDT:USDT",
                "last": 100.0,
                "bid": 100.0,
                "ask": 120.0,
                "info": {"markPrice": "100.0"},
            }
        )
        conn.public.market = lambda s: conn.public.markets[s]
        quote = conn.fetch_bbo("NVDA/USDT:USDT", side="buy")
        self.assertFalse(quote["ok"])
        self.assertEqual(quote["error"], "MARK_DIVERGENCE")


class StampBboTests(unittest.TestCase):
    def test_open_stamps_entry_at_peg(self) -> None:
        record = {"entry_price": 1.0, "price": 1.0}
        _stamp_bbo(record, {"peg": 174.4, "best_bid": 174.0, "best_ask": 174.4, "source": "mainnet_bbo"})
        self.assertEqual(record["price"], 174.4)
        self.assertEqual(record["entry_price"], 174.4)
        self.assertEqual(record["price_source"], "mainnet_bbo")

    def test_close_keeps_original_entry(self) -> None:
        record = {"entry_price": 170.0, "price": 1.0}
        _stamp_bbo(
            record,
            {"peg": 174.0, "best_bid": 174.0, "best_ask": 174.4, "source": "mainnet_bbo"},
            as_entry=False,
        )
        self.assertEqual(record["price"], 174.0)
        self.assertEqual(record["entry_price"], 170.0)


if __name__ == "__main__":
    unittest.main()
