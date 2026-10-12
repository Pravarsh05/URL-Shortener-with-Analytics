import json
import logging
from typing import Optional, Dict, Any, Union
import redis.asyncio as aioredis
from app.config import settings
from app.in_memory_redis import InMemoryRedis

logger = logging.getLogger(__name__)

redis_pool: Optional[Union[aioredis.Redis, InMemoryRedis]] = None

NEGATIVE_CACHE_VALUE = "__NOT_FOUND__"
EXPIRED_CACHE_VALUE = "__EXPIRED__"

async def init_redis() -> Union[aioredis.Redis, InMemoryRedis]:
    """
    Initialize Redis connection pool.
    Falls back gracefully to InMemoryRedis if Redis is not running.
    """
    global redis_pool
    if redis_pool is None:
        try:
            logger.info("Attempting connection to Redis at %s", settings.REDIS_URL)
            client = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=1.5,
                socket_connect_timeout=1.5,
            )
            # Test connectivity
            await client.ping()
            redis_pool = client

            # Create consumer group if it doesn't already exist
            try:
                await redis_pool.xgroup_create(
                    name=settings.REDIS_STREAM_KEY,
                    groupname=settings.REDIS_CONSUMER_GROUP,
                    id="$",
                    mkstream=True,
                )
                logger.info("Initialized Redis Stream group '%s'", settings.REDIS_CONSUMER_GROUP)
            except aioredis.ResponseError as e:
                if "BUSYGROUP" in str(e):
                    pass  # Group already exists
                else:
                    logger.warning("Consumer group creation warning: %s", e)
            except Exception as e:
                logger.warning("Could not pre-create Redis stream group: %s", e)

        except Exception as e:
            logger.warning("Redis unavailable (%s). Falling back to In-Memory cache & queue...", e)
            redis_pool = InMemoryRedis()

    return redis_pool

async def close_redis():
    """Close Redis connection pool."""
    global redis_pool
    if redis_pool is not None:
        await redis_pool.aclose()
        redis_pool = None
        logger.info("Redis connection closed.")

def get_redis() -> Optional[Union[aioredis.Redis, InMemoryRedis]]:
    """Returns active Redis client or InMemoryRedis if uninitialized."""
    global redis_pool
    if redis_pool is None:
        redis_pool = InMemoryRedis()
    return redis_pool

async def get_cached_link(code: str) -> Optional[Dict[str, Any]]:
    """Fetches cached link data from Redis."""
    if redis_pool is None:
        return None
    try:
        val = await redis_pool.get(f"link:{code}")
        if val is None:
            return None
        if val == NEGATIVE_CACHE_VALUE:
            return {"status": "not_found"}
        if val == EXPIRED_CACHE_VALUE:
            return {"status": "expired"}
        return json.loads(val)
    except Exception as e:
        logger.error("Redis error fetching cache for %s: %s", code, e)
        return None

async def set_cached_link(code: str, data: Dict[str, Any], ttl: int = settings.DEFAULT_CACHE_TTL):
    """Caches link data in Redis with a calculated TTL."""
    if redis_pool is None:
        return
    try:
        ttl = max(1, min(ttl, settings.DEFAULT_CACHE_TTL))
        await redis_pool.set(f"link:{code}", json.dumps(data), ex=ttl)
    except Exception as e:
        logger.error("Redis error setting cache for %s: %s", code, e)

async def set_negative_cache(code: str, is_expired: bool = False, ttl: int = settings.NEGATIVE_CACHE_TTL):
    """Caches negative lookup (404/410) to defend Postgres against cache stampedes/attacks."""
    if redis_pool is None:
        return
    try:
        val = EXPIRED_CACHE_VALUE if is_expired else NEGATIVE_CACHE_VALUE
        await redis_pool.set(f"link:{code}", val, ex=ttl)
    except Exception as e:
        logger.error("Redis error setting negative cache for %s: %s", code, e)

async def invalidate_link_cache(code: str):
    """Deletes cached link on delete or update."""
    if redis_pool is None:
        return
    try:
        await redis_pool.delete(f"link:{code}")
    except Exception as e:
        logger.error("Redis error invalidating cache for %s: %s", code, e)

async def acquire_stampede_lock(code: str, ttl: int = settings.STAMPEDE_LOCK_TTL) -> bool:
    """
    Acquires a distributed lock using SETNX to prevent dogpiling/stampede
    on cache miss for hot links.
    """
    if redis_pool is None:
        return True
    try:
        acquired = await redis_pool.set(f"lock:link:{code}", "1", nx=True, ex=ttl)
        return bool(acquired)
    except Exception as e:
        logger.error("Redis lock error for %s: %s", code, e)
        return True  # Fail open to allow request to proceed

async def release_stampede_lock(code: str):
    """Releases distributed lock."""
    if redis_pool is None:
        return
    try:
        await redis_pool.delete(f"lock:link:{code}")
    except Exception:
        pass

async def _record_offline_telemetry(link_id: int, ip: str, user_agent: str, referrer: str, ts_str: str):
    from datetime import datetime, timezone
    from app.worker.consumer import parse_user_agent, resolve_country
    from app.security import hash_ip
    from app.database import get_db_pool

    try:
        pool = get_db_pool()
        ip_hashed = hash_ip(ip)
        device, browser = parse_user_agent(user_agent)
        country = resolve_country(ip)
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            ts = datetime.now(timezone.utc)
        event_date = ts.date().isoformat()

        insert_sql = """
        INSERT INTO clicks (link_id, ts, ip_hash, country, device, browser, referrer)
        VALUES ($1, $2, $3, $4, $5, $6, $7);
        """
        rollup_sql = """
        INSERT INTO click_daily (link_id, day, clicks, unique_visitors)
        VALUES ($1, $2, 1, 1)
        ON CONFLICT (link_id, day)
        DO UPDATE SET
            clicks = click_daily.clicks + 1,
            unique_visitors = click_daily.unique_visitors + 1;
        """
        async with pool.acquire() as conn:
            await conn.execute(insert_sql, link_id, ts, ip_hashed, country, device, browser, referrer[:500] if referrer else "")
            await conn.execute(rollup_sql, link_id, event_date)
    except Exception as e:
        logger.warning("Could not persist offline telemetry: %s", e)

async def enqueue_click_event(
    link_id: int,
    code: str,
    ip: str,
    user_agent: str,
    referrer: str,
    ts: str
) -> bool:
    """
    Fire-and-forget append to Redis Stream.
    In standalone mode, also persists telemetry directly to local database.
    Never blocks or fails the HTTP redirect request.
    """
    if redis_pool is None:
        return False
    try:
        event = {
            "link_id": str(link_id),
            "code": code,
            "ip": ip or "",
            "ua": user_agent or "",
            "ref": referrer or "",
            "ts": ts,
        }
        await redis_pool.xadd(settings.REDIS_STREAM_KEY, event)

        # In standalone/InMemoryRedis mode, record directly to DB in background
        if isinstance(redis_pool, InMemoryRedis):
            import asyncio
            asyncio.create_task(_record_offline_telemetry(link_id, ip, user_agent, referrer, ts))

        return True
    except Exception as e:
        logger.error("Failed to enqueue click event to Redis Stream for link %s: %s", link_id, e)
        return False
