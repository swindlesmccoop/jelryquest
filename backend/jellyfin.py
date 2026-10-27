"""Trigger Jellyfin's library scan using a server-side API key."""

import httpx

from backend.config import Settings


class JellyfinClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    @property
    def configured(self) -> bool:
        return bool(self.settings.jellyfin_url.strip() and self.settings.jellyfin_api_key.strip())

    async def refresh_library(self) -> None:
        response = await self.client.post(
            f"{self.settings.jellyfin_url.strip().rstrip('/')}/Library/Refresh",
            headers={"X-MediaBrowser-Token": self.settings.jellyfin_api_key.strip()},
            # Keep the API key on the configured server, even if it redirects.
            follow_redirects=False,
        )
        response.raise_for_status()
