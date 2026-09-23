"""Futures-only execution and guest Telegram access."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from connectors.bitget_paper import (
    BitgetPaperConnector,
    _select_equity_universe,
    _usdt_fallback_universe,
)
from core.positions import PositionDesk
from utils.commands import (
    ACCESS_DENIED,
    CommandDesk,
    TelegramCommandLoop,
    guest_access_html,
)


class _CaptureNotifier:
    def __init__(self, chat_id: str = "42") -> None:
        self.chat_id = chat_id
        self.token = "test-token"
        self.enabled = True
        self.sent: list[tuple[str, str]] = []
        self.callbacks: list[str] = []

    def send_html_sync(self, text: str, reply_markup=None, chat_id: str | None = None) -> dict:
        del reply_markup
        self.sent.append((str(chat_id or ""), text))
        return {"message_id": 1}

    def reply(self, text: str) -> None:
        self.sent.append((self.chat_id, text))

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        del callback_id
        self.callbacks.append(text)

    def edit_html(self, message_id: int, text: str, reply_markup=None, chat_id: str | None = None) -> dict:
        del message_id, reply_markup
        self.sent.append((str(chat_id or ""), text))
        return {}


def _message(chat_id: str, text: str, *, user_id: str = "9001", first_name: str = "Ada") -> dict:
    return {
        "message": {
            "text": text,
            "chat": {"id": chat_id},
            "from": {"id": user_id, "first_name": first_name, "username": "ada"},
        }
    }


class FuturesUniverseTests(unittest.TestCase):
    def test_fallback_cannot_reintroduce_spot(self) -> None:
        markets = {
            "BTC/USDT": {"base": "BTC", "quote": "USDT", "spot": True},
            "PRESPCX/USDT": {"base": "PRESPCX", "quote": "USDT", "spot": True},
            "PRESPCX/USDT:USDT": {"base": "PRESPCX", "quote": "USDT", "swap": True},
        }
        uni = _usdt_fallback_universe(markets)
        self.assertEqual(uni, _select_equity_universe(markets))
        self.assertEqual(uni, ["PRESPCX/USDT:USDT"])

    def test_execute_refuses_spot_symbol_when_only_spot_is_listed(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.sandbox = True
        conn.exchange = MagicMock()
        conn.exchange.markets = {
            "BTC/USDT": {"base": "BTC", "quote": "USDT", "spot": True, "swap": False},
        }
        conn.exchange.options = {"defaultType": "swap"}
        conn._order_lock = __import__("threading").RLock()
        out = conn.execute_paper_order("BTC/USDT", "buy", 0.01, "hash")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "NOT_PERP")
        conn.exchange.create_order.assert_not_called()

    def test_open_book_reads_swap_positions_once(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.exchange = MagicMock()
        conn.exchange.fetch_positions.return_value = [
            {
                "symbol": "PRESPCX/USDT:USDT",
                "contracts": 12.0,
                "side": "long",
                "entryPrice": 1.5,
                "markPrice": 1.6,
                "info": {"openPriceAvg": "1.5", "markPrice": "1.6"},
            }
        ]
        conn._ccxt = lambda fn, label="": fn()  # type: ignore[method-assign]
        conn._overlay_live_mark = lambda snap: None  # type: ignore[method-assign]
        book = conn.fetch_open_book()
        self.assertEqual(len(book), 1)
        self.assertEqual(book[0]["symbol"], "PRESPCX/USDT:USDT")
        conn.exchange.fetch_positions.assert_called_once()
        args, kwargs = conn.exchange.fetch_positions.call_args
        del kwargs
        self.assertIsNone(args[0])
        self.assertEqual(args[1].get("type"), "swap")
        self.assertEqual(args[1].get("productType"), "USDT-FUTURES")
        conn.exchange.fetch_balance.assert_not_called()


class GuestAccessTests(unittest.TestCase):
    def test_start_greets_by_name_and_prints_user_id(self) -> None:
        note = _CaptureNotifier(chat_id="42")
        loop = TelegramCommandLoop(note, CommandDesk(MagicMock(), PositionDesk()))  # type: ignore[arg-type]
        loop._handle(_message("777", "/start", user_id="55501", first_name="Grace"))
        self.assertTrue(note.sent)
        chat_id, html = note.sent[-1]
        self.assertEqual(chat_id, "777")
        self.assertIn("Grace", html)
        self.assertIn("55501", html)
        self.assertIn("Guest", html)
        self.assertIn(ACCESS_DENIED, html)

    def test_guest_price_is_served_and_closeall_is_denied(self) -> None:
        bitget = MagicMock()
        bitget.fetch_live_price.return_value = {
            "ok": True,
            "symbol": "AAPL/USDT:USDT",
            "last": 220.0,
            "bid": 219.5,
            "ask": 220.5,
            "high": 225.0,
            "low": 210.0,
            "volume_24h": 1000.0,
            "market_cap": None,
            "notional_liquidity": 50_000.0,
            "source": "bitget.mainnet",
        }
        note = _CaptureNotifier(chat_id="42")
        desk = CommandDesk(bitget, PositionDesk())
        loop = TelegramCommandLoop(note, desk)  # type: ignore[arg-type]
        loop._handle(_message("777", "/price AAPL"))
        self.assertEqual(note.sent[-1][0], "777")
        self.assertIn("220", note.sent[-1][1])
        bitget.close_all.assert_not_called()
        loop._handle(_message("777", "/closeall"))
        self.assertIn(ACCESS_DENIED, note.sent[-1][1])
        bitget.close_all.assert_not_called()
        loop._handle(_message("777", "BTC/USDT BUY 10"))
        self.assertIn(ACCESS_DENIED, note.sent[-1][1])
        loop._handle(_message("777", "BUY 1000 USDT BTC"))
        self.assertIn(ACCESS_DENIED, note.sent[-1][1])
        bitget.execute_spot_market.assert_not_called()

    def test_guest_chatter_is_not_silent(self) -> None:
        note = _CaptureNotifier(chat_id="42")
        loop = TelegramCommandLoop(note, CommandDesk(None, PositionDesk()))  # type: ignore[arg-type]
        loop._handle(_message("777", "hello evaluators", first_name="Sam"))
        self.assertTrue(note.sent)
        self.assertIn("Sam", note.sent[-1][1])
        self.assertIn("9001", note.sent[-1][1])

    def test_operator_spot_command_hits_the_spot_wallet(self) -> None:
        bitget = MagicMock()
        bitget.execute_spot_market.return_value = {
            "ok": True,
            "status": "closed",
            "symbol": "BTC/USDT",
            "side": "buy",
            "amount": 0.01,
            "price": 100000.0,
            "account_balance_before": 10000.0,
            "account_balance_after": 9000.0,
            "account_balance_change": -1000.0,
        }
        note = _CaptureNotifier(chat_id="42")
        loop = TelegramCommandLoop(note, CommandDesk(bitget, PositionDesk()))  # type: ignore[arg-type]
        loop._handle(_message("42", "BUY 1000 USDT BTC", user_id="42", first_name="Op"))
        bitget.execute_spot_market.assert_called_once_with("BTC", "buy", 1000.0)
        self.assertEqual(note.sent[-1][0], "42")
        self.assertIn("SPOT FILLED", note.sent[-1][1])
        self.assertIn("9000", note.sent[-1][1])

    def test_operator_closeall_still_runs(self) -> None:
        bitget = MagicMock()
        bitget.close_all.return_value = []
        note = _CaptureNotifier(chat_id="42")
        desk = CommandDesk(bitget, PositionDesk())
        loop = TelegramCommandLoop(note, desk)  # type: ignore[arg-type]
        loop._handle(_message("42", "/closeall", user_id="42", first_name="Op"))
        bitget.close_all.assert_called_once()
        self.assertEqual(note.sent[-1][0], "42")

    def test_guest_card_contains_identity(self) -> None:
        html = guest_access_html("Lin", "4242")
        self.assertIn("Lin", html)
        self.assertIn("4242", html)
        self.assertIn(ACCESS_DENIED, html)


if __name__ == "__main__":
    unittest.main()
