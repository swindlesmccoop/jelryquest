import re
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

_RD_TOKEN_RE = re.compile(r"realdebrid=([^/&]+)", re.IGNORECASE)
DEFAULT_HTTP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    stremio_addon_manifest_url: str
    cinemeta_base: str = "https://v3-cinemeta.strem.io"
    http_user_agent: str = DEFAULT_HTTP_USER_AGENT

    jellyfin_url: str = ""
    jellyfin_api_key: str = ""

    real_debrid_api_token: str | None = None
    real_debrid_api_base: str = "https://api.real-debrid.com/rest/1.0"
    real_debrid_remote: bool = False
    real_debrid_connect_timeout_sec: float = 15.0
    real_debrid_read_timeout_sec: float = 60.0

    # CDN selection: pin a host, or let a background scan pick the fastest.
    real_debrid_preferred_cdn: str = ""
    real_debrid_auto_scan: bool = True
    real_debrid_scan_seconds: float = 6.0
    real_debrid_scan_workers: int = 8
    real_debrid_scan_hosts: str = ""
    real_debrid_scan_connect_timeout_sec: float = 8.0
    real_debrid_scan_cache_ttl_sec: float = 86400.0

    cache_dir: str = "/cache"
    download_dir: str = "/downloads"
    download_dir_movies: str | None = None
    download_dir_series: str | None = None
    max_active_downloads: int = 1

    host: str = "0.0.0.0"
    web_port: int = 8080

    @property
    def addon_base(self) -> str:
        url = self.stremio_addon_manifest_url.strip().rstrip("/")
        return url.removesuffix("/manifest.json")

    @property
    def rd_token(self) -> str | None:
        if self.real_debrid_api_token:
            return self.real_debrid_api_token
        match = _RD_TOKEN_RE.search(self.stremio_addon_manifest_url)
        return match.group(1) if match else None

    def download_path_for(self, content_type: Literal["movie", "series"]) -> str:
        if content_type == "series":
            return self.download_dir_series or self.download_dir_movies or self.download_dir
        return self.download_dir_movies or self.download_dir


@lru_cache
def get_settings() -> Settings:
    return Settings()
