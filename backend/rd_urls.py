"""Real Debrid URL helpers (panel vs CDN)."""

import re
from urllib.parse import urlparse

_PANEL_LINK_RE = re.compile(
    r"^https?://(?:www\.)?real-debrid\.com/d/([A-Za-z0-9]+)/?(?:\?.*)?$",
    re.IGNORECASE,
)

_CDN_HOST_RE = re.compile(
    r"(?:^|\.)("
    r"download\.real-debrid\.(?:com|cloud)"
    r"|(?:f)?cdn\.real-debrid\.com"
    r"|rdeb\.io"
    r")$",
    re.IGNORECASE,
)


def is_panel_link(url: str) -> bool:
    return bool(_PANEL_LINK_RE.match((url or "").strip()))


def is_cdn_link(url: str) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    host = (urlparse(raw).hostname or "").lower()
    return bool(host and _CDN_HOST_RE.search(host))


def is_realdebrid_url(url: str | None) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    return is_panel_link(raw) or is_cdn_link(raw)


def needs_unrestrict(url: str) -> bool:
    return is_panel_link(url) or is_cdn_link(url)
