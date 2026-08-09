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
