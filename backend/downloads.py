"""Download queue: resolves sources through Real-Debrid and writes files to disk."""

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from backend.config import Settings
from backend.models import ContentType, DownloadJob, DownloadState, StreamItem
from backend.rd import RealDebridClient, is_realdebrid_url

logger = logging.getLogger(__name__)

RD_ERROR_HINT = (
    "Real Debrid rejected the download link (403/429). Common causes: the link "
    "expired, another device is using your RD account from a different IP, or too "
    "many simultaneous downloads. Check active downloads at "
    "real-debrid.com/downloads."
)
CHUNK_SIZE = 256 * 1024


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:200] or "download"


def filename_from_url(url: str) -> str | None:
    name = Path(urlparse(url).path).name
    return unquote(name) if name and name not in (".", "..") else None


def guess_extension(url: str, content_type: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix
    if suffix and len(suffix) <= 8:
        return suffix
    if content_type and "mp4" in content_type:
        return ".mp4"
    return ".mkv"


def resolve_dest_filename(
    filename_hint: str | None, url: str, content_type: str | None, fallback: str
) -> str:
    for raw in (filename_hint, filename_from_url(url)):
        if not raw or not raw.strip():
            continue
        cleaned = sanitize_filename(raw.strip())
        return cleaned if "." in cleaned else f"{cleaned}{guess_extension(url, content_type)}"
    return f"{sanitize_filename(fallback)}{guess_extension(url, content_type)}"


@dataclass
class DownloadSource:
    """Where a job's bytes come from. Priority: rd_link, then info_hash, then direct_url."""

    direct_url: str | None = None
    rd_link: str | None = None
    info_hash: str | None = None
    file_idx: int | None = None
    filename_hint: str | None = None

    @classmethod
    def from_stream(cls, stream: StreamItem) -> "DownloadSource":
        rd_link = stream.rd_link
        if stream.url and is_realdebrid_url(stream.url):
            rd_link = stream.url.strip()
        filename = stream.filename
        if not filename and stream.title and "\n" in stream.title:
            filename = stream.title.split("\n", 1)[0].strip() or None
        return cls(
            direct_url=stream.ddl,
            rd_link=rd_link,
            info_hash=stream.info_hash,
            file_idx=stream.file_idx,
            filename_hint=filename,
        )

    def validate(self, rd_enabled: bool) -> None:
        if self.rd_link or self.info_hash:
            if not rd_enabled:
                raise ValueError("Real Debrid API token required for this download")
        elif not self.direct_url:
            raise ValueError(
                "Selected stream has no direct download URL or Real Debrid link. "
                "Torrentio may need RD in the manifest or magnet resolution failed."
            )


class DownloadManager:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, rd: RealDebridClient):
        self.settings = settings
        self.client = client
        self.rd = rd
        self.jobs: dict[str, DownloadJob] = {}
        self._slots = asyncio.Semaphore(max(1, settings.max_active_downloads))
        self._tasks: set[asyncio.Task] = set()

    def list_jobs(self) -> list[DownloadJob]:
        return list(reversed(self.jobs.values()))

    def get_job(self, job_id: str) -> DownloadJob | None:
        return self.jobs.get(job_id)

    def enqueue(
        self,
        source: DownloadSource,
        *,
        title: str,
        content_type: ContentType,
        imdb_id: str = "paste",
        stream_index: int = -1,
    ) -> DownloadJob:
        source.validate(bool(self.rd.token))
        job = DownloadJob(
            id=uuid.uuid4().hex[:8], imdb_id=imdb_id, stream_index=stream_index, title=title
        )
        self.jobs[job.id] = job
        task = asyncio.create_task(
            self._run(job, source, self.settings.download_path_for(content_type))
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self, job: DownloadJob, source: DownloadSource, download_dir: str) -> None:
        async with self._slots:
            job.state = DownloadState.downloading
            try:
                Path(download_dir).mkdir(parents=True, exist_ok=True)
                await self._fetch(job, source, download_dir)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                job.state = DownloadState.error
                code = exc.response.status_code
                job.error = RD_ERROR_HINT if code in (403, 429) else f"HTTP {code}: {exc}"
                logger.warning("Download %s failed: %s", job.id, job.error)
            except Exception as exc:
                job.state = DownloadState.error
                job.error = str(exc) or type(exc).__name__
                logger.exception("Download %s failed", job.id)

    async def _fetch(self, job: DownloadJob, source: DownloadSource, download_dir: str) -> None:
        rd_link = source.rd_link
        filename_hint = source.filename_hint
        if not rd_link and source.info_hash:
            resolved = await self.rd.resolve_magnet(source.info_hash, source.file_idx)
            rd_link = resolved.rd_link
            filename_hint = resolved.filename or filename_hint

        for attempt in (1, 2):
            url = await self.rd.resolve_download_url(rd_link) if rd_link else source.direct_url
            if not url:
                raise ValueError("No download URL or Real Debrid link")
            try:
                await self._download_file(job, url, download_dir, filename_hint)
                return
            except httpx.HTTPStatusError as exc:
                # RD links go stale; one retry re-unrestricts to get a fresh URL.
                if attempt == 1 and rd_link and exc.response.status_code in (403, 429):
                    logger.info("Download %s got %s, re-resolving link", job.id, exc.response.status_code)
                    continue
                raise

    async def _download_file(
        self, job: DownloadJob, url: str, download_dir: str, filename_hint: str | None
    ) -> None:
        job.bytes_received = 0
        job.total_bytes = None
        async with self.client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            filename = resolve_dest_filename(
                filename_hint, str(response.url), response.headers.get("content-type"), job.title
            )
            dest = Path(download_dir) / filename
            if dest.exists():
                dest = dest.with_name(f"{dest.stem}_{job.id}{dest.suffix}")
            job.filename = dest.name

            length = response.headers.get("content-length")
            job.total_bytes = int(length) if length and length.isdigit() else None

            part = dest.with_name(dest.name + ".part")
            last_tick, last_bytes = time.monotonic(), 0
            try:
                with part.open("wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                        f.write(chunk)
                        job.bytes_received += len(chunk)
                        now = time.monotonic()
                        if now - last_tick >= 1.0:
                            job.speed_bps = (job.bytes_received - last_bytes) / (now - last_tick)
                            last_tick, last_bytes = now, job.bytes_received
                part.rename(dest)
            except BaseException:
                part.unlink(missing_ok=True)
                raise

        job.state = DownloadState.done
        job.speed_bps = None
        logger.info("Download complete: %s -> %s", job.id, dest)
