"""Phase 5: margin SL, scaled TP, candle confluence, anti-stack isolation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from agents.analyst import AnalystAgent
from agents.risk_manager import RiskManagerAgent
from connectors.bitget_paper import (
    SWAP_LEVERAGE,
    _book_payload,
    protective_prices,
)
from core.positions import (
    PARTIAL_ARM_PCT,
    PositionDesk,
    _exit_reason,
    is_occupied,
    occupied_symbols,
    strip_occupied_universe,
)
from core.schemas import AnalystBrief
from core.ta import (
    SL_MARGIN_FRAC_MAX,
    SL_MARGIN_FRAC_MIN,
    VETO_REASON_CANDLE,
    VETO_REASON_RSI_OVERBOUGHT,
    analyze_candles,
    candle_veto,
    confluence_veto,
    ratchet_trail_sl,
    score_asset_risk,
    sl_margin_frac,
    sl_tp_from_margin,
    structure_break_against,
)


class DummyCortex:
    model_name = "test-qwen"

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {
            "verdict": "CLEAR",
            "fake_news_risk": "LOW",
            "black_swan_flags": [],
            "max_notional_usdt": 15.0,
            "size_multiplier": 1.0,
            "rationale": "tape is clean",
            "asset_risk_score": 20,
            "thesis": "x",
            "monday_gap_bias": "GAP_UP",
            "primary_symbol": "TSLA/USDT:USDT",
            "side": "buy",
            "conviction": 80,
            "horizon": "intraday",
            "affected_tickers": [],
            "news_good": "ok",
            "news_bad": "",
            "stay_away": [],
            "selection_reason": "tsla",
        }

    def generate_json(self, *args, **kwargs):
        return dict(self.payload), False


def _ohlcv_down_break() -> list[list[float]]:
    rows: list[list[float]] = []
    for i in range(16):
        px = 100.0 + (i % 3) * 0.2
        rows.append([i, px, px + 0.4, px - 0.4, px, 1.0])
    rows.append([16, 99.0, 99.2, 88.0, 88.5, 1.0])
    return rows


def _ohlcv_up_break() -> list[list[float]]:
    rows: list[list[float]] = []
    for i in range(16):
        px = 100.0 - (i % 3) * 0.2
        rows.append([i, px, px + 0.4, px - 0.4, px, 1.0])
    rows.append([16, 101.0, 112.0, 100.8, 111.5, 1.0])
    return rows


class MarginStopTests(unittest.TestCase):
    def test_frac_maps_risk_to_50_100(self) -> None:
        self.assertEqual(sl_margin_frac(0), SL_MARGIN_FRAC_MAX)
        self.assertEqual(sl_margin_frac(100), SL_MARGIN_FRAC_MIN)
        self.assertAlmostEqual(sl_margin_frac(50), 0.75)

    def test_ten_dollar_high_risk_long(self) -> None:
        sl, tp = sl_tp_from_margin(100.0, "buy", 10.0, 0.50, leverage=5.0)
        self.assertAlmostEqual(sl, 90.0)
        self.assertGreater(tp, 100.0)

    def test_ten_dollar_low_risk_long(self) -> None:
        sl, _tp = sl_tp_from_margin(100.0, "buy", 10.0, 1.00, leverage=5.0)
        self.assertAlmostEqual(sl, 80.0)

    def test_short_inverts(self) -> None:
        sl, tp = sl_tp_from_margin(100.0, "sell", 10.0, 0.50, leverage=5.0)
        self.assertAlmostEqual(sl, 110.0)
        self.assertLess(tp, 100.0)

    def test_legacy_protective_prices_untouched(self) -> None:
        sl, tp = protective_prices(100.0)
        self.assertAlmostEqual(sl, 98.0)
        self.assertAlmostEqual(tp, 105.0)

    def test_margin_kwargs_override_legacy(self) -> None:
        sl, _tp = protective_prices(
            100.0,
            "buy",
            margin_usdt=10.0,
            sl_margin_frac=0.50,
            leverage=SWAP_LEVERAGE,
        )
        self.assertAlmostEqual(sl, 90.0)


class CandleStructureTests(unittest.TestCase):
    def test_breakdown_vetoes_buy(self) -> None:
        ta = analyze_candles(_ohlcv_down_break())
        self.assertTrue(ta["structure_break"])
        self.assertEqual(ta["break_dir"], "down")
        self.assertEqual(candle_veto("buy", ta), VETO_REASON_CANDLE)
        self.assertEqual(candle_veto("sell", ta), "")
        self.assertTrue(structure_break_against("buy", ta))

    def test_breakout_vetoes_sell(self) -> None:
        ta = analyze_candles(_ohlcv_up_break())
        self.assertEqual(ta["break_dir"], "up")
        self.assertEqual(candle_veto("sell", ta), VETO_REASON_CANDLE)
        self.assertEqual(candle_veto("buy", ta), "")

    def test_rsi_confluence_still_hard(self) -> None:
        self.assertEqual(confluence_veto("buy", 70.0), VETO_REASON_RSI_OVERBOUGHT)

    def test_engulfing_pattern(self) -> None:
        rows = [
            [0, 10, 10.2, 9.8, 10.1, 1],
            [1, 10.1, 10.2, 9.9, 9.95, 1],
            [2, 10, 10.1, 9.9, 9.92, 1],
            [3, 10, 10.05, 9.9, 9.93, 1],
            [4, 9.85, 10.5, 9.8, 10.4, 1],
        ]
        ta = analyze_candles(rows)
        self.assertEqual(ta["pattern"], "bullish_engulfing")


class RiskScoreTests(unittest.TestCase):
    def test_high_vol_and_toxic_news_tighten_stop(self) -> None:
        hot = score_asset_risk(
            side="buy",
            atr_pct=0.06,
            fake_news_risk="HIGH",
            structure_break=True,
            candle_bias="bearish",
            market_cap_usdt=1.0e6,
        )
        calm = score_asset_risk(
            side="buy",
            atr_pct=0.008,
            fake_news_risk="LOW",
            candle_bias="bullish",
            market_cap_usdt=5.0e11,
        )
        self.assertGreater(hot, calm)
        self.assertLess(sl_margin_frac(hot), sl_margin_frac(calm))


class AntiStackTests(unittest.TestCase):
    def test_strips_occupied_aliases(self) -> None:
        book = [{"open": True, "symbol": "AAPL/USDT:USDT", "side": "buy"}]
        uni = ["AAPL/USDT:USDT", "rAAPL/USDT", "TSLA/USDT:USDT", "NVDA/USDT:USDT"]
        self.assertEqual(occupied_symbols(book), ["AAPL/USDT:USDT"])
        self.assertTrue(is_occupied("rAAPL/USDT", book))
        stripped = strip_occupied_universe(uni, book)
        self.assertNotIn("AAPL/USDT:USDT", stripped)
        self.assertNotIn("rAAPL/USDT", stripped)
        self.assertIn("TSLA/USDT:USDT", stripped)

    def test_oracle_universe_excludes_occupied(self) -> None:
        agent = AnalystAgent(DummyCortex())  # type: ignore[arg-type]
        from core.schemas import WeekendTrigger

        brief = agent.brief(
            [
                WeekendTrigger(
                    id="RSS-01",
                    category="tech-shift",
                    headline="Tesla Cybertruck production ramps",
                    detail="EV tape",
                )
            ],
            ["AAPL/USDT:USDT", "TSLA/USDT:USDT"],
            occupied=["AAPL/USDT:USDT"],
        )
        self.assertNotEqual(brief.primary_symbol, "AAPL/USDT:USDT")


class SentinelCandleTests(unittest.TestCase):
    def test_structure_break_overrides_clear_llm(self) -> None:
        agent = RiskManagerAgent(DummyCortex())  # type: ignore[arg-type]
        ta = analyze_candles(_ohlcv_down_break())
        ta["rsi"] = 48.0
        ta["timeframe"] = "15m"
        ta["period"] = 14
        book = _book_payload(
            "AAPL/USDT:USDT",
            [[99.9, 2.0]],
            [[100.1, 2.0]],
            ok=True,
            source="l2",
            error=None,
        )
        risk = agent.evaluate(
            AnalystBrief(
                thesis="bullish",
                rationale="beat",
                primary_symbol="AAPL/USDT:USDT",
                side="buy",
                conviction=80,
            ),
            {"ok": True, "last": 100.0, "bid": 99.9, "ask": 100.1, "symbol": "AAPL/USDT:USDT"},
            15.0,
            tradable_symbol="AAPL/USDT:USDT",
            order_book=book,
            ta=ta,
            fundamentals={"ok": True, "price": 100.0, "is_swap": True, "market_cap_usdt": 2e12},
            equity_usdt=10_000.0,
            daily={"entries": 0, "wins": 0, "sl_hits": 0, "deployed_usdt": 0.0, "halt": ""},
        )
        self.assertEqual(risk.verdict, "VETO")
        self.assertIn(VETO_REASON_CANDLE, risk.black_swan_flags)

    def test_clear_sets_margin_stop(self) -> None:
        agent = RiskManagerAgent(DummyCortex())  # type: ignore[arg-type]
        book = _book_payload(
            "AAPL/USDT:USDT",
            [[99.9, 2.0]],
            [[100.1, 2.0]],
            ok=True,
            source="l2",
            error=None,
        )
        risk = agent.evaluate(
            AnalystBrief(
                thesis="bullish",
                rationale="beat",
                primary_symbol="AAPL/USDT:USDT",
                side="buy",
                conviction=80,
            ),
            {"ok": True, "last": 100.0, "bid": 99.9, "ask": 100.1, "symbol": "AAPL/USDT:USDT"},
            15.0,
            tradable_symbol="AAPL/USDT:USDT",
            order_book=book,
            ta={"ok": True, "rsi": 48.0, "timeframe": "15m", "period": 14, "structure": "RANGE"},
            fundamentals={"ok": True, "price": 100.0, "is_swap": True, "volume_24h_usdt": 1e9},
            equity_usdt=10_000.0,
            daily={"entries": 0, "wins": 0, "sl_hits": 0, "deployed_usdt": 0.0, "halt": ""},
        )
        self.assertEqual(risk.verdict, "CLEAR")
        self.assertIsNotNone(risk.sl_price)
        self.assertLess(float(risk.sl_price or 0), 100.0)
        self.assertGreaterEqual(risk.sl_margin_frac, SL_MARGIN_FRAC_MIN)
        self.assertLessEqual(risk.sl_margin_frac, SL_MARGIN_FRAC_MAX)
        self.assertIsNotNone(risk.margin_usdt)


class DeskTrailTests(unittest.TestCase):
    def test_partial_waits_for_twenty_five_pct(self) -> None:
        pos = {"symbol": "NVDA/USDT:USDT", "side": "buy", "pnl_pct": 4.2}
        self.assertEqual(_exit_reason(pos, None, {}, 4.2), "")
        self.assertTrue(
            _exit_reason(
                {"symbol": "NVDA/USDT:USDT", "side": "buy", "pnl_pct": 25.0},
                None,
                {},
                25.0,
            ).startswith("PARTIAL")
        )
        self.assertEqual(PARTIAL_ARM_PCT, 25.0)

    def test_structure_break_exits_open_long(self) -> None:
        pos = {
            "symbol": "AAPL/USDT:USDT",
            "side": "buy",
            "pnl_pct": 0.4,
            "mark_price": 88.5,
            "entry_price": 100.0,
        }
        reason = _exit_reason(pos, None, {}, 0.4, candles=analyze_candles(_ohlcv_down_break()))
        self.assertTrue(reason.startswith("STRUCTURE"))

    def test_ratchet_only_moves_up_for_longs(self) -> None:
        first = ratchet_trail_sl("buy", 100.0, 110.0, 100.0)
        self.assertGreaterEqual(first, 100.0)
        second = ratchet_trail_sl("buy", 100.0, 108.0, first)
        self.assertLessEqual(second, first)
        self.assertGreaterEqual(second, 100.0)

    def test_manage_scans_candles_and_closes_structure(self) -> None:
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
                    "mark_price": 88.5,
                    "pnl_pct": 0.4,
                    "pnl_usdt": 0.4,
                }
            ]
            bitget.fetch_ta_bundle.return_value = analyze_candles(_ohlcv_down_break())
            bitget.close_market.return_value = {
                "ok": True,
                "status": "CLOSED",
                "symbol": "AAPL/USDT:USDT",
                "pnl_pct": -11.5,
                "pnl_usdt": -11.5,
            }
            actions = desk.manage(bitget, None)
            self.assertEqual(len(actions), 1)
            self.assertTrue(str(actions[0].get("reason") or "").startswith("STRUCTURE"))
            bitget.close_market.assert_called_once()


if __name__ == "__main__":
    unittest.main()
