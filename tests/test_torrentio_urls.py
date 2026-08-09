from backend.torrentio_urls import is_torrentio_resolve_url, parse_torrentio_stream_url


def test_parse_rd_resolve_url():
    url = (
        "https://torrentio.strem.fun/resolve/realdebrid/TOKEN/"
        "59e11cef8c2152ac73681092844ebd3db19025bc/null/0/"
        "Phenomenon.1996.1080p.BluRay.HEVC.x265.mkv"
    )
    parsed = parse_torrentio_stream_url(url)
    assert parsed["info_hash"] == "59e11cef8c2152ac73681092844ebd3db19025bc"
    assert parsed["file_idx"] == 0
    assert "Phenomenon.1996" in (parsed["filename"] or "")


def test_is_torrentio_resolve_url():
    assert is_torrentio_resolve_url(
        "https://torrentio.strem.fun/resolve/realdebrid/x/abc/null/0/file.mkv"
    )
    assert not is_torrentio_resolve_url("https://example.com/file.mkv")
