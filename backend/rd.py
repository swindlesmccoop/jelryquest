"""Real-Debrid: URL helpers, CDN host preference, and API client."""

import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from backend.config import Settings
from backend.models import ResolvedMagnet

logger = logging.getLogger(__name__)

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

POLL_INTERVAL_SEC = 2.0
POLL_TIMEOUT_SEC = 60.0
FAILED_STATUSES = ("error", "magnet_error", "virus", "dead")


def is_panel_link(url: str | None) -> bool:
    return bool(_PANEL_LINK_RE.match((url or "").strip()))


def is_cdn_link(url: str | None) -> bool:
    host = (urlparse((url or "").strip()).hostname or "").lower()
    return bool(host and _CDN_HOST_RE.search(host))


def is_realdebrid_url(url: str | None) -> bool:
    return is_panel_link(url) or is_cdn_link(url)


def build_magnet(info_hash: str) -> str:
    text = info_hash.strip()
    if text.lower().startswith("magnet:"):
        return text
    return f"magnet:?xt=urn:btih:{text.lower()}"


def normalize_cdn_host(host: str) -> str:
    """Accept ``nyk7-4`` or a full ``nyk7-4.download.real-debrid.com``."""
    h = (host or "").strip().lower()
    if not h:
        return ""
    if "real-debrid" in h or h.endswith(".rdeb.io"):
        return h
    return f"{h}.download.real-debrid.com"


def split_hosts(raw: str) -> list[str]:
    seen: dict[str, None] = {}
    for part in re.split(r"[,;\s]+", raw or ""):
        norm = normalize_cdn_host(part)
        if norm:
            seen.setdefault(norm)
    return list(seen)


class CdnPreference:
    """Rewrites the hostname on unrestricted links to the preferred CDN node.

    Real-Debrid mirrors the same ``/d/<id>/file`` path on every node. A pinned
    host (config) always wins over one chosen at runtime by the background scan.
    """

    def __init__(self, pinned: str = ""):
        self.pinned = split_hosts(pinned)
        self.runtime: str | None = None

    @property
    def active(self) -> str | None:
        if self.pinned:
            return self.pinned[0]
        return self.runtime

    def apply(self, url: str) -> str:
        pref = self.active
        if not pref or not is_cdn_link(url):
            return url
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() == pref:
            return url
        netloc = f"{pref}:{parsed.port}" if parsed.port else pref
        return urlunparse(parsed._replace(netloc=netloc))


def pick_link_and_filename(
    info: dict[str, Any], file_idx: int | None
) -> tuple[str, str | None]:
    """Choose the RD panel link (and file name) for a downloaded torrent.

    ``links`` only holds entries for files that were selected, in file order,
    so the link index is the file's position among the selected files.
    """
    links = [link.strip() for link in (info.get("links") or []) if link and link.strip()]
    if not links:
        raise ValueError("Real Debrid torrent has no links after download")

    files = info.get("files") or []
    selected = [f for f in files if f.get("selected")]
    link_index = 0
    chosen: dict[str, Any] | None = None

    if file_idx is not None and 0 <= file_idx < len(files):
        chosen = files[file_idx]
        if chosen in selected:
            link_index = selected.index(chosen)
    elif len(selected) == 1:
        chosen = selected[0]
    if link_index >= len(links):
        link_index = 0

    path = ((chosen or {}).get("path") or info.get("filename") or "").strip()
    filename = path.rsplit("/", 1)[-1] if path else None
    return links[link_index], filename


class RealDebridClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, cdn: CdnPreference):
        self.settings = settings
        self.client = client
        self.cdn = cdn
        self.token = settings.rd_token

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.token:
            raise ValueError("Real Debrid API token is not configured")
        s = self.settings
        response = await self.client.request(
            method,
            f"{s.real_debrid_api_base.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx.Timeout(
                s.real_debrid_read_timeout_sec,
                connect=s.real_debrid_connect_timeout_sec,
            ),
            **kwargs,
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def _torrent_info(self, torrent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/torrents/info/{torrent_id}")

    async def _wait(self, torrent_id: str, done, timeout: float, what: str) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = await self._torrent_info(torrent_id)
            if info.get("status") in FAILED_STATUSES:
                raise ValueError(f"Real Debrid torrent failed: {info.get('status')}")
            if done(info):
                return info
            await asyncio.sleep(POLL_INTERVAL_SEC)
        raise TimeoutError(f"Real Debrid torrent {what} not ready within timeout")

    @staticmethod
    def file_selection(files: list[dict[str, Any]], file_idx: int | None) -> str:
        if file_idx is None or not 0 <= file_idx < len(files):
            return "all"
        file_id = files[file_idx].get("id")
        return "all" if file_id is None else str(file_id)

    async def resolve_magnet(
        self, info_hash: str, file_idx: int | None = None
    ) -> ResolvedMagnet:
        """Add a magnet, wait for RD's cloud copy, return the panel link."""
        data = await self._request(
            "POST", "/torrents/addMagnet", data={"magnet": build_magnet(info_hash)}
        )
        torrent_id = (data or {}).get("id")
        if not torrent_id:
            raise ValueError("Real Debrid did not return a torrent id")

        info = await self._wait(
            torrent_id, lambda i: bool(i.get("files")), POLL_TIMEOUT_SEC, "files"
        )
        await self._request(
            "POST",
            f"/torrents/selectFiles/{torrent_id}",
            data={"files": self.file_selection(info["files"], file_idx)},
        )
        info = await self._wait(
            torrent_id, lambda i: i.get("status") == "downloaded", POLL_TIMEOUT_SEC, "download"
        )
        link, filename = pick_link_and_filename(info, file_idx)
        return ResolvedMagnet(rd_link=link, filename=filename)

    async def unrestrict(self, link: str) -> str:
        data = {"link": link}
        if self.settings.real_debrid_remote:
            data["remote"] = "1"
        payload = await self._request("POST", "/unrestrict/link", data=data)
        url = (payload or {}).get("download")
        if not url:
            raise ValueError("Real Debrid unrestrict did not return a download URL")
        return url

    async def resolve_download_url(self, link: str) -> str:
        """Turn a panel/CDN link into a fresh direct URL on the preferred CDN."""
        raw = (link or "").strip()
        if not raw:
            raise ValueError("Empty Real Debrid link")
        url = await self.unrestrict(raw) if is_realdebrid_url(raw) else raw
        rewritten = self.cdn.apply(url)
        if rewritten != url:
            logger.info(
                "Real-Debrid CDN host %s -> %s",
                httpx.URL(url).host,
                httpx.URL(rewritten).host,
            )
        return rewritten
