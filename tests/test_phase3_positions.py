"""Position desk: SL/TP both sides, PnL math, thesis/trail exits, Telegram commands."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from connectors.bitget_paper import protective_prices, unrealized_pnl
from core.positions import (
    PARTIAL_ARM_PCT,
    TRAIL_ARM_PCT,
    PositionDesk,
    _exit_reason,
    _thesis_invalidated,
    ticker_root,
)
from core.schemas import AnalystBrief
from utils.commands import CommandDesk, parse_command, _match_symbol
from utils.notifier import TelegramNotifier, escape_html


class ProtectiveBothSidesTests(unittest.TestCase):
    def test_long_defaults_remain_2_and_5(self) -> None:
        sl, tp = protective_prices(100.0)
        self.assertAlmostEqual(sl, 98.0)
        self.assertAlmostEqual(tp, 105.0)

    def test_short_sl_is_above_entry(self) -> None:
        sl, tp = protective_prices(100.0, "sell")
        self.assertAlmostEqual(sl, 102.0)
        self.assertAlmostEqual(tp, 95.0)

    def test_atr_widens_but_stays_capped(self) -> None:
        sl, tp = protective_prices(100.0, "buy", atr=4.0)
        self.assertLess(sl, 100.0)
        self.assertGreater(tp, 100.0)
        self.assertGreaterEqual(100.0 - sl, 1.2)
        self.assertLessEqual(100.0 - sl, 5.0)


class PnlMathTests(unittest.TestCase):
    def test_long_profit(self) -> None:
        usdt, pct = unrealized_pnl(100.0, 105.0, 2.0, "buy")
        self.assertAlmostEqual(usdt, 10.0)
        self.assertAlmostEqual(pct, 5.0)

    def test_short_profit(self) -> None:
        usdt, pct = unrealized_pnl(100.0, 95.0, 2.0, "sell")
        self.assertAlmostEqual(usdt, 10.0)
        self.assertAlmostEqual(pct, 5.0)


class DeskLogicTests(unittest.TestCase):
    def test_stay_away_invalidates(self) -> None:
        brief = AnalystBrief(
            thesis="toxic",
            rationale="miss",
            primary_symbol="NONE",
            side="none",
            conviction=70,
            news_bad="AAPL earnings miss",
            stay_away=["AAPL — earnings miss"],
        )
        reason = _thesis_invalidated(
            {"symbol": "AAPL/USDT:USDT", "side": "buy"}, brief
        )
        self.assertIn("stay_away", reason)

    def test_idle_oracle_does_not_flatten_unrelated_book(self) -> None:
        brief = AnalystBrief(
            thesis="idle",
            rationale="none",
            primary_symbol="NONE",
            side="none",
            conviction=0,
        )
        self.assertEqual(
            _thesis_invalidated({"symbol": "TSLA/USDT:USDT", "side": "buy"}, brief),
            "",
        )

    def test_trail_and_partial(self) -> None:
        pos = {"symbol": "NVDA/USDT:USDT", "side": "buy", "pnl_pct": 4.2}
        self.assertEqual(_exit_reason(pos, None, {}, 4.2), "")
        self.assertTrue(
            _exit_reason(
                {"symbol": "NVDA/USDT:USDT", "side": "buy", "pnl_pct": 26.0},
                None,
                {},
                26.0,
            ).startswith("PARTIAL")
        )
        self.assertEqual(PARTIAL_ARM_PCT, 25.0)
        self.assertEqual(TRAIL_ARM_PCT, 3.0)
        trailed = _exit_reason(
            {"symbol": "NVDA/USDT:USDT", "side": "buy", "pnl_pct": 2.0},
            None,
            {"partial_taken": True},
            3.5,
        )
        self.assertEqual(trailed, "TRAIL")

    def test_record_and_drop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = PositionDesk(path=Path(tmp) / "desk.json")
            desk.record_open(
                {
                    "ok": True,
                    "symbol": "AAPL/USDT:USDT",
                    "side": "buy",
                    "entry_price": 10.0,
                    "amount": 1.0,
                    "sl_price": 9.8,
                    "tp_price": 10.5,
                },
                AnalystBrief(thesis="iphone beat", rationale="beat", primary_symbol="AAPL/USDT:USDT"),
            )
            stored = desk._load()
            self.assertTrue(any("AAPL" in k for k in stored))
            desk.drop("AAPL/USDT:USDT", "buy")
            self.assertFalse(desk._load())


class CommandParseTests(unittest.TestCase):
    def test_close_alias_matches_root(self) -> None:
        book = [{"symbol": "AAPL/USDT:USDT", "side": "buy"}]
        self.assertEqual(_match_symbol("AAPL", book)["symbol"], "AAPL/USDT:USDT")
        self.assertIsNone(_match_symbol("TSLA", book))

    def test_slash_and_bare_commands(self) -> None:
        self.assertEqual(parse_command("/positions"), ("positions", ""))
        self.assertEqual(parse_command("pos"), ("positions", ""))
        self.assertEqual(parse_command("/close AAPL"), ("close", "AAPL"))
        self.assertEqual(parse_command("closeall"), ("closeall", ""))
        self.assertEqual(parse_command("flatten"), ("closeall", ""))
        self.assertEqual(parse_command("/help"), ("help", ""))

    def test_manual_open_is_refused(self) -> None:
        desk = CommandDesk(None, PositionDesk())
        result = desk.handle("/buy AAPL", source="terminal")
        self.assertFalse(result.ok)
        self.assertIn("disabled", result.plain.lower())

    def test_help_is_shared(self) -> None:
        desk = CommandDesk(None, PositionDesk())
        result = desk.handle("/help", source="terminal")
        self.assertTrue(result.ok)
        self.assertIn("/positions", result.plain)
        self.assertIn("/close", result.html)

    def test_ticker_root_strips_rtoken(self) -> None:
        self.assertEqual(ticker_root("rAAPL/USDT"), "AAPL")
        self.assertEqual(ticker_root("GOOGL/USDT:USDT"), "GOOG")


class TelegramPnlTests(unittest.TestCase):
    def test_disabled_pnl_does_not_raise(self) -> None:
        n = TelegramNotifier("", "")
        n.alert_pnl(symbol="AAPL/USDT:USDT", pnl_pct=5.2, pnl_usdt=12.5, kind="TP")
        n.alert_pnl(symbol="TSLA/USDT:USDT", pnl_pct=-2.0, pnl_usdt=-3.1, kind="SL")
        n.drain(timeout=0.1)
        self.assertIn("&lt;", escape_html("<AAPL>"))

    def test_manage_closes_on_stay_away(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = PositionDesk(path=Path(tmp) / "desk.json")
            bitget = MagicMock()
            bitget.fetch_open_book.return_value = [
                {
                    "open": True,
                    "symbol": "AAPL/USDT:USDT",
                    "side": "buy",
                    "contracts": 1.0,
                    "entry_price": 100.0,
                    "mark_price": 101.0,
                    "pnl_pct": 1.0,
                    "pnl_usdt": 1.0,
                }
            ]
            bitget.close_market.return_value = {
                "ok": True,
                "status": "CLOSED",
                "symbol": "AAPL/USDT:USDT",
                "pnl_pct": 1.0,
                "pnl_usdt": 1.0,
            }
            brief = AnalystBrief(
                thesis="x",
                rationale="y",
                stay_away=["AAPL — miss"],
                conviction=80,
            )
            actions = desk.manage(bitget, brief)
            self.assertEqual(len(actions), 1)
            bitget.close_market.assert_called_once()
            self.assertIn("THESIS", actions[0].get("reason", ""))


if __name__ == "__main__":
    unittest.main()
