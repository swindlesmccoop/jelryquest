import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.cdn_scan import CdnScanner
from backend.config import Settings, get_settings
from backend.downloads import DownloadManager, DownloadSource
from backend.jellyfin import JellyfinClient
from backend.links import parse_pasted_link
from backend.models import (
    DownloadRequest,
    EpisodeItem,
    PasteDownloadRequest,
    SearchResult,
    StreamItem,
)
from backend.rd import CdnPreference, RealDebridClient
from backend.stremio import StremioClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Addon URLs contain credentials; httpx logs full request URLs at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or get_settings()
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=30.0),
            follow_redirects=True,
            headers={"User-Agent": cfg.http_user_agent},
        )
        cdn = CdnPreference(cfg.real_debrid_preferred_cdn)
        rd = RealDebridClient(cfg, http, cdn)
        scanner = CdnScanner(cfg, http, cdn)
        app.state.settings = cfg
        app.state.stremio = StremioClient(cfg, http)
        app.state.jellyfin = JellyfinClient(cfg, http)
        app.state.downloads = DownloadManager(cfg, http, rd)
        app.state.scanner = scanner
        scanner.start()
        logger.info("JelryQuest listening on port %s", cfg.web_port)
        try:
            yield
        finally:
            await scanner.stop()
            await app.state.downloads.close()
            await http.aclose()

    app = FastAPI(title="JelryQuest", lifespan=lifespan)

    def stremio_of(request: Request) -> StremioClient:
        return request.app.state.stremio

    async def upstream(coro, what: str):
        try:
            return await coro
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"{what} failed: {exc}") from exc

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        return {
            "status": "ok",
            "rd_cdn_scan": request.app.state.scanner.status(),
            "jellyfin_configured": request.app.state.jellyfin.configured,
        }

    @app.post("/api/jellyfin/refresh")
    async def refresh_jellyfin(request: Request) -> dict:
        jellyfin = request.app.state.jellyfin
        if not jellyfin.configured:
            raise HTTPException(503, "Set JELLYFIN_URL and JELLYFIN_API_KEY to enable library refresh.")
        try:
            await jellyfin.refresh_library()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                detail = "Jellyfin denied access. Check JELLYFIN_API_KEY has administrator access."
            else:
                detail = f"Jellyfin library refresh failed (HTTP {exc.response.status_code})."
            raise HTTPException(502, detail) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Could not reach Jellyfin. Check JELLYFIN_URL and server availability.") from exc
        return {"status": "accepted"}

    @app.get("/api/search", response_model=list[SearchResult])
    async def search(
        request: Request,
        q: str = Query(..., min_length=1),
        type: str = Query("movie", pattern="^(movie|series)$"),
    ):
        return await upstream(stremio_of(request).search(q, type), "Search")

    @app.get("/api/movie/{imdb_id}/meta")
    async def movie_meta(request: Request, imdb_id: str) -> dict:
        return await upstream(stremio_of(request).get_meta(imdb_id, "movie"), "Meta fetch")

    @app.get("/api/movie/{imdb_id}/streams", response_model=list[StreamItem])
    async def movie_streams(request: Request, imdb_id: str):
        streams = await upstream(stremio_of(request).get_streams(imdb_id), "Stream fetch")
        if not streams:
            raise HTTPException(404, "No streams found for this title")
        return streams

    @app.get("/api/show/{imdb_id}/meta")
    async def show_meta(request: Request, imdb_id: str) -> dict:
        return await upstream(stremio_of(request).get_meta(imdb_id, "series"), "Meta fetch")

    @app.get("/api/show/{imdb_id}/episodes", response_model=list[EpisodeItem])
    async def show_episodes(request: Request, imdb_id: str):
        episodes = await upstream(stremio_of(request).get_episodes(imdb_id), "Episode fetch")
        if not episodes:
            raise HTTPException(404, "No episodes found for this show")
        return episodes

    @app.get("/api/show/{imdb_id}/streams", response_model=list[StreamItem])
    async def show_streams(
        request: Request,
        imdb_id: str,
        season: int = Query(..., ge=1),
        episode: int = Query(..., ge=1),
    ):
        streams = await upstream(
            stremio_of(request).get_streams(imdb_id, season, episode), "Stream fetch"
        )
        if not streams:
            raise HTTPException(404, "No streams found for this episode")
        return streams

    @app.post("/api/download")
    async def start_download(request: Request, body: DownloadRequest) -> dict:
        if body.content_type == "series":
            if body.season is None or body.episode is None:
                raise HTTPException(400, "season and episode required for series downloads")
            fetch = stremio_of(request).get_streams(body.imdb_id, body.season, body.episode)
        else:
            fetch = stremio_of(request).get_streams(body.imdb_id)
        streams = await upstream(fetch, "Stream fetch")
        if not 0 <= body.stream_index < len(streams):
            raise HTTPException(400, "Invalid stream index")

        stream = streams[body.stream_index]
        title = stream.title or stream.name or body.title or stream.filename or body.imdb_id
        try:
            job = request.app.state.downloads.enqueue(
                DownloadSource.from_stream(stream),
                title=title,
                content_type=body.content_type,
                imdb_id=body.imdb_id,
                stream_index=stream.index,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"job_id": job.id, "state": job.state}

    @app.post("/api/download/link")
    async def download_pasted_link(request: Request, body: PasteDownloadRequest) -> dict:
        try:
            link = parse_pasted_link(body.link)
            job = request.app.state.downloads.enqueue(
                DownloadSource(
                    direct_url=link.direct_url,
                    rd_link=link.rd_link,
                    info_hash=link.info_hash,
                    filename_hint=link.filename,
                ),
                title=(body.title or link.title or "download").strip(),
                content_type=body.content_type,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"job_id": job.id, "state": job.state}

    @app.get("/api/downloads")
    async def list_downloads(request: Request) -> list[dict]:
        return [j.model_dump() for j in request.app.state.downloads.list_jobs()]

    @app.get("/api/downloads/{job_id}")
    async def get_download(request: Request, job_id: str) -> dict:
        job = request.app.state.downloads.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job.model_dump()

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("backend.main:app", host=settings.host, port=settings.web_port, log_level="info")


if __name__ == "__main__":
    run()
