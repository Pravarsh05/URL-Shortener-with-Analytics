from datetime import datetime, date, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, status

from app.config import settings
from app.database import get_db_pool
from app.schemas import (
    LinkStatsResponse,
    LinkStatsTimelineItem,
    DeviceStats,
    BrowserStats,
    CountryStats,
    ReferrerStats,
)

router = APIRouter(prefix="/api/v1/links", tags=["Analytics"])

@router.get("/{code}/stats", response_model=LinkStatsResponse)
async def get_link_stats(
    code: str,
    from_date: Optional[date] = Query(None, alias="from", description="Start date (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, alias="to", description="End date (YYYY-MM-DD)"),
    granularity: str = Query("day", pattern="^(day|hour)$"),
):
    """
    Retrieves aggregated link analytics:
    - Click timeline from click_daily rollups
    - Dimensional breakdowns (devices, browsers, countries, referrers) from clicks
    - Total clicks & unique visitor estimates
    """
    # Date range validation
    today = datetime.now(timezone.utc).date()
    start_day = from_date or (today - timedelta(days=29))
    end_day = to_date or today

    if start_day > end_day:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Start date must be before or equal to end date.",
        )

    if (end_day - start_day).days > settings.MAX_STATS_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Requested date range exceeds maximum allowed limit of {settings.MAX_STATS_RANGE_DAYS} days.",
        )

    pool = get_db_pool()

    # 1. Fetch link metadata
    link_query = """
    SELECT id, code, long_url, created_at, expires_at, is_active
    FROM links
    WHERE code = $1;
    """
    async with pool.acquire() as conn:
        link = await conn.fetchrow(link_query, code)

    if not link:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found.")

    link_id = link["id"]

    async with pool.acquire() as conn:
        # 2. Timeline from rollups (click_daily)
        timeline_query = """
        SELECT day, clicks, unique_visitors
        FROM click_daily
        WHERE link_id = $1 AND day BETWEEN $2 AND $3
        ORDER BY day ASC;
        """
        timeline_rows = await conn.fetch(timeline_query, link_id, start_day, end_day)

        # 3. Overall Totals
        totals_query = """
        SELECT COALESCE(SUM(clicks), 0) AS total_clicks,
               COALESCE(SUM(unique_visitors), 0) AS total_unique_visitors
        FROM click_daily
        WHERE link_id = $1;
        """
        totals_row = await conn.fetchrow(totals_query, link_id)
        total_clicks = totals_row["total_clicks"] if totals_row else 0
        total_uniques = totals_row["total_unique_visitors"] if totals_row else 0

        # Fallback check against raw clicks if rollups are still catching up
        if total_clicks == 0:
            raw_count = await conn.fetchval("SELECT COUNT(*) FROM clicks WHERE link_id = $1;", link_id)
            total_clicks = raw_count or 0
            if total_clicks > 0:
                raw_uniques = await conn.fetchval(
                    "SELECT COUNT(DISTINCT ip_hash) FROM clicks WHERE link_id = $1;", link_id
                )
                total_uniques = raw_uniques or 1

        # 4. Device breakdown
        device_query = """
        SELECT device, COUNT(*) as count
        FROM clicks
        WHERE link_id = $1
        GROUP BY device
        ORDER BY count DESC
        LIMIT 10;
        """
        device_rows = await conn.fetch(device_query, link_id)

        # 5. Browser breakdown
        browser_query = """
        SELECT browser, COUNT(*) as count
        FROM clicks
        WHERE link_id = $1
        GROUP BY browser
        ORDER BY count DESC
        LIMIT 10;
        """
        browser_rows = await conn.fetch(browser_query, link_id)

        # 6. Country breakdown
        country_query = """
        SELECT country, COUNT(*) as count
        FROM clicks
        WHERE link_id = $1
        GROUP BY country
        ORDER BY count DESC
        LIMIT 10;
        """
        country_rows = await conn.fetch(country_query, link_id)

        # 7. Referrer breakdown
        referrer_query = """
        SELECT COALESCE(NULLIF(referrer, ''), 'Direct / None') AS referrer, COUNT(*) as count
        FROM clicks
        WHERE link_id = $1
        GROUP BY 1
        ORDER BY count DESC
        LIMIT 10;
        """
        referrer_rows = await conn.fetch(referrer_query, link_id)

    # Convert timeline records to mapping and fill missing days
    timeline_dict = {r["day"]: (r["clicks"], r["unique_visitors"]) for r in timeline_rows}
    timeline_items = []
    curr = start_day
    while curr <= end_day:
        c, u = timeline_dict.get(curr, (0, 0))
        timeline_items.append(LinkStatsTimelineItem(date=curr, clicks=c, unique_visitors=u))
        curr += timedelta(days=1)

    return LinkStatsResponse(
        code=link["code"],
        long_url=link["long_url"],
        total_clicks=total_clicks,
        total_unique_visitors=total_uniques,
        created_at=link["created_at"],
        is_active=link["is_active"],
        expires_at=link["expires_at"],
        timeline=timeline_items,
        devices=[DeviceStats(device=r["device"], count=r["count"]) for r in device_rows],
        browsers=[BrowserStats(browser=r["browser"], count=r["count"]) for r in browser_rows],
        countries=[CountryStats(country=r["country"], count=r["count"]) for r in country_rows],
        referrers=[ReferrerStats(referrer=r["referrer"], count=r["count"]) for r in referrer_rows],
    )
