"""
Background job to soft-delete expired links in batches using partial index idx_links_expiry.
"""
import asyncio
import logging
from datetime import datetime, timezone
import asyncpg
from app.redis_client import invalidate_link_cache, set_negative_cache

logger = logging.getLogger(__name__)

async def cleanup_expired_links(pool: asyncpg.Pool, batch_size: int = 1000):
    """
    Finds active links where expires_at < now(), marks them is_active = FALSE,
    and updates Redis cache.
    """
    now_utc = datetime.now(timezone.utc)
    query = """
    WITH expired_batch AS (
        SELECT id, code
        FROM links
        WHERE expires_at IS NOT NULL
          AND expires_at <= $1
          AND is_active = TRUE
        LIMIT $2
    )
    UPDATE links l
    SET is_active = FALSE
    FROM expired_batch eb
    WHERE l.id = eb.id
    RETURNING eb.code;
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, now_utc, batch_size)

        if rows:
            logger.info("Deactivated %d expired links in background cleaner.", len(rows))
            for r in rows:
                code = r["code"]
                await invalidate_link_cache(code)
                await set_negative_cache(code, is_expired=True, ttl=86400)
    except Exception as e:
        logger.error("Error during expired link cleanup: %s", e)
