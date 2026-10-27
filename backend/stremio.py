"""Cinemeta (search/metadata) and Torrentio (streams) via the Stremio addon protocol."""

import re
import time
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

from backend.config import Settings
from backend.models import EpisodeItem, SearchResult, StreamItem
from backend.rd import build_magnet, is_realdebrid_url

MAX_STREAMS = 10
STREAM_CACHE_TTL_SEC = 600.0

_HASH_RE = re.compile(r"^[a-fA-F0-9]{32,40}$")
_NULLS = ("null", "none", "-")


def is_torrentio_resolve_url(url: str | None) -> bool:
    return "torrentio.strem.fun/resolve/" in (url or "").strip().lower()


def parse_torrentio_stream_url(url: str) -> dict[str, str | int | None]:
    """Extract info_hash, file_idx and filename from a Torrentio resolve URL."""
    raw = (url or "").strip()
    if not is_torrentio_resolve_url(raw):
        return {}
    parts = [p for p in urlparse(raw).path.split("/") if p]
    try:
        after = parts[parts.index("resolve") + 1 :]
    except ValueError:
        return {}

    for i, part in enumerate(after):
        if not _HASH_RE.match(part):
            continue
        rest = after[i + 1 :]
        # Layout is <hash>/<fileIdx>/<filename>, optionally with a "null"
        # placeholder before or after the hash segment.
        if rest and rest[0].lower() in _NULLS:
            rest = rest[1:]
        elif len(rest) > 2 and rest[1].lower() in _NULLS:
            rest = rest[2:]
        file_idx = int(rest[0]) if rest and rest[0].isdigit() else None
        filename = unquote(rest[1]) if file_idx is not None and len(rest) > 1 else None
        return {"info_hash": part.lower(), "file_idx": file_idx, "filename": filename}
    return {}


def normalize_imdb_id(imdb_id: str) -> str:
    imdb_id = imdb_id.strip()
    return f"tt{imdb_id}" if imdb_id.isdigit() else imdb_id


class StremioClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self._stream_cache: dict[str, tuple[float, list[StreamItem]]] = {}

    async def _get_json(self, url: str) -> dict[str, Any]:
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()

    # --- Cinemeta -----------------------------------------------------------

    async def search(self, query: str, content_type: str = "movie") -> list[SearchResult]:
        kind = "series" if content_type == "series" else "movie"
        url = f"{self.settings.cinemeta_base}/catalog/{kind}/top/search={quote(query.strip())}.json"
        data = await self._get_json(url)
        results: list[SearchResult] = []
        for meta in data.get("metas") or []:
            imdb_id = meta.get("id")
            if not imdb_id:
                continue
            results.append(
                SearchResult(
                    id=imdb_id,
                    type=meta.get("type") or kind,
                    name=meta.get("name") or imdb_id,
                    year=str(meta["year"]) if meta.get("year") else None,
                    poster=meta.get("poster"),
                )
            )
        return results

    async def get_meta(self, imdb_id: str, kind: str) -> dict[str, Any]:
        url = f"{self.settings.cinemeta_base}/meta/{kind}/{normalize_imdb_id(imdb_id)}.json"
        return await self._get_json(url)

    async def get_episodes(self, imdb_id: str) -> list[EpisodeItem]:
        meta = (await self.get_meta(imdb_id, "series")).get("meta") or {}
        episodes: list[EpisodeItem] = []
        for video in meta.get("videos") or []:
            season = video.get("season")
            episode = video.get("episode") or video.get("number")
            if season is None or episode is None:
                continue
            episodes.append(
                EpisodeItem(
                    id=video.get("id") or f"{imdb_id}:{season}:{episode}",
                    title=video.get("title") or f"S{season}E{episode}",
                    season=int(season),
                    episode=int(episode),
                    thumbnail=video.get("thumbnail"),
                )
            )
        episodes.sort(key=lambda e: (e.season, e.episode))
        return episodes

    # --- Torrentio ----------------------------------------------------------

    async def get_streams(
        self, imdb_id: str, season: int | None = None, episode: int | None = None
    ) -> list[StreamItem]:
        """Streams for a movie, or for one episode when season/episode are given.

        Results are cached briefly so a later download request resolves the same
        ``stream_index`` the user clicked on.
        """
        imdb_id = normalize_imdb_id(imdb_id)
        if season is not None and episode is not None:
            path = f"series/{imdb_id}:{season}:{episode}"
        else:
            path = f"movie/{imdb_id}"
        url = f"{self.settings.addon_base}/stream/{path}.json"

        now = time.monotonic()
        cached = self._stream_cache.get(url)
        if cached and now - cached[0] < STREAM_CACHE_TTL_SEC:
            return cached[1]

        data = await self._get_json(url)
        streams = [
            self._to_stream_item(i, raw)
            for i, raw in enumerate((data.get("streams") or [])[:MAX_STREAMS])
        ]
        self._stream_cache = {
            k: v for k, v in self._stream_cache.items() if now - v[0] < STREAM_CACHE_TTL_SEC
        }
        self._stream_cache[url] = (now, streams)
        return streams

    @staticmethod
    def _to_stream_item(index: int, raw: dict[str, Any]) -> StreamItem:
        url = raw.get("url")
        info_hash = raw.get("infoHash")
        file_idx = raw.get("fileIdx")
        title = raw.get("title")
        hint = ((raw.get("behaviorHints") or {}).get("filename") or "").strip() or None
        filename = hint

        if url:
            parsed = parse_torrentio_stream_url(url)
            info_hash = info_hash or parsed.get("info_hash")
            if file_idx is None:
                file_idx = parsed.get("file_idx")
            filename = filename or parsed.get("filename")
        if hint and (not title or not title.split("\n", 1)[0].strip()):
            title = hint

        return StreamItem(
            index=index,
            name=raw.get("name"),
            title=title,
            url=url,
            infoHash=info_hash,
            fileIdx=file_idx,
            filename=filename,
            magnet=build_magnet(info_hash) if info_hash else None,
            rd_link=url.strip() if url and is_realdebrid_url(url) else None,
            ddl=url if url and not is_realdebrid_url(url) and not is_torrentio_resolve_url(url) else None,
            raw=raw,
        )
