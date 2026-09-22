"""Exponential backoff for external API calls.

3 attempts. Retry only on rate-limit / timeout / transient network faults.
Callers catch the final exception and degrade — never crash the CIC.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.75
DEFAULT_MAX_DELAY = 6.0

_RETRY_TOKENS = (
    "429",
    "rate limit",
    "ratelimit",
    "throttling",
    "ratequota",
    "too many requests",
    "resource exhausted",
    "timeout",
    "timed out",
    "deadline",
    "temporarily unavailable",
    "service unavailable",
    "502",
    "503",
    "504",
    "connection",
    "connecterror",
    "reset by peer",
    "econnreset",
    "econnrefused",
    "network",
    "temporarily",
    "try again",
)

_RETRY_TYPE_TOKENS = (
    "timeout",
    "network",
    "ratelimit",
    "ddos",
    "unavailable",
    "connection",
    "requesttimeout",
    "exchangenotavailable",
)

_NO_RETRY_TOKENS = (
    "insufficient funds",
    "insufficient margin",
    "insufficient balance",
    "authentication",
    "invalid api",
    "invalid key",
    "invalid order",
    "nonce too low",
    "already known",
    "25203",
    "25202",
    "45113",
    "45112",
    "45110",
    "45104",
    "45103",
    "40404",
    "request url not found",
    "less than the minimum amount",
    "maximum order value",
    "maximum order quantity",
)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, BrokenPipeError, OSError)):
        return True
    name = type(exc).__name__.lower()
    if any(tok in name for tok in _RETRY_TYPE_TOKENS):
        return True
    msg = str(exc).lower()
    if any(tok in msg for tok in _NO_RETRY_TOKENS):
        return False
    return any(tok in msg for tok in _RETRY_TOKENS)


def call_with_backoff(
    fn: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    label: str = "api",
) -> T:
    """Run `fn` up to `attempts` times with exponential sleep on retryable faults."""
    last: BaseException | None = None
    tries = max(1, int(attempts))
    for i in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — must classify every rail fault
            last = exc
            if i >= tries or not is_retryable(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (i - 1)))
            time.sleep(delay)
    raise RuntimeError(f"{label} failed after {tries} attempts: {last}") from last
