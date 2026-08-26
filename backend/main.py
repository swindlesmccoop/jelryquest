import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import Settings, get_settings
from backend.downloader import DownloadManager
from backend.egress_check import EgressResult, verify_egress_on_startup
from backend.link_import import classify_pasted_link, resolve_pasted_link
from backend.models import (
    DownloadRequest,
    EpisodeItem,
    HealthResponse,
    PasteDownloadRequest,
    SearchResult,
    StreamItem,
)
from backend.real_debrid import RealDebridClient
from backend.server_scan import get_scan_status, start_background_server_scan
from backend.stremio_client import StremioClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

egress_state: EgressResult | None = None
http_client: httpx.AsyncClient | None = None
rd_client: RealDebridClient | None = None
stremio: StremioClient | None = None
downloads: DownloadManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global egress_state, http_client, rd_client, stremio, downloads
    settings = get_settings()
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=30.0),
        follow_redirects=True,
    )
    egress_state = EgressResult(
        ok=False,
        observed_ip=None,
        expected_ip=settings.expected_egress_ip.strip(),
        message="Egress check in progress",
    )
    rd_client = RealDebridClient(settings, http_client)
    stremio = StremioClient(settings, http_client, rd_client)
    downloads = DownloadManager(settings, http_client)

    asyncio.create_task(start_background_server_scan(http_client))

    async def run_egress() -> None:
        global egress_state
        egress_state = await verify_egress_on_startup(settings)
        logger.info("JelryQuest egress check complete: %s", egress_state.message)

    egress_task = asyncio.create_task(run_egress())
    logger.info("JelryQuest listening on port %s (egress check in background)", settings.web_port)
    yield
    egress_task.cancel()
    try:
        await egress_task
    except asyncio.CancelledError:
        pass
    await http_client.aclose()


app = FastAPI(title="JelryQuest", lifespan=lifespan)


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings = get_settings()
    if egress_state is None:
        return HealthResponse(
            egress_ok=False,
            observed_ip=None,
            expected_ip=settings.expected_egress_ip,
            message="Egress check pending",
            rd_cdn_scan=get_scan_status(),
        )
    return HealthResponse(
        egress_ok=egress_state.ok,
        observed_ip=egress_state.observed_ip,
        expected_ip=egress_state.expected_ip,
        message=egress_state.message,
        rd_cdn_scan=get_scan_status(),
    )


@app.get("/api/search", response_model=list[SearchResult])
async def search(
    q: str = Query(..., min_length=1),
    type: str = Query("movie", pattern="^(movie|series)$"),
) -> list[SearchResult]:
    if not egress_state or not egress_state.ok:
        raise HTTPException(503, "Egress check failed; downloads unavailable")
    assert stremio is not None
    try:
        return await stremio.search(q, type)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Search failed: {exc}") from exc


@app.get("/api/movie/{imdb_id}/meta")
async def movie_meta(imdb_id: str) -> dict:
    assert stremio is not None
    try:
        return await stremio.get_movie_meta(imdb_id)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Meta fetch failed: {exc}") from exc


@app.get("/api/movie/{imdb_id}/streams", response_model=list[StreamItem])
async def movie_streams(imdb_id: str) -> list[StreamItem]:
    if not egress_state or not egress_state.ok:
        raise HTTPException(503, "Egress check failed; downloads unavailable")
    assert stremio is not None
    try:
        streams = await stremio.get_streams(imdb_id)
        if not streams:
            raise HTTPException(404, "No streams found for this title")
        return streams
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Stream fetch failed: {exc}") from exc


@app.get("/api/show/{imdb_id}/meta")
async def show_meta(imdb_id: str) -> dict:
    assert stremio is not None
    try:
        return await stremio.get_series_meta(imdb_id)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Meta fetch failed: {exc}") from exc


@app.get("/api/show/{imdb_id}/episodes", response_model=list[EpisodeItem])
async def show_episodes(imdb_id: str) -> list[EpisodeItem]:
    assert stremio is not None
    try:
        episodes = await stremio.get_series_episodes(imdb_id)
        if not episodes:
            raise HTTPException(404, "No episodes found for this show")
        return episodes
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Episode fetch failed: {exc}") from exc


@app.get("/api/show/{imdb_id}/streams", response_model=list[StreamItem])
async def show_streams(
    imdb_id: str,
    season: int = Query(..., ge=1),
    episode: int = Query(..., ge=1),
) -> list[StreamItem]:
    if not egress_state or not egress_state.ok:
        raise HTTPException(503, "Egress check failed; downloads unavailable")
    assert stremio is not None
    try:
        streams = await stremio.get_series_streams(imdb_id, season, episode)
        if not streams:
            raise HTTPException(404, "No streams found for this episode")
        return streams
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Stream fetch failed: {exc}") from exc


async def _fetch_streams_for_download(body: DownloadRequest) -> list[StreamItem]:
    assert stremio is not None
    if body.content_type == "series":
        if body.season is None or body.episode is None:
            raise HTTPException(400, "season and episode required for series downloads")
        return await stremio.get_series_streams(
            body.imdb_id, body.season, body.episode
        )
    return await stremio.get_streams(body.imdb_id)


@app.post("/api/download", response_model=dict)
async def start_download(body: DownloadRequest) -> dict:
    if not egress_state or not egress_state.ok:
        raise HTTPException(503, "Egress check failed; downloads unavailable")
    assert stremio is not None and downloads is not None and rd_client is not None
    try:
        streams = await _fetch_streams_for_download(body)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Stream fetch failed: {exc}") from exc

    if body.stream_index < 0 or body.stream_index >= len(streams):
        raise HTTPException(400, "Invalid stream index")

    stream = streams[body.stream_index]
    try:
        job = await downloads.enqueue(
            body.imdb_id,
            stream,
            body.title,
            rd_client,
            body.content_type,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {"job_id": job.id, "state": job.state}


@app.post("/api/download/link", response_model=dict)
async def download_pasted_link(body: PasteDownloadRequest) -> dict:
    if not egress_state or not egress_state.ok:
        raise HTTPException(503, "Egress check failed; downloads unavailable")
    assert downloads is not None and rd_client is not None
    settings = get_settings()
    try:
        kind = classify_pasted_link(body.link)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if kind in ("magnet", "rd") and not settings.resolved_rd_token:
        raise HTTPException(
            400,
            "Real Debrid API token required for magnet and real-debrid.com links",
        )
    try:
        direct_url, rd_link, suggested_title, filename_hint = await resolve_pasted_link(
            body.link, rd_client
        )
        title = (body.title or suggested_title or "download").strip()
        job = await downloads.enqueue_direct(
            direct_url,
            title,
            body.content_type,
            rd_link,
            rd_client,
            filename_hint,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Link resolve failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {"job_id": job.id, "state": job.state}


@app.get("/api/downloads")
async def list_downloads() -> list[dict]:
    assert downloads is not None
    return [j.model_dump() for j in downloads.list_jobs()]


@app.get("/api/downloads/{job_id}")
async def get_download(job_id: str) -> dict:
    assert downloads is not None
    job = downloads.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.model_dump()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.web_port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
