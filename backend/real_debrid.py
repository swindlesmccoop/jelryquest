import asyncio
import logging
import re
import time
from typing import Any

import httpx

from backend.config import Settings
from backend.models import ResolvedLinks, StreamItem
from backend.rd_urls import is_realdebrid_url, needs_unrestrict

logger = logging.getLogger(__name__)

RD_API_BASE = "https://api.real-debrid.com/rest/1.0"
POLL_INTERVAL_SEC = 2.0
POLL_TIMEOUT_SEC = 60.0


def extract_rd_token_from_manifest(manifest_url: str) -> str | None:
    match = re.search(r"realdebrid=([^/&]+)", manifest_url, re.IGNORECASE)
    return match.group(1) if match else None


def build_magnet(info_hash: str) -> str:
    hash_clean = info_hash.strip().lower()
    if hash_clean.startswith("magnet:"):
        return info_hash.strip()
    return f"magnet:?xt=urn:btih:{hash_clean}"


def _pick_rd_link_and_filename(
    info: dict[str, Any],
    file_idx: int | None,
) -> tuple[str, str | None]:
    links = info.get("links") or []
    if not links:
        raise ValueError("Real Debrid torrent has no links after download")

    files = info.get("files") or []
    filename: str | None = None
    link_index = 0

    if file_idx is not None and 0 <= file_idx < len(links):
        link_index = file_idx
    if file_idx is not None and files and 0 <= file_idx < len(files):
        path = (files[file_idx].get("path") or "").strip()
        if path:
            filename = path.rsplit("/", 1)[-1]

    rd_link = (links[link_index] or "").strip()
    if not rd_link:
        rd_link = (links[0] or "").strip()
    if not filename and files:
        path = (files[link_index].get("path") or info.get("filename") or "").strip()
        if path:
            filename = path.rsplit("/", 1)[-1]

    return rd_link, filename


class RealDebridClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self._token = settings.resolved_rd_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        url = f"{RD_API_BASE}{path}"
        response = await self.client.request(
            method,
            url,
            headers=self._headers(),
            **kwargs,
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def add_magnet(self, magnet: str) -> str:
        data = await self._request(
            "POST",
            "/torrents/addMagnet",
            data={"magnet": magnet},
        )
        torrent_id = data.get("id")
        if not torrent_id:
            raise ValueError("Real Debrid did not return a torrent id")
        return torrent_id

    async def select_files(self, torrent_id: str, files_param: str) -> None:
        await self._request(
            "POST",
            f"/torrents/selectFiles/{torrent_id}",
            data={"files": files_param},
        )

    def _file_selection(self, files: list[dict[str, Any]], file_idx: int | None) -> str:
        if not files:
            return "all"
        if file_idx is None:
            return "all"
        if file_idx < 0 or file_idx >= len(files):
            return "all"
        file_id = files[file_idx].get("id")
        return str(file_id) if file_id is not None else "all"

    async def wait_for_files(
        self,
        torrent_id: str,
        timeout: float = POLL_TIMEOUT_SEC,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = await self.get_torrent_info(torrent_id)
            status = info.get("status")
            if status in ("error", "magnet_error", "virus", "dead"):
                raise ValueError(f"Real Debrid torrent failed: {status}")
            files = info.get("files") or []
            if files:
                return info
            await asyncio.sleep(POLL_INTERVAL_SEC)
        raise TimeoutError("Real Debrid torrent files not available within timeout")

    async def get_torrent_info(self, torrent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/torrents/info/{torrent_id}")

    async def delete_torrent(self, torrent_id: str) -> None:
        try:
            await self._request("DELETE", f"/torrents/delete/{torrent_id}")
        except httpx.HTTPError:
            logger.warning("Failed to delete RD torrent %s", torrent_id)

    async def unrestrict_link(self, link: str) -> str:
        data = await self._request(
            "POST",
            "/unrestrict/link",
            data={"link": link},
        )
        download_url = data.get("download")
        if not download_url:
            raise ValueError("Real Debrid unrestrict did not return a download URL")
        return download_url

    async def resolve_download_url(self, link: str) -> str:
        """Resolve panel or CDN RD URL to a fresh DDL on current egress IP."""
        raw = (link or "").strip()
        if not raw:
            raise ValueError("Empty Real Debrid link")
        if needs_unrestrict(raw):
            return await self.unrestrict_link(raw)
        return raw

    async def wait_for_downloaded(
        self,
        torrent_id: str,
        timeout: float = POLL_TIMEOUT_SEC,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = await self.get_torrent_info(torrent_id)
            status = info.get("status")
            if status == "downloaded":
                return info
            if status in ("error", "magnet_error", "virus", "dead"):
                raise ValueError(f"Real Debrid torrent failed: {status}")
            await asyncio.sleep(POLL_INTERVAL_SEC)
        raise TimeoutError("Real Debrid torrent did not finish within timeout")

    async def resolve_magnet_to_rd_link(
        self,
        info_hash: str,
        file_idx: int | None = None,
        *,
        delete_after: bool = False,
    ) -> ResolvedLinks:
        """Add magnet on RD, wait for RD cloud copy, return panel link (not CDN DDL)."""
        magnet = build_magnet(info_hash)
        torrent_id = await self.add_magnet(magnet)
        try:
            info = await self.wait_for_files(torrent_id)
            files = info.get("files") or []
            selection = self._file_selection(files, file_idx)
            await self.select_files(torrent_id, selection)
            info = await self.wait_for_downloaded(torrent_id)
            rd_link, filename = _pick_rd_link_and_filename(info, file_idx)
            return ResolvedLinks(
                magnet=magnet,
                rd_link=rd_link,
                ddl=None,
                filename=filename,
            )
        finally:
            if delete_after:
                await self.delete_torrent(torrent_id)

    async def resolve_magnet_to_ddl(
        self,
        info_hash: str,
        file_idx: int | None = None,
    ) -> ResolvedLinks:
        resolved = await self.resolve_magnet_to_rd_link(info_hash, file_idx)
        ddl = await self.unrestrict_link(resolved.rd_link)
        return resolved.model_copy(update={"ddl": ddl})

    async def enrich_stream(self, stream: StreamItem) -> StreamItem:
        magnet = build_magnet(stream.info_hash) if stream.info_hash else None
        rd_link: str | None = None
        if stream.url and is_realdebrid_url(stream.url):
            rd_link = stream.url.strip()

        return stream.model_copy(
            update={
                "magnet": magnet,
                "rd_link": rd_link,
                "ddl": None,
            }
        )

    async def enrich_streams(self, streams: list[StreamItem]) -> list[StreamItem]:
        tasks = [self.enrich_stream(s) for s in streams]
        return await asyncio.gather(*tasks)
