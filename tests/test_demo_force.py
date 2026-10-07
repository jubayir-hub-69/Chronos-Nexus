"""Demo force: USDT-M entries clear SENTINEL vetoes and keep SL/TP math."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from agents.executive import ExecutiveAgent
from agents.risk_manager import RiskManagerAgent
from connectors.bitget_paper import SWAP_LEVERAGE, _book_payload, protective_prices
from core.schemas import AnalystBrief, RiskReport
from core.ta import SCALE_OUT_PCT, VETO_REASON_WALL
import core.ta as ta


class DummyCortex:
    model_name = "test-qwen"

    def generate_json(self, *args, **kwargs):
        return {
            "verdict": "CLEAR",
            "fake_news_risk": "LOW",
            "black_swan_flags": [],
            "max_notional_usdt": 15.0,
            "size_multiplier": 1.0,
            "rationale": "model would have vetoed the wall",
            "asset_risk_score": 40,
            "action": "EXECUTE",
            "consensus": "UNANIMOUS",
            "reasoning": "locked by the chair",
        }, False


def _brief(side: str = "buy", symbol: str = "BZ/USDT:USDT", conviction: int = 45) -> AnalystBrief:
    return AnalystBrief(
        thesis="Oil supply shock",
        rationale="Houthi and Hormuz headlines",
        primary_symbol=symbol,
        side=side,  # type: ignore[arg-type]
        conviction=conviction,
    )


def _book(symbol: str = "BZ/USDT:USDT") -> dict:
    return _book_payload(
        symbol,
        [[99.9, 2.0]],
        [[100.1, 2.0]],
        ok=True,
        source="l2",
        error=None,
    )


def _ticker(symbol: str = "BZ/USDT:USDT") -> dict:
    return {
        "ok": True,
        "last": 100.0,
        "bid": 99.9,
        "ask": 100.1,
        "mark": 100.0,
        "symbol": symbol,
    }


_WALL = (
    VETO_REASON_WALL,
    {
        "score": 27.26,
        "align": "CONFLICT",
        "pullback_ok": False,
        "measurable": True,
        "threshold": 40.0,
        "book": {"imbalance": -0.8},
    },
)


class DemoForceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._force = ta.DEMO_FORCE_EXECUTE
        ta.DEMO_FORCE_EXECUTE = True

    def tearDown(self) -> None:
        ta.DEMO_FORCE_EXECUTE = self._force

    def _eval(self, **kwargs):
        agent = RiskManagerAgent(DummyCortex())  # type: ignore[arg-type]
        symbol = kwargs.pop("symbol", "BZ/USDT:USDT")
        side = kwargs.pop("side", "buy")
        defaults = dict(
            brief=_brief(side, symbol),
            ticker=_ticker(symbol),
            paper_cap_usdt=15.0,
            tradable_symbol=symbol,
            order_book=_book(symbol),
            ta={
                "ok": True,
                "rsi": 48.82,
                "timeframe": "15m",
                "period": 14,
                "structure": "DOWNTREND",
                "volatility": "MEDIUM",
                "symbol": symbol,
            },
            fundamentals={
                "ok": True,
                "price": 100.0,
                "is_swap": True,
                "volume_24h_usdt": 5_000_000.0,
                "symbol": symbol,
            },
            equity_usdt=10_000.0,
            daily={"entries": 0, "wins": 0, "sl_hits": 0, "deployed_usdt": 0.0, "halt": ""},
        )
        defaults.update(kwargs)
        return agent.evaluate(**defaults)

    def test_opposing_wall_clears_and_sizes_usdt_m(self) -> None:
        with patch("agents.risk_manager.setup_veto", return_value=_WALL):
            risk = self._eval()
        self.assertEqual(risk.verdict, "CLEAR")
        self.assertGreater(risk.size_multiplier, 0.0)
        self.assertIn(VETO_REASON_WALL, risk.black_swan_flags)
        self.assertTrue(risk.rationale.startswith("DEMO FORCE:"))
        self.assertEqual(risk.daily_halt, "")
        self.assertAlmostEqual(risk.max_notional_usdt * risk.size_multiplier, 15.0, places=4)
        self.assertIsNotNone(risk.last_price)
        px = float(risk.last_price or 0.0)
        self.assertAlmostEqual(px, 100.1, places=4)
        notional = float(risk.max_notional_usdt) * float(risk.size_multiplier)
        self.assertAlmostEqual(float(risk.margin_usdt or 0.0), notional / SWAP_LEVERAGE, places=4)
        qty = notional / px
        sl, tp = protective_prices(
            px,
            "buy",
            margin_usdt=risk.margin_usdt,
            qty=qty,
            sl_margin_frac=risk.sl_margin_frac,
            leverage=SWAP_LEVERAGE,
            take_profit_pct=SCALE_OUT_PCT / SWAP_LEVERAGE,
        )
        self.assertAlmostEqual(float(risk.sl_price or 0.0), sl, places=6)
        self.assertAlmostEqual(float(risk.tp_price or 0.0), tp, places=6)
        self.assertLess(float(risk.sl_price or 0.0), px)
        self.assertAlmostEqual(tp, px * (1.0 + SCALE_OUT_PCT / SWAP_LEVERAGE), places=6)
        self.assertGreater(float(risk.sl_margin_frac), 0.0)

    def test_wall_still_vetoes_when_force_is_off(self) -> None:
        ta.DEMO_FORCE_EXECUTE = False
        with patch("agents.risk_manager.setup_veto", return_value=_WALL):
            risk = self._eval()
        self.assertEqual(risk.verdict, "VETO")
        self.assertEqual(risk.size_multiplier, 0.0)
        self.assertIn(VETO_REASON_WALL, risk.black_swan_flags)
        self.assertIsNone(risk.sl_price)

    def test_spot_symbol_stays_vetoed(self) -> None:
        risk = self._eval(
            symbol="BTC/USDT",
            brief=_brief("buy", "BTC/USDT"),
            ticker=_ticker("BTC/USDT"),
            order_book=_book("BTC/USDT"),
            ta={"ok": True, "rsi": 88.0, "timeframe": "15m", "period": 14, "symbol": "BTC/USDT"},
            fundamentals={"ok": True, "price": 100.0, "is_swap": False, "symbol": "BTC/USDT"},
        )
        self.assertEqual(risk.verdict, "VETO")
        self.assertEqual(risk.size_multiplier, 0.0)
        self.assertIsNone(risk.sl_price)

    def test_sell_stop_is_above_and_target_is_below(self) -> None:
        risk = self._eval(
            side="sell",
            brief=_brief("sell"),
            ta={
                "ok": True,
                "rsi": 22.0,
                "timeframe": "15m",
                "period": 14,
                "structure": "UPTREND",
                "symbol": "BZ/USDT:USDT",
            },
        )
        self.assertEqual(risk.verdict, "CLEAR")
        px = float(risk.last_price or 0.0)
        self.assertAlmostEqual(px, 99.9, places=4)
        self.assertGreater(float(risk.sl_price or 0.0), px)
        self.assertAlmostEqual(
            float(risk.tp_price or 0.0),
            px * (1.0 - SCALE_OUT_PCT / SWAP_LEVERAGE),
            places=6,
        )
        notional = float(risk.max_notional_usdt) * float(risk.size_multiplier)
        self.assertGreater(notional / px, 0.0)

    def test_chairman_executes_a_vetoed_perp(self) -> None:
        agent = ExecutiveAgent(DummyCortex())  # type: ignore[arg-type]
        risk = RiskReport(
            verdict="VETO",
            rationale="VETO: Opposing order-book wall",
            max_notional_usdt=15.0,
            size_multiplier=0.0,
            rsi=48.82,
            sl_price=97.5,
            tp_price=105.15,
            margin_usdt=3.0,
        )
        decision = agent.synthesize(_brief(), risk, "BZ/USDT:USDT", 100.1)
        self.assertEqual(decision.action, "EXECUTE")
        self.assertNotEqual(decision.consensus, "VETOED")
        self.assertAlmostEqual(decision.notional_usdt, 15.0, places=4)
        self.assertAlmostEqual(decision.amount, 15.0 / 100.1, places=6)
        self.assertEqual(decision.symbol, "BZ/USDT:USDT")

    def test_chairman_refuses_spot_even_when_clear(self) -> None:
        agent = ExecutiveAgent(DummyCortex())  # type: ignore[arg-type]
        risk = RiskReport(
            verdict="CLEAR",
            rationale="clean",
            max_notional_usdt=15.0,
            size_multiplier=1.0,
            rsi=50.0,
        )
        decision = agent.synthesize(_brief("buy", "SOL/USDT"), risk, "SOL/USDT", 100.0)
        self.assertEqual(decision.action, "STAND_DOWN")
        self.assertEqual(decision.consensus, "VETOED")
        self.assertEqual(decision.amount, 0.0)
        self.assertIn("SPOT trading is disabled", decision.reasoning)

    def test_chairman_does_not_invent_an_idle_trade(self) -> None:
        agent = ExecutiveAgent(DummyCortex())  # type: ignore[arg-type]
        risk = RiskReport(verdict="CLEAR", rationale="idle", max_notional_usdt=15.0, size_multiplier=1.0)
        decision = agent.synthesize(_brief("none", "NONE", conviction=0), risk, "BZ/USDT:USDT", 100.0)
        self.assertEqual(decision.action, "STAND_DOWN")
        self.assertEqual(decision.amount, 0.0)

    def test_daily_halt_does_not_block_the_forced_perp(self) -> None:
        agent = ExecutiveAgent(DummyCortex())  # type: ignore[arg-type]
        risk = RiskReport(
            verdict="CLEAR",
            rationale="win streak",
            max_notional_usdt=15.0,
            size_multiplier=1.0,
            rsi=50.0,
            daily_halt="VETO: Win-streak stand-down",
        )
        decision = agent.synthesize(_brief(), risk, "rNVDA/USDT:USDT", 180.0)
        self.assertEqual(decision.action, "EXECUTE")
        self.assertGreater(decision.amount, 0.0)
        self.assertTrue(decision.symbol.endswith(":USDT"))


if __name__ == "__main__":
    unittest.main()
