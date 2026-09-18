"""Phase 2: position guard, SL/TP math, backoff, Telegram escaping."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from connectors.bitget_paper import (
    POSITION_OPEN_MSG,
    TAKE_PROFIT_PCT,
    STOP_LOSS_PCT,
    TAKER_FEE_RATE,
    SWAP_LEVERAGE,
    _SYMBOL_CANDIDATES,
    _position_is_open,
    coerce_price,
    protective_prices,
    simulate_account_balance_change,
    stamp_gitbook_log,
)
from core.llm import (
    API_QUOTA_VETO,
    API_TIMEOUT_VETO,
    LLM_TIMEOUT_S,
    QwenCortex,
    _parse_json,
    _response_text,
    _run_with_timeout,
    _timeout_fallback,
    is_quota_fault,
)
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


class GitBookPaperLogTests(unittest.TestCase):
    REQUIRED = (
        "timestamp",
        "instrument",
        "direction",
        "quantity",
        "price",
        "account_balance_change",
    )

    def test_fill_never_logs_null_price(self) -> None:
        stamped = stamp_gitbook_log(
            {
                "ts": "2026-09-10T07:51:02+00:00",
                "symbol": "NVDA/USDT:USDT",
                "side": "buy",
                "amount": 0.03,
                "ok": True,
                "ticker": {"last": 225.32},
            }
        )
        for key in self.REQUIRED:
            self.assertIn(key, stamped)
            self.assertIsNotNone(stamped[key])
        self.assertEqual(stamped["price"], 225.32)
        self.assertEqual(stamped["entry_price"], 225.32)
        self.assertEqual(stamped["instrument"], "NVDA/USDT:USDT")
        self.assertEqual(stamped["direction"], "buy")
        self.assertEqual(stamped["quantity"], 0.03)
        expected = simulate_account_balance_change(
            side="buy", notional_usdt=225.32 * 0.03, filled=True, is_swap=True
        )
        self.assertEqual(stamped["account_balance_change"], expected)
        self.assertLess(expected, 0.0)

    def test_stand_down_zero_delta_zero_price(self) -> None:
        stamped = stamp_gitbook_log(
            {
                "ts": "2026-09-12T03:59:38+00:00",
                "symbol": "NONE",
                "side": "none",
                "ok": False,
                "status": "VETOED",
            }
        )
        self.assertEqual(stamped["price"], 0.0)
        self.assertEqual(stamped["entry_price"], 0.0)
        self.assertEqual(stamped["account_balance_change"], 0.0)
        self.assertEqual(stamped["quantity"], 0.0)

    def test_swap_margin_plus_fee_simulation(self) -> None:
        notional = 15.0
        change = simulate_account_balance_change(
            side="buy", notional_usdt=notional, filled=True, is_swap=True
        )
        self.assertAlmostEqual(
            change, -(notional / SWAP_LEVERAGE + notional * TAKER_FEE_RATE)
        )

    def test_coerce_price_never_returns_none(self) -> None:
        self.assertEqual(coerce_price(None, "", {"last": None}, 0, "x"), 0.0)
        self.assertEqual(coerce_price({"last": 220.24}), 220.24)


class UniverseCandidateTests(unittest.TestCase):
    def test_msft_and_googl_are_listed(self) -> None:
        for symbol in ("rMSFT/USDT", "MSFT/USDT", "rGOOGL/USDT", "GOOG/USDT"):
            self.assertIn(symbol, _SYMBOL_CANDIDATES)


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


class QwenTimeoutTests(unittest.TestCase):
    def test_hard_timeout_does_not_hang(self) -> None:
        import time

        def hang() -> str:
            time.sleep(30)
            return "nope"

        t0 = time.perf_counter()
        with self.assertRaises(TimeoutError):
            _run_with_timeout(hang, timeout=0.25, label="qwen:test")
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_timeout_is_forty_five_seconds(self) -> None:
        self.assertEqual(LLM_TIMEOUT_S, 45)

    def test_timeout_fallback_is_veto(self) -> None:
        out = _timeout_fallback(
            {
                "rationale": "x",
                "verdict": "CLEAR",
                "thesis": "y",
                "conviction": 80,
                "primary_symbol": "NVDA/USDT:USDT",
                "side": "buy",
            },
            TimeoutError("timed out after 45s"),
        )
        self.assertEqual(out["verdict"], "VETO")
        self.assertEqual(out["rationale"], API_TIMEOUT_VETO)
        self.assertEqual(out["thesis"], API_TIMEOUT_VETO)
        self.assertEqual(out["conviction"], 0)
        self.assertEqual(out["primary_symbol"], "NONE")
        self.assertEqual(out["side"], "none")

    def test_unterminated_json_is_parser_fault(self) -> None:
        import json as jsonlib

        with self.assertRaises(jsonlib.JSONDecodeError):
            _parse_json('{"thesis": "Apple said "iPhone" sales\n"side": "buy"')

    def test_generate_json_parse_fault_returns_fallback(self) -> None:
        cortex = QwenCortex.__new__(QwenCortex)
        cortex.last_error = None
        cortex.model_name = "test"
        cortex._complete = lambda *a, **k: '{"thesis": "Apple said "iPhone"\n"rationale":'
        fallback = {
            "thesis": "keep",
            "rationale": "keep",
            "primary_symbol": "NVDA/USDT:USDT",
            "side": "buy",
            "conviction": 80,
            "verdict": "CLEAR",
        }
        payload, degraded = cortex.generate_json("sys", "user", fallback=fallback)
        self.assertTrue(degraded)
        self.assertEqual(payload["primary_symbol"], "NONE")
        self.assertEqual(payload["side"], "none")
        self.assertEqual(payload["conviction"], 0)
        self.assertEqual(payload["verdict"], "VETO")
        self.assertIn("parser fault", (cortex.last_error or "").lower())
        self.assertNotIn("Unterminated string", cortex.last_error or "")

    def test_503_unavailable_forces_stand_down(self) -> None:
        out = _timeout_fallback(
            {"verdict": "CLEAR", "primary_symbol": "AAPL/USDT:USDT", "side": "buy", "conviction": 70},
            RuntimeError("503 UNAVAILABLE high demand"),
        )
        self.assertEqual(out["verdict"], "VETO")
        self.assertEqual(out["primary_symbol"], "NONE")
        self.assertEqual(out["side"], "none")
        self.assertEqual(out["conviction"], 0)

    def test_429_quota_stand_down_is_not_a_crash(self) -> None:
        self.assertTrue(is_quota_fault(RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")))
        self.assertTrue(is_quota_fault(RuntimeError("Throttling.RateQuota flow control")))
        self.assertTrue(is_quota_fault("Bitget Qwen API quota reached. System safely standing down until limits reset."))
        out = _timeout_fallback(
            {
                "verdict": "CLEAR",
                "primary_symbol": "NVDA/USDT:USDT",
                "side": "buy",
                "conviction": 90,
                "thesis": "trade it",
                "rationale": "trade it",
            },
            RuntimeError("429 Resource exhausted: free_tier quota exceeded"),
        )
        self.assertEqual(out["primary_symbol"], "NONE")
        self.assertEqual(out["side"], "none")
        self.assertEqual(out["conviction"], 0)
        self.assertEqual(out["rationale"], API_QUOTA_VETO)
        self.assertEqual(out["thesis"], API_QUOTA_VETO)
        self.assertEqual(
            API_QUOTA_VETO,
            "Bitget Qwen API quota reached. System safely standing down until limits reset.",
        )

    def test_generate_json_quota_returns_fallback(self) -> None:
        cortex = QwenCortex.__new__(QwenCortex)
        cortex.last_error = None
        cortex.model_name = "test"

        def boom(*a, **k):
            raise RuntimeError("429 RESOURCE_EXHAUSTED: Quota exceeded for free_tier")

        cortex._complete = boom
        payload, degraded = cortex.generate_json(
            "sys",
            "user",
            fallback={
                "primary_symbol": "AAPL/USDT:USDT",
                "side": "buy",
                "conviction": 55,
                "thesis": "x",
                "rationale": "x",
            },
        )
        self.assertTrue(degraded)
        self.assertEqual(payload["primary_symbol"], "NONE")
        self.assertEqual(payload["side"], "none")
        self.assertEqual(payload["conviction"], 0)
        self.assertEqual(payload["rationale"], API_QUOTA_VETO)
        self.assertEqual(cortex.last_error, API_QUOTA_VETO)

    def test_openai_choice_content_is_parsed(self) -> None:
        from types import SimpleNamespace

        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"verdict": "CLEAR", "rationale": "ok"}'
                    )
                )
            ]
        )
        payload = _parse_json(_response_text(response))
        self.assertEqual(payload["verdict"], "CLEAR")
        self.assertEqual(payload["rationale"], "ok")

    def test_openai_dict_and_fenced_json(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": '```json\n{"action": "STAND_DOWN", "consensus": "VETOED"}\n```'
                    }
                }
            ]
        }
        payload = _parse_json(_response_text(response))
        self.assertEqual(payload["action"], "STAND_DOWN")
        self.assertEqual(payload["consensus"], "VETOED")

    def test_responses_output_text_is_parsed(self) -> None:
        from types import SimpleNamespace

        response = SimpleNamespace(
            choices=None,
            output_text='{"verdict": "CLEAR", "rationale": "responses wire"}',
            output=[
                SimpleNamespace(
                    type="reasoning",
                    content=[{"text": "internal chain of thought"}],
                )
            ],
        )
        payload = _parse_json(_response_text(response))
        self.assertEqual(payload["verdict"], "CLEAR")
        self.assertEqual(payload["rationale"], "responses wire")


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
        n.alert_news_analysis(headlines=["x"], news_good="g", news_bad="b")
        n.alert_stay_away(items=["NFLX — miss"])
        n.alert_stand_down(reason="no tape")
        n.alert_api_error(error="504 DEADLINE_EXCEEDED")
        n.alert_api_error(error="429 RESOURCE_EXHAUSTED quota exceeded")
        n.drain(timeout=0.1)


if __name__ == "__main__":
    unittest.main()
