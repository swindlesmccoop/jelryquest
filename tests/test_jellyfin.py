import httpx
import pytest

from backend.config import Settings
from backend.main import create_app
from backend.jellyfin import JellyfinClient


@pytest.mark.parametrize(
    "upstream_status, expected_status, detail",
    [(204, 200, None), (401, 502, "key was rejected or the authentication header format is unsupported"),
     (403, 502, "lacks administrator rights"),
     (500, 502, "HTTP 500"), (302, 502, "HTTP 302"), (None, 502, "Could not reach")],
)
async def test_jellyfin_refresh(tmp_path, monkeypatch, caplog, upstream_status, expected_status, detail):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[])
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
                assert response.json()["status"] == "accepted"
                assert response.json()["state"] == "starting"
            assert "SECRET" not in response.text
            assert "private connection detail" not in response.text
            # The server credential must not become a shared client header.
            assert "Authorization" not in app.state.stremio.client.headers
    assert len(requests) == 2  # Status check, then refresh; no redirects.
    assert requests[0].method == "GET"
    requests = requests[1:]
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://jellyfin.example/jellyfin/Library/Refresh"
    assert requests[0].headers["Authorization"] == 'MediaBrowser Token="SECRET"'
    assert "api_key" not in requests[0].url.params
    assert "SECRET" not in caplog.text


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
            assert (await client.get("/api/jellyfin/status")).json() == {
                "configured": False, "state": "unknown", "progress": None,
            }


@pytest.mark.parametrize("task, expected", [
    ({"Name": "Scan Media Library", "State": "Running", "CurrentProgressPercentage": 87.264},
     {"state": "Running", "progress": 87.264}),
    ({"Name": "Scan Media Library", "State": "Idle", "CurrentProgressPercentage": 99},
     {"state": "Idle", "progress": None}),
    ({"Name": "Scan Media Library", "State": "Cancelling"}, {"state": "Cancelling", "progress": None}),
    ({"Name": "Other", "State": "Running"}, {"state": "unknown", "progress": None}),
    ({"Name": "Scan Media Library", "State": "Running", "CurrentProgressPercentage": "bad"},
     {"state": "Running", "progress": None}),
    ({"Name": "Scan Media Library", "State": "Running", "CurrentProgressPercentage": 101},
     {"state": "Running", "progress": 100.0}),
])
async def test_scan_status(task, expected):
    def handler(request):
        assert request.url.path == "/jellyfin/ScheduledTasks"
        assert request.headers["Authorization"] == 'MediaBrowser Token="SECRET"'
        return httpx.Response(200, json=[{"Name": "Unrelated", "State": "Running"}, task])

    settings = Settings(_env_file=None, stremio_addon_manifest_url="https://example.com",
                        jellyfin_url="https://jellyfin.example/jellyfin/", jellyfin_api_key="SECRET")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await JellyfinClient(settings, client).scan_status() == expected


@pytest.mark.parametrize("state, should_post", [("Running", False), ("Cancelling", False), ("Idle", True), ("error", True)])
async def test_refresh_guard(tmp_path, state, should_post):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(204)
        if state == "error":
            return httpx.Response(401)
        return httpx.Response(200, json=[{"Name": "Scan Media Library", "State": state,
                                             "CurrentProgressPercentage": 42.5}])

    settings = Settings(_env_file=None, stremio_addon_manifest_url="https://example.com",
                        real_debrid_auto_scan=False, cache_dir=str(tmp_path),
                        jellyfin_url="https://jellyfin.example", jellyfin_api_key="SECRET")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            app.state.jellyfin.client = upstream
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                status = await client.get("/api/jellyfin/status")
                assert status.status_code == (502 if state == "error" else 200)
                response = await client.post("/api/jellyfin/refresh")
                assert response.status_code == 200
                if not should_post:
                    assert response.json()["already_running"] is True
                    assert response.json()["state"] == state
    assert any(r.method == "POST" for r in requests) is should_post
