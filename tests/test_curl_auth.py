from pathlib import Path

import pytest

from aviasales_search.curl_auth import (
    CurlAuth,
    ExpiredCurlError,
    classify_response,
    parse_curl_auth,
)

FIXTURE = Path(__file__).parent / "fixtures" / "curl_sample.txt"


def test_parse_curl_auth_extracts_headers_and_cookies():
    auth = parse_curl_auth(FIXTURE.read_text())

    assert isinstance(auth, CurlAuth)
    assert auth.headers["accept"] == "application/json"
    assert auth.headers["content-type"] == "application/json"
    assert auth.headers["x-aws-waf-token"] == "PLACEHOLDER_WAF_TOKEN"
    assert auth.headers["x-client-type"] == "web"
    assert auth.headers["origin"] == "https://www.aviasales.ru"

    assert auth.cookies["auid"] == "PLACEHOLDER"
    assert auth.cookies["marker"] == "PLACEHOLDER"
    assert auth.cookies["nuid"] == "PLACEHOLDER"
    assert auth.cookies["uxs_uid"] == "PLACEHOLDER"


def test_parse_curl_auth_ignores_url_body_and_method():
    auth = parse_curl_auth(FIXTURE.read_text())

    # URL, HTTP method and request body are not exposed on CurlAuth at all —
    # R2 only extracts headers/cookies, R4 builds the body itself.
    assert not hasattr(auth, "url")
    assert not hasattr(auth, "body")
    assert not hasattr(auth, "method")


def test_parse_curl_auth_ignores_header_without_colon():
    # Chrome's "Copy as cURL" emits `-H 'authorization;'` for empty headers
    # (no colon) — must not crash and must not leak a bogus header entry.
    auth = parse_curl_auth(FIXTURE.read_text())
    assert "authorization;" not in auth.headers
    assert "authorization" not in auth.headers


def test_parse_curl_auth_prefers_explicit_cookie_flag_over_cookie_header():
    text = (
        "curl 'https://x' "
        "-H 'cookie: foo=bar' "
        "-b 'auid=PLACEHOLDER; marker=direct'"
    )
    auth = parse_curl_auth(text)
    assert auth.cookies == {"auid": "PLACEHOLDER", "marker": "direct"}


def test_parse_curl_auth_falls_back_to_cookie_header_when_no_dash_b():
    text = "curl 'https://x' -H 'cookie: auid=PLACEHOLDER; marker=direct'"
    auth = parse_curl_auth(text)
    assert auth.cookies == {"auid": "PLACEHOLDER", "marker": "direct"}


def test_parse_curl_auth_truncated_flag_raises_valueerror():
    with pytest.raises(ValueError, match="флага .* нет значения"):
        parse_curl_auth("curl 'https://x' -H")


def test_parse_curl_auth_truncated_cookie_flag_raises_valueerror():
    with pytest.raises(ValueError, match="флага .* нет значения"):
        parse_curl_auth("curl 'https://x' -b")


def test_parse_curl_auth_requires_curl_command():
    with pytest.raises(ValueError, match="curl"):
        parse_curl_auth("wget 'https://x'")


@pytest.mark.parametrize(
    "status,text",
    [
        (401, ""),
        (403, ""),
        (429, ""),
        (500, ""),
        (503, "Service Unavailable"),
        (200, "Just a moment... captcha"),
        (200, "Attention Required! | Cloudflare"),
    ],
)
def test_classify_response_raises_on_block(status, text):
    with pytest.raises(ExpiredCurlError):
        classify_response(status, text)


def test_classify_response_ok_on_success():
    classify_response(200, '{"search_id":"x"}')  # не кидает
