import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, unquote

import httpx

from backend.config import Settings
from backend.models import DownloadJob, DownloadState, StreamItem
from backend.rd_urls import is_realdebrid_url

logger = logging.getLogger(__name__)

RD_ERROR_HINT = (
    "Real Debrid rejected the download link (403/429). Common causes: another device "
    "is using your RD account from a different IP, the link expired, or this server's "
    "egress IP does not match the IP used when the link was created. Check active "
    "downloads at real-debrid.com/downloads — RD allows many jobs from one IP, not "
    "one job per account."
)


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:200] or "download"


def filename_from_url(url: str) -> str | None:
    name = Path(urlparse(url).path).name
    if not name or name in ("/", ".", ".."):
        return None
    return unquote(name)


def resolve_dest_filename(
    filename_hint: str | None,
    url: str,
    content_type: str | None,
    fallback: str,
) -> str:
    for raw in (filename_hint, filename_from_url(url)):
        if not raw or not raw.strip():
            continue
        cleaned = sanitize_filename(raw.strip())
        if not cleaned:
            continue
        if "." in cleaned:
            return cleaned
        return f"{cleaned}{guess_extension(url, content_type)}"
    base = sanitize_filename(fallback)
    return f"{base}{guess_extension(url, content_type)}"


def guess_extension(url: str, content_type: str | None) -> str:
    path = urlparse(url).path
    if "." in path.split("/")[-1]:
        ext = Path(path).suffix
        if ext and len(ext) <= 8:
            return ext
    if content_type:
        if "mp4" in content_type:
            return ".mp4"
        if "mkv" in content_type or "matroska" in content_type:
            return ".mkv"
        if "x-matroska" in content_type:
            return ".mkv"
    return ".mkv"


class DownloadManager:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.jobs: dict[str, DownloadJob] = {}
        self._lock = asyncio.Lock()
        self._active = 0

    def list_jobs(self) -> list[DownloadJob]:
        return sorted(self.jobs.values(), key=lambda j: j.id, reverse=True)

    def get_job(self, job_id: str) -> DownloadJob | None:
        return self.jobs.get(job_id)

    async def enqueue(
        self,
        imdb_id: str,
        stream: StreamItem,
        title: str | None = None,
        rd_client: Any | None = None,
        content_type: Literal["movie", "series"] = "movie",
    ) -> DownloadJob:
        direct_url, rd_link = self._resolve_enqueue_urls(stream)
        if not direct_url and not rd_link and not stream.info_hash:
            raise ValueError(
                "Selected stream has no direct download URL or Real Debrid link. "
                "Torrentio may need RD in manifest or magnet resolution failed."
            )

        job_id = str(uuid.uuid4())[:8]
        display_title = (
            stream.title or stream.name or title or stream.filename or imdb_id
        )
        filename_hint = stream.filename
        if not filename_hint and stream.title and "\n" in stream.title:
            first_line = stream.title.split("\n", 1)[0].strip()
            if first_line:
                filename_hint = first_line
        download_dir = self.settings.download_path_for(content_type)
        job = DownloadJob(
            id=job_id,
            imdb_id=imdb_id,
            stream_index=stream.index,
            title=display_title,
            state=DownloadState.queued,
        )
        self.jobs[job_id] = job
        asyncio.create_task(
            self._run(
                job,
                direct_url,
                display_title,
                download_dir,
                rd_link,
                rd_client,
                stream,
                filename_hint,
            )
        )
        return job

    def _resolve_enqueue_urls(self, stream: StreamItem) -> tuple[str | None, str | None]:
        rd_link = stream.rd_link
        if stream.url and is_realdebrid_url(stream.url):
            rd_link = stream.url.strip()

        direct_url: str | None = None
        if stream.ddl and not is_realdebrid_url(stream.ddl):
            direct_url = stream.ddl
        elif stream.url and not is_realdebrid_url(stream.url):
            direct_url = stream.url.strip()

        return direct_url, rd_link

    async def enqueue_direct(
        self,
        direct_url: str | None,
        title: str,
        content_type: Literal["movie", "series"] = "movie",
        rd_link: str | None = None,
        rd_client: Any | None = None,
        filename_hint: str | None = None,
    ) -> DownloadJob:
        if not direct_url and not rd_link:
            raise ValueError("No download URL or Real Debrid link")
        job_id = str(uuid.uuid4())[:8]
        download_dir = self.settings.download_path_for(content_type)
        job = DownloadJob(
            id=job_id,
            imdb_id="paste",
            stream_index=-1,
            title=title,
            state=DownloadState.queued,
        )
        self.jobs[job_id] = job
        asyncio.create_task(
            self._run(
                job,
                direct_url,
                title,
                download_dir,
                rd_link,
                rd_client,
                None,
                filename_hint or title,
            )
        )
        return job

    async def _run(
        self,
        job: DownloadJob,
        direct_url: str | None,
        title: str,
        download_dir: str,
        rd_link: str | None = None,
        rd_client: Any | None = None,
        stream: StreamItem | None = None,
        filename_hint: str | None = None,
    ) -> None:
        async with self._lock:
            while self._active >= self.settings.max_active_downloads:
                await asyncio.sleep(1)
            self._active += 1

        job.state = DownloadState.downloading
        Path(download_dir).mkdir(parents=True, exist_ok=True)

        active_rd_link = rd_link
        dest_filename_hint = filename_hint
        try:
            url = direct_url
            if not active_rd_link and stream and stream.info_hash and rd_client:
                resolved = await rd_client.resolve_magnet_to_rd_link(
                    stream.info_hash,
                    stream.file_idx,
                )
                active_rd_link = resolved.rd_link
                if resolved.filename:
                    dest_filename_hint = resolved.filename
            if active_rd_link and rd_client:
                url = await rd_client.resolve_download_url(active_rd_link)
            if not url:
                raise ValueError("No download URL or Real Debrid link")
            await self._download_file(
                job, url, download_dir, dest_filename_hint, title
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 429) and active_rd_link and rd_client:
                try:
                    new_url = await rd_client.resolve_download_url(active_rd_link)
                    await self._download_file(
                        job, new_url, download_dir, dest_filename_hint, title
                    )
                    return
                except Exception:
                    pass
            job.state = DownloadState.error
            detail = str(exc) if str(exc) else RD_ERROR_HINT
            if exc.response.status_code in (403, 429):
                detail = RD_ERROR_HINT
            job.error = detail
            logger.exception("Download failed for job %s", job.id)
        except Exception as exc:
            job.state = DownloadState.error
            job.error = str(exc) if str(exc) else RD_ERROR_HINT
            logger.exception("Download failed for job %s", job.id)
        finally:
            async with self._lock:
                self._active -= 1

    async def _download_file(
        self,
        job: DownloadJob,
        url: str,
        download_dir: str,
        filename_hint: str | None,
        fallback_title: str,
    ) -> None:
        async with self.client.stream("GET", url, follow_redirects=True) as response:
            if response.status_code in (403, 429):
                raise httpx.HTTPStatusError(
                    RD_ERROR_HINT,
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            mime_type = response.headers.get("content-type")
            final_url = str(response.url)
            filename = resolve_dest_filename(
                filename_hint,
                final_url,
                mime_type,
                fallback_title,
            )
            dest_dir = Path(download_dir)
            dest = dest_dir / filename
            if dest.exists():
                stem = dest.stem
                suffix = dest.suffix
                dest = dest_dir / f"{stem}_{job.id}{suffix}"
            job.filename = dest.name

            total = response.headers.get("content-length")
            if total and total.isdigit():
                job.total_bytes = int(total)

            last_tick = time.monotonic()
            last_bytes = 0
            with dest.open("wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)
                    job.bytes_received += len(chunk)
                    now = time.monotonic()
                    if now - last_tick >= 1.0:
                        job.speed_bps = (job.bytes_received - last_bytes) / (
                            now - last_tick
                        )
                        last_bytes = job.bytes_received
                        last_tick = now

        job.state = DownloadState.done
        job.speed_bps = None
        logger.info("Download complete: %s -> %s/%s", job.id, download_dir, job.filename)
