import pytest

from seqflow.retry import backoff_delay, retry_call


def test_backoff_doubles_and_is_capped():
    middle = lambda: 0.5
    delays = [backoff_delay(n, base_delay=1, max_delay=10, rng=middle) for n in range(1, 7)]
    assert delays == [1, 2, 4, 8, 10, 10]


def test_jitter_stays_within_bounds():
    low = backoff_delay(1, base_delay=10, jitter=0.2, rng=lambda: 0.0)
    high = backoff_delay(1, base_delay=10, jitter=0.2, rng=lambda: 1.0)
    assert (low, high) == (8.0, 12.0)
    assert backoff_delay(1, base_delay=0, rng=lambda: 0.0) == 0.0


def test_returns_first_success_without_sleeping():
    slept = []
    assert retry_call(lambda: 42, sleep=slept.append) == 42
    assert slept == []


def test_retries_then_succeeds_and_reports_each_retry():
    calls, slept, seen = [], [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OSError("temporary")
        return "ok"

    result = retry_call(flaky, attempts=5, retry_on=(OSError,), sleep=slept.append,
                        on_retry=lambda n, exc, delay: seen.append((n, str(exc))),
                        base_delay=1, jitter=0)
    assert result == "ok" and len(calls) == 3
    assert slept == [1.0, 2.0]
    assert seen == [(1, "temporary"), (2, "temporary")]


def test_gives_up_and_raises_the_last_error():
    calls = []

    def always_fails():
        calls.append(1)
        raise OSError(f"failure {len(calls)}")

    with pytest.raises(OSError, match="failure 3"):
        retry_call(always_fails, attempts=3, retry_on=(OSError,), sleep=lambda _: None)
    assert len(calls) == 3


def test_other_exceptions_are_not_retried():
    calls = []

    def wrong():
        calls.append(1)
        raise ValueError("a bug, not a glitch")

    with pytest.raises(ValueError):
        retry_call(wrong, attempts=5, retry_on=(OSError,), sleep=lambda _: None)
    assert len(calls) == 1


def test_attempts_must_be_positive():
    with pytest.raises(ValueError):
        retry_call(lambda: 1, attempts=0)
