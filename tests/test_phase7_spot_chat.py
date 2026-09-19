"""Phase 7: Telegram Spot chatbox parser, tickets, SPOT-only resolver."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from connectors.bitget_paper import BitgetPaperConnector, _is_spot_market
from utils.commands import CommandDesk, parse_command
from core.positions import PositionDesk
from utils.spot_chat import (
    CALLBACK_CANCEL,
    CALLBACK_CONFIRM,
    consume_ticket,
    issue_ticket,
    parse_callback,
    parse_spot_intent,
)


class ParseSpotIntentTests(unittest.TestCase):
    def test_nvda_usdt_buy_10(self) -> None:
        intent = parse_spot_intent("NVDA/USDT BUY $10")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.symbol, "NVDA/USDT")
        self.assertEqual(intent.side, "buy")
        self.assertEqual(intent.quote_usdt, 10.0)

    def test_nvda_buy_10_usdt(self) -> None:
        intent = parse_spot_intent("NVDA BUY 10 USDT")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.symbol, "NVDA")
        self.assertEqual(intent.side, "buy")
        self.assertEqual(intent.quote_usdt, 10.0)

    def test_buy_btc_10(self) -> None:
        intent = parse_spot_intent("BTC/USDT BUY 10")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.symbol, "BTC/USDT")
        self.assertEqual(intent.side, "buy")

    def test_slash_buy_with_amount(self) -> None:
        intent = parse_spot_intent("/buy NVDA 10")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.side, "buy")
        self.assertEqual(intent.symbol, "NVDA")
        self.assertEqual(intent.quote_usdt, 10.0)

    def test_strips_swap_suffix_for_spot_lookup(self) -> None:
        intent = parse_spot_intent("NVDA/USDT:USDT SELL 12")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.symbol, "NVDA/USDT")
        self.assertEqual(intent.side, "sell")

    def test_desk_commands_are_not_intents(self) -> None:
        self.assertIsNone(parse_spot_intent("/positions"))
        self.assertIsNone(parse_spot_intent("/close AAPL"))
        self.assertIsNone(parse_spot_intent("closeall"))
        self.assertIsNone(parse_spot_intent("/buy AAPL"))
        self.assertIsNone(parse_spot_intent("hello world"))


class TicketSecurityTests(unittest.TestCase):
    def test_callback_roundtrip_and_wrong_chat(self) -> None:
        ticket = issue_ticket(
            chat_id="111",
            symbol="BTC/USDT",
            side="buy",
            quote_usdt=10.0,
            last=50000.0,
            qty=0.0002,
            balance_usdt=100.0,
        )
        parsed = parse_callback(f"{CALLBACK_CONFIRM}:{ticket.id}")
        self.assertEqual(parsed, (CALLBACK_CONFIRM, ticket.id))
        self.assertIsNone(consume_ticket(ticket.id, chat_id="999", action=CALLBACK_CONFIRM))
        got = consume_ticket(ticket.id, chat_id="111", action=CALLBACK_CONFIRM)
        self.assertIsNotNone(got)
        self.assertIsNone(consume_ticket(ticket.id, chat_id="111", action=CALLBACK_CONFIRM))

    def test_cancel_token(self) -> None:
        ticket = issue_ticket(
            chat_id="5",
            symbol="ETH/USDT",
            side="sell",
            quote_usdt=8.0,
            last=2000.0,
            qty=0.004,
            balance_usdt=50.0,
        )
        parsed = parse_callback(f"{CALLBACK_CANCEL}:{ticket.id}")
        self.assertEqual(parsed[0], CALLBACK_CANCEL)
        got = consume_ticket(ticket.id, chat_id="5", action=CALLBACK_CANCEL)
        self.assertEqual(got.status, "cancelled")

    def test_garbage_callback_rejected(self) -> None:
        self.assertIsNone(parse_callback("confirm-all"))
        self.assertIsNone(parse_callback("s1:"))
        self.assertIsNone(parse_callback("s1:../etc"))


class SpotMarketGuardTests(unittest.TestCase):
    def test_swap_symbol_is_not_spot(self) -> None:
        self.assertFalse(_is_spot_market("NVDA/USDT:USDT", {"swap": True, "spot": False}))
        self.assertTrue(_is_spot_market("NVDA/USDT", {"spot": True, "swap": False}))

    def test_resolve_skips_perps(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.exchange = MagicMock()
        conn.exchange.markets = {
            "NVDA/USDT:USDT": {"spot": False, "swap": True, "base": "NVDA", "quote": "USDT"},
            "NVDA/USDT": {"spot": True, "swap": False, "base": "NVDA", "quote": "USDT"},
            "BTC/USDT": {"spot": True, "swap": False, "base": "BTC", "quote": "USDT"},
        }
        conn._ccxt = lambda fn, label="": fn()  # type: ignore[method-assign]
        self.assertEqual(conn.resolve_spot_symbol("NVDA"), "NVDA/USDT")
        self.assertEqual(conn.resolve_spot_symbol("NVDA/USDT:USDT"), "NVDA/USDT")
        self.assertEqual(conn.resolve_spot_symbol("BTC/USDT"), "BTC/USDT")
        self.assertIsNone(conn.resolve_spot_symbol("NOTAREAL"))

    def test_execute_spot_refuses_swap_listing(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.sandbox = True
        conn.exchange = MagicMock()
        conn.exchange.markets = {
            "DOGE/USDT:USDT": {"spot": False, "swap": True, "base": "DOGE", "quote": "USDT"},
        }
        conn.exchange.options = {"defaultType": "swap"}
        conn._ccxt = lambda fn, label="": fn()  # type: ignore[method-assign]
        out = conn.execute_spot_market("DOGE/USDT:USDT", "buy", 10.0)
        self.assertFalse(out["ok"])
        self.assertIn("SPOT", str(out.get("error") or "").upper())

    def test_slash_buy_without_amount_still_disabled_on_desk(self) -> None:
        desk = CommandDesk(None, PositionDesk())
        result = desk.handle("/buy AAPL", source="terminal")
        self.assertFalse(result.ok)
        self.assertIn("disabled", result.plain.lower())
        self.assertEqual(parse_command("/positions")[0], "positions")


if __name__ == "__main__":
    unittest.main()
