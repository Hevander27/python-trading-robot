"""
Shared utilities for the trading robot.

retry_with_backoff -- decorator for API calls that may hit Schwab rate limits
    (HTTP 429) or transient server errors (HTTP 500). Retries up to max_retries
    times with exponentially increasing delays between attempts.
"""

import time
import functools
import logging

logger = logging.getLogger(__name__)


def retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0, backoff_factor: float = 2.0):
    """Decorator that retries a function on exception with exponential backoff.

    Arguments:
    ----
    max_retries {int} -- Maximum number of retry attempts after the first failure.
        e.g. 3 means up to 4 total attempts. (default: 3)
    base_delay {float} -- Wait time in seconds before the first retry. (default: 1.0)
    backoff_factor {float} -- Multiplier applied to delay after each failure.
        e.g. 2.0 with base_delay=1.0 gives waits of 1s, 2s, 4s. (default: 2.0)

    Example:
    ----
        @retry_with_backoff(max_retries=3, base_delay=1.0)
        def fetch_quotes(self, symbols):
            return self.schwab_client.get_quotes(symbols).json()
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries:
                        logger.error(
                            f"{func.__name__} failed after {max_retries + 1} attempts: {e}"
                        )
                        raise
                    logger.warning(
                        f"{func.__name__} attempt {attempt + 1}/{max_retries + 1} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                    delay *= backoff_factor
        return wrapper
    return decorator
