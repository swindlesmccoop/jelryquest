import re
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


def _extract_rd_token(manifest_url: str) -> str | None:
    match = re.search(r"realdebrid=([^/&]+)", manifest_url, re.IGNORECASE)
    return match.group(1) if match else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    stremio_addon_manifest_url: str
    real_debrid_api_token: str | None = None
    download_dir: str = "/downloads"
    download_dir_movies: str | None = None
    download_dir_series: str | None = None
    expected_egress_ip: str = ""  # empty disables the egress check
    egress_check_url: str = "https://api.ipify.org"
    egress_check_strict: bool = True
    max_active_downloads: int = 1
    host: str = "0.0.0.0"
    web_port: int = 8080
    cinemeta_base: str = "https://v3-cinemeta.strem.io"
    # Fetches Torrentio/Cinemeta via home WAN (addon-bridge); RD/downloads use VPS egress.
    addon_bridge_url: str = "http://addon-bridge:8090"

    @property
    def addon_base(self) -> str:
        url = self.stremio_addon_manifest_url.rstrip("/")
        if url.endswith("/manifest.json"):
            return url[: -len("/manifest.json")]
        return url

    @property
    def resolved_rd_token(self) -> str | None:
        if self.real_debrid_api_token:
            return self.real_debrid_api_token
        return _extract_rd_token(self.stremio_addon_manifest_url)

    def download_path_for(self, content_type: Literal["movie", "series"]) -> str:
        if content_type == "series":
            return (
                self.download_dir_series
                or self.download_dir_movies
                or self.download_dir
            )
        return self.download_dir_movies or self.download_dir


@lru_cache
def get_settings() -> Settings:
    return Settings()
