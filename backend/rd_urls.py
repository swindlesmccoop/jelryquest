"""Real Debrid URL helpers (panel vs CDN)."""

import os
import re
from urllib.parse import urlparse, urlunparse

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


# Public CDN hosts used by background scans (community-maintained; not exhaustive).
DEFAULT_CDN_HOSTS = [
    *[f"{n}.download.real-debrid.com" for n in range(20, 24)],
    *[f"{n}.download.real-debrid.com" for n in range(30, 35)],
    *[f"{n}.download.real-debrid.com" for n in range(40, 46)],
    *[f"{n}.download.real-debrid.com" for n in range(50, 70)],
    "rbx.download.real-debrid.com",
    "den1.download.real-debrid.com",
    "sea1.download.real-debrid.com",
    "nyk1.download.real-debrid.com",
    "chi1.download.real-debrid.com",
    "lax1.download.real-debrid.com",
    "mia1.download.real-debrid.com",
    "dal1.download.real-debrid.com",
    "qro1.download.real-debrid.com",
    "sao1.download.real-debrid.com",
    "scl1.download.real-debrid.com",
    "lon1.download.real-debrid.com",
    "hkg1.download.real-debrid.com",
    "sgp1.download.real-debrid.com",
    "tyo1.download.real-debrid.com",
    "mum1.download.real-debrid.com",
    "tlv1.download.real-debrid.com",
    "jnb1.download.real-debrid.com",
    "45.download.real-debrid.cloud",
]

SPEEDTEST_PATHS = ("/speedtest/test.rar", "/speedtest/testDefault.rar")

_RUNTIME_PREFERRED_CDN_RAW = ""


def normalize_cdn_host(host: str) -> str:
    """Accept ``nyk7-4`` or full ``nyk7-4.download.real-debrid.com``."""
    h = (host or "").strip().lower()
    if not h:
        return ""
    if "real-debrid" in h or h.endswith(".rdeb.io"):
        return h
    return f"{h}.download.real-debrid.com"


def set_runtime_preferred_cdn(raw: str) -> None:
    """Set preferred CDN host for this process (used by background scan)."""
    global _RUNTIME_PREFERRED_CDN_RAW
    _RUNTIME_PREFERRED_CDN_RAW = (raw or "").strip()


def preferred_cdn_hosts() -> list[str]:
    """Preferred CDN hosts (env pin first, then runtime override from scan)."""
    raw = (os.environ.get("REAL_DEBRID_PREFERRED_CDN") or "").strip()
    if not raw:
        raw = _RUNTIME_PREFERRED_CDN_RAW
    if not raw:
        return []
    parts = re.split(r"[,;\s]+", raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        norm = normalize_cdn_host(part)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def apply_preferred_cdn(url: str) -> str:
    """
    Rewrite the CDN hostname on an unrestricted link.

    Real-Debrid mirrors the same ``/d/<id>/file`` path on every CDN node.
    """
    raw = (url or "").strip()
    if not raw or not is_cdn_link(raw):
        return raw

    prefs = preferred_cdn_hosts()
    if not prefs:
        return raw

    parsed = urlparse(raw)
    orig = (parsed.hostname or "").lower()
    pref = prefs[0]
    if orig == pref:
        return raw

    netloc = pref
    if parsed.port:
        netloc = f"{pref}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))
