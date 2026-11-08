"""
SENTRIX — Rate Limiter (token bucket, in-memory)
=================================================
Protects the LLM-backed /api/chat endpoint (an open LLM proxy in v4.0) and the
ingest endpoint from abuse. Per-client-IP token bucket; stdlib only.
"""
from __future__ import annotations

import threading
import time

from fastapi import HTTPException, Request

from agent import config

_buckets: dict = {}
_lock = threading.Lock()


def _allow(key: str, rate_per_min: int, burst: int) -> bool:
    now = time.time()
    with _lock:
        tokens, last = _buckets.get(key, (float(burst), now))
        tokens = min(burst, tokens + (now - last) * (rate_per_min / 60.0))
        if tokens < 1.0:
            _buckets[key] = (tokens, now)
            return False
        _buckets[key] = (tokens - 1.0, now)
        return True


def limiter(scope: str, rate_per_min: int = None, burst: int = None):
    """FastAPI dependency factory: Depends(limiter('chat'))."""
    rate = rate_per_min or config.RATE_LIMIT_PER_MIN
    b = burst or max(3, rate // 2)

    async def _dep(request: Request):
        ip = (request.client.host if request.client else "unknown")
        if not _allow(f"{scope}:{ip}", rate, b):
            raise HTTPException(status_code=429, detail=f"rate limit exceeded for {scope}; try again shortly")
        return True
    return _dep
