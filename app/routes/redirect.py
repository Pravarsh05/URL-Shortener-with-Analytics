import asyncio
from datetime import datetime, timezone
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
import time

from app.config import settings
from app.database import get_db_pool
from app.rate_limiter import RateLimiter
from app.redis_client import (
    get_cached_link,
    set_cached_link,
    set_negative_cache,
    acquire_stampede_lock,
    release_stampede_lock,
    enqueue_click_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Redirect"])

redirect_limiter = RateLimiter(
    requests_per_minute=settings.RATE_LIMIT_REDIRECT_PER_MIN,
    endpoint_name="redirect",
    fail_open=True,  # Fail open for high availability
)

@router.get("/{code}")
async def redirect_short_url(
    code: str,
    request: Request,
    _: None = Depends(redirect_limiter),
):
    """
    High-performance 302 redirect handler (<50ms p99 cache hit).
    - Cache-aside via Redis
    - Negative caching on nonexistent or expired links
    - Distributed stampede lock on cache miss
    - Asynchronous fire-and-forget click ingestion to Redis Streams
    """
    # Exclude reserved paths or static/favicon
    if code in ("favicon.ico", "robots.txt", "health", "metrics", "docs", "redoc", "api"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    now_utc = datetime.now(timezone.utc)
    link_id: Optional[int] = None
    target_url: Optional[str] = None
    expires_at_dt: Optional[datetime] = None
    cache_hit = False

    # 1. Check Redis Cache
    cached = await get_cached_link(code)
    if cached is not None:
        if cached.get("status") == "not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found.")
        if cached.get("status") == "expired":
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="Link expired or inactive.")

        # Cache hit
        cache_hit = True
        link_id = cached["id"]
        target_url = cached["long_url"]
        exp_str = cached.get("expires_at")
        if exp_str:
            expires_at_dt = datetime.fromisoformat(exp_str)

        # Lazy check expiry on cache hit
        if expires_at_dt and expires_at_dt <= now_utc:
            await set_negative_cache(code, is_expired=True, ttl=settings.NEGATIVE_CACHE_TTL)
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="Link expired.")

        if not cached.get("is_active", True):
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="Link is disabled.")

    # 2. Cache Miss -> Database Lookup with Stampede Protection
    if target_url is None:
        # Acquire stampede lock to prevent dogpiling Postgres
        got_lock = await acquire_stampede_lock(code, ttl=settings.STAMPEDE_LOCK_TTL)
        if not got_lock:
            # Another request is populating the cache; wait briefly and retry cache
            await asyncio.sleep(0.05)
            retry_cached = await get_cached_link(code)
            if retry_cached and retry_cached.get("long_url"):
                target_url = retry_cached["long_url"]
                link_id = retry_cached["id"]

        if target_url is None:
            try:
                pool = get_db_pool()
                query = """
                SELECT id, code, long_url, created_at, expires_at, is_active
                FROM links
                WHERE code = $1;
                """
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(query, code)

                if not row:
                    # Negative cache unknown code
                    await set_negative_cache(code, is_expired=False, ttl=settings.NEGATIVE_CACHE_TTL)
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found.")

                link_id = row["id"]
                target_url = row["long_url"]
                expires_at_dt = row["expires_at"]
                is_active = row["is_active"]

                # Expiry check
                if expires_at_dt and expires_at_dt <= now_utc:
                    await set_negative_cache(code, is_expired=True, ttl=settings.NEGATIVE_CACHE_TTL)
                    raise HTTPException(status_code=status.HTTP_410_GONE, detail="Link expired.")

                if not is_active:
                    await set_negative_cache(code, is_expired=True, ttl=settings.NEGATIVE_CACHE_TTL)
                    raise HTTPException(status_code=status.HTTP_410_GONE, detail="Link disabled.")

                # Populate Cache with TTL = min(time until expiry, 24h)
                ttl = settings.DEFAULT_CACHE_TTL
                if expires_at_dt:
                    sec_left = int((expires_at_dt - now_utc).total_seconds())
                    ttl = max(1, min(sec_left, ttl))

                await set_cached_link(
                    code,
                    {
                        "id": link_id,
                        "code": code,
                        "long_url": target_url,
                        "is_active": is_active,
                        "expires_at": expires_at_dt.isoformat() if expires_at_dt else None,
                    },
                    ttl=ttl,
                )
            finally:
                if got_lock:
                    await release_stampede_lock(code)

    # 3. Fire-and-forget enqueue click event to Redis Streams
    if link_id is not None:
        client_ip = request.headers.get("X-Forwarded-For")
        if client_ip:
            client_ip = client_ip.split(",")[0].strip()
        elif request.client:
            client_ip = request.client.host
        else:
            client_ip = "127.0.0.1"

        user_agent = request.headers.get("user-agent", "")
        referrer = request.headers.get("referer", "")
        timestamp_iso = now_utc.isoformat()

        # Non-blocking background task enqueue
        asyncio.create_task(
            enqueue_click_event(
                link_id=link_id,
                code=code,
                ip=client_ip,
                user_agent=user_agent,
                referrer=referrer,
                ts=timestamp_iso,
            )
        )

    # 4. HTTP 302 Found redirect
    # Important: 302 (Found / Temporary Redirect) prevents browser-side permanent caching
    response = RedirectResponse(url=target_url, status_code=status.HTTP_302_FOUND)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["X-Cache"] = "HIT" if cache_hit else "MISS"
    return response
