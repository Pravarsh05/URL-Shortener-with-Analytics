import time
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
from pathlib import Path
from prometheus_client import (
    Counter,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

from app.config import settings
from app.database import init_db, close_db, get_db_pool
from app.redis_client import init_redis, close_redis, get_redis
from migrations.partition_manager import ensure_partitions
from app.routes import links, stats, qr, redirect

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("api_main")

# Prometheus Metrics
REDIRECT_LATENCY_HISTOGRAM = Histogram(
    "redirect_latency_seconds",
    "Redirect request latency in seconds",
    ["cache_status"],
    buckets=[0.001, 0.002, 0.005, 0.010, 0.020, 0.050, 0.100, 0.250, 0.500],
)
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests handled",
    ["method", "endpoint", "status"],
)
CACHE_OPERATIONS = Counter(
    "redirect_cache_operations_total",
    "Redirect cache hits vs misses",
    ["status"],
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing %s...", settings.APP_NAME)
    pool = await init_db()
    try:
        await ensure_partitions(pool)
    except Exception as e:
        logger.warning("Could not auto-create upcoming partitions: %s", e)
    await init_redis()
    logger.info("Application startup complete.")

    yield

    # Shutdown
    logger.info("Shutting down application...")
    await close_redis()
    await close_db()
    logger.info("Application shutdown complete.")

app = FastAPI(
    title=settings.APP_NAME,
    description="High-performance URL Shortener with real-time analytics, Redis caching, and stream ingestion.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
is_wildcard_cors = "*" in settings.CORS_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=not is_wildcard_cors,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Security Hardening Headers Middleware
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self';"
    )
    return response

# Metrics & Latency Middleware
@app.middleware("http")
async def metrics_and_timing_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    response: Response = await call_next(request)
    duration = time.perf_counter() - start_time

    # Record metrics for redirect path
    path = request.url.path
    if request.method == "GET" and not path.startswith(("/api", "/health", "/metrics", "/docs", "/static")):
        cache_status = response.headers.get("X-Cache", "MISS")
        REDIRECT_LATENCY_HISTOGRAM.labels(cache_status=cache_status).observe(duration)
        CACHE_OPERATIONS.labels(status=cache_status.lower()).inc()

    response.headers["X-Response-Time"] = f"{duration * 1000:.2f}ms"
    return response

# Prometheus Endpoint
@app.get("/metrics", tags=["Monitoring"])
async def metrics():
    """Prometheus exposition endpoint for scraping."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

# Health Check Endpoint
@app.get("/health", tags=["Monitoring"])
async def health_check():
    """Health status check verifying both PostgreSQL and Redis connectivity."""
    db_ok = False
    redis_ok = False

    try:
        pool = get_db_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1;")
            db_ok = (val == 1)
    except Exception:
        db_ok = False

    try:
        redis_client = get_redis()
        if redis_client:
            redis_ok = await redis_client.ping()
    except Exception:
        redis_ok = False

    status_str = "healthy" if (db_ok and redis_ok) else "degraded"
    return {
        "status": status_str,
        "database": "connected" if db_ok else "disconnected",
        "redis": "connected" if redis_ok else "disconnected",
    }

# Mount API Routers
app.include_router(links.router)
app.include_router(stats.router)
app.include_router(qr.router)

# Mount Static Files & Web Dashboard
static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", tags=["Dashboard"])
    async def dashboard_home():
        return FileResponse(static_dir / "index.html")

# Mount Redirect Router LAST so /:code doesn't shadow /api, /health, /metrics, etc.
app.include_router(redirect.router)
