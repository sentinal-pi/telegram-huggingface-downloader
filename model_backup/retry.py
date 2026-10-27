"""Bounded retries for HTTP 429, with progress visible to the Telegram process."""
import math
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx
from huggingface_hub import get_session, set_client_factory

from .core import atomic_json


class RateLimited(Exception):
    def __init__(self, headers):
        super().__init__('HTTP 429: rate limited')
        self.headers = {k.lower(): v for k, v in headers.items()
                        if k.lower() in ('retry-after', 'ratelimit')}


def check_rate_limit(response):
    if response.status_code == 429:
        error = RateLimited(response.headers)
        response.close()
        raise error


def configure_http_client():
    # Public HF API. Own 429 handling avoids hidden SDK waits/double retries.
    # Xet has its own HTTP client; its final 429 failures are handled below too.
    # Preserve SDK request hooks (offline mode, request IDs, download metadata).
    request_hooks = list(get_session().event_hooks.get('request', []))
    set_client_factory(lambda: httpx.Client(follow_redirects=True, timeout=None,
                                           event_hooks={'request': request_hooks,
                                                        'response': [check_rate_limit]}))


@dataclass(frozen=True)
class RetryPolicy:
    retries: int = 5
    base_seconds: float = 30
    max_seconds: float = 600

    @classmethod
    def from_env(cls):
        policy = cls(int(os.getenv('HF_MAX_RETRIES', '5')),
                     float(os.getenv('HF_RETRY_BASE_SECONDS', '30')),
                     float(os.getenv('HF_RETRY_MAX_SECONDS', '600')))
        if not 0 <= policy.retries <= 100 or not all(
            math.isfinite(value) and 0 < value <= 86400
            for value in (policy.base_seconds, policy.max_seconds)
        ) or policy.base_seconds > policy.max_seconds:
            raise ValueError('HF_MAX_RETRIES ต้องเป็น 0–100 และเวลา retry ต้องเป็นเลขบวก '
                             'base <= max <= 86400 วินาที')
        return policy


def rate_limit_headers(error):
    seen = set()
    pending = [error]
    while pending:
        exc = pending.pop()
        if id(exc) in seen:
            continue
        seen.add(id(exc))
        if isinstance(exc, RateLimited):
            return exc.headers
        response = getattr(exc, 'response', None)
        if response is not None and getattr(response, 'status_code', None) == 429:
            return {k.lower(): v for k, v in response.headers.items()}
        # Rust Xet errors do not expose an HTTP response object.
        if isinstance(exc, RuntimeError) and re.search(
            r'429\s+Too Many Requests|status(?: code)?[:= ]+429\b', str(exc), re.IGNORECASE
        ):
            return {}
        pending.extend(cause for cause in (exc.__cause__, exc.__context__) if cause is not None)
    return None


def retry_delay(headers, attempt, policy):
    hints = []
    value = headers.get('retry-after', '').strip()
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            hints.append(seconds)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            hints.append(max(0, date.timestamp() - time.time()))
        except (ValueError, TypeError, OverflowError):
            pass
    hints.extend(float(match) for match in re.findall(
        r';\s*r\s*=\s*0\s*;\s*t\s*=\s*(\d+)', headers.get('ratelimit', '')))
    # Never shorten the server's wait with the configurable fallback cap.
    delay = max(hints) if hints else min(policy.max_seconds, policy.base_seconds * 2**attempt)
    return delay + 1 + random.uniform(0, min(delay * 0.05, 5))


def with_rate_limit_retry(operation, progress, details, policy):
    for attempt in range(policy.retries + 1):
        atomic_json(progress, details)
        try:
            return operation()
        except Exception as exc:
            headers = rate_limit_headers(exc)
            if headers is None:
                raise
            if attempt == policy.retries:
                raise ValueError(f'ติด Rate Limit (HTTP 429) หลังลองใหม่ {policy.retries} ครั้ง '
                                 'รอแล้วใช้ /retry job_id เพื่อลองงานนี้ใหม่') from None
            delay = retry_delay(headers, attempt, policy)
            atomic_json(progress, {**details, 'phase': 'rate_limited',
                                  'retry_number': attempt + 1, 'retry_limit': policy.retries,
                                  'retry_at': time.time() + delay})
            # This is a child process: /cancel terminates it even while sleeping.
            time.sleep(delay)
