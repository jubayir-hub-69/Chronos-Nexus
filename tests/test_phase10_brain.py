"""Phase 10: 75% TA setup ensemble, MTF confluence, book walls, news NLP."""

from __future__ import annotations

import unittest

from connectors.bitget_paper import _book_payload
from core.news import conviction_score, score_headline, score_wire
from core.neutral_lane import seek_neutral_candidate
from core.ta import (
    NEUTRAL_SETUP_THRESHOLD,
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

    def _mixed_quiet_frames(self) -> dict:
        return {
            "15m": _frame(structure="RANGE", bias="bullish", rsi=52.0, rvol=1.6),
            "1h": _frame(structure="UPTREND", bias="bullish", rsi=55.0, rvol=1.6),
            "4h": _frame(structure="RANGE", bias="neutral", rsi=50.0, rvol=1.0),
        }

    def _flat_book(self) -> dict:
        return _book_payload(
            "AAPL/USDT:USDT",
            [[99.9, 2.0]],
            [[100.1, 2.0]],
            ok=True,
            source="l2",
            error=None,
        )

    def test_neutral_premarket_strong_volume_clears_at_70(self) -> None:
        frames = self._mixed_quiet_frames()
        book = self._flat_book()
        blocked, blocked_score = setup_veto(
            side="buy",
            frames=frames,
            book=book,
            last=100.0,
            news={"scored": False, "sentiment": 50.0},
            conviction=0,
        )
        self.assertEqual(blocked, VETO_REASON_SETUP)
        self.assertGreaterEqual(blocked_score["score"], NEUTRAL_SETUP_THRESHOLD)
        self.assertLess(blocked_score["score"], SETUP_THRESHOLD)
        reason, scored = setup_veto(
            side="buy",
            frames=frames,
            book=book,
            last=100.0,
            news={"scored": False, "sentiment": 50.0},
            conviction=0,
            volume_24h=1_500_000.0,
            session="PRE-MARKET — US cash not yet open",
        )
        self.assertEqual(reason, "")
        self.assertTrue(scored["neutral_lane"])
        self.assertGreaterEqual(scored["score"], NEUTRAL_SETUP_THRESHOLD)
        self.assertLess(scored["score"], SETUP_THRESHOLD)

    def test_thin_volume_keeps_75_rail(self) -> None:
        reason, scored = setup_veto(
            side="buy",
            frames=self._mixed_quiet_frames(),
            book=self._flat_book(),
            last=100.0,
            news={"scored": False, "sentiment": 50.0},
            conviction=0,
            volume_24h=50_000.0,
            session="PRE-MARKET — US cash not yet open",
        )
        self.assertGreaterEqual(scored["score"], NEUTRAL_SETUP_THRESHOLD)
        self.assertLess(scored["score"], SETUP_THRESHOLD)
        self.assertEqual(reason, VETO_REASON_SETUP)
        self.assertFalse(scored["neutral_lane"])

    def test_premarket_conflict_still_vetoes(self) -> None:
        frames = _aligned_frames()
        frames["4h"] = _frame(structure="DOWNTREND", bias="bearish")
        reason, scored = setup_veto(
            side="buy",
            frames=frames,
            book=_bid_heavy_book(),
            last=100.0,
            news={"scored": False, "sentiment": 50.0},
            conviction=0,
            volume_24h=5_000_000.0,
            session="PRE-MARKET — US cash not yet open",
        )
        self.assertEqual(reason, VETO_REASON_MTF)
        self.assertEqual(scored["align"], "CONFLICT")


class NeutralScanTests(unittest.TestCase):
    def test_seek_promotes_liquid_tape_and_skips_stay_away(self) -> None:
        class _Bitget:
            def fetch_fundamentals(self, symbol: str) -> dict:
                vol = 80_000.0 if "NVDA" in symbol else 2_000_000.0
                return {"ok": True, "volume_24h_usdt": vol, "price": 100.0}

            def fetch_mtf_bundle(self, symbol: str) -> dict:
                del symbol
                up = _frame(structure="UPTREND", bias="bullish", rsi=52.0, rvol=1.7)
                return {"ok": True, "frames": {"15m": up, "1h": up, "4h": up}, **up}

            def fetch_order_book(self, symbol: str) -> dict:
                return _bid_heavy_book()

            def fetch_ticker(self, symbol: str) -> dict:
                del symbol
                return {"last": 100.0, "high": 104.0, "low": 96.0, "quoteVolume": 2_000_000.0}

        pick = seek_neutral_candidate(
            _Bitget(),
            ["rNVDA/USDT:USDT", "rAAPL/USDT:USDT"],
            news={"scored": False, "sentiment": 50.0},
            session="PRE-MARKET — US cash not yet open",
            stay_away=["NVDA — headline risk"],
        )
        self.assertIsNotNone(pick)
        assert pick is not None
        self.assertEqual(pick["symbol"], "rAAPL/USDT:USDT")
        self.assertEqual(pick["side"], "buy")
        self.assertGreaterEqual(pick["score"], NEUTRAL_SETUP_THRESHOLD)
        self.assertGreaterEqual(pick["conviction"], 55)

    def test_seek_stands_down_on_a_directional_cash_session(self) -> None:
        class _Bitget:
            def fetch_fundamentals(self, symbol: str) -> dict:
                del symbol
                raise AssertionError("cash-session scan must not hit the book")

        pick = seek_neutral_candidate(
            _Bitget(),
            ["rAAPL/USDT:USDT"],
            news={"scored": True, "sentiment": 74.0, "conflict": False},
            session="US CASH OPEN (regular session 09:30–16:00 ET)",
            stay_away=[],
        )
        self.assertIsNone(pick)


if __name__ == "__main__":
    unittest.main()
