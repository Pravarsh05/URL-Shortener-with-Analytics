import pytest
from app.worker.consumer import parse_user_agent, resolve_country

def test_parse_desktop_ua():
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    device, browser = parse_user_agent(ua)
    assert device == "desktop"
    assert "Chrome" in browser

def test_parse_mobile_ua():
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    device, browser = parse_user_agent(ua)
    assert device == "mobile"
    assert "Safari" in browser or "Mobile Safari" in browser

def test_parse_bot_ua():
    ua = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    device, browser = parse_user_agent(ua)
    assert device == "bot"

def test_resolve_country():
    assert resolve_country("127.0.0.1") == "LO"
    assert resolve_country("8.8.8.8") == "US"
    assert resolve_country("49.205.10.1") == "IN"
    assert resolve_country("81.2.69.142") == "GB"
