"""Phase 1.5: Telegram menu + 100% real /pnl and /status commands."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.memory import (
    DAILY_MAX_ENTRIES,
    BoardMemory,
    build_engine_snapshot,
    live_desk_status,
    utc_today,
)
from core.positions import PositionDesk
from utils.commands import BOT_MENU, CommandDesk, parse_command, register_bot_menu
from utils.spot_chat import parse_spot_intent


def _desk(memory: BoardMemory, bitget: MagicMock | None = None) -> CommandDesk:
    return CommandDesk(bitget, PositionDesk(), memory=memory)


class TelegramMenuTests(unittest.TestCase):
    def test_native_menu_lists_required_commands(self) -> None:
        names = [row["command"] for row in BOT_MENU]
        self.assertEqual(
            names,
            ["menu", "positions", "close", "closeall", "price", "balance", "pnl", "status"],
        )
        for row in BOT_MENU:
            self.assertTrue(row["description"])

    def test_register_bot_menu_posts_set_my_commands(self) -> None:
        with patch("utils.commands.requests.post") as post:
            post.return_value.content = b'{"ok": true, "result": true}'
            post.return_value.json.return_value = {"ok": True, "result": True}
            out = register_bot_menu("123:ABC")
        self.assertTrue(out["ok"])
        self.assertEqual(out["commands"], [row["command"] for row in BOT_MENU])
        args, kwargs = post.call_args
        self.assertIn("setMyCommands", args[0])
        self.assertEqual(kwargs["json"]["commands"], BOT_MENU)

    def test_register_bot_menu_skips_without_token(self) -> None:
        self.assertEqual(register_bot_menu(""), {"ok": False, "skipped": True})


class ParseAnalyticsCommandTests(unittest.TestCase):
    def test_parse_pnl_and_status(self) -> None:
        self.assertEqual(parse_command("/pnl"), ("pnl", ""))
        self.assertEqual(parse_command("/status"), ("status", ""))
        self.assertEqual(parse_command("pnl"), ("pnl", ""))
        self.assertIsNone(parse_spot_intent("/pnl"))
        self.assertIsNone(parse_spot_intent("/status"))

    def test_help_lists_new_commands(self) -> None:
        desk = CommandDesk(None, PositionDesk())
        result = desk.handle("/help", source="telegram")
        self.assertIn("/pnl", result.plain)
        self.assertIn("/status", result.html)
        self.assertIn("/positions", result.plain)
        self.assertIn("/menu", result.plain)


class RealPnlCommandTests(unittest.TestCase):
    def test_pnl_uses_live_daily_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            mem.note_entry(symbol="AAPL/USDT:USDT", margin_usdt=2.0, order_id="e1")
            mem.note_close(
                {"ok": True, "kind": "TP", "pnl_usdt": 1.25, "symbol": "AAPL/USDT:USDT"}
            )
            mem.note_entry(symbol="MSFT/USDT:USDT", margin_usdt=1.5, order_id="e2")
            mem.note_close(
                {"ok": True, "kind": "SL", "pnl_usdt": -0.40, "symbol": "MSFT/USDT:USDT"}
            )
            session = mem.session_pnl()
            self.assertAlmostEqual(session["realized_pnl_usdt"], 0.85)
            self.assertEqual(session["wins"], 1)
            self.assertEqual(session["losses"], 1)
            self.assertEqual(session["trades_left"], 0)  # SL halt
            result = _desk(mem).handle("/pnl", source="telegram")
            self.assertTrue(result.ok)
            self.assertEqual(result.cmd, "pnl")
            self.assertIn("+0.85 USDT", result.plain)
            self.assertIn("1W / 1L", result.plain)
            self.assertIn("Trades left: 0", result.plain)
            self.assertIn("STOP_LOSS", result.plain)
            self.assertNotIn("dummy", result.plain.lower())

    def test_pnl_trades_left_is_remaining_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            mem.note_entry(symbol="NVDA/USDT:USDT", margin_usdt=1.0)
            mem.note_close(
                {"ok": True, "kind": "TRAIL", "pnl_usdt": 2.10, "symbol": "NVDA/USDT:USDT"}
            )
            result = _desk(mem).handle("/pnl", source="telegram")
            self.assertIn("+2.10 USDT", result.plain)
            self.assertIn("1W / 0L", result.plain)
            self.assertIn(f"Trades left: {DAILY_MAX_ENTRIES - 1}", result.plain)
            self.assertIn(f"1/{DAILY_MAX_ENTRIES}", result.plain)

    def test_pnl_backfills_realized_from_fills_when_ledger_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            path.write_text(
                json.dumps(
                    {
                        "trades": [],
                        "daily": {
                            "date": utc_today(),
                            "entries": 1,
                            "wins": 1,
                            "losses": 0,
                            "sl_hits": 0,
                            "deployed_usdt": 1.0,
                            "realized_pnl_usdt": 0.0,
                            "halt": "",
                            "halt_reason": "",
                            "fills": [
                                {"kind": "TP", "symbol": "AAPL/USDT:USDT", "pnl_usdt": 3.33},
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            mem = BoardMemory(path=path)
            result = _desk(mem).handle("/pnl", source="telegram")
            self.assertIn("+3.33 USDT", result.plain)
            self.assertIn("1W / 0L", result.plain)

    def test_pnl_includes_live_bitget_unrealized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            mem.note_close(
                {"ok": True, "kind": "TP", "pnl_usdt": 0.50, "symbol": "AAPL/USDT:USDT"}
            )
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
            result = _desk(mem, bitget).handle("/pnl", source="telegram")
            self.assertIn("+0.50 USDT", result.plain)
            self.assertIn("+4.00 USDT", result.plain)
            self.assertIn("Open unrealized", result.plain)

    def test_empty_day_is_real_zero_not_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            result = _desk(mem).handle("/pnl", source="telegram")
            self.assertIn("+0.00 USDT", result.plain)
            self.assertIn("0W / 0L", result.plain)
            self.assertIn(f"Trades left: {DAILY_MAX_ENTRIES}", result.plain)


class RealStatusCommandTests(unittest.TestCase):
    def test_status_reads_exact_oracle_sentinel_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            mem.record_snapshot(
                build_engine_snapshot(
                    news_context=["Nvidia raises data-center guidance after earnings beat"],
                    brief={
                        "primary_symbol": "NVDA/USDT:USDT",
                        "side": "buy",
                        "conviction": 71,
                        "sentiment_score": 63.41,
                        "thesis": "Guidance raise is the tape.",
                        "news_good": "earnings beat",
                        "news_conflict": False,
                        "news_credibility": 0.98,
                        "news_impact": "high",
                    },
                    risk={
                        "verdict": "CLEAR",
                        "rationale": "spread tight, RSI 54",
                        "rsi": 54.2,
                        "ta_verdict": "PASS",
                    },
                    decision={"action": "EXECUTE", "symbol": "NVDA/USDT:USDT", "side": "buy"},
                    result={"status": "submitted"},
                    daily=mem.daily_state(),
                )
            )
            result = _desk(mem).handle("/status", source="telegram")
            self.assertTrue(result.ok)
            self.assertEqual(result.cmd, "status")
            self.assertIn("BULL 63.41", result.plain)
            self.assertIn("71/100", result.plain)
            self.assertIn("ARMED", result.plain)
            self.assertIn("NVDA/USDT:USDT", result.plain)
            self.assertIn("CLEAR", result.plain)
            self.assertNotIn("NO SCAN YET", result.plain)
            self.assertNotIn("50.00", result.plain)

    def test_status_stand_down_is_the_live_chair_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            mem.record_snapshot(
                build_engine_snapshot(
                    news_context=["Weekend personal-finance tape, no equity catalyst"],
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
            result = _desk(mem).handle("/status", source="telegram")
            self.assertIn("NEUTRAL 50.17", result.plain)
            self.assertIn("0/100", result.plain)
            self.assertIn("STAND_DOWN", result.plain)
            self.assertIn("NONE / none", result.plain)

    def test_status_unscanned_does_not_invent_sentiment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            result = _desk(mem).handle("/status", source="telegram")
            self.assertIn("NO SCAN YET", result.plain)
            self.assertNotIn("BULL", result.plain)
            self.assertNotIn("BEAR", result.plain)
            self.assertEqual(result.plain.count("NO SCAN YET"), 2)

    def test_live_desk_status_halt_overrides_execute(self) -> None:
        self.assertEqual(live_desk_status({"halt": "WIN_STREAK"}, "EXECUTE"), "STAND_DOWN")
        self.assertEqual(live_desk_status({"halt": ""}, "EXECUTE"), "ARMED")
        self.assertEqual(live_desk_status({"halt": ""}, "STAND_DOWN"), "STAND_DOWN")


if __name__ == "__main__":
    unittest.main()
