"""Stage timings without OAuth codes, credentials, or callback URLs."""

import time
from functools import wraps

from log import log


def auth_stage(function):
    @wraps(function)
    async def measured(*args, **kwargs):
        started = time.monotonic()
        outcome = "failed"
        try:
            result = await function(*args, **kwargs)
            outcome = "completed"
            return result
        finally:
            log.info(
                f"[AUTH_TIMING] stage={function.__name__} outcome={outcome} "
                f"duration_ms={(time.monotonic() - started) * 1000:.0f}"
            )
    return measured
