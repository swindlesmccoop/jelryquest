import asyncio
import logging
import re
import time
from typing import Any

import httpx

from backend.config import Settings
from backend.models import ResolvedLinks, StreamItem

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

    async def resolve_magnet_to_ddl(
        self,
        info_hash: str,
        file_idx: int | None = None,
    ) -> ResolvedLinks:
        magnet = build_magnet(info_hash)
        torrent_id = await self.add_magnet(magnet)
        try:
            info = await self.wait_for_files(torrent_id)
            files = info.get("files") or []
            selection = self._file_selection(files, file_idx)
            await self.select_files(torrent_id, selection)
            info = await self.wait_for_downloaded(torrent_id)
            links = info.get("links") or []
            if not links:
                raise ValueError("Real Debrid torrent has no links after download")

            if selection == "all" and file_idx is not None and file_idx < len(links):
                rd_link = links[file_idx]
            else:
                rd_link = links[0]
            ddl = await self.unrestrict_link(rd_link)
            return ResolvedLinks(magnet=magnet, rd_link=rd_link, ddl=ddl)
        finally:
            await self.delete_torrent(torrent_id)

    async def enrich_stream(self, stream: StreamItem) -> StreamItem:
        info_hash = stream.info_hash
        magnet = build_magnet(info_hash) if info_hash else None

        if stream.url:
            return stream.model_copy(
                update={
                    "magnet": magnet,
                    "ddl": stream.url,
                }
            )

        if not info_hash:
            return stream.model_copy(update={"magnet": magnet})

        try:
            resolved = await self.resolve_magnet_to_ddl(info_hash, stream.file_idx)
            return stream.model_copy(
                update={
                    "magnet": resolved.magnet,
                    "rd_link": resolved.rd_link,
                    "ddl": resolved.ddl,
                }
            )
        except Exception as exc:
            logger.warning("RD resolve failed for stream %s: %s", stream.index, exc)
            return stream.model_copy(
                update={
                    "magnet": magnet,
                    "resolve_error": str(exc),
                }
            )

    async def enrich_streams(self, streams: list[StreamItem]) -> list[StreamItem]:
        tasks = [self.enrich_stream(s) for s in streams]
        return await asyncio.gather(*tasks)
