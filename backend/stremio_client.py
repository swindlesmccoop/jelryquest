from typing import Any
from urllib.parse import quote

import httpx

from backend.config import Settings
from backend.models import EpisodeItem, SearchResult, StreamItem
from backend.real_debrid import RealDebridClient
from backend.torrentio_urls import is_torrentio_resolve_url, parse_torrentio_stream_url

MAX_STREAMS = 10


class StremioClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        rd_client: RealDebridClient | None = None,
    ):
        self.settings = settings
        self.client = client
        self.rd_client = rd_client

    async def search(self, query: str, content_type: str = "movie") -> list[SearchResult]:
        if content_type == "series":
            return await self.search_series(query)
        return await self.search_movies(query)

    async def search_movies(self, query: str) -> list[SearchResult]:
        encoded = quote(query.strip())
        url = (
            f"{self.settings.cinemeta_base}/catalog/movie/top/search={encoded}.json"
        )
        return self._map_search_results(await self._get_json(url))

    async def search_series(self, query: str) -> list[SearchResult]:
        encoded = quote(query.strip())
        url = (
            f"{self.settings.cinemeta_base}/catalog/series/top/search={encoded}.json"
        )
        return self._map_search_results(await self._get_json(url))

    def _map_search_results(self, data: dict[str, Any]) -> list[SearchResult]:
        metas = data.get("metas") or []
        results: list[SearchResult] = []
        for meta in metas:
            imdb_id = meta.get("id")
            if not imdb_id:
                continue
            results.append(
                SearchResult(
                    id=imdb_id,
                    type=meta.get("type") or "movie",
                    name=meta.get("name") or imdb_id,
                    year=str(meta["year"]) if meta.get("year") else None,
                    poster=meta.get("poster"),
                )
            )
        return results

    async def get_movie_meta(self, imdb_id: str) -> dict[str, Any]:
        imdb_id = self._normalize_imdb_id(imdb_id)
        url = f"{self.settings.cinemeta_base}/meta/movie/{imdb_id}.json"
        return await self._get_json(url)

    async def get_series_meta(self, imdb_id: str) -> dict[str, Any]:
        imdb_id = self._normalize_imdb_id(imdb_id)
        url = f"{self.settings.cinemeta_base}/meta/series/{imdb_id}.json"
        return await self._get_json(url)

    async def get_series_episodes(self, imdb_id: str) -> list[EpisodeItem]:
        data = await self.get_series_meta(imdb_id)
        meta = data.get("meta") or {}
        videos = meta.get("videos") or []
        episodes: list[EpisodeItem] = []
        for video in videos:
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

    async def get_streams(self, imdb_id: str) -> list[StreamItem]:
        imdb_id = self._normalize_imdb_id(imdb_id)
        url = f"{self.settings.addon_base}/stream/movie/{imdb_id}.json"
        return await self._fetch_streams(url)

    async def get_series_streams(
        self,
        imdb_id: str,
        season: int,
        episode: int,
    ) -> list[StreamItem]:
        imdb_id = self._normalize_imdb_id(imdb_id)
        url = (
            f"{self.settings.addon_base}/stream/series/"
            f"{imdb_id}:{season}:{episode}.json"
        )
        return await self._fetch_streams(url)

    async def _fetch_streams(self, url: str) -> list[StreamItem]:
        data = await self._get_json(url)
        streams = data.get("streams") or []
        items: list[StreamItem] = []
        for index, stream in enumerate(streams[:MAX_STREAMS]):
            normalized = self._normalize_raw_stream(stream)
            items.append(
                StreamItem(
                    index=index,
                    name=normalized["name"],
                    title=normalized["title"],
                    url=normalized["url"],
                    infoHash=normalized["info_hash"],
                    fileIdx=normalized["file_idx"],
                    raw=normalized["raw"],
                )
            )
        if self.rd_client and self.settings.resolved_rd_token:
            return await self.rd_client.enrich_streams(items)
        return await self._enrich_fast_path(items)

    def _normalize_raw_stream(self, stream: dict[str, Any]) -> dict[str, Any]:
        url = stream.get("url")
        info_hash = stream.get("infoHash")
        file_idx = stream.get("fileIdx")
        title = stream.get("title")

        hints = stream.get("behaviorHints") or {}
        hint_filename = (hints.get("filename") or "").strip()

        if url:
            parsed = parse_torrentio_stream_url(url)
            if not info_hash and parsed.get("info_hash"):
                info_hash = parsed["info_hash"]
            if file_idx is None and parsed.get("file_idx") is not None:
                file_idx = parsed["file_idx"]
            if hint_filename and not title:
                title = hint_filename
            elif hint_filename and title and "\n" in title:
                first_line = title.split("\n", 1)[0].strip()
                if not first_line:
                    title = hint_filename

        return {
            "name": stream.get("name"),
            "title": title,
            "url": url,
            "info_hash": info_hash,
            "file_idx": file_idx,
            "raw": stream,
        }

    async def _enrich_fast_path(self, items: list[StreamItem]) -> list[StreamItem]:
        from backend.real_debrid import build_magnet
        from backend.rd_urls import is_realdebrid_url

        enriched: list[StreamItem] = []
        for item in items:
            magnet = build_magnet(item.info_hash) if item.info_hash else None
            rd_link = item.url.strip() if item.url and is_realdebrid_url(item.url) else None
            ddl = None
            if (
                item.url
                and not is_realdebrid_url(item.url)
                and not is_torrentio_resolve_url(item.url)
            ):
                ddl = item.url
            enriched.append(
                item.model_copy(
                    update={
                        "magnet": magnet,
                        "rd_link": rd_link,
                        "ddl": ddl,
                    }
                )
            )
        return enriched

    def _normalize_imdb_id(self, imdb_id: str) -> str:
        imdb_id = imdb_id.strip()
        if not imdb_id.startswith("tt"):
            imdb_id = f"tt{imdb_id}" if imdb_id.isdigit() else imdb_id
        return imdb_id

    async def _get_json(self, url: str) -> dict[str, Any]:
        fetch_url = url
        bridge = self.settings.addon_bridge_url.strip()
        if bridge:
            fetch_url = f"{bridge.rstrip('/')}/fetch?url={quote(url, safe='')}"
        response = await self.client.get(fetch_url)
        response.raise_for_status()
        return response.json()
