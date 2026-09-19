"""Phase 6: daily trade limits, 25% scaled TP, 6% portfolio risk."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents.executive import ExecutiveAgent
from agents.risk_manager import RiskManagerAgent
from connectors.bitget_paper import _book_payload
from core.memory import (
    DAILY_MAX_ENTRIES,
    DAILY_RISK_PCT,
    DAILY_WIN_STREAK,
    VETO_REASON_DAILY_MAX,
    VETO_REASON_DAILY_RISK,
    VETO_REASON_STOP_LOSS_DAY,
    VETO_REASON_WIN_STREAK,
    BoardMemory,
    clip_notional_to_daily_budget,
    daily_block_reason,
)
from core.positions import PARTIAL_ARM_PCT, _exit_reason
from core.schemas import AnalystBrief, RiskReport
from core.ta import SCALE_OUT_PCT, VETO_REASON_CHOP, severe_tape_halt


class DummyCortex:
    model_name = "test-qwen"

    def generate_json(self, *args, **kwargs):
        return {
            "verdict": "CLEAR",
            "fake_news_risk": "LOW",
            "black_swan_flags": [],
            "max_notional_usdt": 15.0,
            "size_multiplier": 1.0,
            "rationale": "tape is clean",
            "asset_risk_score": 20,
            "action": "EXECUTE",
            "consensus": "UNANIMOUS",
            "reasoning": "go",
        }, False


def _brief() -> AnalystBrief:
    return AnalystBrief(
        thesis="bullish",
        rationale="beat",
        primary_symbol="AAPL/USDT:USDT",
        side="buy",
        conviction=80,
    )


def _liquid() -> dict:
    return _book_payload(
        "AAPL/USDT:USDT",
        [[99.9, 2.0]],
        [[100.1, 2.0]],
        ok=True,
        source="l2",
        error=None,
    )


class ScaleOutTests(unittest.TestCase):
    def test_partial_is_twenty_five_percent(self) -> None:
        self.assertEqual(SCALE_OUT_PCT, 0.25)
        self.assertEqual(PARTIAL_ARM_PCT, 25.0)
        self.assertEqual(_exit_reason({"side": "buy", "pnl_pct": 10.0}, None, {}, 10.0), "")
        self.assertTrue(
            _exit_reason({"side": "buy", "pnl_pct": 25.0}, None, {}, 25.0).startswith("PARTIAL")
        )


class DailyLedgerTests(unittest.TestCase):
    def test_win_streak_and_sl_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            mem.note_close(
                {"ok": True, "kind": "TP", "pnl_usdt": 2.0, "symbol": "AAPL/USDT:USDT"}
            )
            mem.note_close(
                {"ok": True, "kind": "TRAIL", "pnl_usdt": 1.5, "symbol": "MSFT/USDT:USDT"}
            )
            self.assertEqual(mem.daily_state()["wins"], 2)
            self.assertEqual(daily_block_reason(mem.daily_state()), "")
            mem.note_close(
                {"ok": True, "kind": "TP", "pnl_usdt": 0.8, "symbol": "NVDA/USDT:USDT"}
            )
            state = mem.daily_state()
            self.assertEqual(state["wins"], DAILY_WIN_STREAK)
            self.assertEqual(state["halt"], "WIN_STREAK")
            self.assertEqual(daily_block_reason(state), VETO_REASON_WIN_STREAK)

    def test_stop_loss_halts_the_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            mem.note_close({"ok": True, "kind": "SL", "pnl_usdt": -1.2, "symbol": "TSLA/USDT:USDT"})
            self.assertEqual(daily_block_reason(mem.daily_state()), VETO_REASON_STOP_LOSS_DAY)

    def test_max_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            for i in range(DAILY_MAX_ENTRIES):
                mem.note_entry(symbol="AAPL/USDT:USDT", margin_usdt=1.0, order_id=str(i))
            self.assertEqual(mem.daily_state()["entries"], DAILY_MAX_ENTRIES)
            self.assertEqual(daily_block_reason(mem.daily_state()), VETO_REASON_DAILY_MAX)
            allowed, reason = mem.can_enter()
            self.assertFalse(allowed)
            self.assertEqual(reason, VETO_REASON_DAILY_MAX)

    def test_partial_does_not_count_as_a_win(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = BoardMemory(path=Path(tmp) / "history.json")
            mem.note_close({"ok": True, "kind": "PARTIAL", "pnl_usdt": 4.0, "symbol": "AAPL/USDT:USDT"})
            self.assertEqual(mem.daily_state()["wins"], 0)
            self.assertEqual(daily_block_reason(mem.daily_state()), "")

    def test_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            mem = BoardMemory(path=path)
            mem.note_entry(symbol="AAPL/USDT:USDT", margin_usdt=2.5)
            again = BoardMemory(path=path)
            self.assertEqual(again.daily_state()["entries"], 1)
            self.assertAlmostEqual(again.daily_state()["deployed_usdt"], 2.5)


class BudgetTests(unittest.TestCase):
    def test_six_percent_split_across_slots(self) -> None:
        self.assertEqual(DAILY_RISK_PCT, 0.06)
        notional, reason = clip_notional_to_daily_budget(
            equity_usdt=100.0,
            daily={"entries": 0, "deployed_usdt": 0.0},
            requested_notional=15.0,
            leverage=5.0,
        )
        self.assertEqual(reason, "")
        # 6 USDT budget / 4 slots = 1.5 margin → 7.5 notional at 5x, below the 15 request
        self.assertAlmostEqual(notional, 7.5)

    def test_exhausted_budget_vetoes(self) -> None:
        notional, reason = clip_notional_to_daily_budget(
            equity_usdt=100.0,
            daily={"entries": 1, "deployed_usdt": 5.9},
            requested_notional=15.0,
            leverage=5.0,
        )
        self.assertEqual(notional, 0.0)
        self.assertEqual(reason, VETO_REASON_DAILY_RISK)

    def test_missing_equity_vetoes(self) -> None:
        notional, reason = clip_notional_to_daily_budget(
            equity_usdt=None,
            daily={},
            requested_notional=15.0,
            leverage=5.0,
        )
        self.assertEqual(notional, 0.0)
        self.assertIn("fetch_balance", reason)


class ChopHaltTests(unittest.TestCase):
    def test_downtrend_halts(self) -> None:
        self.assertEqual(
            severe_tape_halt({"structure": "DOWNTREND", "volatility": "MEDIUM"}),
            VETO_REASON_CHOP,
        )
        self.assertEqual(
            severe_tape_halt({"structure": "RANGE", "volatility": "HIGH"}),
            VETO_REASON_CHOP,
        )
        self.assertEqual(
            severe_tape_halt({"structure": "UPTREND", "volatility": "LOW"}),
            "",
        )


class SentinelDailyTests(unittest.TestCase):
    def _eval(self, **kwargs):
        agent = RiskManagerAgent(DummyCortex())  # type: ignore[arg-type]
        defaults = dict(
            brief=_brief(),
            ticker={"ok": True, "last": 100.0, "bid": 99.9, "ask": 100.1, "symbol": "AAPL/USDT:USDT"},
            paper_cap_usdt=15.0,
            tradable_symbol="AAPL/USDT:USDT",
            order_book=_liquid(),
            ta={"ok": True, "rsi": 48.0, "timeframe": "15m", "period": 14, "structure": "RANGE", "volatility": "LOW"},
            fundamentals={"ok": True, "price": 100.0, "is_swap": True, "volume_24h_usdt": 1e9},
            equity_usdt=10_000.0,
            daily={"entries": 0, "wins": 0, "sl_hits": 0, "deployed_usdt": 0.0, "halt": ""},
        )
        defaults.update(kwargs)
        return agent.evaluate(**defaults)

    def test_win_streak_veto(self) -> None:
        risk = self._eval(daily={"entries": 3, "wins": 3, "sl_hits": 0, "deployed_usdt": 4.0, "halt": "WIN_STREAK", "halt_reason": VETO_REASON_WIN_STREAK})
        self.assertEqual(risk.verdict, "VETO")
        self.assertIn(VETO_REASON_WIN_STREAK, risk.black_swan_flags)

    def test_chop_veto(self) -> None:
        risk = self._eval(ta={"ok": True, "rsi": 48.0, "structure": "DOWNTREND", "volatility": "HIGH", "timeframe": "15m", "period": 14})
        self.assertEqual(risk.verdict, "VETO")
        self.assertTrue(any("Choppy" in f or "chop" in f.lower() for f in risk.black_swan_flags) or VETO_REASON_CHOP in risk.rationale)

    def test_sizes_inside_six_percent(self) -> None:
        risk = self._eval(equity_usdt=100.0, paper_cap_usdt=15.0)
        self.assertEqual(risk.verdict, "CLEAR")
        self.assertLessEqual(risk.max_notional_usdt, 7.5 + 1e-6)


class ChairmanDailyTests(unittest.TestCase):
    def test_cannot_override_win_streak(self) -> None:
        agent = ExecutiveAgent(DummyCortex())  # type: ignore[arg-type]
        risk = RiskReport(
            verdict="CLEAR",
            rationale="llm clear",
            max_notional_usdt=15.0,
            size_multiplier=1.0,
            rsi=50.0,
            daily_halt=VETO_REASON_WIN_STREAK,
        )
        decision = agent.synthesize(_brief(), risk, "AAPL/USDT:USDT", 100.0)
        self.assertEqual(decision.action, "STAND_DOWN")
        self.assertEqual(decision.consensus, "VETOED")
        self.assertIn("Win-streak", decision.reasoning)


if __name__ == "__main__":
    unittest.main()
