"""
One retry policy for the whole codebase: exponential backoff with jitter, built
on tenacity (already a dependency). Wrap httpx calls to Qdrant, Ollama, and the
sub-agents so a transient blip retries instead of failing the request.

Usage:
    from common.retry import async_retry

    @async_retry()
    async def fetch():
        ...

    # or inline
    result = await async_retry()(fetch)()
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_MIN_WAIT = float(os.getenv("RETRY_MIN_WAIT", "0.5"))
RETRY_MAX_WAIT = float(os.getenv("RETRY_MAX_WAIT", "8.0"))

# Exceptions worth retrying: network/timeout transients.
try:
    import httpx
    _RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)
except Exception:  # pragma: no cover
    _RETRYABLE = (Exception,)


def async_retry(
    attempts: int = RETRY_ATTEMPTS,
    min_wait: float = RETRY_MIN_WAIT,
    max_wait: float = RETRY_MAX_WAIT,
    retry_on: tuple = _RETRYABLE,
):
    """Return a decorator that retries an async function with backoff + jitter.

    Falls back to a no-op passthrough if tenacity is unavailable, so importing
    this module never hard-fails a service.
    """
    try:
        from tenacity import (
            retry, stop_after_attempt, wait_exponential_jitter,
            retry_if_exception_type, before_sleep_log,
        )
    except Exception:  # pragma: no cover
        def _passthrough(fn):
            return fn
        return _passthrough

    return retry(
        reraise=True,
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=min_wait, max=max_wait),
        retry=retry_if_exception_type(retry_on),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
