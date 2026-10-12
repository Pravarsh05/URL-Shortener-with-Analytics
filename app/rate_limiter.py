import time
import uuid
import logging
from typing import Tuple
from fastapi import Request, HTTPException, status
from app.config import settings
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

# Sliding window rate limiter Lua script
# KEYS[1] = ratelimit key
# ARGV[1] = current time in milliseconds
# ARGV[2] = window size in milliseconds
# ARGV[3] = max allowed requests in window
# ARGV[4] = unique member id
SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

local clear_before = now - window
redis.call('ZREMRANGEBYSCORE', key, 0, clear_before)

local current_requests = redis.call('ZCARD', key)
if current_requests < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window)
    return {1, limit - current_requests - 1, 0}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = 1
    if #oldest > 0 then
        local oldest_time = tonumber(oldest[2])
        retry_after = math.ceil((oldest_time + window - now) / 1000)
        if retry_after < 1 then retry_after = 1 end
    end
    return {0, 0, retry_after}
end
"""

class RateLimiter:
    def __init__(self, requests_per_minute: int, endpoint_name: str, fail_open: bool = True):
        self.requests_per_minute = requests_per_minute
        self.window_ms = 60 * 1000
        self.endpoint_name = endpoint_name
        self.fail_open = fail_open

    def get_client_ip(self, request: Request) -> str:
        """
        Extracts client IP securely.
        Only trusts X-Forwarded-For if the direct peer is in settings.TRUSTED_PROXIES
        or settings.TRUST_ALL_PROXIES is enabled.
        """
        direct_ip = request.client.host if request.client and request.client.host else "127.0.0.1"
        is_trusted = settings.TRUST_ALL_PROXIES or (direct_ip in settings.TRUSTED_PROXIES)

        forwarded = request.headers.get("X-Forwarded-For")
        if is_trusted and forwarded:
            candidate = forwarded.split(",")[0].strip()
            if candidate:
                return candidate
        return direct_ip

    async def check(self, request: Request) -> Tuple[bool, int, int]:
        """
        Executes sliding window rate limit check.
        Returns: (is_allowed, remaining_quota, retry_after_seconds)
        """
        client_ip = self.get_client_ip(request)
        key = f"ratelimit:{self.endpoint_name}:{client_ip}"
        redis = get_redis()

        if redis is None:
            # If Redis is down, honor fail-open policy
            return True, self.requests_per_minute, 0

        try:
            now_ms = int(time.time() * 1000)
            member = f"{now_ms}:{uuid.uuid4().hex[:8]}"

            result = await redis.eval(
                SLIDING_WINDOW_LUA,
                1,
                key,
                now_ms,
                self.window_ms,
                self.requests_per_minute,
                member,
            )

            is_allowed = bool(result[0])
            remaining = int(result[1])
            retry_after = int(result[2])
            return is_allowed, remaining, retry_after

        except Exception as e:
            logger.error("Rate limiter evaluation failed for %s: %s", key, e)
            if self.fail_open:
                return True, self.requests_per_minute, 0
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Rate limiter temporarily unavailable.",
            )

    async def __call__(self, request: Request):
        """Dependency invocation for FastAPI route decorators."""
        allowed, remaining, retry_after = await self.check(request)
        if not allowed:
            headers = {
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(self.requests_per_minute),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(retry_after),
            }
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Try again in {retry_after} seconds.",
                headers=headers,
            )
        # Store in request state so response headers can include them
        request.state.ratelimit_limit = self.requests_per_minute
        request.state.ratelimit_remaining = remaining
