import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

from backend.config import Settings
from backend.stremio_client import StremioClient


def _settings():
    return Settings(
        stremio_addon_manifest_url=(
            "https://torrentio.strem.fun/realdebrid=test/manifest.json"
        ),
        expected_egress_ip="1.2.3.4",
        cinemeta_base="https://v3-cinemeta.strem.io",
    )


def test_search_series_catalog_url():
    settings = _settings()
    mock_client = AsyncMock()
    client = StremioClient(settings, mock_client)
    mock_client.get.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(),
        json=MagicMock(return_value={"metas": []}),
    )

    asyncio.run(client.search_series("breaking bad"))

    called_url = mock_client.get.call_args[0][0]
    assert "/catalog/series/top/search=" in called_url
    assert quote("breaking bad") in called_url


def test_get_series_streams_url():
    settings = _settings()
    mock_client = AsyncMock()
    client = StremioClient(settings, mock_client, rd_client=None)
    mock_client.get.return_value = MagicMock(
        status_code=200,
        raise_for_status=MagicMock(),
        json=MagicMock(return_value={"streams": []}),
    )

    async def run():
        with patch.object(client, "_enrich_fast_path", new_callable=AsyncMock) as enrich:
            enrich.return_value = []
            await client.get_series_streams("tt0903747", 1, 1)

    asyncio.run(run())

    called_url = mock_client.get.call_args[0][0]
    assert called_url.endswith("/stream/series/tt0903747:1:1.json")
