from backend.downloader import resolve_dest_filename, sanitize_filename


def test_resolve_dest_filename_uses_hint():
    name = resolve_dest_filename(
        "Phenomenon 1996 1080p BluRay HEVC x265 5.1 BONE.mkv",
        "https://download.real-debrid.com/d/TOKEN/file.mkv",
        "video/x-matroska",
        "Phenomenon",
    )
    assert name == sanitize_filename("Phenomenon 1996 1080p BluRay HEVC x265 5.1 BONE.mkv")


def test_resolve_dest_filename_from_url():
    name = resolve_dest_filename(
        None,
        "https://cdn.example.com/movies/Cool.Film.2024.1080p.mkv",
        None,
        "Cool",
    )
    assert name == "Cool.Film.2024.1080p.mkv"


def test_resolve_dest_filename_not_movie_title():
    name = resolve_dest_filename(
        "Phenomenon 1996 1080p BluRay HEVC x265 5.1 BONE.mkv",
        "https://cdn.example.com/d/abc",
        "video/x-matroska",
        "Phenomenon",
    )
    assert name != "Phenomenon.mkv"
