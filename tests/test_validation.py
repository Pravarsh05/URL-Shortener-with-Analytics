import pytest
from app.security import (
    validate_url,
    validate_custom_alias,
    hash_ip,
    ValidationError,
)

def test_valid_urls():
    valid = [
        "https://example.com",
        "http://github.com/torvalds/linux",
        "https://sub.domain.org/path/to/resource?param=1&foo=bar#section",
        "https://en.wikipedia.org/wiki/URL_shortening",
    ]
    for url in valid:
        assert validate_url(url, check_dns=False) == url

def test_invalid_url_schemes():
    invalid_schemes = [
        "ftp://example.com/file.txt",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,hello",
        "gopher://example.com",
    ]
    for url in invalid_schemes:
        with pytest.raises(ValidationError):
            validate_url(url, check_dns=False)

def test_ssrf_private_and_loopback_ips():
    dangerous_urls = [
        "http://localhost/admin",
        "http://127.0.0.1:8080/secret",
        "http://10.0.0.1/internal-api",
        "http://172.16.0.5/dashboard",
        "http://192.168.1.1/router",
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata endpoint
        "http://[::1]/status",
    ]
    for url in dangerous_urls:
        with pytest.raises(ValidationError) as exc:
            validate_url(url, check_dns=False)
        msg = exc.value.message.lower()
        assert any(term in msg for term in ("private", "localhost", "service domain"))

def test_custom_alias_valid():
    valid_aliases = ["promo2026", "my_link", "cool-deal", "Link_1", "ABC-123_xyz"]
    for alias in valid_aliases:
        validate_custom_alias(alias)  # Should not raise

def test_custom_alias_invalid():
    invalid_aliases = [
        "ab",                   # Too short (< 3 chars)
        "this_alias_is_way_too_long_for_limit", # Too long (> 16 chars)
        "has spaces",           # Spaces not allowed
        "bad@symbol!",          # Special characters not allowed
        "slash/path",           # Slashes not allowed
    ]
    for alias in invalid_aliases:
        with pytest.raises(ValidationError):
            validate_custom_alias(alias)

def test_reserved_aliases_rejected():
    reserved = ["api", "admin", "health", "metrics", "docs", "redoc", "static", "dashboard"]
    for r in reserved:
        with pytest.raises(ValidationError) as exc:
            validate_custom_alias(r)
        assert "reserved" in exc.value.message.lower()

def test_ip_hashing():
    ip1 = "203.0.113.195"
    h1 = hash_ip(ip1)
    assert len(h1) == 64
    assert h1 == hash_ip(ip1)  # Deterministic with same salt

    ip2 = "198.51.100.22"
    h2 = hash_ip(ip2)
    assert h1 != h2

def test_blocked_chained_shorteners():
    chained = [
        "https://bit.ly/3xyz",
        "http://tinyurl.com/abc1234",
        "https://t.co/shortlink",
        "https://is.gd/deal",
    ]
    for url in chained:
        with pytest.raises(ValidationError) as exc:
            validate_url(url, check_dns=False)
        assert "chaining" in exc.value.message.lower()

def test_blocked_dangerous_file_extensions():
    malware_urls = [
        "https://example.com/payload.exe",
        "https://example.com/installer.msi",
        "https://example.com/script.sh",
        "https://example.com/worm.bat",
    ]
    for url in malware_urls:
        with pytest.raises(ValidationError) as exc:
            validate_url(url, check_dns=False)
        assert "executable" in exc.value.message.lower()

def test_blocked_embedded_credentials():
    phishing_urls = [
        "https://user:pass@legit-looking.com/login",
        "http://admin:secret@bank.com",
    ]
    for url in phishing_urls:
        with pytest.raises(ValidationError) as exc:
            validate_url(url, check_dns=False)
        assert "credentials" in exc.value.message.lower()
