import re
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _extract_rd_token(manifest_url: str) -> str | None:
    match = re.search(r"realdebrid=([^/&]+)", manifest_url, re.IGNORECASE)
    return match.group(1) if match else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    stremio_addon_manifest_url: str
    real_debrid_api_token: str | None = None
    download_dir: str = "/downloads"
    expected_egress_ip: str
    egress_check_url: str = "https://api.ipify.org"
    egress_check_strict: bool = True
    max_active_downloads: int = 1
    host: str = "0.0.0.0"
    web_port: int = 8080
    cinemeta_base: str = "https://v3-cinemeta.strem.io"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
