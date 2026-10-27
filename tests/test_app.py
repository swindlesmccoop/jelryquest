import asyncio

import httpx
import pytest

from backend.config import Settings
from backend.downloads import (
    DownloadManager,
    DownloadSource,
    resolve_dest_filename,
    sanitize_filename,
)
from backend.links import classify_link, magnet_info_hash, parse_pasted_link
from backend.models import DownloadState
from backend.rd import (
    CdnPreference,
    RealDebridClient,
    is_cdn_link,
    is_panel_link,
    pick_link_and_filename,
)
from backend.stremio import StremioClient, parse_torrentio_stream_url

HASH = "a" * 40
MANIFEST = "https://torrentio.strem.fun/realdebrid=TOK123/manifest.json"


def make_settings(tmp_path, **kw) -> Settings:
    return Settings(
        _env_file=None,
        stremio_addon_manifest_url=MANIFEST,
        download_dir=str(tmp_path),
        cache_dir=str(tmp_path / "cache"),
        **kw,
    )


def test_settings(tmp_path):
    s = make_settings(tmp_path, download_dir_series=str(tmp_path / "tv"))
    assert s.addon_base == "https://torrentio.strem.fun/realdebrid=TOK123"
    assert s.rd_token == "TOK123"
    assert s.download_path_for("movie") == str(tmp_path)
    assert s.download_path_for("series") == str(tmp_path / "tv")
    assert make_settings(tmp_path, real_debrid_api_token="X").rd_token == "X"


def test_rd_urls():
    assert is_panel_link("https://real-debrid.com/d/ABC123")
    assert not is_panel_link("https://example.com/d/ABC123")
    assert is_cdn_link("https://45.download.real-debrid.cloud/d/X/f.mkv")
    assert not is_cdn_link("https://example.com/f.mkv")


def test_cdn_preference():
    url = "https://20.download.real-debrid.com:8443/d/X/f.mkv"
    assert CdnPreference().apply(url) == url
    pref = CdnPreference("nyk1")
    assert pref.apply(url) == "https://nyk1.download.real-debrid.com:8443/d/X/f.mkv"
    assert pref.apply("https://example.com/x") == "https://example.com/x"
    runtime = CdnPreference()
    runtime.runtime = "sea1.download.real-debrid.com"
    assert "sea1.download" in runtime.apply(url)
    pinned = CdnPreference("nyk1")
    pinned.runtime = "sea1.download.real-debrid.com"
    assert pinned.active == "nyk1.download.real-debrid.com"


def test_pick_link_among_selected():
    info = {
        "files": [
            {"id": 1, "path": "/a/one.mkv", "selected": 0},
            {"id": 2, "path": "/a/two.mkv", "selected": 1},
            {"id": 3, "path": "/a/three.mkv", "selected": 1},
        ],
        "links": ["L2", "L3"],
    }
    assert pick_link_and_filename(info, 2) == ("L3", "three.mkv")
    assert pick_link_and_filename(info, 1) == ("L2", "two.mkv")
    assert pick_link_and_filename(info, None)[0] == "L2"
    with pytest.raises(ValueError):
        pick_link_and_filename({"links": []}, None)


def test_torrentio_url():
    url = f"https://torrentio.strem.fun/resolve/realdebrid/TOK/{HASH}/null/3/My%20Movie.mkv"
    assert parse_torrentio_stream_url(url) == {
        "info_hash": HASH,
        "file_idx": 3,
        "filename": "My Movie.mkv",
    }
    assert parse_torrentio_stream_url("https://example.com/x") == {}


def test_links():
    assert classify_link(f"magnet:?xt=urn:btih:{HASH}") == "magnet"
    assert classify_link(HASH) == "magnet"
    assert classify_link("https://real-debrid.com/d/ABC") == "rd"
    assert classify_link("https://x.com/f.mkv") == "ddl"
    with pytest.raises(ValueError):
        classify_link("nope")
    assert magnet_info_hash(f"magnet:?xt=urn:btih:{HASH.upper()}") == HASH
    link = parse_pasted_link(f"magnet:?xt=urn:btih:{HASH}&dn=Some+Movie")
    assert (link.info_hash, link.title) == (HASH, "Some Movie")
    assert parse_pasted_link("https://x.com/a/File+Name.mkv").title == "File Name"


def test_filenames():
    assert sanitize_filename('a<b>:"c"/d') == "abcd"
    assert resolve_dest_filename("Movie", "https://x/y.mp4", None, "t") == "Movie.mp4"
    assert resolve_dest_filename("M.mkv", "https://x/y.mp4", None, "t") == "M.mkv"
    assert resolve_dest_filename(None, "https://x/", "video/mp4", "Title") == "Title.mp4"


def stream_payload():
    return {
        "streams": [
            {"name": "RD", "title": "Movie\n1GB", "url": f"https://torrentio.strem.fun/resolve/realdebrid/T/{HASH}/null/0/m.mkv"},
            {"name": "DDL", "title": "x", "url": "https://cdn.example.com/f.mkv"},
        ]
    }


async def test_stremio_streams_and_cache(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "/stream/movie/" in request.url.path:
            return httpx.Response(200, json=stream_payload())
        if "/catalog/" in request.url.path:
            return httpx.Response(200, json={"metas": [{"id": "tt1", "name": "N", "year": 2020}]})
        return httpx.Response(200, json={"meta": {"videos": [
            {"season": 1, "episode": 2, "title": "B"},
            {"season": 1, "episode": 1, "title": "A"},
            {"season": 0},
        ]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        st = StremioClient(make_settings(tmp_path), http)
        streams = await st.get_streams("1")
        assert streams[0].info_hash == HASH and streams[0].magnet and streams[0].ddl is None
        assert streams[1].ddl == "https://cdn.example.com/f.mkv"
        await st.get_streams("tt1")
        assert sum("/stream/" in c for c in calls) == 1  # cached
        assert (await st.search("n"))[0].year == "2020"
        assert [e.episode for e in await st.get_episodes("tt1")] == [1, 2]


async def test_download_direct_and_magnet(tmp_path):
    body = b"x" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "api.real-debrid.com":
            if path.endswith("/addMagnet"):
                return httpx.Response(200, json={"id": "T1"})
            if "/selectFiles/" in path:
                return httpx.Response(204)
            if "/torrents/info/" in path:
                return httpx.Response(200, json={
                    "status": "downloaded",
                    "files": [{"id": 1, "path": "/v.mkv", "selected": 1}],
                    "links": ["https://real-debrid.com/d/ABC"],
                })
            if path.endswith("/unrestrict/link"):
                return httpx.Response(200, json={"download": "https://20.download.real-debrid.com/d/ABC/v.mkv"})
        if request.url.host.endswith("real-debrid.com") or request.url.host == "cdn.example.com":
            return httpx.Response(200, content=body, headers={"content-length": "1000"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        s = make_settings(tmp_path)
        rd = RealDebridClient(s, http, CdnPreference())
        dm = DownloadManager(s, http, rd)

        job = dm.enqueue(DownloadSource(direct_url="https://cdn.example.com/a.mkv"), title="A", content_type="movie")
        job2 = dm.enqueue(DownloadSource(info_hash=HASH), title="B", content_type="movie")
        await asyncio.gather(*dm._tasks)

        assert job.state == DownloadState.done and job.bytes_received == 1000
        assert (tmp_path / "a.mkv").read_bytes() == body
        assert job2.state == DownloadState.done, job2.error
        assert (tmp_path / "v.mkv").exists()
        assert not list(tmp_path.glob("*.part"))


async def test_enqueue_validation(tmp_path):
    async with httpx.AsyncClient() as http:
        s = Settings(_env_file=None, stremio_addon_manifest_url="https://x/manifest.json", download_dir=str(tmp_path))
        dm = DownloadManager(s, http, RealDebridClient(s, http, CdnPreference()))
        with pytest.raises(ValueError):
            dm.enqueue(DownloadSource(), title="t", content_type="movie")
        with pytest.raises(ValueError, match="token"):
            dm.enqueue(DownloadSource(info_hash=HASH), title="t", content_type="movie")
