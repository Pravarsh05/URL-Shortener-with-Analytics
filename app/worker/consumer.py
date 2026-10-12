import asyncio
import logging
from datetime import datetime, timezone, date
from collections import defaultdict
from typing import List, Dict, Any, Tuple
import user_agents
import asyncpg
import redis.asyncio as aioredis

from app.config import settings
from app.security import hash_ip

logger = logging.getLogger(__name__)

def parse_user_agent(ua_string: str) -> Tuple[str, str]:
    """
    Parses User-Agent header into (device_category, browser_family).
    """
    if not ua_string:
        return "desktop", "Other"
    try:
        ua = user_agents.parse(ua_string)
        if ua.is_bot:
            device = "bot"
        elif ua.is_mobile:
            device = "mobile"
        elif ua.is_tablet:
            device = "tablet"
        else:
            device = "desktop"

        browser = ua.browser.family or "Other"
        return device, browser
    except Exception:
        return "desktop", "Other"

def resolve_country(ip: str) -> str:
    """
    Resolves two-letter ISO country code from IP address.
    Uses mock/offline ranges or GeoIP header when running behind reverse proxy/Cloudflare.
    """
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return "LO"  # Localhost / Private

    # Heuristic for test/demo addresses or Cloudflare/AWS header simulation
    # In production, can plug MaxMind GeoLite2 City/Country database (.mmdb)
    octets = ip.split(".")
    if len(octets) == 4:
        try:
            first = int(octets[0])
            if first in (8, 9, 13, 15, 23, 34, 52):
                return "US"
            elif first in (27, 49, 103, 106, 117):
                return "IN"
            elif first in (51, 80, 81, 82):
                return "GB"
            elif first in (31, 46, 62, 77, 85):
                return "DE"
            elif first in (1, 14, 27, 36, 42):
                return "JP"
            elif first in (101, 112, 113, 114):
                return "AU"
        except ValueError:
            pass
    return "US"

class ClickEventWorker:
    def __init__(self, db_pool: asyncpg.Pool, redis: aioredis.Redis):
        self.db_pool = db_pool
        self.redis = redis
        self.running = False

    async def init_group(self):
        """Ensures the stream consumer group exists."""
        try:
            await self.redis.xgroup_create(
                name=settings.REDIS_STREAM_KEY,
                groupname=settings.REDIS_CONSUMER_GROUP,
                id="$",
                mkstream=True,
            )
            logger.info("Created consumer group '%s'", settings.REDIS_CONSUMER_GROUP)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                pass
            else:
                logger.warning("Consumer group notice: %s", e)

    async def process_batch(self, messages: List[Tuple[str, Dict[str, str]]]):
        if not messages:
            return

        click_records = []
        msg_ids = []
        daily_clicks_map = defaultdict(int)  # (link_id, date) -> count
        daily_hll_keys = set()               # (link_id, date_str)

        for msg_id, data in messages:
            msg_ids.append(msg_id)
            link_id = int(data.get("link_id", 0))
            if link_id <= 0:
                continue

            ts_str = data.get("ts")
            try:
                ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(timezone.utc)
            except Exception:
                ts = datetime.now(timezone.utc)

            ip = data.get("ip", "")
            ua_str = data.get("ua", "")
            referrer = data.get("ref", "")[:500]

            ip_hashed = hash_ip(ip)
            device, browser = parse_user_agent(ua_str)
            country = resolve_country(ip)

            click_records.append((
                link_id,
                ts,
                ip_hashed,
                country,
                device,
                browser,
                referrer,
            ))

            event_date = ts.date()
            daily_clicks_map[(link_id, event_date)] += 1

            # HyperLogLog for unique visitors: visitor fingerprint = ip_hashed:browser
            visitor_id = f"{ip_hashed}:{device}:{browser}"
            date_str = event_date.isoformat()
            hll_key = f"hll:{link_id}:{date_str}"
            daily_hll_keys.add((link_id, event_date, hll_key))

            # Add to Redis HyperLogLog
            await self.redis.pfadd(hll_key, visitor_id)

        # 1. Bulk insert raw clicks into partitioned clicks table
        if click_records:
            insert_clicks_sql = """
            INSERT INTO clicks (link_id, ts, ip_hash, country, device, browser, referrer)
            VALUES ($1, $2, $3, $4, $5, $6, $7);
            """
            async with self.db_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(insert_clicks_sql, click_records)

        # 2. Update click_daily rollups with ON CONFLICT DO UPDATE
        rollup_sql = """
        INSERT INTO click_daily (link_id, day, clicks, unique_visitors)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (link_id, day)
        DO UPDATE SET
            clicks = click_daily.clicks + EXCLUDED.clicks,
            unique_visitors = GREATEST(click_daily.unique_visitors, EXCLUDED.unique_visitors);
        """
        rollup_params = []
        for (link_id, event_date, hll_key) in daily_hll_keys:
            clicks_count = daily_clicks_map[(link_id, event_date)]
            # Query HyperLogLog cardinality estimate for this link & day
            try:
                unique_count = await self.redis.pfcount(hll_key)
            except Exception:
                unique_count = clicks_count

            rollup_params.append((link_id, event_date, clicks_count, max(1, unique_count)))

        if rollup_params:
            async with self.db_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(rollup_sql, rollup_params)

        # 3. Acknowledge messages in stream
        if msg_ids:
            await self.redis.xack(settings.REDIS_STREAM_KEY, settings.REDIS_CONSUMER_GROUP, *msg_ids)
            # Trim processed messages from stream to maintain bound memory
            await self.redis.xdel(settings.REDIS_STREAM_KEY, *msg_ids)

        logger.info("Worker processed %d click events across %d rollups.", len(click_records), len(rollup_params))

    async def run(self):
        """Continuous stream reading loop."""
        self.running = True
        await self.init_group()
        logger.info(
            "ClickEventWorker started: group=%s consumer=%s batch_size=%d timeout=%dms",
            settings.REDIS_CONSUMER_GROUP,
            settings.REDIS_CONSUMER_NAME,
            settings.WORKER_BATCH_SIZE,
            settings.WORKER_BATCH_TIMEOUT_MS,
        )

        while self.running:
            try:
                # XREADGROUP GROUP <group> <consumer> COUNT <batch> BLOCK <timeout> STREAMS <key> >
                entries = await self.redis.xreadgroup(
                    groupname=settings.REDIS_CONSUMER_GROUP,
                    consumername=settings.REDIS_CONSUMER_NAME,
                    streams={settings.REDIS_STREAM_KEY: ">"},
                    count=settings.WORKER_BATCH_SIZE,
                    block=settings.WORKER_BATCH_TIMEOUT_MS,
                )

                if entries:
                    for stream_name, messages in entries:
                        if messages:
                            await self.process_batch(messages)
                else:
                    await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                logger.info("ClickEventWorker cancelled.")
                break
            except Exception as e:
                logger.error("Error in ClickEventWorker loop: %s", e, exc_info=True)
                await asyncio.sleep(2.0)

    def stop(self):
        self.running = False
