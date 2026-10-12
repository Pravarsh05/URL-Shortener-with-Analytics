"""
Automated PostgreSQL partition maintenance for the clicks table.
Ensures partitions exist for the current month and N months ahead.
"""
from datetime import datetime, timezone
import asyncpg
import logging

logger = logging.getLogger(__name__)

async def ensure_partitions(pool, months_ahead: int = 3):
    """
    Creates monthly partition tables for clicks if they do not exist.
    """
    if getattr(pool, "db_path", None) is not None:
        logger.info("SQLite standalone pool detected - skipping PostgreSQL declarative partition creation.")
        return

    now = datetime.now(timezone.utc)
    current_year = now.year
    current_month = now.month

    for offset in range(months_ahead + 1):
        target_month = ((current_month - 1 + offset) % 12) + 1
        target_year = current_year + ((current_month - 1 + offset) // 12)

        # Start of target month
        start_date = f"{target_year:04d}-{target_month:02d}-01 00:00:00+00"

        # Start of next month
        next_m = 1 if target_month == 12 else target_month + 1
        next_y = target_year + 1 if target_month == 12 else target_year
        end_date = f"{next_y:04d}-{next_m:02d}-01 00:00:00+00"

        part_name = f"clicks_{target_year:04d}_{target_month:02d}"

        sql = f"""
        CREATE TABLE IF NOT EXISTS {part_name} PARTITION OF clicks
            FOR VALUES FROM ('{start_date}') TO ('{end_date}');
        """
        try:
            async with pool.acquire() as conn:
                await conn.execute(sql)
            logger.info("Ensured partition %s exists", part_name)
        except Exception as e:
            logger.warning("Partition creation notice for %s: %s", part_name, e)
