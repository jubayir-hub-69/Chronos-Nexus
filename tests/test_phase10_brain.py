"""Phase 10: 75% TA setup ensemble, MTF confluence, book walls, news NLP."""

from __future__ import annotations

import unittest

from connectors.bitget_paper import _book_payload
from core.news import conviction_score, score_headline, score_wire
from core.ta import (
    SETUP_THRESHOLD,
    VETO_REASON_MTF,
    VETO_REASON_SETUP,
    VETO_REASON_WALL,
    analyze_book,
    compute_sma,
    mtf_alignment,
    setup_veto,
)


def _frame(
    *,
    structure: str,
    bias: str,
    rsi: float = 52.0,
    rvol: float = 1.6,
    vwap_dev: float = 0.003,
    sma_bias: str = "",
    structure_break: bool = False,
) -> dict:
    return {
        "ok": True,
        "structure": structure,
        "bias": bias,
        "rsi": rsi,
        "rvol": rvol,
        "vwap_dev_pct": vwap_dev,
        "in_value_area": True,
        "structure_break": structure_break,
        "sma_bias": sma_bias or bias,
    }


def _aligned_frames() -> dict:
    up = _frame(structure="UPTREND", bias="bullish", rsi=52.0, rvol=1.7, vwap_dev=0.004)
    return {"15m": up, "1h": dict(up, rsi=55.0), "4h": dict(up, rsi=58.0)}


def _news_bull() -> dict:
    return {
        "scored": True,
        "sentiment": 74.0,
        "credibility": 0.98,
        "conflict": False,
        "impact": "high",
        "macro": False,
        "recency": 1.0,
    }


def _bid_heavy_book() -> dict:
    return _book_payload(
        "AAPL/USDT:USDT",
        [[100.0, 12.0], [99.9, 8.0], [99.8, 6.0]],
        [[100.1, 1.5], [100.2, 1.2], [100.3, 1.0]],
        ok=True,
        source="l2",
        error=None,
    )


class NewsNlpTests(unittest.TestCase):
    def test_earnings_beat_is_bull_with_source_weight(self) -> None:
        cnbc = score_headline("Apple earnings beat estimates, raises guidance", "CNBC")
        rumor = score_headline("Apple earnings beat estimates, raises guidance", "chronos-nexus")
        self.assertGreaterEqual(cnbc["sentiment"], 58)
        self.assertEqual(cnbc["direction"], "bull")
        self.assertGreater(cnbc["sentiment"], rumor["sentiment"])
        self.assertGreaterEqual(cnbc["credibility"], rumor["credibility"])

    def test_conviction_caps_conflicted_tape(self) -> None:
        score = conviction_score(
            "buy",
            {
                "scored": True,
                "sentiment": 70.0,
                "credibility": 0.9,
                "recency": 1.0,
                "impact": "high",
                "conflict": True,
            },
        )
        self.assertEqual(score, 20)

    def test_wire_aggregate_is_zero_to_one_hundred(self) -> None:
        wire = score_wire(
            [
                {
                    "headline": "Nvidia raises guidance after earnings beat",
                    "source": "Bloomberg",
                    "published": "",
                }
            ]
        )
        self.assertTrue(wire["scored"])
        self.assertGreaterEqual(wire["sentiment"], 0)
        self.assertLessEqual(wire["sentiment"], 100)
        self.assertGreaterEqual(wire["conviction"], 0)
        self.assertLessEqual(wire["conviction"], 100)


class SmaAndMtfTests(unittest.TestCase):
    def test_sma_window(self) -> None:
        self.assertIsNone(compute_sma([1, 2, 3], 20))
        self.assertAlmostEqual(compute_sma(list(range(1, 21)), 20), 10.5)

    def test_mtf_aligned_vs_conflict(self) -> None:
        frames = _aligned_frames()
        self.assertEqual(mtf_alignment(frames, "buy"), "ALIGNED")
        frames["4h"] = _frame(structure="DOWNTREND", bias="bearish")
        self.assertEqual(mtf_alignment(frames, "buy"), "CONFLICT")

    def test_unmeasured_without_htf(self) -> None:
        self.assertEqual(mtf_alignment({"15m": _frame(structure="RANGE", bias="neutral")}, "buy"), "UNMEASURED")


class SetupThresholdTests(unittest.TestCase):
    def test_unmeasured_htf_does_not_fire_setup_rail(self) -> None:
        """Zero-regression: RSI-only unit fixtures must not die on setup < 75."""
        reason, scored = setup_veto(
            side="buy",
            frames={"15m": {"ok": True, "rsi": 48.0, "timeframe": "15m", "structure": "RANGE"}},
            book=_book_payload(
                "AAPL/USDT:USDT",
                [[99.9, 2.0]],
                [[100.1, 2.0]],
                ok=True,
                source="l2",
                error=None,
            ),
            last=100.0,
            news={"scored": False, "sentiment": 50.0},
            conviction=80,
        )
        self.assertFalse(scored["measurable"])
        self.assertNotEqual(reason, VETO_REASON_SETUP)
        self.assertEqual(reason, "")

    def test_quality_setup_clears_threshold(self) -> None:
        reason, scored = setup_veto(
            side="buy",
            frames=_aligned_frames(),
            book=_bid_heavy_book(),
            last=100.0,
            news=_news_bull(),
            conviction=88,
        )
        self.assertTrue(scored["measurable"])
        self.assertGreaterEqual(scored["score"], SETUP_THRESHOLD)
        self.assertEqual(reason, "")

    def test_measured_weak_setup_is_rejected(self) -> None:
        weak = {
            "15m": _frame(structure="RANGE", bias="neutral", rsi=50.0, rvol=0.5, vwap_dev=0.003),
            "1h": _frame(structure="UPTREND", bias="bullish", rsi=55.0, rvol=0.6),
            "4h": _frame(structure="UPTREND", bias="bullish", rsi=58.0, rvol=0.6),
        }
        reason, scored = setup_veto(
            side="buy",
            frames=weak,
            book=_book_payload(
                "AAPL/USDT:USDT",
                [[99.9, 1.0]],
                [[100.1, 1.0]],
                ok=True,
                source="l2",
                error=None,
            ),
            last=100.0,
            news={"scored": True, "sentiment": 58.0, "credibility": 0.5, "conflict": False, "impact": "low"},
            conviction=20,
        )
        self.assertTrue(scored["measurable"])
        self.assertLess(scored["score"], SETUP_THRESHOLD)
        self.assertEqual(reason, VETO_REASON_SETUP)

    def test_htf_conflict_vetoes(self) -> None:
        frames = _aligned_frames()
        frames["4h"] = _frame(structure="DOWNTREND", bias="bearish")
        reason, scored = setup_veto(
            side="buy",
            frames=frames,
            book=_bid_heavy_book(),
            last=100.0,
            news=_news_bull(),
            conviction=88,
        )
        self.assertEqual(reason, VETO_REASON_MTF)
        self.assertEqual(scored["align"], "CONFLICT")

    def test_ask_wall_blocks_buy(self) -> None:
        book = _book_payload(
            "AAPL/USDT:USDT",
            [[99.95, 1.0], [99.9, 1.0], [99.8, 1.0]],
            [[100.05, 20.0], [100.1, 1.0], [100.2, 1.0]],
            ok=True,
            source="l2",
            error=None,
        )
        wall = analyze_book(book, side="buy", last=100.0)
        self.assertEqual(wall["veto"], VETO_REASON_WALL)
        reason, _ = setup_veto(
            side="buy",
            frames=_aligned_frames(),
            book=book,
            last=100.0,
            news=_news_bull(),
            conviction=88,
        )
        self.assertEqual(reason, VETO_REASON_WALL)


if __name__ == "__main__":
    unittest.main()
