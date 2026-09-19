"""Phase 4: RSI confluence filter — math, SENTINEL veto, CHAIRMAN fail-safe."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from agents.executive import ExecutiveAgent
from agents.risk_manager import RiskManagerAgent
from connectors.bitget_paper import BitgetPaperConnector, _book_payload
from core.memory import BoardMemory
from core.schemas import AnalystBrief, RiskReport
from core.ta import (
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_PERIOD,
    VETO_REASON_RSI_OVERBOUGHT,
    VETO_REASON_RSI_OVERSOLD,
    compute_rsi,
    confluence_veto,
    rsi_zone,
    ta_verdict,
)


# StockCharts RSI(14) first value after 15 closes is ~70.46
_STOCKCHARTS_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
    46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
]


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
            "action": "EXECUTE",
            "consensus": "UNANIMOUS",
            "reasoning": "news and risk agree",
        }

    def generate_json(self, *args, **kwargs):
        return dict(self.payload), False


def _brief(side: str = "buy") -> AnalystBrief:
    return AnalystBrief(
        thesis="bullish iPhone beat" if side == "buy" else "bearish miss",
        rationale="live wire",
        primary_symbol="AAPL/USDT:USDT",
        side=side,  # type: ignore[arg-type]
        conviction=80,
    )


def _liquid_book() -> dict:
    return _book_payload(
        "AAPL/USDT:USDT",
        [[99.9, 2.0]],
        [[100.1, 2.0]],
        ok=True,
        source="l2",
        error=None,
    )


def _ticker() -> dict:
    return {"ok": True, "last": 100.0, "bid": 99.9, "ask": 100.1, "symbol": "AAPL/USDT:USDT"}


class RsiMathTests(unittest.TestCase):
    def test_wilder_rsi_matches_stockcharts_first_value(self) -> None:
        rsi = compute_rsi(_STOCKCHARTS_CLOSES, 14)
        self.assertIsNotNone(rsi)
        self.assertAlmostEqual(rsi or 0.0, 70.46, places=1)

    def test_monotonic_up_is_high(self) -> None:
        closes = [100.0 + i for i in range(20)]
        rsi = compute_rsi(closes)
        self.assertIsNotNone(rsi)
        assert rsi is not None
        self.assertGreaterEqual(rsi, 90.0)

    def test_monotonic_down_is_low(self) -> None:
        closes = [100.0 - i for i in range(20)]
        rsi = compute_rsi(closes)
        self.assertIsNotNone(rsi)
        assert rsi is not None
        self.assertLessEqual(rsi, 10.0)

    def test_insufficient_bars_is_none(self) -> None:
        self.assertIsNone(compute_rsi([1, 2, 3], 14))
        self.assertIsNone(compute_rsi([], 14))
        self.assertIsNone(compute_rsi(None))  # type: ignore[arg-type]

    def test_flat_tape_is_fifty(self) -> None:
        rsi = compute_rsi([50.0] * 20)
        self.assertEqual(rsi, 50.0)

    def test_period_is_fourteen(self) -> None:
        self.assertEqual(RSI_PERIOD, 14)
        self.assertEqual(RSI_OVERBOUGHT, 70.0)
        self.assertEqual(RSI_OVERSOLD, 30.0)


class ConfluenceRuleTests(unittest.TestCase):
    def test_buy_overbought_vetoes(self) -> None:
        self.assertEqual(confluence_veto("buy", 70.0), VETO_REASON_RSI_OVERBOUGHT)
        self.assertEqual(confluence_veto("BUY", 81.2), VETO_REASON_RSI_OVERBOUGHT)
        self.assertEqual(ta_verdict("buy", 70.0), "VETO")
        self.assertEqual(rsi_zone(72.0), "OVERBOUGHT")

    def test_buy_not_overbought_passes(self) -> None:
        self.assertEqual(confluence_veto("buy", 69.99), "")
        self.assertEqual(confluence_veto("buy", 50.0), "")
        self.assertEqual(ta_verdict("buy", 50.0), "PASS")

    def test_sell_oversold_vetoes(self) -> None:
        self.assertEqual(confluence_veto("sell", 30.0), VETO_REASON_RSI_OVERSOLD)
        self.assertEqual(confluence_veto("SELL", 12.0), VETO_REASON_RSI_OVERSOLD)
        self.assertEqual(ta_verdict("sell", 30.0), "VETO")
        self.assertEqual(rsi_zone(18.0), "OVERSOLD")

    def test_sell_not_oversold_passes(self) -> None:
        self.assertEqual(confluence_veto("sell", 30.01), "")
        self.assertEqual(confluence_veto("sell", 55.0), "")
        self.assertEqual(ta_verdict("sell", 55.0), "PASS")

    def test_unmeasured_rsi_does_not_veto(self) -> None:
        self.assertEqual(confluence_veto("buy", None), "")
        self.assertEqual(confluence_veto("sell", None), "")
        self.assertEqual(ta_verdict("buy", None), "SKIPPED")
        self.assertEqual(rsi_zone(None), "unmeasured")

    def test_idle_side_does_not_veto(self) -> None:
        self.assertEqual(confluence_veto("none", 90.0), "")
        self.assertEqual(confluence_veto("", 5.0), "")


class SentinelTaTests(unittest.TestCase):
    def _eval(self, side: str, rsi: float | None) -> RiskReport:
        agent = RiskManagerAgent(DummyCortex())  # type: ignore[arg-type]
        return agent.evaluate(
            _brief(side),
            _ticker(),
            15.0,
            tradable_symbol="AAPL/USDT:USDT",
            order_book=_liquid_book(),
            ta={"ok": rsi is not None, "rsi": rsi, "timeframe": "15m", "period": 14, "symbol": "AAPL/USDT:USDT"},
            equity_usdt=10_000.0,
            daily={"date": "2026-09-19", "entries": 0, "wins": 0, "sl_hits": 0, "deployed_usdt": 0.0, "halt": ""},
        )

    def test_clear_llm_still_vetoes_overbought_buy(self) -> None:
        risk = self._eval("buy", 74.2)
        self.assertEqual(risk.verdict, "VETO")
        self.assertEqual(risk.size_multiplier, 0.0)
        self.assertEqual(risk.ta_verdict, "VETO")
        self.assertAlmostEqual(risk.rsi or 0.0, 74.2)
        self.assertIn(VETO_REASON_RSI_OVERBOUGHT, risk.black_swan_flags)
        self.assertTrue(risk.rationale.startswith(VETO_REASON_RSI_OVERBOUGHT))

    def test_clear_llm_still_vetoes_oversold_sell(self) -> None:
        risk = self._eval("sell", 22.0)
        self.assertEqual(risk.verdict, "VETO")
        self.assertEqual(risk.ta_verdict, "VETO")
        self.assertIn(VETO_REASON_RSI_OVERSOLD, risk.black_swan_flags)
        self.assertTrue(risk.rationale.startswith(VETO_REASON_RSI_OVERSOLD))

    def test_neutral_rsi_does_not_override_clear(self) -> None:
        risk = self._eval("buy", 48.0)
        self.assertEqual(risk.verdict, "CLEAR")
        self.assertEqual(risk.ta_verdict, "PASS")
        self.assertGreater(risk.size_multiplier, 0.0)
        self.assertNotIn(VETO_REASON_RSI_OVERBOUGHT, risk.black_swan_flags)

    def test_missing_rsi_skips_confluence(self) -> None:
        risk = self._eval("buy", None)
        self.assertEqual(risk.verdict, "CLEAR")
        self.assertEqual(risk.ta_verdict, "SKIPPED")
        self.assertIsNone(risk.rsi)


class ChairmanTaTests(unittest.TestCase):
    def _chair(self, side: str, rsi: float | None, verdict: str = "CLEAR") -> tuple:
        agent = ExecutiveAgent(DummyCortex())  # type: ignore[arg-type]
        risk = RiskReport(
            verdict=verdict,  # type: ignore[arg-type]
            rationale="llm clear",
            max_notional_usdt=15.0,
            size_multiplier=1.0,
            rsi=rsi,
            rsi_timeframe="15m",
            rsi_period=14,
            ta_verdict=ta_verdict(side, rsi),  # type: ignore[arg-type]
        )
        decision = agent.synthesize(_brief(side), risk, "AAPL/USDT:USDT", 100.0)
        return decision, risk

    def test_chairman_cannot_buy_overbought(self) -> None:
        decision, _ = self._chair("buy", 71.0)
        self.assertEqual(decision.action, "STAND_DOWN")
        self.assertEqual(decision.consensus, "VETOED")
        self.assertEqual(decision.amount, 0.0)
        self.assertIn(VETO_REASON_RSI_OVERBOUGHT, decision.reasoning)

    def test_chairman_cannot_sell_oversold(self) -> None:
        decision, _ = self._chair("sell", 29.0)
        self.assertEqual(decision.action, "STAND_DOWN")
        self.assertEqual(decision.consensus, "VETOED")
        self.assertIn(VETO_REASON_RSI_OVERSOLD, decision.reasoning)

    def test_chairman_executes_when_rsi_agrees(self) -> None:
        decision, _ = self._chair("buy", 55.0)
        self.assertEqual(decision.action, "EXECUTE")
        self.assertGreater(decision.amount, 0.0)
        self.assertNotIn("RSI Overbought", decision.reasoning)

    def test_sentinel_veto_still_wins_on_neutral_rsi(self) -> None:
        decision, _ = self._chair("buy", 45.0, verdict="VETO")
        self.assertEqual(decision.action, "STAND_DOWN")
        self.assertEqual(decision.consensus, "VETOED")


class FetchRsiTests(unittest.TestCase):
    def test_15m_thin_tape_falls_back_to_1h(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)

        def fake_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 64):
            if timeframe == "15m":
                return [[0, 1, 1, 1, 10.0, 1]]
            return [[i, 100 + i, 101 + i, 99 + i, 100.0 + i, 1] for i in range(30)]

        conn.fetch_ohlcv = fake_ohlcv  # type: ignore[method-assign]
        payload = conn.fetch_rsi("AAPL/USDT:USDT")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["timeframe"], "1h")
        self.assertIsNotNone(payload["rsi"])
        self.assertGreaterEqual(payload["bars"], 15)

    def test_no_candles_returns_unmeasured(self) -> None:
        conn = BitgetPaperConnector.__new__(BitgetPaperConnector)
        conn.fetch_ohlcv = lambda *a, **k: []  # type: ignore[method-assign]
        payload = conn.fetch_rsi("AAPL/USDT:USDT")
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["rsi"])
        self.assertIn("insufficient", str(payload.get("error") or "").lower())


class MemoryTaTests(unittest.TestCase):
    def test_board_memory_records_rsi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json", max_trades=5)
            mem.record(
                news_context=["Apple beat"],
                decision={
                    "action": "STAND_DOWN",
                    "side": "buy",
                    "symbol": "AAPL/USDT:USDT",
                    "consensus": "VETOED",
                    "verdict": "VETO",
                    "rsi": 73.5,
                    "ta_verdict": "VETO",
                    "thesis": "bullish but overbought",
                    "model": "qwen3.8-max",
                },
                result={"ok": False, "status": "VETOED", "error": VETO_REASON_RSI_OVERBOUGHT},
            )
            block = mem.prompt_block()
            self.assertIn("rsi=73.50", block)
            self.assertIn("ta=VETO", block)
            self.assertIn(VETO_REASON_RSI_OVERBOUGHT, block)


class ProtectiveOrdersUntouchedTests(unittest.TestCase):
    def test_fetch_rsi_does_not_exist_on_close_path(self) -> None:
        """Sanity: close_market / SL-TP helpers are still on the connector."""
        self.assertTrue(hasattr(BitgetPaperConnector, "close_market"))
        self.assertTrue(hasattr(BitgetPaperConnector, "close_all"))
        self.assertTrue(hasattr(BitgetPaperConnector, "_place_sl_tp"))
        self.assertTrue(hasattr(BitgetPaperConnector, "fetch_atr"))


if __name__ == "__main__":
    unittest.main()
