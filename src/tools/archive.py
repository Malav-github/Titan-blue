"""Content-addressed evidence archive: every fetched byte is stored under its SHA-256."""

import hashlib
import ipaddress
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

from config import DATA_DIR, TITAN_CONTACT

ARCHIVE_DIR = DATA_DIR / "archive"
USER_AGENT = f"TitanResearchBot/0.1 ({TITAN_CONTACT})" if TITAN_CONTACT else "TitanResearchBot/0.1"


def archive_bytes(data: bytes, suffix: str) -> tuple[str, str]:
    """Store bytes under their hash; existing files are never overwritten."""
    digest = hashlib.sha256(data).hexdigest()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"{digest}{suffix}"
    if not path.exists():
        path.write_bytes(data)
    return digest, str(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_public_url(url: str) -> None:
    """Allow only http(s) URLs that resolve to public addresses (blocks localhost, LAN, cloud metadata)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Only http(s) URLs are allowed: {url}")
    for info in socket.getaddrinfo(parsed.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"Refusing non-public address {ip} for host {parsed.hostname}")


def clip(text: str, max_chars: int) -> tuple[str, bool]:
    return (text[:max_chars], True) if len(text) > max_chars else (text, False)
