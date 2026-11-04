"""Retry with exponential backoff and jitter."""
from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def backoff_delay(attempt: int, *, base_delay: float = 1.0, factor: float = 2.0,
                  max_delay: float = 30.0, jitter: float = 0.1,
                  rng: Callable[[], float] = random.random) -> float:
    """Seconds to wait after the given failed attempt (1-based).

    The delay doubles each time up to max_delay; jitter spreads it by +/- that fraction
    so that many clients do not retry in lock-step.
    """
    delay = min(max_delay, base_delay * factor ** (attempt - 1))
    return max(0.0, delay * (1 + jitter * (2 * rng() - 1)))


def retry_call(fn: Callable[[], T], *, attempts: int = 3,
               retry_on: tuple[type[BaseException], ...] = (Exception,),
               sleep: Callable[[float], None] = time.sleep,
               on_retry: Callable[[int, BaseException, float], None] | None = None,
               **backoff) -> T:
    """Call fn until it succeeds or attempts are used up; the last error is re-raised.

    Only exceptions in retry_on are retried; anything else propagates immediately.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = backoff_delay(attempt, **backoff)
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
