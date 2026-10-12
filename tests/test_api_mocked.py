import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone, timedelta
import asyncpg
from app.main import app

@pytest.mark.asyncio
async def test_health_check_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "database" in data
        assert "redis" in data

@pytest.mark.asyncio
async def test_create_link_validation_failure():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Invalid scheme
        res1 = await ac.post("/api/v1/links", json={"url": "ftp://bad-scheme.com"})
        assert res1.status_code == 400
        assert "scheme" in res1.json()["detail"].lower()

        # 2. SSRF attempt
        res2 = await ac.post("/api/v1/links", json={"url": "http://127.0.0.1/admin"})
        assert res2.status_code == 400
        assert "private" in res2.json()["detail"].lower() or "localhost" in res2.json()["detail"].lower() or "service domain" in res2.json()["detail"].lower()

        # 3. Reserved alias
        res3 = await ac.post("/api/v1/links", json={"url": "https://example.com", "alias": "admin"})
        assert res3.status_code == 400
        assert "reserved" in res3.json()["detail"].lower()

        # 4. Past expiry date
        past_date = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        res4 = await ac.post("/api/v1/links", json={
            "url": "https://example.com",
            "expires_at": past_date
        })
        assert res4.status_code == 400
        assert "future" in res4.json()["detail"].lower()

@pytest.mark.asyncio
async def test_create_link_and_alias_conflict():
    mock_pool = MagicMock()
    mock_conn = AsyncMock()

    # Mock custom alias insert raising UniqueViolationError on collision
    mock_conn.fetchrow.side_effect = asyncpg.exceptions.UniqueViolationError("Duplicate code")
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    with patch("app.routes.links.get_db_pool", return_value=mock_pool):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.post("/api/v1/links", json={
                "url": "https://example.com/target",
                "alias": "existing-code"
            })
            assert res.status_code == 409
            assert "already in use" in res.json()["detail"].lower()

@pytest.mark.asyncio
async def test_redirect_cache_hit_302():
    # Mock cached link
    fake_cached_link = {
        "id": 101,
        "code": "quick302",
        "long_url": "https://example.com/cached-destination",
        "is_active": True,
        "expires_at": None,
    }
    with patch("app.routes.redirect.get_cached_link", new=AsyncMock(return_value=fake_cached_link)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as ac:
            res = await ac.get("/quick302")
            assert res.status_code == 302
            assert res.headers["Location"] == "https://example.com/cached-destination"
            assert res.headers["X-Cache"] == "HIT"

@pytest.mark.asyncio
async def test_redirect_expired_returns_410():
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    expired_cached_link = {
        "id": 102,
        "code": "deadlink",
        "long_url": "https://example.com/expired",
        "is_active": True,
        "expires_at": past_iso,
    }
    with patch("app.routes.redirect.get_cached_link", new=AsyncMock(return_value=expired_cached_link)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as ac:
            res = await ac.get("/deadlink")
            assert res.status_code == 410

@pytest.mark.asyncio
async def test_redirect_unknown_negative_cache_404():
    # When cache indicates not_found
    with patch("app.routes.redirect.get_cached_link", new=AsyncMock(return_value={"status": "not_found"})):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as ac:
            res = await ac.get("/nonexistent")
            assert res.status_code == 404

@pytest.mark.asyncio
async def test_metrics_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/metrics")
        assert res.status_code == 200
        assert b"redirect_latency_seconds" in res.content
        assert b"http_requests_total" in res.content

@pytest.mark.asyncio
async def test_security_headers_present():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/health")
        assert res.status_code == 200
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["x-frame-options"] == "DENY"
        assert "content-security-policy" in res.headers
        assert "referrer-policy" in res.headers

@pytest.mark.asyncio
async def test_stats_date_range_validation():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Start date after end date
        res1 = await ac.get("/api/v1/links/testcode/stats?from=2026-05-10&to=2026-05-01")
        assert res1.status_code == 400
        assert "before or equal" in res1.json()["detail"].lower()

        # 2. Date span exceeding 365 days
        res2 = await ac.get("/api/v1/links/testcode/stats?from=2024-01-01&to=2026-01-01")
        assert res2.status_code == 400
        assert "exceeds maximum" in res2.json()["detail"].lower()

@pytest.mark.asyncio
async def test_delete_link_with_admin_api_key():
    from app.config import settings
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1}
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    transport = ASGITransport(app=app)
    with patch("app.routes.links.get_db_pool", return_value=mock_pool), \
         patch("app.routes.links.invalidate_link_cache", new=AsyncMock()), \
         patch("app.routes.links.set_negative_cache", new=AsyncMock()):

        # When ADMIN_API_KEY is configured
        with patch.object(settings, "ADMIN_API_KEY", "super-secret-key"):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                # 1. Missing key -> 401
                res_unauth = await ac.delete("/api/v1/links/mycode")
                assert res_unauth.status_code == 401

                # 2. Invalid key -> 401
                res_bad = await ac.delete("/api/v1/links/mycode", headers={"X-API-Key": "wrong-key"})
                assert res_bad.status_code == 401

                # 3. Valid key via X-API-Key -> 204
                res_ok = await ac.delete("/api/v1/links/mycode", headers={"X-API-Key": "super-secret-key"})
                assert res_ok.status_code == 204

                # 4. Valid key via Bearer token -> 204
                res_bearer = await ac.delete("/api/v1/links/mycode", headers={"Authorization": "Bearer super-secret-key"})
                assert res_bearer.status_code == 204
