"""Phase 1: live wire, L2 spread veto, board memory."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents.analyst import _parse_with_xml, _to_triggers, pick_symbol_from_news, snap_to_universe
from agents.risk_manager import SPREAD_VETO_PCT, VETO_REASON_SPREAD, _is_illiquid, _spread_pct
from connectors.bitget_paper import _book_payload
from core.memory import BoardMemory


class SpreadTests(unittest.TestCase):
    def test_book_spread_above_half_percent(self) -> None:
        book = _book_payload(
            "NVDA/USDT:USDT",
            [[100.0, 1.0]],
            [[100.6, 1.0]],
            ok=True,
            source="l2",
            error=None,
        )
        self.assertIsNotNone(book["spread_pct"])
        self.assertGreater(book["spread_pct"], SPREAD_VETO_PCT)
        self.assertTrue(_is_illiquid(book, {}, book["spread_pct"]))

    def test_book_spread_tight_is_liquid(self) -> None:
        book = _book_payload(
            "NVDA/USDT:USDT",
            [[100.0, 2.0]],
            [[100.2, 2.0]],
            ok=True,
            source="l2",
            error=None,
        )
        self.assertIsNotNone(book["spread_pct"])
        self.assertLess(book["spread_pct"], SPREAD_VETO_PCT)
        self.assertFalse(_is_illiquid(book, {}, book["spread_pct"]))

    def test_crossed_book_is_illiquid(self) -> None:
        book = _book_payload(
            "NVDA/USDT:USDT",
            [[101.0, 1.0]],
            [[100.0, 1.0]],
            ok=True,
            source="l2",
            error=None,
        )
        self.assertTrue(book["crossed"])
        spread = _spread_pct(book, {})
        self.assertTrue(_is_illiquid(book, {}, spread))
        self.assertEqual(VETO_REASON_SPREAD, "Illiquid Market / High Spread")


class RssTests(unittest.TestCase):
    def test_rss_xml_top_headlines(self) -> None:
        xml = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item>
            <title>Nvidia supply chatter lifts rToken tape</title>
            <description>Weekend GPU leak</description>
            <link>https://example.com/a</link>
            <pubDate>Wed, 10 Sep 2026 08:00:00 GMT</pubDate>
          </item>
          <item>
            <title>Bitcoin ETF flows reverse</title>
            <description>Crypto tape</description>
            <link>https://example.com/b</link>
            <pubDate>Wed, 10 Sep 2026 09:00:00 GMT</pubDate>
          </item>
          <item>
            <title>Apple services beat leaks</title>
            <description>Megacap</description>
            <link>https://example.com/c</link>
            <pubDate>Wed, 10 Sep 2026 10:00:00 GMT</pubDate>
          </item>
        </channel></rss>
        """
        items = _parse_with_xml(xml, "Yahoo Finance")
        triggers = _to_triggers(items, limit=3)
        self.assertEqual(len(triggers), 3)
        self.assertEqual(triggers[0].headline, "Apple services beat leaks")
        self.assertIn("rAAPL/USDT", triggers[0].rtoken_map)
        self.assertEqual(triggers[0].source, "Yahoo Finance")


class UniverseTests(unittest.TestCase):
    UNIVERSE = [
        "AAPL/USDT:USDT",
        "NVDA/USDT:USDT",
        "MSFT/USDT:USDT",
        "GOOG/USDT:USDT",
        "TSLA/USDT:USDT",
    ]

    def test_iphone_news_maps_to_aapl(self) -> None:
        picked = pick_symbol_from_news(
            ["Dan Ives Says Apple’s $1,999 Foldable Could Drive iPhone Revenue"],
            self.UNIVERSE,
        )
        self.assertEqual(picked, "AAPL/USDT:USDT")

    def test_ev_news_maps_to_tsla(self) -> None:
        picked = pick_symbol_from_news(["Tesla Cybertruck production ramps in Texas"], self.UNIVERSE)
        self.assertEqual(picked, "TSLA/USDT:USDT")

    def test_snap_rejects_off_list_rnvda(self) -> None:
        self.assertEqual(
            snap_to_universe("rNVDA/USDT", self.UNIVERSE, ["Nvidia CUDA conference"]),
            "NVDA/USDT:USDT",
        )
        self.assertEqual(snap_to_universe("GOOGL", self.UNIVERSE), "GOOG/USDT:USDT")
        self.assertIn(snap_to_universe("DOGE/USDT", self.UNIVERSE), self.UNIVERSE)


class MemoryTests(unittest.TestCase):
    def test_memory_ring_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json", max_trades=5)
            for i in range(6):
                mem.record(
                    news_context=[f"headline-{i}"],
                    decision={
                        "action": "EXECUTE" if i % 2 == 0 else "STAND_DOWN",
                        "side": "buy",
                        "symbol": "NVDA/USDT:USDT",
                        "consensus": "MAJORITY",
                        "verdict": "REDUCE",
                        "thesis": f"thesis-{i}",
                        "model": "gemini-3.8-flash",
                    },
                    result={"ok": i % 2 == 0, "status": "submitted" if i % 2 == 0 else "VETOED"},
                )
            rows = mem.recent()
            self.assertEqual(len(rows), 5)
            self.assertEqual(rows[0]["news_context"], ["headline-1"])
            self.assertEqual(rows[-1]["news_context"], ["headline-5"])
            block = mem.prompt_block()
            self.assertNotIn("headline-0", block)
            self.assertIn("headline-5", block)
            self.assertEqual(rows[-1]["decision"]["model"], "gemini-3.8-flash")


if __name__ == "__main__":
    unittest.main()
