from backend.rd_urls import is_cdn_link, is_panel_link, is_realdebrid_url


def test_panel_link():
    assert is_panel_link("https://real-debrid.com/d/ABC123")


def test_cdn_link():
    assert is_cdn_link("https://download.real-debrid.com/d/TOKEN/file.mkv")


def test_realdebrid_url():
    assert is_realdebrid_url("https://real-debrid.com/d/ABC123")
    assert not is_realdebrid_url("https://example.com/file.mkv")
