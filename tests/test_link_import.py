import pytest

from backend.link_import import (
    classify_pasted_link,
    magnet_info_hash,
    title_from_magnet,
    title_from_url,
)


def test_classify_magnet_uri():
    assert classify_pasted_link("magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789") == "magnet"


def test_classify_bare_hash():
    assert classify_pasted_link("ABCDEF0123456789ABCDEF0123456789") == "magnet"


def test_classify_rd_page():
    assert classify_pasted_link("https://real-debrid.com/d/ABC123") == "rd"


def test_classify_rd_download_host():
    assert (
        classify_pasted_link("https://download.real-debrid.com/d/TOKEN/file.mkv")
        == "ddl"
    )


def test_classify_generic_ddl():
    assert classify_pasted_link("https://example.com/movie.mkv") == "ddl"


def test_classify_empty_raises():
    with pytest.raises(ValueError, match="empty"):
        classify_pasted_link("  ")


def test_magnet_info_hash_from_uri():
    assert magnet_info_hash(
        "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789&dn=Test"
    ) == "abcdef0123456789abcdef0123456789"


def test_title_from_magnet():
    assert title_from_magnet(
        "magnet:?xt=urn:btih:abc&dn=My+Movie+2024"
    ) == "My Movie 2024"


def test_title_from_url():
    assert title_from_url("https://host/path/Cool.Film.2024.mkv") == "Cool.Film.2024"
