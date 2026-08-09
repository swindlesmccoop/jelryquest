from backend.config import Settings


def test_download_path_for_movies_and_series():
    settings = Settings(
        stremio_addon_manifest_url="https://torrentio.strem.fun/manifest.json",
        expected_egress_ip="1.2.3.4",
        download_dir_movies="/downloads/movies",
        download_dir_series="/downloads/series",
    )
    assert settings.download_path_for("movie") == "/downloads/movies"
    assert settings.download_path_for("series") == "/downloads/series"


def test_download_path_series_falls_back_to_movies():
    settings = Settings(
        stremio_addon_manifest_url="https://torrentio.strem.fun/manifest.json",
        expected_egress_ip="1.2.3.4",
        download_dir_movies="/downloads/movies",
    )
    assert settings.download_path_for("series") == "/downloads/movies"


def test_download_path_legacy_download_dir():
    settings = Settings(
        stremio_addon_manifest_url="https://torrentio.strem.fun/manifest.json",
        expected_egress_ip="1.2.3.4",
        download_dir="/downloads",
    )
    assert settings.download_path_for("movie") == "/downloads"
    assert settings.download_path_for("series") == "/downloads"
