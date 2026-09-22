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
        self.assertIsNone(parse_spot_intent("/price BGB"))
        self.assertIsNone(parse_spot_intent("/balance"))
        self.assertIsNone(parse_spot_intent("/pnl"))
        self.assertIsNone(parse_spot_intent("/status"))
        self.assertIsNone(parse_spot_intent("/menu"))
        self.assertIsNone(parse_spot_intent("/dashboard"))

    def test_bgb_buy_five_is_a_spot_ticket(self) -> None:
        intent = parse_spot_intent("BGB BUY 5 USDT")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.symbol, "BGB")
        self.assertEqual(intent.side, "buy")
        self.assertEqual(intent.quote_usdt, 5.0)

    def test_sol_usdt_large_notional_is_a_ticker(self) -> None:
        """Slash in SOL/USDT is a pair separator. $999999 must reach the wallet gate."""
        intent = parse_spot_intent("SOL/USDT BUY $999999")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.symbol, "SOL/USDT")
        self.assertEqual(intent.side, "buy")
        self.assertEqual(intent.quote_usdt, 999999.0)
        self.assertEqual(parse_command("SOL/USDT BUY $999999"), ("", ""))
        self.assertEqual(parse_command("/SOL/USDT BUY $999999"), ("", ""))
        desk = CommandDesk(None, PositionDesk())
        result = desk.handle("SOL/USDT BUY $999999", source="telegram")
        self.assertEqual(result.cmd, "")
        self.assertNotIn("Unknown command", result.plain)


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

    def test_execute_spot_rejects_insufficient_large_quote(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.sandbox = True
        conn.exchange = MagicMock()
        conn.exchange.markets = {
            "SOL/USDT": {"spot": True, "swap": False, "base": "SOL", "quote": "USDT"},
        }
        conn.exchange.options = {"defaultType": "spot"}
        conn.exchange.market = lambda s: conn.exchange.markets[s]
        conn._ccxt = lambda fn, label="": fn()  # type: ignore[method-assign]
        conn.resolve_spot_symbol = lambda q: "SOL/USDT"  # type: ignore[method-assign]
        conn.fetch_spot_quote = lambda s, side="buy": {  # type: ignore[method-assign]
            "ok": True,
            "symbol": "SOL/USDT",
            "last": 150.0,
            "peg": 150.0,
        }
        conn.fetch_spot_usdt_free = lambda: 10.0  # type: ignore[method-assign]
        conn._size_amount = lambda *a, **k: 1.0  # type: ignore[method-assign]
        out = conn.execute_spot_market("SOL/USDT", "buy", 999999.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "INSUFFICIENT_MARGIN")
        self.assertIn("10.0000", str(out.get("error") or ""))

    def test_slash_buy_without_amount_still_disabled_on_desk(self) -> None:
        desk = CommandDesk(None, PositionDesk())
        result = desk.handle("/buy AAPL", source="terminal")
        self.assertFalse(result.ok)
        self.assertIn("disabled", result.plain.lower())
        self.assertEqual(parse_command("/positions")[0], "positions")
        self.assertEqual(parse_command("/price BGB/USDT"), ("price", "BGB/USDT"))
        self.assertEqual(parse_command("/price BGB"), ("price", "BGB"))
        self.assertEqual(parse_command("/balance"), ("balance", ""))
        self.assertEqual(parse_command("/balance USDT"), ("balance", "USDT"))
        self.assertEqual(parse_command("/balance nvda"), ("balance", "nvda"))
        self.assertEqual(parse_command("bal"), ("balance", ""))
        self.assertEqual(parse_command("/pnl"), ("pnl", ""))
        self.assertEqual(parse_command("/status"), ("status", ""))


class PriceBalanceCommandTests(unittest.TestCase):
    def test_price_replies_with_mainnet_quote(self) -> None:
        bitget = MagicMock()
        bitget.fetch_live_price.return_value = {
            "ok": True,
            "symbol": "BGB/USDT",
            "last": 4.21,
            "bid": 4.20,
            "ask": 4.22,
            "mark": None,
            "source": "bitget.mainnet",
        }
        desk = CommandDesk(bitget, PositionDesk())
        result = desk.handle("/price BGB", source="telegram")
        self.assertTrue(result.ok)
        self.assertEqual(result.cmd, "price")
        self.assertIn("4.21", result.plain)
        self.assertIn("BGB/USDT", result.html)
        bitget.fetch_live_price.assert_called_once_with("BGB")

    def test_price_prints_live_24h_metrics(self) -> None:
        bitget = MagicMock()
        bitget.fetch_live_price.return_value = {
            "ok": True,
            "symbol": "BGB/USDT",
            "last": 4.21,
            "bid": 4.20,
            "ask": 4.22,
            "mark": None,
            "high": 4.55,
            "low": 4.01,
            "volume_24h": 1_250_000.0,
            "market_cap": 890_000_000.0,
            "notional_liquidity": None,
            "source": "bitget.mainnet",
        }
        desk = CommandDesk(bitget, PositionDesk())
        result = desk.handle("/price BGB", source="telegram")
        self.assertTrue(result.ok)
        self.assertIn("4.21", result.plain)
        self.assertIn("4.55", result.plain)
        self.assertIn("4.01", result.plain)
        self.assertIn("1,250,000.00", result.plain)
        self.assertIn("market cap", result.plain)
        self.assertIn("890,000,000.00", result.html)

    def test_price_uses_notional_liquidity_when_cap_is_absent(self) -> None:
        bitget = MagicMock()
        bitget.fetch_live_price.return_value = {
            "ok": True,
            "symbol": "rNVDA/USDT:USDT",
            "last": 180.5,
            "bid": 180.4,
            "ask": 180.6,
            "high": 182.0,
            "low": 176.2,
            "volume_24h": 4_200_000.0,
            "market_cap": None,
            "notional_liquidity": 250_000.0,
            "source": "bitget.mainnet",
        }
        desk = CommandDesk(bitget, PositionDesk())
        result = desk.handle("/price NVDA", source="telegram")
        self.assertTrue(result.ok)
        self.assertIn("notional liquidity", result.plain)
        self.assertIn("250,000.00", result.plain)
        self.assertNotIn("market cap", result.plain)

    def test_balance_ledger_error_is_not_a_fake_zero(self) -> None:
        bitget = MagicMock()
        bitget.fetch_asset_balance.return_value = {
            "ok": False,
            "coin": "USDT",
            "free": 0.0,
            "used": 0.0,
            "total": 0.0,
            "found": False,
            "error": "spot:timeout | swap:timeout",
        }
        desk = CommandDesk(bitget, PositionDesk())
        result = desk.handle("/balance USDT", source="telegram")
        self.assertFalse(result.ok)
        self.assertIn("timeout", result.plain)
        self.assertNotIn("0.00 USDT found", result.plain)

    def test_price_requires_symbol(self) -> None:
        desk = CommandDesk(MagicMock(), PositionDesk())
        result = desk.handle("/price", source="telegram")
        self.assertFalse(result.ok)
        self.assertIn("/price", result.plain.lower())

    def test_balance_requires_symbol(self) -> None:
        desk = CommandDesk(MagicMock(), PositionDesk())
        result = desk.handle("/balance", source="telegram")
        self.assertFalse(result.ok)
        self.assertIn("Please specify a token", result.plain)
        self.assertIn("/balance USDT", result.plain)

    def test_balance_shows_only_requested_coin(self) -> None:
        bitget = MagicMock()
        bitget.fetch_asset_balance.return_value = {
            "ok": True,
            "coin": "BGB",
            "free": 3.0,
            "total": 3.0,
            "found": True,
        }
        desk = CommandDesk(bitget, PositionDesk())
        result = desk.handle("/balance BGB", source="telegram")
        self.assertTrue(result.ok)
        self.assertEqual(result.cmd, "balance")
        self.assertIn("BGB", result.plain)
        self.assertIn("3.00000000", result.plain)
        self.assertNotIn("USDT", result.plain)
        bitget.fetch_asset_balance.assert_called_with("BGB")
        desk.handle("/balance usdt", source="telegram")
        bitget.fetch_asset_balance.assert_called_with("USDT")

    def test_balance_zero_or_missing_is_explicit(self) -> None:
        bitget = MagicMock()
        bitget.fetch_asset_balance.return_value = {
            "ok": True,
            "coin": "NVDA",
            "free": 0.0,
            "total": 0.0,
            "found": False,
        }
        desk = CommandDesk(bitget, PositionDesk())
        result = desk.handle("/balance NVDA", source="telegram")
        self.assertTrue(result.ok)
        self.assertEqual(result.plain, "0.00 NVDA found in wallet.")

    def test_balance_pair_uses_base_asset(self) -> None:
        bitget = MagicMock()
        bitget.fetch_asset_balance.return_value = {
            "ok": True,
            "coin": "NVDA",
            "free": 1.5,
            "total": 1.5,
            "found": True,
        }
        desk = CommandDesk(bitget, PositionDesk())
        desk.handle("/balance NVDA/USDT", source="telegram")
        bitget.fetch_asset_balance.assert_called_once_with("NVDA")

    def test_fetch_asset_balance_any_coin_case_insensitive(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn._ccxt = lambda fn, label="": {  # type: ignore[method-assign]
            "free": {"bgb": 3.0, "rNVDA": 0.01, "USDT": None},
            "total": {"BGB": 3.0, "rNVDA": 0.01, "USDT": None},
        }
        bgb = conn.fetch_asset_balance("bgb")
        self.assertTrue(bgb["found"])
        self.assertEqual(bgb["free"], 3.0)  # identical spot+swap snapshot is one wallet
        nvda = conn.fetch_asset_balance("rnvda")
        self.assertTrue(nvda["found"])
        missing = conn.fetch_asset_balance("DOGE")
        self.assertFalse(missing["found"])
        self.assertEqual(missing["free"], 0.0)

    def test_fetch_asset_balance_sums_distinct_wallets(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        books = {
            "spot": {
                "free": {"BGB": 1.0},
                "used": {"BGB": 0.25},
                "total": {"BGB": 1.25},
            },
            "swap": {
                "free": {"BGB": 2.0},
                "used": {"BGB": 0.0},
                "total": {"BGB": 2.0},
            },
        }

        def _ccxt(fn, label=""):
            del label
            return fn()

        conn._ccxt = _ccxt  # type: ignore[method-assign]
        conn.exchange = MagicMock()
        conn.exchange.fetch_balance.side_effect = lambda params: books[params["type"]]
        bgb = conn.fetch_asset_balance("BGB")
        self.assertTrue(bgb["ok"])
        self.assertEqual(bgb["free"], 3.0)
        self.assertEqual(bgb["used"], 0.25)
        self.assertEqual(bgb["total"], 3.25)
        self.assertIn("spot", bgb["source"])
        self.assertIn("swap", bgb["source"])


class BgbFeeDeductTests(unittest.TestCase):
    def test_uta_switch_deduct_on(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.exchange = MagicMock()
        conn.exchange.private_uta_post_v3_account_switch_deduct = MagicMock(
            return_value={"code": "00000", "data": True}
        )
        out = conn._enable_bgb_fee_deduct()
        self.assertTrue(out["ok"])
        self.assertEqual(out["deduct"], "on")
        self.assertEqual(out["via"], "uta_v3")
        conn.exchange.private_uta_post_v3_account_switch_deduct.assert_called_once_with(
            {"deduct": "on"}
        )
        conn.exchange.request.assert_not_called()

    def test_spot_v2_fallback_when_uta_missing(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.exchange = MagicMock()
        conn.exchange.private_uta_post_v3_account_switch_deduct = None
        conn.exchange.request = MagicMock(return_value={"code": "00000", "data": True})
        out = conn._enable_bgb_fee_deduct()
        self.assertTrue(out["ok"])
        self.assertEqual(out["via"], "spot_v2")
        conn.exchange.request.assert_called_once_with(
            "v2/spot/account/switch-deduct",
            ["private", "spot"],
            "POST",
            {"deduct": "on"},
        )

    def test_demo_skips_uta_v3_that_404s(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.sandbox = True
        conn.exchange = MagicMock()
        conn.exchange.private_uta_post_v3_account_switch_deduct = MagicMock(
            side_effect=RuntimeError('{"code":"40404","msg":"Request URL NOT FOUND"}')
        )
        conn.exchange.request = MagicMock(return_value={"code": "00000", "data": True})
        out = conn._enable_bgb_fee_deduct()
        self.assertTrue(out["ok"])
        self.assertEqual(out["deduct"], "on")
        self.assertEqual(out["via"], "spot_v2")
        conn.exchange.private_uta_post_v3_account_switch_deduct.assert_not_called()
        self.assertNotIn("40404", str(out.get("error") or ""))

    def test_uta_40404_falls_through_without_raw_body(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.sandbox = False
        conn.exchange = MagicMock()
        conn.exchange.private_uta_post_v3_account_switch_deduct = MagicMock(
            side_effect=RuntimeError('bitget {"code":"40404","msg":"Request URL NOT FOUND"}')
        )
        conn.exchange.request = MagicMock(return_value={"code": "00000", "data": True})
        out = conn._enable_bgb_fee_deduct()
        self.assertTrue(out["ok"])
        self.assertEqual(out["via"], "spot_v2")
        self.assertNotIn("40404", str(out.get("error") or ""))

    def test_fail_open_when_both_endpoints_reject(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.exchange = MagicMock()
        conn.exchange.private_uta_post_v3_account_switch_deduct = MagicMock(
            side_effect=RuntimeError("uta unavailable")
        )
        conn.exchange.request = MagicMock(side_effect=RuntimeError("spot unavailable"))
        out = conn._enable_bgb_fee_deduct()
        self.assertFalse(out["ok"])
        self.assertEqual(out["deduct"], "off")
        self.assertIsNone(out["via"])


if __name__ == "__main__":
    unittest.main()
