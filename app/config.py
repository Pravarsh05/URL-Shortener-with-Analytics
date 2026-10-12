from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional

class Settings(BaseSettings):
    # App
    APP_NAME: str = "PulseLink URL Shortener"
    BASE_URL: str = "http://localhost:8000"
    DEBUG: bool = False
    SECRET_SALT: str = "url-shortener-secure-salt-2026"

    # Database
    DATABASE_URL: str = Field(
        default="postgresql://postgres:postgrespassword@localhost:5432/urlshortener",
        description="Asyncpg compatible PostgreSQL connection URL"
    )
    DB_POOL_MIN_SIZE: int = 5
    DB_POOL_MAX_SIZE: int = 20

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_STREAM_KEY: str = "clicks"
    REDIS_CONSUMER_GROUP: str = "click_workers"
    REDIS_CONSUMER_NAME: str = "worker-1"

    # Cache TTLs (in seconds)
    DEFAULT_CACHE_TTL: int = 86400       # 24 hours
    NEGATIVE_CACHE_TTL: int = 60         # 60 seconds
    STAMPEDE_LOCK_TTL: int = 5           # 5 seconds lock

    # Rate Limiting
    RATE_LIMIT_CREATE_PER_MIN: int = 20
    RATE_LIMIT_REDIRECT_PER_MIN: int = 300

    # Worker Settings
    WORKER_BATCH_SIZE: int = 500
    WORKER_BATCH_TIMEOUT_MS: int = 2000

    # Security & Protection
    ADMIN_API_KEY: Optional[str] = None
    TRUSTED_PROXIES: list[str] = ["127.0.0.1", "::1", "localhost", "testclient"]
    TRUST_ALL_PROXIES: bool = False
    CORS_ORIGINS: list[str] = ["*"]
    MAX_STATS_RANGE_DAYS: int = 365

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
