import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from backend.config import Settings
from backend.models import DownloadJob, DownloadState, StreamItem

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
        download_url = stream.ddl or stream.url
        if not download_url:
            raise ValueError(
                "Selected stream has no direct download URL. "
                "Torrentio may need RD in manifest or magnet resolution failed."
            )

        job_id = str(uuid.uuid4())[:8]
        display_title = title or stream.title or stream.name or imdb_id
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
                download_url,
                display_title,
                download_dir,
                stream.rd_link,
                rd_client,
            )
        )
        return job

    async def enqueue_direct(
        self,
        download_url: str,
        title: str,
        content_type: Literal["movie", "series"] = "movie",
        rd_link: str | None = None,
        rd_client: Any | None = None,
    ) -> DownloadJob:
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
                download_url,
                title,
                download_dir,
                rd_link,
                rd_client,
            )
        )
        return job

    async def _run(
        self,
        job: DownloadJob,
        url: str,
        title: str,
        download_dir: str,
        rd_link: str | None = None,
        rd_client: Any | None = None,
    ) -> None:
        async with self._lock:
            while self._active >= self.settings.max_active_downloads:
                await asyncio.sleep(1)
            self._active += 1

        job.state = DownloadState.downloading
        Path(download_dir).mkdir(parents=True, exist_ok=True)

        try:
            await self._download_file(job, url, title, download_dir)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 429) and rd_link and rd_client:
                try:
                    new_url = await rd_client.unrestrict_link(rd_link)
                    await self._download_file(job, new_url, title, download_dir)
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
        title: str,
        download_dir: str,
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
            ext = guess_extension(str(response.url), mime_type)
            filename = f"{sanitize_filename(title)}{ext}"
            dest_dir = Path(download_dir)
            dest = dest_dir / filename
            if dest.exists():
                dest = dest_dir / f"{sanitize_filename(title)}_{job.id}{ext}"
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
