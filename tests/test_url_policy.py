import pytest

from vexic.url_policy import require_http_url


def test_require_http_url_accepts_http_and_https_case_insensitively() -> None:
    assert require_http_url("base_url", " HTTPS://api.example.test/ ") == "HTTPS://api.example.test"


@pytest.mark.parametrize("url", ["file:///tmp/vexic", "https:///missing-host", "api.example.test"])
def test_require_http_url_rejects_non_http_or_hostless_urls(url: str) -> None:
    with pytest.raises(ValueError, match="base_url.*http"):
        require_http_url("base_url", url)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.test",
        "HTTP://api.example.test:8080/v1",
        "http://192.168.1.10:8000",
    ],
)
def test_require_http_url_rejects_cleartext_http_for_non_loopback_hosts(url: str) -> None:
    # A bearer API key and memory queries travel over this URL; cleartext is
    # only acceptable when the traffic never leaves the machine.
    with pytest.raises(ValueError, match="base_url.*https"):
        require_http_url("base_url", url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ],
)
def test_require_http_url_accepts_cleartext_http_for_loopback_hosts(url: str) -> None:
    assert require_http_url("base_url", url) == url
