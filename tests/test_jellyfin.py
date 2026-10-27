import httpx
import pytest

from backend.config import Settings
from backend.main import create_app


@pytest.mark.parametrize(
    "upstream_status, expected_status, detail",
    [(204, 200, None), (401, 502, "denied access"), (403, 502, "denied access"),
     (500, 502, "HTTP 500"), (302, 502, "HTTP 302"), (None, 502, "Could not reach")],
)
async def test_jellyfin_refresh(tmp_path, monkeypatch, upstream_status, expected_status, detail):
    requests = []

    def handler(request):
        requests.append(request)
        if upstream_status is None:
            raise httpx.ConnectError("private connection detail", request=request)
        return httpx.Response(upstream_status, headers={"Location": "https://other.example"})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.main.httpx.AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = Settings(
        _env_file=None, stremio_addon_manifest_url="https://example.com/manifest.json",
        real_debrid_auto_scan=False, cache_dir=str(tmp_path),
        jellyfin_url="https://jellyfin.example/jellyfin/", jellyfin_api_key="SECRET",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with async_client(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            health = (await client.get("/api/health")).json()
            assert health["jellyfin_configured"] is True
            assert "SECRET" not in str(health)
            response = await client.post("/api/jellyfin/refresh")
            assert response.status_code == expected_status
            if detail:
                assert detail in response.json()["detail"]
            else:
                assert response.json() == {"status": "accepted"}
            assert "SECRET" not in response.text
            assert "private connection detail" not in response.text
            # The server credential must not become a shared client header.
            assert "X-MediaBrowser-Token" not in app.state.stremio.client.headers
    assert len(requests) == 1  # Redirects must never forward the API key.
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://jellyfin.example/jellyfin/Library/Refresh"
    assert requests[0].headers["X-MediaBrowser-Token"] == "SECRET"
    assert "Authorization" not in requests[0].headers


@pytest.mark.parametrize("url, key", [("", ""), ("https://jellyfin.example", ""), ("", "SECRET")])
async def test_jellyfin_not_configured(tmp_path, url, key):
    settings = Settings(
        _env_file=None, stremio_addon_manifest_url="https://example.com/manifest.json",
        real_debrid_auto_scan=False, cache_dir=str(tmp_path),
        jellyfin_url=url, jellyfin_api_key=key,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/api/health")).json()["jellyfin_configured"] is False
            response = await client.post("/api/jellyfin/refresh")
            assert response.status_code == 503
            assert "JELLYFIN_URL and JELLYFIN_API_KEY" in response.json()["detail"]
