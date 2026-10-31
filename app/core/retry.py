"""
Retry utility used by connectors. Handles exponential backoff and Retry-After headers.
"""
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class RetryableError(Exception):
    """Raised by connectors for errors that should trigger a retry."""
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after  # seconds; from Retry-After header if available


class NonRetryableError(Exception):
    """Raised by connectors for errors that should not be retried."""


def retry_with_backoff(
    fn: Callable[[], T],
    max_attempts: int = 5,
    base_delay: float = 0.5,
    backoff_factor: float = 2.0,
) -> T:
    """
    Call fn() up to max_attempts times, with exponential backoff on RetryableError.
    Respects retry_after if set on the exception.
    Raises the last exception if all attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except RetryableError as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after else base_delay * (backoff_factor ** (attempt - 1))
            time.sleep(delay)
        except NonRetryableError:
            raise
    raise last_exc  # type: ignore[misc]
