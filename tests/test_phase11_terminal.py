"""Phase 11: Telegram headless terminal — inline dashboard, 100% real callbacks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.memory import BoardMemory, build_engine_snapshot
from core.positions import PositionDesk
from utils.commands import (
    CommandDesk,
    TelegramCommandLoop,
    desk_keyboard,
    flatten_keyboard,
    parse_command,
    parse_desk_callback,
)
from utils.spot_chat import CALLBACK_CONFIRM, confirm_keyboard, parse_callback, parse_spot_intent


def _desk(memory: BoardMemory, bitget: MagicMock | None = None) -> CommandDesk:
    return CommandDesk(bitget, PositionDesk(), memory=memory)


class ParseMenuTests(unittest.TestCase):
    def test_menu_and_dashboard_alias(self) -> None:
        self.assertEqual(parse_command("/menu"), ("menu", ""))
        self.assertEqual(parse_command("/dashboard"), ("menu", ""))
        self.assertEqual(parse_command("menu"), ("menu", ""))
        self.assertIsNone(parse_spot_intent("/menu"))
        self.assertIsNone(parse_spot_intent("/dashboard"))

    def test_desk_callbacks_do_not_steal_spot_tickets(self) -> None:
        self.assertEqual(parse_desk_callback("nx:status"), "status")
        self.assertEqual(parse_desk_callback("nx:pnl"), "pnl")
        self.assertEqual(parse_desk_callback("nx:positions"), "positions")
        self.assertEqual(parse_desk_callback("nx:closeall"), "closeall_ask")
        self.assertEqual(parse_desk_callback("nx:closeall_yes"), "closeall_run")
        self.assertEqual(parse_desk_callback("nx:home"), "menu")
        self.assertIsNone(parse_desk_callback(f"{CALLBACK_CONFIRM}:abc123XYZ"))
        self.assertEqual(parse_callback(f"{CALLBACK_CONFIRM}:abc123XYZ_-"), (CALLBACK_CONFIRM, "abc123XYZ_-"))


class KeyboardTests(unittest.TestCase):
    def test_pad_has_four_live_actions(self) -> None:
        rows = desk_keyboard()["inline_keyboard"]
        labels = [cell["text"] for row in rows for cell in row]
        data = [cell["callback_data"] for row in rows for cell in row]
        self.assertTrue(any("Live Market Status" in t for t in labels))
        self.assertTrue(any("My Real PnL" in t for t in labels))
        self.assertTrue(any("Open Positions" in t for t in labels))
        self.assertTrue(any("Force Close All" in t for t in labels))
        self.assertEqual(
            set(data),
            {"nx:status", "nx:pnl", "nx:positions", "nx:closeall", "nx:home"},
        )

    def test_spot_confirm_keyboard_untouched(self) -> None:
        keys = confirm_keyboard("ticket01ab")
        texts = [cell["text"] for row in keys["inline_keyboard"] for cell in row]
        self.assertIn("✅ CONFIRM TRADE", texts)
        self.assertIn("❌ CANCEL", texts)


class RealDashboardTests(unittest.TestCase):
    def test_menu_uses_live_ledger_and_oracle_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            mem.note_close(
                {"ok": True, "kind": "TP", "pnl_usdt": 2.40, "symbol": "AAPL/USDT:USDT"}
            )
            mem.record_snapshot(
                build_engine_snapshot(
                    news_context=["Nvidia raises guidance after earnings beat"],
                    brief={
                        "primary_symbol": "NVDA/USDT:USDT",
                        "side": "buy",
                        "conviction": 71,
                        "sentiment_score": 63.41,
                        "thesis": "Guidance raise.",
                    },
                    risk={"verdict": "CLEAR", "rsi": 54.2, "ta_verdict": "PASS"},
                    decision={"action": "EXECUTE", "symbol": "NVDA/USDT:USDT", "side": "buy"},
                    daily=mem.daily_state(),
                )
            )
            result = _desk(mem).handle("/menu", source="telegram")
            self.assertTrue(result.ok)
            self.assertEqual(result.cmd, "menu")
            self.assertIn("+2.40 USDT", result.plain)
            self.assertIn("1W / 0L", result.plain)
            self.assertIn("BULL 63.41", result.plain)
            self.assertIn("71/100", result.plain)
            self.assertIn("ARMED", result.plain)
            self.assertNotIn("NO SCAN YET", result.plain)

    def test_unscanned_menu_does_not_invent_sentiment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            result = _desk(mem).handle("/menu", source="telegram")
            self.assertIn("NO SCAN YET", result.plain)
            self.assertIn("+0.00 USDT", result.plain)
            self.assertIn("0W / 0L", result.plain)


class CallbackUsesExactCommandsTests(unittest.TestCase):
    def test_pnl_and_status_buttons_match_slash_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            mem.note_close(
                {"ok": True, "kind": "TP", "pnl_usdt": 1.25, "symbol": "MSFT/USDT:USDT"}
            )
            mem.record_snapshot(
                build_engine_snapshot(
                    news_context=["Weekend tape"],
                    brief={
                        "primary_symbol": "NONE",
                        "side": "none",
                        "conviction": 0,
                        "sentiment_score": 50.17,
                        "thesis": "Standing down.",
                    },
                    risk={"verdict": "CLEAR", "ta_verdict": "SKIPPED"},
                    decision={"action": "STAND_DOWN", "symbol": "NONE", "side": "none"},
                    daily=mem.daily_state(),
                )
            )
            desk = _desk(mem)
            loop = TelegramCommandLoop(MagicMock(), desk)
            pnl_slash = desk.handle("/pnl", source="telegram")
            st_slash = desk.handle("/status", source="telegram")
            pnl_html, _ = loop._desk_view("pnl")
            st_html, _ = loop._desk_view("status")
            self.assertEqual(pnl_html, pnl_slash.html)
            self.assertEqual(st_html, st_slash.html)
            self.assertIn("+1.25 USDT", pnl_html)
            self.assertIn("NEUTRAL 50.17", st_html)

    def test_positions_button_matches_slash_command(self) -> None:
        bitget = MagicMock()
        bitget.fetch_open_book.return_value = [
            {
                "open": True,
                "symbol": "NVDA/USDT:USDT",
                "side": "buy",
                "contracts": 1.0,
                "entry_price": 100.0,
                "mark_price": 104.0,
                "pnl_usdt": 4.0,
                "pnl_pct": 4.0,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            desk = _desk(mem, bitget)
            loop = TelegramCommandLoop(MagicMock(), desk)
            slash = desk.handle("/positions", source="telegram")
            html, _ = loop._desk_view("positions")
            self.assertEqual(html, slash.html)
            self.assertIn("NVDA/USDT:USDT", html)
            self.assertIn("+4.00 USDT", html)

    def test_flatten_confirm_uses_live_book_then_closeall(self) -> None:
        bitget = MagicMock()
        bitget.fetch_open_book.return_value = [
            {
                "open": True,
                "symbol": "AAPL/USDT:USDT",
                "side": "buy",
                "contracts": 1.0,
                "entry_price": 100.0,
                "mark_price": 101.0,
                "pnl_usdt": 1.0,
                "pnl_pct": 1.0,
            }
        ]
        bitget.close_all.return_value = [
            {
                "ok": True,
                "status": "CLOSED",
                "symbol": "AAPL/USDT:USDT",
                "side": "buy",
                "pnl_pct": 1.0,
                "pnl_usdt": 1.0,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            desk = _desk(mem, bitget)
            loop = TelegramCommandLoop(MagicMock(), desk)
            prompt, markup = loop._desk_view("closeall_ask")
            self.assertIn("AAPL/USDT:USDT", prompt)
            self.assertIn("+1.00 USDT", prompt)
            self.assertEqual(markup, flatten_keyboard())
            result_html, _ = loop._desk_view("closeall_run")
            slash = desk.handle("/closeall", source="telegram")
            self.assertIn("CLOSED AAPL/USDT:USDT", result_html)
            bitget.close_all.assert_called()
            self.assertIn("CLOSED", slash.plain)


if __name__ == "__main__":
    unittest.main()
