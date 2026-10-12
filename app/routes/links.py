from datetime import datetime, timezone
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
import asyncpg

from app.config import settings
from app.database import get_db_pool
from app.key_generator import generate_code_from_id
from app.rate_limiter import RateLimiter
from app.redis_client import invalidate_link_cache, set_negative_cache, get_cached_link, set_cached_link
from app.schemas import LinkCreateRequest, LinkResponse
from app.security import validate_url, validate_custom_alias, ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/links", tags=["Links"])

create_limiter = RateLimiter(
    requests_per_minute=settings.RATE_LIMIT_CREATE_PER_MIN,
    endpoint_name="create_link",
    fail_open=False,  # strict for writes
)

@router.post("", response_model=LinkResponse, status_code=status.HTTP_201_CREATED)
async def create_link(
    payload: LinkCreateRequest,
    request: Request,
    response: Response,
    _: None = Depends(create_limiter),
):
    """
    Shorten a URL with optional custom alias and expiration date.
    Uses PostgreSQL unique index as the source of truth for collision prevention.
    """
    # 1. URL validation (SSRF, scheme, length)
    try:
        clean_url = validate_url(payload.url)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)

    # 2. Expiry validation
    now_utc = datetime.now(timezone.utc)
    if payload.expires_at:
        exp = payload.expires_at if payload.expires_at.tzinfo is not None else payload.expires_at.replace(tzinfo=timezone.utc)
        if exp <= now_utc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Expiration date must be in the future.",
            )
        payload.expires_at = exp

    # 3. Custom Alias validation
    if payload.alias:
        try:
            validate_custom_alias(payload.alias)
        except ValidationError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)

    pool = get_db_pool()

    # 4. Custom Alias Insertion Flow
    if payload.alias:
        insert_sql = """
        INSERT INTO links (code, long_url, is_custom, created_at, expires_at, is_active)
        VALUES ($1, $2, TRUE, now(), $3, TRUE)
        RETURNING id, code, long_url, is_custom, created_at, expires_at, is_active;
        """
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(insert_sql, payload.alias, clean_url, payload.expires_at)
        except asyncpg.exceptions.UniqueViolationError:
            # Handle conflict error atomically (no race conditions)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"The alias '{payload.alias}' is already in use. Please choose another.",
            )

    # 4. Auto-Generated Base62 + Scrambled Code Flow
    else:
        max_retries = 3
        row = None
        for attempt in range(max_retries):
            try:
                async with pool.acquire() as conn:
                    # Allocate ID from sequence
                    seq_id = await conn.fetchval("SELECT nextval('links_id_seq');")
                    code = generate_code_from_id(seq_id)

                    insert_sql = """
                    INSERT INTO links (id, code, long_url, is_custom, created_at, expires_at, is_active)
                    VALUES ($1, $2, $3, FALSE, now(), $4, TRUE)
                    RETURNING id, code, long_url, is_custom, created_at, expires_at, is_active;
                    """
                    row = await conn.fetchrow(insert_sql, seq_id, code, clean_url, payload.expires_at)
                    break
            except asyncpg.exceptions.UniqueViolationError:
                logger.warning("Collision encountered on auto-code '%s', retrying...", code)
                if attempt == max_retries - 1:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Could not generate unique short code. Please try again.",
                    )

    short_url = f"{settings.BASE_URL.rstrip('/')}/{row['code']}"

    # Pre-warm cache for the new link
    ttl = settings.DEFAULT_CACHE_TTL
    if row["expires_at"]:
        sec_left = int((row["expires_at"] - now_utc).total_seconds())
        ttl = max(1, min(sec_left, ttl))

    await set_cached_link(
        row["code"],
        {
            "id": row["id"],
            "code": row["code"],
            "long_url": row["long_url"],
            "is_active": row["is_active"],
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        },
        ttl=ttl,
    )

    return LinkResponse(
        code=row["code"],
        short_url=short_url,
        long_url=row["long_url"],
        is_custom=row["is_custom"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        is_active=row["is_active"],
    )

@router.get("/{code}", response_model=LinkResponse)
async def get_link_metadata(code: str):
    """Retrieves link metadata without triggering a redirect event."""
    pool = get_db_pool()
    query = """
    SELECT id, code, long_url, is_custom, created_at, expires_at, is_active
    FROM links
    WHERE code = $1;
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, code)

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found.")

    now_utc = datetime.now(timezone.utc)
    if not row["is_active"] or (row["expires_at"] and row["expires_at"] <= now_utc):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This link has been disabled or expired.",
        )

    short_url = f"{settings.BASE_URL.rstrip('/')}/{row['code']}"
    return LinkResponse(
        code=row["code"],
        short_url=short_url,
        long_url=row["long_url"],
        is_custom=row["is_custom"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        is_active=row["is_active"],
    )

@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_link(code: str, request: Request):
    """
    Deactivates/disables a link and invalidates its Redis cache entry.
    Requires X-API-Key or Bearer Authorization if ADMIN_API_KEY is configured.
    """
    if settings.ADMIN_API_KEY:
        api_key = request.headers.get("X-API-Key")
        auth_header = request.headers.get("Authorization")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
        provided_key = api_key or token

        if not provided_key or provided_key != settings.ADMIN_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized. Valid admin API key required.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    pool = get_db_pool()
    update_sql = """
    UPDATE links
    SET is_active = FALSE
    WHERE code = $1
    RETURNING id;
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(update_sql, code)

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found.")

    # Cache invalidation & negative cache as expired
    await invalidate_link_cache(code)
    await set_negative_cache(code, is_expired=True, ttl=86400)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
