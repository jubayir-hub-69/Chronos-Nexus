"""Phase 2: position guard, SL/TP math, backoff, Telegram escaping."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from connectors.bitget_paper import (
    POSITION_OPEN_MSG,
    TAKE_PROFIT_PCT,
    STOP_LOSS_PCT,
    _position_is_open,
    protective_prices,
)
from core.llm import API_TIMEOUT_VETO, _run_with_timeout, _timeout_fallback
from core.retry import call_with_backoff, is_retryable
from utils.notifier import TelegramNotifier, escape_html


class ProtectiveOrderTests(unittest.TestCase):
    def test_sl_tp_from_entry(self) -> None:
        sl, tp = protective_prices(100.0)
        self.assertAlmostEqual(sl, 98.0)
        self.assertAlmostEqual(tp, 105.0)
        self.assertEqual(STOP_LOSS_PCT, 0.02)
        self.assertEqual(TAKE_PROFIT_PCT, 0.05)


class PositionTests(unittest.TestCase):
    def test_open_swap_position_matches_symbol(self) -> None:
        pos = {"symbol": "NVDA/USDT:USDT", "contracts": 0.01, "side": "long"}
        self.assertTrue(_position_is_open(pos, "NVDA/USDT:USDT"))
        self.assertTrue(_position_is_open(pos, "NVDA/USDT"))

    def test_flat_or_zero_is_not_open(self) -> None:
        self.assertFalse(_position_is_open({"symbol": "NVDA/USDT:USDT", "contracts": 0, "side": "long"}, "NVDA/USDT:USDT"))
        self.assertFalse(_position_is_open({"symbol": "BTC/USDT:USDT", "contracts": 1, "side": "long"}, "NVDA/USDT:USDT"))
        self.assertEqual(POSITION_OPEN_MSG, "Position already open")


class RetryTests(unittest.TestCase):
    def test_429_and_timeout_are_retryable(self) -> None:
        self.assertTrue(is_retryable(RuntimeError("429 Too Many Requests")))
        self.assertTrue(is_retryable(TimeoutError("deadline exceeded")))
        self.assertFalse(is_retryable(RuntimeError("insufficient margin 25203")))

    def test_backoff_retries_then_succeeds(self) -> None:
        hits = {"n": 0}

        def flaky() -> str:
            hits["n"] += 1
            if hits["n"] < 3:
                raise TimeoutError("timed out")
            return "ok"

        with patch("core.retry.time.sleep") as slept:
            self.assertEqual(call_with_backoff(flaky, attempts=3, base_delay=0.01), "ok")
            self.assertEqual(hits["n"], 3)
            self.assertEqual(slept.call_count, 2)

    def test_non_retryable_fails_fast(self) -> None:
        hits = {"n": 0}

        def auth() -> None:
            hits["n"] += 1
            raise RuntimeError("authentication failed")

        with patch("core.retry.time.sleep") as slept:
            with self.assertRaises(RuntimeError):
                call_with_backoff(auth, attempts=3)
            self.assertEqual(hits["n"], 1)
            slept.assert_not_called()


class GeminiTimeoutTests(unittest.TestCase):
    def test_hard_timeout_does_not_hang(self) -> None:
        import time

        def hang() -> str:
            time.sleep(30)
            return "nope"

        t0 = time.perf_counter()
        with self.assertRaises(TimeoutError):
            _run_with_timeout(hang, timeout=0.25, label="gemini:test")
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_timeout_fallback_is_veto(self) -> None:
        out = _timeout_fallback(
            {"rationale": "x", "verdict": "CLEAR", "thesis": "y", "conviction": 80},
            TimeoutError("timed out after 15s"),
        )
        self.assertEqual(out["verdict"], "VETO")
        self.assertEqual(out["rationale"], API_TIMEOUT_VETO)
        self.assertEqual(out["thesis"], API_TIMEOUT_VETO)
        self.assertEqual(out["conviction"], 0)


class TelegramTests(unittest.TestCase):
    def test_html_escapes(self) -> None:
        escaped = escape_html("<NVDA/USDT> & friends")
        self.assertIn("&lt;NVDA/USDT&gt;", escaped)
        self.assertIn("&amp;", escaped)
        self.assertNotIn("<NVDA", escaped)

    def test_disabled_notifier_does_not_send(self) -> None:
        n = TelegramNotifier("", "")
        self.assertFalse(n.enabled)
        n.alert_veto(reason="x", symbol="NVDA/USDT:USDT")
        n.drain(timeout=0.1)


if __name__ == "__main__":
    unittest.main()
