"""Trigger Jellyfin's library scan using a server-side API key."""

import asyncio
import math

import httpx

from backend.config import Settings


class JellyfinClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.refresh_lock = asyncio.Lock()

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f'MediaBrowser Token="{self.settings.jellyfin_api_key.strip()}"'}

    async def scan_status(self) -> dict:
        response = await self.client.get(
            f"{self.settings.jellyfin_url.strip().rstrip('/')}/ScheduledTasks",
            headers=self.headers,
            follow_redirects=False,
        )
        response.raise_for_status()
        tasks = response.json()
        if not isinstance(tasks, list):
            raise ValueError("Invalid scheduled tasks response")
        # Match the visible task name; do not depend on an unverified task key.
        task = next((t for t in tasks if isinstance(t, dict) and t.get("Name") == "Scan Media Library"), {})
        state = task.get("State", "unknown")
        if state not in ("Idle", "Running", "Cancelling"):
            state = "unknown"
        progress = task.get("CurrentProgressPercentage") if state == "Running" else None
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not math.isfinite(progress):
            progress = None
        elif progress is not None:
            progress = max(0.0, min(100.0, float(progress)))
        return {"state": state, "progress": progress}

    @property
    def configured(self) -> bool:
        return bool(self.settings.jellyfin_url.strip() and self.settings.jellyfin_api_key.strip())

    async def refresh_library(self) -> None:
        response = await self.client.post(
            f"{self.settings.jellyfin_url.strip().rstrip('/')}/Library/Refresh",
            headers=self.headers,
            # Keep the API key on the configured server, even if it redirects.
            follow_redirects=False,
        )
        response.raise_for_status()
