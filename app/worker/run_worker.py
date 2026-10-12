"""
Standalone entry point to run the Click Event Worker as a separate process.
Usage:
    python -m app.worker.run_worker
"""
import asyncio
import logging
import signal
import sys
from app.database import init_db, close_db
from app.redis_client import init_redis, close_redis
from app.worker.consumer import ClickEventWorker
from migrations.partition_manager import ensure_partitions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("worker_main")

async def main():
    logger.info("Starting Click Analytics Worker process...")
    db_pool = await init_db()
    redis_conn = await init_redis()

    # Ensure monthly partitions exist
    await ensure_partitions(db_pool)

    worker = ClickEventWorker(db_pool, redis_conn)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received.")
        worker.stop()
        stop_event.set()

    # On Windows, signals like SIGTERM may behave differently, so catch KeyboardInterrupt gracefully
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                pass  # Windows event loop fallback
    except Exception:
        pass

    worker_task = asyncio.create_task(worker.run())

    try:
        await worker_task
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Worker shutting down...")
    finally:
        worker.stop()
        await close_redis()
        await close_db()
        logger.info("Worker cleanly stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
