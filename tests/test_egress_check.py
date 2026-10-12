from backend.egress_check import parse_ip_response


def test_parse_plain_text():
    assert parse_ip_response("203.0.113.10\n", "https://api.ipify.org") == "203.0.113.10"


def test_parse_json():
    url = "https://api.ipify.org?format=json"
    assert parse_ip_response('{"ip":"203.0.113.10"}', url) == "203.0.113.10"


def test_parse_empty_raises():
    try:
        parse_ip_response("", "https://api.ipify.org")
        raise AssertionError("Expected ValueError")
    except ValueError:
        pass


def test_disabled_when_no_expected_ip():
    import asyncio

    from backend.config import Settings
    from backend.egress_check import verify_egress_on_startup

    settings = Settings(stremio_addon_manifest_url="https://x/manifest.json", expected_egress_ip="")
    result = asyncio.run(verify_egress_on_startup(settings))
    assert result.ok
    assert result.message == "Egress check disabled"


def test_resolvers_opt_in(monkeypatch, tmp_path):
    from backend import egress_check

    opened = []
    monkeypatch.setattr("builtins.open", lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(OSError()))
    monkeypatch.delenv("PUBLIC_RESOLVERS", raising=False)
    egress_check.ensure_public_resolvers()
    assert not opened
    monkeypatch.setenv("PUBLIC_RESOLVERS", "1")
    egress_check.ensure_public_resolvers()
    assert opened
