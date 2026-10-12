from datetime import datetime, date
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, HttpUrl

class LinkCreateRequest(BaseModel):
    url: str = Field(..., description="The target long URL (http or https)")
    alias: Optional[str] = Field(None, min_length=3, max_length=16, description="Optional custom alias (3-16 chars, [a-zA-Z0-9_-])")
    expires_at: Optional[datetime] = Field(None, description="Optional expiration timestamp in ISO 8601 format (UTC)")

class LinkResponse(BaseModel):
    code: str
    short_url: str
    long_url: str
    is_custom: bool
    created_at: datetime
    expires_at: Optional[datetime]
    is_active: bool

class LinkStatsTimelineItem(BaseModel):
    date: date
    clicks: int
    unique_visitors: int

class DeviceStats(BaseModel):
    device: str
    count: int

class BrowserStats(BaseModel):
    browser: str
    count: int

class CountryStats(BaseModel):
    country: str
    count: int

class ReferrerStats(BaseModel):
    referrer: str
    count: int

class LinkStatsResponse(BaseModel):
    code: str
    long_url: str
    total_clicks: int
    total_unique_visitors: int
    created_at: datetime
    is_active: bool
    expires_at: Optional[datetime]
    timeline: List[LinkStatsTimelineItem]
    devices: List[DeviceStats]
    browsers: List[BrowserStats]
    countries: List[CountryStats]
    referrers: List[ReferrerStats]
