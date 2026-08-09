from backend.config import Settings
from backend.real_debrid import (
    build_magnet,
    extract_rd_token_from_manifest,
)


def test_build_magnet_from_hash():
    assert build_magnet("ABCDEF0123456789") == (
        "magnet:?xt=urn:btih:abcdef0123456789"
    )


def test_build_magnet_preserves_full_magnet():
    magnet = "magnet:?xt=urn:btih:abc&dn=test"
    assert build_magnet(magnet) == magnet


def test_extract_rd_token_from_manifest():
    url = (
        "https://torrentio.strem.fun/realdebrid=MYTOKEN/"
        "providers=yts,eztv/manifest.json"
    )
    assert extract_rd_token_from_manifest(url) == "MYTOKEN"


def test_extract_rd_token_returns_none_when_missing():
    assert extract_rd_token_from_manifest(
        "https://torrentio.strem.fun/manifest.json"
    ) is None


def test_settings_resolved_rd_token_from_env():
    settings = Settings(
        stremio_addon_manifest_url="https://torrentio.strem.fun/manifest.json",
        expected_egress_ip="1.2.3.4",
        real_debrid_api_token="explicit-token",
    )
    assert settings.resolved_rd_token == "explicit-token"


def test_settings_resolved_rd_token_from_manifest_fallback():
    settings = Settings(
        stremio_addon_manifest_url=(
            "https://torrentio.strem.fun/realdebrid=fallback-token/manifest.json"
        ),
        expected_egress_ip="1.2.3.4",
    )
    assert settings.resolved_rd_token == "fallback-token"
