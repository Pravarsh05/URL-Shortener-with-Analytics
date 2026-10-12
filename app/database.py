import asyncpg
import logging
from typing import Optional, Union
from pathlib import Path
from app.config import settings
from app.sqlite_db import SQLitePool

logger = logging.getLogger(__name__)

db_pool: Optional[Union[asyncpg.Pool, SQLitePool]] = None

async def init_db() -> Union[asyncpg.Pool, SQLitePool]:
    """
    Initialize database connection pool.
    Attempts PostgreSQL first; if unreachable or configured for SQLite,
    gracefully falls back to a high-performance local SQLite database.
    """
    global db_pool
    if db_pool is None:
        # Check if explicitly configured for SQLite
        if settings.DATABASE_URL.startswith("sqlite"):
            logger.info("Initializing Standalone SQLite database at local_pulselink.db...")
            sqlite_p = SQLitePool("local_pulselink.db")
            sqlite_p.init_schema()
            db_pool = sqlite_p
            return db_pool

        try:
            logger.info("Connecting to PostgreSQL at %s", settings.DATABASE_URL)
            db_pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_URL,
                min_size=settings.DB_POOL_MIN_SIZE,
                max_size=settings.DB_POOL_MAX_SIZE,
                command_timeout=2.0,
            )

            # Run initial schema migration
            migration_file = Path(__file__).resolve().parent.parent / "migrations" / "001_initial_schema.sql"
            if migration_file.exists():
                schema_sql = migration_file.read_text(encoding="utf-8")
                async with db_pool.acquire() as conn:
                    await conn.execute(schema_sql)
                logger.info("Database schema migration verified/applied.")
        except Exception as e:
            logger.warning("PostgreSQL unavailable (%s). Falling back to local standalone SQLite database...", e)
            sqlite_p = SQLitePool("local_pulselink.db")
            sqlite_p.init_schema()
            db_pool = sqlite_p

    return db_pool

async def close_db():
    """Close database connection pool gracefully."""
    global db_pool
    if db_pool is not None:
        await db_pool.close()
        db_pool = None
        logger.info("Database pool closed.")

def get_db_pool() -> Union[asyncpg.Pool, SQLitePool]:
    """Returns the current database pool, lazily creating a SQLitePool if uninitialized."""
    global db_pool
    if db_pool is None:
        sqlite_p = SQLitePool("local_pulselink.db")
        sqlite_p.init_schema()
        db_pool = sqlite_p
    return db_pool

