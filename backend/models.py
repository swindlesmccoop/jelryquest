from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DownloadState(str, Enum):
    queued = "queued"
    downloading = "downloading"
    done = "done"
    error = "error"


class SearchResult(BaseModel):
    id: str
    type: str = "movie"
    name: str
    year: str | None = None
    poster: str | None = None


class ResolvedLinks(BaseModel):
    magnet: str | None = None
    rd_link: str | None = None
    ddl: str | None = None
    filename: str | None = None


class StreamItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    index: int
    name: str | None = None
    title: str | None = None
    url: str | None = None
    info_hash: str | None = Field(None, alias="infoHash")
    file_idx: int | None = Field(None, alias="fileIdx")
    magnet: str | None = None
    rd_link: str | None = None
    ddl: str | None = None
    filename: str | None = None
    resolve_error: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class EpisodeItem(BaseModel):
    id: str
    title: str
    season: int
    episode: int
    thumbnail: str | None = None


class DownloadRequest(BaseModel):
    imdb_id: str
    stream_index: int
    title: str | None = None
    content_type: Literal["movie", "series"] = "movie"
    season: int | None = None
    episode: int | None = None


class PasteDownloadRequest(BaseModel):
    link: str
    title: str | None = None
    content_type: Literal["movie", "series"] = "movie"


class DownloadJob(BaseModel):
    id: str
    imdb_id: str
    stream_index: int
    title: str
    filename: str | None = None
    state: DownloadState
    bytes_received: int = 0
    total_bytes: int | None = None
    error: str | None = None
    speed_bps: float | None = None


class HealthResponse(BaseModel):
    egress_ok: bool
    observed_ip: str | None
    expected_ip: str
    message: str | None = None
    rd_cdn_scan: dict[str, Any] | None = None
