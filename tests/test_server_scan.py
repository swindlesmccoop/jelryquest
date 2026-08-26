import asyncio
from unittest.mock import AsyncMock, patch

from backend import server_scan
from backend.rd_urls import apply_preferred_cdn, normalize_cdn_host, set_runtime_preferred_cdn


def test_normalize_cdn_host_short_name():
    assert normalize_cdn_host("nyk7-4") == "nyk7-4.download.real-debrid.com"


def test_normalize_cdn_host_full_name():
    host = "45.download.real-debrid.cloud"
    assert normalize_cdn_host(host) == host


def test_apply_preferred_cdn_rewrites_host(monkeypatch):
    monkeypatch.setenv("REAL_DEBRID_PREFERRED_CDN", "45.download.real-debrid.cloud")
    set_runtime_preferred_cdn("")
    url = "https://den1-4.download.real-debrid.com/d/TOKEN/file.mkv"
    rewritten = apply_preferred_cdn(url)
    assert rewritten.startswith("https://45.download.real-debrid.cloud/")


def test_apply_preferred_cdn_runtime_override(monkeypatch):
    monkeypatch.delenv("REAL_DEBRID_PREFERRED_CDN", raising=False)
    set_runtime_preferred_cdn("lon1.download.real-debrid.com")
    url = "https://den1-4.download.real-debrid.com/d/TOKEN/file.mkv"
    rewritten = apply_preferred_cdn(url)
    assert rewritten.startswith("https://lon1.download.real-debrid.com/")


def test_benchmark_hosts_sorts_by_speed():
    client = AsyncMock()

    async def fake_probe(_client, host, _seconds, _connect_to):
        speeds = {
            "slow.download.real-debrid.com": 10.0,
            "fast.download.real-debrid.com": 100.0,
        }
        mbps = speeds.get(host, 1.0)
        return {"host": host, "ok": True, "mbps": mbps}

    async def run() -> None:
        with patch.object(server_scan, "_probe_host", side_effect=fake_probe):
            results = await server_scan.benchmark_hosts(
                client,
                ["slow.download.real-debrid.com", "fast.download.real-debrid.com"],
                seconds=1.0,
                workers=2,
                connect_to=1.0,
            )
        assert results[0]["host"] == "fast.download.real-debrid.com"

    asyncio.run(run())


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(server_scan, "CACHE_DIR", str(tmp_path))
    server_scan._write_cache(
        "rd-best-cdn.json",
        {"host": "45.download.real-debrid.cloud", "mbps": 500.0, "at": 1_700_000_000.0},
    )
    monkeypatch.setenv("REAL_DEBRID_SCAN_CACHE_TTL_SEC", "86400")
    loaded = server_scan._load_cache("rd-best-cdn.json")
    assert loaded is not None
    assert loaded["host"] == "45.download.real-debrid.cloud"
