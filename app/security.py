import hashlib
import ipaddress
import re
import socket
from urllib.parse import urlparse
from typing import Optional
from app.config import settings

RESERVED_ALIASES = {
    "api",
    "admin",
    "health",
    "metrics",
    "docs",
    "redoc",
    "openapi.json",
    "favicon.ico",
    "static",
    "dashboard",
    "stats",
    "qr",
    "auth",
    "login",
    "logout",
    "register",
    "user",
    "links",
    "null",
    "undefined",
}

ALIAS_REGEX = re.compile(r"^[a-zA-Z0-9_-]{3,16}$")
MAX_URL_LENGTH = 2048

# Chained shorteners blocked to prevent redirect obfuscation / phishing loops
BLOCKED_SHORTENERS = {
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "ow.ly",
    "is.gd",
    "buff.ly",
    "adf.ly",
    "bitly.com",
    "cutt.ly",
    "rebrand.ly",
}

# Dangerous executable or script file extensions
DANGEROUS_EXTENSIONS = (
    ".exe",
    ".bat",
    ".cmd",
    ".sh",
    ".vbs",
    ".msi",
    ".scr",
    ".pif",
    ".dll",
    ".ps1",
    ".com",
)

class ValidationError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code

def validate_custom_alias(alias: str) -> None:
    """
    Validates custom alias format and reserved keywords.
    Rules: 3-16 characters, [a-zA-Z0-9_-], case-sensitive, no reserved names.
    """
    if not alias:
        return
    if alias.lower() in RESERVED_ALIASES:
        raise ValidationError(f"The alias '{alias}' is reserved and cannot be used.")
    if not ALIAS_REGEX.match(alias):
        raise ValidationError(
            "Custom alias must be 3-16 characters long and contain only letters, digits, underscores, or hyphens."
        )

def validate_url(url: str, check_dns: bool = True) -> str:
    """
    Validates URL safety and format.
    - Validates http / https scheme
    - Length check (<= 2048 chars)
    - Rejects embedded user credentials (user:pass@host)
    - Blocks chained URL shorteners (prevents phishing obfuscation)
    - Blocks direct downloads of executable payloads
    - SSRF prevention: blocks private IPs, loopback, link-local, and self-references.
    """
    if not url or len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"URL is required and must not exceed {MAX_URL_LENGTH} characters.")

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValidationError("URL scheme must be http or https.")

    hostname = parsed.hostname
    if not hostname:
        raise ValidationError("URL must include a valid host.")

    # Block embedded authentication credentials (e.g. http://victim.com:fake@attacker.com)
    if parsed.username or parsed.password:
        raise ValidationError("URLs containing embedded username or password credentials are not permitted.")

    # Block chained URL shorteners to prevent open redirect loops and phishing nesting
    host_clean = hostname.lower().lstrip("www.")
    if host_clean in BLOCKED_SHORTENERS:
        raise ValidationError(f"Chaining shortened URLs from '{host_clean}' is not permitted.")

    # Block dangerous executable files
    path_lower = parsed.path.lower()
    for ext in DANGEROUS_EXTENSIONS:
        if path_lower.endswith(ext):
            raise ValidationError(f"URLs pointing directly to executable files ({ext}) are prohibited.")

    # Block self-referencing to our own domain
    base_host = urlparse(settings.BASE_URL).hostname
    if base_host and hostname.lower() == base_host.lower():
        raise ValidationError("Cannot create short URLs linking back to this service domain.")

    # Direct IP string check
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValidationError("Target URL points to a private or restricted network address.")
        return url
    except ValueError:
        # Not a direct IP literal; continue to DNS check
        pass

    # Check common local domain names
    if hostname.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "local"):
        raise ValidationError("Target URL points to localhost or a loopback address.")

    if check_dns:
        try:
            # Resolve hostname to verify destination IP isn't private (SSRF protection)
            addr_info = socket.getaddrinfo(hostname, None)
            for item in addr_info:
                ip_str = item[4][0]
                ip = ipaddress.ip_address(ip_str)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                    raise ValidationError("Target domain resolves to a private or restricted IP address.")
        except socket.gaierror:
            # Domain resolution failed, but we still allow valid-format external domains if offline
            pass

    return url

def hash_ip(ip: Optional[str]) -> str:
    """
    Hashes client IP address using SHA-256 with salt for GDPR/privacy compliance.
    """
    if not ip:
        return "0" * 64
    salted = f"{ip}:{settings.SECRET_SALT}".encode("utf-8")
    return hashlib.sha256(salted).hexdigest()
