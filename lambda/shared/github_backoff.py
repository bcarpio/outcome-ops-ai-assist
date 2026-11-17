# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""GitHub API backoff wrapper ().

At enterprise scale (thousands of repos, org-wide PR analysis) the
GitHub App installation-token 5000 req/hr ceiling matters. Prior
behavior across `github-sync`, `analyze-pr`, and `shared/source_fetch`
was to call `requests.get` / `urllib.request.urlopen` directly with
no 403 / X-RateLimit / Retry-After awareness — a rate-limit hit fails
the request, ships to DLQ, and silently leaves code-map gaps.

This module wraps HTTP requests with:
- 403 + "rate limit" body detection -> sleep and retry.
- X-RateLimit-Remaining==0 preemptive detection -> sleep until reset.
- 429 status -> sleep per Retry-After.
- 5xx status -> exponential backoff.

Two interfaces:
- `github_request(...)`: `requests` shape (used by analyze-pr, source_fetch).
- `github_urllib_request(...)`: `urllib.request` shape (used by github-sync).

Env knobs:
- GITHUB_BACKOFF_MAX_ATTEMPTS  default 5
- GITHUB_BACKOFF_MAX_DELAY_S   default 60 (skip retry if Retry-After > this)
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

def _default_max_attempts() -> int:
    try:
        return int(os.environ.get("GITHUB_BACKOFF_MAX_ATTEMPTS", "5"))
    except ValueError:
        return 5


def _max_delay_s() -> int:
    try:
        return int(os.environ.get("GITHUB_BACKOFF_MAX_DELAY_S", "60"))
    except ValueError:
        return 60


def _retry_delay(headers: Dict[str, str], attempt: int) -> Optional[float]:
    """Extract the wait-until delay in seconds from GitHub response headers.

    Precedence:
    1. Retry-After (RFC-standard 429/503 header) — highest priority.
    2. X-RateLimit-Reset (GitHub-specific, epoch seconds) — when the primary
       limit resets.
    3. Fallback exponential backoff base=1s, cap=8s, jittered.

    Returns None if the wait would exceed `_max_delay_s()` (caller should give up).
    """
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra:
        try:
            delay = float(ra)
            return delay if delay <= _max_delay_s() else None
        except ValueError:
            pass

    reset = (headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
             or headers.get("X-RateLimit-Reset-At"))
    if reset:
        try:
            delay = max(0.0, float(reset) - time.time())
            return delay if delay <= _max_delay_s() else None
        except ValueError:
            pass

    # Fallback jittered backoff.
    return random.uniform(0, min(8.0, 1.0 * (2 ** (attempt - 1))))


def _looks_like_rate_limit(status_code: int, body: str) -> bool:
    """Heuristic: is this a rate-limit 403, not a permissions/auth 403?"""
    if status_code != 403:
        return False
    lower = (body or "").lower()
    return "rate limit" in lower or "abuse detection" in lower or "secondary rate" in lower


def _should_retry_status(status_code: int, body: str) -> bool:
    # 429 and transient 5xx are always retryable.
    if status_code == 429 or status_code in (500, 502, 503, 504):
        return True
    # 403 requires body-content check — permissions 403 shouldn't retry.
    return _looks_like_rate_limit(status_code, body)


# ---------------------------------------------------------------------------
# `requests`-shaped interface.
# ---------------------------------------------------------------------------


def github_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    max_attempts: Optional[int] = None,
    **kwargs: Any,
):
    """`requests`-shape wrapper. Returns the final Response (retryable or not).

    Callers should check `response.status_code` after return — this function
    returns the last response on give-up (not raise), so callers can treat
    a give-up as a normal error path.
    """
    import requests  # local import: github-sync doesn't use `requests`

    attempts = max_attempts or _default_max_attempts()
    last_response = None
    for attempt in range(1, attempts + 1):
        response = requests.request(method, url, headers=headers, **kwargs)
        last_response = response
        if response.status_code < 400 or not _should_retry_status(
            response.status_code, response.text or ""
        ):
            return response
        if attempt >= attempts:
            return response
        delay = _retry_delay(dict(response.headers), attempt)
        if delay is None:
            logger.warning(
                "github_request: give up at attempt=%d status=%d "
                "(reset delay exceeds max %ds)",
                attempt, response.status_code, _max_delay_s(),
            )
            return response
        logger.info(
            "github_request retry: attempt=%d/%d status=%d delay=%.1fs url=%s",
            attempt, attempts, response.status_code, delay, url,
        )
        time.sleep(delay)
    return last_response


# ---------------------------------------------------------------------------
# `urllib.request`-shaped interface. Wraps a Request; on 403/429/5xx, retries.
# ---------------------------------------------------------------------------


def github_urllib_request(
    request: urllib.request.Request,
    timeout: float = 30.0,
    max_attempts: Optional[int] = None,
):
    """`urllib.request`-shape wrapper. Returns the raw response object on
    success (2xx). On give-up, re-raises the last HTTPError so callers see
    the same error shape they used to see.
    """
    attempts = max_attempts or _default_max_attempts()
    last_exc: Optional[urllib.error.HTTPError] = None
    for attempt in range(1, attempts + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            last_exc = e
            body = ""
            try:
                body = e.read().decode() if e.fp else ""
            except Exception:
                pass
            if not _should_retry_status(e.code, body) or attempt >= attempts:
                raise
            delay = _retry_delay(dict(e.headers or {}), attempt)
            if delay is None:
                logger.warning(
                    "github_urllib_request: give up at attempt=%d status=%d "
                    "(reset delay exceeds max %ds)",
                    attempt, e.code, _max_delay_s(),
                )
                raise
            logger.info(
                "github_urllib_request retry: attempt=%d/%d status=%d delay=%.1fs url=%s",
                attempt, attempts, e.code, delay, request.full_url,
            )
            time.sleep(delay)
    if last_exc:
        raise last_exc
